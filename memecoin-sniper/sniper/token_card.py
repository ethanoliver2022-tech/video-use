"""Token research card: paste an address in Telegram, get everything needed to decide.

Market data comes from DexScreener, curve progress and dev holdings from the chain,
and the verdict from the same safety checks the sniper uses. Every lookup is
independent: one failing API never breaks the card.
"""
from __future__ import annotations

import asyncio
import html
import math
import logging
import time
from typing import TYPE_CHECKING, Optional

from .models import PUMP_TOTAL_SUPPLY, Candidate, num
from .pump_curve import fetch_curve

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)
esc = html.escape
BUY_AMOUNTS = (0.05, 0.1, 0.25, 0.5, 1.0)


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _usd(v) -> str:
    v = num(v, allow_zero=True)
    if v is None:
        return "n/a"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suffix}"
    return f"${v:,.2f}" if v >= 1 else f"${v:.6g}"


def _pct(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"{v:+.1f}%" if math.isfinite(v) else "n/a"


def _age(created_ms) -> str:
    created_ms = num(created_ms)
    if not created_ms:
        return "n/a"
    s = max(0, time.time() - created_ms / 1000)
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


async def _dexscreener(engine: "Engine", mint: str) -> Optional[dict]:
    resp = await engine.http.get(f"{engine.cfg.endpoints.dexscreener_api}/tokens/v1/solana/{mint}",
                                 timeout=8)
    if resp.status_code != 200:
        return None
    pairs = [p for p in (resp.json() or []) if isinstance(p, dict)]
    if not pairs:
        return None
    return max(pairs, key=lambda p: num(_d(p.get("liquidity")).get("usd")) or 0)


async def build_card(engine: "Engine", mint: str) -> tuple[str, list]:
    async def safe(coro):
        try:
            return await coro
        except Exception as e:
            log.debug("card lookup failed: %s", e)
            return None

    pair, curve = await asyncio.gather(safe(_dexscreener(engine, mint)),
                                       safe(fetch_curve(engine.rpc, mint)))
    pair = pair or {}
    base = _d(pair.get("baseToken"))
    name = base.get("name") if isinstance(base.get("name"), str) else ""
    symbol = base.get("symbol") if isinstance(base.get("symbol"), str) and base["symbol"] else mint[:6]
    route = "pump" if (curve and not curve.complete) or mint.endswith("pump") else "jupiter"
    cand = Candidate(chain="solana", mint=mint, source="manual", symbol=symbol, name=name,
                     creator=curve.creator if curve else None, route=route,
                     liquidity_usd=num(_d(pair.get("liquidity")).get("usd"), allow_zero=True),
                     fdv_usd=num(pair.get("marketCap")) or num(pair.get("fdv")))
    report = await safe(engine.safety.evaluate(cand))

    dev_pct = None
    if curve and curve.creator:
        held = await safe(engine.rpc.get_token_balance(curve.creator, mint))
        if held is not None:
            dev_pct = held / PUMP_TOTAL_SUPPLY * 100

    lines = [f"🪙 <b>{esc(name or symbol)}</b> (${esc(symbol)})", f"<code>{mint}</code>", ""]
    if pair:
        vol, chg = _d(pair.get("volume")), _d(pair.get("priceChange"))
        tx = _d(_d(pair.get("txns")).get("h1"))
        lines += [
            f"💲 Price {_usd(pair.get('priceUsd'))} · "
            f"MC {_usd(num(pair.get('marketCap')) or pair.get('fdv'))}",
            f"💧 Liquidity {_usd(_d(pair.get('liquidity')).get('usd'))} · "
            f"Vol 1h {_usd(vol.get('h1'))} · 24h {_usd(vol.get('h24'))}",
            f"📈 5m {_pct(chg.get('m5'))} · 1h {_pct(chg.get('h1'))} · 24h {_pct(chg.get('h24'))}",
            f"🔁 1h trades: {num(tx.get('buys'), True) or 0:.0f} buys / "
            f"{num(tx.get('sells'), True) or 0:.0f} sells · "
            f"age {_age(pair.get('pairCreatedAt'))} · {esc(pair['dexId'] if isinstance(pair.get('dexId'), str) else '?')}",
        ]
    else:
        lines.append("No DEX pair data yet (brand new, or not indexed).")
    if curve:
        if curve.complete:
            lines.append("🎓 Graduated off the pump.fun curve")
        else:
            mc_sol = curve.price * PUMP_TOTAL_SUPPLY
            lines.append(f"📊 Bonding curve {curve.progress_pct:.0f}% · MC {mc_sol:,.0f} SOL")
    if dev_pct is not None:
        lines.append(f"👨‍💻 Dev holds {dev_pct:.2f}%")
    if report:
        lines.append("")
        if report.passed:
            lines.append("✅ <b>Passes your filters</b>")
        else:
            lines.append("❌ <b>Fails your filters:</b> " + esc("; ".join(report.reasons)))
        if report.notes:
            lines.append("ℹ️ " + esc("; ".join(report.notes)))
    held = engine.positions.get(mint)
    if held and not held.closed:
        lines.append(f"\n📦 You hold this: PnL {held.pnl_pct:+.0f}%")

    buttons = [
        [(f"Buy {a:g}", f"b:{mint}:{a:g}") for a in BUY_AMOUNTS[:3]],
        [(f"Buy {a:g}", f"b:{mint}:{a:g}") for a in BUY_AMOUNTS[3:]] + [("✏️ Custom", f"bc:{mint}")],
        [("📋 Limit buy", f"lb:{mint}"), ("🔄 Refresh", f"tc:{mint}")],
    ]
    if held and not held.closed:
        buttons.insert(0, [("Sell 50%", f"s:{mint}:50"), ("Sell 100%", f"s:{mint}:100"),
                           ("📋 Limit sell", f"ls:{mint}")])
    links = [("DexScreener", f"https://dexscreener.com/solana/{mint}")]
    if route == "pump" or curve:
        links.append(("pump.fun", f"https://pump.fun/coin/{mint}"))
    links.append(("Solscan", f"https://solscan.io/token/{mint}"))
    buttons.append(links)
    if report and not report.passed:
        buttons.append([(f"⚠️ Buy {engine.cfg.trading.buy_amount_sol:g}, skip filters", f"bf:{mint}:")])
    return "\n".join(lines), buttons
