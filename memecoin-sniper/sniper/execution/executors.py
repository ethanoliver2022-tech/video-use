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
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from ..config import Config
from ..models import SOL_MINT, Candidate, Fill
from ..solana_rpc import SolanaRpc, TxFailed, balance_deltas
from .pump_native import NotNative, PumpNative
from .sender import TxSender
from .txguard import UnsafeTransaction, check_transaction, describe, strip_untrusted

log = logging.getLogger(__name__)

# a sell bundle not confirmed by then also goes out through RPC (which gives up its sandwich
# protection): fast for emergency exits (stop loss, dev dump), patient for the rest
SELL_ESCALATE_SECONDS = 2.5
SELL_ESCALATE_CALM_SECONDS = 10.0
PREBUILT_MAX_AGE = 5.0  # seconds a transaction built ahead of the buy may be used for
SIMULATE_TIMEOUT = 5.0
CLOSE_BATCH = 12             # empty token accounts closed per transaction
CLOSE_UNITS = 10_000         # compute units per close (it uses ~3-5k)
CLOSE_CU_PRICE = 100_000     # micro-lamports per unit: ~0.000001 SOL per account closed
CLOSE_CONFIRM_TIMEOUT = 60.0
PUMP_FEE = 0.0125  # protocol + creator fee on the bonding curve, approx.
PUMPPORTAL_FEE = 0.005     # PumpPortal's fee on trades it builds (paper estimate; see pumpportal.fun)
NETWORK_FEE_SOL = 0.000005  # base signature fee


_ERROR_CODE = re.compile(r"Error Code: (\w+)")
_SLIPPAGE = ("TooMuchSolRequired", "TooLittleSolReceived", "ExceededSlippage",
             "SlippageToleranceExceeded", "SlippageExceeded", "MaxQuoteAmountInExceeded",
             "MinQuoteAmountOutNotMet")


def explain_failure(err, logs) -> str:
    """A simulation's error and logs -> a short reason a person can act on."""
    logs = [x for x in logs if isinstance(x, str)] if isinstance(logs, list) else []
    text = " ".join(logs) + " " + str(err)
    code = next((m.group(1) for x in logs if (m := _ERROR_CODE.search(x))), "")
    low = text.lower()
    if code in _SLIPPAGE or "slippage" in low:
        return ("the price moved more than your slippage allows (it was pumping too fast). "
                "Raise slippage in ⚙️ Settings, or skip tokens this hot")
    if code == "BondingCurveComplete":
        return "the token graduated off pump.fun at that moment"
    if "insufficient lamports" in low or "insufficientfunds" in low.replace(" ", ""):
        return "not enough SOL in the wallet for the trade plus fees"
    if code:
        return f"the trade program refused it ({code})"
    return f"the network refused it ({str(err)[:120]})"


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
LITE_STRIKES = 3  # lite-api failures in a row before moving to api.jup.ag for good
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
        self._lite_strikes = 0  # lite-api failures in a row
        self.limited: deque[float] = deque(maxlen=500)  # when Jupiter still said 429 (health)

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
        if resp.status_code == 429:
            self.limited.append(time.monotonic())
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
        if LITE_API_HOST not in self.api:
            return resp
        if resp is not None and resp.status_code not in RETIRED_STATUS:
            self._lite_strikes = 0
            return resp
        # this request goes to api.jup.ag; only repeated failures move there for good (one
        # Cloudflare hiccup mustn't halve the rate budget until the next restart)
        self._lite_strikes += 1
        fallback = self.api.replace(LITE_API_HOST, "api.jup.ag")
        if self._lite_strikes >= LITE_STRIKES:
            self.api = fallback
            log.warning("Jupiter's keyless lite-api stopped answering (%s): switched to %s. "
                        "Add a free JUPITER_API_KEY to .env for faster quotes",
                        "unreachable" if resp is None else resp.status_code, fallback)
        self._calls.append(time.monotonic())
        return await self.http.request(method, f"{fallback}{path}", headers=self.headers, **kw)

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
        self.native = PumpNative(rpc)
        # direct pump.fun trades, per side: None = not checked yet, True = checked and used,
        # False = the check failed this session (pump.fun changed): other routes are used.
        # Buys and sells are checked separately: each has its own accounts.
        self.native_state: dict[str, Optional[bool]] = {"buy": None, "sell": None}
        self.notice = None  # set by the engine: a one-line Telegram message
        self.native_why = ""  # why the last direct pump.fun trade wasn't possible

    @property
    def native_ok(self) -> Optional[bool]:
        """Direct pump.fun buys: checked and on (True), off (False) or not checked yet."""
        return self.native_state["buy"]

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
               swap_sol: float = 0.0, mint: Optional[str] = None) -> None:
        s = self.cfg.speed  # the priority fee may not exceed what you allow (plus margin)
        max_fee = 2 * max(s.max_priority_fee_sol, self.cfg.trading.priority_fee_sol) + 0.001
        # a pump.fun buy may pull at most the swap amount plus your slippage (plus margin)
        curve_cap = swap_sol * (1 + self._max_buy_slippage() / 100) * 1.05 + 0.01
        check_transaction(tx, self.kp.pubkey(), max_sol_out,
                          frozenset(self.cfg.extra_allowed_programs), max_fee, side,
                          curve_cap if side == "buy" else 0.0, mint)

    def _check_unsigned(self, unsigned: bytes, max_sol_out: float, side: str = "sell") -> None:
        """Guard a built transaction before choosing it (raises UnsafeTransaction)."""
        try:
            tx = VersionedTransaction.from_bytes(unsigned)
        except Exception:
            return  # not a transaction at all: _submit refuses it as unbuildable
        self._guard(tx, max_sol_out, side)

    def _buy_cap(self, sol: float) -> float:
        # top-level SOL out on a buy: the SOL wrapped for the swap (a PumpSwap buy wraps up to
        # the swap amount plus your slippage; Jupiter wraps the amount) plus PumpPortal's 0.5%
        # fee and room for rent; a bonding-curve payment itself runs inside the program
        return sol * (1 + max(0.0, self._max_buy_slippage()) / 100) * 1.05 + 0.01

    def _max_buy_slippage(self) -> float:
        """The highest slippage any buy may use (your normal one, or the copy-trade one):
        the transaction guard allows a buy to pull at most that much."""
        return max(self.cfg.trading.slippage_pct, getattr(self.cfg.copytrade, "slippage_pct", 0) or 0)

    def _slip(self, cand: Candidate) -> float:
        return cand.slippage_pct or self.cfg.trading.slippage_pct

    @staticmethod
    def _sell_cap(value_sol: Optional[float]) -> float:
        # top-level SOL out on a sell is only PumpPortal's 0.5% fee
        return 0.01 + 0.02 * value_sol if value_sol else 0.05

    async def _submit(self, unsigned: bytes, mint: str, side: str,
                      max_sol_out: float = 0.05, swap_sol: float = 0.0,
                      urgent: bool = False, timings: Optional[dict] = None) -> Fill:
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
                self._guard(built, max_sol_out, side, swap_sol, mint)
            except UnsafeTransaction as e:
                log.error("refused to sign a %s for %s: %s", side, mint, e)
                raise NotSent(f"🛡 refused to sign it: {e}") from e
        timings = {} if timings is None else timings
        t_send = time.monotonic()
        try:
            signed = self._sign(unsigned)
        except Exception as e:  # e.g. an error body instead of a transaction: nothing was sent
            raise NotSent(f"couldn't build the transaction: {str(e)[:120]}") from e
        sig = await self.sender.send(signed, self.kp)
        timings["send"] = time.monotonic() - t_send  # guard + sign + first path accepting it
        t_confirm = time.monotonic()
        log.info("sent %s %s", side, sig)
        # An exit must not wait a whole blockhash lifetime on a bundle no leader picked (tip
        # below the going rate): if it hasn't confirmed shortly, the *same* signed transaction
        # also goes out through the RPCs. One signature can only land once: never a double sell.
        escalate = None
        if side == "sell" and hasattr(self.sender, "rebroadcast"):
            escalate = asyncio.ensure_future(self._escalate(
                signed, sig, SELL_ESCALATE_SECONDS if urgent else SELL_ESCALATE_CALM_SECONDS))
        try:
            landed = await self.rpc.confirm(sig)
        except TxFailed as e:  # landed but failed: its logs say why (slippage, graduated...)
            why = await self._why_failed(sig)
            raise TxFailed(str(e) + (f". Reason: {why}" if why else "")) from e
        finally:
            if escalate is not None:
                escalate.cancel()
        if not landed:
            why = await self.why_not_landed(signed, sig)
            raise NotLanded(f"transaction {sig} expired without landing"
                            + (f". Likely reason: {why}" if why else ""))
        timings["confirm"] = time.monotonic() - t_confirm
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
                    sol_known=readable, timings=timings)

    async def _why_failed(self, sig: str) -> str:
        """A failed transaction's own logs, as a reason a person can act on ('' if unread)."""
        try:
            tx = await asyncio.wait_for(self.rpc.get_transaction(sig), SIMULATE_TIMEOUT)
            meta = (tx or {}).get("meta") or {}
            if not meta.get("logMessages"):
                return ""
            return explain_failure(meta.get("err"), meta.get("logMessages"))
        except Exception as e:
            log.debug("couldn't read why %s failed: %s", sig, e)
            return ""

    async def why_not_landed(self, signed: VersionedTransaction, sig: str = "") -> str:
        """The likely reason a transaction expired: it's simulated against the chain as it is
        now (free, read-only). '' when the simulation passes or can't tell."""
        try:
            res = await asyncio.wait_for(self.rpc.simulate(bytes(signed)), SIMULATE_TIMEOUT)
        except Exception as e:
            log.debug("simulation failed: %s", e)
            res = None  # Jito may still know
        if res and res.get("err"):
            return explain_failure(res.get("err"), res.get("logs"))
        jito = await self._jito_verdict(sig)
        if jito:
            return jito
        if not res:
            return ""
        # it would work: it simply wasn't picked up in time
        sp = self.cfg.speed
        if getattr(self.sender, "bundles", None) is not None and sp.jito_enabled:
            more = ("raise the Min priority fee" if sp.jito_also_send_rpc
                    else "turn on 'Also send via RPC'")
            return ("the trade itself was fine, but no validator picked it up in time. Try a "
                    f"higher Jito tip, or {more} (⚙️ Settings → ⚡ Speed)")
        return ("the trade itself was fine, but no validator picked it up in time (the "
                "network was busy and the priority fee too low, or the RPC didn't pass it "
                "on). Jito on usually fixes this")

    async def _jito_verdict(self, sig: str) -> str:
        """Jito's own record of the bundle (kept ~5 min), as a reason; '' if it can't tell."""
        status_of = getattr(self.sender, "bundle_status", None)
        if status_of is None or not sig:
            return ""
        try:
            status = await asyncio.wait_for(status_of(sig), SIMULATE_TIMEOUT)
        except Exception as e:
            log.debug("bundle status failed: %s", e)
            return ""
        tip = self.cfg.speed.jito_tip_sol
        if status == "Failed":
            return (f"Jito ran it in the tip auction, but your {tip:g} SOL tip lost: other "
                    "bundles paid more. Raise the Jito tip"
                    + ("" if self.cfg.speed.jito_also_send_rpc
                       else ", or turn on 'Also send via RPC' (⚙️ Settings → ⚡ Speed)"))
        if status == "Invalid":
            if self.cfg.speed.jito_also_send_rpc:
                return ("Jito dropped the bundle before the auction, and the copy sent through "
                        "your RPC wasn't picked up either. More paths help: a second RPC in "
                        "EXTRA_RPC_URLS (.env), a higher Min priority fee, or a paid RPC plan")
            return ("Jito accepted the bundle but dropped it before the auction (it rejected "
                    "the bundle, or was rate-limiting this server). Turn on 'Also send via "
                    "RPC' (⚙️ Settings → ⚡ Speed) so trades don't depend on Jito alone")
        return ""

    async def close_empty(self, only: Optional[set] = None,
                          keep: frozenset = frozenset()) -> tuple[int, float]:
        """Close this wallet's empty token accounts and take back their rent (~0.002 SOL
        each). Solana refuses to close an account that still holds tokens, so this can never
        lose a token. `only`: just these mints; `keep`: never these. (closed, SOL back)."""
        found = [a for a in await self.rpc.empty_token_accounts(self.pubkey)
                 if (only is None or a["mint"] in only) and a["mint"] not in keep]
        closed, back = 0, 0.0
        for i in range(0, len(found), CLOSE_BATCH):
            batch = found[i:i + CLOSE_BATCH]
            ixs = [set_compute_unit_limit(CLOSE_UNITS * len(batch)),
                   set_compute_unit_price(CLOSE_CU_PRICE)]
            ixs += [Instruction(Pubkey.from_string(a["program"]), bytes([9]),
                                [AccountMeta(Pubkey.from_string(a["address"]), False, True),
                                 AccountMeta(self.kp.pubkey(), False, True),
                                 AccountMeta(self.kp.pubkey(), True, False)]) for a in batch]
            msg = MessageV0.try_compile(self.kp.pubkey(), ixs, [],
                                        await self.rpc.get_latest_blockhash())
            tx = VersionedTransaction(msg, [self.kp])
            self._guard(tx, 0.0, "sell")  # closes into this wallet only, moves no SOL out
            sig = await self.rpc.send_raw_transaction(bytes(tx))
            try:
                landed = await self.rpc.confirm(sig, timeout=CLOSE_CONFIRM_TIMEOUT)
            except Exception as e:  # failed or unknown: the rent simply stays where it was
                log.warning("closing %d empty token account(s) failed: %s", len(batch), e)
                continue
            if landed:
                closed += len(batch)
                back += sum(a["lamports"] for a in batch) / 1e9
                log.info("closed %d empty token account(s): %.4f SOL rent back (%s)",
                         len(batch), sum(a["lamports"] for a in batch) / 1e9, sig)
        return closed, back

    async def _escalate(self, signed: VersionedTransaction, sig: str, after: float) -> None:
        await asyncio.sleep(after)
        try:
            if await self.sender.rebroadcast(signed):
                log.info("sell %s not confirmed after %.1fs: also sent through RPC", sig, after)
        except Exception as e:  # best effort: the bundle is still in flight
            log.debug("sell rebroadcast failed: %s", e)

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

    async def _native_buy(self, cand: Candidate, sol: float) -> bytes:
        return await self.native.buy_tx(self.kp.pubkey(), cand.mint, sol,
                                        self._slip(cand),
                                        await self.sender.priority_fee())

    async def _pump_buy_tx(self, cand: Candidate, sol: float) -> bytes:
        """Once direct pump.fun trading passed its check: built here (one RPC round trip,
        no PumpPortal). Otherwise, or if that fails: PumpPortal."""
        if self.native_ok:
            native = await self._native("buy", lambda: self._native_buy(cand, sol))
            if native is not None:
                return native
        return await self._pumpportal_tx("buy", cand.mint, sol, in_sol=True,
                                         slippage_pct=self._slip(cand))

    async def prepare_buy(self, cand: Candidate, sol: float) -> tuple[bytes, Optional[float], float]:
        """Build a pump.fun buy (and read the pre-buy balance) ahead of time, while the filters
        are still running: (unsigned tx, tokens already held, monotonic time built). If the
        token passes, only signing and sending are left on the critical path."""
        unsigned, pre = await asyncio.gather(self._pump_buy_tx(cand, sol),
                                             self._held_or_none(cand.mint))
        return unsigned, pre, time.monotonic()

    async def _prebuilt(self, cand: Candidate, sol: float):
        """The transaction built ahead by prepare_buy, if it's for this buy and still fresh."""
        task = cand.prebuilt
        if task is None or cand.prebuilt_sol != sol or cand.route != "pump":
            return None
        try:
            unsigned, pre, built_at = await task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is None or getattr(me, "cancelling", lambda: 1)():  # we're cancelled ourselves
                raise
            return None  # only the prebuild was cancelled: build now
        except Exception as e:
            log.debug("prebuilt buy for %s unusable (%s); building now", cand.mint, e)
            return None
        if time.monotonic() - built_at > PREBUILT_MAX_AGE:
            return None  # the price has moved on: build a fresh one
        return unsigned, pre

    async def _native(self, side: str, build) -> Optional[bytes]:
        """A pump.fun trade built directly (see pump_native), or None to use another route.
        Before the first one is ever used it's test-run on the chain (free, nothing sent)."""
        if self.native_state[side] is False:
            return None
        try:
            unsigned = await build()
        except NotNative as e:
            log.info("direct pump.fun %s not possible: %s", side, e)
            self.native_why = str(e)
            return None
        except Exception as e:
            log.warning("direct pump.fun %s couldn't be built: %s", side, e)
            self.native_why = f"couldn't build it: {str(e)[:80]}"
            return None
        if self.native_state[side] is None and not await self._check_native(unsigned, side):
            return None
        return unsigned

    async def _check_native(self, unsigned: bytes, side: str) -> bool:
        try:
            res = await asyncio.wait_for(self.rpc.simulate(bytes(self._sign(unsigned))),
                                         SIMULATE_TIMEOUT)
        except Exception as e:
            log.info("direct pump.fun check couldn't run (%s); trying again next trade", e)
            return False
        if not isinstance(res, dict):
            return False
        err = res.get("err")
        if not err:
            self.native_state[side] = True
            log.info("direct pump.fun %ss checked: on", side)
            self._notify(f"✅ Direct pump.fun {side}s passed their check: they now go straight "
                         "to pump.fun (no PumpPortal transactions).")
            return True
        why = explain_failure(err, res.get("logs"))
        if why.startswith(("the price moved", "the token graduated", "not enough SOL")):
            log.info("direct pump.fun check inconclusive (%s); trying again next trade", why)
            return False  # the trade itself, not how it's built: check again next time
        self.native_state[side] = False
        logs = [x for x in res.get("logs") or [] if isinstance(x, str)][-6:]
        log.error("direct pump.fun %s failed its check: %s | %s", side, err, " | ".join(logs))
        self._notify(f"⚠️ Direct pump.fun {side}s failed their check ({why}). {side.title()}s "
                     "use Jupiter instead until the bot restarts. Send this to whoever "
                     "maintains the bot.")
        return False

    def _notify(self, text: str) -> None:
        if self.notice is not None:
            try:
                self.notice(text)
            except Exception as e:
                log.debug("notice failed: %s", e)

    def _without_untrusted(self, built: VersionedTransaction, max_sol_out: float, side: str,
                           swap_sol: float = 0.0, mint: Optional[str] = None) -> Optional[bytes]:
        """PumpPortal's trade with its calls to untrusted programs removed, if what's left is
        the plain pump.fun trade and passes the whole guard again; else None."""
        stripped = strip_untrusted(built, frozenset(self.cfg.extra_allowed_programs), side)
        if stripped is None:
            return None
        try:
            self._guard(stripped, max_sol_out, side, swap_sol, mint)
        except UnsafeTransaction as e:
            log.warning("PumpPortal's %s for %s is unsafe even without the extra program: %s",
                        side, mint, e)
            return None
        return bytes(stripped)

    async def _jupiter_if_refused(self, unsigned: bytes, cand: Candidate, sol: float) -> bytes:
        """PumpPortal's buy, unless the guard refuses it. If it only adds a call to a program
        this bot doesn't trust, that call is removed and the plain pump.fun buy is used (it
        passes the whole guard again). Otherwise the buy is built through Jupiter. Nothing
        the guard refused is ever signed."""
        try:
            built = VersionedTransaction.from_bytes(unsigned)
        except Exception:
            return unsigned  # not a transaction at all: _submit reports it
        try:
            self._guard(built, self._buy_cap(sol), "buy", sol, cand.mint)
            return unsigned
        except UnsafeTransaction as e:
            refused = e
        clean = self._without_untrusted(built, self._buy_cap(sol), "buy", sol, cand.mint)
        if clean is not None:
            log.info("removed an untrusted program from PumpPortal's buy for %s (%s). Its "
                     "calls: %s", cand.mint, refused, describe(built))
            return clean
        self.native_why = "turned off after a failed check" if self.native_ok is False else ""
        native = await self._native("buy", lambda: self._native_buy(cand, sol))
        if native is not None:
            log.info("PumpPortal's buy for %s refused (%s); built it directly instead",
                     cand.mint, refused)
            return native
        log.warning("refused PumpPortal's buy for %s (%s); buying through Jupiter. Its "
                    "calls: %s", cand.mint, refused, describe(built))
        try:
            q = await self.jupiter.quote(SOL_MINT, cand.mint, sol, self._slip(cand))
            return await self.jupiter.swap_tx(q, self.pubkey, await self.sender.priority_fee())
        except Exception as e:
            direct = f"; direct pump.fun buy: {self.native_why}" if self.native_why else ""
            raise NotSent(f"🛡 refused PumpPortal's transaction ({refused}){direct}; and Jupiter "
                          f"couldn't build the buy either ({str(e)[:120]})") from e

    async def buy(self, cand: Candidate, sol: float, curve: Optional[CurveState]) -> Fill:
        t_build = time.monotonic()
        ready = await self._prebuilt(cand, sol)
        if ready is not None:
            unsigned, pre = ready
        else:
            # Tokens of this mint already in the wallet (e.g. a written-off bag) must never be
            # mistaken for this buy's fill. Read alongside building the tx: no added latency.
            pre_task = asyncio.ensure_future(self._held_or_none(cand.mint))
            try:
                if cand.route == "pump":
                    unsigned = await self._pump_buy_tx(cand, sol)
                else:
                    q = await self.jupiter.quote(SOL_MINT, cand.mint, sol, self._slip(cand))
                    unsigned = await self.jupiter.swap_tx(q, self.pubkey,
                                                          await self.sender.priority_fee())
            except BaseException:
                pre_task.cancel()
                raise
            pre = await pre_task
        if cand.route == "pump":
            unsigned = await self._jupiter_if_refused(unsigned, cand, sol)
        timings = {"build": time.monotonic() - t_build}  # ~0 when it was built ahead
        base = pre or 0.0
        try:
            fill = await self._submit(unsigned, cand.mint, "buy", self._buy_cap(sol), sol,
                                      timings=timings)
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
                   value_sol: Optional[float] = None, urgent: bool = False) -> Fill:
        t_build = time.monotonic()
        slippage = self.cfg.trading.slippage_pct if slippage_pct is None else slippage_pct
        # A full pump.fun exit ("100%") doesn't depend on the balance: build it while the
        # balance is read, so stop-loss and dev-dump exits don't wait on two round trips.
        early = None
        if pump and sell_all and not self.native_state["sell"]:
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
        async def native_sell() -> bytes:
            return await self.native.sell_tx(self.kp.pubkey(), mint, sell_raw, slippage,
                                             await self.sender.priority_fee())
        if pump and self.native_state["sell"]:
            unsigned = await self._native("sell", native_sell)
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
                clean = self._without_untrusted(VersionedTransaction.from_bytes(unsigned),
                                                self._sell_cap(value_sol), "sell")
                if clean is None:
                    clean = await self._native("sell", native_sell)
                if clean is not None:
                    log.info("PumpPortal's sell for %s refused (%s); using a clean one", mint, e)
                else:
                    log.error("refused PumpPortal's sell for %s (%s); using Jupiter. Its "
                              "calls: %s", mint, e, describe(VersionedTransaction.from_bytes(unsigned)))
                unsigned = clean
        if pump and unsigned is None and self.native_state["sell"] is None:
            unsigned = await self._native("sell", native_sell)  # PumpPortal couldn't build it
        if unsigned is None:
            q = await self.jupiter.quote(mint, SOL_MINT, tokens, slippage, raw_amount=sell_raw)
            unsigned = await self.jupiter.swap_tx(q, self.pubkey, await self.sender.priority_fee())
        fill = await self._submit(unsigned, mint, "sell", self._sell_cap(value_sol), urgent=urgent,
                                  timings={"build": time.monotonic() - t_build})
        if fill.tokens <= 0:
            fill.tokens = tokens
        fill.emptied = sell_all  # incl. a partial that asked for more than the wallet held
        return fill

    async def quote_sell(self, mint: str, tokens: float) -> Optional[float]:
        q = await self.jupiter.quote(mint, SOL_MINT, tokens, self.cfg.trading.slippage_pct,
                                     urgent=False)  # only prices the position
        return await self.jupiter.out_ui(q)
