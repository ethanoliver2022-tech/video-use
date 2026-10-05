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
