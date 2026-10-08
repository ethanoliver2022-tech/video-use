"""Entry gap per copy, the wallet finder, and the panic button."""
import time

import pytest
from solders.keypair import Keypair

from sniper.models import Fill, Position
from sniper.walletfind import find_wallets
from tests import test_copy_upgrade as cu
from tests.test_copy_upgrade import STD, _addr, _buy, _wallet

h = cu.h   # the same fixture


async def test_copy_entry_gap_is_recorded_and_averaged(h):
    eng = h.eng
    w = await _wallet(eng)
    their = 32.0 / (STD / 32)
    paid = {"n": 0}

    async def buy(c, sol, curve):           # our fill: 5% above their price
        paid["n"] += 1
        return Fill(tokens=sol / (their * 1.05), sol=sol)
    eng.executor.buy = buy
    eng.cfg.copytrade.max_chase_pct = 0
    for _ in range(2):
        await eng.handle_copy(_buy(w.address, _addr(), price=their), w)
    r = eng.wallet_results(w.address)
    assert r["gap_n"] == 2 and r["gap"] == pytest.approx(5, abs=0.5)
    assert any("entry +5% vs theirs" in t for t, *_ in h.sent)
    await h.tap(f"wp:{w.address}")
    assert "Entry vs theirs" in h.last and "close to them" in h.last
    await h.close()


async def test_panic_sells_everything_and_pauses(h):
    eng = h.eng
    for m in ("A", "B"):
        eng.positions[m] = Position(mint=m, symbol=m, source="pumpfun", creator=None,
                                    entry_price=1e-8, tokens_initial=100, tokens_remaining=100,
                                    sol_in=0.1)
    sold = []

    async def sell(mint, tokens, sell_all, pump, curve, slippage_pct=None):
        if mint == "B":
            raise RuntimeError("route busy")
        sold.append(mint)
        return Fill(tokens=tokens, sol=0.05, signature="S")
    eng.executor.sell = sell
    await h.tap("panic")
    assert "panic!" in h.buttons()
    out = await eng.panic_sell_all()
    assert eng.paused and sold == ["A"] and eng.positions["A"].closed
    assert not eng.positions["B"].closed and eng.positions["B"].force_sell == "panic sell"
    assert "keeps retrying" in out
    from sniper import exits
    assert exits.evaluate(eng.positions["B"], eng.exit_cfg(eng.positions["B"])).sell_all
    await h.close()


def _swap_tx(signer, mint, sol, tokens_before, tokens_after):
    bal = lambda amt: [{"owner": signer, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}] if amt else []  # noqa: E731
    return {"transaction": {"message": {"accountKeys": [{"pubkey": signer}]}},
            "meta": {"err": None, "fee": 0, "preBalances": [10_000_000_000],
                     "postBalances": [10_000_000_000 + int(sol * 1e9)],
                     "preTokenBalances": bal(tokens_before), "postTokenBalances": bal(tokens_after),
                     "logMessages": []}}


async def test_wallet_finder_ranks_early_buyers_by_what_they_took_out():
    mint, dev, bot, good, meh = (str(Keypair().pubkey()) for _ in range(5))
    t0 = time.time() - 3600
    coin = [("LAUNCH", t0, dev, _swap_tx(dev, mint, -1.0, 0, 1e6)),
            ("B1", t0 + 1, bot, _swap_tx(bot, mint, -2.0, 0, 2e6)),
            ("G1", t0 + 40, good, _swap_tx(good, mint, -1.0, 0, 1e6)),
            ("M1", t0 + 90, meh, _swap_tx(meh, mint, -1.0, 0, 1e6)),
            ("G2", t0 + 900, good, _swap_tx(good, mint, 4.0, 1e6, 0)),     # sold for 4 SOL
            ("M2", t0 + 950, meh, _swap_tx(meh, mint, 0.5, 1e6, 0)),
            ("B2", t0 + 960, bot, _swap_tx(bot, mint, 2.5, 2e6, 0))]
    txs = {s: tx for s, _, _, tx in coin}

    class Rpc:
        async def call(self, method, params):
            if method == "getTransaction":
                return txs.get(params[0])
            addr = params[0]
            rows = [{"signature": s, "blockTime": ts, "err": None}
                    for s, ts, who, _ in reversed(coin) if addr == mint or who == addr]
            return rows
    res = await find_wallets(Rpc(), mint)
    assert res.reached_launch and [b.wallet for b in res.buyers] == [good, meh, bot]
    g = res.buyers[0]
    assert g.pnl == pytest.approx(3.0) and g.secs_after_launch == pytest.approx(40)
    assert res.buyers[2].bot_like and "bot-like" in res.text()
    assert dev not in [b.wallet for b in res.buyers]


async def test_finder_from_chat_offers_to_add_the_wallet(h, monkeypatch):
    from sniper import walletfind
    from sniper.walletfind import EarlyBuyer, FinderResult
    good = _addr()

    async def fake(rpc, mint, exclude=frozenset(), top=8):
        b = EarlyBuyer(good, time.time(), 30, sol_in=1, sol_out=3)
        return FinderResult(mint, time.time(), True, [b], 50)
    monkeypatch.setattr(walletfind, "find_wallets", fake)
    await h.tap("wf")
    await h.text(f"https://pump.fun/coin/{_addr()}")
    assert f"wfa:{good}" in h.buttons() and "+2.00" in h.last
    await h.tap(f"wfa:{good}")
    assert "What should I do with it?" in h.last
    await h.close()
