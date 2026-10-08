"""Background reads spread over every RPC, extra ones first, and rest one that rate-limits."""
import time

import pytest

from sniper.solana_rpc import ReadPool, RpcError


class Fake:
    def __init__(self, name, fail=None):
        self.name, self.fail, self.calls = name, fail, 0

    async def call(self, method, params=None):
        self.calls += 1
        if self.fail:
            raise RpcError(self.fail)
        return self.name


async def test_rotates_and_rests_a_rate_limited_rpc():
    extra, main = Fake("extra", fail="getTransaction: HTTP 429 (rate limited)"), Fake("main")
    pool = ReadPool([extra, main])
    assert await pool.call("getTransaction") == "main"      # extra limited: fell through
    assert extra.calls == 1
    for _ in range(4):
        assert await pool.call("getTransaction") == "main"
    assert extra.calls == 1                                  # resting: not asked again yet
    extra.fail = None
    pool._resting.clear()
    assert {await pool.call("x"), await pool.call("x")} == {"extra", "main"}   # shared


async def test_all_failing_raises_and_pacing_spaces_calls():
    pool = ReadPool([Fake("a", fail="boom")])
    with pytest.raises(RpcError):
        await pool.call("x")
    paced = ReadPool([Fake("a")], rate=50)
    t = time.monotonic()
    for _ in range(6):
        await paced.call("x")
    assert time.monotonic() - t >= 5 / 50 * 0.9


async def test_the_copy_watcher_skips_trades_pumpportal_already_delivered():
    from sniper.copywatch import CopyPoller
    from tests.test_copywatch import FakeRpc, W, _tx
    rpc, seen = FakeRpc(), []

    async def on_trade(m):
        seen.append(m)
    p = CopyPoller(rpc, lambda: [W], on_trade, known=lambda sig: sig == "KNOWN")
    await p.poll_wallet(W)
    rpc.sigs = [{"signature": "KNOWN", "blockTime": time.time(), "err": None}]
    rpc.txs["KNOWN"] = _tx()
    await p.poll_wallet(W)
    assert seen == [] and not any(m == "getTransaction" for m, _ in rpc.calls)
