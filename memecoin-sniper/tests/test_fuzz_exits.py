"""Property/fuzz test: random strategies x random markets must never break the exit math."""
import random
import time

from sniper import exits
from sniper.config import ExitConfig, TakeProfitLevel
from sniper.models import Position

ONE_TIME_KINDS = ("initials", "kol")
BAG_EXITS = ("dev sold", "copied wallet sold", "stop loss", "moonbag")


def random_cfg(rng: random.Random) -> ExitConfig:
    levels, left = [], 100.0
    for _ in range(rng.randint(0, 4)):
        sell = round(rng.uniform(5, min(60, left)), 2)
        if sell <= 0 or left - sell < 0:
            break
        left -= sell
        levels.append(TakeProfitLevel(round(rng.uniform(5, 500), 1), sell))
    levels.sort(key=lambda lvl: lvl.at_pct)
    return ExitConfig(
        take_profit=levels,
        stop_loss_pct=rng.uniform(5, 90),
        breakeven_after_first_tp=rng.random() < 0.5,
        trailing_activate_pct=rng.uniform(0, 300),
        trailing_stop_pct=rng.uniform(5, 80),
        max_hold_seconds=rng.randint(30, 5000),
        stale_seconds=rng.randint(10, 3000),
        exit_on_dev_sell=rng.random() < 0.8,
        kol_buy_sell_pct=rng.choice([0, 25, 50, 100]),
        sell_pressure_ratio=rng.uniform(0.5, 1.0),
        sell_pressure_min_trades=rng.randint(3, 20),
        sell_initials_at_pct=rng.choice([0, 0, 50, 100, 300]),
        moonbag_pct=rng.choice([0, 0, 5, 10, 25, 50]),
        moonbag_trailing_pct=rng.uniform(10, 95),
        moonbag_max_hold_hours=rng.uniform(0.01, 48),
    )


def run_one(seed: int) -> None:
    rng = random.Random(seed)
    cfg = random_cfg(rng)
    t0 = 1_700_000_000.0
    entry = rng.uniform(1e-9, 1e-3)
    tokens = rng.uniform(1e3, 1e9)
    p = Position(mint="M", symbol="T", source="pumpfun", creator="DEV", entry_price=entry,
                 tokens_initial=tokens, tokens_remaining=tokens, sol_in=entry * tokens,
                 opened_at=t0, last_update=t0, leader="LEAD" if rng.random() < 0.3 else None)
    now, price = t0, entry
    kinds_done = []
    tp_seen = set()
    bag = tokens * cfg.moonbag_pct / 100
    for _ in range(400):
        now += rng.choice([0.5, 1, 5, 30, 600, 3600])
        if rng.random() > 0.15:  # sometimes nobody trades: exercises the dead-token exit
            price *= rng.uniform(0.6, 1.6)
        p.update_price(price, ts=now if rng.random() < 0.8 else None)
        r = rng.random()
        if r < 0.01:
            p.dev_sold = True
        elif r < 0.03:
            exits.record_trade(p, {"txType": "buy", "traderPublicKey": "KOL", "solAmount": 1},
                               {"KOL"})
        elif r < 0.04 and p.leader:
            p.leader_sold = True
        elif r < 0.3:
            exits.record_trade(p, {"txType": rng.choice(["buy", "sell"]),
                                   "traderPublicKey": f"w{rng.randint(0, 50)}", "solAmount": 0.1},
                               set())
        before = p.tokens_remaining
        d = exits.evaluate(p, cfg, now=now)
        if d is None:
            continue
        # --- invariants on the decision ---
        assert d.tokens >= 0, (seed, d)
        assert d.tokens <= before * (1 + 1e-9), (seed, d, before)
        if d.sell_all:
            assert abs(d.tokens - before) <= before * 1e-9, (seed, d)
        else:
            assert d.tokens > 0 or d.kind == "tp", (seed, d)
        protected = bag > 0 and (p.tp_levels_hit or p.initials_taken or d.kind in ("tp", "initials"))
        if protected and not d.reason.startswith(BAG_EXITS) and not d.sell_all:
            assert before - d.tokens >= bag * (1 - 1e-6), ("sold into moonbag", seed, d, before, bag)
        if d.kind in ONE_TIME_KINDS:
            assert d.kind not in kinds_done, ("repeated one-time action", seed, d)
            kinds_done.append(d.kind)
        if d.tp_index is not None:
            assert d.tp_index not in tp_seen, ("TP level fired twice", seed, d)
            tp_seen.update(range(d.tp_index + 1))
        # --- fill it like an exchange would ---
        exits.apply_fill(p, d, d.tokens, d.tokens * price, cfg)
        assert p.tokens_remaining >= 0, seed
        assert p.sol_out >= 0, seed
        if d.sell_all:
            assert p.closed and p.tokens_remaining == 0, seed
        if p.closed:
            assert exits.evaluate(p, cfg, now=now) is None, seed
            break


def test_exit_engine_survives_10k_random_markets():
    start = time.time()
    for seed in range(10_000):
        run_one(seed)
    assert time.time() - start < 120
