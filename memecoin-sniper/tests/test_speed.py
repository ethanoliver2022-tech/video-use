"""Hot-path latency: what a snipe waits on before its transaction is sent."""
import asyncio
import random

from solders.keypair import Keypair

import tests.test_chaos as chaos
from sniper.models import Candidate, Fill


def engine(tmp_path):
    eng = chaos.build(tmp_path, random.Random(1))
    eng.cfg.trading.max_open_positions = 50
    eng.cfg.trading.cooldown_after_loss_seconds = 0
    return eng


async def test_live_buys_use_the_background_balance_not_an_rpc_call(tmp_path):
    eng = engine(tmp_path)
    eng.live, eng.own_wallet = True, "W"
    reads = []

    async def bal(*a, **k):
        reads.append(1)
        return 10.0
    eng.rpc.get_balance_sol = bal

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol, signature="S")
    eng.executor.buy = buy
    task = asyncio.create_task(eng.balance_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    assert len(reads) == 1  # the background refresh
    eng._buys_finished = eng._bal_cache[2]
    await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="manual",
                                force=True))
    assert len(reads) == 1  # the snipe itself made no balance call
    # after a buy finished, the next one can't trust that balance: it reads afresh
    await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="manual",
                                force=True))
    assert len(reads) == 2
    eng.live = False
    await eng.http.aclose()


def test_bundles_go_to_every_jito_region_by_default():
    from sniper.config import SpeedConfig
    regions = {u.split("//")[1].split(".")[0] for u in SpeedConfig().jito_block_engines}
    assert regions == {"ny", "amsterdam", "frankfurt", "tokyo", "slc"}


async def test_slow_ipfs_never_delays_a_snipe_when_socials_are_optional(monkeypatch):
    import time
    from sniper import intel, safety
    from sniper.config import FilterConfig

    async def public(uri):
        return True
    monkeypatch.setattr(intel, "_resolves_public", public)

    class SlowHttp:
        def stream(self, *a, **k):
            class Ctx:
                async def __aenter__(self_):
                    await asyncio.sleep(10)

                async def __aexit__(self_, *a):
                    return False
            return Ctx()
    chk = safety.SafetyChecker(FilterConfig(min_socials=0, reject_reused_socials=True), None,
                               SlowHttp(), "")
    from sniper.models import SafetyReport
    r = SafetyReport(passed=True)
    t0 = time.monotonic()
    await chk._socials(Candidate(chain="solana", mint="M", source="pumpfun",
                                 uri="https://ipfs.example/x"), r)
    assert time.monotonic() - t0 < 1.5 and r.passed and "not checked" in r.notes[0]


async def test_sell_on_migration(tmp_path):
    eng = engine(tmp_path)
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    pos = eng.positions[m]
    sold = []

    async def sell(mint, tokens, sell_all, pump, curve, slippage_pct=None):
        sold.append(sell_all)
        return Fill(tokens=tokens, sol=0.04, signature="S")
    eng.executor.sell = sell
    eng.cfg.exits.sell_on_migration = False
    await eng.on_migration(m)
    await eng.settle()
    assert not sold and not pos.closed
    pos.migrated = False
    eng.cfg.exits.sell_on_migration = True
    await eng.on_migration(m)
    await eng.settle()
    assert sold == [True] and pos.closed and pos.close_reason == "migrated"
    await eng.http.aclose()
