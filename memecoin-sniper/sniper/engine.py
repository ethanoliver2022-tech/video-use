"""Wires scanners -> filters -> risk -> execution -> exit monitoring, plus copy trading,
persistence and the Telegram interface."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import deque
from typing import Optional

import httpx

from . import exits
from .config import Config, CopyWallet
from .execution.executors import (BuyUncertain, CurveState, Executor, Jupiter, LiveExecutor,
                                  NothingToSell, NotLanded, PaperExecutor)
from .execution.sender import TxSender
from .execution.wallet import WalletManager, transfer_tx
from .intel import EarlyFlow
from .models import PUMP_TOTAL_SUPPLY, SOL_MINT, Candidate, Position, num
from .notify import Notifier
from .pump_curve import fetch_curve
from .safety import SafetyChecker
from .scanners.multichain import DexScreenerScanner, GeckoTerminalScanner
from .scanners.pumpportal import PumpPortalStream, trade_price
from .settings import (BY_KEY, apply_overrides, apply_setting, format_value, parse_value,
                       to_storable, update_in_place)
from .solana_rpc import RpcError, SolanaRpc
from .store import Store

log = logging.getLogger("sniper")
esc = html.escape

PRICE_POLL_SECONDS = 2
QUOTE_GAP_SECONDS = 5        # poll on-chain / Jupiter when the trade stream has been quiet this long
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
ESTIMATE_HAIRCUT = 0.97  # unrecorded fills: last price minus typical fees/impact
MAX_QUEUE_WAIT = 60      # seconds a launch may wait for a free worker before it's too late
USER_SOURCES = ("manual", "limit")  # user-initiated buys: allowed while auto-sniping is paused


def _finite(*values: float) -> None:
    import math
    for v in values:
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("please send a normal number")


def _boottime() -> float:
    """Seconds since boot, counting time suspended (Linux); monotonic elsewhere."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


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
        self.http = httpx.AsyncClient(timeout=15, headers={"user-agent": "memecoin-sniper/0.4"})
        self.rpc = SolanaRpc(cfg.endpoints.rpc_url, self.http)
        self.jupiter = Jupiter(cfg.endpoints.jupiter_api, self.rpc, self.http, cfg.jupiter_api_key)
        self.store = Store(cfg.data_dir, self.mode)
        self.wallet = WalletManager(cfg.data_dir, cfg.private_key)
        bad = apply_overrides(cfg, self.store.overrides())  # settings changed from Telegram
        if bad:
            log.warning("ignoring invalid saved settings: %s", ", ".join(bad))
        self.safety = SafetyChecker(cfg.filters, self.rpc, self.http, cfg.endpoints.rugcheck_api,
                                    store=self.store, jupiter=self.jupiter,
                                    ipfs_gateway=cfg.endpoints.ipfs_gateway,
                                    probe_sol=cfg.trading.buy_amount_sol)
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

        self.queue: asyncio.Queue[Candidate] = asyncio.Queue(maxsize=500)
        self.seen: dict[tuple, float] = {}
        self.positions: dict[str, Position] = {}
        self.curves: dict[str, CurveState] = {}
        self._curve_ts: dict[str, float] = {}
        self.flows: dict[str, EarlyFlow] = {}
        self.sell_locks: dict[str, asyncio.Lock] = {}
        self.sell_failures: dict[str, int] = {}
        self._sell_next_try: dict[str, float] = {}
        self._buying: dict[str, float] = {}       # mint -> SOL, buys in flight
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
        self._scan_alerts: deque[float] = deque()
        self._bg: set[asyncio.Task] = set()
        self._orders_running: set[int] = set()
        self._curve_misses: dict[str, int] = {}

        saved_pause = self.store.get_setting("paused")
        self.paused = start_paused if saved_pause is None else saved_pause == "1"
        self._copy: dict[str, CopyWallet] = {}
        self._load_copy_wallets()
        self.telegram_ui = False  # set by `sniper bot`: Telegram is the whole interface

    @property
    def kol_wallets(self) -> set[str]:
        return set(self.cfg.exits.kol_wallets)

    @property
    def has_trade_stream(self) -> bool:
        return self.stream.trades_enabled

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
        return LiveExecutor(self.cfg, kp, self.rpc, self.jupiter, self.http, sender)

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
                await self.handle_candidate(c)
            except Exception:
                log.exception("error handling %s", c.mint)
            finally:
                self.queue.task_done()

    async def handle_candidate(self, c: Candidate) -> Optional[str]:
        """Filter, confirm and buy. Returns a human-readable outcome."""
        tag = f"[{c.chain}] {c.symbol or '?'} {c.mint}"
        if self.paused and c.source not in USER_SOURCES and not self.scan_only:
            return "paused"
        notes = ""
        if not c.force:
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
            result = await self.try_buy(c, notes)
            pos = self.positions.get(c.mint)
            if not pos or pos.closed:  # skipped/failed after watching: stop the (billed) feed
                await self.stream.unwatch_token(c.mint)
            return result
        return await self.try_buy(c, notes)

    def _alert_allowed(self, q: Optional[deque] = None, per_hour: int = ALERTS_PER_HOUR) -> bool:
        q = self._alerts if q is None else q
        now = time.monotonic()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= per_hour:
            return False
        q.append(now)
        return True

    async def confirm_flow(self, c: Candidate) -> list[str]:
        """Watch the first seconds of trading before committing (bundle / farm detection)."""
        if not self.has_trade_stream:
            return await self._confirm_onchain(c)
        flow = self.flows[c.mint] = EarlyFlow(creator=c.creator)
        await self.stream.watch_token(c.mint)
        try:
            await asyncio.sleep(self.cfg.entry.confirm_seconds)
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
        await asyncio.sleep(self.cfg.entry.confirm_seconds)
        try:
            curve = await fetch_curve(self.rpc, c.mint)
        except Exception as e:
            return [f"could not read bonding curve: {e}"]
        if not curve:
            return ["bonding curve not found"]
        if curve.complete:
            return ["already graduated"]
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
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)  # validates
        if mode not in ("copy", "alert"):
            raise ValueError("mode must be copy or alert")
        if not 0 <= buy_sol <= 100:
            raise ValueError("size per trade must be between 0 and 100 SOL (0 = your buy size)")
        if not self.has_trade_stream:
            raise ValueError("Copy trading and wallet tracking need a PumpPortal API key "
                             "(PUMPPORTAL_API_KEY in .env).")
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
        if (msg.get("txType") != "buy" or (held and not held.closed)  # a closed one may re-enter
                or mint in self._buying):
            return
        if self.paused or self.scan_only:
            return
        if (num(msg.get("solAmount"), allow_zero=True) or 0.0) < self.cfg.copytrade.min_leader_buy_sol:
            return
        c = Candidate(chain="solana", mint=mint, source="copy", symbol=mint[:6], route="pump",
                      leader=leader.address if leader.copy_sells else None,
                      buy_sol=leader.buy_sol or None, force=not self.cfg.copytrade.run_safety_checks,
                      url=f"https://pump.fun/coin/{mint}")
        name = leader.label or leader.address[:6]
        log.info("👥 %s bought %s (%.3f SOL) — copying", name, mint,
                 num(msg.get("solAmount"), allow_zero=True) or 0.0)
        result = await self.handle_candidate(c)
        if result and not result.startswith("🟢"):
            await self.notifier.send(f"👥 {esc(name)} bought {mint[:8]}… — not copied: {esc(result)}",
                                     telegram=False)

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

    # ---------- entries ----------

    async def risk_block(self, sol: float, bal: Optional[float] = None) -> Optional[str]:
        t = self.cfg.trading
        open_n = sum(1 for p in self.positions.values()   # moonbags don't take up a slot
                     if not p.closed and not exits.in_moonbag(p, self.cfg.exits)) \
            + len(set(self._buying) | set(self.pending_buys()))  # in-flight buys are also pending
        if open_n >= t.max_open_positions:
            return "max open positions"
        if t.daily_loss_limit_sol > 0 and -self.store.realized_today() >= t.daily_loss_limit_sol:
            return "daily loss limit hit"
        if (t.cooldown_after_loss_seconds and self.last_loss_at is not None
                and time.monotonic() - self.last_loss_at < t.cooldown_after_loss_seconds):
            return "cooling down after loss"
        if self.live:
            if bal is None:
                try:
                    bal = await self.rpc.get_balance_sol(self.own_wallet)
                except Exception as e:  # can't verify funds: don't buy blind
                    return f"couldn't check the wallet balance ({str(e)[:80]})"
            tip = self._tip_estimate()
            needed = sol + sum(self._buying.values()) + tip + self.cfg.speed.max_priority_fee_sol \
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
            blocked = await self.risk_block(sol, bal)  # in-flight buys are subtracted here
            if blocked:
                log.info("skip %s %s: %s", c.symbol, c.mint, blocked)
                return f"skipped: {blocked}"
            self._buying[c.mint] = sol
            if self.live:
                # write-ahead: if the bot dies anywhere from here on, the reconciler still
                # knows this buy may have landed and will adopt or expire it after restart
                tip = self._tip_estimate()
                self._add_pending(c, sol + tip)
        try:
            curve = await self._paper_curve(c) if not self.live and c.on_bonding_curve else None
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
                           route=c.route, leader=c.leader,
                           migrated=c.source == "pumpfun-migration",  # already off the curve
                           dev_tokens=c.creator_initial_buy_tokens or None)
            self.positions[c.mint] = pos
            self.store.save_position(pos)
            self.store.event("buy", c.mint, pos.symbol, source=c.source, sol=fill.sol,
                             tokens=fill.tokens, sig=fill.signature)
            self._drop_pending(c.mint)  # only after the position is safely on disk
        finally:
            self._buying.pop(c.mint, None)
            self._buys_finished += 1

        why = {"dev": "👀 watched dev launched", "limit": "📋 limit order"}.get(
            c.trigger, f"🔑 {c.trigger.split(':', 1)[-1]}" if c.trigger.startswith("keyword") else "")
        text = (f"🟢 BUY <b>{esc(pos.symbol)}</b> {fill.sol:.4f} SOL → {fill.tokens:,.0f} tokens "
                f"({self.mode}, {c.source}) {esc(why)} {esc(notes)}\n<code>{c.mint}</code> "
                f"{esc(c.url or '')}")
        if c.route == "pump":  # exits first: a slow Telegram must never delay the feed
            await self.stream.watch_token(c.mint)
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

    async def _paper_curve(self, c: Candidate) -> Optional[CurveState]:
        """Paper fills price off the bonding curve: it must be current. A snapshot from the
        launch minutes ago (a manual buy of a token that has since run) would fake a fill."""
        curve = self.curves.get(c.mint)
        if curve and time.time() - self._curve_ts.get(c.mint, 0) <= PAPER_CURVE_MAX_AGE:
            return curve
        try:
            info = await fetch_curve(self.rpc, c.mint)
        except Exception as e:
            log.debug("curve refresh %s failed: %s", c.mint, e)
            info = None
        if info and not info.complete:
            fresh = CurveState(info.v_sol, info.v_tokens)
            self._set_curve(c.mint, fresh)
            return fresh
        return None  # no current curve: price it with a Jupiter quote instead

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
        v_sol, v_tok = num(msg.get("vSolInBondingCurve")), num(msg.get("vTokensInBondingCurve"))
        if v_sol and v_tok:
            self._set_curve(mint, CurveState(v_sol, v_tok))
        if mint in self.flows:
            self.flows[mint].add(msg)
        leader = self._copy.get(msg.get("traderPublicKey", ""))
        if leader:
            self._spawn(self.handle_copy(msg, leader))
        pos = self.positions.get(mint)
        if not pos or pos.closed:
            return
        pool = msg.get("pool")
        if isinstance(pool, str) and pool and pool != "pump" and not pos.migrated:  # trading on PumpSwap / an AMM now
            await self.on_migration(mint)
        price = trade_price(msg)
        if price:
            pos.update_price(price)
        exits.record_trade(pos, msg, self.kol_wallets, self.own_wallet)
        self._spawn(self.check_exit(pos))

    async def on_migration(self, mint: str) -> None:
        self.curves.pop(mint, None)  # graduated: that curve no longer prices anything
        pos = self.positions.get(mint)
        if pos and not pos.closed and not pos.migrated:
            pos.migrated = True
            if self.cfg.exits.sell_on_migration:  # runs in the websocket loop: don't wait here
                self._spawn(self.execute_sell(
                    pos, exits.ExitDecision(pos.tokens_remaining, True, "migrated")))
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
                        pos.update_price(curve.price)
                if not self.has_trade_stream and pos.creator and self.cfg.exits.exit_on_dev_sell:
                    dev = await self.rpc.get_token_balance(pos.creator, pos.mint)
                    if pos.dev_tokens is None:
                        pos.dev_tokens = dev
                    elif dev < pos.dev_tokens * 0.99:
                        pos.dev_sold = True
            elif quiet and pos.tokens_remaining > 0:
                out = await self.executor.quote_sell(pos.mint, pos.tokens_remaining)
                if out:
                    pos.update_price(out / pos.tokens_remaining)
        except Exception as e:
            log.debug("price poll %s failed: %s", pos.symbol, e)

    # ---------- exits ----------

    async def exit_loop(self) -> None:
        offset = time.time() - time.monotonic()
        step_offset = time.time() - _boottime()
        while True:
            # Position timestamps are wall-clock. A clock *step* (NTP fix, VM migration) moves
            # them all without any real time passing: shift them with it, or max-hold and
            # stale exits fire on healthy tokens. A suspend/resume is real time (max hold may
            # rightly expire), but prices weren't polled: give the feeds a fresh window.
            new_step = time.time() - _boottime()
            if abs(new_step - step_offset) > CLOCK_JUMP_SECONDS:
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
        dec = exits.evaluate(pos, self.cfg.exits)
        if dec:
            await self.execute_sell(pos, dec)

    async def execute_sell(self, pos: Position, dec: exits.ExitDecision) -> str:
        lock = self.sell_locks.setdefault(pos.mint, asyncio.Lock())
        if lock.locked():
            return "a sell is already in progress"
        async with lock:
            if pos.closed:
                return "already closed"
            if dec.tokens <= 0 and not dec.sell_all:  # bookkeeping only (e.g. TP above a moonbag)
                exits.apply_fill(pos, dec, 0.0, 0.0, self.cfg.exits)
                self.store.save_position(pos)
                return "nothing to sell"
            curve = self.curves.get(pos.mint) if not pos.migrated else None
            if self.live:
                pos.pending_exit = {"reason": dec.reason, "tp_index": dec.tp_index,
                                    "kind": dec.kind}
                self.store.save_position(pos)
            try:
                extra = {"value_sol": dec.tokens * pos.last_price} \
                    if isinstance(self.executor, LiveExecutor) else {}  # caps the tx guard
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
            exits.apply_fill(pos, dec, fill.tokens, fill.sol, self.cfg.exits)
            pos.pending_exit = None
            self.store.save_position(pos)
            self.store.event("sell", pos.mint, pos.symbol, reason=dec.reason, tokens=fill.tokens,
                             sol=fill.sol, sig=fill.signature)
            text = (f"🔴 SELL {esc(pos.symbol)} {'ALL' if dec.sell_all else f'{dec.tokens:,.0f}'} "
                    f"→ {fill.sol:.4f} SOL{' (estimated)' if estimated else ''} — "
                    f"{esc(dec.reason)} (pnl {pos.pnl_pct:+.0f}%)")
            self._spawn(self.notifier.send(text))  # never hold the sell lock waiting on Telegram
            if pos.closed:
                await self._closed(pos)
            return text

    def _sell_slippage(self, pos: Position, dec: exits.ExitDecision) -> float:
        """A token crashing through a rug blows past normal slippage, so each failed attempt
        allows more, and emergency exits start higher."""
        base = self.cfg.trading.slippage_pct
        if dec.reason.startswith(DANGER_EXITS):
            base *= 1.5
        n = self.sell_failures.get(pos.mint, 0)
        # never below what buys use: someone running 80% slippage gets at least 80% on exits
        cap = min(100.0, max(MAX_SELL_SLIPPAGE, self.cfg.trading.slippage_pct))
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
                if held <= 0:
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
                    exits.apply_fill(pos, done, sold, self._estimate_value(pos, sold),
                                     self.cfg.exits)
                    self.store.save_position(pos)
                    self.sell_failures.pop(pos.mint, None)
                    self._sell_next_try.pop(pos.mint, None)
                    text = (f"🔴 SELL {esc(pos.symbol)} {sold:,.0f} — {esc(done.reason)}: it landed "
                            "late (PnL estimated)")
                    self._spawn(self.notifier.send(text))
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

    async def _closed(self, pos: Position) -> None:
        pnl = pos.realized_pnl_sol
        if pnl < 0:
            self.last_loss_at = time.monotonic()
        if (pos.close_reason == "dev sold" and pnl < 0 and pos.creator
                and self.cfg.filters.auto_blocklist_ruggers):
            self.store.block(pos.creator, f"dev dumped {pos.symbol}")
        self.store.event("close", pos.mint, pos.symbol, reason=pos.close_reason, source=pos.source,
                         sol_in=pos.sol_in, sol_out=pos.sol_out, pnl_sol=pnl,
                         held_s=round(time.time() - pos.opened_at))
        self.sell_failures.pop(pos.mint, None)
        self._sell_next_try.pop(pos.mint, None)
        self._curve_misses.pop(pos.mint, None)
        self._spawn(self.notifier.send(  # callers may hold the sell lock: don't wait on Telegram
            f"🏁 closed {esc(pos.symbol)}: {pnl:+.4f} SOL ({esc(pos.close_reason)})"
            f" — today {self.store.realized_today():+.4f} SOL"))
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
        q = await self.jupiter.quote(SOL_MINT, mint, probe,
                                     self.cfg.trading.slippage_pct)
        tokens = await self.jupiter.out_ui(q)
        if tokens <= 0:
            raise ValueError("no price available")
        return probe / tokens

    async def place_limit_buy(self, mint: str, sol: float, change_pct: float,
                              hours: float = 24.0) -> str:
        from solders.pubkey import Pubkey
        Pubkey.from_string(mint)
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
        if key == "copytrade.enabled" and value and not self.has_trade_stream:
            raise ValueError("Copy trading needs a PumpPortal API key (PUMPPORTAL_API_KEY in .env).")
        apply_setting(self.cfg, key, value)
        self.store.set_override(key, to_storable(s, value))
        await self._setting_changed(key)
        note = ""
        if key == "exits.kol_wallets" and value and not self.has_trade_stream:
            note = " (needs a PumpPortal API key to see their buys)"
        return f"{s.label}: {format_value(s, value)}{note}"

    async def _setting_changed(self, key: str) -> None:
        if key.startswith("discovery.pumpfun"):
            await self.stream.set_feeds(self.cfg.discovery.pumpfun_new_tokens,
                                        self.cfg.discovery.pumpfun_migrations)
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

    async def apply_preset(self, name: str) -> str:
        from .config import PRESETS, load_config
        if name not in PRESETS:  # never let load_config's startup exit end the running bot
            raise ValueError(f"unknown preset {name!r}")
        new = load_config(self.config_path, preset=name)
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
        from solders.pubkey import Pubkey
        Pubkey.from_string(to)
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
                if bal < pos.tokens_remaining * 0.999:  # a sell landed while we were down
                    sold = pos.tokens_remaining - max(bal, 0.0)
                    if pos.pending_exit and bal > 0:  # book it as the exit it was (TP, ...)
                        pe = pos.pending_exit
                        exits.apply_fill(pos, exits.ExitDecision(
                            sold, False, pe.get("reason", "sell"), pe.get("tp_index"),
                            pe.get("kind", "")), sold, self._estimate_value(pos, sold),
                            self.cfg.exits)
                    else:
                        pos.sol_out += self._estimate_value(pos, sold)
                pos.pending_exit = None
                if bal <= 0:
                    pos.tokens_remaining, pos.closed = 0.0, True
                    pos.close_reason = "sold before restart (PnL estimated)"
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
        if not self.has_trade_stream:
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
                 ("balance", self.balance_loop)]
        if d.geckoterminal_networks:
            gecko = GeckoTerminalScanner(e.geckoterminal_api, d.geckoterminal_networks,
                                         d.geckoterminal_poll_seconds, self.on_candidate, self.http,
                                         active=self._scanning)
            loops.append(("geckoterminal", gecko.run))
        if d.dexscreener_profiles:
            dex = DexScreenerScanner(e.dexscreener_api, d.dexscreener_poll_seconds,
                                     self.on_candidate, self.http, active=self._scanning)
            loops.append(("dexscreener", dex.run))
        loops += [(f"worker{i}", self.worker) for i in range(CANDIDATE_WORKERS)]
        if tg:
            loops.append(("telegram", tg.run))
        tasks = [asyncio.create_task(self._supervise(name, fn)) for name, fn in loops]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks + list(self._bg):
                t.cancel()
            open_pos = [p for p in self.positions.values() if not p.closed]
            if open_pos:
                log.warning("stopping with %d open position(s) — they resume on next start: %s",
                            len(open_pos), ", ".join(f"{p.symbol} {p.mint}" for p in open_pos))
            await self.http.aclose()
