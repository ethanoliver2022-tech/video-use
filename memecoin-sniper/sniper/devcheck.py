"""Dev wallet background checks, read from the chain before buying a launch.

* Wallet age: when the deployer's first transaction happened. A wallet made this morning
  that buys 2% of its own coin is how most dev-sold rugs look.
* Funding: who sent the deployer its first SOL. Rejected when that funder is itself brand
  new (a throwaway "parent" hop, the cheap way to hide where money came from), or when it's
  on your funder blocklist (mixers, known rug funders) or the bot's rugger blocklist.
* Curve sells: whether the deployer sold into any pump.fun bonding curve in the last hours,
  even on coins it didn't launch. Serial dumpers rotate deployers but keep selling.

All lookups are plain RPC reads and are cached per wallet for a few minutes."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .config import FilterConfig

if TYPE_CHECKING:
    from .solana_rpc import SolanaRpc
    from .store import Store

log = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
PAGE = 1000           # getSignaturesForAddress maximum: a wallet with more is not new
CACHE_SECONDS = 600
PARALLEL_TX = 5       # transactions fetched at once when scanning a wallet's history


@dataclass
class DevProfile:
    wallet: str
    first_seen: Optional[float] = None    # unix time of its oldest transaction; None = older
    funder: Optional[str] = None          # who sent its first SOL
    funder_first_seen: Optional[float] = None
    funder_checked: bool = False
    curve_sells: list[str] = field(default_factory=list)  # signatures of recent curve sells
    error: str = ""


def _program_sells(logs: list[str]) -> int:
    """pump.fun 'Sell' instructions in a transaction's logs (CPI depth tracked, so another
    program merely logging the word can't fake one)."""
    stack: list[str] = []
    sells = 0
    for line in logs or []:
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "Program" and parts[2] == "invoke":
            stack.append(parts[1])
        elif len(parts) >= 3 and parts[0] == "Program" and parts[2] in ("success", "failed:"):
            if stack:
                stack.pop()
        elif line == "Program log: Instruction: Sell" and stack and stack[-1] == PUMP_PROGRAM:
            sells += 1
    return sells


def funder_of(tx: dict, wallet: str) -> Optional[str]:
    """The account that sent `wallet` SOL in this (parsed) transaction."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    ixs = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    for ix in ixs:
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or ix.get("program") != "system":
            continue
        info = parsed.get("info") or {}
        if parsed.get("type") in ("transfer", "transferWithSeed"):
            to, frm = info.get("destination"), info.get("source")
        elif parsed.get("type") in ("createAccount", "createAccountWithSeed"):
            to, frm = info.get("newAccount"), info.get("source")
        else:
            continue
        if to == wallet and frm and frm != wallet:
            return frm
    return None


class DevChecker:
    def __init__(self, cfg: FilterConfig, rpc: "SolanaRpc", store: Optional["Store"] = None):
        self.cfg, self.rpc, self.store = cfg, rpc, store
        self.cache: dict[str, tuple[float, DevProfile]] = {}

    @property
    def enabled(self) -> bool:
        c = self.cfg
        return bool(c.dev_min_wallet_age_min or c.dev_min_funder_age_min
                    or c.dev_funder_blocklist or c.dev_curve_sell_hours)

    def known_funder(self, wallet: str) -> Optional[str]:
        hit = self.cache.get(wallet)
        return hit[1].funder if hit else None

    async def _sigs(self, wallet: str) -> list[dict]:
        return await self.rpc.call("getSignaturesForAddress",
                                   [wallet, {"limit": PAGE, "commitment": "confirmed"}]) or []

    async def _tx(self, sig: str) -> Optional[dict]:
        return await self.rpc.call("getTransaction", [sig, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed"}])

    @staticmethod
    def _oldest(sigs: list[dict]) -> Optional[float]:
        if len(sigs) >= PAGE:
            return None             # a full page: far too busy to be a fresh wallet
        times = [s.get("blockTime") for s in sigs if isinstance(s.get("blockTime"), (int, float))]
        return float(min(times)) if times else None

    async def check(self, wallet: str, now: Optional[float] = None) -> list[str]:
        """Reasons to skip this deployer (empty = fine)."""
        if not wallet or not self.enabled:
            return []
        now = now or time.time()
        hit = self.cache.get(wallet)
        if hit and now - hit[0] < CACHE_SECONDS:
            prof = hit[1]
        else:
            prof = await self._profile(wallet, now)
            if not prof.error:
                self.cache[wallet] = (now, prof)
                if len(self.cache) > 5000:
                    for k in list(self.cache)[:1000]:
                        self.cache.pop(k, None)
        return self.verdict(prof, now)

    def verdict(self, p: DevProfile, now: float) -> list[str]:
        c = self.cfg
        if p.error:
            if c.dev_check_on_error == "skip":
                return [f"dev wallet check failed ({p.error})"]
            return []
        out = []
        if c.dev_min_wallet_age_min and p.first_seen is not None:
            age = (now - p.first_seen) / 60
            if age < c.dev_min_wallet_age_min:
                out.append(f"dev wallet is only {_mins(age)} old")
        if p.funder:
            blocked = (p.funder in set(c.dev_funder_blocklist)
                       or (self.store and self.store.is_blocked(p.funder)))
            if blocked:
                out.append("dev funded by a blocklisted wallet")
            elif (c.dev_min_funder_age_min and p.funder_first_seen is not None
                  and (now - p.funder_first_seen) / 60 < c.dev_min_funder_age_min):
                out.append(f"dev funded by a fresh wallet "
                           f"({_mins((now - p.funder_first_seen) / 60)} old)")
        if c.dev_curve_sell_hours and len(p.curve_sells) > c.dev_max_curve_sells:
            out.append(f"dev sold into pump.fun curves {len(p.curve_sells)}x in the last "
                       f"{c.dev_curve_sell_hours:g}h")
        return out

    async def _profile(self, wallet: str, now: float) -> DevProfile:
        c = self.cfg
        p = DevProfile(wallet)
        try:
            sigs = await self._sigs(wallet)
            p.first_seen = self._oldest(sigs)
            if c.dev_min_wallet_age_min and p.first_seen is not None \
                    and (now - p.first_seen) / 60 < c.dev_min_wallet_age_min:
                return p    # already rejected: no need to spend more lookups on it
            jobs = []
            if c.dev_curve_sell_hours:
                jobs.append(self._curve_sells(p, sigs, now))
            if (c.dev_min_funder_age_min or c.dev_funder_blocklist
                    or c.auto_blocklist_ruggers) and p.first_seen is not None:
                jobs.append(self._funding(p, sigs))
            await asyncio.gather(*jobs)
        except Exception as e:
            p.error = type(e).__name__ if not str(e) else str(e)[:80]
            log.debug("dev check %s failed: %s", wallet, e)
        return p

    async def _funding(self, p: DevProfile, sigs: list[dict]) -> None:
        dated = [s for s in sigs if isinstance(s.get("blockTime"), (int, float))]
        if not dated:
            return
        first = min(dated, key=lambda s: s["blockTime"])
        tx = await self._tx(first["signature"])
        p.funder = funder_of(tx or {}, p.wallet)
        if p.funder and self.cfg.dev_min_funder_age_min:
            p.funder_first_seen = self._oldest(await self._sigs(p.funder))
            p.funder_checked = True

    async def _curve_sells(self, p: DevProfile, sigs: list[dict], now: float) -> None:
        since = now - self.cfg.dev_curve_sell_hours * 3600
        recent = [s["signature"] for s in sigs
                  if not s.get("err") and isinstance(s.get("blockTime"), (int, float))
                  and s["blockTime"] >= since][:self.cfg.dev_history_max_txs]
        sem = asyncio.Semaphore(PARALLEL_TX)

        async def one(sig: str) -> None:
            async with sem:
                tx = await self._tx(sig)
            if tx and _program_sells((tx.get("meta") or {}).get("logMessages") or []):
                p.curve_sells.append(sig)
        await asyncio.gather(*(one(s) for s in recent))


def _mins(m: float) -> str:
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"
