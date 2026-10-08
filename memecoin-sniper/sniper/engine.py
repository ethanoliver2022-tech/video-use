"""Wires scanners -> filters -> risk -> execution -> exit monitoring, plus copy trading,
persistence and the Telegram interface."""
from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import re
import time
from collections import Counter, deque
from typing import Optional

import httpx

from . import exits
from .config import Config, CopyWallet, ExitConfig
from .execution.executors import (BuyUncertain, CurveState, Executor, Jupiter, LiveExecutor,
                                  NothingToSell, NotLanded, PaperExecutor)
from .execution.sender import TxSender
from .execution.wallet import WalletManager, transfer_tx
from .health import HealthWatch
from .calls import CallGroup, CallWatcher
from .copywatch import CopyPoller
from .walletcheck import WalletChecker
from .devcheck import DevChecker
from .intel import EarlyFlow
from .models import PUMP_TOTAL_SUPPLY, SOL_MINT, Candidate, Position, num
from .notify import Notifier
from .pump_curve import fetch_curve, is_standard
from .safety import SafetyChecker
from .scanners.momentum import MomentumScanner, MomentumSignal, age_text
from .scanners.multichain import DexScreenerScanner, GeckoTerminalScanner
from .scanners.pumpportal import PumpPortalStream, trade_price
from .settings import (BY_KEY, apply_overrides, apply_setting, format_value, parse_value,
                       to_storable, update_in_place)
from .solana_rpc import ReadPool, RpcError, SolanaRpc, TxFailed
from .store import Store

log = logging.getLogger("sniper")
esc = html.escape

PRICE_POLL_SECONDS = 2
QUOTE_GAP_SECONDS = 5        # poll on-chain / Jupiter when the trade stream has been quiet this long
JUPITER_PRICE_SECONDS = 6    # at most one Jupiter price quote per position this often (rate limits)
SELL_BACKOFF_MAX = 60        # seconds between retries of a failing sell
SELL_FAST_RETRIES = 10       # the first retries come quickly (2, 4, 8, 10, 10... seconds)
MAX_SELL_SLIPPAGE = 50.0     # failed sells retry with more slippage, up to this
DANGER_EXITS = ("dev sold", "copied wallet sold", "stop loss")
WRITE_OFF_AFTER = 40         # failed sells (with backoff, ~30+ min) before giving a position up
ALERTS_PER_HOUR = 20         # cap on "other chain" alerts so Telegram never gets flooded
TRACKER_ALERTS_PER_HOUR = 60 # cap on wallet-tracker alerts
SCAN_ALERTS_PER_HOUR = 60    # scan-only mode: "passes filters" messages
SEEN_TTL = 3 * 3600          # forget candidates after this long (memory stays flat 24/7)
CURVE_TTL = 15 * 60          # forget bonding-curve snapshots of tokens we don't hold
ATA_RENT_SOL = 0.0025        # rent for a new token account, roughly
RENT_BACK_TIMEOUT = 30       # closing the empty account after a full exit
ORDER_POLL_SECONDS = 3
MAX_OPEN_ORDERS = 20
CURVE_MISSES_BEFORE_MIGRATED = 5  # consecutive "no curve account" reads before giving up on it
MIN_RENT_LAMPORTS = 890_880       # a SOL account can't be left between 0 and this
PENDING_BUY_WINDOW = 180          # seconds an unconfirmed buy is watched (> blockhash lifetime)
RECONCILE_SECONDS = 5
CLOCK_JUMP_SECONDS = 30  # wall clock moved this much more than real elapsed time
# candidates are handled concurrently: a confirmation window (e.g. 6s) waits inside a worker,
# so there must be enough of them for a launch burst (they're cheap coroutines)
CANDIDATE_WORKERS = 32
BALANCE_REFRESH_SECONDS = 2   # live wallet balance kept fresh in the background
BALANCE_CACHE_SECONDS = 3     # a buy uses it if no newer than this (and no buy finished since)
PAPER_CURVE_MAX_AGE = 10  # seconds a cached bonding curve may price a paper fill
# A live trade fills about this long after the decision (build, send, confirm: see the
# speed stats), and on a fresh launch other snipers move the price in that time. Paper waits
# the same and prices the fill off the curve as it is then, not as it was at the decision.
PAPER_BUY_DELAY = 1.0
PAPER_SELL_DELAY = 0.8
ESTIMATE_HAIRCUT = 0.97  # unrecorded fills: last price minus typical fees/impact
ROUTE_WAIT_SECONDS = 30  # a just-graduated coin may wait this long for Jupiter to route it
ROUTE_POLL_SECONDS = 4
MAX_QUEUE_WAIT = 60      # seconds a launch may wait for a free worker before it's too late
DEV_CHECK_STREAM_SECONDS = 6  # dev balance check per position while the trade feed is live
FEED_RETRY_SECONDS = 300      # re-ask PumpPortal for a refused trade feed this often
KEEPALIVE_SECONDS = 120  # idle connections kept open this long
WARM_SECONDS = 20        # each trading host gets a keep-alive ping about this often (live)
DEVCHECK_LEAD = 3.0      # with a window: start the dev wallet lookups this long before it ends
COPIED_TTL = 7 * 86400  # 'first buy only' remembers a copied coin this long
BACKGROUND_RPS = 4.0    # pace of background RPC reads (copy watcher, wallet checks)
SOL_PRICE_TTL = 60.0     # seconds a SOL/USD price is reused for $ market cap limits
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PREBUILD_LEAD = 0.5      # with a confirmation window: build the buy this long before it ends
USER_SOURCES = ("manual", "limit")  # user-initiated buys: allowed while auto-sniping is paused


def _finite(*values: float) -> None:
    import math
    for v in values:
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("please send a normal number")


def _boottime() -> Optional[float]:
    """Seconds on a clock that keeps counting while the machine sleeps (Linux BOOTTIME, macOS
    MONOTONIC), or None where there's no such clock: then clock steps aren't corrected,
    rather than a sleep being mistaken for one."""
    import sys
    for name in ("CLOCK_BOOTTIME",) + (("CLOCK_MONOTONIC",) if sys.platform == "darwin" else ()):
        clock = getattr(time, name, None)
        if clock is not None:
            try:
                return time.clock_gettime(clock)
            except OSError:
                pass
    return None


def solana_address(text: str, what: str = "address") -> str:
    """`text` if it's a valid Solana address, else a clear error (not the library's
    "String is the wrong size")."""
    from solders.pubkey import Pubkey
    try:
        Pubkey.from_string(text)
    except (ValueError, TypeError):
        n = len(text or "")
        raise ValueError(
            f"That's not a valid Solana {what}: it should be 32-44 letters and numbers, copied in "
            f"full (yours has {n} characters). Use the copy button in your wallet, and check "
            "nothing was cut off or added.") from None
    return text


def keyword_hit(text: str, keywords: list[str]) -> Optional[str]:
    """Words of 4+ letters match anywhere ("trump" in "TRUMP2028"); shorter ones must be a
    whole word, so "ai" matches "AI Agent" but not "pain" or "daisy"."""
    import re
    low = text.lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        if (len(k) >= 4 and k in low) or k in words:
            return kw.strip()
    return None


class Engine:
    def __init__(self, cfg: Config, live: bool = False, scan_only: bool = False,
                 config_path: Optional[str] = None, start_paused: bool = False):
        self.cfg, self.live, self.scan_only = cfg, live, scan_only
        self.config_path = config_path
        self.mode = "live" if live else "paper"
        # connections stay open between snipes (httpx's default drops them after 5s idle, so
        # every buy would pay a fresh TCP + TLS handshake to PumpPortal and each Jito region)
        self.http = httpx.AsyncClient(timeout=15, headers={"user-agent": "memecoin-sniper/0.4"},
                                      limits=httpx.Limits(max_connections=200,
                                                          max_keepalive_connections=100,
                                                          keepalive_expiry=KEEPALIVE_SECONDS))
        self.rpc = SolanaRpc(cfg.endpoints.rpc_url, self.http)
        self.jupiter = Jupiter(cfg.endpoints.jupiter_api, self.rpc, self.http, cfg.jupiter_api_key)
        self.store = Store(cfg.data_dir, self.mode)
        self.wallet = WalletManager(cfg.data_dir, cfg.private_key)
        for old in ("copytrade.max_market_cap_sol", "calls.max_market_cap_sol"):
            self.store.drop_override(old)   # now set in $ (copytrade/calls.max_market_cap_usd)
        bad = apply_overrides(cfg, self.store.overrides())  # settings changed from Telegram
        if bad:
            log.warning("ignoring invalid saved settings: %s", ", ".join(bad))
        self.safety = SafetyChecker(cfg.filters, self.rpc, self.http, cfg.endpoints.rugcheck_api,
                                    store=self.store, jupiter=self.jupiter,
                                    ipfs_gateway=cfg.endpoints.ipfs_gateway,
                                    probe_sol=cfg.trading.buy_amount_sol)
        # background reads go over the extra RPCs first (main last), paced, so trading keeps
        # the main RPC's rate limit to itself
        extra = [SolanaRpc(u, self.http) for u in dict.fromkeys(cfg.speed.broadcast_rpcs)
                 if u.startswith(("http://", "https://")) and u != cfg.endpoints.rpc_url]
        self.reads = ReadPool([*extra, self.rpc], rate=BACKGROUND_RPS)
        self.quick_reads = ReadPool([*extra, self.rpc])      # dev checks: before a buy, no queue
        self.devcheck = DevChecker(cfg.filters, self.quick_reads, self.store)
        self.calls = CallWatcher(cfg.data_dir, self.store, self.on_call)
        self.notifier = Notifier(self.http, cfg.telegram_bot_token if cfg.notify.telegram else "",
                                 cfg.telegram_chat_id)
        self.own_wallet = ""
        self.executor: Executor = self._build_executor(live)

        d = cfg.discovery
        self.stream = PumpPortalStream(cfg.endpoints.pumpportal_ws, d.pumpfun_new_tokens,
                                       d.pumpfun_migrations, cfg.pumpportal_api_key)
        self.stream.on_candidate = self.on_candidate
        self.stream.on_trade = self.on_trade
        self.stream.on_migration = self.on_migration
        self.stream.on_feed_change = self._feed_changed

        self.queue: asyncio.Queue[Candidate] = asyncio.Queue(maxsize=500)
        self.outcomes: deque[tuple[float, str]] = deque(maxlen=20_000)  # (time, reason): /why
        self.outcomes_seen: deque[float] = deque(maxlen=10_000)
        # launches that cleared the confirmation window but one check (for /why)
        self.near_misses: deque[tuple[float, str]] = deque(maxlen=2_000)
        self.why_reset_at = 0.0
        self.seen: dict[tuple, float] = {}
        self.positions: dict[str, Position] = {}
        self.curves: dict[str, CurveState] = {}
        self._curve_ts: dict[str, float] = {}
        self.flows: dict[str, EarlyFlow] = {}
        self.sell_locks: dict[str, asyncio.Lock] = {}
        self._rent_note: dict[str, float] = {}  # rent taken back on a close, for its message
        self.sell_failures: dict[str, int] = {}
        self._sell_next_try: dict[str, float] = {}
        self._buying: dict[str, float] = {}       # mint -> SOL, buys in flight
        self._copy_buying: set[str] = set()       # ...of which copies (separate copy limit)
        self._buys_finished = 0                   # bumps when a buy leaves _buying
        # (SOL, monotonic time, _buys_finished when read): the wallet balance, refreshed in
        # the background so buys don't wait on an RPC round trip
        self._bal_cache: Optional[tuple[float, float, int]] = None
        self.buy_lock = asyncio.Lock()
        self.last_loss_at: Optional[float] = None  # monotonic clock
        self._recent_sigs: deque[str] = deque(maxlen=5000)
        self._recent_sig_set: set[str] = set()
        self._alerts: deque[float] = deque()
        self._tracker_alerts: deque[float] = deque()
        self._copy_misses: deque[float] = deque()
        # copied / tracked wallet -> (time, "pumpportal" | "rpc", buy/sell, mint) of the last
        # trade seen from it: shows in the copy menu whether their trades reach the bot
        self.copy_seen: dict[str, tuple[float, str, str, str]] = {}
        try:   # coins already copied once ('first buy only')
            self._copied: dict[str, float] = {
                k: float(v) for k, v in json.loads(self.store.get_setting("copied_mints") or "{}").items()}
        except (ValueError, TypeError, AttributeError):
            self._copied = {}
        self.wallet_checker = WalletChecker(self.reads, self.store)
        self.copy_poller = CopyPoller(self.reads, lambda: list(self._copy), self.on_trade,
                                      enabled=lambda: self.cfg.copytrade.rpc_watch,
                                      curve=lambda m: fetch_curve(self.rpc, m),
                                      known=lambda sig: sig in self._recent_sig_set,
                                      # only a backup while PumpPortal's feed is live
                                      interval=lambda: 6.0 if self.has_trade_stream else 2.0)
        self._sol_usd: tuple[float, float] = (0.0, 0.0)   # (price, when) for $ market caps
        self._scan_alerts: deque[float] = deque()
        self._bg: set[asyncio.Task] = set()
        self._orders_running: set[int] = set()
        self._curve_misses: dict[str, int] = {}
        self._quoted_at: dict[str, float] = {}  # mint -> monotonic time of its last price quote
        self._dev_checked: dict[str, float] = {}  # mint -> monotonic time of its last dev check
        self._momentum_seen: dict[str, float] = {}  # mint -> monotonic time it was last signalled
        self._momentum_alerts: deque[float] = deque()

        saved_pause = self.store.get_setting("paused")
        self.paused = start_paused if saved_pause is None else saved_pause == "1"
        self._copy: dict[str, CopyWallet] = {}
        self._load_copy_wallets()
        self.telegram_ui = False  # set by `sniper bot`: Telegram is the whole interface
        self.health = HealthWatch(lambda text, ok: self._spawn(
            self.notifier.send(text, logging.INFO if ok else logging.WARNING)))

    @property
    def kol_wallets(self) -> set[str]:
        return set(self.cfg.exits.kol_wallets)

    @property
    def has_trade_stream(self) -> bool:
        """The live trade feed can be relied on (a key, and PumpPortal delivering it)."""
        return self.stream.trades_live

    @property
    def has_pumpportal_key(self) -> bool:
        return self.stream.trades_enabled

    def _feed_changed(self, ok: bool, error: str) -> None:
        """PumpPortal started refusing (or delivering again) the paid trade feed."""
        if ok:
            msg = "✅ PumpPortal's live trade feed is working again: instant exits are back on."
        else:
            msg = (f"⚠️ PumpPortal refused the live trade feed ({esc(error)}). The bot switched to "
                   "on-chain checks (slower exits) until it's fixed. Usually the wallet linked to "
                   "your PumpPortal API key needs topping up (minimum 0.02 SOL). It retries every "
                   "5 minutes on its own.")
        self._spawn(self.notifier.send(msg, logging.INFO if ok else logging.WARNING))

    async def _health_watch(self) -> None:
        from . import health
        await health.watch(self)

    async def feed_watch(self) -> None:
        """While PumpPortal refuses the trade feed, ask again every few minutes."""
        while True:
            await asyncio.sleep(FEED_RETRY_SECONDS)
            if self.stream.trades_enabled and not self.stream.feed_ok:
                await self.stream.resubscribe()

    def _build_executor(self, live: bool) -> Executor:
        self._bal_cache = None  # belongs to the previous wallet / mode
        if not live:
            self.own_wallet = ""
            return PaperExecutor(self.jupiter, self.cfg.trading.slippage_pct, self.cfg)
        kp = self.wallet.keypair()
        if kp is None:
            raise ValueError("no wallet yet — create or import one first")
        self.own_wallet = str(kp.pubkey())
        sender = TxSender(self.cfg.speed, self.rpc, self.http,
                          lambda: self.cfg.trading.priority_fee_sol)  # follows setting changes
        ex = LiveExecutor(self.cfg, kp, self.rpc, self.jupiter, self.http, sender)
        ex.notice = lambda text: self._spawn(self.notifier.send(text))
        return ex

    def _load_copy_wallets(self) -> None:
        """Copy-mode wallets only while copy trading is on; alert-mode (tracked) wallets always."""
        self._copy.clear()
        enabled = self.cfg.copytrade.enabled
        removed = self._removed()
        for w in self.cfg.copytrade.wallets:
            if w.address not in removed and (enabled or w.mode == "alert"):  # not removed in chat
                self._copy[w.address] = w
        for w in self.store.copy_wallets():  # added from Telegram
            if enabled or w["mode"] == "alert":
                self._copy[w["address"]] = CopyWallet(**w)

    def _set_curve(self, mint: str, curve: CurveState) -> None:
        if not is_standard(curve.v_sol, curve.v_tokens):
            # not a SOL bonding curve (a PumpSwap pool, a coin priced in USDC...): its
            # numbers aren't SOL and tokens, so they must never price a fill or an exit
            self.curves.pop(mint, None)
            return
        self.curves[mint] = curve
        self._curve_ts[mint] = time.time()

    # ---------- discovery ----------

    async def on_candidate(self, c: Candidate) -> None:
        if c.source == "pumpfun" and c.creator:
            self.store.record_launch(c.mint, c.creator)  # reputation keeps learning while paused
        # a graduation is a new event for a mint we saw launch: dedup it separately
        key = (c.chain, c.mint) + (("migration",) if c.source == "pumpfun-migration" else ())
        if key in self.seen:
            return
        self.seen[key] = time.time()
        if c.age_seconds > self.cfg.discovery.max_candidate_age_seconds:
            return
        if (c.source == "pumpfun-migration" and self.cfg.exits.sell_on_migration
                and c.mint in self.positions):
            return  # we're exiting this one *because* it migrated: don't buy it straight back
        if self.paused and not self.scan_only:
            return  # don't spend RPC calls (or Telegram alerts) while paused
        if not self._targeted(c):
            return
        if c.v_sol and c.v_tokens:
            self._set_curve(c.mint, CurveState(c.v_sol, c.v_tokens))
        c.queued_at = time.monotonic()
        try:
            self.queue.put_nowait(c)
        except asyncio.QueueFull:
            log.debug("candidate queue full, dropping %s", c.mint)

    def _targeted(self, c: Candidate) -> bool:
        """Apply the snipe mode. Tags dev-watchlist and keyword hits on the candidate."""
        d = self.cfg.discovery
        if c.chain != "solana":
            return True  # other chains are alerts only; notify.alert_other_chains decides
        if c.source == "pumpfun" and c.creator and c.creator in set(d.dev_watchlist):
            c.trigger = "dev"
            c.force = d.dev_snipe_skip_filters
            c.buy_sol = d.dev_snipe_sol or None
            return d.auto_snipe != "off"
        kw = keyword_hit(f"{c.name} {c.symbol}", d.snipe_keywords)
        if kw:
            c.trigger = f"keyword:{kw}"
            return d.auto_snipe != "off"
        return d.auto_snipe == "all"

    async def worker(self) -> None:
        while True:
            c = await self.queue.get()
            try:
                waited = time.monotonic() - c.queued_at if c.queued_at else 0.0
                if waited > MAX_QUEUE_WAIT and c.source not in USER_SOURCES:
                    log.debug("dropping %s: waited %.0fs in a backed-up queue", c.mint, waited)
                    continue  # a snipe minutes late is no snipe
                self._note_outcome(c, await self.handle_candidate(c))
            except Exception:
                log.exception("error handling %s", c.mint)
            finally:
                self.queue.task_done()

    def _note_outcome(self, c: Candidate, result: Optional[str]) -> None:
        """Remember why each launch was (not) bought, for /why."""
        if c.source in USER_SOURCES or not result:
            return
        now = time.time()
        if result.startswith("🟢"):
            reasons = ["bought"]
        elif result.startswith("❌"):
            body = result.split(":", 1)[1] if ":" in result else result
            reasons = [r.strip() for r in html.unescape(body).split(";") if r.strip()]
        else:
            reasons = [html.unescape(result).strip()]
        keys = [self._reason_key(r) for r in reasons]
        for k in keys[:3]:
            self.outcomes.append((now, k))
        self.outcomes_seen.append(now)
        # one failed check after watching the early trades, or only the dev wallet: the
        # launches your settings came closest to buying
        if (result.startswith("❌ confirmation failed") and len(keys) == 1) \
                or result.startswith("❌ dev check"):
            for k in keys[:3]:
                self.near_misses.append((now, k))

    @staticmethod
    def _reason_key(r: str) -> str:
        """Group reasons that differ only by numbers or a wallet: "dev bought 9.1%" and
        "dev bought 12.0%" count as one."""
        r = re.sub(r"\s*\([1-9A-HJ-NP-Za-km-z]{3,}…\)", "", r)  # "(BwWK7f…)" wallet tags
        if r.startswith("socials reused"):
            r = r.split(":", 1)[0]          # the links differ per launch: one line for all
        return re.sub(r"\d+(?:[.,]\d+)*", "#", r)[:90]

    def reset_why(self) -> None:
        """Start the /why counts over, e.g. right after changing a filter."""
        self.outcomes.clear()
        self.outcomes_seen.clear()
        self.near_misses.clear()
        self.why_reset_at = time.time()

    def why_summary(self, minutes: int = 60) -> str:
        """What happened to the launches of the last `minutes`: bought, or why not."""
        now = time.time()
        since = now - minutes * 60
        span = f"Last {minutes} min"
        if self.why_reset_at > since:   # counting started over more recently than that
            since = self.why_reset_at
            span = f"Since reset ({max(0, now - since) / 60:.0f} min ago)"
        seen = sum(1 for t in self.outcomes_seen if t >= since)
        counts = Counter(r for t, r in self.outcomes if t >= since)
        if not seen:
            return (f"🔎 {span}: no launches handled yet. "
                    + ("The bot is paused: tap ▶️ Start sniping." if self.paused
                       else "Give it a few minutes, or check 🩺 Health: is the PumpPortal "
                            "feed connected?"))
        bought = counts.pop("bought", 0)
        lines = [f"🔎 <b>{span}</b>: {seen} launches looked at, "
                 f"{bought} bought.", "", "Top reasons for not buying:"]
        lines += [f"  {n:>4} × {html.escape(r)}" for r, n in counts.most_common(10)]
        near = Counter(r for t, r in self.near_misses if t >= since)
        if near:
            lines += ["", "Closest calls (passed everything else):"]
            lines += [f"  {n:>4} × {html.escape(r)}" for r, n in near.most_common(5)]
        elif not bought and self.cfg.entry.confirm_seconds > 0:
            lines += ["", "No launch got within one check of a buy."]
        return "\n".join(lines)

    async def handle_candidate(self, c: Candidate) -> Optional[str]:
        """Filter, confirm and buy. Returns a human-readable outcome."""
        try:
            return await self._handle_candidate(c)
        finally:
            if c.prebuilt is not None and not c.prebuilt.done():
                c.prebuilt.cancel()  # rejected or skipped: the built transaction is never sent
            if c.mint in self.flows:   # left before its window (confirm_flow clears its own)
                await self._drop_flow(c)

    async def _handle_candidate(self, c: Candidate) -> Optional[str]:
        if not c.queued_at:  # copy trades, manual and momentum buys: speed is timed from here
            c.queued_at = time.monotonic()
        tag = f"[{c.chain}] {c.symbol or '?'} {c.mint}"
        if self.paused and c.source not in USER_SOURCES and not self.scan_only:
            return "paused"
        notes = ""
        if not c.force:
            # no confirmation window ahead: build the buy now, while the slower filters run
            # (metadata / socials, on-chain lookups), if the instant checks already pass
            windowed = c.source == "pumpfun" and self.cfg.entry.confirm_seconds > 0
            if self.live and not windowed and c.route == "pump" and self.safety.quick_check(c):
                self._start_prebuild(c)
            if windowed and self.has_trade_stream and self.safety.quick_check(c):
                # watch its trades from now, not after the slower filters: who bought at
                # launch is the bundle check, and those buys happen in the first second
                await self._open_flow(c)
            report = await self.safety.evaluate(c)
            if not report.passed:
                log.debug("reject %s: %s", tag, "; ".join(report.reasons))
                return "❌ rejected: " + esc("; ".join(report.reasons))
            notes = "; ".join(report.notes)
        if c.chain != "solana":
            if self.cfg.notify.alert_other_chains and self._alert_allowed():
                liq = f" liq ${c.liquidity_usd:,.0f}" if c.liquidity_usd else ""
                await self.notifier.send(f"👀 {esc(tag)}{liq} via {c.source} — {esc(c.url or '')}")
            return "alert only (not Solana)"
        if self.scan_only:
            if self._alert_allowed(self._scan_alerts, SCAN_ALERTS_PER_HOUR):
                self._spawn(self.notifier.send(
                    f"✅ passes filters: {esc(tag)} ({esc(notes)}) {esc(c.url or '')}"))
            return "passes filters"
        if c.source == "pumpfun" and self.cfg.entry.confirm_seconds > 0 and not c.force:
            problems = await self.confirm_flow(c)
            if problems:
                log.debug("skip %s after confirmation: %s", tag, "; ".join(problems))
                return "❌ confirmation failed: " + esc("; ".join(problems))
            notes += "; early flow confirmed"
            problems = await self.dev_problems(c)
            if problems:
                result = "❌ dev check: " + esc("; ".join(problems))
            else:
                result = await self.try_buy(c, notes)
            pos = self.positions.get(c.mint)
            if not pos or pos.closed:  # skipped/failed after watching: stop the (billed) feed
                await self.stream.unwatch_token(c.mint)
            return result
        problems = await self.dev_problems(c)
        if problems:
            return "❌ dev check: " + esc("; ".join(problems))
        if c.source == "pumpfun-migration" and not await self._route_ready(c):
            return "❌ rejected: no trading route yet after graduating"
        return await self.try_buy(c, notes)

    async def _route_ready(self, c: Candidate) -> bool:
        """A coin that just graduated off pump.fun trades in a new PumpSwap pool, and Jupiter
        (which prices and buys it) needs a few seconds to a minute to pick that pool up.
        Wait for a quote rather than failing the buy straight away."""
        from .execution.executors import JupiterBusy
        deadline = time.monotonic() + ROUTE_WAIT_SECONDS
        while True:
            try:
                await self.jupiter.quote(SOL_MINT, c.mint, self.cfg.trading.buy_amount_sol,
                                         self.cfg.trading.slippage_pct, urgent=False)
                return True
            except JupiterBusy:
                pass  # rate budget in use by trades: try again shortly
            except Exception as e:
                log.debug("no route for %s yet: %s", c.mint, e)
            if time.monotonic() + ROUTE_POLL_SECONDS > deadline:
                return False
            await asyncio.sleep(ROUTE_POLL_SECONDS)

    def _alert_allowed(self, q: Optional[deque] = None, per_hour: int = ALERTS_PER_HOUR) -> bool:
        q = self._alerts if q is None else q
        now = time.monotonic()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return False
        q.append(now)
        return True

    async def _window(self, c: Candidate, flow: Optional[EarlyFlow] = None) -> None:
        """Wait out the confirmation window, building the buy shortly before it ends so a
        launch that passes is bought without waiting on PumpPortal. With the trade stream,
        only if the early flow still looks clean (most launches don't: no wasted builds)."""
        wait = self.cfg.entry.confirm_seconds
        c.window_s = wait  # a deliberate wait: not counted as the bot being slow
        dev_lead = min(DEVCHECK_LEAD, wait)
        lead = min(PREBUILD_LEAD, dev_lead)
        await asyncio.sleep(wait - dev_lead)
        # the dev wallet lookups take a few RPC calls: only for launches still looking clean
        if flow is None or not flow.evaluate(self.cfg.entry):
            self._start_devcheck(c)
        await asyncio.sleep(dev_lead - lead)
        if flow is not None and not flow.evaluate(self.cfg.entry):
            self._start_prebuild(c)
        await asyncio.sleep(lead)

    def _devcheck_applies(self, c: Candidate) -> bool:
        return (c.chain == "solana" and bool(c.creator) and not c.force and c.trigger != "dev"
                and c.source in ("pumpfun", "pumpfun-migration", "call") and self.devcheck.enabled)

    def _start_devcheck(self, c: Candidate) -> None:
        if c.dev_check is None and self._devcheck_applies(c):
            task = asyncio.ensure_future(self.devcheck.check(c.creator))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            c.dev_check = task

    async def dev_problems(self, c: Candidate) -> list[str]:
        """Dev wallet age / funding / recent curve sells (started early when possible)."""
        self._start_devcheck(c)
        if c.dev_check is None:
            return []
        try:
            return await c.dev_check
        except Exception as e:
            log.debug("dev check failed: %s", e)
            return (["dev wallet check failed"]
                    if self.cfg.filters.dev_check_on_error == "skip" else [])

    def _start_prebuild(self, c: Candidate) -> None:
        """Live pump.fun buys: have PumpPortal build the transaction while the filters (or the
        confirmation window) still run. It's only signed and sent if the token passes."""
        prepare = getattr(self.executor, "prepare_buy", None)
        if (not self.live or prepare is None or c.prebuilt is not None or c.chain != "solana"
                or c.route != "pump" or self.scan_only or c.mint in self._buying
                or (c.mint in self.positions and not self.positions[c.mint].closed)):
            return
        if self._slots_used("snipe") >= self.cfg.trading.max_open_positions:
            return  # it couldn't be bought anyway (the same rule the buy itself applies)
        sol = c.buy_sol or self.cfg.trading.buy_amount_sol
        task = asyncio.ensure_future(prepare(c, sol))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # never "unretrieved"
        c.prebuilt, c.prebuilt_sol = task, sol

    async def _open_flow(self, c: Candidate) -> EarlyFlow:
        flow = self.flows.get(c.mint)
        if flow is None:
            flow = self.flows[c.mint] = EarlyFlow(
                creator=c.creator, dev_tokens=c.creator_initial_buy_tokens or 0.0,
                v_tokens=c.v_tokens or 0.0,
                started=time.monotonic() - c.age_seconds)   # "at launch" counts from the launch
            await self.stream.watch_token(c.mint)
        return flow

    async def _drop_flow(self, c: Candidate) -> None:
        """Rejected before its confirmation window: stop the (billed) trade feed for it."""
        if self.flows.pop(c.mint, None) is not None and c.mint not in self.positions:
            await self.stream.unwatch_token(c.mint)

    async def confirm_flow(self, c: Candidate) -> list[str]:
        """Watch the first seconds of trading before committing (bundle / farm detection)."""
        if not self.has_trade_stream:
            return await self._confirm_onchain(c)
        flow = await self._open_flow(c)
        try:
            await self._window(c, flow)
        finally:
            self.flows.pop(c.mint, None)
        problems = flow.evaluate(self.cfg.entry)
        if problems and c.mint not in self.positions:
            await self.stream.unwatch_token(c.mint)
        return problems

    async def _confirm_onchain(self, c: Candidate) -> list[str]:
        """Without PumpPortal's paid trade stream: compare the bonding curve and the dev's
        balance before and after the window. Can't count unique buyers or spot bundles."""
        start_sol = c.v_sol
        c.window_s = self.cfg.entry.confirm_seconds
        await asyncio.sleep(self.cfg.entry.confirm_seconds)
        try:
            curve = await fetch_curve(self.rpc, c.mint)
        except Exception as e:
            return [f"could not read bonding curve: {e}"]
        if not curve:
            return ["bonding curve not found"]
        if curve.complete:
            return ["already graduated"]
        if not getattr(curve, "sol_quoted", True) or not is_standard(curve.v_sol, curve.v_tokens):
            return ["not a standard SOL pump.fun coin"]
        self._set_curve(c.mint, CurveState(curve.v_sol, curve.v_tokens))
        problems = []
        if start_sol is not None:
            inflow = curve.v_sol - start_sol
            # must exceed min_net_flow_sol, and be positive: without the stream we can't
            # count buyers, so some inflow is the only sign anyone is buying at all
            if inflow <= max(0.0, self.cfg.entry.min_net_flow_sol):
                problems.append(f"net flow {inflow:+.2f} SOL in {self.cfg.entry.confirm_seconds:g}s")
        mcap = curve.price * PUMP_TOTAL_SUPPLY
        if self.cfg.entry.max_market_cap_sol and mcap > self.cfg.entry.max_market_cap_sol:
            problems.append(f"market cap already {mcap:.0f} SOL")
        if not problems:
            self._start_prebuild(c)  # built while the dev's balance is read
        if c.creator and c.creator_initial_buy_tokens:
            try:
                dev = await self.rpc.get_token_balance(c.creator, c.mint)
                if dev < c.creator_initial_buy_tokens * 0.99:
                    problems.append("dev sold during confirmation window")
            except Exception as e:
                log.debug("dev balance check failed: %s", e)
        return problems

    # ---------- copy trading ----------

    def copy_wallets(self) -> list[CopyWallet]:
        return list(self._copy.values())

    async def add_copy_wallet(self, address: str, label: str = "", buy_sol: float = 0.0,
                              mode: str = "copy", copy_sells: Optional[bool] = None) -> None:
        solana_address(address, "wallet address")
        if mode not in ("copy", "alert"):
            raise ValueError("mode must be copy or alert")
        if not 0 <= buy_sol <= 100:
            raise ValueError("size per trade must be between 0 and 100 SOL (0 = your buy size)")
        if not self.has_pumpportal_key and not self.cfg.copytrade.rpc_watch:
            raise ValueError("Copy trading and wallet tracking need a PumpPortal API key "
                             "(PUMPPORTAL_API_KEY in .env) or the Backup wallet watcher on.")
        if copy_sells is None:  # keep what the wallet already had (default: follow their sells)
            old = next((x for x in self.store.copy_wallets() if x["address"] == address), None)
            cw = next((x for x in self.cfg.copytrade.wallets if x.address == address), None)
            copy_sells = old["copy_sells"] if old else (cw.copy_sells if cw else True)
        self._set_removed(address, False)
        self.store.add_copy_wallet(address, label, buy_sol, copy_sells, mode)
        if mode == "copy" and not self.cfg.copytrade.enabled:
            await self.set_setting("copytrade.enabled", True)
        self._copy[address] = CopyWallet(address, label, buy_sol, copy_sells, mode)
        await self.stream.watch_accounts([address])

    async def set_wallet_mode(self, address: str, mode: str) -> None:
        w = next((x for x in self.store.copy_wallets() if x["address"] == address), None)
        if not w:  # a wallet from config.yaml: the change is saved as a chat override
            cw = next((x for x in self.cfg.copytrade.wallets if x.address == address), None)
            if not cw:
                raise ValueError("wallet not found")
            w = {"label": cw.label, "buy_sol": cw.buy_sol, "copy_sells": cw.copy_sells}
        await self.add_copy_wallet(address, w["label"], w["buy_sol"], mode, w["copy_sells"])

    def _removed(self) -> set[str]:
        import json
        return set(json.loads(self.store.get_setting("removed_copy_wallets") or "[]"))

    def _set_removed(self, address: str, removed: bool) -> None:
        import json
        r = self._removed()
        r.add(address) if removed else r.discard(address)
        self.store.set_setting("removed_copy_wallets", json.dumps(sorted(r)))

    async def remove_copy_wallet(self, address: str) -> bool:
        found = self._copy.pop(address, None) is not None
        found = self.store.remove_copy_wallet(address) or found
        if any(w.address == address for w in self.cfg.copytrade.wallets):
            self._set_removed(address, True)  # from config.yaml: keep it removed on reload
            found = True
        await self.stream.unwatch_account(address)
        return found

    async def handle_copy(self, msg: dict, leader: CopyWallet) -> None:
        mint = msg["mint"]
        if leader.mode == "alert":
            await self._wallet_alert(msg, leader)
            return
        held = self.positions.get(mint)
        if (msg.get("txType") != "buy" or (held and not held.closed)  # their add-ons: skipped
                or mint in self._buying or self.scan_only):
            return
        ct = self.cfg.copytrade
        name = leader.label or leader.address[:6]
        via = msg.get("via", "pumpportal")
        spent = num(msg.get("solAmount"), allow_zero=True) or 0.0
        got = num(msg.get("tokenAmount"))
        if ct.first_buy_only and self._copied_before(mint):
            log.info("👥 %s bought %s again: already copied once (first buy only)", name, mint)
            return
        if ct.first_buy_only:
            held = await self._held_before(leader.address, mint, got or 0.0, msg)
            if held:
                # they held it before this buy: adding to a position, not entering one
                log.info("👥 %s added to %s (%s): not a first buy", name, mint, held)
                return
        if leader.paused:   # you were told when it paused: no message per buy
            log.info("👥 %s bought %s: wallet paused, not copied", name, mint)
            return
        if self.paused:
            return await self._not_copied(name, mint, "⏸ sniping is paused (tap ▶️ Start)", via)
        min_buy = leader.min_leader_sol if leader.min_leader_sol is not None else ct.min_leader_buy_sol
        if spent < min_buy:
            return await self._not_copied(
                name, mint, f"they only bought {spent:.3f} SOL (min for this wallet is "
                            f"{min_buy:g})", via)
        pool = msg.get("pool")
        pump = pool in (None, "", "pump", "pump-amm")  # pump.fun curve or PumpSwap
        on_curve = pool == "pump"
        v_sol, v_tok = num(msg.get("vSolInBondingCurve")), num(msg.get("vTokensInBondingCurve"))
        cap = leader.max_mcap_usd if leader.max_mcap_usd is not None else ct.max_market_cap_usd
        if cap:
            mcap = num(msg.get("marketCapSol"))
            if mcap is None:
                mcap = await self._market_cap_sol(mint, None)
            over = await self._over_cap(mcap, cap)
            if over:
                return await self._not_copied(name, mint, f"{over} (your copy limit)", via)
        if ct.max_chase_pct and spent > 0 and got:
            # measured from the price right after their buy when PumpPortal reports it: a big
            # buy moves the price itself, and that move is not the crowd piling in
            after = v_sol / v_tok if (on_curve and via != "rpc" and v_sol and v_tok) else None
            chase = await self._chase_pct(mint, after or spent / got, on_curve)
            if chase is not None and chase > ct.max_chase_pct:
                return await self._not_copied(
                    name, mint, f"price is already {chase:+.0f}% above "
                                + ("the price right after their buy" if after else "what they paid")
                                + f" (max {ct.max_chase_pct:g}%)", via)
        size = leader.buy_sol or None
        if leader.size_pct:
            size = spent * leader.size_pct / 100
            if leader.max_sol:
                size = min(size, leader.max_sol)
            size = max(size, 0.001)
        filters = ct.run_safety_checks if leader.filters is None else leader.filters
        sells = leader.sells if leader.copy_sells else "off"
        c = Candidate(chain="solana", mint=mint, source="copy", symbol=mint[:6],
                      route="pump" if pump else "jupiter",  # other venues (LetsBonk...): Jupiter
                      leader=leader.address if sells != "off" else None,
                      leader_mode="mirror" if sells == "mirror" else "all",
                      copied_from=leader.address, slippage_pct=ct.slippage_pct or None,
                      buy_sol=size, force=not filters,
                      v_sol=v_sol if on_curve else None, v_tokens=v_tok if on_curve else None,
                      trigger=f"copy:{name}",
                      url=f"https://pump.fun/coin/{mint}" if pump
                      else f"https://dexscreener.com/solana/{mint}")
        log.info("👥 %s bought %s (%.3f SOL) — copying", name, mint, spent)
        result = await self.handle_candidate(c)
        if result and result.startswith("🟢"):
            self._mark_copied(mint)
        elif result:
            await self._not_copied(name, mint, html.unescape(result), via)

    async def _held_before(self, wallet: str, mint: str, got: float, msg: dict) -> str:
        """Why we think the copied wallet already held this coin before this buy ('' = it
        didn't). Three independent signs, as no single one is always available."""
        sig = msg.get("signature")
        since = time.time() - COPIED_TTL
        for row_sig, row_mint, _ts, side, _sol, _tok in self.store.wallet_trades(wallet, since):
            if row_mint == mint and side == "buy" and row_sig != sig:
                return "it bought this coin before"
        after = num(msg.get("newTokenBalance"), allow_zero=True)
        if got and after is not None and after - got > max(after * 0.01, 1.0):
            return f"it held {after - got:,.0f} tokens before"
        try:   # straight from the chain: catches bags bought before the bot was watching
            bal = await self.rpc.get_token_balance(wallet, mint)
        except Exception as e:
            log.debug("balance of %s in %s unavailable: %s", wallet[:6], mint, e)
            return ""
        # whether or not the RPC already shows this buy, holding clearly more than it means
        # there was a bag already
        if got and bal > got * 1.02 + 1.0:
            return f"it holds {bal:,.0f} tokens, this buy was {got:,.0f}"
        return ""

    async def _chase_pct(self, mint: str, their_price: float, on_curve: bool) -> Optional[float]:
        """How far the price is now above what the copied wallet paid (None = unknown)."""
        try:
            if on_curve or mint.endswith("pump"):
                curve = await fetch_curve(self.rpc, mint)
                if curve and not curve.complete and curve.sol_quoted and curve.price > 0:
                    return (curve.price / their_price - 1) * 100
            return (await self.price_of(mint) / their_price - 1) * 100
        except Exception as e:
            log.debug("chase check for %s skipped: %s", mint, e)
            return None

    def _copied_before(self, mint: str) -> bool:
        t = self._copied.get(mint)
        return t is not None and time.time() - t < COPIED_TTL

    def _mark_copied(self, mint: str) -> None:
        now = time.time()
        self._copied[mint] = now
        self._copied = {m: t for m, t in self._copied.items() if now - t < COPIED_TTL}
        self.store.set_setting("copied_mints", json.dumps(self._copied))

    async def update_wallet(self, address: str, **changes) -> CopyWallet:
        """Change a copied / tracked wallet's own settings (size, sells, filters, pause...)."""
        import dataclasses
        w = self._copy.get(address)
        stored = next((x for x in self.store.copy_wallets() if x["address"] == address), None)
        if stored is None:
            if w is None:
                w = next((x for x in self.cfg.copytrade.wallets if x.address == address), None)
            if w is None:
                raise ValueError("wallet not found")
            stored = dataclasses.asdict(w)
        stored.update(changes)
        if "sells" in changes:
            stored["copy_sells"] = changes["sells"] != "off"
        opts = {k: stored.get(k) for k in self.store.WALLET_OPTS}
        self.store.add_copy_wallet(address, stored.get("label", ""), stored.get("buy_sol", 0.0),
                                   stored.get("copy_sells", True), stored.get("mode", "copy"), opts)
        self._load_copy_wallets()
        if "sells" in changes:   # open copies of this wallet follow the new sell mode
            for p in self.positions.values():
                if p.copied_from == address and not p.closed:
                    p.leader = None if changes["sells"] == "off" else address
                    p.leader_mode = "mirror" if changes["sells"] == "mirror" else "all"
                    self.store.save_position(p)
        cw = self._copy.get(address)
        if cw is None:   # copying off: still report the wallet's settings
            cw = CopyWallet(**{k: v for k, v in stored.items()
                               if k in CopyWallet.__dataclass_fields__})
        return cw

    def wallet_results(self, address: str) -> dict:
        """This wallet's copies: closed count, wins, PnL, today's PnL, losses in a row."""
        closes = [e for e in self.store.events("close") if e.get("copied_from") == address]
        pnls = [float(e.get("pnl_sol", 0)) for e in closes]
        streak = 0
        for p in reversed(pnls):
            if p > 0:
                break
            streak += 1
        midnight = time.time() - (time.time() % 86400)
        today = sum(float(e.get("pnl_sol", 0)) for e in closes if e["ts"] >= midnight)
        return {"n": len(pnls), "wins": sum(1 for p in pnls if p > 0), "pnl": sum(pnls),
                "today": today, "loss_streak": streak}

    async def _check_wallet_pause(self, address: str) -> None:
        """After a copy closes: pause a wallet that keeps losing (if set)."""
        ct = self.cfg.copytrade
        w = self._copy.get(address)
        if w is None or w.paused or not (ct.pause_after_losses or ct.pause_daily_loss_sol):
            return
        r = self.wallet_results(address)
        why = ""
        if ct.pause_after_losses and r["loss_streak"] >= ct.pause_after_losses:
            why = f"{r['loss_streak']} losing copies in a row"
        elif ct.pause_daily_loss_sol and -r["today"] >= ct.pause_daily_loss_sol:
            why = f"lost {-r['today']:.3f} SOL today"
        if why:
            await self.update_wallet(address, paused=True, paused_reason=why)
            await self.notifier.send(
                f"⏸ Paused copying <b>{esc(w.label or address[:6])}</b>: {esc(why)}. "
                "Resume it from 👥 Copy & track.", buttons=[[("👥 Copy & track", "c")]])

    async def _not_copied(self, name: str, mint: str, why: str, via: str = "") -> None:
        """Say why a copy didn't happen: in Telegram too, or copying looks broken."""
        src = {"rpc": " (seen on chain)", "pumpportal": " (seen via PumpPortal)"}.get(via, "")
        text = f"👥 {esc(name)} bought <code>{mint}</code>{src}, not copied: {esc(why)}"
        await self.notifier.send(text, telegram=self._alert_allowed(self._copy_misses, 30),
                                 buttons=[[("🔍 Card", f"tc:{mint}")]])

    async def _wallet_alert(self, msg: dict, w: CopyWallet) -> None:
        side, mint = msg.get("txType"), msg["mint"]
        if side not in ("buy", "sell"):
            return
        if not self._alert_allowed(self._tracker_alerts, TRACKER_ALERTS_PER_HOUR):
            return
        sol = num(msg.get("solAmount"), allow_zero=True) or 0.0
        name = esc(w.label or w.address[:6])
        icon = "🟢" if side == "buy" else "🔴"
        buttons = [[(f"Buy {a:g}", f"b:{mint}:{a:g}") for a in (0.05, 0.1, 0.25)],
                   [("🔍 Token card", f"tc:{mint}")]]
        await self.notifier.send(f"🔔 {icon} <b>{name}</b> {'bought' if side == 'buy' else 'sold'} "
                                 f"{sol:.3f} SOL of <code>{mint}</code>", buttons=buttons)

    # ---------- momentum ----------

    async def on_momentum(self, sig: MomentumSignal) -> None:
        """A token pumping right now (from the momentum scanner): tell the owner, and in buy
        mode also try to buy it through the normal filters and risk limits."""
        import dataclasses
        d = self.cfg.discovery
        now = time.monotonic()
        last = self._momentum_seen.get(sig.mint)
        if last is not None and now - last < d.momentum_repeat_minutes * 60:
            return  # already signalled: one message per run, not one every scan
        held = self.positions.get(sig.mint)
        if held and not held.closed:
            return  # you're already in it
        c = Candidate(chain="solana", mint=sig.mint, source="momentum", symbol=sig.symbol,
                      name=sig.name, liquidity_usd=sig.liquidity_usd, fdv_usd=sig.market_cap_usd,
                      route="pump" if sig.pump_route else "jupiter", url=sig.url,
                      buy_sol=d.momentum_buy_sol or None, trigger="momentum")
        # the name / creator blocklists apply to signals too (size limits only to buys)
        if not self.safety.quick_check(dataclasses.replace(c, liquidity_usd=None, fdv_usd=None)):
            return
        if not self._alert_allowed(self._momentum_alerts, d.momentum_alerts_per_hour):
            return
        self._momentum_seen[sig.mint] = now
        buying = d.momentum_action == "buy" and not self.paused and not self.scan_only
        buyers = (f"{sig.buyers_5m} buyers" if sig.buyers_5m is not None
                  else f"{sig.buys_5m} buys") + f" / {sig.sells_5m} sells"
        mc = f" · MC ${sig.market_cap_usd:,.0f}" if sig.market_cap_usd else ""
        note = (" — buying (filters running)" if buying else
                " — buy mode is paused" if d.momentum_action == "buy" else "")
        text = (f"🚀 <b>Momentum: {esc(sig.symbol)}</b> +{sig.change_5m:.0f}% in 5m{note}\n"
                f"Vol 5m ${sig.volume_5m:,.0f} · {buyers} · liq ${sig.liquidity_usd or 0:,.0f}"
                f"{mc} · age {age_text(sig.created_at)}\n<code>{sig.mint}</code>")
        buttons = [[(f"Buy {a:g}", f"b:{sig.mint}:{a:g}") for a in (0.05, 0.1, 0.25)],
                   [("🔍 Token card", f"tc:{sig.mint}"), ("📈 Chart", sig.url)]]
        await self.notifier.send(text, buttons=buttons)
        if buying:
            self._spawn(self._momentum_buy(c))

    async def _momentum_buy(self, c: Candidate) -> None:
        result = await self.handle_candidate(c) or ""
        if not result.startswith("🟢"):  # bought: the BUY message already went out
            await self.notifier.send(f"🚀 {esc(c.symbol)} not bought: {result}")  # already escaped

    # ---------- entries ----------

    def exit_cfg(self, pos: Position) -> ExitConfig:
        """The exit rules for a position: copied ones can have their own TP / SL / moonbag."""
        ct = self.cfg.copytrade
        if (pos.copied_from or pos.source == "copy") and ct.only_their_sells:
            import dataclasses
            never = 10.0 ** 12
            e = self.cfg.exits
            return dataclasses.replace(
                e, take_profit=[], stop_loss_pct=ct.copy_stop_loss_pct or 100.0,
                breakeven_after_first_tp=False, trailing_activate_pct=never,
                max_hold_seconds=int(never), stale_seconds=int(never), sell_pressure_ratio=2.0,
                exit_on_dev_sell=ct.copy_rug_exits and e.exit_on_dev_sell,
                exit_on_whale_sell_pct=e.exit_on_whale_sell_pct if ct.copy_rug_exits else 0.0,
                sell_on_migration=False, kol_buy_sell_pct=0.0, sell_initials_at_pct=0.0,
                moonbag_pct=0.0)
        if pos.source == "copy" and self.cfg.copytrade.own_exits:
            import dataclasses
            ce = self.cfg.copyexits
            return dataclasses.replace(self.cfg.exits, **{f.name: getattr(ce, f.name)
                                                          for f in dataclasses.fields(ce)})
        return self.cfg.exits

    def _slots_used(self, kind: Optional[str] = None) -> int:
        """Open positions (moonbags don't take up a slot) plus buys in flight or unconfirmed.
        With a separate copy limit set, `kind` "copy" counts copies only, "snipe" the rest."""
        split = kind is not None and self.cfg.copytrade.max_positions > 0
        want_copy = kind == "copy"

        def counts(is_copy: bool) -> bool:
            return not split or is_copy == want_copy
        held = sum(1 for p in self.positions.values()
                   if not p.closed and not exits.in_moonbag(p, self.exit_cfg(p))
                   and counts(bool(p.copied_from) or p.source == "copy"))
        flying = {m for m in set(self._buying) | set(self.pending_buys())
                  if counts(m in self._copy_buying)}
        return held + len(flying)

    async def risk_block(self, sol: float, bal: Optional[float] = None,
                         copy: bool = False) -> Optional[str]:
        t, ct = self.cfg.trading, self.cfg.copytrade
        if copy and ct.max_positions:   # copies have their own slots
            if self._slots_used("copy") >= ct.max_positions:
                return "max copy positions"
        elif self._slots_used("snipe") >= t.max_open_positions:
            return "max open positions"
        if t.daily_loss_limit_sol > 0 and -self.store.realized_today() >= t.daily_loss_limit_sol:
            return "daily loss limit hit"
        if (not copy   # the cool-down after a losing snipe doesn't stop copying a wallet
                and t.cooldown_after_loss_seconds and self.last_loss_at is not None
                and time.monotonic() - self.last_loss_at < t.cooldown_after_loss_seconds):
            return "cooling down after loss"
        if self.live:
            if bal is None:
                try:
                    bal = await self.rpc.get_balance_sol(self.own_wallet)
                except Exception as e:  # can't verify funds: don't buy blind
                    return f"couldn't check the wallet balance ({str(e)[:80]})"
            tip = self._tip_estimate()
            # unconfirmed buys can still land within their window: reserve their SOL too
            unconfirmed = sum(num(p.get("sol"), allow_zero=True) or 0.0
                              for m, p in self.pending_buys().items() if m not in self._buying)
            needed = sol + sum(self._buying.values()) + unconfirmed + tip \
                + self.cfg.speed.max_priority_fee_sol \
                + ATA_RENT_SOL
            if bal - needed < t.min_sol_reserve:
                return f"balance too low ({bal:.4f} SOL)"
        return None

    async def try_buy(self, c: Candidate, notes: str = "") -> str:
        sol = c.buy_sol or self.cfg.trading.buy_amount_sol
        bal = None
        done_before = self._buys_finished
        cached = self._bal_cache
        if (self.live and cached and cached[2] == done_before
                and time.monotonic() - cached[1] < BALANCE_CACHE_SECONDS):
            bal = cached[0]  # kept fresh in the background: no RPC round trip on a snipe
        elif self.live:  # the slow RPC read happens outside the lock: buys never queue on it
            try:
                bal = await self.rpc.get_balance_sol(self.own_wallet)
                self._bal_cache = (bal, time.monotonic(), done_before)
            except Exception as e:  # can't verify funds: don't buy blind
                log.info("skip %s %s: balance unreadable (%s)", c.symbol, c.mint, e)
                return f"skipped: couldn't check the wallet balance ({esc(str(e)[:80])})"
        async with self.buy_lock:  # reserve a slot atomically, then trade without the lock
            if (c.mint in self._buying or c.mint in self.pending_buys()
                    or (c.mint in self.positions and not self.positions[c.mint].closed)):
                return "already holding"
            if self._buys_finished != done_before:
                bal = None  # a buy finished meanwhile: its spend may not be in that balance
            blocked = await self.risk_block(sol, bal, copy=c.source == "copy")
            if blocked:
                log.info("skip %s %s: %s", c.symbol, c.mint, blocked)
                return f"skipped: {blocked}"
            self._buying[c.mint] = sol
            if c.source == "copy":
                self._copy_buying.add(c.mint)
            if self.live:
                # write-ahead: if the bot dies anywhere from here on, the reconciler still
                # knows this buy may have landed and will adopt or expire it after restart
                tip = self._tip_estimate()
                self._add_pending(c, sol + tip)
        try:
            curve = None
            if not self.live:
                t_paper = time.time()
                await asyncio.sleep(PAPER_BUY_DELAY)
                if c.on_bonding_curve:
                    since = t_paper if PAPER_BUY_DELAY else 0.0
                    curve = await self._paper_curve(c, fresh_after=since)
            t_exec = time.monotonic()
            try:
                fill = await self.executor.buy(c, sol, curve)
            except NotLanded as e:
                self._drop_pending(c.mint)
                await self.notifier.send(f"⌛ buy {esc(c.symbol)} didn't land: {esc(str(e))}",
                                         logging.WARNING)
                return f"buy didn't land: {e}"
            except BuyUncertain as e:
                self._add_pending(c, e.sol, getattr(e, "pre", None))
                await self.notifier.send(f"⏳ buy {esc(c.symbol)}: {esc(str(e))}. Watching the wallet; "
                                         "if the tokens arrive they'll be managed automatically.",
                                         logging.WARNING)
                return f"buy unconfirmed: {e}"
            except Exception as e:  # failed before anything was sent, or failed on-chain
                self._drop_pending(c.mint)
                await self.notifier.send(f"❌ buy failed {esc(c.symbol)} {c.mint}: {esc(str(e))}",
                                         logging.WARNING)
                return f"buy failed: {e}"
            fill.tokens = num(fill.tokens, allow_zero=True) or 0.0  # never trust a fill blindly
            if fill.from_wallet and fill.pre is None:  # whole-wallet count, baseline unknown
                fill.tokens = max(0.0, fill.tokens - self._known_leftover(c.mint))
            fill.sol = num(fill.sol, allow_zero=True) or 0.0
            if not fill.sol_known or (fill.tokens > 0 and fill.sol <= 0):
                fill.sol = sol + self._tip_estimate()  # unreadable tx: we know what we sent
            if fill.tokens <= 0:  # confirmed but no tokens visible yet: let the reconciler decide
                if self.live:
                    self._add_pending(c, fill.sol or sol + self._tip_estimate(), fill.pre)
                await self.notifier.send(f"⏳ buy {esc(c.symbol)} confirmed but no tokens visible "
                                         f"yet ({fill.signature}); checking the wallet")
                return "buy returned 0 tokens"
            tag = c.trigger.split(":")[0]
            source = c.source + (f"/{tag}" if tag and tag != c.source else "")
            pos = Position(mint=c.mint, symbol=c.symbol or c.mint[:6], source=source,
                           # the market price paid: fixed costs (rent, tip, fees) are in sol_in
                           # for PnL in SOL, but must not make exits fire at once on small buys
                           creator=c.creator, entry_price=min(sol, fill.sol) / fill.tokens,
                           tokens_initial=fill.tokens, tokens_remaining=fill.tokens, sol_in=fill.sol,
                           route=c.route, leader=c.leader, copied_from=c.copied_from,
                           leader_mode=c.leader_mode,
                           migrated=c.source == "pumpfun-migration",  # already off the curve
                           seen_on_curve=c.source == "pumpfun" or curve is not None,
                           dev_tokens=c.creator_initial_buy_tokens or None)
            self.positions[c.mint] = pos
            self.store.save_position(pos)
            self.store.event("buy", c.mint, pos.symbol, source=c.source, sol=fill.sol,
                             tokens=fill.tokens, sig=fill.signature,
                             timing=self._timing(fill, t_exec, c.queued_at, c.window_s))
            self._drop_pending(c.mint)  # only after the position is safely on disk
        finally:
            self._buying.pop(c.mint, None)
            self._copy_buying.discard(c.mint)
            self._buys_finished += 1

        why = {"dev": "👀 watched dev launched", "limit": "📋 limit order"}.get(
            c.trigger, f"🔑 {c.trigger.split(':', 1)[-1]}" if c.trigger.startswith("keyword")
            else f"📣 called in {c.trigger.split(':', 1)[-1]}" if c.trigger.startswith("call:")
            else f"👥 copied {c.trigger.split(':', 1)[-1]}" if c.trigger.startswith("copy:")
            else "")
        text = (f"🟢 BUY <b>{esc(pos.symbol)}</b> {fill.sol:.4f} SOL → {fill.tokens:,.0f} tokens "
                f"({self.mode}, {c.source}) {esc(why)} {esc(notes)}\n<code>{c.mint}</code> "
                f"{esc(c.url or '')}")
        if c.route == "pump":  # exits first: a slow Telegram must never delay the feed
            await self.stream.watch_token(c.mint)
            if not (pos.seen_on_curve or pos.migrated):  # in the background: off the hot path
                self._spawn(self._note_curve_state(pos))
        await self.notifier.send(text, buttons=[[("Sell 50%", f"s:{c.mint}:50"),
                                                 ("Sell 100%", f"s:{c.mint}:100")]])
        return text

    # ---------- unconfirmed buys ----------

    def pending_buys(self) -> dict:
        import json
        return json.loads(self.store.get_setting(f"pending_buys:{self.mode}") or "{}")

    def _save_pending(self, pending: dict) -> None:
        import json
        self.store.set_setting(f"pending_buys:{self.mode}", json.dumps(pending))

    def _drop_pending(self, mint: str) -> None:
        pending = self.pending_buys()
        if pending.pop(mint, None) is not None:
            self._save_pending(pending)

    async def _paper_curve(self, c: Candidate, fresh_after: float = 0.0) -> Optional[CurveState]:
        """Paper fills price off the bonding curve: it must be current. A snapshot from the
        launch minutes ago (a manual buy of a token that has since run) would fake a fill.
        `fresh_after`: a cached curve must have been seen since then (the fill's delay)."""
        curve = self.curves.get(c.mint)
        seen = self._curve_ts.get(c.mint, 0)
        if curve and time.time() - seen <= PAPER_CURVE_MAX_AGE and seen >= fresh_after:
            return curve
        try:
            info = await fetch_curve(self.rpc, c.mint)
        except Exception as e:
            log.debug("curve refresh %s failed: %s", c.mint, e)
            info = None
        if info and not info.complete:
            if not getattr(info, "sol_quoted", True) or not is_standard(info.v_sol, info.v_tokens):
                raise ValueError("not a standard SOL pump.fun coin: paper can't price it")
            fresh = CurveState(info.v_sol, info.v_tokens)
            self._set_curve(c.mint, fresh)
            return fresh
        return None  # no current curve: price it with a Jupiter quote instead

    async def _paper_sell_curve(self, pos: Position) -> Optional[CurveState]:
        """A paper sell with no curve in memory (none seen lately): read it from the chain,
        as a live sell would trade against it, instead of asking Jupiter, which can't price
        a token still on its bonding curve."""
        try:
            info = await fetch_curve(self.rpc, pos.mint)
        except Exception as e:
            log.debug("curve refresh %s failed: %s", pos.mint, e)
            return None
        if (info is None or info.complete or not getattr(info, "sol_quoted", True)
                or not is_standard(info.v_sol, info.v_tokens)):
            return None
        curve = CurveState(info.v_sol, info.v_tokens)
        self._set_curve(pos.mint, curve)
        return curve

    def _mark_on_curve(self, pos: Position) -> None:
        if not pos.seen_on_curve:
            pos.seen_on_curve = True
            self.store.save_position(pos)  # survives a restart (the migration exit needs it)

    async def _note_curve_state(self, pos: Position) -> None:
        """Right after a buy: is this token on the bonding curve, or already graduated?
        (a buy of a graduated token must never look like a migration later)"""
        try:
            info = await fetch_curve(self.rpc, pos.mint)
        except Exception as e:
            log.debug("curve check %s failed: %s", pos.mint, e)
            return
        if info is None or pos.closed:
            return
        if info.complete:
            if not pos.migrated:
                pos.migrated = True
                self.curves.pop(pos.mint, None)
                self.store.save_position(pos)
        elif not pos.migrated:
            self._mark_on_curve(pos)

    def _tip_estimate(self) -> float:
        return self.cfg.speed.tip_sol()

    # Tokens the bot knows are in the wallet but belong to no position: what's left of a
    # written-off bag. Kept apart from the positions table (a re-buy of the mint replaces
    # its row) so a new position never mistakes them for its own.
    def _leftovers(self) -> dict:
        import json
        return json.loads(self.store.get_setting(f"leftovers:{self.mode}") or "{}")

    def _known_leftover(self, mint: str) -> float:
        return num(self._leftovers().get(mint), allow_zero=True) or 0.0

    def _set_leftover(self, mint: str, tokens: float) -> None:
        import json
        left = self._leftovers()
        if tokens > 0:
            left[mint] = tokens
        else:
            left.pop(mint, None)
        self.store.set_setting(f"leftovers:{self.mode}", json.dumps(left))

    def _clear_leftover(self, mint: str) -> None:
        if mint in self._leftovers():
            self._set_leftover(mint, 0.0)

    def _add_pending(self, c: Candidate, sol: float, pre: Optional[float] = None) -> None:
        pending = self.pending_buys()
        pending[c.mint] = {"sol": sol, "ts": time.time(), "symbol": c.symbol, "source": c.source,
                           "trigger": c.trigger, "route": c.route, "creator": c.creator,
                           "leader": c.leader, "dev_tokens": c.creator_initial_buy_tokens,
                           "pre": pre,  # tokens already held before this buy (None = unknown)
                           "swap_sol": c.buy_sol or self.cfg.trading.buy_amount_sol}
        self._save_pending(pending)

    async def keep_warm(self) -> None:
        """Live: ping each trading host in turn so its connection is open when a snipe comes.
        (The main RPC is kept warm by the balance refresh.) One host at a time, so a ping can
        never collide with a bundle in every Jito region at once."""
        from urllib.parse import urlsplit
        i = 0
        while True:
            hosts = sorted({f"{u.scheme}://{u.netloc}/" for u in map(urlsplit, [
                self.cfg.endpoints.pumpportal_trade,
                *(self.cfg.speed.jito_block_engines if self.cfg.speed.jito_enabled else []),
                *self.cfg.speed.broadcast_rpcs]) if u.scheme in ("http", "https") and u.netloc})
            await asyncio.sleep(WARM_SECONDS / max(1, len(hosts)))
            if self.live and hosts:
                url = hosts[i % len(hosts)]
                i += 1
                try:
                    await self.http.head(url, timeout=5)
                except Exception as e:  # only a keep-alive: never matters if it fails
                    log.debug("keep-alive %s: %s", url.split("//")[-1].split("/")[0], type(e).__name__)

    async def balance_loop(self) -> None:
        while True:
            if self.live and self.own_wallet:
                done = self._buys_finished
                try:
                    bal = await self.rpc.get_balance_sol(self.own_wallet)
                    self._bal_cache = (bal, time.monotonic(), done)
                except Exception as e:
                    log.debug("balance refresh failed: %s", e)
            await asyncio.sleep(BALANCE_REFRESH_SECONDS)

    async def reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_SECONDS)
            await self.reconcile_pending()

    async def reconcile_pending(self, now: Optional[float] = None) -> None:
        """Adopt tokens from buys whose outcome was unknown; forget them once they can no
        longer land."""
        for mint, info in list(self.pending_buys().items()):
            if mint in self._buying:
                continue  # still being confirmed by try_buy in this process
            try:
                held = await self.rpc.get_token_balance(self.own_wallet, mint) if self.live else 0.0
                pre = info.get("pre")
                if pre is None:  # crashed before the pre-buy balance was known
                    pre = self._known_leftover(mint)
                    if held < pre:  # that bag has since left the wallet: the guess is stale
                        pre = 0.0
                held -= num(pre, allow_zero=True) or 0.0  # only what this buy added
            except Exception as e:
                log.debug("reconcile %s: %s", mint, e)
                continue  # try again next round; never drop a buy we couldn't check
            pending = self.pending_buys()
            existing = self.positions.get(mint)
            if existing and not existing.closed:  # adopted before a crash: just clear it
                pending.pop(mint, None)
                self._save_pending(pending)
                continue
            if held > 0:
                # never a zero price: it would disable every exit (and divide by zero)
                spent = num(info.get("sol")) or num(info.get("swap_sol")) \
                    or self.cfg.trading.buy_amount_sol
                swap = min(num(info.get("swap_sol")) or spent, spent)
                info["sol"] = spent
                tag = (info.get("trigger") or "").split(":")[0]
                source = info["source"] + (f"/{tag}" if tag and tag != info["source"] else "")
                pos = Position(mint=mint, symbol=info.get("symbol") or mint[:6], source=source,
                               creator=info.get("creator"),
                               entry_price=swap / held,
                               tokens_initial=held, tokens_remaining=held, sol_in=info["sol"],
                               route=info.get("route", "jupiter"), leader=info.get("leader"),
                               migrated=info.get("source") == "pumpfun-migration",
                               seen_on_curve=info.get("source") == "pumpfun",
                               dev_tokens=info.get("dev_tokens"))
                self.positions[mint] = pos
                self.store.save_position(pos)
                self.store.event("buy", mint, pos.symbol, source=info["source"], sol=info["sol"],
                                 tokens=held, sig="reconciled")
                pending.pop(mint, None)
                self._save_pending(pending)
                if pos.route == "pump":
                    await self.stream.watch_token(mint)
                await self.notifier.send(f"✅ the unconfirmed buy of {esc(pos.symbol)} did land: "
                                         f"{held:,.0f} tokens, now managed.",
                                         buttons=[[("Sell 50%", f"s:{mint}:50"),
                                                   ("Sell 100%", f"s:{mint}:100")]])
            elif (now or time.time()) - info["ts"] > PENDING_BUY_WINDOW:
                pending.pop(mint, None)
                self._save_pending(pending)
                await self.notifier.send(f"ℹ️ the unconfirmed buy of {esc(info.get('symbol') or mint[:6])} "
                                         "never landed; no SOL was spent on it.")

    # ---------- price feeds ----------

    def _dup(self, msg: dict) -> bool:
        sig = msg.get("signature")
        if not sig:
            return False
        if sig in self._recent_sig_set:
            return True
        if len(self._recent_sigs) == self._recent_sigs.maxlen:
            self._recent_sig_set.discard(self._recent_sigs[0])
        self._recent_sigs.append(sig)
        self._recent_sig_set.add(sig)
        return False

    async def on_trade(self, msg: dict) -> None:
        """Called from the websocket loop, so it must never wait on a trade."""
        if self._dup(msg):  # token + account subscriptions can both deliver the same trade
            return
        mint = msg["mint"]
        if msg.get("via") == "rpc":   # from the backup wallet watcher: copy signals only
            return self._on_wallet_trade(msg)
        v_sol, v_tok = num(msg.get("vSolInBondingCurve")), num(msg.get("vTokensInBondingCurve"))
        if v_sol and v_tok:
            self._set_curve(mint, CurveState(v_sol, v_tok))
        if mint in self.flows:
            self.flows[mint].add(msg)
        self._on_wallet_trade(msg)
        pos = self.positions.get(mint)
        if not pos or pos.closed:
            return
        pool = msg.get("pool")
        if pos.route == "pump" and not pos.migrated:  # only pump.fun tokens can graduate
            if pool == "pump":
                self._mark_on_curve(pos)
            elif isinstance(pool, str) and pool:  # trading on PumpSwap / an AMM now
                await self.on_migration(mint)
        price = trade_price(msg)
        if price:
            pos.update_price(price)
        exits.record_trade(pos, msg, self.kol_wallets, self.own_wallet)
        self._spawn(self.check_exit(pos))

    def _on_wallet_trade(self, msg: dict) -> None:
        """A trade by a copied / tracked wallet (from PumpPortal or the RPC watcher)."""
        trader = msg.get("traderPublicKey", "")
        leader = self._copy.get(trader)
        if not leader:
            return
        self.copy_seen[trader] = (time.time(), msg.get("via", "pumpportal"),
                                  msg.get("txType", ""), msg["mint"])
        self.wallet_checker.record(msg)
        self._spawn(self.handle_copy(msg, leader))
        pos = self.positions.get(msg["mint"])
        if (msg.get("via") == "rpc" and pos and not pos.closed and msg.get("txType") == "sell"
                and pos.leader == trader):
            exits.leader_sold_part(pos, msg)   # follow their sell, even without PumpPortal
            self._spawn(self.check_exit(pos))

    async def on_migration(self, mint: str) -> None:
        self.curves.pop(mint, None)  # graduated: that curve no longer prices anything
        pos = self.positions.get(mint)
        if pos and not pos.closed and not pos.migrated:
            pos.migrated = True  # with sell_on_migration on, the exit loop sells it next tick
            self.store.save_position(pos)
            # runs inside the websocket loop: never wait on Telegram here
            self._spawn(self.notifier.send(f"🎓 {esc(pos.symbol)} graduated off the bonding curve"))

    async def price_poller(self) -> None:
        """Prices held tokens when the trade stream is unavailable or quiet: on-chain
        bonding curve for pump.fun tokens, a Jupiter exit quote for everything else.
        Without the stream it also watches the dev's balance to catch dev dumps."""
        while True:
            open_pos = [p for p in self.positions.values() if not p.closed]
            if open_pos:
                await asyncio.gather(*(self._poll_position(p) for p in open_pos))
            await asyncio.sleep(PRICE_POLL_SECONDS)

    async def _poll_position(self, pos: Position) -> None:
        quiet = time.time() - pos.last_update >= QUOTE_GAP_SECONDS
        try:
            if pos.route == "pump" and not pos.migrated:
                if quiet or not self.has_trade_stream:
                    curve = await fetch_curve(self.rpc, pos.mint)
                    if curve is None:
                        # A just-created account can lag on some RPC nodes; only after several
                        # misses do we conclude the token isn't on a pump.fun curve at all.
                        n = self._curve_misses[pos.mint] = self._curve_misses.get(pos.mint, 0) + 1
                        if n >= CURVE_MISSES_BEFORE_MIGRATED:
                            await self.on_migration(pos.mint)
                    elif curve.complete:
                        await self.on_migration(pos.mint)  # graduated
                    else:
                        self._curve_misses.pop(pos.mint, None)
                        self._set_curve(pos.mint, CurveState(curve.v_sol, curve.v_tokens))
                        self._mark_on_curve(pos)
                        pos.update_price(curve.price)
                # The dev's balance is checked even with the trade feed: the feed only shows
                # sells from the dev's own wallet, not tokens moved elsewhere and sold there
                # (and it can stop). Less often while the feed is live, to spare the RPC.
                every = DEV_CHECK_STREAM_SECONDS if self.has_trade_stream else 0.0
                if (pos.creator and self.cfg.exits.exit_on_dev_sell and time.monotonic()
                        - self._dev_checked.get(pos.mint, float("-inf")) >= every):
                    self._dev_checked[pos.mint] = time.monotonic()
                    dev = await self.rpc.get_token_balance(pos.creator, pos.mint)
                    if pos.dev_tokens is None:
                        pos.dev_tokens = dev
                    elif dev < pos.dev_tokens * 0.99:
                        pos.dev_sold = True
            elif (quiet and pos.tokens_remaining > 0 and time.monotonic()
                  - self._quoted_at.get(pos.mint, float("-inf")) >= JUPITER_PRICE_SECONDS):
                self._quoted_at[pos.mint] = time.monotonic()
                out = await self.executor.quote_sell(pos.mint, pos.tokens_remaining)
                if out:
                    pos.update_price(out / pos.tokens_remaining)
        except Exception as e:
            log.debug("price poll %s failed: %s", pos.symbol, e)

    # ---------- exits ----------

    async def exit_loop(self) -> None:
        offset = time.time() - time.monotonic()
        boot = _boottime()
        step_offset = None if boot is None else time.time() - boot
        while True:
            # Position timestamps are wall-clock. A clock *step* (NTP fix, VM migration) moves
            # them all without any real time passing: shift them with it, or max-hold and
            # stale exits fire on healthy tokens. A suspend/resume is real time (max hold may
            # rightly expire), but prices weren't polled: give the feeds a fresh window.
            boot = _boottime()
            new_step = None if boot is None or step_offset is None else time.time() - boot
            if new_step is not None and abs(new_step - step_offset) > CLOCK_JUMP_SECONDS:
                step = new_step - step_offset
                log.warning("system clock stepped %+.0fs; shifting position timers", step)
                for pos in self.positions.values():
                    pos.opened_at += step
                    pos.last_update += step
                    if not pos.closed:
                        self.store.save_position(pos)  # survives a restart too
                pending = self.pending_buys()  # an unconfirmed buy keeps its full window
                for info in pending.values():
                    info["ts"] = info.get("ts", 0) + step
                self._save_pending(pending)
                self.store.db.execute("UPDATE orders SET expires = expires + ? WHERE status = 'open'",
                                      (step,))
            step_offset = new_step
            new_offset = time.time() - time.monotonic()
            if abs(new_offset - offset) > CLOCK_JUMP_SECONDS:
                log.warning("clock jumped %+.0fs; refreshing price timers", new_offset - offset)
                now = time.time()
                for pos in self.positions.values():
                    pos.last_update = now
            offset = new_offset
            for pos in list(self.positions.values()):
                if not pos.closed:
                    self._spawn(self.check_exit(pos))  # one slow sell never delays the others
            await asyncio.sleep(1)

    async def check_exit(self, pos: Position) -> None:
        lock = self.sell_locks.get(pos.mint)
        if lock and lock.locked():
            return
        if time.monotonic() < self._sell_next_try.get(pos.mint, float("-inf")):
            return  # backing off after a failed sell
        dec = exits.evaluate(pos, self.exit_cfg(pos))
        if dec:
            await self.execute_sell(pos, dec)

    @staticmethod
    def _timing(fill, t_exec: float, t0: float, window: float = 0.0) -> dict:
        """Seconds per step of one trade, for the speed stats: decide (seen -> sending, minus a
        confirmation window), build, send, confirm, total."""
        now = time.monotonic()
        out = {k: round(v, 3) for k, v in (getattr(fill, "timings", None) or {}).items()
               if isinstance(v, (int, float))}
        if t0:
            out["decide"] = round(max(0.0, t_exec - t0 - window), 3)
            out["total"] = round(max(0.0, now - t0 - window), 3)
        return out

    async def execute_sell(self, pos: Position, dec: exits.ExitDecision) -> str:
        t_decided = time.monotonic()  # the exit rule fired just now
        lock = self.sell_locks.setdefault(pos.mint, asyncio.Lock())
        if lock.locked():
            return "a sell is already in progress"
        async with lock:
            if pos.closed:
                return "already closed"
            if dec.tokens <= 0 and not dec.sell_all:  # bookkeeping only (e.g. TP above a moonbag)
                exits.apply_fill(pos, dec, 0.0, 0.0, self.exit_cfg(pos))
                self.store.save_position(pos)
                return "nothing to sell"
            if not self.live:  # a live sell fills ~1s later too: price it then (see above)
                await asyncio.sleep(PAPER_SELL_DELAY)
            curve = self.curves.get(pos.mint) if not pos.migrated else None
            if curve is None and not self.live and not pos.migrated:
                curve = await self._paper_sell_curve(pos)
            if self.live:
                pos.pending_exit = {"reason": dec.reason, "tp_index": dec.tp_index,
                                    "kind": dec.kind}
                self.store.save_position(pos)
            try:
                extra = {"value_sol": dec.tokens * pos.last_price,  # caps the tx guard
                         "urgent": dec.reason.startswith(DANGER_EXITS)} \
                    if isinstance(self.executor, LiveExecutor) else {}
                fill = await self.executor.sell(pos.mint, dec.tokens, dec.sell_all,
                                                pump=pos.route == "pump", curve=curve,
                                                slippage_pct=self._sell_slippage(pos, dec),
                                                **extra)
            except NothingToSell:
                self._clear_leftover(pos.mint)  # the wallet holds none at all
                pos.sol_out += self._estimate_value(pos)
                pos.tokens_remaining, pos.closed = 0.0, True
                pos.close_reason = "no tokens left in wallet (PnL estimated)"
                self.store.save_position(pos)
                await self._closed(pos)
                return f"{esc(pos.symbol)}: nothing left in the wallet; position closed"
            except Exception as e:
                return await self._sell_failed(pos, e, dec)
            self.sell_failures.pop(pos.mint, None)
            self._sell_next_try.pop(pos.mint, None)
            fill.sol = num(fill.sol, allow_zero=True) or 0.0  # a bad fill never corrupts the books
            fill.tokens = min(num(fill.tokens) or dec.tokens, pos.tokens_remaining)
            leftover = self._known_leftover(pos.mint) if fill.emptied else 0.0
            if leftover > 0 and fill.sol_known:
                # "sell 100%" also sold the old written-off bag: only this position's share of
                # the SOL is its own; the rest is booked as the old bag's (untracked) sale
                mine = pos.tokens_remaining
                share = mine / (mine + leftover) if mine + leftover > 0 else 1.0
                self.store.event("sell", pos.mint, pos.symbol, reason="old bag (untracked)",
                                 tokens=leftover, sol=fill.sol * (1 - share), sig=fill.signature)
                fill.sol *= share
            if fill.emptied:
                self._clear_leftover(pos.mint)  # "sell 100%" took any old bag with it
            if fill.emptied and not dec.sell_all:  # the wallet held less than the position
                dec = exits.ExitDecision(fill.tokens, True, dec.reason, dec.tp_index, dec.kind)
            estimated = not fill.sol_known
            if estimated:  # landed, but the tx couldn't be read: never book it as 0 SOL
                fill.sol = self._estimate_value(pos, fill.tokens)
            exits.apply_fill(pos, dec, fill.tokens, fill.sol, self.exit_cfg(pos))
            pos.pending_exit = None
            self.store.save_position(pos)
            self.store.event("sell", pos.mint, pos.symbol, reason=dec.reason, tokens=fill.tokens,
                             sol=fill.sol, sig=fill.signature,
                             timing=self._timing(fill, t_decided, t_decided))
            text = (f"🔴 SELL {esc(pos.symbol)} {'ALL' if dec.sell_all else f'{dec.tokens:,.0f}'} "
                    f"→ {fill.sol:.4f} SOL{' (estimated)' if estimated else ''} — "
                    f"{esc(dec.reason)} (pnl {pos.pnl_pct:+.0f}%)")
            self._spawn(self.notifier.send(text))  # never hold the sell lock waiting on Telegram
            if pos.closed:
                if self.live and fill.emptied:
                    await self._rent_back(pos)
                await self._closed(pos)
            return text

    def _sell_slippage(self, pos: Position, dec: exits.ExitDecision) -> float:
        """A token crashing through a rug blows past normal slippage, so each failed attempt
        allows more, and emergency exits start higher."""
        t, ct = self.cfg.trading, self.cfg.copytrade
        base = t.sell_slippage_pct or t.slippage_pct
        if (pos.copied_from or pos.source == "copy") and ct.sell_slippage_pct:
            base = ct.sell_slippage_pct
        start = base
        if dec.reason.startswith(DANGER_EXITS):
            base *= 1.5
        n = self.sell_failures.get(pos.mint, 0)
        # never below the slippage you set: someone running 80% gets at least 80% on exits
        cap = min(100.0, max(MAX_SELL_SLIPPAGE, start))
        return min(cap, max(base, base * (1 + n)))

    async def _sell_failed(self, pos: Position, err: Exception,
                           dec: Optional[exits.ExitDecision] = None) -> str:
        """Never abandon a position on a transient failure: back off, reconcile, retry."""
        pos.pending_exit = None  # handled here; the next attempt writes its own
        n = self.sell_failures[pos.mint] = self.sell_failures.get(pos.mint, 0) + 1
        wait = min(2 ** n, 10) if n < SELL_FAST_RETRIES else SELL_BACKOFF_MAX
        self._sell_next_try[pos.mint] = time.monotonic() + wait
        if self.live:  # a sell that "failed" may still have landed: trust the wallet
            try:
                held = await self.rpc.get_token_balance(self.own_wallet, pos.mint)
                if held <= 0:
                    self._clear_leftover(pos.mint)  # the wallet is empty: no old bag either
                held = max(0.0, held - self._known_leftover(pos.mint))  # only this position's
                if exits.is_dust(pos, held):  # all of it (bar dust) left the wallet
                    pos.sol_out += self._estimate_value(pos)
                    pos.tokens_remaining, pos.closed = 0.0, True
                    pos.close_reason = "sold (confirmed late, PnL estimated)"
                    self.store.save_position(pos)
                    await self._closed(pos)
                    return f"{esc(pos.symbol)}: the sell landed late; position closed"
                if held < pos.tokens_remaining * 0.999:  # it (partly) landed: book it as a fill
                    sold = pos.tokens_remaining - held
                    done = exits.ExitDecision(sold, False, dec.reason if dec else "sell",
                                              dec.tp_index if dec else None, dec.kind if dec else "")
                    # marks the TP level / initials / KOL exit as done, so it isn't sold again
                    est = self._estimate_value(pos, sold)
                    exits.apply_fill(pos, done, sold, est, self.exit_cfg(pos))
                    self.store.save_position(pos)
                    self.store.event("sell", pos.mint, pos.symbol, reason=f"{done.reason} (late)",
                                     tokens=sold, sol=est, sig="landed-late")
                    self.sell_failures.pop(pos.mint, None)
                    self._sell_next_try.pop(pos.mint, None)
                    text = (f"🔴 SELL {esc(pos.symbol)} {sold:,.0f} — {esc(done.reason)}: it landed "
                            "late (PnL estimated)")
                    self._spawn(self.notifier.send(text))
                    if pos.closed:  # booked, counted in today's PnL and the loss limit
                        await self._closed(pos)
                    return text  # a real sell: callers (limit orders) must see it as filled
            except Exception as e:
                log.debug("balance reconcile failed: %s", e)
        if n in (1, 5) or n % 20 == 0:
            self._spawn(self.notifier.send(f"⚠️ sell {esc(pos.symbol)} failed ({n}x), retrying: "
                                           f"{esc(str(err)[:300])}", logging.WARNING))
        if n >= WRITE_OFF_AFTER:
            if self.live:
                self._set_leftover(pos.mint, self._known_leftover(pos.mint) + pos.tokens_remaining)
            pos.closed, pos.close_reason = True, "unsellable: written off"
            self.store.save_position(pos)
            await self._closed(pos)
            where = (" The tokens are still in your wallet: send /sell <mint> to try again "
                     "later.") if self.live else ""
            self._spawn(self.notifier.send(f"🛑 {esc(pos.symbol)} couldn't be sold after {n} tries "
                                           f"and was written off.{esc(where)}", logging.ERROR))
        return f"sell failed: {esc(str(err)[:300])}"

    @staticmethod
    def _estimate_value(pos: Position, tokens: Optional[float] = None) -> float:
        """Best guess of what tokens that left the wallet without a recorded fill sold for."""
        n = pos.tokens_remaining if tokens is None else tokens
        return max(0.0, n * pos.last_price * ESTIMATE_HAIRCUT)

    async def _rent_back(self, pos: Position) -> None:
        """After a full exit, close the now-empty token account: its rent (~0.002 SOL, paid
        by the buy) comes back to the wallet and into this trade's PnL."""
        try:
            n, sol = await asyncio.wait_for(
                self.executor.close_empty(only={pos.mint}, keep=self._busy_mints() - {pos.mint}),
                RENT_BACK_TIMEOUT)
        except Exception as e:  # never blocks the close: /reclaim can sweep it later
            log.info("couldn't close the empty %s token account: %s", pos.symbol, e)
            return
        if n:
            pos.sol_out += sol
            self._rent_note[pos.mint] = sol
            self.store.save_position(pos)

    def _busy_mints(self) -> set[str]:
        """Mints whose token account must stay open: held, being bought, or maybe landing."""
        return ({m for m, p in self.positions.items() if not p.closed}
                | set(self._buying) | set(self.pending_buys()))

    async def reclaim_rent(self) -> str:
        """Close every empty token account in the wallet and take back the rent."""
        if not self.live:
            return "Paper mode has no real token accounts to close."
        n, sol = await self.executor.close_empty(keep=frozenset(self._busy_mints()))
        if not n:
            return "🧹 No empty token accounts to close: nothing to take back."
        self.store.event("rent", sol=sol, accounts=n)
        return f"🧹 Closed {n} empty token account(s): {sol:.4f} SOL back in your wallet."

    def reset_paper_results(self) -> str:
        """Start paper results over (e.g. after a change in how paper trades are priced).
        Never touches live results, open paper positions or anything else."""
        n = self.store.clear_results("paper")
        return f"🗑 Paper results cleared ({n} records). New paper trades start from zero."

    async def _closed(self, pos: Position) -> None:
        pnl = pos.realized_pnl_sol
        if pnl < 0:
            self.last_loss_at = time.monotonic()
        if (pos.close_reason == "dev sold" and pnl < 0 and pos.creator
                and self.cfg.filters.auto_blocklist_ruggers):
            self.store.block(pos.creator, f"dev dumped {pos.symbol}")
            funder = self.devcheck.known_funder(pos.creator)
            if funder:  # the wallet that bankrolled this dev will bankroll the next one
                self.store.block(funder, f"funded the dev who dumped {pos.symbol}")
        self.store.event("close", pos.mint, pos.symbol, reason=pos.close_reason, source=pos.source,
                         sol_in=pos.sol_in, sol_out=pos.sol_out, pnl_sol=pnl,
                         held_s=round(time.time() - pos.opened_at), copied_from=pos.copied_from)
        if pos.copied_from:   # in the background: never hold up the sell path
            self._spawn(self._check_wallet_pause(pos.copied_from))
        self.sell_failures.pop(pos.mint, None)
        self._sell_next_try.pop(pos.mint, None)
        self._curve_misses.pop(pos.mint, None)
        self._quoted_at.pop(pos.mint, None)
        self._dev_checked.pop(pos.mint, None)
        rent = self._rent_note.pop(pos.mint, 0.0)
        self._spawn(self.notifier.send(  # callers may hold the sell lock: don't wait on Telegram
            f"🏁 closed {esc(pos.symbol)}: {pnl:+.4f} SOL ({esc(pos.close_reason)})"
            + (f", incl. {rent:.4f} SOL account rent back" if rent else "")
            + f" — today {self.store.realized_today():+.4f} SOL"))
        await self.stream.unwatch_token(pos.mint)

    # ---------- manual control (CLI / Telegram) ----------

    def find_position(self, key: str) -> Optional[Position]:
        if key in self.positions:
            return self.positions[key]
        matches = [p for p in self.positions.values()
                   if not p.closed and p.symbol.lower() == key.lower()]
        return matches[0] if len(matches) == 1 else None

    async def manual_sell(self, key: str, pct: float = 100.0) -> str:
        pos = self.find_position(key)
        if not pos or pos.closed:
            if self.live and len(key) >= 32:
                return await self.sell_wallet_token(key)
            return f"no open position for {esc(key)}"
        try:
            _finite(pct)  # min/max let NaN straight through
        except ValueError as e:
            return str(e)
        pct = min(max(pct, 1.0), 100.0)
        dec = exits._partial(pos, pos.tokens_remaining * pct / 100, "manual")
        if pct >= 100 or dec is None:
            dec = exits.ExitDecision(pos.tokens_remaining, True, "manual")
        return await self.execute_sell(pos, dec)

    async def sell_wallet_token(self, mint: str) -> str:
        """Live only: sell everything the wallet holds of a token the bot isn't tracking
        (e.g. a written-off position)."""
        if not self.live:
            return "only available in LIVE mode"
        try:
            fill = await self.executor.sell(mint, 0.0, True, pump=mint.endswith("pump"), curve=None)
        except NothingToSell:
            return "the wallet holds none of that token"
        except Exception as e:
            return f"sell failed: {esc(str(e)[:300])}"
        self.store.event("sell", mint, mint[:6], reason="manual (untracked)", tokens=fill.tokens,
                         sol=fill.sol, sig=fill.signature)
        self._clear_leftover(mint)  # the written-off bag is gone now
        return f"🔴 sold {fill.tokens:,.0f} tokens → {fill.sol:.4f} SOL"

    async def _calls_loop(self) -> None:
        import importlib.util
        if importlib.util.find_spec("telethon") is None:
            log.warning("call sniper unavailable: re-run the installer to add its library")
            await asyncio.Event().wait()
        await self.calls.run(lambda: self.cfg.calls)

    async def on_call(self, mint: str, group: CallGroup, n_groups: int) -> None:
        """A CA was posted in a watched Telegram group: buy it (or just say so)."""
        cfg = self.cfg.calls
        where = esc(group.title) + (f" (posted in {n_groups} groups)" if n_groups > 1 else "")
        head = f"📣 Call in {where}: <code>{mint}</code>"
        size = group.sol or self.cfg.trading.buy_amount_sol
        buttons = [[(f"Buy {size:g} SOL", f"b:{mint}:{size:g}"), ("🔍 Card", f"tc:{mint}")]]
        if cfg.action != "buy" or self.paused:
            note = "⏸ sniping is paused, not buying" if cfg.action == "buy" else "alert only"
            await self.notifier.send(f"{head}\n{note}", buttons=buttons)
            return
        try:
            curve = await fetch_curve(self.rpc, mint)
        except Exception as e:
            log.debug("curve lookup for call %s failed: %s", mint, e)
            curve = None
        on_curve = bool(curve and not curve.complete and curve.sol_quoted)
        if cfg.max_market_cap_usd:
            mcap = await self._market_cap_sol(mint, curve if on_curve else None)
            over = await self._over_cap(mcap, cfg.max_market_cap_usd)
            if over:
                await self.notifier.send(f"{head}\n❌ not bought: {esc(over)}", buttons=buttons)
                return
        c = Candidate(chain="solana", mint=mint, source="call", symbol=mint[:6],
                      creator=curve.creator if curve else None, buy_sol=group.sol or None,
                      force=not group.filters, trigger=f"call:{group.title[:40]}",
                      route="pump" if on_curve or mint.endswith("pump") else "jupiter",
                      v_sol=curve.v_sol if on_curve else None,
                      v_tokens=curve.v_tokens if on_curve else None)
        if on_curve:
            self._set_curve(mint, CurveState(curve.v_sol, curve.v_tokens))
        result = await self.handle_candidate(c) or "done"
        if not result.startswith("🟢"):   # a buy announces itself
            await self.notifier.send(f"{head}\n{result}", buttons=buttons)

    async def sol_usd(self) -> Optional[float]:
        """SOL's price in dollars, refreshed at most once a minute (stale beats none)."""
        price, at = self._sol_usd
        if price and time.monotonic() - at < SOL_PRICE_TTL:
            return price
        try:
            q = await self.jupiter.quote(SOL_MINT, USDC_MINT, 1.0, 1, urgent=False)
            fresh = await self.jupiter.out_ui(q)
            if fresh > 0:
                self._sol_usd = (fresh, time.monotonic())
                return fresh
        except Exception as e:
            log.debug("SOL price unavailable: %s", e)
        return price or None

    async def _over_cap(self, mcap_sol: Optional[float], cap_usd: float) -> Optional[str]:
        """Why a coin is too big for a $ market cap limit, or None if it's under (or the
        numbers aren't available: a missing price never blocks a buy)."""
        if mcap_sol is None:
            return None
        usd = await self.sol_usd()
        if not usd:
            log.warning("market cap limit skipped: no SOL price right now")
            return None
        mcap = mcap_sol * usd
        if mcap <= cap_usd:
            return None
        return f"market cap ${mcap:,.0f} is over ${cap_usd:,.0f}"

    async def _sol_price_loop(self) -> None:
        """Keep the SOL price warm while a $ market cap limit is set, so the check
        never waits on a price lookup when a buy comes in."""
        while True:
            if self.cfg.copytrade.max_market_cap_usd or self.cfg.calls.max_market_cap_usd:
                await self.sol_usd()
            await asyncio.sleep(SOL_PRICE_TTL - 5)

    async def _market_cap_sol(self, mint: str, curve) -> Optional[float]:
        try:
            if curve is not None:
                return curve.price * PUMP_TOTAL_SUPPLY
            info = await self.rpc.get_mint_info(mint)
            supply = int(info.get("supply", 0)) / 10 ** int(info.get("decimals", 0))
            return await self.price_of(mint) * supply
        except Exception as e:
            log.debug("market cap for %s unavailable: %s", mint, e)
            return None

    async def manual_buy(self, mint: str, sol: Optional[float], force: bool = False) -> str:
        from solders.pubkey import Pubkey
        try:
            Pubkey.from_string(mint)
        except ValueError:
            return "invalid mint address"
        if sol is not None and not 0 < sol <= 100:
            return "amount must be between 0 and 100 SOL"
        c = Candidate(chain="solana", mint=mint, source="manual", symbol=mint[:6], buy_sol=sol,
                      force=force, route="pump" if mint.endswith("pump") else "jupiter")
        return await self.handle_candidate(c) or "done"

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.store.set_setting("paused", "1" if paused else "0")

    async def status_text(self) -> str:
        open_pos = [p for p in self.positions.values() if not p.closed]
        lines = [f"<b>Mode:</b> {self.mode.upper()} · preset {self.cfg.preset}"
                 f"{' · ⏸ PAUSED' if self.paused else ''}"]
        if self.live:
            try:
                bal = await self.rpc.get_balance_sol(self.own_wallet)
                lines.append(f"<b>Wallet:</b> <code>{self.own_wallet}</code> · {bal:.4f} SOL")
            except Exception as e:
                lines.append(f"<b>Wallet:</b> balance unavailable ({esc(str(e))})")
        lines += [
            f"<b>Buy size:</b> {self.cfg.trading.buy_amount_sol} SOL · "
            f"Jito {'on' if self.cfg.speed.jito_enabled else 'off'}",
            f"<b>Open:</b> {len(open_pos)}/{self.cfg.trading.max_open_positions}",
            f"<b>Realized today:</b> {self.store.realized_today():+.4f} SOL",
            f"<b>Copying:</b> {len(self._copy)} wallet(s)",
        ]
        return "\n".join(lines)

    # ---------- limit orders ----------

    async def price_of(self, mint: str) -> float:
        """Current price in SOL per token: held position, else bonding curve, else Jupiter."""
        pos = self.positions.get(mint)
        if pos and not pos.closed and pos.last_price > 0:
            return pos.last_price
        try:
            curve = await fetch_curve(self.rpc, mint)
            if curve and not curve.complete and curve.price > 0:
                self._set_curve(mint, CurveState(curve.v_sol, curve.v_tokens))
                return curve.price
        except Exception as e:
            log.debug("curve lookup %s failed: %s", mint, e)
        probe = 0.01
        q = await self.jupiter.quote(SOL_MINT, mint, probe, self.cfg.trading.slippage_pct,
                                     urgent=False)  # a price check, not a trade
        tokens = await self.jupiter.out_ui(q)
        if tokens <= 0:
            raise ValueError("no price available")
        return probe / tokens

    async def place_limit_buy(self, mint: str, sol: float, change_pct: float,
                              hours: float = 24.0) -> str:
        solana_address(mint, "token address")
        _finite(sol, change_pct, hours)
        if not 0 < sol <= 100:
            raise ValueError("amount must be between 0 and 100 SOL")
        if change_pct == 0 or change_pct <= -99:
            raise ValueError("change must be non-zero and above -99%")
        if not 0 < hours <= 24 * 30:
            raise ValueError("expiry must be between 0 and 720 hours")
        if len(self.store.open_orders()) >= MAX_OPEN_ORDERS:
            raise ValueError(f"at most {MAX_OPEN_ORDERS} open orders")
        base = await self.price_of(mint)
        trigger = base * (1 + change_pct / 100)
        direction = "<=" if change_pct < 0 else ">="
        oid = self.store.add_order(mint, "buy", sol, 0.0, trigger, direction, base,
                                   time.time() + hours * 3600)
        kind = "dips" if change_pct < 0 else "rises"
        return (f"📋 Order #{oid}: buy {sol:g} SOL of <code>{mint}</code> if it {kind} "
                f"{abs(change_pct):g}% (expires in {hours:g}h)")

    async def place_limit_sell(self, key: str, pct: float, pnl_pct: float,
                               hours: float = 24.0) -> str:
        pos = self.find_position(key)
        if not pos or pos.closed:
            raise ValueError(f"no open position for {key}")
        _finite(pct, pnl_pct, hours)
        if not 0 < pct <= 100:
            raise ValueError("sell % must be between 0 and 100")
        if pnl_pct <= -100:
            raise ValueError("profit target must be above -100%")
        if not 0 < hours <= 24 * 30:
            raise ValueError("expiry must be between 0 and 720 hours")
        if len(self.store.open_orders()) >= MAX_OPEN_ORDERS:
            raise ValueError(f"at most {MAX_OPEN_ORDERS} open orders")
        trigger = pos.entry_price * (1 + pnl_pct / 100)
        direction = ">=" if trigger >= pos.last_price else "<="
        oid = self.store.add_order(pos.mint, "sell", 0.0, pct, trigger, direction, pos.entry_price,
                                   time.time() + hours * 3600)
        return (f"📋 Order #{oid}: sell {pct:g}% of {esc(pos.symbol)} at {pnl_pct:+g}% from entry "
                f"(now {pos.pnl_pct:+.0f}%, expires in {hours:g}h)")

    def cancel_order(self, order_id: int) -> bool:
        return self.store.set_order_status(order_id, "cancelled")

    async def order_loop(self) -> None:
        while True:
            await asyncio.sleep(ORDER_POLL_SECONDS)
            await self.check_orders()

    async def check_orders(self) -> None:
        now = time.time()
        prices: dict[str, float] = {}
        # protective sells first (no network needed), then buys with their prices fetched
        # concurrently: a slow quote must never delay a stop-loss order
        orders = sorted(self.store.open_orders(), key=lambda o: o["side"] != "sell")
        fetched = False
        for o in orders:
            if o["id"] in self._orders_running:
                continue
            if now >= o["expires"]:
                self.store.set_order_status(o["id"], "expired")
                self._spawn(self.notifier.send(f"⌛ Order #{o['id']} expired"))  # don't stall stops
                continue
            if o["side"] == "sell":
                pos = self.positions.get(o["mint"])
                if not pos or pos.closed:
                    self.store.set_order_status(o["id"], "cancelled")
                    continue
                if time.monotonic() < self._sell_next_try.get(pos.mint, float("-inf")):
                    continue  # its last sell failed: same backoff as automatic exits
                price = pos.last_price
            else:
                if not fetched:
                    fetched = True
                    mints = sorted({b["mint"] for b in orders if b["side"] == "buy"})
                    got = await asyncio.gather(*(self.price_of(m) for m in mints),
                                               return_exceptions=True)
                    for m, v in zip(mints, got):
                        if isinstance(v, Exception) or num(v) is None:
                            log.debug("order price for %s unavailable: %r", m, v)
                        else:
                            prices[m] = num(v)
                if o["mint"] not in prices:
                    continue
                price = prices[o["mint"]]
            hit = price <= o["trigger_price"] if o["direction"] == "<=" else price >= o["trigger_price"]
            if hit and self.store.set_order_status(o["id"], "executing"):  # write-ahead
                self._orders_running.add(o["id"])
                self._spawn(self._fill_order(o))

    async def _fill_order(self, o: dict) -> None:
        try:
            if o["side"] == "buy":
                c = Candidate(chain="solana", mint=o["mint"], source="limit", symbol=o["mint"][:6],
                              buy_sol=o["sol"], trigger="limit",
                              route="pump" if o["mint"].endswith("pump") else "jupiter")
                result = await self.handle_candidate(c) or ""
                ok = result.startswith("🟢")
                if not ok and o["mint"] in self.pending_buys():  # sent; outcome not known yet
                    self.store.set_order_status(o["id"], "unconfirmed", from_status="executing")
                    await self.notifier.send(f"⏳ Order #{o['id']}: {result}. If it lands it "
                                             "will be managed automatically.", logging.WARNING)
                    return
            else:
                pos = self.positions.get(o["mint"])
                if not pos or pos.closed:
                    self.store.set_order_status(o["id"], "cancelled", from_status="executing")
                    return
                dec = exits._partial(pos, pos.tokens_remaining * o["pct"] / 100, "limit sell")
                if o["pct"] >= 100 or dec is None:
                    dec = exits.ExitDecision(pos.tokens_remaining, True, "limit sell")
                result = await self.execute_sell(pos, dec)
                # our sell that only confirmed late also filled the order; tokens found gone
                # from the wallet (sold some other way) did not
                ok = result.startswith("🔴") or (
                    pos.closed and pos.close_reason.startswith("sold (confirmed late"))
                if not ok and not pos.closed:  # busy or a transient failure: keep the stop armed
                    self.store.set_order_status(o["id"], "open", from_status="executing")
                    return
                if not ok:  # the position closed some other way (e.g. nothing left to sell)
                    self.store.set_order_status(o["id"], "cancelled", from_status="executing")
                    await self.notifier.send(f"ℹ️ Order #{o['id']} cancelled: {result}")
                    return
            self.store.set_order_status(o["id"], "filled" if ok else "failed",
                                        from_status="executing")
            if not ok:
                await self.notifier.send(f"⚠️ Order #{o['id']} triggered but didn't fill: "
                                         f"{result}", logging.WARNING)  # already escaped
        except Exception as e:  # never leave an order stuck in "executing"
            log.exception("order %s failed", o["id"])
            self.store.set_order_status(o["id"], "failed", from_status="executing")
            await self.notifier.send(f"⚠️ Order #{o['id']} failed: {esc(str(e)[:200])}",
                                     logging.WARNING)
        finally:
            self._orders_running.discard(o["id"])

    # ---------- runtime reconfiguration (Telegram) ----------

    async def set_setting(self, key: str, raw) -> str:
        if key not in BY_KEY:
            raise ValueError(f"unknown setting {key}")
        s = BY_KEY[key]
        value = parse_value(s, raw)
        if (key == "copytrade.enabled" and value and not self.has_pumpportal_key
                and not self.cfg.copytrade.rpc_watch):
            raise ValueError("Copy trading needs a PumpPortal API key (PUMPPORTAL_API_KEY in "
                             ".env) or the Backup wallet watcher on.")
        apply_setting(self.cfg, key, value)
        self.store.set_override(key, to_storable(s, value))
        await self._setting_changed(key)
        note = ""
        if key == "exits.kol_wallets" and value and not self.has_pumpportal_key:
            note = " (needs a PumpPortal API key to see their buys)"
        return f"{s.label}: {format_value(s, value)}{note}"

    async def _setting_changed(self, key: str) -> None:
        if key.startswith("discovery.pumpfun"):
            await self.stream.set_feeds(self.cfg.discovery.pumpfun_new_tokens,
                                        self.cfg.discovery.pumpfun_migrations)
        elif key == "copytrade.own_exits" and self.cfg.copytrade.own_exits:
            self._seed_copy_exits()
        elif key.startswith("copytrade."):
            before = set(self._copy)
            self._load_copy_wallets()
            for w in before - set(self._copy):
                await self.stream.unwatch_account(w)
            if self._copy:
                await self.stream.watch_accounts(list(self._copy))
        elif key == "trading.buy_amount_sol":
            self.safety.probe_sol = self.cfg.trading.buy_amount_sol
        elif key == "trading.slippage_pct" and isinstance(self.executor, PaperExecutor):
            self.executor.slippage_pct = self.cfg.trading.slippage_pct

    def _seed_copy_exits(self) -> None:
        """Copy exits switched on for the first time: start from the main exit settings,
        so nothing changes until you edit them."""
        import dataclasses
        if any(k.startswith("copyexits.") for k in self.store.overrides()):
            return
        for f in dataclasses.fields(self.cfg.copyexits):
            key = f"copyexits.{f.name}"
            if key in BY_KEY:
                value = getattr(self.cfg.exits, f.name)
                setattr(self.cfg.copyexits, f.name, copy.deepcopy(value))
                self.store.set_override(key, to_storable(BY_KEY[key], value))

    async def apply_preset(self, name: str) -> str:
        from .config import PRESETS, load_config
        if name not in PRESETS:  # never let load_config's startup exit end the running bot
            raise ValueError(f"unknown preset {name!r}")
        try:
            new = load_config(self.config_path, preset=name)
        except SystemExit as e:  # e.g. a typo in config.yaml: report it, never stop the bot
            raise ValueError(f"couldn't reload config.yaml: {e}") from None
        apply_overrides(new, self.store.overrides())
        update_in_place(self.cfg, new, skip={"private_key", "telegram_bot_token", "telegram_chat_id",
                                             "pumpportal_api_key", "jupiter_api_key",
                                             "withdraw_allowlist", "allow_key_export",
                                             "extra_allowed_programs",
                                             "data_dir", "endpoints"})
        self.store.set_setting("preset", name)
        for key in ("discovery.pumpfun_new_tokens", "copytrade.enabled", "trading.buy_amount_sol",
                    "trading.slippage_pct"):
            await self._setting_changed(key)
        n = len(self.store.overrides())
        return f"Preset {name} applied" + (f" (your {n} custom setting(s) still win)" if n else "")

    async def reset_settings(self) -> str:
        from .config import load_config
        try:  # check config.yaml loads before wiping anything: a typo must change nothing
            load_config(self.config_path, preset=self.cfg.preset)
        except SystemExit as e:
            raise ValueError(f"couldn't reload config.yaml: {e}") from None
        self.store.clear_overrides()
        return await self.apply_preset(self.cfg.preset)

    async def switch_mode(self, live: bool) -> str:
        # Hold the buy lock for the whole switch: a buy starting halfway through would
        # trade with one mode's executor while being booked in the other's ledger.
        async with self.buy_lock:
            if live == self.live:
                return f"Already in {self.mode.upper()} mode."
            open_pos = [p for p in self.positions.values() if not p.closed]
            if open_pos or self._buying or self.pending_buys():
                n = len(open_pos) + len(self._buying) + len(self.pending_buys())
                return f"Close your {n} open {self.mode} position(s) first (Positions → Sell 100%)."
            executor = self._build_executor(live)  # raises if no wallet
            old = list(self.positions)
            self.executor, self.live, self.mode = executor, live, "live" if live else "paper"
            self.store.mode = self.mode
            self.store.set_setting("mode", self.mode)
            self.positions.clear()
            self.sell_failures.clear()
            self._sell_next_try.clear()
            for mint in old:
                await self.stream.unwatch_token(mint)
            await self.restore()
            return f"Switched to {self.mode.upper()} mode."

    async def withdraw(self, to: str, amount: Optional[float]) -> str:
        """Send SOL out of the hot wallet. amount=None sends everything minus the fee."""
        solana_address(to)
        if amount is not None:
            _finite(amount)
        kp = self.wallet.keypair()
        if kp is None:
            raise ValueError("no wallet")
        if to == str(kp.pubkey()):
            raise ValueError("that's the bot's own address")
        if self.cfg.withdraw_allowlist and to not in self.cfg.withdraw_allowlist:
            raise ValueError("withdrawals are locked to the addresses in WITHDRAW_ALLOWLIST "
                             "(in .env on the server)")
        bal = await self.rpc.get_balance_sol(str(kp.pubkey()))
        bal_lamports = int(round(bal * 1e9))
        fee = 5000  # one signature, no priority fee
        lamports = bal_lamports - fee if amount is None else int(round(amount * 1e9))
        if lamports <= 0 or lamports + fee > bal_lamports:
            raise ValueError(f"balance is {bal:.6f} SOL")
        left = bal_lamports - lamports - fee
        if 0 < left < MIN_RENT_LAMPORTS:
            raise ValueError(f"that would leave {left / 1e9:.6f} SOL, below Solana's minimum of "
                             f"{MIN_RENT_LAMPORTS / 1e9:.6f}. Send a bit less, or use 'all'.")
        tx = transfer_tx(kp, to, lamports, await self.rpc.get_latest_blockhash())
        self._bal_cache = None  # the balance is about to change
        sol = lamports / 1e9
        sig = str(tx.signatures[0])
        try:
            await self.rpc.send_raw_transaction(bytes(tx))
        except Exception as e:  # it may still have reached the network
            raise ValueError(f"sending failed ({str(e)[:80]}). It may still go through: check "
                             f"https://solscan.io/tx/{sig} before trying again") from None
        try:
            confirmed: Optional[bool] = await self.rpc.confirm(sig)
        except TxFailed as e:  # landed and failed: nothing moved
            self.store.event("withdraw", to=to, sol=0.0, sig=sig, failed=True)
            return (f"❌ The withdrawal failed on-chain, so the SOL stayed in the wallet (only the "
                    f"network fee was spent) ({esc(str(e)[:120])})")
        except RpcError:
            confirmed = None  # outcome unknown
        if confirmed is False:  # the blockhash expired without it landing: nothing was sent
            return "❌ The withdrawal didn't go through; no SOL left the wallet. You can try again."
        self.store.event("withdraw", to=to, sol=sol, sig=sig, confirmed=bool(confirmed))
        if confirmed:
            return f"✅ Sent {sol:.9f} SOL to {to}\nhttps://solscan.io/tx/{sig}"
        return (f"⏳ Sent {sol:.9f} SOL to {to}, but it isn't confirmed yet. Check "
                f"https://solscan.io/tx/{sig} before withdrawing again, or it could be sent twice.")

    # ---------- lifecycle ----------

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, t: asyncio.Task) -> None:
        self._bg.discard(t)
        if not t.cancelled() and t.exception():
            log.error("background task failed", exc_info=t.exception())

    async def settle(self) -> None:
        """Wait for background work (exit checks, copy trades) to finish."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    async def housekeeping(self) -> None:
        """Keep memory flat for 24/7 operation, and send the daily report."""
        while True:
            await asyncio.sleep(300)
            self.prune()
            try:
                await self.daily_report()
            except Exception:
                log.exception("daily report failed")

    async def daily_report(self, now: Optional[float] = None) -> bool:
        """Once per UTC day, report yesterday's results. Returns True if one was sent."""
        from datetime import datetime, timedelta, timezone
        from .stats import format_summary, summarize
        now_dt = datetime.fromtimestamp(now or time.time(), timezone.utc)
        today = now_dt.date().isoformat()
        last = self.store.get_setting("last_report")
        if last is None:  # first run: start counting from today
            self.store.set_setting("last_report", today)
            return False
        if last >= today:  # ISO dates sort; also covers the clock stepping back a day
            return False
        self.store.set_setting("last_report", today)
        midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        start = (midnight - timedelta(days=1)).timestamp()
        s = summarize(self.store, start, midnight.timestamp())
        lines = [f"🗓 <b>Daily report</b> ({(midnight - timedelta(days=1)).date()}, {self.mode})"]
        lines.append(f"<pre>{esc(format_summary(s))}</pre>")
        if self.live:
            try:
                lines.append(f"Wallet: {await self.rpc.get_balance_sol(self.own_wallet):.4f} SOL")
            except Exception:
                pass
        open_n = sum(1 for p in self.positions.values() if not p.closed)
        lines.append(f"Open positions: {open_n}")
        await self.notifier.send("\n".join(lines))
        return True

    def prune(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        ttl = max(SEEN_TTL, self.cfg.discovery.max_candidate_age_seconds * 2)
        for key, ts in list(self.seen.items()):
            if now - ts > ttl:
                del self.seen[key]
        held = {m for m, p in self.positions.items() if not p.closed} | set(self.flows)
        for mint, ts in list(self._curve_ts.items()):
            if mint not in held and now - ts > CURVE_TTL:
                self.curves.pop(mint, None)
                del self._curve_ts[mint]
        repeat = self.cfg.discovery.momentum_repeat_minutes * 60
        mono = time.monotonic()
        for mint, ts in list(self._momentum_seen.items()):
            if mono - ts > repeat:
                del self._momentum_seen[mint]
        for mint in [m for m, p in self.positions.items() if p.closed]:
            lock = self.sell_locks.get(mint)
            if not (lock and lock.locked()):
                del self.positions[mint]
                self.sell_locks.pop(mint, None)
        self.store.prune_launches(now - 2 * 86400)

    def _scanning(self) -> bool:
        return self.scan_only or not self.paused

    async def _supervise(self, name: str, factory) -> None:
        """Restart a crashed loop instead of letting one bug stop the whole bot."""
        while True:
            try:
                await factory()
                log.warning("%s stopped; restarting", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s crashed; restarting in 5s", name)
            await asyncio.sleep(5)

    async def restore(self) -> None:
        """Reload open positions from the database after a restart."""
        for pos in self.store.open_positions():
            if self.live:
                try:
                    bal = await self.rpc.get_token_balance(self.own_wallet, pos.mint)
                    if bal <= 0:
                        self._clear_leftover(pos.mint)  # the wallet is empty: no old bag either
                    bal = max(0.0, bal - self._known_leftover(pos.mint))  # only this position's
                except Exception:
                    bal = pos.tokens_remaining
                empty = exits.is_dust(pos, bal)  # all of it (bar dust) left the wallet
                if bal < pos.tokens_remaining * 0.999:  # a sell landed while we were down
                    sold = pos.tokens_remaining - max(bal, 0.0)
                    if pos.pending_exit and not empty:  # book it as the exit it was (TP, ...)
                        pe = pos.pending_exit
                        exits.apply_fill(pos, exits.ExitDecision(
                            sold, False, pe.get("reason", "sell"), pe.get("tp_index"),
                            pe.get("kind", "")), sold, self._estimate_value(pos, sold),
                            self.exit_cfg(pos))
                    else:
                        pos.sol_out += self._estimate_value(pos, sold)
                pos.pending_exit = None
                if empty or pos.closed:
                    pos.tokens_remaining, pos.closed = 0.0, True
                    pos.close_reason = pos.close_reason or "sold before restart (PnL estimated)"
                    self.store.save_position(pos)
                    await self._closed(pos)
                    continue
                pos.tokens_remaining = min(bal, pos.tokens_remaining)  # never adopt unrelated tokens
                self.store.save_position(pos)
            pos.last_update = time.time()
            self.positions[pos.mint] = pos
            self.seen[("solana", pos.mint)] = time.time()
            if pos.route == "pump":
                await self.stream.watch_token(pos.mint)
        if self.positions:
            await self.notifier.send(f"♻️ restored {len(self.positions)} open position(s)")
        for oid in self.store.interrupted_orders():
            await self.notifier.send(f"⚠️ Order #{oid} was executing when the bot stopped, so it "
                                     "was not re-run. Check 📊 Positions to see if it filled.",
                                     logging.WARNING)

    async def run(self) -> None:
        d, e = self.cfg.discovery, self.cfg.endpoints
        mode = "SCAN-ONLY" if self.scan_only else self.mode.upper()
        self.store.prune_launches(time.time() - 2 * 86400)
        tg = None
        if self.cfg.telegram_bot_token and (
                self.telegram_ui or (self.notifier.enabled and self.cfg.notify.telegram_control)):
            from .telegram_bot import TelegramControl
            tg = TelegramControl(self, self.cfg.telegram_bot_token, self.cfg.telegram_chat_id, self.http)
        if tg is None and self.paused and not self.scan_only:
            # paused from Telegram earlier, but nothing here could resume it
            log.warning("ignoring the pause set from Telegram: no Telegram control in this mode")
            self.paused = False
        if self.telegram_ui and not self.live and self.store.get_setting("mode") == "live":
            try:  # resume live mode chosen from chat before the restart
                self.executor = self._build_executor(True)
                self.live, self.mode, self.store.mode = True, "live", "live"
                mode = "LIVE"
            except Exception as ex:
                log.warning("could not resume live mode: %s", ex)
        if not self.has_pumpportal_key:
            log.warning("No PUMPPORTAL_API_KEY: live trade stream off. Prices and dev-dump checks "
                        "use on-chain polling; copy trading, KOL and sell-pressure exits are off.")
            if self.cfg.copytrade.enabled:
                log.warning("copy trading is enabled but needs PUMPPORTAL_API_KEY; it won't fire")
        if "api.mainnet-beta.solana.com" in e.rpc_url:
            log.warning("Using the public Solana RPC: fine for testing, too slow and rate-limited "
                        "for live trading. Set SOLANA_RPC_URL.")
        await self.restore()
        if self._copy:
            await self.stream.watch_accounts(list(self._copy))
        who = f" wallet {self.own_wallet}" if self.live else ""
        paused = " — paused, tap /menu to start" if self.paused and not self.scan_only else ""
        await self.notifier.send(
            f"🚀 sniper starting in {mode} mode (preset {self.cfg.preset}){who}{paused}")

        loops = [("pumpportal", self.stream.run), ("exits", self.exit_loop),
                 ("prices", self.price_poller), ("housekeeping", self.housekeeping),
                 ("orders", self.order_loop), ("reconcile", self.reconcile_loop),
                 ("balance", self.balance_loop), ("keep-warm", self.keep_warm),
                 ("feed-watch", self.feed_watch), ("health", self._health_watch),
                 ("sol-price", self._sol_price_loop), ("copy-watch", self.copy_poller.run),
                 ("wallet-check", lambda: self.wallet_checker.loop(
                     lambda: list(self._copy), lambda: self.cfg.copytrade.check_days,
                     lambda: self.cfg.copytrade.check_every_hours))]
        # switched on/off live from Telegram (Settings → Snipers): no requests while off
        if d.geckoterminal_networks:
            gecko = GeckoTerminalScanner(e.geckoterminal_api, d.geckoterminal_networks,
                                         d.geckoterminal_poll_seconds, self.on_candidate, self.http,
                                         active=lambda: self._scanning() and d.geckoterminal_enabled)
            loops.append(("geckoterminal", gecko.run))
        dex = DexScreenerScanner(e.dexscreener_api, d.dexscreener_poll_seconds,
                                 self.on_candidate, self.http,
                                 active=lambda: self._scanning() and d.dexscreener_profiles)
        loops.append(("dexscreener", dex.run))
        # always running, idle until switched on (Telegram main menu → 🚀 Momentum)
        momentum = MomentumScanner(d, e.geckoterminal_api, e.dexscreener_api, self.http,
                                   self.on_momentum)
        loops.append(("momentum", momentum.run))
        loops += [(f"worker{i}", self.worker) for i in range(CANDIDATE_WORKERS)]
        if tg:  # logging the Telegram account in happens in the bot chat
            loops.append(("calls", self._calls_loop))
        if tg:
            loops.append(("telegram", tg.run))
        tasks = [asyncio.create_task(self._supervise(name, fn)) for name, fn in loops]
        try:
            await asyncio.gather(*tasks)
        finally:
            running = tasks + list(self._bg)
            for t in running:
                t.cancel()
            # let every task finish stopping (at an await, never mid-bookkeeping) before the
            # connections close; one that won't stop can't hold shutdown hostage
            if running:
                await asyncio.wait(running, timeout=5)
            open_pos = [p for p in self.positions.values() if not p.closed]
            if open_pos:
                log.warning("stopping with %d open position(s) — they resume on next start: %s",
                            len(open_pos), ", ".join(f"{p.symbol} {p.mint}" for p in open_pos))
            await self.http.aclose()
