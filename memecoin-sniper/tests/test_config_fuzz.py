"""Any combination of settings a user can enter from Telegram — including every range
edge — must keep the exit math sound and the engine's books balanced."""
import random

import pytest
from solders.keypair import Keypair

import tests.test_chaos as chaos
from sniper.config import load_config
from sniper.settings import SETTINGS
from tests.test_fuzz_exits import run_one

SKIP_IN_CHAOS = {"entry.confirm_seconds"}  # a real wait; the chaos loop runs without one


def user_input(s, rng):
    """What someone might type for this setting: often a range edge."""
    if s.kind == "bool":
        return rng.choice(["on", "off"])
    if s.kind == "choice":
        return rng.choice(s.options)
    if s.kind == "tp":
        if rng.random() < 0.15:
            return "none"
        left, parts = 100.0, []
        for _ in range(rng.randint(1, 5)):
            sell = rng.choice([left, rng.uniform(0.01, left), 100.0 if not parts else 0])
            if sell <= 0 or sell > left:
                break
            left -= sell
            parts.append(f"{rng.choice([0.01, 1, rng.uniform(1, 1000), 100000]):g}:{sell:g}")
            if left <= 0:
                break
        return ",".join(parts) or "none"
    if s.kind == "words":
        return rng.choice(["none", "pepe, ai", "x" * 2])
    if s.kind == "wallets":
        return rng.choice(["none", f"{Keypair().pubkey()}"])
    v = rng.choice([s.lo, s.hi, rng.uniform(s.lo, min(s.hi, max(s.lo * 10, 100)))])
    return f"{int(v)}" if s.kind == "int" else f"{v:g}"


def random_user_config(rng, cfg):
    from sniper.settings import apply_setting
    for s in SETTINGS:
        if rng.random() < 0.8:
            try:
                apply_setting(cfg, s.key, user_input(s, rng))
            except ValueError:
                pass  # the bot rejects it too (e.g. TP sells over 100%)
    return cfg


def test_exit_math_holds_for_any_user_settings():
    for i in range(400):
        rng = random.Random(i)
        cfg = random_user_config(rng, load_config("config.example.yaml",
                                                  preset=rng.choice(["degen", "balanced", "safe"])))
        for market in range(25):
            run_one(i * 1000 + market, cfg.exits)


@pytest.mark.parametrize("seed", range(10))
async def test_engine_books_balance_for_any_user_settings(tmp_path, seed, monkeypatch):
    real_build = chaos.build

    def build(tmp, rng):
        eng = real_build(tmp, rng)
        from sniper.settings import apply_setting
        for s in SETTINGS:
            if s.key in SKIP_IN_CHAOS or s.key.startswith("copytrade."):
                continue
            if rng.random() < 0.8:
                try:
                    apply_setting(eng.cfg, s.key, user_input(s, rng))
                except ValueError:
                    pass
        return eng
    monkeypatch.setattr(chaos, "build", build)
    await chaos.test_chaos(tmp_path, 1000 + seed)


async def test_zero_daily_loss_limit_means_no_limit(tmp_path):
    from sniper.engine import Engine
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    eng = Engine(cfg, live=False)
    await eng.set_setting("trading.daily_loss_limit_sol", "0")
    assert await eng.risk_block(0.01) is None
    eng.store.event("close", "M", "M", pnl_sol=-5.0)
    assert await eng.risk_block(0.01) is None
    await eng.set_setting("trading.daily_loss_limit_sol", "1")
    assert await eng.risk_block(0.01) == "daily loss limit hit"
    await eng.http.aclose()


async def test_zero_launch_limit_means_off(tmp_path):
    from sniper.config import FilterConfig
    from sniper.models import Candidate, SafetyReport
    from sniper.safety import SafetyChecker
    from sniper.store import Store
    store = Store(str(tmp_path))
    dev = str(Keypair().pubkey())
    store.record_launch(str(Keypair().pubkey()), dev)
    for limit, ok in ((0, True), (1, False), (2, True)):
        chk = SafetyChecker(FilterConfig(max_creator_launches_24h=limit), None, None, "", store=store)
        r = SafetyReport(passed=True)
        chk._reputation(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="x",
                                  creator=dev), r)
        assert r.passed is ok, (limit, r.reasons)
