"""Minimal async Solana JSON-RPC client (just the calls the bot needs)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError)


def _malformed(method: str) -> RpcError:
    return RpcError(f"{method}: malformed response from the RPC node")


CONFIRM_EVIDENCE_SECONDS = 15  # a 'not found' must be this recent to mean 'never landed'


class TxFailed(RpcError):
    """The transaction landed on-chain but failed (e.g. slippage exceeded): nothing changed."""


class SolanaRpc:
    def __init__(self, url: str, client: Optional[httpx.AsyncClient] = None):
        self.url = url
        self.http = client or httpx.AsyncClient(timeout=15)
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        try:
            resp = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:  # never echo the URL: paid RPC URLs embed the API key
            raise RpcError(f"{method}: {type(e).__name__}") from None
        if resp.status_code != 200:
            raise RpcError(f"{method}: HTTP {resp.status_code}"
                           + (" (rate limited)" if resp.status_code == 429 else ""))
        try:
            data = resp.json()
        except ValueError:
            raise RpcError(f"{method}: invalid JSON response") from None
        if "error" in data:
            raise RpcError(f"{method}: {data['error']}")
        return data.get("result")

    async def get_balance_sol(self, pubkey: str) -> float:
        res = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        try:
            lamports = res["value"]
            if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports < 0:
                raise ValueError(lamports)
        except PARSE_ERRORS:
            raise _malformed("getBalance") from None
        return lamports / 1e9

    async def get_latest_blockhash(self):
        from solders.hash import Hash
        res = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        try:
            return Hash.from_string(res["value"]["blockhash"])
        except PARSE_ERRORS:
            raise _malformed("getLatestBlockhash") from None

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

    async def get_token_balance_raw(self, owner: str, mint: str) -> tuple[int, int]:
        """(raw amount, decimals) summed over the owner's accounts for this mint."""
        res = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        raw, decimals = 0, 0
        try:
            for acct in res["value"]:
                amt = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
                if not str(amt["amount"]).isdigit() or not 0 <= int(amt["decimals"]) <= 18:
                    raise ValueError(amt)
                raw += int(amt["amount"])
                decimals = int(amt["decimals"])
        except PARSE_ERRORS:
            raise _malformed("getTokenAccountsByOwner") from None
        return raw, decimals

    async def get_token_balance(self, owner: str, mint: str) -> float:
        raw, decimals = await self.get_token_balance_raw(owner, mint)
        return raw / 10 ** decimals if raw else 0.0

    async def get_account_bytes(self, address: str) -> Optional[bytes]:
        res = await self.call("getAccountInfo", [address, {"encoding": "base64",
                                                           "commitment": "confirmed"}])
        try:
            value = res and res.get("value")
            if not value:
                return None
            return base64.b64decode(value["data"][0], validate=True)
        except (*PARSE_ERRORS, binascii.Error):
            raise _malformed("getAccountInfo") from None

    async def send_raw_transaction(self, raw: bytes) -> str:
        encoded = base64.b64encode(raw).decode()
        return await self.call(
            "sendTransaction",
            [encoded, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
        )

    async def confirm(self, signature: str, timeout: float = 90.0) -> bool:
        """Poll until confirmed. Returns False on timeout, raises on on-chain error.

        90s covers a blockhash's whole lifetime (~150 slots), so a False here means
        the transaction can no longer land."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_unseen = None   # when a good poll last said "no such transaction"
        seen = False         # ever seen processed (could still be confirmed)
        while loop.time() < deadline:
            try:
                res = await self.call(
                    "getSignatureStatuses", [[signature], {"searchTransactionHistory": False}]
                )
            except (httpx.HTTPError, ValueError, RpcError) as e:  # a flaky poll must not abort
                log.debug("status poll failed: %s", e)
                await asyncio.sleep(1.0)
                continue
            try:
                status = ((res or {}).get("value") or [None])[0]
                if status is not None and not isinstance(status, dict):
                    raise TypeError(status)
            except PARSE_ERRORS:
                status = None  # malformed: treat as "not seen yet" and keep polling
            if status:
                if status.get("err"):
                    raise TxFailed(f"transaction {signature} failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
                seen = True
            elif res is not None:
                last_unseen = loop.time()
            await asyncio.sleep(1.0)
        # "Didn't land" only when the node answered, near the end of the blockhash's
        # life, that it has never seen it. Polls failing, or a processed-but-unconfirmed
        # status, leave the outcome unknown: callers then check the wallet instead.
        if seen or last_unseen is None or deadline - last_unseen > CONFIRM_EVIDENCE_SECONDS:
            raise RpcError(f"couldn't confirm {signature}: outcome unknown")
        return False

    async def get_transaction(self, signature: str) -> Optional[dict]:
        for _ in range(15):
            try:
                res = await self.call(
                "getTransaction",
                    [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                 "commitment": "confirmed"}],
                )
            except (httpx.HTTPError, ValueError, RpcError) as e:
                log.debug("getTransaction failed: %s", e)
                res = None
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
