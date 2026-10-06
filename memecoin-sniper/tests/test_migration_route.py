"""A just-graduated coin is bought once Jupiter can route it, not failed straight away."""
import sniper.engine as engine_mod
from sniper.models import Candidate, Fill
from tests.test_telegram import Harness


def _setup(tmp_path, monkeypatch, routable_after):
    monkeypatch.setattr(engine_mod, "ROUTE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(engine_mod, "ROUTE_WAIT_SECONDS", 0.2)
    h = Harness(tmp_path)
    e = h.eng
    e.paused = False
    calls = []

    async def quote(*a, **k):
        calls.append(1)
        if len(calls) <= routable_after:
            raise RuntimeError('jupiter quote 400: {"error":"No routes found"}')
        return {"outAmount": "1000"}
    e.jupiter.quote = quote

    async def evaluate(c):
        from sniper.models import SafetyReport
        return SafetyReport(passed=True)
    e.safety.evaluate = evaluate
    bought = []

    async def buy(c, sol, curve):
        bought.append(c.mint)
        return Fill(tokens=1000.0, sol=sol, signature="S")
    e.executor.buy = buy
    return h, calls, bought


def _migration():
    return Candidate(chain="solana", mint="So1idMint" + "1" * 35, source="pumpfun-migration",
                     route="pump", symbol="G")


async def test_waits_for_the_route_then_buys(tmp_path, monkeypatch):
    h, calls, bought = _setup(tmp_path, monkeypatch, routable_after=2)
    res = await h.eng.handle_candidate(_migration())
    assert res.startswith("🟢") and len(calls) == 3 and bought
    await h.close()


async def test_gives_up_quietly_when_no_route_appears(tmp_path, monkeypatch):
    h, calls, bought = _setup(tmp_path, monkeypatch, routable_after=10**6)
    res = await h.eng.handle_candidate(_migration())
    assert "no trading route yet" in res and not bought and len(calls) >= 2
    await h.close()
