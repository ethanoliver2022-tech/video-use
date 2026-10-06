"""PumpPortal accepting the connection but refusing the paid trade feed (e.g. the wallet
linked to the key is below its minimum balance): the bot must notice, fall back to on-chain
checks, tell the owner, and recover by itself."""
import asyncio
import json

from solders.keypair import Keypair

from sniper.models import Position
from sniper.scanners.pumpportal import PumpPortalStream
from tests.test_telegram import Harness

REFUSAL = json.dumps({"errors": "Minimum balance not met for PumpSwap websocket data."})


def trade(mint):
    return json.dumps({"txType": "buy", "mint": mint, "traderPublicKey": str(Keypair().pubkey()),
                       "solAmount": 0.5, "signature": str(Keypair().pubkey())})


async def test_refused_feed_is_noticed_and_recovery_too():
    s = PumpPortalStream("wss://x", True, False, api_key="k")
    changes = []
    s.on_feed_change = lambda ok, err: changes.append((ok, err))
    assert s.trades_live
    await s._dispatch(json.dumps({"errors": "Too many requests, slow down"}))  # not a refusal
    assert s.trades_live and not changes
    await s._dispatch(REFUSAL)
    assert not s.trades_live and changes == [(False, "Minimum balance not met for PumpSwap "
                                                      "websocket data.")]
    await s._dispatch(REFUSAL)                       # repeats: one notice, not one per message
    assert len(changes) == 1
    await s._dispatch(trade(str(Keypair().pubkey())))  # trades flowing again
    assert s.trades_live and changes[-1] == (True, "")


async def test_without_a_key_nothing_is_refused():
    s = PumpPortalStream("wss://x", True, False, api_key="")
    await s._dispatch(REFUSAL)
    assert s.feed_ok and not s.trades_live


def _held(eng, creator):
    m = str(Keypair().pubkey())
    pos = Position(mint=m, symbol="T", source="pumpfun", creator=creator, entry_price=1e-6,
                   tokens_initial=1000.0, tokens_remaining=1000.0, sol_in=0.05, route="pump")
    eng.positions[m] = pos
    return pos


async def test_refused_feed_falls_back_to_on_chain_checks_and_tells_the_owner(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    pos = _held(eng, str(Keypair().pubkey()))
    reads = {"curve": 0, "dev": 0}

    async def curve(rpc, mint):
        reads["curve"] += 1
    async def dev_bal(owner, mint):
        reads["dev"] += 1
        return 0.0 if reads["dev"] > 1 else 1000.0  # the dev's tokens leave after the first look
    import sniper.engine as em
    em_fetch = em.fetch_curve
    em.fetch_curve = curve
    eng.rpc.get_token_balance = dev_bal
    try:
        await eng.stream._dispatch(REFUSAL)
        await eng.settle()
        assert not eng.has_trade_stream
        assert any("refused the live trade feed" in t for t, _, _ in h.sent)
        await eng._poll_position(pos)                # the price comes from the chain...
        await eng._poll_position(pos)                # ...and so does the dev's balance
        assert reads["curve"] >= 1 and pos.dev_sold
        await h.text("/menu")
        assert "refusing the live trade feed" in h.last
        await eng.stream._dispatch(trade(pos.mint))  # topped up: trades arrive again
        await eng.settle()
        assert eng.has_trade_stream
        assert any("working again" in t for t, _, _ in h.sent)
    finally:
        em.fetch_curve = em_fetch
    await h.close()


async def test_dev_wallet_is_watched_even_with_a_live_feed(tmp_path, monkeypatch):
    """The feed shows the dev's own sells, not tokens moved to another wallet and sold there."""
    import sniper.engine as em
    h = Harness(tmp_path)
    eng = h.eng
    assert eng.has_trade_stream
    pos = _held(eng, str(Keypair().pubkey()))
    pos.last_update = __import__("time").time()      # not quiet: no curve polling needed
    calls = []

    async def dev_bal(owner, mint):
        calls.append(owner)
        return 1000.0 if len(calls) == 1 else 0.0    # moved out of the dev wallet
    eng.rpc.get_token_balance = dev_bal
    await eng._poll_position(pos)
    await eng._poll_position(pos)                    # within 6s: not checked again (spare RPC)
    assert len(calls) == 1 and not pos.dev_sold
    monkeypatch.setattr(em, "DEV_CHECK_STREAM_SECONDS", 0.0)
    await eng._poll_position(pos)
    assert len(calls) == 2 and pos.dev_sold          # caught without any trade on the feed
    await h.close()


async def test_refused_feed_is_asked_for_again(tmp_path, monkeypatch):
    import sniper.engine as em
    monkeypatch.setattr(em, "FEED_RETRY_SECONDS", 0.01)
    h = Harness(tmp_path)
    eng = h.eng
    eng.stream.token_subs.add(str(Keypair().pubkey()))
    sent = []

    async def send(payload):
        sent.append(payload)
    eng.stream._send = send
    task = asyncio.ensure_future(eng.feed_watch())
    await asyncio.sleep(0.05)
    assert not sent                                  # feed fine: nothing to re-ask
    await eng.stream._dispatch(REFUSAL)
    await asyncio.sleep(0.05)
    assert any(p.get("method") == "subscribeTokenTrade" for p in sent)
    task.cancel()
    await h.close()
