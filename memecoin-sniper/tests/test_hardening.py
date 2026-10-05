"""Regression tests for the production-hardening pass."""
import asyncio
import json
import struct
import time

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper import engine as engine_mod
from sniper.config import SpeedConfig, load_config
from sniper.engine import Engine
from sniper.execution.executors import LiveExecutor, NothingToSell, NotLanded
from sniper.execution.sender import TxSender, tip_transaction
from sniper.models import Candidate, Fill, Position
from sniper.notify import Notifier
from sniper.pump_curve import PUMP_PROGRAM, bonding_curve_address, parse_curve
from sniper.scanners.multichain import parse_gecko_pools
from sniper.solana_rpc import RpcError, SolanaRpc
from sniper.telegram_bot import TelegramControl

SOL = "So11111111111111111111111111111111111111112"


def wallet():
    return str(Keypair().pubkey())


def curve_bytes(v_tok_raw, v_sol_lamports, complete=False, creator=None):
    data = b"\x17" * 8 + struct.pack("<QQQQQ?", v_tok_raw, v_sol_lamports, 0, 0, 10**15, complete)
    return data + (bytes(Pubkey.from_string(creator)) if creator else b"")


def make_engine(tmp_path, api_key="", **sections) -> Engine:
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = api_key
    cfg.filters.reject_reused_socials = False
    for section, values in sections.items():
        for k, v in values.items():
            setattr(getattr(cfg, section), k, v)
    eng = Engine(cfg, live=False)
    eng.sent_ws = []

    async def fake_ws(payload):
        eng.sent_ws.append(payload)
    eng.stream._send = fake_ws

    async def no_tg(*a, **k):
        pass
    eng.notifier.telegram = no_tg
    return eng


def pump_pos(mint="M", **kw):
    base = dict(mint=mint, symbol="T", source="pumpfun", creator=None, entry_price=1e-7,
                tokens_initial=1e6, tokens_remaining=1e6, sol_in=0.1, route="pump")
    base.update(kw)
    return Position(**base)


# ---------- on-chain bonding curve ----------

def test_curve_pda_and_layout():
    mint = wallet()
    expected, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                              PUMP_PROGRAM)
    assert bonding_curve_address(mint) == str(expected)
    creator = wallet()
    info = parse_curve(curve_bytes(1_000_000_000_000_000, 30_000_000_000, False, creator))
    assert info.v_tokens == pytest.approx(1e9) and info.v_sol == pytest.approx(30.0)
    assert info.price == pytest.approx(3e-8) and not info.complete and info.creator == creator
    assert parse_curve(curve_bytes(1, 1, True)).complete
    assert parse_curve(b"short") is None


# ---------- no PumpPortal key: on-chain fallback ----------

async def test_without_key_no_paid_subscriptions_and_polling_prices(tmp_path):
    eng = make_engine(tmp_path, api_key="")
    assert not eng.has_trade_stream
    await eng.stream.watch_token("X")
    await eng.stream.watch_accounts([wallet()])
    assert eng.sent_ws == []  # never sends billable subscriptions without a key
    with pytest.raises(ValueError, match="PumpPortal API key"):
        await eng.add_copy_wallet(wallet())
    with pytest.raises(ValueError, match="PumpPortal API key"):
        await eng.set_setting("copytrade.enabled", "on")

    dev, mint = wallet(), wallet()
    pos = pump_pos(mint, creator=dev, dev_tokens=5_000_000.0)
    eng.positions[mint] = pos
    state = {"curve": curve_bytes(900_000_000_000_000, 40_000_000_000), "dev": 5_000_000.0}

    async def account_bytes(addr):
        return state["curve"]

    async def token_balance(owner, mint):
        return state["dev"]
    eng.rpc.get_account_bytes, eng.rpc.get_token_balance = account_bytes, token_balance

    await eng._poll_position(pos)
    assert pos.last_price == pytest.approx(40 / 9e8) and not pos.dev_sold
    state["dev"] = 1_000_000.0  # dev dumped
    await eng._poll_position(pos)
    assert pos.dev_sold
    state["curve"] = curve_bytes(1, 1, complete=True)
    await eng._poll_position(pos)
    assert pos.migrated  # graduated -> Jupiter pricing from now on
    await eng.http.aclose()


async def test_onchain_confirmation_window(tmp_path):
    eng = make_engine(tmp_path, entry={"confirm_seconds": 0.01})
    dev = wallet()
    c = Candidate(chain="solana", mint=wallet(), source="pumpfun", creator=dev, v_sol=30.0,
                  v_tokens=1.07e9, creator_initial_buy_tokens=2e7, route="pump")
    state = {"sol": 30_000_000_000, "dev": 2e7}

    async def account_bytes(addr):
        return curve_bytes(1_000_000_000_000_000, state["sol"])

    async def token_balance(owner, mint):
        return state["dev"]
    eng.rpc.get_account_bytes, eng.rpc.get_token_balance = account_bytes, token_balance

    assert any("net flow" in p for p in await eng.confirm_flow(c))  # nobody bought
    state["sol"] = 34_000_000_000
    assert await eng.confirm_flow(c) == []                           # organic inflow
    state["dev"] = 0
    assert "dev sold during confirmation window" in await eng.confirm_flow(c)
    await eng.http.aclose()


# ---------- engine robustness ----------

class SlowExecutor:
    def __init__(self, delay=0.3):
        self.delay, self.sells, self.buys = delay, 0, 0

    async def buy(self, c, sol, curve):
        self.buys += 1
        await asyncio.sleep(self.delay)
        return Fill(tokens=1000.0, sol=sol)

    async def sell(self, mint, tokens, sell_all, pump, curve, slippage_pct=None):
        self.sells += 1
        await asyncio.sleep(self.delay)
        return Fill(tokens=tokens, sol=0.2)

    async def quote_sell(self, mint, tokens):
        return None


async def test_trade_stream_never_waits_on_a_sell(tmp_path):
    eng = make_engine(tmp_path, api_key="k")
    eng.executor = SlowExecutor(delay=0.5)
    dev = wallet()
    eng.positions["M"] = pump_pos(creator=dev)
    t0 = time.perf_counter()
    await eng.on_trade({"mint": "M", "txType": "sell", "traderPublicKey": dev, "solAmount": 1,
                        "signature": "s1"})
    assert time.perf_counter() - t0 < 0.1  # returned immediately; the sell runs in background
    await eng.on_trade({"mint": "M", "txType": "buy", "traderPublicKey": wallet(),
                        "solAmount": 1, "signature": "s2"})
    await eng.settle()
    assert eng.executor.sells == 1 and eng.positions["M"].closed  # no duplicate sell
    await eng.http.aclose()


class FailingExecutor(SlowExecutor):
    def __init__(self, exc):
        super().__init__(0)
        self.exc = exc

    async def sell(self, *a, **k):
        self.sells += 1
        raise self.exc


async def test_failed_sells_back_off_and_never_abandon_early(tmp_path):
    eng = make_engine(tmp_path)
    eng.executor = FailingExecutor(RuntimeError("rpc down"))
    pos = pump_pos(opened_at=time.time() - 10_000)  # max-hold exit wants out
    eng.positions["M"] = pos
    for _ in range(10):
        await eng.check_exit(pos)
        eng._sell_next_try["M"] = 0  # skip the wait in the test
    assert eng.executor.sells == 10 and not pos.closed  # old code gave up after 5
    await eng.check_exit(pos)
    assert eng.executor.sells == 11
    assert eng._sell_next_try["M"] > time.time()  # backing off
    await eng.check_exit(pos)
    assert eng.executor.sells == 11  # respected the backoff
    eng.sell_failures["M"] = engine_mod.WRITE_OFF_AFTER - 1
    eng._sell_next_try["M"] = 0
    await eng.check_exit(pos)
    assert pos.closed and "written off" in pos.close_reason
    await eng.http.aclose()


async def test_nothing_to_sell_closes_position(tmp_path):
    eng = make_engine(tmp_path)
    eng.executor = FailingExecutor(NothingToSell("gone"))
    pos = pump_pos(opened_at=time.time() - 10_000)
    eng.positions["M"] = pos
    await eng.check_exit(pos)
    assert pos.closed and pos.close_reason == "no tokens left in wallet (PnL estimated)"
    assert pos.sol_out == pytest.approx(pos.entry_price * 1e6 * 0.97)  # not booked as -100%
    await eng.http.aclose()


async def test_concurrent_buys_respect_position_limit(tmp_path):
    eng = make_engine(tmp_path, trading={"max_open_positions": 1})
    eng.executor = SlowExecutor(delay=0.2)
    cands = [Candidate(chain="solana", mint=f"M{i}", source="manual", symbol=f"M{i}", force=True)
             for i in range(3)]
    results = await asyncio.gather(*(eng.try_buy(c) for c in cands))
    assert eng.executor.buys == 1
    assert sum(r.startswith("🟢") for r in results) == 1
    assert sum("max open positions" in r for r in results) == 2
    await eng.http.aclose()


async def test_buy_lock_not_held_during_execution(tmp_path):
    eng = make_engine(tmp_path, trading={"max_open_positions": 5})
    eng.executor = SlowExecutor(delay=0.3)
    t0 = time.perf_counter()
    await asyncio.gather(*(eng.try_buy(Candidate(chain="solana", mint=f"P{i}", source="manual",
                                                 symbol="P", force=True)) for i in range(3)))
    assert time.perf_counter() - t0 < 0.6  # ran in parallel, not 3 x 0.3s
    await eng.http.aclose()


def test_stale_timer_only_moves_with_price():
    p = pump_pos()
    p.last_update = 100.0
    p.update_price(p.last_price, ts=200.0)
    assert p.last_update == 100.0  # same price: still counts as "no trades"
    p.update_price(p.last_price * 1.1, ts=300.0)
    assert p.last_update == 300.0


async def test_memory_is_pruned(tmp_path):
    eng = make_engine(tmp_path)
    old = time.time() - 10 * 3600
    for i in range(1000):
        eng.seen[("solana", f"m{i}")] = old
        eng._set_curve(f"m{i}", engine_mod.CurveState(30, 1e9))
        eng._curve_ts[f"m{i}"] = old
    eng.positions["held"] = pump_pos("held")
    eng._set_curve("held", engine_mod.CurveState(30, 1e9))
    eng._curve_ts["held"] = old
    eng.positions["done"] = pump_pos("done", closed=True)
    eng.prune()
    assert not eng.seen and list(eng.curves) == ["held"]
    assert "done" not in eng.positions and "held" in eng.positions
    await eng.http.aclose()


async def test_paused_skips_work_and_other_chain_alerts_are_capped(tmp_path):
    eng = make_engine(tmp_path)
    eng.set_paused(True)
    await eng.on_candidate(Candidate(chain="solana", mint="A", source="pumpfun", creator=wallet()))
    assert eng.queue.empty()
    eng.set_paused(False)
    sent = []

    async def capture(text, *a, **k):
        sent.append(text)
    eng.notifier.send = capture

    async def ok(c):
        from sniper.models import SafetyReport
        return SafetyReport(passed=True)
    eng.safety.evaluate = ok
    for i in range(engine_mod.ALERTS_PER_HOUR + 15):
        await eng.handle_candidate(Candidate(chain="base", mint=f"0x{i}", source="geckoterminal"))
    assert len(sent) == engine_mod.ALERTS_PER_HOUR
    await eng.http.aclose()


async def test_html_in_token_names_is_escaped(tmp_path):
    eng = make_engine(tmp_path)
    eng.executor = SlowExecutor(delay=0)
    sent = []

    async def capture(text, *a, **k):
        sent.append(text)
    eng.notifier.send = capture
    c = Candidate(chain="solana", mint="H", source="manual", symbol='<a href="x">CLICK</a>', force=True)
    await eng.try_buy(c)
    assert "<a href" not in sent[0] and "&lt;a href" in sent[0]
    await eng.http.aclose()


async def test_withdraw_all_is_lamport_exact(tmp_path):
    eng = make_engine(tmp_path)
    eng.wallet.create()
    sent = []

    async def bal(_):
        return 1.234567891

    async def bh():
        return Hash.new_unique()

    async def send(raw):
        sent.append(raw)
        return "SIG"

    async def confirm(sig):
        return True
    eng.rpc.get_balance_sol, eng.rpc.get_latest_blockhash = bal, bh
    eng.rpc.send_raw_transaction, eng.rpc.confirm = send, confirm
    out = await eng.withdraw(wallet(), None)
    assert "1.234562891" in out  # balance minus exactly 5000 lamports
    with pytest.raises(ValueError):
        await eng.withdraw(str(eng.wallet.keypair().pubkey()), 0.1)  # to itself
    await eng.http.aclose()


# ---------- live execution ----------

class FakeRpc:
    def __init__(self):
        self.balance_raw = 0
        self.confirm_result = True
        self.tx = None
        self.confirm_raises = None

    async def get_token_balance_raw(self, owner, mint):
        return self.balance_raw, 6

    async def get_token_balance(self, owner, mint):
        return self.balance_raw / 1e6

    async def confirm(self, sig):
        if self.confirm_raises:
            raise self.confirm_raises
        return self.confirm_result

    async def get_transaction(self, sig):
        return self.tx


class FakeSender:
    def __init__(self):
        self.sent = 0

    async def priority_fee(self):
        return 0.0001

    async def send(self, tx, payer):
        self.sent += 1
        return "SIG"


def live_executor(cfg):
    kp = Keypair()
    rpc = FakeRpc()
    ex = LiveExecutor(cfg, kp, rpc, jupiter=None, http=None, sender=FakeSender())
    ex._sign = lambda unsigned: unsigned
    return ex, rpc


async def test_live_buy_expired_raises_not_landed():
    cfg = load_config(None)
    ex, rpc = live_executor(cfg)

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_result = False
    with pytest.raises(NotLanded):
        await ex.buy(Candidate(chain="solana", mint="M", source="pumpfun", route="pump"), 0.1, None)


async def test_live_buy_rpc_error_after_send_tracks_tokens_that_arrived():
    cfg = load_config(None)
    ex, rpc = live_executor(cfg)

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_raises = RpcError("getSignatureStatuses: HTTP 502")
    rpc.balance_raw = 5_000_000_000  # 5000 tokens arrived anyway
    fill = await ex.buy(Candidate(chain="solana", mint="M", source="pumpfun", route="pump"), 0.1, None)
    assert fill.tokens == 5000 and fill.sol > 0.1  # sol includes the Jito tip


async def test_live_sell_sends_at_most_one_transaction():
    cfg = load_config(None)
    ex, rpc = live_executor(cfg)
    rpc.balance_raw = 1_000_000

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_raises = RpcError("boom")  # uncertain outcome after sending
    with pytest.raises(RpcError):
        await ex.sell("M", 1.0, True, pump=True, curve=None)
    assert ex.sender.sent == 1  # old code fell back to Jupiter and could sell twice

    rpc.balance_raw = 0
    with pytest.raises(NothingToSell):
        await ex.sell("M", 1.0, True, pump=True, curve=None)


async def test_live_sell_falls_back_to_jupiter_only_when_building_fails():
    cfg = load_config(None)
    ex, rpc = live_executor(cfg)
    rpc.balance_raw = 2_500_000
    calls = []

    async def pp(*a, **k):
        raise RuntimeError("pumpportal 500")
    ex._pumpportal_tx = pp

    class J:
        async def quote(self, i, o, amt, slip, raw_amount=None):
            calls.append(raw_amount)
            return {}

        async def swap_tx(self, q, user, fee):
            return b"tx"
    ex.jupiter = J()
    rpc.tx = {"transaction": {"message": {"accountKeys": [{"pubkey": ex.pubkey}]}},
              "meta": {"preBalances": [1_000_000_000], "postBalances": [1_200_000_000],
                       "preTokenBalances": [{"mint": "M", "owner": ex.pubkey,
                                             "uiTokenAmount": {"uiAmount": 2.5}}],
                       "postTokenBalances": []}}
    fill = await ex.sell("M", 99.0, False, pump=True, curve=None)  # more than held -> sell all
    assert calls == [2_500_000]  # exact raw wallet balance, no float rounding
    assert fill.tokens == pytest.approx(2.5) and ex.sender.sent == 1
    assert fill.sol == pytest.approx(0.2 - cfg.speed.jito_tip_sol)


async def test_sender_falls_back_to_rpc_when_jito_fails():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if "jito" in req.url.host:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "SIG"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kp = Keypair()
    sender = TxSender(SpeedConfig(jito_block_engines=["https://x.jito.wtf"]),
                      SolanaRpc("https://rpc.example", http), http, 0.001)
    tx = tip_transaction(kp, 0.0001, Hash.new_unique())
    assert await sender.send(tx, kp) == str(tx.signatures[0])
    assert hosts == ["x.jito.wtf", "rpc.example"]


async def test_rpc_errors_never_leak_the_url_api_key():
    def handler(req):
        return httpx.Response(429, text="slow down")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = SolanaRpc("https://mainnet.helius-rpc.com/?api-key=SECRET123", http)
    with pytest.raises(RpcError) as e:
        await rpc.get_balance_sol(wallet())
    assert "SECRET123" not in str(e.value) and "rate limited" in str(e.value)


# ---------- telegram ----------

async def test_pairing_brute_force_lockout(tmp_path):
    eng = make_engine(tmp_path)
    tg = TelegramControl(eng, "TOKEN", "", eng.http)
    first = tg.pair_code
    assert len(first) == 10
    for _ in range(10):
        await tg.handle_update({"message": {"chat": {"id": 9}, "text": "/start 0000000000"}})
    assert tg.owner == "" and tg.pair_code != first  # code rotated after 10 misses
    await tg.handle_update({"message": {"chat": {"id": 9}, "text": f"/start {first}"}})
    assert tg.owner == ""  # the old code no longer works
    await eng.http.aclose()


async def test_telegram_errors_hide_the_token(tmp_path):
    def handler(req):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    eng = make_engine(tmp_path)
    tg = TelegramControl(eng, "123:SECRETTOKEN", "1", httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception) as e:
        await tg.api("getMe")
    assert "SECRETTOKEN" not in str(e.value) and "Unauthorized" in str(e.value)
    await eng.http.aclose()


async def test_notifier_retries_after_rate_limit(monkeypatch):
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["text"])
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        if len(calls) == 2:
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
        return httpx.Response(200, json={"ok": True})
    n = Notifier(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "T", "1")
    await n.telegram("<b>hi</b> &amp; bye")
    assert calls == ["<b>hi</b> &amp; bye", "<b>hi</b> &amp; bye", "hi & bye"]


# ---------- scanners ----------

def test_gecko_skips_quote_tokens_and_uses_the_real_token():
    payload = {"data": [
        {"attributes": {"name": "SOL / NEW", "address": "p1"},
         "relationships": {"base_token": {"data": {"id": f"solana_{SOL}"}},
                           "quote_token": {"data": {"id": "solana_NEWMINT"}}}},
        {"attributes": {"name": "SOL / USDC", "address": "p2"},
         "relationships": {"base_token": {"data": {"id": f"solana_{SOL}"}},
                           "quote_token": {"data": {
                               "id": "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}}}},
    ]}
    assert [c.mint for c in parse_gecko_pools("solana", payload)] == ["NEWMINT"]


# ---------- third review pass ----------

async def test_missing_curve_needs_several_misses_before_migrating(tmp_path):
    eng = make_engine(tmp_path, api_key="")
    mint = wallet()
    pos = pump_pos(mint)
    pos.last_update = time.time() - 60
    eng.positions[mint] = pos

    async def missing(addr):
        return None
    eng.rpc.get_account_bytes = missing
    for _ in range(engine_mod.CURVE_MISSES_BEFORE_MIGRATED - 1):
        await eng._poll_position(pos)
    assert not pos.migrated  # a lagging RPC node doesn't flip a fresh token to "graduated"
    await eng._poll_position(pos)
    assert pos.migrated
    await eng.http.aclose()


def test_keyword_matching():
    from sniper.engine import keyword_hit
    assert keyword_hit("TRUMP2028 coin", ["trump"]) == "trump"
    assert keyword_hit("AI Agent", ["ai"]) == "ai"
    assert keyword_hit("pain daisy", ["ai"]) is None       # no substring hits for short words
    assert keyword_hit("Pepe on Sol", ["  ", "pepe"]) == "pepe"


async def test_withdraw_refuses_unrentable_remainder(tmp_path):
    eng = make_engine(tmp_path)
    eng.wallet.create()

    async def bal(_):
        return 1.0
    eng.rpc.get_balance_sol = bal
    with pytest.raises(ValueError, match="minimum"):
        await eng.withdraw(wallet(), 0.9995)  # would leave ~0.0005 SOL
    await eng.http.aclose()


async def test_copy_wallet_size_validated(tmp_path):
    eng = make_engine(tmp_path, api_key="k")
    with pytest.raises(ValueError, match="between 0 and 100"):
        await eng.add_copy_wallet(wallet(), "x", -5)
    await eng.http.aclose()


async def test_pumpportal_slippage_is_a_whole_percent_of_at_least_one():
    cfg = load_config(None)
    cfg.trading.slippage_pct = 0.5
    sent = {}

    class Http:
        async def post(self, url, data):
            sent.update(data)
            return httpx.Response(200, content=b"tx")
    ex = LiveExecutor(cfg, Keypair(), FakeRpc(), jupiter=None, http=Http(), sender=FakeSender())
    await ex._pumpportal_tx("buy", "M", 0.1, True)
    assert sent["slippage"] == 1


async def test_telegram_deletes_pasted_private_keys_and_parses_copy_add(tmp_path):
    eng = make_engine(tmp_path, api_key="k")
    tg = TelegramControl(eng, "T", "5", eng.http)
    calls, sent = [], []

    async def api(method, **p):
        calls.append((method, p))
        return {"message_id": 1}
    tg.api = api

    async def capture(text, buttons=None, chat_id=None):
        sent.append(text)
    eng.notifier.telegram = capture
    key = str(Keypair())  # base58 secret, 87-88 chars
    await tg.handle_update({"message": {"chat": {"id": 5}, "text": key, "message_id": 77}})
    assert ("deleteMessage", {"chat_id": "5", "message_id": 77}) in calls
    assert "private key" in sent[-1]
    assert eng.wallet.keypair() is None  # not imported by accident

    w = wallet()
    await tg.handle_update({"message": {"chat": {"id": 5}, "text": f"/copy add {w} 0.25",
                                        "message_id": 78}})
    cw = eng.copy_wallets()[0]
    assert cw.buy_sol == 0.25 and cw.label == ""  # a number is a size, not a label
    await eng.http.aclose()


async def test_telegram_ignores_backlog_from_while_offline(tmp_path):
    eng = make_engine(tmp_path)
    tg = TelegramControl(eng, "T", "5", eng.http)
    calls = []

    async def api(method, **p):
        calls.append((method, p))
        if method == "getUpdates" and p.get("offset") == -1:
            return [{"update_id": 41, "callback_query": {"data": "b:X:1"}}]
        return []
    tg.api = api

    async def capture(*a, **k):
        pass
    eng.notifier.telegram = capture
    await tg._drop_backlog()
    assert tg.offset == 42  # the queued "Buy" tap is skipped, never executed
    assert ("getUpdates", {"offset": 42, "timeout": 0}) in calls
    await eng.http.aclose()


# ---------- fourth review pass ----------

async def test_failed_sells_retry_with_more_slippage(tmp_path):
    eng = make_engine(tmp_path)
    seen = []

    class Ex(SlowExecutor):
        async def sell(self, mint, tokens, sell_all, pump, curve, slippage_pct=None):
            seen.append(slippage_pct)
            raise RuntimeError("slippage exceeded")
    eng.executor = Ex(0)
    base = eng.cfg.trading.slippage_pct
    pos = pump_pos(opened_at=time.time() - 10_000)
    eng.positions["M"] = pos
    for _ in range(4):
        await eng.check_exit(pos)
        assert eng._sell_next_try["M"] - time.time() <= 10.5  # quick retries early on
        eng._sell_next_try["M"] = 0
    assert seen == [min(50.0, base * k) for k in (1, 2, 3, 4)]  # capped at 50%
    pos.dev_sold = True
    eng.sell_failures.pop("M")
    await eng.check_exit(pos)
    assert seen[-1] == base * 1.5  # emergency exits start higher
    await eng.http.aclose()


async def test_buy_that_failed_on_chain_is_never_tracked():
    from sniper.solana_rpc import TxFailed
    cfg = load_config(None)
    ex, rpc = live_executor(cfg)

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_raises = TxFailed("transaction X failed on-chain: slippage")
    rpc.balance_raw = 5_000  # leftover dust from an earlier trade
    with pytest.raises(TxFailed):
        await ex.buy(Candidate(chain="solana", mint="M", source="pumpfun", route="pump"), 0.1, None)


async def test_confirmed_launch_that_is_not_bought_stops_its_trade_feed(tmp_path):
    eng = make_engine(tmp_path, api_key="k", entry={"confirm_seconds": 0.01, "min_unique_buyers": 0},
                      trading={"max_open_positions": 0})
    c = Candidate(chain="solana", mint=wallet(), source="pumpfun", creator=wallet(), route="pump",
                  v_sol=30.0, v_tokens=1.07e9)

    async def ok(cand):
        from sniper.models import SafetyReport
        return SafetyReport(passed=True)
    eng.safety.evaluate = ok
    res = await eng.handle_candidate(c)
    assert "max open positions" in res
    assert c.mint not in eng.stream.token_subs
    assert {"method": "unsubscribeTokenTrade", "keys": [c.mint]} in eng.sent_ws
    await eng.http.aclose()


# ---------- final-check finding: unconfirmable buys must never orphan tokens ----------

async def test_landed_buy_with_unreadable_wallet_is_watched_then_adopted(tmp_path):
    from sniper.execution.executors import BuyUncertain
    cfg = load_config(None)
    ex, rpc = live_executor(cfg)

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_raises = RpcError("getSignatureStatuses: HTTP 502")  # landed, but unclear

    async def rate_limited(owner, mint):
        raise RpcError("getTokenAccountsByOwner: HTTP 429 (rate limited)")
    rpc.get_token_balance = rate_limited
    with pytest.raises(BuyUncertain):  # not "buy failed"
        await ex.buy(Candidate(chain="solana", mint="M", source="pumpfun", route="pump"), 0.1, None)

    eng = make_engine(tmp_path, api_key="k")
    eng.live, eng.own_wallet = True, "ME"
    mint = wallet()

    async def sol_bal(owner):
        return 10.0
    eng.rpc.get_balance_sol = sol_bal

    class Ex:
        async def buy(self, c, sol, curve):
            raise BuyUncertain(c.mint, sol, "502")
    eng.executor = Ex()
    res = await eng.try_buy(Candidate(chain="solana", mint=mint, source="manual", symbol="P",
                                      route="pump", force=True))
    assert res.startswith("buy unconfirmed") and mint in eng.pending_buys()
    assert "already holding" in await eng.try_buy(  # no double buy while we watch
        Candidate(chain="solana", mint=mint, source="manual", symbol="P", force=True))

    balance = {"v": None}

    async def bal(owner, m):
        if balance["v"] is None:
            raise RpcError("429")
        return balance["v"]
    eng.rpc.get_token_balance = bal

    await eng.reconcile_pending(now=time.time() + 10_000)
    assert mint in eng.pending_buys()  # couldn't check: never dropped
    balance["v"] = 5000.0
    await eng.reconcile_pending()
    pos = eng.positions[mint]
    assert pos.tokens_remaining == 5000 and pos.sol_in == pytest.approx(0.05)  # as reported
    assert not eng.pending_buys()
    await eng.http.aclose()


async def test_unconfirmed_buy_that_never_lands_is_forgotten_after_window(tmp_path):
    eng = make_engine(tmp_path, api_key="k")
    eng.live, eng.own_wallet = True, "ME"
    mint = wallet()
    eng._add_pending(Candidate(chain="solana", mint=mint, source="manual", symbol="Q"), 0.1)

    async def zero(owner, m):
        return 0.0
    eng.rpc.get_token_balance = zero
    await eng.reconcile_pending()
    assert mint in eng.pending_buys()  # still inside the landing window
    await eng.reconcile_pending(now=time.time() + engine_mod.PENDING_BUY_WINDOW + 1)
    assert not eng.pending_buys() and mint not in eng.positions
    await eng.http.aclose()


async def test_reconcile_never_adopts_twice(tmp_path):
    eng = make_engine(tmp_path, api_key="k")
    eng.live, eng.own_wallet = True, "ME"
    mint = wallet()
    eng._add_pending(Candidate(chain="solana", mint=mint, source="manual", symbol="R"), 0.1)
    eng.positions[mint] = pump_pos(mint)  # adopted just before a crash

    async def held(owner, m):
        return 777.0
    eng.rpc.get_token_balance = held
    await eng.reconcile_pending()
    assert not eng.pending_buys() and eng.positions[mint].tokens_remaining == 1e6
    assert eng.store.events("buy") == []
    await eng.http.aclose()
