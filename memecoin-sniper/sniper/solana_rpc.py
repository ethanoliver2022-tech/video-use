"""Minimal async Solana JSON-RPC client (just the calls the bot needs)."""
from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class SolanaRpc:
    def __init__(self, url: str, client: Optional[httpx.AsyncClient] = None):
        self.url = url
        self.http = client or httpx.AsyncClient(timeout=15)
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        resp = await self.http.post(self.url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RpcError(f"{method}: {data['error']}")
        return data.get("result")

    async def get_balance_sol(self, pubkey: str) -> float:
        res = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return res["value"] / 1e9

    async def get_mint_info(self, mint: str) -> Optional[dict]:
        res = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = res and res.get("value")
        if not value:
            return None
        data = value.get("data")
        if not isinstance(data, dict):
            return None
        info = data.get("parsed", {}).get("info", {})
        info["program"] = data.get("program")
        return info

    async def get_largest_accounts(self, mint: str) -> list[dict]:
        res = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return res.get("value", []) if res else []

    async def get_multiple_accounts_parsed(self, addresses: list[str]) -> list[Optional[dict]]:
        if not addresses:
            return []
        res = await self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])
        return res.get("value", []) if res else []

    async def get_token_balance(self, owner: str, mint: str) -> float:
        res = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0.0
        for acct in (res or {}).get("value", []):
            amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amt.get("uiAmount") or 0)
        return total

    async def send_raw_transaction(self, raw: bytes) -> str:
        encoded = base64.b64encode(raw).decode()
        return await self.call(
            "sendTransaction",
            [encoded, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
        )

    async def confirm(self, signature: str, timeout: float = 45.0) -> bool:
        """Poll until confirmed. Returns False on timeout, raises on on-chain error."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            res = await self.call(
                "getSignatureStatuses", [[signature], {"searchTransactionHistory": False}]
            )
            status = (res or {}).get("value", [None])[0]
            if status:
                if status.get("err"):
                    raise RpcError(f"transaction {signature} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(1.0)
        return False

    async def get_transaction(self, signature: str) -> Optional[dict]:
        for _ in range(10):
            res = await self.call(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                             "commitment": "confirmed"}],
            )
            if res:
                return res
            await asyncio.sleep(1.0)
        return None


def balance_deltas(tx: dict, owner: str, mint: str) -> tuple[float, float]:
    """(token delta, SOL delta) for `owner` in a parsed transaction.

    SOL delta is the fee payer's lamport change, so it already includes network
    and priority fees — the true cost of the trade.
    """
    meta = tx["meta"]
    keys = tx["transaction"]["message"]["accountKeys"]
    payer_index = next(
        (i for i, k in enumerate(keys) if (k["pubkey"] if isinstance(k, dict) else k) == owner), 0
    )
    sol_delta = (meta["postBalances"][payer_index] - meta["preBalances"][payer_index]) / 1e9

    def total(balances: list[dict]) -> float:
        return sum(
            float(b["uiTokenAmount"].get("uiAmount") or 0)
            for b in balances
            if b.get("mint") == mint and b.get("owner") == owner
        )

    token_delta = total(meta.get("postTokenBalances") or []) - total(meta.get("preTokenBalances") or [])
    return token_delta, sol_delta
