"""Token research card: paste an address in Telegram, get everything needed to decide.

Market data comes from DexScreener, curve progress and dev holdings from the chain,
and the verdict from the same safety checks the sniper uses. Every lookup is
independent: one failing API never breaks the card.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING, Optional

from .models import PUMP_TOTAL_SUPPLY, Candidate
from .pump_curve import fetch_curve

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)
esc = html.escape
BUY_AMOUNTS = (0.05, 0.1, 0.25, 0.5, 1.0)


def _usd(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suffix}"
    return f"${v:,.2f}" if v >= 1 else f"${v:.6g}"


def _pct(v) -> str:
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _age(created_ms: Optional[float]) -> str:
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
    return max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))


async def build_card(engine: "Engine", mint: str) -> tuple[str, list]:
    async def safe(coro):
        try:
            return await coro
        except Exception as e:
            log.debug("card lookup failed: %s", e)
            return None

    pair, curve = await asyncio.gather(safe(_dexscreener(engine, mint)),
                                       safe(fetch_curve(engine.rpc, mint)))
    base = (pair or {}).get("baseToken") or {}
    name, symbol = base.get("name") or "", base.get("symbol") or mint[:6]
    route = "pump" if (curve and not curve.complete) or mint.endswith("pump") else "jupiter"
    cand = Candidate(chain="solana", mint=mint, source="manual", symbol=symbol, name=name,
                     creator=curve.creator if curve else None, route=route,
                     liquidity_usd=((pair or {}).get("liquidity") or {}).get("usd"),
                     fdv_usd=(pair or {}).get("marketCap") or (pair or {}).get("fdv"))
    report = await safe(engine.safety.evaluate(cand))

    dev_pct = None
    if curve and curve.creator:
        held = await safe(engine.rpc.get_token_balance(curve.creator, mint))
        if held is not None:
            dev_pct = held / PUMP_TOTAL_SUPPLY * 100

    lines = [f"🪙 <b>{esc(name or symbol)}</b> (${esc(symbol)})", f"<code>{mint}</code>", ""]
    if pair:
        vol = pair.get("volume") or {}
        chg = pair.get("priceChange") or {}
        tx = (pair.get("txns") or {}).get("h1") or {}
        lines += [
            f"💲 Price {_usd(float(pair['priceUsd'])) if pair.get('priceUsd') else 'n/a'} · "
            f"MC {_usd(pair.get('marketCap') or pair.get('fdv'))}",
            f"💧 Liquidity {_usd((pair.get('liquidity') or {}).get('usd'))} · "
            f"Vol 1h {_usd(vol.get('h1'))} · 24h {_usd(vol.get('h24'))}",
            f"📈 5m {_pct(chg.get('m5'))} · 1h {_pct(chg.get('h1'))} · 24h {_pct(chg.get('h24'))}",
            f"🔁 1h trades: {tx.get('buys', 0)} buys / {tx.get('sells', 0)} sells · "
            f"age {_age(pair.get('pairCreatedAt'))} · {esc(pair.get('dexId') or '?')}",
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
