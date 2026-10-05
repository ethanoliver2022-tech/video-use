"""Polling scanners for fresh pools across chains (GeckoTerminal, DexScreener)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

import httpx

from ..models import Candidate

log = logging.getLogger(__name__)

CandidateHandler = Callable[[Candidate], Awaitable[None]]

# GeckoTerminal network id -> our chain id
GECKO_CHAINS = {"solana": "solana", "eth": "eth", "base": "base", "bsc": "bsc",
                "arbitrum": "arbitrum", "polygon_pos": "polygon", "avax": "avax"}
DEX_CHAINS = {"solana": "solana", "ethereum": "eth", "base": "base", "bsc": "bsc",
              "arbitrum": "arbitrum", "polygon": "polygon", "avalanche": "avax"}


def _float(v) -> Optional[float]:
    from ..models import num
    return num(v, allow_zero=True)


# Quote assets that are never the "new token" in a pool.
QUOTE_TOKENS = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (Solana)
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (Solana)
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",    # WETH (Ethereum)
    "0x4200000000000000000000000000000000000006",    # WETH (Base)
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",    # WBNB
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",    # USDC (Ethereum)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",    # USDC (Base)
    "0xdac17f958d2ee523a2206206994597c13d831ec7",    # USDT (Ethereum)
    "0x55d398326f99059ff775485246999027b3197955",    # USDT (BSC)
}


def _get(d, *path):
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _token_id(pool: dict, rel: str) -> str:
    tid = _get(pool, "relationships", rel, "data", "id")
    return tid.split("_", 1)[1] if isinstance(tid, str) and "_" in tid else ""


def parse_gecko_pools(network: str, payload: dict) -> list[Candidate]:
    out = []
    data = payload.get("data") if isinstance(payload, dict) else None
    for pool in data if isinstance(data, list) else []:
        try:
            cand = _parse_gecko_pool(network, pool)
        except (TypeError, ValueError, AttributeError) as e:
            log.debug("skipping malformed pool: %s", e)
            continue
        if cand:
            out.append(cand)
    return out


def _parse_gecko_pool(network: str, pool) -> Optional[Candidate]:
    if not isinstance(pool, dict):
        return None
    attrs = pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}
    mint = _token_id(pool, "base_token")
    if mint.lower() in {q.lower() for q in QUOTE_TOKENS}:
        mint = _token_id(pool, "quote_token")  # pool listed as SOL/NEW instead of NEW/SOL
    if not mint or mint.lower() in {q.lower() for q in QUOTE_TOKENS}:
        return None
    created = attrs.get("pool_created_at")
    ts = None
    if isinstance(created, str) and created:
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
    name = attrs.get("name") if isinstance(attrs.get("name"), str) else ""
    cand = Candidate(
        chain=GECKO_CHAINS.get(network, network),
        mint=mint,
        source="geckoterminal",
        symbol=name.split("/")[0].strip(),
        name=name,
        pool=attrs.get("address") if isinstance(attrs.get("address"), str) else None,
        liquidity_usd=_float(attrs.get("reserve_in_usd")),
        fdv_usd=_float(attrs.get("fdv_usd")),
        url=f"https://www.geckoterminal.com/{network}/pools/{attrs.get('address')}",
    )
    if ts:
        cand.created_at = ts
    return cand


def _describe(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def _poll(name: str, interval: int, fn: Callable[[], Awaitable[None]],
                active: Callable[[], bool] = lambda: True) -> None:
    while True:
        if active():  # no point burning API rate limits while the bot is paused
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s poll failed: %s", name, _describe(e))
        await asyncio.sleep(interval)


class GeckoTerminalScanner:
    def __init__(self, api: str, networks: list[str], interval: int, on_candidate: CandidateHandler,
                 http: httpx.AsyncClient, active: Callable[[], bool] = lambda: True):
        self.api, self.networks, self.interval = api, networks, interval
        self.on_candidate, self.http, self.active = on_candidate, http, active

    async def run(self) -> None:
        await _poll("geckoterminal", self.interval, self._tick, self.active)

    async def _tick(self) -> None:
        for network in self.networks:
            resp = await self.http.get(
                f"{self.api}/networks/{network}/new_pools",
                params={"include": "base_token"},
                headers={"accept": "application/json"},
            )
            if resp.status_code == 429:
                log.info("geckoterminal rate limited; backing off")
                await asyncio.sleep(10)
                continue
            resp.raise_for_status()
            for cand in parse_gecko_pools(network, resp.json()):
                await self.on_candidate(cand)
            await asyncio.sleep(2.5)  # stay well under the free 30 req/min limit


class DexScreenerScanner:
    """Watches newly created DexScreener token profiles, then enriches with pair data."""

    def __init__(self, api: str, interval: int, on_candidate: CandidateHandler, http: httpx.AsyncClient,
                 active: Callable[[], bool] = lambda: True):
        self.api, self.interval, self.on_candidate, self.http = api, interval, on_candidate, http
        self.active = active
        self._seen: set[tuple[str, str]] = set()

    async def run(self) -> None:
        await _poll("dexscreener", self.interval, self._tick, self.active)

    async def _tick(self) -> None:
        if len(self._seen) > 20_000:  # keep memory flat; old profiles have aged out anyway
            self._seen.clear()
        resp = await self.http.get(f"{self.api}/token-profiles/latest/v1")
        resp.raise_for_status()
        fresh: dict[str, list[str]] = {}
        profiles = resp.json()
        for prof in profiles if isinstance(profiles, list) else []:
            if not isinstance(prof, dict):
                continue
            chain, addr = prof.get("chainId"), prof.get("tokenAddress")
            if not isinstance(chain, str) or not isinstance(addr, str) or not chain or not addr \
                    or (chain, addr) in self._seen:
                continue
            self._seen.add((chain, addr))
            fresh.setdefault(chain, []).append(addr)
        for chain, addrs in fresh.items():
            for i in range(0, len(addrs), 30):
                await self._enrich(chain, addrs[i:i + 30])

    async def _enrich(self, chain: str, addrs: list[str]) -> None:
        resp = await self.http.get(f"{self.api}/tokens/v1/{chain}/{','.join(addrs)}")
        resp.raise_for_status()
        best: dict[str, dict] = {}
        pairs = resp.json()
        for pair in pairs if isinstance(pairs, list) else []:
            if not isinstance(pair, dict):
                continue
            addr = _get(pair, "baseToken", "address")
            if not isinstance(addr, str) or not addr:
                continue
            liq = _float(_get(pair, "liquidity", "usd")) or 0.0
            prev = _float(_get(best.get(addr), "liquidity", "usd")) if addr in best else -1.0
            if liq >= (prev if prev is not None else 0.0):
                best[addr] = pair
        for addr, pair in best.items():
            sym, name = _get(pair, "baseToken", "symbol"), _get(pair, "baseToken", "name")
            url, pair_addr = pair.get("url"), pair.get("pairAddress")
            cand = Candidate(
                chain=DEX_CHAINS.get(chain, chain),
                mint=addr,
                source="dexscreener",
                symbol=sym if isinstance(sym, str) else "",
                name=name if isinstance(name, str) else "",
                pool=pair_addr if isinstance(pair_addr, str) else None,
                liquidity_usd=_float(_get(pair, "liquidity", "usd")),
                fdv_usd=_float(pair.get("fdv")),
                url=url if isinstance(url, str) else None,
            )
            created = _float(pair.get("pairCreatedAt"))
            if created:
                cand.created_at = created / 1000
            await self.on_candidate(cand)
