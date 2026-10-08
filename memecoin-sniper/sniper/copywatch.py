"""Backup watcher for copied / tracked wallets, straight from the chain (RPC).

PumpPortal's account feed is the fast path, but when it isn't delivering (the paid feed
refused, a dropped subscription, a trade it doesn't cover) the bot would sit silent while
a copied wallet buys. This polls each wallet's newest transactions and turns every swap
into the same message shape PumpPortal sends, so the normal copy path handles it; a trade
both deliver is handled once (same signature)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Optional

from .models import PUMP_TOTAL_SUPPLY, SOL_MINT

if TYPE_CHECKING:
    from .solana_rpc import SolanaRpc

log = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
POLL_SECONDS = 2.0
MAX_AGE_SECONDS = 120   # a buy older than this when first seen is history, not a signal


def _keys(tx: dict) -> list[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k["pubkey"] if isinstance(k, dict) else k for k in keys]


def _venue(tx: dict) -> str:
    logs = " ".join((tx.get("meta") or {}).get("logMessages") or [])
    if f"Program {PUMP_PROGRAM} invoke" in logs:
        return "pump"
    if f"Program {PUMP_AMM_PROGRAM} invoke" in logs:
        return "pump-amm"
    return "other"


def trades_in(tx: dict, wallet: str, signature: str) -> list[dict]:
    """The wallet's swaps in a parsed transaction, as PumpPortal-style trade messages."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = _keys(tx)
    if wallet not in keys:
        return []
    i = keys.index(wallet)
    try:
        lamports = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    except (KeyError, IndexError, TypeError):
        return []

    def held(balances, mint):
        return sum(float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                   for b in balances or [] if b.get("owner") == wallet and b.get("mint") == mint)
    pre, post = meta.get("preTokenBalances"), meta.get("postTokenBalances")
    mints = {b.get("mint") for b in (pre or []) + (post or []) if b.get("owner") == wallet}
    sol = lamports + (held(post, SOL_MINT) - held(pre, SOL_MINT))   # wrapped SOL counts as SOL
    out = []
    for mint in mints - {SOL_MINT, None}:
        delta = held(post, mint) - held(pre, mint)
        if delta > 0 and sol < 0:
            side = "buy"
        elif delta < 0 and sol > 0:
            side = "sell"
        else:
            continue   # a transfer, an airdrop, a token-for-token swap: not a SOL trade
        out.append({"signature": signature, "mint": mint, "txType": side,
                    "traderPublicKey": wallet, "solAmount": abs(sol), "tokenAmount": abs(delta),
                    "newTokenBalance": held(post, mint), "pool": _venue(tx), "via": "rpc"})
    return out


class CopyPoller:
    def __init__(self, rpc: "SolanaRpc", wallets: Callable[[], Iterable[str]],
                 on_trade: Callable[[dict], Awaitable[None]],
                 enabled: Callable[[], bool] = lambda: True,
                 curve: Optional[Callable[[str], Awaitable[object]]] = None):
        self.rpc, self.wallets, self.on_trade = rpc, wallets, on_trade
        self.enabled, self.curve = enabled, curve
        self.last_sig: dict[str, Optional[str]] = {}
        self.errors = 0

    async def _signatures(self, wallet: str) -> list[dict]:
        opts: dict = {"limit": 20, "commitment": "confirmed"}
        if self.last_sig.get(wallet):
            opts["until"] = self.last_sig[wallet]
        return await self.rpc.call("getSignaturesForAddress", [wallet, opts]) or []

    async def poll_wallet(self, wallet: str) -> None:
        sigs = await self._signatures(wallet)
        first_look = wallet not in self.last_sig
        if sigs:
            self.last_sig[wallet] = sigs[0]["signature"]
        elif first_look:
            self.last_sig[wallet] = None
        if first_look:
            return   # start from now: what it did before the bot was watching isn't a signal
        now = time.time()
        for s in reversed(sigs):   # oldest first
            if s.get("err") is not None:
                continue
            bt = s.get("blockTime")
            if isinstance(bt, (int, float)) and now - bt > MAX_AGE_SECONDS:
                continue
            tx = await self.rpc.call("getTransaction", [s["signature"], {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"}])
            for msg in trades_in(tx or {}, wallet, s["signature"]):
                await self._enrich(msg)
                await self.on_trade(msg)

    async def _enrich(self, msg: dict) -> None:
        """Curve numbers, as PumpPortal sends them: needed for the market cap limit and to
        treat a coin still on its curve like one (it is)."""
        if msg["pool"] != "pump" or self.curve is None:
            return
        try:
            c = await self.curve(msg["mint"])
        except Exception:
            return
        if c is not None and not getattr(c, "complete", True) and getattr(c, "sol_quoted", True):
            msg["vSolInBondingCurve"], msg["vTokensInBondingCurve"] = c.v_sol, c.v_tokens
            msg["marketCapSol"] = c.price * PUMP_TOTAL_SUPPLY

    async def run(self) -> None:
        while True:
            wallets = list(self.wallets()) if self.enabled() else []
            for gone in set(self.last_sig) - set(wallets):
                self.last_sig.pop(gone, None)
            if wallets:
                results = await asyncio.gather(*(self.poll_wallet(w) for w in wallets),
                                               return_exceptions=True)
                failed = [r for r in results if isinstance(r, Exception)]
                if failed:
                    self.errors += 1
                    log.debug("copy watcher: %d wallet lookup(s) failed: %s", len(failed), failed[0])
            # a few wallets every 2s; more wallets, a bit slower, to spare the RPC
            await asyncio.sleep(max(POLL_SECONDS, len(wallets) * 0.4))
