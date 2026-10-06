"""Health: a `/health` report that checks every service live, and automatic alerts when
one starts failing quietly (one message when it breaks, one when it's fixed).

Never prints a full RPC URL: paid RPC URLs carry the API key. Only the host is shown.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from .models import SOL_MINT

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

CHECK_SECONDS = 60
RPC_WINDOW = 300            # judge the RPC over the last 5 minutes
RPC_MIN_CALLS = 10          # ...once there's enough traffic to judge
RPC_BAD_SHARE = 0.5         # more than half failing
RPC_LIMITED = 5             # or this many rate-limited answers
PUMPPORTAL_DOWN = 120       # websocket down this long: launches are being missed
JITO_WINDOW, JITO_FAILS = 600, 3
JUPITER_WINDOW, JUPITER_LIMITED = 600, 5
PROBE_TIMEOUT = 6.0
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def host(url: str) -> str:
    return urlsplit(url).hostname or "?"


def _recent(times, window: float) -> int:
    since = time.monotonic() - window
    return sum(1 for t in times if t >= since)


class HealthWatch:
    """Remembers what's broken so each problem is announced once, and its fix once."""

    def __init__(self, notify: Callable[[str, bool], None]):
        self.notify = notify
        self.down: dict[str, str] = {}

    def update(self, name: str, problem: Optional[str]) -> None:
        if problem and name not in self.down:
            self.down[name] = problem
            self.notify(f"⚠️ {problem}", False)
        elif not problem and name in self.down:
            del self.down[name]
            self.notify(f"✅ {name} is working normally again.", True)


def problems(eng: "Engine") -> dict[str, Optional[str]]:
    """What's wrong right now, per service (None = fine). Cheap: reads counters only."""
    out: dict[str, Optional[str]] = {}
    calls, failed, limited = eng.rpc.outcomes(RPC_WINDOW)
    rpc_bad = calls >= RPC_MIN_CALLS and (failed > calls * RPC_BAD_SHARE or limited >= RPC_LIMITED)
    out["Your Solana RPC"] = (
        f"Your Solana RPC ({host(eng.rpc.url)}) is refusing requests: {failed} of {calls} failed "
        f"in the last 5 minutes ({limited} rate-limited). Prices, exits and buys can be delayed. "
        "On a free plan you may have hit its limit: check your RPC dashboard (e.g. helius.dev), "
        "or upgrade the plan." if rpc_bad else None)
    s = eng.stream
    down = s.down_since is not None and time.monotonic() - s.down_since > PUMPPORTAL_DOWN
    out["The PumpPortal connection"] = (
        f"Lost the connection to PumpPortal {(time.monotonic() - s.down_since) / 60:.0f} min ago: "
        "new pump.fun launches aren't being seen. It keeps reconnecting by itself."
        if down else None)
    sender = getattr(eng.executor, "sender", None)
    fails = _recent(getattr(sender, "jito_failures", ()), JITO_WINDOW)
    out["Jito"] = (
        f"Jito refused every region {fails} times in the last 10 minutes: trades are going out "
        "through your RPC instead (no sandwich protection)." if fails >= JITO_FAILS else None)
    lim = _recent(eng.jupiter.limited, JUPITER_WINDOW)
    out["Jupiter"] = (
        f"Jupiter rate-limited the bot {lim} times in the last 10 minutes (exits on graduated "
        "tokens can be slower). A free JUPITER_API_KEY in .env, or JUPITER_RPM for a paid plan, "
        "helps." if lim >= JUPITER_LIMITED else None)
    return out


async def watch(eng: "Engine") -> None:
    """Background: check the counters every minute and alert on changes."""
    hw = eng.health
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        for name, problem in problems(eng).items():
            hw.update(name, problem)


# ---------- the /health report ----------

async def _timed(coro: Awaitable) -> tuple[Optional[float], Optional[str]]:
    """(milliseconds, None) or (None, short error)."""
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(coro, PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return None, "no answer"
    except Exception as e:
        return None, (str(e) or type(e).__name__)[:80]
    return (time.perf_counter() - t0) * 1000, None


def _line(ok: bool, name: str, detail: str) -> str:
    return f"{'✅' if ok else '❌'} <b>{name}</b> {detail}"


async def report(eng: "Engine") -> str:
    """Checks every service live (in parallel) and formats the answer for Telegram."""
    import html
    esc = html.escape
    cfg, d = eng.cfg, eng.cfg.discovery
    http = eng.http

    async def head(url: str):
        r = await http.head(url, timeout=PROBE_TIMEOUT)
        return r

    async def jup_probe():
        from .execution.executors import JupiterBusy
        try:
            q = await eng.jupiter.quote(SOL_MINT, USDC, 0.01, 1, urgent=False)
        except JupiterBusy:
            raise RuntimeError("busy") from None
        if "outAmount" not in q:
            raise RuntimeError("no route")

    jobs: dict[str, Awaitable] = {"rpc": eng.rpc.call("getSlot")}
    for i, r in enumerate(getattr(getattr(eng.executor, "sender", None), "extra", []) or []):
        jobs[f"extra{i}"] = r.call("getSlot")
    async def pp_probe():
        # the trade endpoint itself, with an empty request: it answers "bad request" at
        # once (nothing is built). The homepage can be slow while trading works fine.
        await http.post(cfg.endpoints.pumpportal_trade, data={}, timeout=PROBE_TIMEOUT)

    jobs["pp_api"] = pp_probe()
    if cfg.speed.jito_enabled:
        for u in dict.fromkeys(cfg.speed.jito_block_engines):
            jobs[f"jito:{host(u)}"] = head(f"https://{host(u)}/")
    jobs["jupiter"] = jup_probe()
    if (d.geckoterminal_enabled and d.geckoterminal_networks) or d.momentum_enabled:
        jobs["gecko"] = head(f"https://{host(cfg.endpoints.geckoterminal_api)}/")
    if d.dexscreener_profiles or d.momentum_enabled:
        jobs["dex"] = head(f"https://{host(cfg.endpoints.dexscreener_api)}/")
    names = list(jobs)
    results = dict(zip(names, await asyncio.gather(*(_timed(j) for j in jobs.values()))))

    def ms(key: str) -> tuple[bool, str]:
        t, err = results[key]
        return (t is not None), (f"{t:.0f} ms" if t is not None else f"— {esc(err or '?')}")

    lines = ["🩺 <b>Health check</b>"]
    calls, failed, limited = eng.rpc.outcomes(RPC_WINDOW)
    ok, txt = ms("rpc")
    usage = (f" · last 5 min: {failed}/{calls} failed" + (f", {limited} rate-limited" if limited else "")
             if calls else "")
    lines.append(_line(ok and not (calls >= RPC_MIN_CALLS and failed > calls * RPC_BAD_SHARE),
                       "Solana RPC", f"({esc(host(eng.rpc.url))}) {txt}{usage}"))
    if "api.mainnet-beta.solana.com" in eng.rpc.url:
        lines.append("   ⚠️ public RPC: set SOLANA_RPC_URL (e.g. Helius) before going live")
    for k in names:
        if k.startswith("extra"):
            ok, txt = ms(k)
            lines.append(_line(ok, "Extra RPC", txt))

    s = eng.stream
    if s.down_since is None:
        age = (f", last message {time.monotonic() - s.last_message:.0f}s ago"
               if s.last_message else "")
        lines.append(_line(True, "PumpPortal feed", f"connected{age}"))
    else:
        lines.append(_line(False, "PumpPortal feed",
                           f"disconnected for {time.monotonic() - s.down_since:.0f}s (reconnecting)"))
    if not s.trades_enabled:
        lines.append("   ➖ live trade feed: no API key (on-chain checks)")
    elif s.trades_live:
        lines.append("   ✅ live trade feed: on")
    else:
        lines.append(f"   ❌ live trade feed refused: {esc(s.feed_error)} (top up the wallet "
                     "linked to your PumpPortal key, min 0.02 SOL)")
    ok, txt = ms("pp_api")
    lines.append(_line(ok, "PumpPortal trading", txt))

    jito = [k for k in names if k.startswith("jito:")]
    if jito:
        up = [k for k in jito if results[k][0] is not None]
        best = min((results[k][0] for k in up), default=None)
        detail = f"{len(up)}/{len(jito)} regions reachable" + (f", fastest {best:.0f} ms" if best else "")
        lines.append(_line(bool(up), "Jito", detail))
        down = [k.split(":", 1)[1].split(".")[0] for k in jito if k not in up]
        if down:
            lines.append(f"   unreachable: {esc(', '.join(down))}")
    else:
        lines.append("➖ <b>Jito</b> off")

    ok, txt = ms("jupiter")
    if not ok and "busy" in txt.lower():
        ok, txt = True, "busy with trades (rate budget in use), fine"
    lines.append(_line(ok, "Jupiter", f"{txt} · budget {eng.jupiter.budget()}/min"))
    for key, name in (("gecko", "GeckoTerminal"), ("dex", "DexScreener")):
        if key in results:
            ok, txt = ms(key)
            lines.append(_line(ok, name, txt))
        else:
            lines.append(f"➖ <b>{name}</b> off")

    if eng.live:
        try:
            bal = await eng.rpc.get_balance_sol(eng.own_wallet)
            low = bal < cfg.trading.buy_amount_sol + cfg.trading.min_sol_reserve + 0.01
            lines.append(_line(not low, "Wallet", f"{bal:.4f} SOL" + (" (too low to buy)" if low else "")))
        except Exception as e:
            lines.append(_line(False, "Wallet", f"balance unreadable ({esc(str(e)[:60])})"))
    else:
        lines.append("📝 <b>Mode</b> paper (no real trades)")

    if eng.health.down:
        lines.append("\n<b>Open alerts:</b> " + esc("; ".join(eng.health.down)))
    from .stats import speed_line
    sp = speed_line(eng.store)
    if sp:
        lines.append("\n⏱ " + esc(sp))
    return "\n".join(lines)
