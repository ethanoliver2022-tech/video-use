"""Momentum scanner: Solana tokens pumping right now, from free public data.

Every few seconds (only while switched on): GeckoTerminal's trending pools, sorted by the
last 5 minutes, give the shortlist; DexScreener then gives each one's live 5-minute numbers
(price change, volume, buys vs sells). A token is a signal when, over the last 5 minutes,
its price is up enough, real volume is behind it, buyers clearly outnumber sellers, and
there's enough liquidity to get out again.

Free data runs ~10-30s behind the chain: this catches pumps in motion, not their first
second. Both APIs are untrusted input: every field is checked, nothing is assumed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx
from solders.pubkey import Pubkey

from ..config import DiscoveryConfig
from ..models import num
from .multichain import QUOTE_TOKENS, _get, _token_id

log = logging.getLogger(__name__)

MIN_POLL_SECONDS = 15   # GeckoTerminal's free API allows ~30 calls a minute, shared with others
PUMP_DEXES = {"pump-fun", "pumpfun", "pump_fun", "pumpswap", "pump-swap", "pump_swap"}
_QUOTES = {q.lower() for q in QUOTE_TOKENS}


@dataclass
class MomentumSignal:
    mint: str
    symbol: str
    name: str
    change_5m: float           # %
    volume_5m: float           # USD
    buys_5m: int
    sells_5m: int
    buyers_5m: Optional[int]   # unique buyers, when GeckoTerminal reports it
    liquidity_usd: Optional[float]
    market_cap_usd: Optional[float]
    created_at: Optional[float]  # unix seconds
    dex: str
    url: str

    @property
    def pump_route(self) -> bool:
        """Tradeable through PumpPortal (pump.fun curve or PumpSwap)."""
        return self.dex.lower() in PUMP_DEXES or self.mint.endswith("pump")


def _is_address(v: str) -> bool:
    try:
        Pubkey.from_string(v)
    except (ValueError, TypeError):
        return False
    return 32 <= len(v) <= 44


def _int(v) -> Optional[int]:
    f = num(v, allow_zero=True)
    return int(f) if f is not None else None


def _change(v) -> Optional[float]:
    """A % change (may be negative), or None."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def parse_trending(payload) -> list[dict]:
    """GeckoTerminal trending pools -> [{mint, name, dex, buyers_5m, change_5m, ...}]."""
    out, seen = [], set()
    data = payload.get("data") if isinstance(payload, dict) else None
    for pool in data if isinstance(data, list) else []:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}
        mint = _token_id(pool, "base_token")
        if mint.lower() in _QUOTES:
            mint = _token_id(pool, "quote_token")  # listed as SOL/NEW
        if not mint or mint.lower() in _QUOTES or mint in seen or not _is_address(mint):
            continue  # (it ends up in buttons and buy commands: a real Solana address only)
        seen.add(mint)
        dex = _get(pool, "relationships", "dex", "data", "id")
        tx5 = _get(attrs, "transactions", "m5")
        name = attrs.get("name") if isinstance(attrs.get("name"), str) else ""
        out.append({
            "mint": mint, "name": name, "dex": dex if isinstance(dex, str) else "",
            "buyers_5m": _int(tx5.get("buyers")) if isinstance(tx5, dict) else None,
            "change_5m": _change(_get(attrs, "price_change_percentage", "m5")),
            "volume_5m": num(_get(attrs, "volume_usd", "m5"), allow_zero=True),
            "buys_5m": _int(tx5.get("buys")) if isinstance(tx5, dict) else None,
            "sells_5m": _int(tx5.get("sells")) if isinstance(tx5, dict) else None,
            "liquidity_usd": num(attrs.get("reserve_in_usd"), allow_zero=True),
            "market_cap_usd": num(attrs.get("market_cap_usd")) or num(attrs.get("fdv_usd")),
        })
    return out


def best_pairs(pairs) -> dict[str, dict]:
    """DexScreener /tokens/v1 pairs -> the most liquid pair per base token."""
    best: dict[str, dict] = {}
    for pair in pairs if isinstance(pairs, list) else []:
        if not isinstance(pair, dict):
            continue
        addr = _get(pair, "baseToken", "address")
        if not isinstance(addr, str) or not addr:
            continue
        liq = num(_get(pair, "liquidity", "usd"), allow_zero=True) or 0.0
        prev = num(_get(best.get(addr), "liquidity", "usd"), allow_zero=True) if addr in best else -1.0
        if liq >= (prev if prev is not None else 0.0):
            best[addr] = pair
    return best


def build_signal(t: dict, pair: Optional[dict]) -> MomentumSignal:
    """Merge one trending pool with its DexScreener pair (fresher 5-minute numbers win)."""
    pair = pair or {}
    tx5 = _get(pair, "txns", "m5")
    tx5 = tx5 if isinstance(tx5, dict) else {}
    change = _change(_get(pair, "priceChange", "m5"))
    volume = num(_get(pair, "volume", "m5"), allow_zero=True)
    buys, sells = _int(tx5.get("buys")), _int(tx5.get("sells"))
    sym = _get(pair, "baseToken", "symbol")
    name = _get(pair, "baseToken", "name")
    created = num(pair.get("pairCreatedAt"))
    dex = pair.get("dexId") if isinstance(pair.get("dexId"), str) else ""
    gname = t.get("name") or ""
    return MomentumSignal(
        mint=t["mint"],
        symbol=sym if isinstance(sym, str) and sym else (gname.split("/")[0].strip() or t["mint"][:6]),
        name=name if isinstance(name, str) else gname,
        change_5m=change if change is not None else (t.get("change_5m") or 0.0),
        volume_5m=volume if volume is not None else (t.get("volume_5m") or 0.0),
        buys_5m=buys if buys is not None else (t.get("buys_5m") or 0),
        sells_5m=sells if sells is not None else (t.get("sells_5m") or 0),
        buyers_5m=t.get("buyers_5m"),
        liquidity_usd=num(_get(pair, "liquidity", "usd"), allow_zero=True) or t.get("liquidity_usd"),
        market_cap_usd=num(pair.get("marketCap")) or num(pair.get("fdv")) or t.get("market_cap_usd"),
        created_at=created / 1000 if created else None,
        dex=dex or t.get("dex") or "",
        url=f"https://dexscreener.com/solana/{t['mint']}",  # built here, never taken from the API
    )


def why_not(s: MomentumSignal, d: DiscoveryConfig) -> Optional[str]:
    """None if `s` is a momentum signal under these settings, else the first reason it isn't."""
    if s.change_5m < d.momentum_min_change_5m_pct:
        return f"up {s.change_5m:.0f}% in 5m"
    if s.volume_5m < d.momentum_min_volume_5m_usd:
        return f"5m volume ${s.volume_5m:,.0f}"
    # unique buyers when GeckoTerminal reports them; otherwise buy transactions (a looser
    # stand-in: one wallet can buy several times)
    buyers = s.buyers_5m if s.buyers_5m is not None else s.buys_5m
    if buyers < d.momentum_min_buyers_5m:
        return f"{buyers} buyers in 5m"
    if s.buys_5m < d.momentum_min_buy_ratio * max(1, s.sells_5m):
        return f"{s.buys_5m} buys vs {s.sells_5m} sells"
    if (s.liquidity_usd or 0.0) < d.momentum_min_liquidity_usd:
        return f"liquidity ${s.liquidity_usd or 0:,.0f}"
    return None


class MomentumScanner:
    def __init__(self, discovery: DiscoveryConfig, gecko_api: str, dex_api: str,
                 http: httpx.AsyncClient, on_signal: Callable[[MomentumSignal], Awaitable[None]]):
        self.d, self.gecko_api, self.dex_api = discovery, gecko_api, dex_api
        self.http, self.on_signal = http, on_signal

    async def run(self) -> None:
        while True:
            if self.d.momentum_enabled:  # read every round: switched on/off live from Telegram
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("momentum scan failed: %s", type(e).__name__
                                + (f" {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else ""))
            await asyncio.sleep(max(MIN_POLL_SECONDS, self.d.momentum_poll_seconds))

    async def tick(self) -> list[MomentumSignal]:
        resp = await self.http.get(f"{self.gecko_api}/networks/solana/trending_pools",
                                   params={"include": "base_token,dex", "duration": "5m", "page": 1},
                                   headers={"accept": "application/json"}, timeout=10)
        if resp.status_code == 429:
            log.info("geckoterminal rate limited; momentum scan skipped this round")
            return []
        resp.raise_for_status()
        trending = parse_trending(resp.json())[:30]  # DexScreener takes up to 30 tokens a call
        if not trending:
            return []
        pairs: dict[str, dict] = {}
        try:
            r = await self.http.get(f"{self.dex_api}/tokens/v1/solana/"
                                    + ",".join(t["mint"] for t in trending), timeout=10)
            if r.status_code == 200:
                pairs = best_pairs(r.json())
        except (httpx.HTTPError, ValueError) as e:  # GeckoTerminal's own numbers still work
            log.debug("dexscreener enrich failed: %s", e)
        hits = []
        for t in trending:
            sig = build_signal(t, pairs.get(t["mint"]))
            if why_not(sig, self.d) is None:
                hits.append(sig)
        for sig in hits:
            await self.on_signal(sig)
        return hits


def age_text(created_at: Optional[float]) -> str:
    if not created_at:
        return "?"
    s = max(0.0, time.time() - created_at)
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.0f}d"
