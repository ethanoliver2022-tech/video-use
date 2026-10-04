"""Paper and live trade execution on Solana.

Routing:
  * pump.fun tokens (bonding curve or graduated) -> PumpPortal local-transaction API,
    which builds the tx; we sign it locally so the key never leaves this machine.
  * everything else -> Jupiter aggregator.
Fills are read back from the confirmed transaction's balance changes, so PnL
reflects actual slippage and fees.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..config import Config
from ..models import SOL_MINT, Candidate, Fill
from ..solana_rpc import SolanaRpc, balance_deltas
from .sender import TxSender

log = logging.getLogger(__name__)

PUMP_FEE = 0.0125  # protocol + creator fee on the bonding curve, approx.


@dataclass
class CurveState:
    v_sol: float
    v_tokens: float

    @property
    def price(self) -> float:
        return self.v_sol / self.v_tokens if self.v_tokens else 0.0

    def buy_out(self, sol_in: float) -> float:
        sol_net = sol_in * (1 - PUMP_FEE)
        k = self.v_sol * self.v_tokens
        return self.v_tokens - k / (self.v_sol + sol_net)

    def sell_out(self, tokens_in: float) -> float:
        k = self.v_sol * self.v_tokens
        return (self.v_sol - k / (self.v_tokens + tokens_in)) * (1 - PUMP_FEE)


class Executor(Protocol):
    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill: ...
    async def sell(self, mint: str, tokens: float, sell_all: bool, pump: bool,
                   curve: Optional[CurveState]) -> Fill: ...
    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]: ...


class Jupiter:
    def __init__(self, api: str, rpc: SolanaRpc, http: httpx.AsyncClient):
        self.api, self.rpc, self.http = api, rpc, http
        self._decimals: dict[str, int] = {SOL_MINT: 9}

    async def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            info = await self.rpc.get_mint_info(mint)
            if not info:
                raise RuntimeError(f"unknown mint {mint}")
            self._decimals[mint] = int(info["decimals"])
        return self._decimals[mint]

    async def quote(self, in_mint: str, out_mint: str, amount_ui: float, slippage_pct: float) -> dict:
        raw = int(amount_ui * 10 ** await self.decimals(in_mint))
        resp = await self.http.get(f"{self.api}/quote", params={
            "inputMint": in_mint, "outputMint": out_mint, "amount": raw,
            "slippageBps": int(slippage_pct * 100), "restrictIntermediateTokens": "true",
        })
        resp.raise_for_status()
        return resp.json()

    async def out_ui(self, quote: dict) -> float:
        return int(quote["outAmount"]) / 10 ** await self.decimals(quote["outputMint"])

    async def swap_tx(self, quote: dict, user: str, priority_fee_sol: float) -> bytes:
        resp = await self.http.post(f"{self.api}/swap", json={
            "quoteResponse": quote,
            "userPublicKey": user,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": int(priority_fee_sol * 1e9),
        })
        resp.raise_for_status()
        return base64.b64decode(resp.json()["swapTransaction"])


class PaperExecutor:
    """Simulates fills from live prices. No wallet, no transactions."""

    def __init__(self, jupiter: Jupiter, slippage_pct: float):
        self.jupiter, self.slippage_pct = jupiter, slippage_pct

    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill:
        if curve:
            return Fill(tokens=curve.buy_out(sol), sol=sol)
        q = await self.jupiter.quote(SOL_MINT, cand.mint, sol, self.slippage_pct)
        return Fill(tokens=await self.jupiter.out_ui(q), sol=sol)

    async def sell(self, mint: str, tokens: float, sell_all: bool, pump: bool,
                   curve: Optional[CurveState]) -> Fill:
        if curve:
            return Fill(tokens=tokens, sol=curve.sell_out(tokens))
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.slippage_pct)
        return Fill(tokens=tokens, sol=await self.jupiter.out_ui(q))

    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]:
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.slippage_pct)
        return await self.jupiter.out_ui(q)


class LiveExecutor:
    def __init__(self, cfg: Config, keypair: Keypair, rpc: SolanaRpc, jupiter: Jupiter,
                 http: httpx.AsyncClient, sender: Optional[TxSender] = None):
        self.cfg, self.kp, self.rpc, self.jupiter, self.http = cfg, keypair, rpc, jupiter, http
        self.pubkey = str(keypair.pubkey())
        self.sender = sender or TxSender(cfg.speed, rpc, http, cfg.trading.priority_fee_sol)

    def _sign(self, unsigned: bytes) -> VersionedTransaction:
        tx = VersionedTransaction.from_bytes(unsigned)
        return VersionedTransaction(tx.message, [self.kp])

    async def _pumpportal_tx(self, action: str, mint: str, amount, in_sol: bool) -> bytes:
        resp = await self.http.post(self.cfg.endpoints.pumpportal_trade, data={
            "publicKey": self.pubkey,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if in_sol else "false",
            "slippage": int(self.cfg.trading.slippage_pct),
            "priorityFee": await self.sender.priority_fee(),
            "pool": "auto",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"pumpportal {resp.status_code}: {resp.text[:200]}")
        return resp.content

    async def _submit(self, unsigned: bytes, mint: str) -> Fill:
        sig = await self.sender.send(self._sign(unsigned), self.kp)
        log.info("sent %s", sig)
        if not await self.rpc.confirm(sig):
            raise RuntimeError(f"transaction {sig} not confirmed in time")
        tx = await self.rpc.get_transaction(sig)
        if not tx:
            raise RuntimeError(f"confirmed tx {sig} not retrievable")
        tok, sol = balance_deltas(tx, self.pubkey, mint)
        return Fill(tokens=abs(tok), sol=abs(sol), signature=sig)

    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill:
        if cand.route == "pump":
            unsigned = await self._pumpportal_tx("buy", cand.mint, sol, in_sol=True)
        else:
            q = await self.jupiter.quote(SOL_MINT, cand.mint, sol, self.cfg.trading.slippage_pct)
            unsigned = await self.jupiter.swap_tx(q, self.pubkey, await self.sender.priority_fee())
        return await self._submit(unsigned, cand.mint)

    async def sell(self, mint: str, tokens: float, sell_all: bool, pump: bool,
                   curve: Optional[CurveState]) -> Fill:
        if sell_all:
            # sell what is actually in the wallet, not what we think is there
            tokens = await self.rpc.get_token_balance(self.pubkey, mint) or tokens
        if pump:
            try:
                unsigned = await self._pumpportal_tx(
                    "sell", mint, "100%" if sell_all else tokens, in_sol=False)
                return await self._submit(unsigned, mint)
            except Exception as e:
                log.warning("pumpportal sell failed (%s); falling back to Jupiter", e)
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.cfg.trading.slippage_pct)
        unsigned = await self.jupiter.swap_tx(q, self.pubkey, await self.sender.priority_fee())
        return await self._submit(unsigned, mint)

    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]:
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.cfg.trading.slippage_pct)
        return await self.jupiter.out_ui(q)
