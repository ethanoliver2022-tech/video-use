"""Regression tests for the findings of an independent code review."""
import asyncio
import random

import pytest
from solders.keypair import Keypair

import tests.test_chaos as chaos
from sniper import exits
from sniper.models import Fill
from sniper.solana_rpc import RpcError, SolanaRpc


class StatusRpc(SolanaRpc):
    def __init__(self, replies):
        super().__init__("https://rpc.invalid")
        self.replies = replies

    async def call(self, method, params=None):
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def fast_sleep(monkeypatch):
    import sniper.solana_rpc as srpc
    real = asyncio.sleep

    async def quick(*_):
        await real(0)
    monkeypatch.setattr(srpc.asyncio, "sleep", quick)


async def test_confirm_never_says_not_landed_without_evidence(fast_sleep):
    # every poll failed: unknown, not "didn't land"
    with pytest.raises(RpcError):
        await StatusRpc([RpcError("429")]).confirm("S", timeout=0.05)
    # seen processed, never confirmed: unknown
    with pytest.raises(RpcError):
        await StatusRpc([{"value": [{"confirmationStatus": "processed", "err": None}]}]).confirm(
            "S", timeout=0.05)
    # the node kept answering "never seen it" until the end: really didn't land
    assert await StatusRpc([{"value": [None]}]).confirm("S", timeout=0.05) is False
    assert await StatusRpc([{"value": [{"confirmationStatus": "confirmed", "err": None}]}]).confirm(
        "S", timeout=0.05) is True


def engine(tmp_path, seed=1):
    eng = chaos.build(tmp_path, random.Random(seed))
    eng.cfg.trading.max_open_positions = 50
    eng.cfg.trading.cooldown_after_loss_seconds = 0
    return eng


async def bought(eng):
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    assert m in eng.positions
    return eng.positions[m]


async def test_unreadable_sell_is_estimated_not_zero(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    pos.update_price(pos.entry_price * 2)

    async def sell(*a, **k):
        return Fill(tokens=pos.tokens_remaining, sol=0.0, signature="S", sol_known=False)
    eng.executor.sell = sell
    res = await eng.manual_sell(pos.mint, 100)
    assert "estimated" in res and pos.sol_out > pos.sol_in  # a 2x isn't booked as a total loss
    await eng.http.aclose()


async def test_sell_that_emptied_the_wallet_closes_the_position(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    held = pos.tokens_remaining * 0.8  # the wallet had less than the position thought

    async def sell(*a, **k):
        return Fill(tokens=held, sol=0.04, signature="S", emptied=True)
    eng.executor.sell = sell
    await eng.execute_sell(pos, exits.ExitDecision(pos.tokens_remaining * 0.9, False, "take profit"))
    assert pos.closed and pos.tokens_remaining == 0  # no phantom tokens left open
    await eng.http.aclose()


async def test_partly_landed_failed_sell_books_its_proceeds(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    eng.live = True
    half = pos.tokens_remaining / 2

    async def bal(*a, **k):
        return half
    eng.rpc.get_token_balance = bal
    await eng._sell_failed(pos, RuntimeError("timeout"))
    assert pos.tokens_remaining == half and pos.sol_out > 0
    eng.live = False
    await eng.http.aclose()


async def test_manual_sell_rejects_nan(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    res = await eng.manual_sell(pos.mint, float("nan"))
    assert "number" in res and not pos.closed and pos.tokens_remaining > 0
    await eng.http.aclose()


async def test_a_crashing_order_is_marked_failed_not_stuck(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    oid = eng.store.add_order(pos.mint, "sell", 0, 100, 0.0, "below", pos.entry_price, 1e12)
    assert eng.store.set_order_status(oid, "executing")

    async def boom(*a, **k):
        raise RuntimeError("bad extension data")
    eng.execute_sell = boom
    await eng._fill_order({"id": oid, "side": "sell", "mint": pos.mint, "pct": 100})
    status = eng.store.db.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    assert status == "failed"
    await eng.http.aclose()


async def test_backlog_is_never_processed_when_telegram_was_down_at_boot(tmp_path):
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    calls = []

    async def api(method, **params):
        calls.append((method, params))
        if len([c for c in calls if c[0] == "getUpdates"]) <= 3:
            raise RuntimeError("network unreachable")
        if params.get("offset") == -1:
            return [{"update_id": 41, "callback_query": {"id": "q", "data": "b:X:0.5"}}]
        if params.get("timeout") == 25:
            raise asyncio.CancelledError
        return []
    h.tg.api = api
    import sniper.telegram_bot as tb
    real = tb.asyncio.sleep

    async def quick(*_):
        await real(0)
    tb.asyncio.sleep = quick
    try:
        with pytest.raises(asyncio.CancelledError):
            await h.tg.run()
    finally:
        tb.asyncio.sleep = real
    polls = [p for m, p in calls if m == "getUpdates" and p.get("timeout") == 25]
    assert polls and polls[0]["offset"] == 42  # the stale tap was skipped, not run
    await h.close()


async def test_text_during_withdraw_confirmation_keeps_it_open(tmp_path):
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    await h.tap("w:new")
    await h.text(f"/withdraw {Keypair().pubkey()} 0.5")
    assert h.tg.pending["kind"] == "withdraw_confirm"
    await h.text("yes")
    assert h.tg.pending and h.tg.pending["kind"] == "withdraw_confirm" and "wd!" in h.buttons()
    await h.close()


def test_name_blocklist_whole_words_and_punctuation():
    from sniper.config import FilterConfig
    from sniper.models import Candidate
    from sniper.safety import static_checks
    f = FilterConfig(name_blocklist=["test", "rug", "scam", "rug pull"])

    def blocked(name):
        r = static_checks(Candidate(chain="solana", mint="M", source="x", name=name, symbol="X"), f)
        return any("blocked word" in x for x in r.reasons)
    assert blocked("RUG!") and blocked("scam-coin") and blocked("Rug Pull Inu")
    assert not blocked("Contest") and not blocked("Latest") and not blocked("Pullrug")


# ---------- second review ----------

async def test_late_landed_partial_take_profit_is_not_sold_twice(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    eng.live = True
    pos.update_price(pos.entry_price * 3)
    dec = exits.evaluate(pos, eng.cfg.exits)
    assert dec and dec.kind in ("tp", "initials")
    left = pos.tokens_remaining - dec.tokens

    async def bal(*a, **k):
        return left  # the "failed" sell actually landed
    eng.rpc.get_token_balance = bal
    await eng._sell_failed(pos, RuntimeError("outcome unknown"), dec)
    again = exits.evaluate(pos, eng.cfg.exits)
    assert again is None or again.kind != dec.kind or again.tp_index != dec.tp_index
    eng.live = False
    await eng.http.aclose()


async def test_confirmed_buy_without_visible_tokens_keeps_its_baseline(tmp_path):
    eng = engine(tmp_path)
    eng.live = True
    m = str(Keypair().pubkey())

    async def sol_balance(*a, **k):
        return 10.0
    eng.rpc.get_balance_sol = sol_balance

    async def buy(c, sol, curve):
        return Fill(tokens=0.0, sol=sol, signature="S", pre=5000.0)
    eng.executor.buy = buy
    from sniper.models import Candidate
    await eng.try_buy(Candidate(chain="solana", mint=m, source="manual", symbol="B", force=True))
    assert eng.pending_buys()[m]["pre"] == 5000.0

    async def held(*a, **k):
        return 5000.0  # only the old bag: this buy hasn't landed
    eng.rpc.get_token_balance = held
    await eng.reconcile_pending()
    assert m not in eng.positions  # the old bag is never adopted as the new buy
    eng.live = False
    await eng.http.aclose()


async def test_busy_limit_stop_stays_armed(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    oid = eng.store.add_order(pos.mint, "sell", 0, 100, 0.0, "<=", pos.entry_price, 1e12)
    assert eng.store.set_order_status(oid, "executing")

    async def busy(*a, **k):
        return "a sell is already in progress"
    eng.execute_sell = busy
    await eng._fill_order({"id": oid, "side": "sell", "mint": pos.mint, "pct": 100})
    assert [o["id"] for o in eng.store.open_orders()] == [oid]
    await eng.http.aclose()


async def test_wallet_cant_be_replaced_while_live_positions_exist(tmp_path):
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    await h.tap("w:new")
    from sniper.models import Position
    live = Position(mint=str(Keypair().pubkey()), symbol="L", source="x", creator=None,
                    entry_price=1e-6, tokens_initial=1.0, tokens_remaining=1.0, sol_in=0.01)
    h.eng.store.mode = "live"
    h.eng.store.save_position(live)
    h.eng.store.mode = "paper"
    before = h.eng.wallet.keypair().pubkey()
    await h.tap("w:new!")
    assert h.eng.wallet.keypair().pubkey() == before and "LIVE position" in h.last
    await h.close()


async def test_dust_sell_whose_fees_exceed_proceeds_receives_nothing():
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    import sniper.execution.executors as exm
    ex, rpc = live_executor(load_config(None))

    async def confirm(sig, timeout=90):
        return True
    rpc.confirm = confirm
    real = exm.balance_deltas
    exm.balance_deltas = lambda tx, owner, mint: (-1000.0, -0.00098)  # fees > proceeds
    try:
        async def tx(sig):
            return {"meta": {}}
        rpc.get_transaction = tx
        fill = await ex._submit(b"tx", "M", "sell")
    finally:
        exm.balance_deltas = real
    assert fill.sol == 0.0


async def test_group_chat_id_never_obeyed_and_sender_checked(tmp_path):
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    owner = h.tg.owner
    await h.tg.handle_update({"message": {"chat": {"id": int(owner)}, "from": {"id": 999},
                                          "text": "/pause", "message_id": 5}})
    assert h.eng.store.get_setting("paused") is None  # someone else's message: ignored
    h.tg.owner = "-100123"
    await h.tg.handle_update({"message": {"chat": {"id": -100123}, "from": {"id": 1},
                                          "text": "/pause", "message_id": 6}})
    assert h.eng.store.get_setting("paused") is None  # a group id is never obeyed
    await h.close()


def test_take_profit_rejects_nan_and_inf():
    from sniper.settings import BY_KEY, parse_value
    for bad in ("nan:50", "inf:50", "50:nan"):
        with pytest.raises(ValueError):
            parse_value(BY_KEY["exits.take_profit"], bad)


# ---------- third review ----------

async def test_unreadable_buy_books_what_was_sent_not_just_the_tip(tmp_path):
    eng = engine(tmp_path)

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=0.0005, signature="S", sol_known=False)  # tip only
    eng.executor.buy = buy
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    assert eng.positions[m].sol_in >= 0.05
    await eng.http.aclose()


async def test_migration_of_a_token_seen_at_launch_is_still_sniped(tmp_path):
    from sniper.models import Candidate
    eng = engine(tmp_path)
    eng.paused = False
    m = str(Keypair().pubkey())
    await eng.on_candidate(Candidate(chain="solana", mint=m, source="pumpfun", creator="D"))
    while not eng.queue.empty():
        eng.queue.get_nowait()
    await eng.on_candidate(Candidate(chain="solana", mint=m, source="pumpfun-migration",
                                     route="pump"))
    assert not eng.queue.empty()
    await eng.http.aclose()


async def test_late_landed_limit_sell_is_not_rearmed(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    eng.live = True
    oid = eng.store.add_order(pos.mint, "sell", 0, 50, 0.0, "<=", pos.entry_price, 1e12)
    assert eng.store.set_order_status(oid, "executing")
    left = pos.tokens_remaining / 2

    async def sell(*a, **k):
        raise RuntimeError("outcome unknown")
    eng.executor.sell = sell

    async def bal(*a, **k):
        return left
    eng.rpc.get_token_balance = bal
    await eng._fill_order({"id": oid, "side": "sell", "mint": pos.mint, "pct": 50})
    assert not eng.store.open_orders()  # filled, not re-armed to sell another 50%
    eng.live = False
    await eng.http.aclose()


async def test_slow_telegram_never_holds_the_sell_lock(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    gate = asyncio.Event()

    async def slow(*a, **k):
        await gate.wait()  # Telegram rate-limited
    eng.notifier.send = slow
    await asyncio.wait_for(eng.manual_sell(pos.mint, 25), 2)
    assert not eng.sell_locks[pos.mint].locked()
    gate.set()
    await eng.http.aclose()


async def test_restore_never_adopts_unrelated_tokens(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    tracked = pos.tokens_remaining
    eng.live = True

    async def bal(*a, **k):
        return tracked * 3  # an old bag of the same mint is also in the wallet
    eng.rpc.get_token_balance = bal
    await eng.restore()
    assert eng.positions[pos.mint].tokens_remaining == tracked
    eng.live = False
    await eng.http.aclose()


async def test_jito_with_no_block_engines_falls_back_to_rpc():
    from solders.hash import Hash
    from solders.keypair import Keypair as Kp
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    from sniper.config import load_config
    from sniper.execution.sender import TxSender
    cfg = load_config("config.example.yaml")
    cfg.speed.jito_enabled, cfg.speed.jito_block_engines = True, []
    sent = []

    class Rpc:
        url = "x"

        async def send_raw_transaction(self, raw):
            sent.append(raw)
            return "SIG"
    s = TxSender(cfg.speed, Rpc(), None, 0.0001)
    kp = Kp()
    tx = VersionedTransaction(MessageV0.try_compile(kp.pubkey(), [transfer(TransferParams(
        from_pubkey=kp.pubkey(), to_pubkey=Kp().pubkey(), lamports=1))], [], Hash.new_unique()), [kp])
    await s.send(tx, kp)
    assert sent


async def test_config_wallet_mode_can_be_toggled(tmp_path):
    from sniper.config import CopyWallet
    eng = engine(tmp_path)
    addr = str(Keypair().pubkey())
    eng.cfg.copytrade.wallets = [CopyWallet(address=addr, label="whale", buy_sol=0.2)]
    await eng.set_wallet_mode(addr, "alert")
    w = [x for x in eng.store.copy_wallets() if x["address"] == addr][0]
    assert w["mode"] == "alert" and w["label"] == "whale" and w["buy_sol"] == 0.2
    await eng.http.aclose()


async def test_copy_add_track_alone_gives_usage(tmp_path):
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    await h.text("/copy add track")
    assert "address" in h.last and "index" not in h.last
    await h.close()


async def test_candidates_stuck_in_a_backed_up_queue_are_dropped(tmp_path):
    import time as _t
    from sniper.models import Candidate
    eng = engine(tmp_path)
    handled = []

    async def handle(c):
        handled.append(c.mint)
    eng.handle_candidate = handle
    stale = Candidate(chain="solana", mint="S", source="pumpfun", queued_at=_t.monotonic() - 120)
    fresh = Candidate(chain="solana", mint="F", source="pumpfun", queued_at=_t.monotonic())
    manual = Candidate(chain="solana", mint="M", source="manual", queued_at=_t.monotonic() - 120)
    for c in (stale, fresh, manual):
        eng.queue.put_nowait(c)
    task = asyncio.create_task(eng.worker())
    await eng.queue.join()
    task.cancel()
    assert handled == ["F", "M"]
    await eng.http.aclose()


async def test_balance_rpc_is_read_outside_the_buy_lock(tmp_path):
    eng = engine(tmp_path)
    eng.live = True
    seen_locked = []

    async def bal(*a, **k):
        seen_locked.append(eng.buy_lock.locked())
        return 10.0
    eng.rpc.get_balance_sol = bal

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol, signature="S")
    eng.executor.buy = buy
    from sniper.models import Candidate
    await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="manual",
                                force=True))
    assert seen_locked == [False]
    eng.live = False
    await eng.http.aclose()


# ---------- fourth review ----------

def test_sell_slippage_never_below_the_users_setting(tmp_path):
    eng = engine(tmp_path)
    eng.cfg.trading.slippage_pct = 80
    from sniper.models import Position
    pos = Position(mint="M", symbol="M", source="x", creator=None, entry_price=1.0,
                   tokens_initial=1.0, tokens_remaining=1.0, sol_in=1.0)
    assert eng._sell_slippage(pos, exits.ExitDecision(1.0, True, "stop loss (-30%)")) >= 80
    eng.cfg.trading.slippage_pct = 20
    assert eng._sell_slippage(pos, exits.ExitDecision(1.0, True, "stop loss (-30%)")) <= 50


async def test_small_buys_dont_start_below_the_stop_loss(tmp_path):
    eng = engine(tmp_path)

    async def buy(c, sol, curve):  # 0.01 SOL swap, plus ~0.005 of rent/tip/fees
        return Fill(tokens=1_000_000.0, sol=sol + 0.005, signature="S")
    eng.executor.buy = buy
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.01, force=True)
    pos = eng.positions[m]
    pos.update_price(0.01 / 1_000_000.0)  # the market hasn't moved
    assert abs(pos.pnl_pct) < 1 and pos.sol_in > 0.0149  # costs are still in SOL PnL
    assert exits.evaluate(pos, eng.cfg.exits) is None
    await eng.http.aclose()


def test_metadata_urls_are_restricted():
    from sniper.intel import _safe_url
    assert _safe_url("https://ipfs.io/ipfs/Qm") and _safe_url("https://cf-ipfs.com/x")
    for bad in ("http://127.0.0.1/x", "http://10.1.2.3/", "http://169.254.169.254/latest",
                "file:///etc/passwd", "http://localhost:9000", "https://[::1]/", "gopher://x"):
        assert not _safe_url(bad), bad


async def test_metadata_body_is_size_capped():
    import httpx
    from sniper import intel

    def handler(req):
        return httpx.Response(200, content=b'{"a":"' + b"x" * 70_000 + b'"}')  # > 64 KB
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await intel.fetch_metadata(http, "https://meta.example/x") is None
    await http.aclose()


async def test_toggling_mode_keeps_copy_sells_off(tmp_path):
    from sniper.config import CopyWallet
    eng = engine(tmp_path)
    addr = str(Keypair().pubkey())
    eng.cfg.copytrade.wallets = [CopyWallet(address=addr, copy_sells=False)]
    await eng.set_wallet_mode(addr, "alert")
    await eng.set_wallet_mode(addr, "copy")
    w = [x for x in eng.store.copy_wallets() if x["address"] == addr][0]
    assert w["copy_sells"] is False
    await eng.http.aclose()


async def test_removed_config_wallet_stays_removed(tmp_path):
    from sniper.config import CopyWallet
    eng = engine(tmp_path)
    addr = str(Keypair().pubkey())
    eng.cfg.copytrade.enabled = True
    eng.cfg.copytrade.wallets = [CopyWallet(address=addr)]
    eng._load_copy_wallets()
    assert addr in eng._copy
    assert await eng.remove_copy_wallet(addr)
    eng._load_copy_wallets()  # e.g. after a copytrade setting change, or a restart
    assert addr not in eng._copy
    await eng.add_copy_wallet(addr)  # re-adding from chat brings it back
    eng._load_copy_wallets()
    assert addr in eng._copy
    await eng.http.aclose()


async def test_balance_reread_when_a_buy_finished_meanwhile(tmp_path):
    eng = engine(tmp_path)
    eng.live = True
    reads = []

    async def bal(*a, **k):
        reads.append(1)
        if len(reads) == 1:
            eng._buys_finished += 1  # another buy completes during this read
        return 10.0
    eng.rpc.get_balance_sol = bal

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol, signature="S")
    eng.executor.buy = buy
    from sniper.models import Candidate
    await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="manual",
                                force=True))
    assert len(reads) == 2  # the stale balance was read again under the lock
    eng.live = False
    await eng.http.aclose()


async def test_limit_sell_on_a_closed_position_is_cancelled_not_failed(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    oid = eng.store.add_order(pos.mint, "sell", 0, 100, 0.0, "<=", pos.entry_price, 1e12)
    assert eng.store.set_order_status(oid, "executing")
    from sniper.execution.executors import NothingToSell

    async def gone(*a, **k):
        raise NothingToSell("gone")
    eng.executor.sell = gone
    await eng._fill_order({"id": oid, "side": "sell", "mint": pos.mint, "pct": 100})
    status = eng.store.db.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    assert status == "cancelled"
    await eng.http.aclose()


async def test_leftover_bag_survives_a_rebuy_of_the_same_mint(tmp_path):
    eng = engine(tmp_path)
    pos = await bought(eng)
    eng.live = True
    eng._set_leftover(pos.mint, 5000.0)  # an old written-off bag
    tracked = pos.tokens_remaining

    async def bal(*a, **k):
        return tracked + 5000.0  # nothing of this position sold yet
    eng.rpc.get_token_balance = bal
    await eng._sell_failed(pos, RuntimeError("outcome unknown"),
                           exits.ExitDecision(tracked / 2, False, "take profit +50%", 0, "tp"))
    assert pos.tokens_remaining == tracked and pos.sol_out == 0  # the old bag isn't a landing
    eng.live = False
    await eng.http.aclose()


def test_sender_follows_config_changes():
    from sniper.config import load_config
    from sniper.execution.sender import TxSender
    cfg = load_config("config.example.yaml")

    class Rpc:
        url = "https://main"
    s = TxSender(cfg.speed, Rpc(), None, lambda: cfg.trading.priority_fee_sol)
    cfg.speed.broadcast_rpcs = ["https://a", "https://b"]
    assert [r.url for r in s.extra] == ["https://a", "https://b"]
    cfg.trading.priority_fee_sol = 0.0042
    assert s.default_fee == 0.0042


async def test_full_pump_exit_is_built_while_the_balance_is_read():
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    ex, rpc = live_executor(load_config(None))
    order = []

    async def pp(*a, **k):
        order.append("build-start")
        await asyncio.sleep(0)
        return b"tx"
    ex._pumpportal_tx = pp
    real = rpc.get_token_balance_raw

    async def bal(*a, **k):
        await asyncio.sleep(0)
        order.append("balance-done")
        return await real(*a, **k)
    rpc.get_token_balance_raw = bal
    rpc.balance_raw = 1_000_000

    async def confirm(sig, timeout=90):
        return True
    rpc.confirm = confirm
    await ex.sell("M", 1.0, True, pump=True, curve=None)
    assert order.index("build-start") < order.index("balance-done")
