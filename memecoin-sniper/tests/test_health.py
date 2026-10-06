"""/health report, automatic service alerts, and per-trade speed tracking."""
import asyncio
import time

import httpx
import pytest
from solders.keypair import Keypair

from sniper import health
from sniper.models import Candidate, Fill
from sniper.solana_rpc import RpcError, SolanaRpc
from tests.test_telegram import Harness

SECRET_RPC = "https://mainnet.helius-rpc.com/?api-key=SECRET123"


# ---- counting what each service does ----

async def test_rpc_remembers_failures_and_rate_limits():
    answers = iter([httpx.Response(200, json={"result": 1}),
                    httpx.Response(429, text="slow down"),
                    httpx.Response(200, json={"error": {"code": -32429, "message": "rate limit"}}),
                    httpx.Response(200, json={"error": {"code": -32602, "message": "bad params"}}),
                    httpx.Response(503, text="down")])
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(answers)))
    rpc = SolanaRpc(SECRET_RPC, http)
    await rpc.call("getSlot")
    for _ in range(4):
        with pytest.raises(RpcError):
            await rpc.call("getSlot")
    calls, failed, limited = rpc.outcomes(60)
    # a "bad params" answer is about the request, not the service failing
    assert (calls, failed, limited) == (5, 3, 2)
    await http.aclose()


# ---- alerts ----

async def test_each_problem_is_alerted_once_and_its_fix_once(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    eng.stream.down_since = None
    assert all(p is None for p in health.problems(eng).values())
    for _ in range(12):                                  # RPC refusing most requests
        eng.rpc._note(True, True)
    for _ in range(6):
        eng.jupiter.limited.append(time.monotonic())
    eng.stream.down_since = time.monotonic() - 600       # PumpPortal down 10 minutes
    probs = health.problems(eng)
    assert probs["Your Solana RPC"] and "helius" in probs["Your Solana RPC"].lower()
    assert probs["Jupiter"] and probs["The PumpPortal connection"]
    for name, p in probs.items():
        eng.health.update(name, p)
    for name, p in health.problems(eng).items():         # next minute: still broken
        eng.health.update(name, p)
    await eng.settle()
    alerts = [t for t, _, _ in h.sent if t.startswith("⚠️")]
    assert len(alerts) == 3                              # once each, not every minute
    eng.rpc.recent.clear()
    eng.stream.down_since = None
    for name, p in health.problems(eng).items():
        eng.health.update(name, p)
    await eng.settle()
    assert any("Your Solana RPC is working normally again" in t for t, _, _ in h.sent)
    assert "Jupiter" in eng.health.down                  # still limited: still open
    await h.close()


async def test_jito_alert_counts_regions_refusing_bundles(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    eng.stream.down_since = None

    class S:
        jito_failures = [time.monotonic()] * 3
    eng.executor.sender = S()
    assert health.problems(eng)["Jito"]
    await h.close()


async def test_watch_loop_sends_the_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "CHECK_SECONDS", 0.01)
    h = Harness(tmp_path)
    eng = h.eng
    eng.stream.down_since = time.monotonic() - 999
    task = asyncio.ensure_future(health.watch(eng))
    await asyncio.sleep(0.05)
    task.cancel()
    await eng.settle()
    assert any("Lost the connection to PumpPortal" in t for t, _, _ in h.sent)
    await h.close()


# ---- the /health report ----

def _mock_http(fail_hosts=()):
    def handler(req):
        if req.url.host in fail_hosts:
            raise httpx.ConnectError("unreachable")
        if req.method == "POST" and "rpc" in req.url.host:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 123})
        if "jup.ag" in req.url.host:
            return httpx.Response(200, json={"outAmount": "1500000", "outputMint": "USDC"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_health_report_checks_everything_and_never_shows_the_rpc_key(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    eng.cfg.discovery.momentum_enabled = True
    mock = _mock_http(fail_hosts={"tokyo.mainnet.block-engine.jito.wtf"})
    eng.http = eng.jupiter.http = mock
    eng.rpc = SolanaRpc(SECRET_RPC, mock)
    eng.stream.down_since, eng.stream.last_message = None, time.monotonic() - 3
    text = await health.report(eng)
    assert "SECRET123" not in text and "mainnet.helius-rpc.com" in text
    assert "✅ <b>Solana RPC</b>" in text and "PumpPortal feed</b> connected" in text
    assert "4/5 regions reachable" in text and "tokyo" in text
    assert "✅ <b>Jupiter</b>" in text and "GeckoTerminal" in text and "DexScreener" in text
    eng.stream.feed_ok, eng.stream.feed_error = False, "Minimum balance not met"
    assert "live trade feed refused: Minimum balance not met" in await health.report(eng)
    await mock.aclose()
    await h.close()


async def test_health_from_telegram(tmp_path):
    h = Harness(tmp_path)
    mock = _mock_http()
    h.eng.http = h.eng.jupiter.http = mock
    h.eng.rpc = SolanaRpc("https://rpc.example.com", mock)
    await h.text("/menu")
    assert "hl" in h.buttons()
    await h.tap("hl")
    assert "Health check" in h.last and "hl" in h.buttons()
    await h.text("/health")
    assert "Health check" in h.last
    await mock.aclose()
    await h.close()


# ---- speed ----

async def test_live_trades_record_how_long_each_step_took():
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    ex, rpc = live_executor(load_config(None))

    async def pp(*a, **k):
        await asyncio.sleep(0.02)                        # PumpPortal building it
        return b"tx"
    ex._pumpportal_tx = pp

    async def confirm(sig):
        await asyncio.sleep(0.03)
        return True
    rpc.confirm = confirm
    fill = await ex.buy(Candidate(chain="solana", mint="M", source="pumpfun", route="pump"),
                        0.1, None)
    t = fill.timings
    assert t["build"] >= 0.015 and t["confirm"] >= 0.025 and "send" in t


async def test_speed_is_stored_with_trades_and_shown_in_stats(tmp_path):
    from sniper.stats import format_summary, speed, summarize
    h = Harness(tmp_path)
    eng = h.eng
    eng.paused = False

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol, signature="S",
                    timings={"build": 0.05, "send": 0.1, "confirm": 1.5})
    eng.executor.buy = buy

    async def sell(*a, **k):
        return Fill(tokens=1000.0, sol=0.06, signature="S2",
                    timings={"build": 0.04, "send": 0.08, "confirm": 1.2})
    eng.executor.sell = sell
    m = str(Keypair().pubkey())
    c = Candidate(chain="solana", mint=m, source="pumpfun", route="pump", symbol="T")
    c.queued_at = time.monotonic() - 0.4                 # seen 0.4s ago
    await eng.try_buy(c)
    ev = eng.store.events("buy")[-1]
    assert ev["timing"]["build"] == 0.05 and 0.35 <= ev["timing"]["decide"] <= 1.0
    await eng.manual_sell(m, 100)
    sev = eng.store.events("sell")[-1]
    assert sev["timing"]["confirm"] == 1.2 and "total" in sev["timing"]
    sp = speed(eng.store)
    assert sp["buy"]["confirm"] == 1.5 and sp["sell"]["send"] == 0.08 and "decide" not in sp["sell"]
    text = format_summary(summarize(eng.store))
    assert "Speed (median seconds):" in text and "confirm 1.50s" in text
    await h.close()


async def test_confirmation_window_is_not_counted_as_slowness(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol, signature="S")
    eng.executor.buy = buy
    c = Candidate(chain="solana", mint=str(Keypair().pubkey()), source="pumpfun", route="pump")
    c.queued_at, c.window_s = time.monotonic() - 6.3, 6.0  # 6s watching, 0.3s deciding
    await eng.try_buy(c)
    assert eng.store.events("buy")[-1]["timing"]["decide"] < 1.0
    await h.close()


def test_stats_without_timings_look_as_before(tmp_path):
    from sniper.stats import format_summary, summarize
    from sniper.store import Store
    st = Store(str(tmp_path), "paper")
    st.event("close", "m", "T", pnl_sol=0.01, reason="trailing stop", source="pumpfun")
    assert "Speed" not in format_summary(summarize(st))
