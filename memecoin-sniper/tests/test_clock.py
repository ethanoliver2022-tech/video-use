"""Wall-clock steps (NTP corrections, VM migration, suspend/resume) must never stall a sell,
dump healthy tokens as "dead", or double-send the daily report."""
import asyncio
import time

from solders.keypair import Keypair

from sniper import exits
from sniper.config import load_config
from sniper.engine import Engine
from sniper.models import Position


def engine(tmp_path):
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    eng = Engine(cfg, live=False)
    sent = []

    async def tg(text, *a, **k):
        sent.append(text)
    eng.notifier.telegram = tg
    eng.sent = sent
    return eng


def position(**kw):
    m = str(Keypair().pubkey())
    return Position(mint=m, symbol="C", source="manual", creator=None, entry_price=1e-6,
                    tokens_initial=1000.0, tokens_remaining=1000.0, sol_in=0.01, route="jupiter",
                    **kw)


class Clock:
    """Fake wall clock; the monotonic clock keeps running normally."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)


async def test_backward_step_never_stalls_sell_retries(tmp_path, monkeypatch):
    eng = engine(tmp_path)
    clock = Clock(monkeypatch)
    pos = position()
    eng.positions[pos.mint] = pos
    await eng._sell_failed(pos, RuntimeError("boom"))
    clock.offset = -3600  # clock steps back an hour
    eng._sell_next_try[pos.mint] -= 10  # the short backoff has really elapsed
    calls = []

    async def sell(*a, **k):
        calls.append(1)
        raise RuntimeError("still failing")
    eng.executor.sell = sell
    pos.dev_sold = True
    await eng.check_exit(pos)
    assert calls, "retry was blocked by the clock step"
    await eng.http.aclose()


async def test_backward_step_never_extends_loss_cooldown(tmp_path, monkeypatch):
    eng = engine(tmp_path)
    clock = Clock(monkeypatch)
    eng.cfg.trading.cooldown_after_loss_seconds = 60
    assert await eng.risk_block(0.01) is None  # fresh boot: no phantom cooldown
    eng.last_loss_at = time.monotonic() - 61
    clock.offset = -86400
    assert await eng.risk_block(0.01) is None
    await eng.http.aclose()


async def test_clock_jump_does_not_dump_healthy_tokens(tmp_path, monkeypatch):
    eng = engine(tmp_path)
    clock = Clock(monkeypatch)
    pos = position()
    pos.update_price(1e-6)  # flat: no exit rule should fire
    eng.positions[pos.mint] = pos
    sells = []

    async def sell(*a, **k):
        sells.append(a)
        raise RuntimeError("no")
    eng.executor.sell = sell
    task = asyncio.create_task(eng.exit_loop())
    await asyncio.sleep(0.05)
    # the wall clock leaps forward past both the stale and the max-hold limits
    clock.offset = max(eng.cfg.exits.stale_seconds, eng.cfg.exits.max_hold_seconds) + 600
    await asyncio.sleep(1.2)
    task.cancel()
    await eng.settle()
    assert not sells, "a clock leap made a live token look dead"
    # but a token whose price really stops updating is still exited
    pos.last_update = time.time() - eng.cfg.exits.stale_seconds - 1
    assert exits.evaluate(pos, eng.cfg.exits).reason.startswith("no price updates")
    await eng.http.aclose()


async def test_daily_report_once_per_day_even_if_clock_steps_back(tmp_path):
    eng = engine(tmp_path)
    day = 1_790_000_000 - 1_790_000_000 % 86400  # a UTC midnight
    assert not await eng.daily_report(day + 3600)       # first run: starts counting
    assert await eng.daily_report(day + 86400 + 60)     # next day: report
    assert not await eng.daily_report(day + 86400 + 120)
    assert not await eng.daily_report(day + 86400 - 60)  # clock steps back past midnight
    assert not await eng.daily_report(day + 86400 + 180)  # and forward again: no duplicate
    assert await eng.daily_report(day + 2 * 86400 + 5)
    await eng.http.aclose()


async def test_priority_fee_cache_survives_clock_steps(monkeypatch):
    from sniper.config import load_config as lc
    from sniper.execution.sender import TxSender
    cfg = lc("config.example.yaml")
    cfg.speed.auto_priority_fee = True
    calls = []

    class Rpc:
        url = "https://x"

        async def call(self, m, p=None):
            calls.append(m)
            return [{"prioritizationFee": 50_000}]
    s = TxSender(cfg.speed, Rpc(), None, 0.0001)
    clock = Clock(monkeypatch)
    f1 = await s.priority_fee()
    assert f1 > 0 and len(calls) == 1  # never serves the empty initial cache
    clock.offset = -3600
    await s.priority_fee()
    assert len(calls) == 1  # still cached (10s)
    s._fee_cache = (s._fee_cache[0], s._fee_cache[1] - 11)
    assert await s.priority_fee() == f1  # stale: answered at once, refreshed in the background
    await s._fee_refresh
    assert len(calls) == 2  # refreshed despite the clock being an hour behind


async def test_withdraw_confirmation_expiry_ignores_clock_steps(tmp_path, monkeypatch):
    from sniper import telegram_bot
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    await h.tap("w:new")
    sent = []

    async def withdraw(address, sol):
        sent.append(sol)
        return "sent"
    h.eng.withdraw = withdraw
    clock = Clock(monkeypatch)
    real_mono = time.monotonic
    elapsed = 0.0
    monkeypatch.setattr(telegram_bot.time, "monotonic", lambda: real_mono() + elapsed)
    await h.text(f"/withdraw {Keypair().pubkey()} 0.5")
    elapsed = telegram_bot.PENDING_TTL + 1  # the confirmation window really passed...
    clock.offset = -86400                    # ...while the wall clock stepped back a day
    await h.tap("wd!")
    await h.eng.settle()
    assert not sent and "expired" in h.last
    await h.close()
