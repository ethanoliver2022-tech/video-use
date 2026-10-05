"""Launch intelligence: socials metadata and early order-flow / bundle detection."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .config import EntryConfig

log = logging.getLogger(__name__)

SOCIAL_KEYS = ("twitter", "telegram", "website")


def normalize_social(url: str) -> str:
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.replace("x.com/", "twitter.com/").replace("telegram.me/", "t.me/")
    u = u.split("?")[0].split("#")[0].rstrip("/")
    return u


def extract_socials(meta: dict) -> list[str]:
    out = []
    ext = meta.get("extensions")  # token metadata is written by the token's creator: trust nothing
    ext = ext if isinstance(ext, dict) else {}
    for key in SOCIAL_KEYS:
        val = meta.get(key) or ext.get(key)
        if isinstance(val, str) and "." in val:
            out.append(normalize_social(val))
    return out


METADATA_MAX_BYTES = 64 * 1024  # real token metadata is a few hundred bytes
METADATA_DEADLINE = 3.0         # seconds, total (a trickling server can't stall a worker)


def _safe_url(uri: str) -> bool:
    """The metadata URI is chosen by whoever launched the token: only plain web URLs, never
    this machine or its private network."""
    import ipaddress
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or host == "localhost" \
            or host.endswith(".localhost") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname
    return ip.is_global


def _peer_is_public(resp) -> bool:
    """The address actually connected to. Unknown (e.g. a proxy or test transport): allowed,
    since the hostname was already checked."""
    import ipaddress
    try:
        addr = resp.extensions["network_stream"].get_extra_info("server_addr")
        return ipaddress.ip_address(addr[0]).is_global
    except Exception:
        return True


async def _resolves_public(uri: str) -> bool:
    """A hostname must not lead to this machine or its private network either."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    host = urlsplit(uri).hostname or ""
    try:
        ipaddress.ip_address(host)
        return True  # a literal address: _safe_url already judged it
    except ValueError:
        pass
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM), 2)
    except Exception:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def fetch_metadata(http: httpx.AsyncClient, uri: str, gateway: str = "") -> Optional[dict]:
    if not uri:
        return None
    via_gateway = bool(gateway and "/ipfs/" in uri)
    if via_gateway:  # the user's own gateway (may well be local): trusted
        uri = gateway.rstrip("/") + "/ipfs/" + uri.split("/ipfs/", 1)[1]
    if not via_gateway and not (_safe_url(uri) and await _resolves_public(uri)):
        log.debug("metadata uri refused: %s", uri[:100])
        return None

    async def read() -> Optional[dict]:
        async with http.stream("GET", uri, timeout=2) as resp:
            if resp.status_code != 200:
                return None
            if not via_gateway and not _peer_is_public(resp):
                return None  # DNS changed between our check and the connection (rebinding)
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > METADATA_MAX_BYTES:
                    return None
        data = json.loads(bytes(body))
        return data if isinstance(data, dict) else None
    try:
        return await asyncio.wait_for(read(), METADATA_DEADLINE)
    except Exception as e:
        log.debug("metadata fetch failed for %s: %s", uri[:100], e)
    return None


@dataclass
class EarlyFlow:
    """Trades seen during the confirmation window after a launch."""

    creator: Optional[str]
    buys: list[tuple[str, float]] = field(default_factory=list)   # (wallet, sol)
    sells: list[tuple[str, float]] = field(default_factory=list)
    market_cap_sol: float = 0.0
    dev_sold: bool = False

    def add(self, msg: dict) -> None:
        from .models import num
        trader = msg.get("traderPublicKey", "")
        sol = num(msg.get("solAmount"), allow_zero=True) or 0.0
        mcap = num(msg.get("marketCapSol"))
        if mcap:
            self.market_cap_sol = mcap
        if msg.get("txType") == "buy":
            self.buys.append((trader, sol))
        elif msg.get("txType") == "sell":
            self.sells.append((trader, sol))
            if self.creator and trader == self.creator:
                self.dev_sold = True

    def evaluate(self, cfg: EntryConfig) -> list[str]:
        problems = []
        if self.dev_sold:
            problems.append("dev sold during confirmation window")
        non_dev = [(w, s) for w, s in self.buys if w != self.creator]
        buyers = {w for w, _ in non_dev}
        if len(buyers) < cfg.min_unique_buyers:
            problems.append(f"only {len(buyers)} unique buyers (< {cfg.min_unique_buyers})")
        vol = sum(s for _, s in non_dev)
        if vol > 0:
            per_wallet = Counter()
            for w, s in non_dev:
                per_wallet[w] += s
            top_w, top_sol = per_wallet.most_common(1)[0]
            share = top_sol / vol * 100
            if share > cfg.max_single_buyer_pct:
                problems.append(f"one wallet is {share:.0f}% of early buys ({top_w[:6]}…)")
        sizes = Counter(round(s, 4) for _, s in non_dev if s > 0)
        if sizes:
            size, n = sizes.most_common(1)[0]
            wallets = {w for w, s in non_dev if round(s, 4) == size}
            if n >= cfg.max_identical_buys and len(wallets) >= cfg.max_identical_buys:
                problems.append(f"{len(wallets)} wallets bought exactly {size} SOL (bundle)")
        net = vol - sum(s for _, s in self.sells)
        if net <= cfg.min_net_flow_sol:  # must exceed it (config docs)
            problems.append(f"net flow {net:+.2f} SOL")
        if cfg.max_market_cap_sol and self.market_cap_sol > cfg.max_market_cap_sol:
            problems.append(f"market cap already {self.market_cap_sol:.0f} SOL")
        return problems
