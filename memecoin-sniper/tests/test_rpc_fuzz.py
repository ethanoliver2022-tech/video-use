"""Garbage from the RPC node and Jupiter must never crash an action or corrupt a position."""
import math
import random

import pytest
from solders.keypair import Keypair

from sniper.config import load_config
from sniper.engine import Engine
from sniper.models import Candidate, Position, SafetyReport
from sniper.solana_rpc import RpcError, SolanaRpc

GARBAGE = [None, {}, [], "abc", -1, 0, float("nan"), float("inf"), {"value": None},
           {"value": "x"}, {"value": [None]}, {"value": -5}, True, 10**40, {"outAmount": "abc"},
           {"outAmount": "-7", "outputMint": "So11111111111111111111111111111111111111112"},
           {"outAmount": "1000", "outputMint": None}, {"swapTransaction": 12}]


SANE = {
    "getBalance": {"value": 5_000_000_000},
    "getTokenAccountsByOwner": {"value": [{"account": {"data": {"parsed": {"info": {
        "tokenAmount": {"amount": "1000000000", "decimals": 6}}}}}}]},
    "getSignatureStatuses": {"value": [{"confirmationStatus": "confirmed", "err": None}]},
    "getAccountInfo": {"value": None},
    "getRecentPrioritizationFees": [],
    "sendTransaction": "SIG",
}
RAW_GARBAGE = [None, {}, [], "abc", -1, 0, True, {"value": None}, {"value": "x"},
               {"value": [None]}, {"value": -5}, {"value": [1, 2]}, {"value": {"data": [12]}},
               {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {
                   "amount": "-5", "decimals": 99}}}}}}]},
               {"value": [{"confirmationStatus": 7}]}, {"value": {"data": ["!!notbase64!!"]}}]


class GarbageRpc(SolanaRpc):
    """The real RPC wrappers, fed malformed raw node responses."""

    def __init__(self, rng):
        super().__init__("https://rpc.invalid")
        self.rng = rng

    async def call(self, method, params=None):
        r = self.rng.random()
        if r < 0.25:
            raise RpcError(f"{method}: HTTP 502")
        if r < 0.6:
            return self.rng.choice(RAW_GARBAGE)
        return SANE.get(method)

    async def confirm(self, signature, timeout=0.05):
        return await super().confirm(signature, timeout)

    async def get_transaction(self, signature):
        return self.rng.choice([None, {}, {"meta": None}, {"transaction": []}])


class GarbageJupiter:
    def __init__(self, rng):
        self.rng = rng

    async def quote(self, *a, **k):
        if self.rng.random() < 0.3:
            raise RuntimeError("jupiter down")
        return self.rng.choice(GARBAGE + [{"outAmount": "1000", "outputMint": "M"}])

    async def out_ui(self, q):
        from sniper.execution.executors import Jupiter
        j = Jupiter("x", None, None)
        j._decimals["M"] = 6
        j._decimals[None] = 6
        return await j.out_ui(q)

    async def swap_tx(self, *a, **k):
        if self.rng.random() < 0.5:
            raise RuntimeError("swap failed")
        return b"tx"


def finite_pos(p):
    for v in (p.last_price, p.peak_price, p.entry_price, p.tokens_remaining, p.tokens_initial,
              p.sol_in, p.sol_out):
        assert isinstance(v, (int, float)) and math.isfinite(v) and v >= 0, (p, v)


@pytest.mark.parametrize("live", [False, True])
async def test_garbage_rpc_never_crashes_actions(tmp_path, live):
    rng = random.Random(11 + live)
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    cfg.trading.daily_loss_limit_sol = 1e9
    cfg.trading.max_open_positions = 1000  # let buys reach the executor
    cfg.trading.min_sol_reserve = 0
    if live:
        from sniper.execution.wallet import WalletManager
        WalletManager(cfg.data_dir).create()
    eng = Engine(cfg, live=live)

    async def ws(p):
        pass
    eng.stream._send = ws

    async def tg(*a, **k):
        pass
    eng.notifier.telegram = tg
    import sniper.solana_rpc as srpc
    real_sleep = srpc.asyncio.sleep

    async def fast_sleep(*_):
        await real_sleep(0)
    srpc.asyncio.sleep = fast_sleep
    rpc, jup = GarbageRpc(rng), GarbageJupiter(rng)
    eng.rpc = rpc
    eng.jupiter = jup
    ex = eng.executor
    if live:
        ex.rpc, ex.jupiter = rpc, jup
        ex._sign = lambda b: b

        class Sender:
            async def priority_fee(self):
                return 0.0001

            async def send(self, tx, payer):
                if rng.random() < 0.3:
                    raise RuntimeError("all paths failed")
                return "SIG"
        ex.sender = Sender()

        async def pp(*a, **k):
            if rng.random() < 0.3:
                raise RuntimeError("pumpportal 500")
            return b"tx"
        ex._pumpportal_tx = pp
    else:
        ex.jupiter = jup

    async def ok(c):
        return SafetyReport(passed=True)
    eng.safety.evaluate = ok
    mints = [str(Keypair().pubkey()) for _ in range(5)]
    for step in range(400):
        m = rng.choice(mints)
        if m not in eng.positions or eng.positions[m].closed:
            eng.positions[m] = Position(mint=m, symbol="G", source="manual", creator=None,
                                        entry_price=1e-6, tokens_initial=1000.0,
                                        tokens_remaining=1000.0, sol_in=0.001, route=rng.choice(
                                            ["pump", "jupiter"]))
            eng.store.save_position(eng.positions[m])
        action = rng.choice(["buy", "sell", "poll", "reconcile", "restore", "price", "risk"])
        try:
            if action == "buy":
                res = await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()),
                                                  source="manual", symbol="B", force=True,
                                                  route=rng.choice(["pump", "jupiter"])))
                assert isinstance(res, str)
            elif action == "sell":
                res = await eng.manual_sell(m, rng.choice([25, 100]))
                assert isinstance(res, str)
            elif action == "poll":
                await eng._poll_position(eng.positions[m])
            elif action == "reconcile":
                await eng.reconcile_pending()
            elif action == "restore":
                await eng.restore()
            elif action == "price":
                try:
                    await eng.price_of(m)
                except (ValueError, RuntimeError, TypeError, KeyError):
                    pass  # price_of may raise; its callers handle that
            elif action == "risk":
                res = await eng.risk_block(0.05)
                assert res is None or isinstance(res, str)
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"step {step} {action} raised {e!r}")
        await eng.settle()
        for p in eng.positions.values():
            finite_pos(p)
    srpc.asyncio.sleep = real_sleep
    await eng.http.aclose()
    await rpc.http.aclose()
