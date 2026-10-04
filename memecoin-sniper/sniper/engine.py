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
SEEN_TTL = 3 * 3600          # forget candidates after this long (memory stays flat 24/7)
CURVE_TTL = 15 * 60          # forget bonding-curve snapshots of tokens we don't hold
ATA_RENT_SOL = 0.0025        # rent for a new token account, roughly


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
        self._bg: set[asyncio.Task] = set()

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
        self._copy.clear()
        if not self.cfg.copytrade.enabled:
            return
        for w in self.cfg.copytrade.wallets:
            self._copy[w.address] = w
        for w in self.store.copy_wallets():  # added from Telegram
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
        if c.v_sol and c.v_tokens:
            self._set_curve(c.mint, CurveState(c.v_sol, c.v_tokens))
        try:
            self.queue.put_nowait(c)
        except asyncio.QueueFull:
            log.debug("candidate queue full, dropping %s", c.mint)

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
        if self.paused and c.source != "manual" and not self.scan_only:
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

    def _alert_allowed(self) -> bool:
        now = time.time()
        while self._alerts and now - self._alerts[0] > 3600:
            self._alerts.popleft()
        if len(self._alerts) >= ALERTS_PER_HOUR:
            return False
        self._alerts.append(now)
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

    async def add_copy_wallet(self, address: str, label: str = "", buy_sol: float = 0.0) -> None:
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)  # validates
        if not self.has_trade_stream:
            raise ValueError("Copy trading needs a PumpPortal API key (PUMPPORTAL_API_KEY in .env).")
        self.store.add_copy_wallet(address, label, buy_sol, True)
        if not self.cfg.copytrade.enabled:
            await self.set_setting("copytrade.enabled", True)
        self._copy[address] = CopyWallet(address, label, buy_sol, True)
        await self.stream.watch_accounts([address])

    async def remove_copy_wallet(self, address: str) -> bool:
        found = self._copy.pop(address, None) is not None
        found = self.store.remove_copy_wallet(address) or found
        await self.stream.unwatch_account(address)
        return found

    async def handle_copy(self, msg: dict, leader: CopyWallet) -> None:
        mint = msg["mint"]
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

    # ---------- entries ----------

    async def risk_block(self, sol: float) -> Optional[str]:
        t = self.cfg.trading
        open_n = sum(1 for p in self.positions.values() if not p.closed) + len(self._buying)
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
            pos = Position(mint=c.mint, symbol=c.symbol or c.mint[:6], source=c.source,
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
        text = (f"🟢 BUY <b>{esc(pos.symbol)}</b> {fill.sol:.4f} SOL → {fill.tokens:,.0f} tokens "
                f"({self.mode}, {c.source}) {esc(notes)}\n<code>{c.mint}</code> {esc(c.url or '')}")
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
        if pct >= 100:
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
        """Keep memory flat for 24/7 operation."""
        while True:
            await asyncio.sleep(300)
            self.prune()

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
                 ("prices", self.price_poller), ("housekeeping", self.housekeeping)]
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
