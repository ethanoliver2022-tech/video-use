"""Pre-buy rug filters.

None of this makes a token safe. It removes the cheapest, most mechanical rug
vectors (mint more supply, freeze your tokens, claw them back, one wallet
holding the float) so the exit logic only has to deal with market risk and
coordinated dumps.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Optional

import httpx
from solders.pubkey import Pubkey

from .config import FilterConfig
from .intel import extract_socials, fetch_metadata
from .models import PUMP_TOTAL_SUPPLY, SOL_MINT, Candidate, SafetyReport
from .solana_rpc import SolanaRpc

if TYPE_CHECKING:
    from .execution.executors import Jupiter
    from .store import Store

log = logging.getLogger(__name__)

# Token-2022 extensions that let the issuer take or lock your tokens.
DANGEROUS_EXTENSIONS = {
    "permanentDelegate": "permanent delegate can move holders' tokens",
    "transferHook": "transfer hook can block sells",
    "nonTransferable": "token is non-transferable",
    "pausableConfig": "issuer can pause transfers",
    "defaultAccountState": "accounts may start frozen",
}
MAX_TRANSFER_FEE_BPS = 100


def static_checks(c: Candidate, f: FilterConfig) -> SafetyReport:
    """Cheap checks that need no network calls."""
    r = SafetyReport(passed=True)
    # whole words, ignoring punctuation ("RUG!", "scam-coin"); phrases work too ("rug pull").
    # Not substrings: "test" must not block "Contest".
    text = " " + " ".join(re.findall(r"[a-z0-9]+", f"{c.name} {c.symbol}".lower())) + " "
    for word in f.name_blocklist:
        phrase = " ".join(re.findall(r"[a-z0-9]+", str(word).lower()))
        if phrase and f" {phrase} " in text:
            r.fail(f"name contains blocked word '{word}'")
    if c.creator and c.creator in f.creator_blocklist:
        r.fail("creator is blocklisted")

    if c.source == "pumpfun":
        if c.creator_initial_buy_tokens is not None:
            pct = c.creator_initial_buy_tokens / PUMP_TOTAL_SUPPLY * 100
            r.notes.append(f"dev initial buy {pct:.2f}%")
            if pct > f.max_creator_initial_buy_pct:
                r.fail(f"dev bought {pct:.1f}% of supply at launch")
    else:
        if c.liquidity_usd is not None and c.liquidity_usd < f.min_liquidity_usd:
            r.fail(f"liquidity ${c.liquidity_usd:,.0f} < ${f.min_liquidity_usd:,.0f}")
        if c.fdv_usd is not None and c.fdv_usd > f.max_fdv_usd:
            r.fail(f"FDV ${c.fdv_usd:,.0f} already above ${f.max_fdv_usd:,.0f}")
    return r


def check_mint(info: dict, f: FilterConfig, r: SafetyReport) -> None:
    if f.require_mint_revoked and info.get("mintAuthority"):
        r.fail("mint authority not revoked (supply can be inflated)")
    if f.require_freeze_revoked and info.get("freezeAuthority"):
        r.fail("freeze authority not revoked (your tokens can be frozen)")
    for ext in info.get("extensions") or []:
        name = ext.get("extension")
        if name in DANGEROUS_EXTENSIONS:
            r.fail(f"token-2022 {name}: {DANGEROUS_EXTENSIONS[name]}")
        if name == "transferFeeConfig":
            state = ext.get("state") or {}
            bps = max(
                int((state.get("newerTransferFee") or {}).get("transferFeeBasisPoints", 0)),
                int((state.get("olderTransferFee") or {}).get("transferFeeBasisPoints", 0)),
            )
            if bps > MAX_TRANSFER_FEE_BPS:
                r.fail(f"transfer fee {bps / 100:.1f}%")


def concentration_pct(largest: list[dict], owners: list[Optional[dict]], supply_ui: float) -> float:
    """Share of supply held by the top 10 *wallets*, skipping program-owned (PDA) accounts.

    Bonding curves and AMM vaults are owned by PDAs (off the ed25519 curve), so
    counting them would flag every token. Real wallets are on-curve.
    """
    if supply_ui <= 0:
        return 0.0
    held = 0.0
    counted = 0
    for acct, parsed in zip(largest, owners):
        owner = None
        try:
            owner = parsed["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            pass
        if owner and not Pubkey.from_string(owner).is_on_curve():
            continue
        held += float(acct.get("uiAmount") or 0)
        counted += 1
        if counted == 10:
            break
    return held / supply_ui * 100


class SafetyChecker:
    def __init__(self, cfg: FilterConfig, rpc: SolanaRpc, http: httpx.AsyncClient, rugcheck_api: str,
                 store: Optional["Store"] = None, jupiter: Optional["Jupiter"] = None,
                 ipfs_gateway: str = "", probe_sol: float = 0.05):
        self.cfg, self.rpc, self.http, self.rugcheck_api = cfg, rpc, http, rugcheck_api
        self.store, self.jupiter, self.ipfs_gateway = store, jupiter, ipfs_gateway
        self.probe_sol = probe_sol

    async def evaluate(self, c: Candidate) -> SafetyReport:
        r = static_checks(c, self.cfg)
        if c.chain != "solana" or not r.passed:
            return r
        self._reputation(c, r)
        if not r.passed:
            return r

        if c.uri:
            await self._socials(c, r)
            if not r.passed:
                return r

        # Brand-new pump.fun coins: the program itself revokes mint/freeze and the
        # curve holds ~all supply, so on-chain + rugcheck lookups only add latency.
        if c.source == "pumpfun":
            r.notes.append("pump.fun program guarantees mint/freeze revoked")
            return r

        try:
            info = await self.rpc.get_mint_info(c.mint)
        except Exception as e:
            r.fail(f"could not read mint: {e}")
            return r
        if not info:
            r.fail("mint account not found")
            return r
        check_mint(info, self.cfg, r)
        if not r.passed:
            return r

        try:
            decimals = int(info.get("decimals", 0))
            supply_ui = int(info.get("supply", 0)) / (10 ** decimals)
            largest = await self.rpc.get_largest_accounts(c.mint)
            owners = await self.rpc.get_multiple_accounts_parsed([a["address"] for a in largest])
            pct = concentration_pct(largest, owners, supply_ui)
            r.notes.append(f"top10 wallets hold {pct:.1f}%")
            if pct > self.cfg.max_top10_holder_pct:
                r.fail(f"top 10 wallets hold {pct:.1f}% of supply")
        except Exception as e:
            r.notes.append(f"holder check skipped: {e}")

        if r.passed and self.cfg.use_rugcheck:
            await self._rugcheck(c, r)
        if r.passed and self.cfg.honeypot_check and c.route == "jupiter" and self.jupiter:
            await self._honeypot(c, r)
        return r

    def _reputation(self, c: Candidate, r: SafetyReport) -> None:
        if not self.store or not c.creator:
            return
        reason = self.store.is_blocked(c.creator)
        if reason:
            r.fail(f"creator blocklisted: {reason}")
            return
        n = self.store.launches_since(c.creator, time.time() - 86400, exclude_mint=c.mint)
        if n:
            r.notes.append(f"creator launched {n} other coin(s) in 24h")
        if self.cfg.max_creator_launches_24h and n >= self.cfg.max_creator_launches_24h:
            r.fail(f"serial launcher: {n} other launches in 24h")

    async def _socials(self, c: Candidate, r: SafetyReport) -> None:
        if not (self.cfg.min_socials or self.cfg.reject_reused_socials):
            return
        meta = await fetch_metadata(self.http, c.uri or "", self.ipfs_gateway)
        if meta is None:
            if self.cfg.min_socials:
                r.fail("metadata unavailable")
            return
        socials = extract_socials(meta)
        r.notes.append(f"{len(socials)} socials")
        if len(socials) < self.cfg.min_socials:
            r.fail(f"{len(socials)} socials (< {self.cfg.min_socials})")
        if self.store and socials:
            reused = self.store.record_socials(c.mint, socials)
            if reused and self.cfg.reject_reused_socials:
                r.fail("socials reused from an earlier launch: " + ", ".join(reused))

    async def _honeypot(self, c: Candidate, r: SafetyReport) -> None:
        """Quote SOL -> token -> SOL. No sell route, or a huge round-trip loss, means
        a honeypot, a heavy tax or liquidity too thin to get out."""
        try:
            buy_q = await self.jupiter.quote(SOL_MINT, c.mint, self.probe_sol, 50)
            tokens = await self.jupiter.out_ui(buy_q)
            sell_q = await self.jupiter.quote(c.mint, SOL_MINT, tokens, 50)
            back = await self.jupiter.out_ui(sell_q)
        except Exception as e:
            r.fail(f"no sell route ({e})")
            return
        loss = (1 - back / self.probe_sol) * 100
        r.notes.append(f"round-trip loss {loss:.1f}%")
        if loss > self.cfg.max_roundtrip_loss_pct:
            r.fail(f"round-trip loses {loss:.0f}% (tax / honeypot / thin liquidity)")

    async def _rugcheck(self, c: Candidate, r: SafetyReport) -> None:
        try:
            resp = await self.http.get(f"{self.rugcheck_api}/tokens/{c.mint}/report/summary", timeout=8)
            if resp.status_code != 200:
                r.notes.append(f"rugcheck unavailable ({resp.status_code})")
                return
            data = resp.json()
        except Exception as e:
            r.notes.append(f"rugcheck error: {e}")
            return
        if not isinstance(data, dict):
            r.notes.append("rugcheck: unexpected response")
            return
        risks = data.get("risks") if isinstance(data.get("risks"), list) else []
        dangers = [str(x.get("name", "?")) for x in risks
                   if isinstance(x, dict) and x.get("level") == "danger"]
        r.notes.append(f"rugcheck score {data.get('score_normalised', data.get('score'))}")
        if dangers and self.cfg.rugcheck_reject_danger:
            r.fail("rugcheck danger: " + ", ".join(dangers))
