"""Dev wallet background checks: wallet age, who funded it, and recent curve sells."""
import time

import pytest

from sniper.config import FilterConfig, load_config
from sniper.devcheck import PUMP_PROGRAM, DevChecker, _program_sells, funder_of
from sniper.models import Candidate

NOW = time.time()
DEV, FUNDER, MIXER = "Dev1111", "Fund111", "Mix1111"


def _transfer(src, dst):
    return {"program": "system", "parsed": {"type": "transfer",
                                            "info": {"source": src, "destination": dst}}}


SELL_LOGS = [f"Program {PUMP_PROGRAM} invoke [1]", "Program log: Instruction: Sell",
             f"Program {PUMP_PROGRAM} success"]
BUY_LOGS = [f"Program {PUMP_PROGRAM} invoke [1]", "Program log: Instruction: Buy",
            f"Program {PUMP_PROGRAM} success"]


class FakeRpc:
    def __init__(self, wallets, txs, fail=False):
        self.wallets, self.txs, self.fail, self.calls = wallets, txs, fail, []

    async def call(self, method, params):
        self.calls.append((method, params[0]))
        if self.fail:
            raise RuntimeError("HTTP 429 (rate limited)")
        if method == "getSignaturesForAddress":
            return self.wallets.get(params[0], [])
        return self.txs.get(params[0])


def _sigs(prefix, ages_min, n_extra=0):
    """Signatures newest first, with block times `ages_min` minutes ago."""
    return [{"signature": f"{prefix}{i}", "blockTime": NOW - a * 60, "err": None}
            for i, a in enumerate(sorted(ages_min))] + [
        {"signature": f"{prefix}x{i}", "blockTime": NOW - 10 * 86400, "err": None}
        for i in range(n_extra)]


def _world(dev_age_min=600, funder_age_min=5000, funder=FUNDER, dev_sells=0, busy_funder=False):
    dev = _sigs("d", [1] * dev_sells + [2, 3, dev_age_min])
    first = dev[-1]["signature"]
    txs = {first: {"transaction": {"message": {"instructions": [_transfer(funder, DEV)]}},
                   "meta": {"logMessages": []}}}
    for s in dev[:dev_sells]:
        txs[s["signature"]] = {"meta": {"logMessages": SELL_LOGS}}
    for s in dev[dev_sells:-1]:
        txs[s["signature"]] = {"meta": {"logMessages": BUY_LOGS}}
    fund = [{"signature": f"f{i}", "blockTime": NOW, "err": None} for i in range(1000)] \
        if busy_funder else _sigs("f", [1, funder_age_min])
    return FakeRpc({DEV: dev, funder: fund}, txs)


def _cfg(**kw):
    return FilterConfig(**kw)


async def _check(rpc, cfg=None, store=None):
    return await DevChecker(cfg or _cfg(), rpc, store).check(DEV, now=NOW)


async def test_an_old_dev_funded_by_an_old_wallet_with_no_sells_passes():
    assert await _check(_world()) == []
    assert await _check(_world(busy_funder=True, funder_age_min=1)) == []  # e.g. an exchange


async def test_a_wallet_made_this_hour_is_rejected_without_further_lookups():
    rpc = _world(dev_age_min=20)
    assert await _check(rpc) == ["dev wallet is only 20 min old"]
    assert [m for m, _ in rpc.calls] == ["getSignaturesForAddress"]   # one cheap call
    assert await _check(_world(dev_age_min=20), _cfg(dev_min_wallet_age_min=10)) == []


async def test_a_fresh_parent_funder_is_rejected():
    out = await _check(_world(funder_age_min=30))
    assert out == ["dev funded by a fresh wallet (30 min old)"]
    assert await _check(_world(funder_age_min=30), _cfg(dev_min_funder_age_min=0)) == []


async def test_a_blocklisted_funder_is_rejected():
    out = await _check(_world(funder=MIXER), _cfg(dev_funder_blocklist=[MIXER]))
    assert out == ["dev funded by a blocklisted wallet"]

    class Store:
        def is_blocked(self, w):
            return "funded a rug" if w == MIXER else None
    assert await _check(_world(funder=MIXER), store=Store()) == ["dev funded by a blocklisted wallet"]


async def test_recent_curve_sells_reject_the_dev_with_a_tolerance_option():
    out = await _check(_world(dev_sells=2))
    assert out == ["dev sold into pump.fun curves 2x in the last 24h"]
    assert await _check(_world(dev_sells=2), _cfg(dev_max_curve_sells=2)) == []
    assert await _check(_world(dev_sells=2), _cfg(dev_curve_sell_hours=0)) == []


def test_only_real_pump_sell_instructions_count():
    assert _program_sells(SELL_LOGS) == 1
    fake = ["Program Other111 invoke [1]", "Program log: Instruction: Sell", "Program Other111 success"]
    assert _program_sells(fake) == 0
    nested = [f"Program {PUMP_PROGRAM} invoke [1]", "Program Token111 invoke [2]",
              "Program Token111 success", "Program log: Instruction: Sell",
              f"Program {PUMP_PROGRAM} success"]
    assert _program_sells(nested) == 1


def test_the_funder_is_read_from_inner_instructions_too():
    tx = {"transaction": {"message": {"instructions": []}},
          "meta": {"innerInstructions": [{"instructions": [_transfer("Hop", DEV)]}]}}
    assert funder_of(tx, DEV) == "Hop"
    assert funder_of({"transaction": {"message": {"instructions": [_transfer(DEV, "X")]}}}, DEV) is None


async def test_lookup_failures_follow_the_chosen_option():
    assert await _check(FakeRpc({}, {}, fail=True)) == []
    out = await _check(FakeRpc({}, {}, fail=True), _cfg(dev_check_on_error="skip"))
    assert out and "dev wallet check failed" in out[0]


async def test_results_are_cached_per_wallet():
    rpc = _world()
    chk = DevChecker(_cfg(), rpc)
    await chk.check(DEV, now=NOW)
    n = len(rpc.calls)
    await chk.check(DEV, now=NOW + 60)
    assert len(rpc.calls) == n and chk.known_funder(DEV) == FUNDER


async def test_the_engine_rejects_a_fresh_dev_and_blocks_a_ruggers_funder(tmp_path):
    from sniper.models import Position
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    eng = h.eng
    eng.devcheck.rpc = _world(dev_age_min=10)
    c = Candidate(chain="solana", mint="M1", source="pumpfun", creator=DEV, route="pump")
    assert await eng.dev_problems(c) == ["dev wallet is only 10 min old"]
    watched = Candidate(chain="solana", mint="M2", source="pumpfun", creator=DEV, trigger="dev")
    assert await eng.dev_problems(watched) == []          # your dev watchlist is trusted

    eng.devcheck.cache.clear()
    eng.devcheck.rpc = _world()
    await eng.dev_problems(Candidate(chain="solana", mint="M3", source="pumpfun", creator=DEV))
    pos = Position(mint="M3", symbol="RUG", source="pumpfun", creator=DEV, entry_price=1e-8,
                   tokens_initial=1, tokens_remaining=0, sol_in=0.03, sol_out=0.001)
    pos.closed, pos.close_reason = True, "dev sold"
    await eng._closed(pos)
    assert eng.store.is_blocked(DEV) and eng.store.is_blocked(FUNDER)
    await h.close()


@pytest.mark.parametrize("key", ["filters.dev_min_wallet_age_min", "filters.dev_curve_sell_hours",
                                 "filters.dev_funder_blocklist", "filters.dev_check_on_error"])
def test_every_option_is_a_telegram_setting(key):
    from sniper.settings import BY_KEY
    assert key in BY_KEY
    assert load_config("config.example.yaml").filters.dev_min_wallet_age_min == 60
