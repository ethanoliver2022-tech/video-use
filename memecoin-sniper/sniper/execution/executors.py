"""Paper and live trade execution on Solana.

Routing:
  * pump.fun tokens (bonding curve or graduated) -> PumpPortal local-transaction API,
    which builds the tx; we sign it locally so the key never leaves this machine.
  * everything else -> Jupiter aggregator.
Fills are read back from the confirmed transaction's balance changes, so PnL
reflects actual slippage and fees.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..config import Config
from ..models import SOL_MINT, Candidate, Fill
from ..solana_rpc import SolanaRpc, TxFailed, balance_deltas
from .sender import TxSender
from .txguard import UnsafeTransaction, check_transaction

log = logging.getLogger(__name__)

PUMP_FEE = 0.0125  # protocol + creator fee on the bonding curve, approx.
PUMPPORTAL_FEE = 0.005     # PumpPortal's fee on trades it builds (paper estimate; see pumpportal.fun)
NETWORK_FEE_SOL = 0.000005  # base signature fee


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


class NotLanded(RuntimeError):
    """The transaction expired without landing: safe to retry."""


class NotSent(RuntimeError):
    """Failed before anything reached the network: safe to treat as a plain failure."""


class BuyUncertain(RuntimeError):
    """The buy may or may not have landed and the wallet can't tell us yet. The engine
    keeps watching the wallet so tokens that arrive late are never orphaned."""

    def __init__(self, mint: str, sol: float, detail: str, pre: Optional[float] = None):
        super().__init__(f"couldn't confirm the buy yet ({detail})")
        self.mint, self.sol = mint, sol
        self.pre = pre  # tokens of this mint already in the wallet before the buy


class NothingToSell(RuntimeError):
    """The wallet holds none of the token (sold elsewhere, or a sell landed late)."""


class Executor(Protocol):
    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill: ...
    async def sell(self, mint: str, tokens: float, sell_all: bool, pump: bool,
                   curve: Optional[CurveState], slippage_pct: Optional[float] = None) -> Fill: ...
    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]: ...


def _decimal_str(raw: int, decimals: int) -> str:
    """The exact on-chain amount as a plain decimal (never '1e-05', never past the
    token's precision)."""
    whole, frac = divmod(raw, 10 ** decimals)
    return f"{whole}.{frac:0{decimals}d}".rstrip("0").rstrip(".") if decimals else str(whole)


LITE_API_HOST = "lite-api.jup.ag"
RETIRED_STATUS = (401, 403, 404, 410)  # what a retired endpoint answers
JUPITER_WINDOW = 60.0       # Jupiter counts requests over a sliding minute
BACKGROUND_SHARE = 0.5      # price checks / honeypot probes may use at most this much of it
URGENT_MAX_WAIT = 10.0      # a trade waits at most this long for a free slot, then goes anyway


class JupiterBusy(RuntimeError):
    """Skipped a background Jupiter call to keep the rate limit free for trades."""


class Jupiter:
    def __init__(self, api: str, rpc: SolanaRpc, http: httpx.AsyncClient, api_key: str = "",
                 requests_per_minute: Optional[int] = None):
        self.api, self.rpc, self.http = api, rpc, http
        self.headers = {"x-api-key": api_key} if api_key else {}
        self._decimals: dict[str, int] = {SOL_MINT: 9}
        if requests_per_minute is None:  # JUPITER_RPM in .env: for paid plans with more
            try:
                requests_per_minute = int(os.getenv("JUPITER_RPM", "0") or 0)
            except ValueError:
                requests_per_minute = 0
        self.rpm = max(0, requests_per_minute)
        self._calls: deque[float] = deque()

    def budget(self) -> int:
        """Requests per minute Jupiter allows this setup (free key or lite-api: 60/min,
        keyless api.jup.ag: 30/min)."""
        if self.rpm:
            return self.rpm
        return 60 if self.headers or LITE_API_HOST in self.api else 30

    async def _slot(self, urgent: bool) -> None:
        """Trades (buys, sells) always come first: background calls only get part of the
        budget and are skipped, never queued, when it's used up."""
        give_up = time.monotonic() + URGENT_MAX_WAIT
        while True:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > JUPITER_WINDOW:
                self._calls.popleft()
            budget = self.budget()
            limit = budget if urgent else max(1, int(budget * BACKGROUND_SHARE))
            if len(self._calls) < limit or (urgent and now >= give_up):
                self._calls.append(now)
                return
            if not urgent:
                raise JupiterBusy("Jupiter rate limit reserved for trades right now")
            await asyncio.sleep(min(0.25, max(0.01, give_up - now)))

    async def _request(self, method: str, path: str, urgent: bool = True, **kw) -> httpx.Response:
        await self._slot(urgent)
        resp = await self._send(method, path, **kw)
        for delay in ((0.5, 1.0) if urgent else ()):  # rate limited anyway: a trade tries again
            if resp.status_code != 429:
                break
            await asyncio.sleep(delay)
            self._calls.append(time.monotonic())  # every request Jupiter sees counts
            resp = await self._send(method, path, **kw)
        return resp

    async def _send(self, method: str, path: str, **kw) -> httpx.Response:
        """Jupiter is retiring the keyless lite-api. If it stops answering, move to api.jup.ag
        (keyless there is slower but works) for good, instead of every trade failing."""
        try:
            resp = await self.http.request(method, f"{self.api}{path}", headers=self.headers, **kw)
        except httpx.ConnectError:
            if LITE_API_HOST not in self.api:
                raise
            resp = None
        if LITE_API_HOST in self.api and (resp is None or resp.status_code in RETIRED_STATUS):
            self.api = self.api.replace(LITE_API_HOST, "api.jup.ag")
            log.warning("Jupiter's keyless lite-api didn't answer (%s): switched to %s. Add a "
                        "free JUPITER_API_KEY to .env for faster quotes",
                        "unreachable" if resp is None else resp.status_code, self.api)
            self._calls.append(time.monotonic())
            resp = await self.http.request(method, f"{self.api}{path}", headers=self.headers, **kw)
        return resp

    async def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            if len(self._decimals) > 20_000:  # bounded for 24/7 running
                self._decimals = {SOL_MINT: 9}
            info = await self.rpc.get_mint_info(mint)
            if not info:
                raise RuntimeError(f"unknown mint {mint}")
            self._decimals[mint] = int(info["decimals"])
        return self._decimals[mint]

    async def quote(self, in_mint: str, out_mint: str, amount_ui: float, slippage_pct: float,
                    raw_amount: Optional[int] = None, urgent: bool = True) -> dict:
        """urgent=False for background lookups (prices, safety probes): see _slot."""
        raw = raw_amount if raw_amount is not None else int(amount_ui * 10 ** await self.decimals(in_mint))
        if raw <= 0:
            raise ValueError("amount too small to quote")
        resp = await self._request("GET", "/quote", urgent, params={
            "inputMint": in_mint, "outputMint": out_mint, "amount": raw,
            "slippageBps": int(slippage_pct * 100), "restrictIntermediateTokens": "true",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"jupiter quote {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "outAmount" not in data:
            raise RuntimeError(f"jupiter: no route ({str(data)[:200]})")
        return data

    async def out_ui(self, quote: dict) -> float:
        raw = quote.get("outAmount") if isinstance(quote, dict) else None
        if not isinstance(raw, (str, int)) or not str(raw).isdigit():
            raise ValueError(f"jupiter: malformed outAmount {raw!r}")
        return int(raw) / 10 ** await self.decimals(quote["outputMint"])

    async def swap_tx(self, quote: dict, user: str, priority_fee_sol: float) -> bytes:
        resp = await self._request("POST", "/swap", json={
            "quoteResponse": quote,
            "userPublicKey": user,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": int(priority_fee_sol * 1e9),
        })
        if resp.status_code != 200:
            raise RuntimeError(f"jupiter swap {resp.status_code}: {resp.text[:200]}")
        return base64.b64decode(resp.json()["swapTransaction"])


class PaperExecutor:
    """Simulates fills from live prices. No wallet, no transactions.

    Fills are charged what a live trade pays on top of the price (pump.fun's fee is
    already in the curve math): network + priority fee, the Jito tip and PumpPortal's
    fee on pump.fun trades. Without them paper results would look better than live."""

    def __init__(self, jupiter: Jupiter, slippage_pct: float, cfg: Optional[Config] = None):
        self.jupiter, self.slippage_pct, self.cfg = jupiter, slippage_pct, cfg

    def _costs(self) -> float:
        if self.cfg is None:
            return 0.0
        return NETWORK_FEE_SOL + self.cfg.trading.priority_fee_sol + self.cfg.speed.tip_sol()

    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill:
        if curve:
            pp = sol * PUMPPORTAL_FEE if self.cfg else 0.0
            return Fill(tokens=curve.buy_out(sol), sol=sol + pp + self._costs())
        q = await self.jupiter.quote(SOL_MINT, cand.mint, sol, self.slippage_pct)
        return Fill(tokens=await self.jupiter.out_ui(q), sol=sol + self._costs())

    async def sell(self, mint: str, tokens: float, sell_all: bool, pump: bool,
                   curve: Optional[CurveState], slippage_pct: Optional[float] = None) -> Fill:
        if curve:
            out = curve.sell_out(tokens)
            out -= out * PUMPPORTAL_FEE if self.cfg else 0.0
        else:
            q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.slippage_pct)
            out = await self.jupiter.out_ui(q)
        return Fill(tokens=tokens, sol=max(0.0, out - self._costs()))

    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]:
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.slippage_pct, urgent=False)
        return await self.jupiter.out_ui(q)


class LiveExecutor:
    def __init__(self, cfg: Config, keypair: Keypair, rpc: SolanaRpc, jupiter: Jupiter,
                 http: httpx.AsyncClient, sender: Optional[TxSender] = None):
        self.cfg, self.kp, self.rpc, self.jupiter, self.http = cfg, keypair, rpc, jupiter, http
        self.pubkey = str(keypair.pubkey())
        self.sender = sender or TxSender(cfg.speed, rpc, http, lambda: cfg.trading.priority_fee_sol)

    def _sign(self, unsigned: bytes) -> VersionedTransaction:
        tx = VersionedTransaction.from_bytes(unsigned)
        return VersionedTransaction(tx.message, [self.kp])

    async def _pumpportal_tx(self, action: str, mint: str, amount, in_sol: bool,
                             slippage_pct: Optional[float] = None) -> bytes:
        slippage = self.cfg.trading.slippage_pct if slippage_pct is None else slippage_pct
        resp = await self.http.post(self.cfg.endpoints.pumpportal_trade, data={
            "publicKey": self.pubkey,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if in_sol else "false",
            "slippage": max(1, round(slippage)),  # PumpPortal takes whole percents
            "priorityFee": await self.sender.priority_fee(),
            "pool": "auto",
        })
        if resp.status_code != 200:
            raise RuntimeError(f"pumpportal {resp.status_code}: {resp.text[:200]}")
        return resp.content

    def _tip(self) -> float:
        return self.cfg.speed.tip_sol()

    def _guard(self, tx: VersionedTransaction, max_sol_out: float, side: str = "buy",
               swap_sol: float = 0.0) -> None:
        s = self.cfg.speed  # the priority fee may not exceed what you allow (plus margin)
        max_fee = 2 * max(s.max_priority_fee_sol, self.cfg.trading.priority_fee_sol) + 0.001
        # a pump.fun buy may pull at most the swap amount plus your slippage (plus margin)
        curve_cap = swap_sol * (1 + self.cfg.trading.slippage_pct / 100) * 1.05 + 0.01
        check_transaction(tx, self.kp.pubkey(), max_sol_out,
                          frozenset(self.cfg.extra_allowed_programs), max_fee, side,
                          curve_cap if side == "buy" else 0.0)

    def _check_unsigned(self, unsigned: bytes, max_sol_out: float, side: str = "sell") -> None:
        """Guard a built transaction before choosing it (raises UnsafeTransaction)."""
        try:
            tx = VersionedTransaction.from_bytes(unsigned)
        except Exception:
            return  # not a transaction at all: _submit refuses it as unbuildable
        self._guard(tx, max_sol_out, side)

    def _buy_cap(self, sol: float) -> float:
        # top-level SOL out on a buy: the swap amount (Jupiter wraps it) or PumpPortal's
        # 0.5% fee, plus room for rent; the bonding-curve payment itself runs in the program
        return sol * 1.05 + 0.01

    @staticmethod
    def _sell_cap(value_sol: Optional[float]) -> float:
        # top-level SOL out on a sell is only PumpPortal's 0.5% fee
        return 0.01 + 0.02 * value_sol if value_sol else 0.05

    async def _submit(self, unsigned: bytes, mint: str, side: str,
                      max_sol_out: float = 0.05, swap_sol: float = 0.0) -> Fill:
        """Sign, send, confirm and read back the real fill.

        Raises NotLanded if the transaction expired without landing. Any other error
        after sending means the outcome is uncertain; callers reconcile with the wallet.
        """
        try:  # guard the exact message that gets signed, before the signature exists
            built = VersionedTransaction.from_bytes(unsigned)
        except Exception:
            built = None  # not a transaction at all: signing it fails just below
        if built is not None:
            try:
                self._guard(built, max_sol_out, side, swap_sol)
            except UnsafeTransaction as e:
                log.error("refused to sign a %s for %s: %s", side, mint, e)
                raise NotSent(f"🛡 refused to sign it: {e}") from e
        try:
            signed = self._sign(unsigned)
        except Exception as e:  # e.g. an error body instead of a transaction: nothing was sent
            raise NotSent(f"couldn't build the transaction: {str(e)[:120]}") from e
        sig = await self.sender.send(signed, self.kp)
        log.info("sent %s %s", side, sig)
        if not await self.rpc.confirm(sig):
            raise NotLanded(f"transaction {sig} expired without landing")
        tx = await self.rpc.get_transaction(sig)
        readable = bool(tx)
        try:
            tok, sol = balance_deltas(tx, self.pubkey, mint) if tx else (0.0, 0.0)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            tok, sol, readable = 0.0, 0.0, False  # unreadable: fall back to the wallet below
        # a buy costs the outflow; a sell receives the inflow. A sell whose fees exceeded its
        # proceeds (dust, a rugged token) received nothing, not abs(-fees).
        tok = abs(tok)
        sol = max(0.0, -sol) if side == "buy" else max(0.0, sol)
        from_wallet = False
        if tok <= 0:
            log.warning("could not read fill for %s from the transaction; using wallet balance", sig)
            if side == "buy":
                tok = await self.rpc.get_token_balance(self.pubkey, mint)
                from_wallet = True
        # the Jito tip is a separate transaction in the bundle: count it as a cost
        tip = await self._tip_paid(sig)
        sol = sol + tip if side == "buy" else max(0.0, sol - tip)
        return Fill(tokens=tok, sol=sol, signature=sig, from_wallet=from_wallet,
                    sol_known=readable)

    async def _tip_paid(self, sig: str) -> float:
        """The tip only lands with its bundle. If the trade went through plain RPC instead
        (fallback, or the RPC copy won the race) the tip was never paid."""
        tips = getattr(self.sender, "tip_sigs", None)
        if tips is None:
            return self._tip()
        tip_sig = tips.pop(sig, None)
        if not tip_sig:
            return 0.0  # no bundle was sent for this trade
        try:
            res = await self.rpc.call("getSignatureStatuses",
                                      [[tip_sig], {"searchTransactionHistory": False}])
            status = (res or {}).get("value", [None])[0]
        except Exception:
            return self._tip()  # can't tell: count it (conservative)
        if isinstance(status, dict) and status.get("err") is None:
            return self.cfg.speed.jito_tip_sol
        return 0.0

    async def _held_or_none(self, mint: str) -> Optional[float]:
        try:
            return await self.rpc.get_token_balance(self.pubkey, mint)
        except Exception:
            return None

    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill:
        # Tokens of this mint already in the wallet (e.g. a written-off bag) must never be
        # mistaken for this buy's fill. Read alongside building the tx: no added latency.
        pre_task = asyncio.ensure_future(self._held_or_none(cand.mint))
        try:
            if cand.route == "pump":
                unsigned = await self._pumpportal_tx("buy", cand.mint, sol, in_sol=True)
            else:
                q = await self.jupiter.quote(SOL_MINT, cand.mint, sol, self.cfg.trading.slippage_pct)
                unsigned = await self.jupiter.swap_tx(q, self.pubkey, await self.sender.priority_fee())
        except BaseException:
            pre_task.cancel()
            raise
        pre = await pre_task
        base = pre or 0.0
        try:
            fill = await self._submit(unsigned, cand.mint, "buy", self._buy_cap(sol), sol)
            if fill.from_wallet:
                fill.tokens = max(0.0, fill.tokens - base)
            fill.pre = pre
            return fill
        except (NotLanded, TxFailed, NotSent):
            raise  # definitely didn't buy
        except Exception as e:
            # Outcome unknown (an RPC hiccup after sending). Never report "failed" here: the
            # tokens may already be in the wallet, or land within the blockhash lifetime.
            for delay in (0.5, 1.0, 2.0):
                try:
                    held = await self.rpc.get_token_balance(self.pubkey, cand.mint)
                except Exception:
                    await asyncio.sleep(delay)
                    continue
                if pre is not None and held > base:  # (unknown baseline: the reconciler decides)
                    log.warning("buy %s errored (%s) but tokens arrived; tracking them", cand.mint, e)
                    return Fill(tokens=held - base, sol=sol + self._tip(), signature="unconfirmed")
                break
            raise BuyUncertain(cand.mint, sol + self._tip(), str(e), pre=pre) from e

    async def sell(self, mint: str, tokens: float, sell_all: bool, pump: bool,
                   curve: Optional[CurveState], slippage_pct: Optional[float] = None,
                   value_sol: Optional[float] = None) -> Fill:
        slippage = self.cfg.trading.slippage_pct if slippage_pct is None else slippage_pct
        # A full pump.fun exit ("100%") doesn't depend on the balance: build it while the
        # balance is read, so stop-loss and dev-dump exits don't wait on two round trips.
        early = None
        if pump and sell_all:
            early = asyncio.ensure_future(self._pumpportal_tx("sell", mint, "100%", in_sol=False,
                                                              slippage_pct=slippage))
        try:
            raw, decimals = await self.rpc.get_token_balance_raw(self.pubkey, mint)
        except BaseException:
            if early:
                early.cancel()
            raise
        if raw <= 0:
            if early:
                early.cancel()
            raise NothingToSell(f"no {mint} left in the wallet")
        held = raw / 10 ** decimals
        if sell_all or tokens >= held:
            sell_all, tokens, sell_raw = True, held, raw   # sell what is really there
        else:
            sell_raw = int(tokens * 10 ** decimals)
        unsigned = None
        if early is not None:  # (only built for a full exit, which stays a full exit)
            try:
                unsigned = await early
            except Exception as e:
                log.warning("pumpportal sell build failed (%s); using Jupiter", e)
        if pump and unsigned is None and early is None:
            try:  # only *building* falls back; once a tx is sent we never send a second one
                unsigned = await self._pumpportal_tx(
                    "sell", mint, "100%" if sell_all else _decimal_str(sell_raw, decimals),
                    in_sol=False, slippage_pct=slippage)
            except Exception as e:
                log.warning("pumpportal sell build failed (%s); using Jupiter", e)
        if unsigned is not None:
            try:  # a refused PumpPortal tx must never strand an exit: use Jupiter instead
                self._check_unsigned(unsigned, self._sell_cap(value_sol))
            except UnsafeTransaction as e:
                log.error("refused PumpPortal's sell for %s (%s); using Jupiter", mint, e)
                unsigned = None
        if unsigned is None:
            q = await self.jupiter.quote(mint, SOL_MINT, tokens, slippage, raw_amount=sell_raw)
            unsigned = await self.jupiter.swap_tx(q, self.pubkey, await self.sender.priority_fee())
        fill = await self._submit(unsigned, mint, "sell", self._sell_cap(value_sol))
        if fill.tokens <= 0:
            fill.tokens = tokens
        fill.emptied = sell_all  # incl. a partial that asked for more than the wallet held
        return fill

    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]:
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.cfg.trading.slippage_pct,
                                     urgent=False)  # only prices the position
        return await self.jupiter.out_ui(q)
