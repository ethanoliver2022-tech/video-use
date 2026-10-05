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
