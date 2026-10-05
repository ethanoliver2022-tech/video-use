"""Hostile external data: malformed / adversarial payloads from every outside service
must never crash the bot or corrupt prices and positions."""
import json
import logging
import math
import random

import httpx
from solders.keypair import Keypair

from sniper.config import FilterConfig, load_config
from sniper.engine import Engine
from sniper.models import Candidate, Position
from sniper.safety import SafetyChecker
from sniper.scanners.multichain import DexScreenerScanner, GeckoTerminalScanner, parse_gecko_pools

WEIRD = [None, "", "abc", "NaN", "-5", -5, 0, -1e9, 1e308, float("nan"), float("inf"),
         float("-inf"), [], {}, True, "1e999", " 12 ", 3, 2.5e-9, "2026-13-45T99:99:99Z"]


def weird(rng):
    return rng.choice(WEIRD)


class ErrorCatcher(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def engine(tmp_path):
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    eng = Engine(cfg, live=False)

    async def ws(p):
        pass
    eng.stream._send = ws

    async def tg(*a, **k):
        pass
    eng.notifier.telegram = tg
    return eng


def finite_pos(p: Position):
    for v in (p.last_price, p.peak_price, p.entry_price, p.tokens_remaining, p.sol_out):
        assert isinstance(v, (int, float)) and math.isfinite(v) and v >= 0, (p, v)


async def test_pumpportal_garbage_never_corrupts_positions(tmp_path):
    rng = random.Random(1)
    eng = engine(tmp_path)
    catcher = ErrorCatcher()
    logging.getLogger("sniper").addHandler(catcher)
    logging.getLogger("sniper.scanners.pumpportal").addHandler(catcher)
    mint = str(Keypair().pubkey())
    eng.positions[mint] = Position(mint=mint, symbol="P", source="pumpfun", creator="DEV",
                                   entry_price=1e-6, tokens_initial=1e6, tokens_remaining=1e6,
                                   sol_in=1.0, route="pump")
    keys = ["mint", "txType", "traderPublicKey", "solAmount", "tokenAmount", "vSolInBondingCurve",
            "vTokensInBondingCurve", "marketCapSol", "initialBuy", "name", "symbol", "uri",
            "pool", "signature", "bondingCurveKey"]
    for i in range(3000):
        msg = {k: weird(rng) for k in keys if rng.random() < 0.7}
        if rng.random() < 0.6:
            msg["mint"] = mint
        if rng.random() < 0.7:
            msg["txType"] = rng.choice(["create", "buy", "sell", "migrate", None, 5])
        raw = json.dumps(msg, allow_nan=True) if rng.random() < 0.9 else rng.choice(
            ["", "not json", "[1,2]", "null", "{\"mint\": NaN}", "{\"mint\": Infinity}"])
        await eng.stream._dispatch(raw)
    await eng.settle()
    finite_pos(eng.positions[mint])
    for c in eng.curves.values():
        assert math.isfinite(c.v_sol) and math.isfinite(c.v_tokens) and c.v_sol > 0 and c.v_tokens > 0
    logging.getLogger("sniper").removeHandler(catcher)
    logging.getLogger("sniper.scanners.pumpportal").removeHandler(catcher)
    assert not catcher.records, [r.getMessage() for r in catcher.records][:3]
    await eng.http.aclose()


def test_gecko_parser_survives_garbage():
    rng = random.Random(2)
    for _ in range(3000):
        pool = {"attributes": {k: weird(rng) for k in
                               ("name", "address", "pool_created_at", "reserve_in_usd", "fdv_usd")},
                "relationships": rng.choice([
                    {"base_token": {"data": {"id": f"solana_{Keypair().pubkey()}"}}},
                    {"base_token": {"data": {"id": weird(rng)}}}, weird(rng), {}])}
        payload = rng.choice([{"data": [pool, pool]}, {"data": weird(rng)}, weird(rng), {}])
        try:
            out = parse_gecko_pools("solana", payload if isinstance(payload, dict) else {})
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"parser crashed on {payload!r}: {e!r}")
        for c in out:
            for v in (c.liquidity_usd, c.fdv_usd):
                assert v is None or (math.isfinite(v) and v >= 0), v


async def test_scanners_survive_garbage_http():
    rng = random.Random(3)
    got = []

    async def on_candidate(c):
        got.append(c)

    def handler(req):
        body = rng.choice([
            [{"chainId": weird(rng), "tokenAddress": weird(rng)}],
            [{"chainId": "solana", "tokenAddress": str(Keypair().pubkey())}],
            {"data": [{"attributes": {"pool_created_at": weird(rng)}}]},
            [{"baseToken": weird(rng), "liquidity": weird(rng), "pairCreatedAt": weird(rng),
              "fdv": weird(rng)}],
            [{"baseToken": {"address": str(Keypair().pubkey()), "symbol": weird(rng)},
              "liquidity": {"usd": weird(rng)}, "pairCreatedAt": weird(rng)}],
            weird(rng)])
        return httpx.Response(200, text=json.dumps(body, allow_nan=True))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gecko = GeckoTerminalScanner("https://g", ["solana", "base"], 0, on_candidate, http)
    dex = DexScreenerScanner("https://d", 0, on_candidate, http)
    import sniper.scanners.multichain as mc
    real_sleep = mc.asyncio.sleep

    async def no_sleep(*_):
        await real_sleep(0)
    mc.asyncio.sleep = no_sleep
    try:
        for _ in range(500):
            for tick in (gecko._tick, dex._tick):
                try:
                    await tick()
                except Exception as e:  # noqa: BLE001
                    raise AssertionError(f"scanner tick crashed: {e!r}")
    finally:
        mc.asyncio.sleep = real_sleep
    for c in got:
        assert isinstance(c.mint, str) and c.mint
        assert isinstance(c.created_at, float) and math.isfinite(c.created_at)
        for v in (c.liquidity_usd, c.fdv_usd):
            assert v is None or (math.isfinite(v) and v >= 0), v
    await http.aclose()


async def test_rugcheck_garbage_never_crashes_safety():
    rng = random.Random(4)

    def handler(req):
        body = rng.choice([{"risks": weird(rng)}, {"risks": [weird(rng), {"level": weird(rng)}]},
                           weird(rng), {"score_normalised": weird(rng)}])
        return httpx.Response(200, text=json.dumps(body, allow_nan=True))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chk = SafetyChecker(FilterConfig(), None, http, "https://r")
    from sniper.models import SafetyReport
    for _ in range(500):
        r = SafetyReport(passed=True)
        await chk._rugcheck(Candidate(chain="solana", mint="M", source="x"), r)
    await http.aclose()
