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
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_gecko_pools(network: str, payload: dict) -> list[Candidate]:
    out = []
    for pool in payload.get("data", []):
        attrs = pool.get("attributes", {})
        base_id = (pool.get("relationships", {}).get("base_token", {})
                   .get("data", {}).get("id", ""))
        mint = base_id.split("_", 1)[1] if "_" in base_id else ""
        if not mint:
            continue
        created = attrs.get("pool_created_at")
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
        name = attrs.get("name", "")
        cand = Candidate(
            chain=GECKO_CHAINS.get(network, network),
            mint=mint,
            source="geckoterminal",
            symbol=name.split("/")[0].strip(),
            name=name,
            pool=attrs.get("address"),
            liquidity_usd=_float(attrs.get("reserve_in_usd")),
            fdv_usd=_float(attrs.get("fdv_usd")),
            url=f"https://www.geckoterminal.com/{network}/pools/{attrs.get('address')}",
        )
        if ts:
            cand.created_at = ts
        out.append(cand)
    return out


async def _poll(name: str, interval: int, fn: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s poll failed: %s", name, e)
        await asyncio.sleep(interval)


class GeckoTerminalScanner:
    def __init__(self, api: str, networks: list[str], interval: int, on_candidate: CandidateHandler,
                 http: httpx.AsyncClient):
        self.api, self.networks, self.interval = api, networks, interval
        self.on_candidate, self.http = on_candidate, http

    async def run(self) -> None:
        await _poll("geckoterminal", self.interval, self._tick)

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

    def __init__(self, api: str, interval: int, on_candidate: CandidateHandler, http: httpx.AsyncClient):
        self.api, self.interval, self.on_candidate, self.http = api, interval, on_candidate, http
        self._seen: set[str] = set()

    async def run(self) -> None:
        await _poll("dexscreener", self.interval, self._tick)

    async def _tick(self) -> None:
        resp = await self.http.get(f"{self.api}/token-profiles/latest/v1")
        resp.raise_for_status()
        fresh: dict[str, list[str]] = {}
        for prof in resp.json():
            chain, addr = prof.get("chainId"), prof.get("tokenAddress")
            if not chain or not addr or (chain, addr) in self._seen:
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
        for pair in resp.json() or []:
            addr = pair.get("baseToken", {}).get("address")
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            if addr and liq >= ((best.get(addr) or {}).get("liquidity") or {}).get("usd", -1):
                best[addr] = pair
        for addr, pair in best.items():
            cand = Candidate(
                chain=DEX_CHAINS.get(chain, chain),
                mint=addr,
                source="dexscreener",
                symbol=pair.get("baseToken", {}).get("symbol", ""),
                name=pair.get("baseToken", {}).get("name", ""),
                pool=pair.get("pairAddress"),
                liquidity_usd=_float((pair.get("liquidity") or {}).get("usd")),
                fdv_usd=_float(pair.get("fdv")),
                url=pair.get("url"),
            )
            if pair.get("pairCreatedAt"):
                cand.created_at = pair["pairCreatedAt"] / 1000
            await self.on_candidate(cand)
