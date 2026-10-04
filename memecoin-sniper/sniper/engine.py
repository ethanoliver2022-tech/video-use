"""Wires scanners -> filters -> risk -> execution -> exit monitoring, plus copy trading,
persistence and the Telegram control panel."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import httpx

from . import exits
from .config import Config, CopyWallet
from .execution.executors import CurveState, Executor, Jupiter, LiveExecutor, PaperExecutor
from .execution.sender import TxSender
from .execution.wallet import load_keypair
from .intel import EarlyFlow
from .models import Candidate, Position
from .notify import Notifier
from .safety import SafetyChecker
from .scanners.multichain import DexScreenerScanner, GeckoTerminalScanner
from .scanners.pumpportal import PumpPortalStream, trade_price
from .solana_rpc import SolanaRpc
from .store import Store

log = logging.getLogger("sniper")

SELL_RETRY_LIMIT = 5
QUOTE_GAP_SECONDS = 5  # poll a Jupiter quote when the trade stream has been quiet this long


class Engine:
    def __init__(self, cfg: Config, live: bool = False, scan_only: bool = False):
        self.cfg, self.live, self.scan_only = cfg, live, scan_only
        self.mode = "live" if live else "paper"
        self.http = httpx.AsyncClient(timeout=15, headers={"user-agent": "memecoin-sniper/0.2"})
        self.rpc = SolanaRpc(cfg.endpoints.rpc_url, self.http)
        self.jupiter = Jupiter(cfg.endpoints.jupiter_api, self.rpc, self.http)
        self.store = Store(cfg.data_dir, self.mode)
        self.safety = SafetyChecker(cfg.filters, self.rpc, self.http, cfg.endpoints.rugcheck_api,
                                    store=self.store, jupiter=self.jupiter,
                                    ipfs_gateway=cfg.endpoints.ipfs_gateway,
                                    probe_sol=cfg.trading.buy_amount_sol)
        self.notifier = Notifier(self.http, cfg.telegram_bot_token if cfg.notify.telegram else "",
                                 cfg.telegram_chat_id)
        self.own_wallet = ""
        self.executor: Executor
        if live:
            kp = load_keypair(cfg.private_key)
            self.own_wallet = str(kp.pubkey())
            sender = TxSender(cfg.speed, self.rpc, self.http, cfg.trading.priority_fee_sol)
            self.executor = LiveExecutor(cfg, kp, self.rpc, self.jupiter, self.http, sender)
        else:
            self.executor = PaperExecutor(self.jupiter, cfg.trading.slippage_pct)

        d = cfg.discovery
        self.stream = PumpPortalStream(cfg.endpoints.pumpportal_ws, d.pumpfun_new_tokens,
                                       d.pumpfun_migrations)
        self.stream.on_candidate = self.on_candidate
        self.stream.on_trade = self.on_trade
        self.stream.on_migration = self.on_migration

        self.queue: asyncio.Queue[Candidate] = asyncio.Queue(maxsize=500)
        self.seen: set[tuple[str, str]] = set()
        self.positions: dict[str, Position] = {}
        self.curves: dict[str, CurveState] = {}
        self.flows: dict[str, EarlyFlow] = {}
        self.sell_locks: dict[str, asyncio.Lock] = {}
        self.sell_failures: dict[str, int] = {}
        self.buy_lock = asyncio.Lock()
        self.kol_wallets = set(cfg.exits.kol_wallets)
        self.last_loss_at = 0.0
        self._recent_sigs: deque[str] = deque(maxlen=2000)
        self._recent_sig_set: set[str] = set()
        self._bg: set[asyncio.Task] = set()

        # runtime settings changed from Telegram survive restarts
        if (v := self.store.get_setting("buy_amount_sol")):
            cfg.trading.buy_amount_sol = float(v)
        self.paused = self.store.get_setting("paused") == "1"
        self._copy: dict[str, CopyWallet] = {}
        if cfg.copytrade.enabled:
            for w in cfg.copytrade.wallets:
                self._copy[w.address] = w
        for w in self.store.copy_wallets():  # added from Telegram
            self._copy[w["address"]] = CopyWallet(**w)

    # ---------- discovery ----------

    async def on_candidate(self, c: Candidate) -> None:
        if c.source == "pumpfun" and c.creator:
            self.store.record_launch(c.mint, c.creator)
        key = (c.chain, c.mint)
        if key in self.seen:
            return
        self.seen.add(key)
        if c.age_seconds > self.cfg.discovery.max_candidate_age_seconds:
            return
        if c.v_sol and c.v_tokens:
            self.curves[c.mint] = CurveState(c.v_sol, c.v_tokens)
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
        notes = ""
        if not c.force:
            report = await self.safety.evaluate(c)
            if not report.passed:
                log.debug("reject %s: %s", tag, "; ".join(report.reasons))
                return "❌ rejected: " + "; ".join(report.reasons)
            notes = "; ".join(report.notes)
        if c.chain != "solana":
            if self.cfg.notify.alert_other_chains:
                liq = f" liq ${c.liquidity_usd:,.0f}" if c.liquidity_usd else ""
                await self.notifier.send(f"👀 {tag}{liq} via {c.source} — {c.url or ''}")
            return "alert only (not Solana)"
        if self.scan_only:
            await self.notifier.send(f"✅ passes filters: {tag} ({notes}) {c.url or ''}")
            return "passes filters"
        if self.paused and c.source != "manual":
            return "paused"
        if c.source == "pumpfun" and self.cfg.entry.confirm_seconds > 0 and not c.force:
            problems = await self.confirm_flow(c)
            if problems:
                log.debug("skip %s after confirmation: %s", tag, "; ".join(problems))
                return "❌ confirmation failed: " + "; ".join(problems)
            notes += "; early flow confirmed"
        return await self.try_buy(c, notes)

    async def confirm_flow(self, c: Candidate) -> list[str]:
        """Watch the first seconds of trading before committing (bundle / farm detection)."""
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

    # ---------- copy trading ----------

    def copy_wallets(self) -> list[CopyWallet]:
        return list(self._copy.values())

    async def add_copy_wallet(self, address: str, label: str = "", buy_sol: float = 0.0) -> None:
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)  # validates
        self._copy[address] = CopyWallet(address, label, buy_sol, True)
        self.store.add_copy_wallet(address, label, buy_sol, True)
        await self.stream.watch_accounts([address])

    async def remove_copy_wallet(self, address: str) -> bool:
        found = self._copy.pop(address, None) is not None
        found = self.store.remove_copy_wallet(address) or found
        await self.stream.unwatch_account(address)
        return found

    async def handle_copy(self, msg: dict, leader: CopyWallet) -> None:
        mint = msg["mint"]
        if msg.get("txType") != "buy" or mint in self.positions or self.paused or self.scan_only:
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
            await self.notifier.send(f"👥 {name} bought {mint[:8]}… — not copied: {result}",
                                     telegram=False)

    # ---------- entries ----------

    async def risk_block(self, sol: float) -> Optional[str]:
        t = self.cfg.trading
        open_n = sum(1 for p in self.positions.values() if not p.closed)
        if open_n >= t.max_open_positions:
            return "max open positions"
        if -self.store.realized_today() >= t.daily_loss_limit_sol:
            return "daily loss limit hit"
        if t.cooldown_after_loss_seconds and time.time() - self.last_loss_at < t.cooldown_after_loss_seconds:
            return "cooling down after loss"
        if self.live:
            bal = await self.rpc.get_balance_sol(self.own_wallet)
            if bal - sol - self.cfg.speed.jito_tip_sol - t.priority_fee_sol < t.min_sol_reserve:
                return f"balance too low ({bal:.4f} SOL)"
        return None

    async def try_buy(self, c: Candidate, notes: str = "") -> str:
        sol = c.buy_sol or self.cfg.trading.buy_amount_sol
        async with self.buy_lock:  # serialise so risk limits can't be raced
            if c.mint in self.positions and not self.positions[c.mint].closed:
                return "already holding"
            blocked = await self.risk_block(sol)
            if blocked:
                log.info("skip %s %s: %s", c.symbol, c.mint, blocked)
                return f"skipped: {blocked}"
            curve = self.curves.get(c.mint) if c.route == "pump" else None
            try:
                fill = await self.executor.buy(c, sol, curve)
            except Exception as e:
                await self.notifier.send(f"❌ buy failed {c.symbol} {c.mint}: {e}", logging.WARNING)
                return f"buy failed: {e}"
            if fill.tokens <= 0:
                await self.notifier.send(f"❌ buy {c.symbol} returned 0 tokens ({fill.signature})")
                return "buy returned 0 tokens"
            pos = Position(mint=c.mint, symbol=c.symbol or c.mint[:6], source=c.source,
                           creator=c.creator, entry_price=fill.sol / fill.tokens,
                           tokens_initial=fill.tokens, tokens_remaining=fill.tokens, sol_in=fill.sol,
                           route=c.route, leader=c.leader)
            self.positions[c.mint] = pos
            self.store.save_position(pos)

        self.store.event("buy", c.mint, pos.symbol, source=c.source, sol=fill.sol,
                         tokens=fill.tokens, sig=fill.signature)
        text = (f"🟢 BUY <b>{pos.symbol}</b> {fill.sol:.4f} SOL → {fill.tokens:,.0f} tokens "
                f"({self.mode}, {c.source}) {notes}\n<code>{c.mint}</code> {c.url or ''}")
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
        if self._dup(msg):  # token + account subscriptions can both deliver the same trade
            return
        mint = msg["mint"]
        if msg.get("vSolInBondingCurve") and msg.get("vTokensInBondingCurve"):
            self.curves[mint] = CurveState(float(msg["vSolInBondingCurve"]),
                                           float(msg["vTokensInBondingCurve"]))
        if mint in self.flows:
            self.flows[mint].add(msg)
        leader = self._copy.get(msg.get("traderPublicKey", ""))
        if leader:
            self._spawn(self.handle_copy(msg, leader))
        pos = self.positions.get(mint)
        if not pos or pos.closed:
            return
        price = trade_price(msg)
        if price:
            pos.update_price(price)
        exits.record_trade(pos, msg, self.kol_wallets, self.own_wallet)
        await self.check_exit(pos)

    async def on_migration(self, mint: str) -> None:
        pos = self.positions.get(mint)
        if pos and not pos.closed:
            pos.migrated = True
            self.curves.pop(mint, None)
            self.store.save_position(pos)
            await self.notifier.send(f"🎓 {pos.symbol} graduated off the bonding curve")

    async def quote_poller(self) -> None:
        """Fills price gaps (non-pump tokens, quiet streams) by quoting the actual exit."""
        while True:
            now = time.time()
            for pos in list(self.positions.values()):
                if pos.closed or now - pos.last_update < QUOTE_GAP_SECONDS:
                    continue
                if pos.route == "pump" and not pos.migrated and pos.mint in self.curves:
                    continue  # bonding curve: the trade stream is the price
                try:
                    out = await self.executor.quote_sell(pos.mint, pos.tokens_remaining)
                    if out:
                        pos.update_price(out / pos.tokens_remaining)
                except Exception as e:
                    log.debug("quote %s failed: %s", pos.symbol, e)
            await asyncio.sleep(3)

    # ---------- exits ----------

    async def exit_loop(self) -> None:
        while True:
            for pos in list(self.positions.values()):
                if not pos.closed:
                    await self.check_exit(pos)
            await asyncio.sleep(1)

    async def check_exit(self, pos: Position) -> None:
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
            except Exception as e:
                n = self.sell_failures[pos.mint] = self.sell_failures.get(pos.mint, 0) + 1
                await self.notifier.send(f"⚠️ sell {pos.symbol} failed ({n}x): {e}", logging.WARNING)
                if n >= SELL_RETRY_LIMIT:
                    pos.closed, pos.close_reason = True, "sell failed repeatedly — check wallet"
                    self.store.save_position(pos)
                    await self.notifier.send(f"🛑 giving up on {pos.symbol}; sell manually", logging.ERROR)
                return f"sell failed: {e}"
            self.sell_failures.pop(pos.mint, None)
            exits.apply_fill(pos, dec, fill.tokens or dec.tokens, fill.sol, self.cfg.exits)
            self.store.save_position(pos)
            self.store.event("sell", pos.mint, pos.symbol, reason=dec.reason, tokens=fill.tokens,
                             sol=fill.sol, sig=fill.signature)
            text = (f"🔴 SELL {pos.symbol} {'ALL' if dec.sell_all else f'{dec.tokens:,.0f}'} "
                    f"→ {fill.sol:.4f} SOL — {dec.reason} (pnl {pos.pnl_pct:+.0f}%)")
            await self.notifier.send(text)
            if pos.closed:
                await self._closed(pos)
            return text

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
        await self.notifier.send(f"🏁 closed {pos.symbol}: {pnl:+.4f} SOL ({pos.close_reason}) — "
                                 f"today {self.store.realized_today():+.4f} SOL")
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
            return f"no open position for {key}"
        pct = min(max(pct, 1.0), 100.0)
        dec = exits._partial(pos, pos.tokens_remaining * pct / 100, "manual")
        if pct >= 100:
            dec = exits.ExitDecision(pos.tokens_remaining, True, "manual")
        return await self.execute_sell(pos, dec)

    async def manual_buy(self, mint: str, sol: Optional[float], force: bool = False) -> str:
        from solders.pubkey import Pubkey
        try:
            Pubkey.from_string(mint)
        except ValueError:
            return "invalid mint address"
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
                lines.append(f"<b>Wallet:</b> balance unavailable ({e})")
        lines += [
            f"<b>Buy size:</b> {self.cfg.trading.buy_amount_sol} SOL · "
            f"Jito {'on' if self.cfg.speed.jito_enabled else 'off'}",
            f"<b>Open:</b> {len(open_pos)}/{self.cfg.trading.max_open_positions}",
            f"<b>Realized today:</b> {self.store.realized_today():+.4f} SOL",
            f"<b>Copying:</b> {len(self._copy)} wallet(s)",
        ]
        return "\n".join(lines)

    # ---------- lifecycle ----------

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

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
            self.seen.add(("solana", pos.mint))
            if pos.route == "pump":
                await self.stream.watch_token(pos.mint)
        if self.positions:
            await self.notifier.send(f"♻️ restored {len(self.positions)} open position(s)")

    async def run(self) -> None:
        d, e = self.cfg.discovery, self.cfg.endpoints
        mode = "SCAN-ONLY" if self.scan_only else self.mode.upper()
        who = f" wallet {self.own_wallet}" if self.live else ""
        self.store.prune_launches(time.time() - 2 * 86400)
        await self.restore()
        if self._copy:
            await self.stream.watch_accounts(list(self._copy))
        await self.notifier.send(f"🚀 sniper starting in {mode} mode (preset {self.cfg.preset}){who}")
        tasks = [asyncio.create_task(self.stream.run())]
        if d.geckoterminal_networks:
            tasks.append(asyncio.create_task(GeckoTerminalScanner(
                e.geckoterminal_api, d.geckoterminal_networks, d.geckoterminal_poll_seconds,
                self.on_candidate, self.http).run()))
        if d.dexscreener_profiles:
            tasks.append(asyncio.create_task(DexScreenerScanner(
                e.dexscreener_api, d.dexscreener_poll_seconds, self.on_candidate, self.http).run()))
        tasks += [asyncio.create_task(self.worker()) for _ in range(6)]
        tasks += [asyncio.create_task(self.exit_loop()), asyncio.create_task(self.quote_poller())]
        if self.notifier.enabled and self.cfg.notify.telegram_control:
            from .telegram_bot import TelegramControl
            tasks.append(asyncio.create_task(TelegramControl(
                self, self.cfg.telegram_bot_token, self.cfg.telegram_chat_id, self.http).run()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            open_pos = [p for p in self.positions.values() if not p.closed]
            if open_pos:
                log.warning("stopping with %d open position(s) — they resume on next start: %s",
                            len(open_pos), ", ".join(f"{p.symbol} {p.mint}" for p in open_pos))
            await self.http.aclose()
