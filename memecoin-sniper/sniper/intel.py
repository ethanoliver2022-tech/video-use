"""Launch intelligence: socials metadata and early order-flow / bundle detection."""
from __future__ import annotations

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
    for key in SOCIAL_KEYS:
        val = meta.get(key) or (meta.get("extensions") or {}).get(key)
        if isinstance(val, str) and "." in val:
            out.append(normalize_social(val))
    return out


async def fetch_metadata(http: httpx.AsyncClient, uri: str, gateway: str = "") -> Optional[dict]:
    if not uri:
        return None
    if gateway and "/ipfs/" in uri:
        uri = gateway.rstrip("/") + "/ipfs/" + uri.split("/ipfs/", 1)[1]
    try:
        resp = await http.get(uri, timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        log.debug("metadata fetch failed for %s: %s", uri, e)
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
        trader = msg.get("traderPublicKey", "")
        sol = float(msg.get("solAmount") or 0)
        if msg.get("marketCapSol"):
            self.market_cap_sol = float(msg["marketCapSol"])
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
        if net < cfg.min_net_flow_sol:
            problems.append(f"net flow {net:+.2f} SOL")
        if cfg.max_market_cap_sol and self.market_cap_sol > cfg.max_market_cap_sol:
            problems.append(f"market cap already {self.market_cap_sol:.0f} SOL")
        return problems
