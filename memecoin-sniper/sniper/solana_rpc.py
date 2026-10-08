"""Minimal async Solana JSON-RPC client (just the calls the bot needs)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import logging
import re
import time
from collections import deque
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError)


def _malformed(method: str) -> RpcError:
    return RpcError(f"{method}: malformed response from the RPC node")


CONFIRM_POLL_SECONDS = 0.4  # ~one slot: fills (and the exits after them) are seen sooner
CONFIRM_EVIDENCE_SECONDS = 15  # a 'not found' must be this recent to mean 'never landed'
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
# Token-2022 account extensions that never stop an empty account from being closed
CLOSABLE_EXTENSIONS = {"immutableOwner", "transferFeeAmount", "transferHookAccount",
                       "nonTransferableAccount", "memoTransfer", "cpiGuard"}


class TxFailed(RpcError):
    """The transaction landed on-chain but failed (e.g. slippage exceeded): nothing changed."""


class SolanaRpc:
    def __init__(self, url: str, client: Optional[httpx.AsyncClient] = None):
        self.url = url
        self.http = client or httpx.AsyncClient(timeout=15)
        self._ids = itertools.count(1)
        # (monotonic time, failed, rate limited) of recent calls: the health check reads it
        self.recent: deque[tuple[float, bool, bool]] = deque(maxlen=5000)

    def outcomes(self, window: float) -> tuple[int, int, int]:
        """(calls, failed, rate limited) in the last `window` seconds."""
        since = time.monotonic() - window
        rows = [r for r in self.recent if r[0] >= since]
        return len(rows), sum(1 for r in rows if r[1]), sum(1 for r in rows if r[2])

    def _note(self, failed: bool, limited: bool = False) -> None:
        self.recent.append((time.monotonic(), failed, limited))

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        try:
            resp = await self.http.post(self.url, json=body)
        except httpx.HTTPError as e:  # never echo the URL: paid RPC URLs embed the API key
            self._note(True)
            raise RpcError(f"{method}: {type(e).__name__}") from None
        if resp.status_code != 200:
            self._note(True, resp.status_code == 429)
            raise RpcError(f"{method}: HTTP {resp.status_code}"
                           + (" (rate limited)" if resp.status_code == 429 else ""))
        try:
            data = resp.json()
        except ValueError:
            self._note(True)
            raise RpcError(f"{method}: invalid JSON response") from None
        if "error" in data:
            # an answer about the request itself is not the service failing, unless it says
            # the plan's limit was hit (some providers rate-limit inside a 200 response)
            limited = bool(re.search(r"rate|limit|credits|too many", str(data["error"]), re.I))
            self._note(limited, limited)
            raise RpcError(f"{method}: {data['error']}")
        self._note(False)
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

    async def get_accounts_raw(self, addresses: list[str]) -> list[Optional[tuple[str, bytes]]]:
        """[(owner program, data) or None] for each address, in one call."""
        res = await self.call("getMultipleAccounts", [addresses, {"encoding": "base64",
                                                                  "commitment": "confirmed"}])
        try:
            out = []
            for value in res["value"]:
                if not value:
                    out.append(None)
                    continue
                out.append((str(value["owner"]),
                            base64.b64decode(value["data"][0], validate=True)))
            if len(out) != len(addresses):
                raise ValueError(len(out))
            return out
        except (*PARSE_ERRORS, binascii.Error):
            raise _malformed("getMultipleAccounts") from None

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

    async def empty_token_accounts(self, owner: str) -> list[dict]:
        """The owner's token accounts that hold nothing and that the owner may close:
        [{address, mint, program, lamports}]. Closing one returns its rent (~0.002 SOL)."""
        out = []
        for program in TOKEN_PROGRAMS:
            res = await self.call("getTokenAccountsByOwner", [
                owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            try:
                accounts = res["value"]
                if not isinstance(accounts, list):
                    raise TypeError(accounts)
            except PARSE_ERRORS:
                raise _malformed("getTokenAccountsByOwner") from None
            for a in accounts:
                try:
                    acct = a["account"]
                    info = acct["data"]["parsed"]["info"]
                    if (acct["owner"] != program or info["owner"] != owner
                            or str(info["tokenAmount"]["amount"]) != "0"
                            or info.get("state") != "initialized"
                            or info.get("closeAuthority") not in (None, owner)):
                        continue
                    if any(e.get("extension") not in CLOSABLE_EXTENSIONS
                           or str((e.get("state") or {}).get("withheldAmount", "0")) != "0"
                           for e in info.get("extensions") or []):
                        continue  # e.g. withheld transfer fees: Solana would refuse the close
                    lamports = int(acct["lamports"])
                    if lamports <= 0:
                        continue
                    out.append({"address": str(a["pubkey"]), "mint": str(info["mint"]),
                                "program": program, "lamports": lamports})
                except PARSE_ERRORS:
                    continue  # one odd account never blocks closing the others
        return out

    async def simulate(self, raw: bytes) -> Optional[dict]:
        """What this transaction would do right now (read-only: nothing is sent or paid).
        {"err": ..., "logs": [...]} or None."""
        res = await self.call("simulateTransaction", [base64.b64encode(raw).decode(), {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "confirmed"}])
        value = res.get("value") if isinstance(res, dict) else None
        return value if isinstance(value, dict) else None

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
            await asyncio.sleep(CONFIRM_POLL_SECONDS)  # about one slot
        # "Didn't land" only when the node answered, near the end of the blockhash's
        # life, that it has never seen it. Polls failing, or a processed-but-unconfirmed
        # status, leave the outcome unknown: callers then check the wallet instead.
        if seen or last_unseen is None or deadline - last_unseen > CONFIRM_EVIDENCE_SECONDS:
            raise RpcError(f"couldn't confirm {signature}: outcome unknown")
        return False

    async def get_transaction(self, signature: str) -> Optional[dict]:
        for _ in range(30):  # every 0.5s for up to 15s
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
            await asyncio.sleep(0.5)
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


class ReadPool:
    """Background reads (the copy watcher, wallet checks, dev wallet checks) spread over
    every RPC you have, extra ones first, so they don't use up the main RPC's plan that
    trading needs. An RPC that rate-limits is rested for a while; `rate` paces the calls."""

    REST_SECONDS = 15.0

    def __init__(self, rpcs: list["SolanaRpc"], rate: float = 0.0):
        self.rpcs = rpcs
        self.rate = rate
        self._resting: dict[int, float] = {}
        self._turn = 0
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def _pace(self) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + 1.0 / self.rate
        if wait > 0:
            await asyncio.sleep(wait)

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        await self._pace()
        now = time.monotonic()
        n = len(self.rpcs)
        order = [(self._turn + k) % n for k in range(n)]
        self._turn = (self._turn + 1) % n
        awake = [i for i in order if self._resting.get(i, 0) <= now] or order
        last: Optional[Exception] = None
        for i in awake:
            try:
                return await self.rpcs[i].call(method, params)
            except RpcError as e:
                last = e
                if re.search(r"rate|limit|429|credits|too many", str(e), re.I):
                    self._resting[i] = time.monotonic() + self.REST_SECONDS
                # any other failure (e.g. a send-only endpoint): try the next RPC
        raise last or RpcError(f"{method}: no RPC available")
