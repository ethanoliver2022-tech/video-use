"""Interleaving tests: Telegram actions and automatic events racing each other."""
import asyncio
import random

from solders.keypair import Keypair

from sniper.config import load_config
from sniper.engine import Engine
from sniper.models import Candidate, Fill, SafetyReport


class TaggedExecutor:
    """Records which mode's executor actually traded."""

    def __init__(self, tag, log, rng):
        self.tag, self.log, self.rng = tag, log, rng

    async def buy(self, c, sol, curve):
        await asyncio.sleep(self.rng.choice([0, 0.001, 0.003]))
        self.log.append((self.tag, c.mint))
        return Fill(tokens=1000.0, sol=sol, signature="S")

    async def sell(self, mint, tokens, sell_all, pump, curve, slippage_pct=None):
        await asyncio.sleep(self.rng.choice([0, 0.001]))
        return Fill(tokens=tokens, sol=0.001, signature="S")

    async def quote_sell(self, mint, tokens):
        return 0.001


async def test_buys_racing_a_mode_switch_are_booked_in_the_mode_that_traded(tmp_path):
    switched = total_trades = 0
    for seed in range(40):
        rng = random.Random(seed)
        cfg = load_config("config.example.yaml")
        cfg.data_dir = str(tmp_path / str(seed))
        cfg.pumpportal_api_key = "k"
        cfg.trading.max_open_positions = 1000
        cfg.trading.min_sol_reserve = 0
        cfg.trading.cooldown_after_loss_seconds = 0
        from sniper.execution.wallet import WalletManager
        WalletManager(cfg.data_dir).create()
        eng = Engine(cfg, live=False)
        traded = []
        eng.executor = TaggedExecutor("paper", traded, rng)
        eng._build_executor = lambda live: TaggedExecutor("live" if live else "paper", traded, rng)

        async def slow_send(payload):
            await asyncio.sleep(0.002)
        eng.stream._send = slow_send

        async def tg(*a, **k):
            pass
        eng.notifier.telegram = tg

        async def bal(*a, **k):
            return 100.0
        eng.rpc.get_balance_sol = bal

        async def ok(c):
            return SafetyReport(passed=True)
        eng.safety.evaluate = ok
        # a closed position still subscribed, so the switch has stream work to await
        old = str(Keypair().pubkey())
        await eng.manual_buy(old, 0.01, force=True)
        await eng.manual_sell(old, 100)
        traded.clear()
        # force the switch to suspend mid-way (today only a still-subscribed token does this;
        # the switch must be safe whatever it awaits)
        eng.stream.token_subs.add(old)

        async def buyer():
            await asyncio.sleep(rng.choice([0, 0, 0.001, 0.004]))
            for _ in range(rng.randint(1, 5)):
                await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()),
                                            source="manual", symbol="R", force=True))
                await asyncio.sleep(rng.choice([0, 0.001]))

        async def switcher():
            return await eng.switch_mode(True)
        assert not [p for p in eng.positions.values() if not p.closed]
        res = await asyncio.gather(switcher(), buyer(), buyer())
        switched += res[0].startswith("Switched")
        await eng.settle()
        booked = {}
        for mode in ("paper", "live"):
            rows = eng.store.db.execute("SELECT mint FROM positions WHERE mode = ?", (mode,))
            for (m,) in rows:
                booked[m] = mode
            eng.store.mode = mode
            for m in eng.pending_buys():
                booked[m] = mode
        eng.store.mode = eng.mode
        total_trades += len(traded)
        for tag, mint in traded:
            assert booked.get(mint) == tag, (seed, res[1], tag, mint, booked.get(mint))
        await eng.http.aclose()
    assert switched >= 20 and total_trades >= 40  # the race was really exercised


async def test_racing_actions_on_the_same_tokens_keep_books_balanced(tmp_path):
    import tests.test_chaos as chaos
    total_sells = 0
    for seed in range(30):
        rng = random.Random(seed)
        eng = chaos.build(tmp_path / str(seed), rng)
        eng.cfg.trading.max_open_positions = 50
        mints = [str(Keypair().pubkey()) for _ in range(3)]  # few tokens: lots of collisions
        sells = 0

        async def action():
            nonlocal sells
            await asyncio.sleep(rng.choice([0, 0, 0.001]))
            m = rng.choice(mints)
            r = rng.random()
            if r < 0.3:
                await eng.manual_buy(m, 0.05, force=True)
            elif r < 0.55:
                res = await eng.manual_sell(m, rng.choice([25, 50, 100]))
                sells += res.startswith("🔴")
            elif r < 0.7:
                p = eng.positions.get(m)
                if p and not p.closed:
                    p.dev_sold = rng.random() < 0.5
                    await eng.check_exit(p)
            elif r < 0.8:
                try:
                    await eng.place_limit_sell(m, rng.choice([50, 100]), rng.uniform(-90, -50))
                except ValueError:
                    pass
                await eng.check_orders()
            elif r < 0.9:
                await eng.set_setting(rng.choice(["exits.moonbag_pct", "exits.stop_loss_pct"]),
                                      rng.choice(["5", "10", "30"]))
            else:
                eng.set_paused(not eng.paused)
        for _ in range(6):
            await asyncio.gather(*(action() for _ in range(12)))
            await eng.settle()
            chaos.check_books(eng)
        assert eng.store.events("buy")
        total_sells += sells
        await eng.http.aclose()
    assert total_sells >= 30, total_sells


async def test_a_failing_notification_never_breaks_a_sell(tmp_path):
    import tests.test_chaos as chaos
    eng = chaos.build(tmp_path, random.Random(1))
    eng.cfg.trading.max_open_positions = 50
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    assert m in eng.positions

    class Boom:
        async def post(self, *a, **k):
            raise RuntimeError("event loop is closing")  # not an httpx error
    from sniper.notify import Notifier
    eng.notifier = Notifier(Boom(), "T", "1")
    unwatched = []

    async def unwatch(mint):
        unwatched.append(mint)
    eng.stream.unwatch_token = unwatch

    async def sell(mint, tokens, sell_all, pump, curve, slippage_pct=None):
        return Fill(tokens=tokens, sol=0.04, signature="S")
    eng.executor.sell = sell
    res = await eng.manual_sell(m, 100)
    assert res.startswith("🔴"), res
    assert eng.positions[m].closed
    assert [c["mint"] for c in eng.store.events("close")] == [m]  # bookkeeping completed
    assert unwatched == [m]
    await eng.http.aclose()
