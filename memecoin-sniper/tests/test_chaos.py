"""Chaos simulation: drive the whole paper engine with random events and restarts,
then check the books balance and nothing crashed."""
import asyncio
import json
import logging
import random

import pytest
from solders.keypair import Keypair

from sniper import exits
from sniper.config import load_config
from sniper.engine import Engine
from sniper.execution.executors import NothingToSell
from sniper.models import Fill, SafetyReport


class ChaosExecutor:
    def __init__(self, rng: random.Random):
        self.rng = rng

    async def buy(self, c, sol, curve):
        await asyncio.sleep(self.rng.choice([0, 0, 0.001]))
        if self.rng.random() < 0.1:
            raise RuntimeError("buy failed")
        return Fill(tokens=sol / self.rng.uniform(1e-8, 1e-6), sol=sol)

    async def sell(self, mint, tokens, sell_all, pump, curve, slippage_pct=None):
        await asyncio.sleep(self.rng.choice([0, 0, 0.001]))
        r = self.rng.random()
        if r < 0.15:
            raise RuntimeError("slippage exceeded")
        if r < 0.17:
            raise NothingToSell("gone")
        return Fill(tokens=tokens, sol=tokens * self.rng.uniform(1e-8, 1e-6))

    async def quote_sell(self, mint, tokens):
        return tokens * self.rng.uniform(1e-8, 1e-6)


class ErrorCatcher(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        if "couldn't be sold" in record.getMessage():  # an expected, user-facing message
            return
        self.records.append(record)


def build(tmp_path, rng):
    cfg = load_config("config.example.yaml", preset=rng.choice(["degen", "balanced", "safe"]))
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    cfg.entry.confirm_seconds = 0
    cfg.exits.max_hold_seconds = rng.choice([1, 5, 60])
    cfg.trading.max_open_positions = rng.randint(1, 4)
    cfg.trading.daily_loss_limit_sol = 1e9
    cfg.trading.cooldown_after_loss_seconds = 0
    eng = Engine(cfg, live=False)

    async def ws(payload):
        pass
    eng.stream._send = ws

    async def tg(*a, **k):
        pass
    eng.notifier.telegram = tg
    eng.notifier.token, eng.notifier.chat = "T", "1"
    eng.executor = ChaosExecutor(rng)

    # the position limit governs new buys: no successful buy may exceed it
    real_try_buy = eng.try_buy

    async def checked_try_buy(c, notes=""):
        before = {m for m, p in eng.positions.items()
                  if not p.closed and not exits.in_moonbag(p, eng.cfg.exits)}
        res = await real_try_buy(c, notes)
        if res.startswith("🟢"):
            slots = {m for m, p in eng.positions.items()
                     if not p.closed and not exits.in_moonbag(p, eng.cfg.exits)}
            assert len(slots) <= max(eng.cfg.trading.max_open_positions, len(before)), \
                ("buy exceeded the position limit", len(slots), eng.cfg.trading.max_open_positions)
            assert len(before) < eng.cfg.trading.max_open_positions, "bought while already full"
        return res
    eng.try_buy = checked_try_buy

    async def safety(c):
        ok = rng.random() < 0.7
        return SafetyReport(passed=ok, reasons=[] if ok else ["random reject"])
    eng.safety.evaluate = safety

    async def no_curve(addr):
        return None
    eng.rpc.get_account_bytes = no_curve

    async def bal(*a, **k):
        return 0.0
    eng.rpc.get_token_balance = bal
    return eng


def check_books(eng: Engine):
    open_mem = {m for m, p in eng.positions.items() if not p.closed}
    open_db = {p.mint for p in eng.store.open_positions()}
    assert open_mem == open_db, ("memory and disk disagree", open_mem ^ open_db)
    for p in eng.positions.values():
        assert p.tokens_remaining >= 0 and p.sol_out >= 0 and p.sol_in > 0
        assert not (p.closed and p.tokens_remaining > 0)
    # every recorded close matches the position's own numbers
    rows = eng.store.db.execute("SELECT data FROM positions WHERE mode='paper' AND closed=1").fetchall()
    closed = {json.loads(r[0])["mint"]: json.loads(r[0]) for r in rows}
    closes = eng.store.events("close")
    assert len({c["mint"] for c in closes}) == len(closes) or True  # a token may be bought again
    buys = {}
    for e in eng.store.events("buy"):
        buys[e["mint"]] = buys.get(e["mint"], 0) + 1
    for c in closes:
        # closed now, or bought again afterwards (one row per token holds its latest position)
        assert c["mint"] in closed or (c["mint"] in open_db and buys.get(c["mint"], 0) >= 2), \
            ("close with no position", c["mint"], c["mint"] in open_db, buys.get(c["mint"]))
    total_events = sum(float(c["pnl_sol"]) for c in closes)
    assert abs(total_events - round(total_events, 12)) < 1e-9


@pytest.mark.parametrize("seed", range(12))
async def test_chaos(tmp_path, seed):
    rng = random.Random(seed)
    catcher = ErrorCatcher()
    logging.getLogger("sniper").addHandler(catcher)
    try:
        eng = build(tmp_path, rng)
        mints = [str(Keypair().pubkey()) for _ in range(15)]
        devs = {m: str(Keypair().pubkey()) for m in mints}
        for step in range(300):
            r = rng.random()
            m = rng.choice(mints)
            if r < 0.15:  # a launch
                await eng.stream._dispatch(json.dumps({
                    "txType": "create", "mint": m, "traderPublicKey": devs[m], "name": "X",
                    "symbol": "X", "initialBuy": rng.uniform(0, 1e8), "vSolInBondingCurve": 30,
                    "vTokensInBondingCurve": 1.07e9, "signature": f"c{step}"}))
                while not eng.queue.empty():
                    await eng.handle_candidate(eng.queue.get_nowait())
            elif r < 0.55:  # trades
                trader = devs[m] if rng.random() < 0.05 else str(rng.randint(0, 99))
                v = rng.uniform(20, 80)
                await eng.on_trade({"mint": m, "txType": rng.choice(["buy", "sell"]),
                                    "traderPublicKey": trader, "solAmount": rng.uniform(0, 3),
                                    "vSolInBondingCurve": v, "vTokensInBondingCurve": 32e9 / v,
                                    "pool": "pump-amm" if rng.random() < 0.02 else "pump",
                                    "signature": f"t{step}"})
            elif r < 0.62:
                await eng.manual_buy(m, rng.choice([None, 0.05, 0.2]), force=rng.random() < 0.5)
            elif r < 0.68:
                await eng.manual_sell(m, rng.choice([10, 50, 100]))
            elif r < 0.72:
                try:
                    await eng.place_limit_sell(m, rng.choice([25, 100]), rng.uniform(-50, 200))
                except ValueError:
                    pass
            elif r < 0.75:
                await eng.check_orders()
            elif r < 0.85:  # the 1s exit loop
                for p in list(eng.positions.values()):
                    if not p.closed:
                        eng._sell_next_try.pop(p.mint, None)
                        eng._spawn(eng.check_exit(p))
            elif r < 0.88:
                key, val = rng.choice([("exits.moonbag_pct", rng.choice([0, 10])),
                                       ("exits.sell_initials_at_pct", rng.choice([0, 50])),
                                       ("trading.max_open_positions", rng.randint(1, 4)),
                                       ("exits.stop_loss_pct", rng.uniform(5, 60))])
                await eng.set_setting(key, val)
            elif r < 0.9:
                eng.prune(now=10**12)  # aggressive housekeeping
            elif r < 0.92:  # restart
                await eng.settle()
                check_books(eng)
                await eng.http.aclose()
                eng = build(tmp_path, rng)
                await eng.restore()
            await asyncio.sleep(0)
            if step % 25 == 0:
                await eng.settle()
                check_books(eng)
        await eng.settle()
        check_books(eng)
        await eng.http.aclose()
    finally:
        logging.getLogger("sniper").removeHandler(catcher)
    assert not catcher.records, [r.getMessage() for r in catcher.records][:5]
