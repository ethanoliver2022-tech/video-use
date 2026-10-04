"""Pre-buy rug filters.

None of this makes a token safe. It removes the cheapest, most mechanical rug
vectors (mint more supply, freeze your tokens, claw them back, one wallet
holding the float) so the exit logic only has to deal with market risk and
coordinated dumps.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from solders.pubkey import Pubkey

from .config import FilterConfig
from .models import PUMP_TOTAL_SUPPLY, Candidate, SafetyReport
from .solana_rpc import SolanaRpc

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
    text = f"{c.name} {c.symbol}".lower()
    for word in f.name_blocklist:
        if word and word.lower() in text.split():
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
    def __init__(self, cfg: FilterConfig, rpc: SolanaRpc, http: httpx.AsyncClient, rugcheck_api: str):
        self.cfg, self.rpc, self.http, self.rugcheck_api = cfg, rpc, http, rugcheck_api

    async def evaluate(self, c: Candidate) -> SafetyReport:
        r = static_checks(c, self.cfg)
        if not r.passed or c.chain != "solana":
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

        if self.cfg.use_rugcheck:
            await self._rugcheck(c, r)
        return r

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
        dangers = [x.get("name", "?") for x in data.get("risks", []) if x.get("level") == "danger"]
        r.notes.append(f"rugcheck score {data.get('score_normalised', data.get('score'))}")
        if dangers and self.cfg.rugcheck_reject_danger:
            r.fail("rugcheck danger: " + ", ".join(dangers))
