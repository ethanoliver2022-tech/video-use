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
from .execution.executors import (CurveState, Executor, Jupiter, LiveExecutor, NothingToSell,
                                  NotLanded, PaperExecutor)
from .execution.sender import TxSender
from .execution.wallet import WalletManager, transfer_tx
from .intel import EarlyFlow
from .models import PUMP_TOTAL_SUPPLY, Candidate, Position
from .notify import Notifier
from .pump_curve import fetch_curve
from .safety import SafetyChecker
from .scanners.multichain import DexScreenerScanner, GeckoTerminalScanner
from .scanners.pumpportal import PumpPortalStream, trade_price
from .settings import (BY_KEY, apply_overrides, apply_setting, format_value, parse_value,
                       to_storable, update_in_place)
from .solana_rpc import SolanaRpc
from .store import Store

log = logging.getLogger("sniper")
esc = html.escape

PRICE_POLL_SECONDS = 2
QUOTE_GAP_SECONDS = 5        # poll on-chain / Jupiter when the trade stream has been quiet this long
SELL_BACKOFF_MAX = 60        # seconds between retries of a failing sell
WRITE_OFF_AFTER = 40         # failed sells (with backoff, ~30+ min) before giving a position up
ALERTS_PER_HOUR = 20         # cap on "other chain" alerts so Telegram never gets flooded
TRACKER_ALERTS_PER_HOUR = 60 # cap on wallet-tracker alerts
SEEN_TTL = 3 * 3600          # forget candidates after this long (memory stays flat 24/7)
CURVE_TTL = 15 * 60          # forget bonding-curve snapshots of tokens we don't hold
ATA_RENT_SOL = 0.0025        # rent for a new token account, roughly
ORDER_POLL_SECONDS = 3
MAX_OPEN_ORDERS = 20
USER_SOURCES = ("manual", "limit")  # user-initiated buys: allowed while auto-sniping is paused


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
        self.seen: dict[tuple[str, str], float] = {}
        self.positions: dict[str, Position] = {}
        self.curves: dict[str, CurveState] = {}
        self._curve_ts: dict[str, float] = {}
        self.flows: dict[str, EarlyFlow] = {}
        self.sell_locks: dict[str, asyncio.Lock] = {}
        self.sell_failures: dict[str, int] = {}
        self._sell_next_try: dict[str, float] = {}
        self._buying: dict[str, float] = {}       # mint -> SOL, buys in flight
        self.buy_lock = asyncio.Lock()
        self.last_loss_at = 0.0
        self._recent_sigs: deque[str] = deque(maxlen=5000)
        self._recent_sig_set: set[str] = set()
        self._alerts: deque[float] = deque()
        self._tracker_alerts: deque[float] = deque()
        self._bg: set[asyncio.Task] = set()
        self._orders_running: set[int] = set()

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
        if not live:
            self.own_wallet = ""
            return PaperExecutor(self.jupiter, self.cfg.trading.slippage_pct)
        kp = self.wallet.keypair()
        if kp is None:
            raise ValueError("no wallet yet — create or import one first")
        self.own_wallet = str(kp.pubkey())
        sender = TxSender(self.cfg.speed, self.rpc, self.http, self.cfg.trading.priority_fee_sol)
        return LiveExecutor(self.cfg, kp, self.rpc, self.jupiter, self.http, sender)

    def _load_copy_wallets(self) -> None:
        """Copy-mode wallets only while copy trading is on; alert-mode (tracked) wallets always."""
        self._copy.clear()
        enabled = self.cfg.copytrade.enabled
        if enabled:
            for w in self.cfg.copytrade.wallets:
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
        key = (c.chain, c.mint)
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
        text = f"{c.name} {c.symbol}".lower()
        for word in d.snipe_keywords:
            w = word.strip().lower()
            if w and w in text:
                c.trigger = f"keyword:{word.strip()}"
                return d.auto_snipe != "off"
        return d.auto_snipe == "all"

    async def worker(self) -> None:
        while True:
            c = await self.queue.get()
            try:
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
                return "❌ rejected: " + "; ".join(report.reasons)
            notes = "; ".join(report.notes)
        if c.chain != "solana":
            if self.cfg.notify.alert_other_chains and self._alert_allowed():
                liq = f" liq ${c.liquidity_usd:,.0f}" if c.liquidity_usd else ""
                await self.notifier.send(f"👀 {esc(tag)}{liq} via {c.source} — {esc(c.url or '')}")
            return "alert only (not Solana)"
        if self.scan_only:
            await self.notifier.send(f"✅ passes filters: {esc(tag)} ({esc(notes)}) {esc(c.url or '')}")
            return "passes filters"
        if c.source == "pumpfun" and self.cfg.entry.confirm_seconds > 0 and not c.force:
            problems = await self.confirm_flow(c)
            if problems:
                log.debug("skip %s after confirmation: %s", tag, "; ".join(problems))
                return "❌ confirmation failed: " + "; ".join(problems)
            notes += "; early flow confirmed"
        return await self.try_buy(c, notes)

    def _alert_allowed(self, q: Optional[deque] = None, per_hour: int = ALERTS_PER_HOUR) -> bool:
        q = self._alerts if q is None else q
        now = time.time()
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
                              mode: str = "copy") -> None:
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)  # validates
        if mode not in ("copy", "alert"):
            raise ValueError("mode must be copy or alert")
        if not self.has_trade_stream:
            raise ValueError("Copy trading and wallet tracking need a PumpPortal API key "
                             "(PUMPPORTAL_API_KEY in .env).")
        self.store.add_copy_wallet(address, label, buy_sol, True, mode)
        if mode == "copy" and not self.cfg.copytrade.enabled:
            await self.set_setting("copytrade.enabled", True)
        self._copy[address] = CopyWallet(address, label, buy_sol, True, mode)
        await self.stream.watch_accounts([address])

    async def set_wallet_mode(self, address: str, mode: str) -> None:
        w = next((x for x in self.store.copy_wallets() if x["address"] == address), None)
        if not w:
            raise ValueError("wallet not found")
        await self.add_copy_wallet(address, w["label"], w["buy_sol"], mode)

    async def remove_copy_wallet(self, address: str) -> bool:
        found = self._copy.pop(address, None) is not None
        found = self.store.remove_copy_wallet(address) or found
        await self.stream.unwatch_account(address)
        return found

    async def handle_copy(self, msg: dict, leader: CopyWallet) -> None:
        mint = msg["mint"]
        if leader.mode == "alert":
            await self._wallet_alert(msg, leader)
            return
        if msg.get("txType") != "buy" or mint in self.positions or mint in self._buying:
            return
        if self.paused or self.scan_only:
            return
        if float(msg.get("solAmount") or 0) < self.cfg.copytrade.min_leader_buy_sol:
            return
        c = Candidate(chain="solana", mint=mint, source="copy", symbol=mint[:6], route="pump",
                      leader=leader.address if leader.copy_sells else None,
                      buy_sol=leader.buy_sol or None, force=not self.cfg.copytrade.run_safety_checks,
                      url=f"https://pump.fun/coin/{mint}")
        name = leader.label or leader.address[:6]
        log.info("👥 %s bought %s (%.3f SOL) — copying", name, mint, float(msg.get("solAmount") or 0))
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
        sol = float(msg.get("solAmount") or 0)
        name = esc(w.label or w.address[:6])
        icon = "🟢" if side == "buy" else "🔴"
        buttons = [[(f"Buy {a:g}", f"b:{mint}:{a:g}") for a in (0.05, 0.1, 0.25)],
                   [("🔍 Token card", f"tc:{mint}")]]
        await self.notifier.send(f"🔔 {icon} <b>{name}</b> {'bought' if side == 'buy' else 'sold'} "
                                 f"{sol:.3f} SOL of <code>{mint}</code>", buttons=buttons)

    # ---------- entries ----------

    async def risk_block(self, sol: float) -> Optional[str]:
        t = self.cfg.trading
        open_n = sum(1 for p in self.positions.values()   # moonbags don't take up a slot
                     if not p.closed and not exits.in_moonbag(p, self.cfg.exits)) + len(self._buying)
        if open_n >= t.max_open_positions:
            return "max open positions"
        if -self.store.realized_today() >= t.daily_loss_limit_sol:
            return "daily loss limit hit"
        if t.cooldown_after_loss_seconds and time.time() - self.last_loss_at < t.cooldown_after_loss_seconds:
            return "cooling down after loss"
        if self.live:
            bal = await self.rpc.get_balance_sol(self.own_wallet)
            tip = self.cfg.speed.jito_tip_sol if self.cfg.speed.jito_enabled else 0.0
            needed = sol + sum(self._buying.values()) + tip + self.cfg.speed.max_priority_fee_sol \
                + ATA_RENT_SOL
            if bal - needed < t.min_sol_reserve:
                return f"balance too low ({bal:.4f} SOL)"
        return None

    async def try_buy(self, c: Candidate, notes: str = "") -> str:
        sol = c.buy_sol or self.cfg.trading.buy_amount_sol
        async with self.buy_lock:  # reserve a slot atomically, then trade without the lock
            if c.mint in self._buying or (c.mint in self.positions and not self.positions[c.mint].closed):
                return "already holding"
            blocked = await self.risk_block(sol)
            if blocked:
                log.info("skip %s %s: %s", c.symbol, c.mint, blocked)
                return f"skipped: {blocked}"
            self._buying[c.mint] = sol
        try:
            curve = self.curves.get(c.mint) if c.route == "pump" else None
            try:
                fill = await self.executor.buy(c, sol, curve)
            except NotLanded as e:
                await self.notifier.send(f"⌛ buy {esc(c.symbol)} didn't land: {esc(str(e))}",
                                         logging.WARNING)
                return f"buy didn't land: {e}"
            except Exception as e:
                await self.notifier.send(f"❌ buy failed {esc(c.symbol)} {c.mint}: {esc(str(e))}",
                                         logging.WARNING)
                return f"buy failed: {e}"
            if fill.tokens <= 0:
                await self.notifier.send(f"❌ buy {esc(c.symbol)} returned 0 tokens ({fill.signature})")
                return "buy returned 0 tokens"
            tag = c.trigger.split(":")[0]
            source = c.source + (f"/{tag}" if tag and tag != c.source else "")
            pos = Position(mint=c.mint, symbol=c.symbol or c.mint[:6], source=source,
                           creator=c.creator, entry_price=fill.sol / fill.tokens,
                           tokens_initial=fill.tokens, tokens_remaining=fill.tokens, sol_in=fill.sol,
                           route=c.route, leader=c.leader,
                           dev_tokens=c.creator_initial_buy_tokens or None)
            self.positions[c.mint] = pos
            self.store.save_position(pos)
        finally:
            self._buying.pop(c.mint, None)

        self.store.event("buy", c.mint, pos.symbol, source=c.source, sol=fill.sol,
                         tokens=fill.tokens, sig=fill.signature)
        why = {"dev": "👀 watched dev launched", "limit": "📋 limit order"}.get(
            c.trigger, f"🔑 {c.trigger.split(':', 1)[-1]}" if c.trigger.startswith("keyword") else "")
        text = (f"🟢 BUY <b>{esc(pos.symbol)}</b> {fill.sol:.4f} SOL → {fill.tokens:,.0f} tokens "
                f"({self.mode}, {c.source}) {esc(why)} {esc(notes)}\n<code>{c.mint}</code> "
                f"{esc(c.url or '')}")
        await self.notifier.send(text, buttons=[[("Sell 50%", f"s:{c.mint}:50"),
                                                 ("Sell 100%", f"s:{c.mint}:100")]])
        if c.route == "pump":
            await self.stream.watch_token(c.mint)
        return text

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
        if msg.get("vSolInBondingCurve") and msg.get("vTokensInBondingCurve"):
            self._set_curve(mint, CurveState(float(msg["vSolInBondingCurve"]),
                                             float(msg["vTokensInBondingCurve"])))
        if mint in self.flows:
            self.flows[mint].add(msg)
        leader = self._copy.get(msg.get("traderPublicKey", ""))
        if leader:
            self._spawn(self.handle_copy(msg, leader))
        pos = self.positions.get(mint)
        if not pos or pos.closed:
            return
        pool = msg.get("pool")
        if pool and pool != "pump" and not pos.migrated:  # trading on PumpSwap / an AMM now
            await self.on_migration(mint)
        price = trade_price(msg)
        if price:
            pos.update_price(price)
        exits.record_trade(pos, msg, self.kol_wallets, self.own_wallet)
        self._spawn(self.check_exit(pos))

    async def on_migration(self, mint: str) -> None:
        pos = self.positions.get(mint)
        if pos and not pos.closed and not pos.migrated:
            pos.migrated = True
            self.curves.pop(mint, None)
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
                    if curve is None or curve.complete:
                        await self.on_migration(pos.mint)  # graduated (or never on a curve)
                    else:
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
        while True:
            for pos in list(self.positions.values()):
                if not pos.closed:
                    self._spawn(self.check_exit(pos))  # one slow sell never delays the others
            await asyncio.sleep(1)

    async def check_exit(self, pos: Position) -> None:
        lock = self.sell_locks.get(pos.mint)
        if lock and lock.locked():
            return
        if time.time() < self._sell_next_try.get(pos.mint, 0):
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
            try:
                fill = await self.executor.sell(pos.mint, dec.tokens, dec.sell_all,
                                                pump=pos.route == "pump", curve=curve)
            except NothingToSell:
                pos.tokens_remaining, pos.closed = 0.0, True
                pos.close_reason = "no tokens left in wallet"
                self.store.save_position(pos)
                await self._closed(pos)
                return f"{esc(pos.symbol)}: nothing left in the wallet; position closed"
            except Exception as e:
                return await self._sell_failed(pos, e)
            self.sell_failures.pop(pos.mint, None)
            self._sell_next_try.pop(pos.mint, None)
            exits.apply_fill(pos, dec, fill.tokens or dec.tokens, fill.sol, self.cfg.exits)
            self.store.save_position(pos)
            self.store.event("sell", pos.mint, pos.symbol, reason=dec.reason, tokens=fill.tokens,
                             sol=fill.sol, sig=fill.signature)
            text = (f"🔴 SELL {esc(pos.symbol)} {'ALL' if dec.sell_all else f'{dec.tokens:,.0f}'} "
                    f"→ {fill.sol:.4f} SOL — {esc(dec.reason)} (pnl {pos.pnl_pct:+.0f}%)")
            await self.notifier.send(text)
            if pos.closed:
                await self._closed(pos)
            return text

    async def _sell_failed(self, pos: Position, err: Exception) -> str:
        """Never abandon a position on a transient failure: back off, reconcile, retry."""
        n = self.sell_failures[pos.mint] = self.sell_failures.get(pos.mint, 0) + 1
        self._sell_next_try[pos.mint] = time.time() + min(2 ** n, SELL_BACKOFF_MAX)
        if self.live:  # a sell that "failed" may still have landed: trust the wallet
            try:
                held = await self.rpc.get_token_balance(self.own_wallet, pos.mint)
                if held <= 0:
                    pos.tokens_remaining, pos.closed = 0.0, True
                    pos.close_reason = "sold (confirmed late)"
                    self.store.save_position(pos)
                    await self._closed(pos)
                    return f"{esc(pos.symbol)}: the sell landed late; position closed"
                if held < pos.tokens_remaining * 0.999:
                    pos.tokens_remaining = held
                    self.store.save_position(pos)
            except Exception as e:
                log.debug("balance reconcile failed: %s", e)
        if n in (1, 5) or n % 20 == 0:
            await self.notifier.send(f"⚠️ sell {esc(pos.symbol)} failed ({n}x), retrying: "
                                     f"{esc(str(err)[:300])}", logging.WARNING)
        if n >= WRITE_OFF_AFTER:
            pos.closed, pos.close_reason = True, "unsellable: written off"
            self.store.save_position(pos)
            await self._closed(pos)
            where = (" The tokens are still in your wallet: send /sell <mint> to try again "
                     "later.") if self.live else ""
            await self.notifier.send(f"🛑 {esc(pos.symbol)} couldn't be sold after {n} tries and was "
                                     f"written off.{esc(where)}", logging.ERROR)
        return f"sell failed: {esc(str(err)[:300])}"

    async def _closed(self, pos: Position) -> None:
        pnl = pos.realized_pnl_sol
        if pnl < 0:
            self.last_loss_at = time.time()
        if (pos.close_reason == "dev sold" and pnl < 0 and pos.creator
                and self.cfg.filters.auto_blocklist_ruggers):
            self.store.block(pos.creator, f"dev dumped {pos.symbol}")
        self.store.event("close", pos.mint, pos.symbol, reason=pos.close_reason, source=pos.source,
                         sol_in=pos.sol_in, sol_out=pos.sol_out, pnl_sol=pnl,
                         held_s=round(time.time() - pos.opened_at))
        self.sell_failures.pop(pos.mint, None)
        self._sell_next_try.pop(pos.mint, None)
        await self.notifier.send(f"🏁 closed {esc(pos.symbol)}: {pnl:+.4f} SOL ({esc(pos.close_reason)})"
                                 f" — today {self.store.realized_today():+.4f} SOL")
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
        q = await self.jupiter.quote("So11111111111111111111111111111111111111112", mint, probe,
                                     self.cfg.trading.slippage_pct)
        tokens = await self.jupiter.out_ui(q)
        if tokens <= 0:
            raise ValueError("no price available")
        return probe / tokens

    async def place_limit_buy(self, mint: str, sol: float, change_pct: float,
                              hours: float = 24.0) -> str:
        from solders.pubkey import Pubkey
        Pubkey.from_string(mint)
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
        for o in self.store.open_orders():
            if o["id"] in self._orders_running:
                continue
            if now >= o["expires"]:
                self.store.set_order_status(o["id"], "expired")
                await self.notifier.send(f"⌛ Order #{o['id']} expired")
                continue
            if o["side"] == "sell":
                pos = self.positions.get(o["mint"])
                if not pos or pos.closed:
                    self.store.set_order_status(o["id"], "cancelled")
                    continue
                price = pos.last_price
            else:
                if o["mint"] not in prices:
                    try:
                        prices[o["mint"]] = await self.price_of(o["mint"])
                    except Exception as e:
                        log.debug("order #%s price failed: %s", o["id"], e)
                        continue
                price = prices[o["mint"]]
            hit = price <= o["trigger_price"] if o["direction"] == "<=" else price >= o["trigger_price"]
            if hit:
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
            else:
                pos = self.positions.get(o["mint"])
                if not pos or pos.closed:
                    self.store.set_order_status(o["id"], "cancelled")
                    return
                dec = exits._partial(pos, pos.tokens_remaining * o["pct"] / 100, "limit sell")
                if o["pct"] >= 100 or dec is None:
                    dec = exits.ExitDecision(pos.tokens_remaining, True, "limit sell")
                result = await self.execute_sell(pos, dec)
                ok = result.startswith("🔴")
            self.store.set_order_status(o["id"], "filled" if ok else "failed")
            if not ok:
                await self.notifier.send(f"⚠️ Order #{o['id']} triggered but didn't fill: "
                                         f"{esc(result)}", logging.WARNING)
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
        from .config import load_config
        new = load_config(self.config_path, preset=name)
        apply_overrides(new, self.store.overrides())
        update_in_place(self.cfg, new, skip={"private_key", "telegram_bot_token", "telegram_chat_id",
                                             "pumpportal_api_key", "jupiter_api_key",
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
        if live == self.live:
            return f"Already in {self.mode.upper()} mode."
        open_pos = [p for p in self.positions.values() if not p.closed]
        if open_pos or self._buying:
            n = len(open_pos) + len(self._buying)
            return f"Close your {n} open {self.mode} position(s) first (Positions → Sell 100%)."
        self.executor = self._build_executor(live)  # raises if no wallet
        for mint in list(self.positions):
            await self.stream.unwatch_token(mint)
        self.live, self.mode = live, "live" if live else "paper"
        self.store.mode = self.mode
        self.store.set_setting("mode", self.mode)
        self.positions.clear()
        self.sell_failures.clear()
        self._sell_next_try.clear()
        await self.restore()
        return f"Switched to {self.mode.upper()} mode."

    async def withdraw(self, to: str, amount: Optional[float]) -> str:
        """Send SOL out of the hot wallet. amount=None sends everything minus the fee."""
        from solders.pubkey import Pubkey
        Pubkey.from_string(to)
        kp = self.wallet.keypair()
        if kp is None:
            raise ValueError("no wallet")
        if to == str(kp.pubkey()):
            raise ValueError("that's the bot's own address")
        bal = await self.rpc.get_balance_sol(str(kp.pubkey()))
        bal_lamports = int(round(bal * 1e9))
        fee = 5000  # one signature, no priority fee
        lamports = bal_lamports - fee if amount is None else int(round(amount * 1e9))
        if lamports <= 0 or lamports + fee > bal_lamports:
            raise ValueError(f"balance is {bal:.6f} SOL")
        tx = transfer_tx(kp, to, lamports, await self.rpc.get_latest_blockhash())
        sig = await self.rpc.send_raw_transaction(bytes(tx))
        confirmed = await self.rpc.confirm(sig)
        sol = lamports / 1e9
        self.store.event("withdraw", to=to, sol=sol, sig=sig)
        return (f"{'✅ Sent' if confirmed else '⏳ Submitted (not confirmed yet)'} {sol:.9f} SOL "
                f"to {to}\nhttps://solscan.io/tx/{sig}")

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
        if last == today:
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
                except Exception:
                    bal = pos.tokens_remaining
                if bal <= 0:
                    pos.closed, pos.close_reason = True, "not in wallet on restart"
                    self.store.save_position(pos)
                    continue
                pos.tokens_remaining = bal
            pos.last_update = time.time()
            self.positions[pos.mint] = pos
            self.seen[("solana", pos.mint)] = time.time()
            if pos.route == "pump":
                await self.stream.watch_token(pos.mint)
        if self.positions:
            await self.notifier.send(f"♻️ restored {len(self.positions)} open position(s)")

    async def run(self) -> None:
        d, e = self.cfg.discovery, self.cfg.endpoints
        mode = "SCAN-ONLY" if self.scan_only else self.mode.upper()
        self.store.prune_launches(time.time() - 2 * 86400)
        tg = None
        if self.cfg.telegram_bot_token and (
                self.telegram_ui or (self.notifier.enabled and self.cfg.notify.telegram_control)):
            from .telegram_bot import TelegramControl
            tg = TelegramControl(self, self.cfg.telegram_bot_token, self.cfg.telegram_chat_id, self.http)
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
                 ("orders", self.order_loop)]
        if d.geckoterminal_networks:
            gecko = GeckoTerminalScanner(e.geckoterminal_api, d.geckoterminal_networks,
                                         d.geckoterminal_poll_seconds, self.on_candidate, self.http,
                                         active=self._scanning)
            loops.append(("geckoterminal", gecko.run))
        if d.dexscreener_profiles:
            dex = DexScreenerScanner(e.dexscreener_api, d.dexscreener_poll_seconds,
                                     self.on_candidate, self.http, active=self._scanning)
            loops.append(("dexscreener", dex.run))
        loops += [(f"worker{i}", self.worker) for i in range(6)]
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
