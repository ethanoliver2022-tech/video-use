"""Wires scanners -> safety -> risk -> execution -> exit monitoring."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from . import exits
from .config import Config
from .execution.executors import CurveState, Executor, Jupiter, LiveExecutor, PaperExecutor
from .execution.wallet import load_keypair
from .models import Candidate, Position
from .notify import Ledger, Notifier
from .safety import SafetyChecker
from .scanners.multichain import DexScreenerScanner, GeckoTerminalScanner
from .scanners.pumpportal import PumpPortalStream, trade_price
from .solana_rpc import SolanaRpc

log = logging.getLogger("sniper")

SELL_RETRY_LIMIT = 5


class Engine:
    def __init__(self, cfg: Config, live: bool = False, scan_only: bool = False):
        self.cfg, self.live, self.scan_only = cfg, live, scan_only
        self.http = httpx.AsyncClient(timeout=15, headers={"user-agent": "memecoin-sniper/0.1"})
        self.rpc = SolanaRpc(cfg.endpoints.rpc_url, self.http)
        self.jupiter = Jupiter(cfg.endpoints.jupiter_api, self.rpc, self.http)
        self.safety = SafetyChecker(cfg.filters, self.rpc, self.http, cfg.endpoints.rugcheck_api)
        self.notifier = Notifier(self.http, cfg.telegram_bot_token if cfg.notify.telegram else "",
                                 cfg.telegram_chat_id)
        self.ledger = Ledger(cfg.data_dir, "live" if live else "paper")
        self.own_wallet = ""
        self.executor: Executor
        if live:
            kp = load_keypair(cfg.private_key)
            self.own_wallet = str(kp.pubkey())
            self.executor = LiveExecutor(cfg, kp, self.rpc, self.jupiter, self.http)
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
        self.sell_locks: dict[str, asyncio.Lock] = {}
        self.sell_failures: dict[str, int] = {}
        self.buy_lock = asyncio.Lock()
        self.kol_wallets = set(cfg.exits.kol_wallets)
        self.last_loss_at = 0.0

    # ---------- discovery ----------

    async def on_candidate(self, c: Candidate) -> None:
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

    async def handle_candidate(self, c: Candidate) -> None:
        report = await self.safety.evaluate(c)
        tag = f"[{c.chain}] {c.symbol or '?'} {c.mint}"
        if not report.passed:
            log.debug("reject %s: %s", tag, "; ".join(report.reasons))
            return
        notes = "; ".join(report.notes)
        if c.chain != "solana":
            if self.cfg.notify.alert_other_chains:
                liq = f" liq ${c.liquidity_usd:,.0f}" if c.liquidity_usd else ""
                await self.notifier.send(f"👀 {tag}{liq} via {c.source} — {c.url or ''}")
            return
        if self.scan_only:
            await self.notifier.send(f"✅ passes filters: {tag} ({notes}) {c.url or ''}")
            return
        await self.try_buy(c, notes)

    # ---------- entries ----------

    async def risk_block(self) -> Optional[str]:
        t = self.cfg.trading
        open_n = sum(1 for p in self.positions.values() if not p.closed)
        if open_n >= t.max_open_positions:
            return "max open positions"
        if -self.ledger.realized_today() >= t.daily_loss_limit_sol:
            return "daily loss limit hit"
        if t.cooldown_after_loss_seconds and time.time() - self.last_loss_at < t.cooldown_after_loss_seconds:
            return "cooling down after loss"
        if self.live:
            bal = await self.rpc.get_balance_sol(self.own_wallet)
            if bal - t.buy_amount_sol - t.priority_fee_sol < t.min_sol_reserve:
                return f"balance too low ({bal:.4f} SOL)"
        return None

    async def try_buy(self, c: Candidate, notes: str) -> None:
        async with self.buy_lock:  # serialise so risk limits can't be raced
            if c.mint in self.positions:
                return
            blocked = await self.risk_block()
            if blocked:
                log.info("skip %s %s: %s", c.symbol, c.mint, blocked)
                return
            sol = self.cfg.trading.buy_amount_sol
            curve = self.curves.get(c.mint) if c.on_bonding_curve else None
            try:
                fill = await self.executor.buy(c, sol, curve)
            except Exception as e:
                await self.notifier.send(f"❌ buy failed {c.symbol} {c.mint}: {e}", logging.WARNING)
                return
            if fill.tokens <= 0:
                await self.notifier.send(f"❌ buy {c.symbol} returned 0 tokens ({fill.signature})")
                return
            pos = Position(mint=c.mint, symbol=c.symbol or c.mint[:6], source=c.source,
                           creator=c.creator, entry_price=fill.sol / fill.tokens,
                           tokens_initial=fill.tokens, tokens_remaining=fill.tokens, sol_in=fill.sol)
            self.positions[c.mint] = pos

        self.ledger.write("buy", mint=c.mint, symbol=pos.symbol, source=c.source, sol=fill.sol,
                          tokens=fill.tokens, sig=fill.signature)
        await self.notifier.send(
            f"🟢 BUY {pos.symbol} {fill.sol:.4f} SOL → {fill.tokens:,.0f} tokens "
            f"({'LIVE' if self.live else 'paper'}) [{notes}] {c.url or ''}")
        if c.source.startswith("pumpfun"):
            await self.stream.watch_token(c.mint)

    # ---------- price feeds ----------

    async def on_trade(self, msg: dict) -> None:
        mint = msg["mint"]
        if msg.get("vSolInBondingCurve") and msg.get("vTokensInBondingCurve"):
            self.curves[mint] = CurveState(float(msg["vSolInBondingCurve"]),
                                           float(msg["vTokensInBondingCurve"]))
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
            await self.notifier.send(f"🎓 {pos.symbol} graduated off the bonding curve")

    async def quote_poller(self) -> None:
        """Prices positions that aren't on a pump.fun curve by quoting the actual exit."""
        while True:
            for pos in list(self.positions.values()):
                if pos.closed or (pos.source == "pumpfun" and not pos.migrated):
                    continue
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
        lock = self.sell_locks.setdefault(pos.mint, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            dec = exits.evaluate(pos, self.cfg.exits)
            if not dec:
                return
            curve = self.curves.get(pos.mint) if not pos.migrated else None
            try:
                fill = await self.executor.sell(pos.mint, dec.tokens, dec.sell_all,
                                                pump=pos.source.startswith("pumpfun"), curve=curve)
            except Exception as e:
                n = self.sell_failures[pos.mint] = self.sell_failures.get(pos.mint, 0) + 1
                await self.notifier.send(f"⚠️ sell {pos.symbol} failed ({n}x): {e}", logging.WARNING)
                if n >= SELL_RETRY_LIMIT:
                    pos.closed, pos.close_reason = True, "sell failed repeatedly — check wallet"
                    await self.notifier.send(f"🛑 giving up on {pos.symbol}; sell manually", logging.ERROR)
                return
            self.sell_failures.pop(pos.mint, None)
            exits.apply_fill(pos, dec, fill.tokens or dec.tokens, fill.sol, self.cfg.exits)
            self.ledger.write("sell", mint=pos.mint, symbol=pos.symbol, reason=dec.reason,
                              tokens=fill.tokens, sol=fill.sol, sig=fill.signature)
            await self.notifier.send(
                f"🔴 SELL {pos.symbol} {'ALL' if dec.sell_all else f'{dec.tokens:,.0f}'} "
                f"→ {fill.sol:.4f} SOL — {dec.reason} (pnl {pos.pnl_pct:+.0f}%)")
            if pos.closed:
                await self._closed(pos)

    async def _closed(self, pos: Position) -> None:
        pnl = pos.realized_pnl_sol
        if pnl < 0:
            self.last_loss_at = time.time()
        self.ledger.write("close", mint=pos.mint, symbol=pos.symbol, reason=pos.close_reason,
                          sol_in=pos.sol_in, sol_out=pos.sol_out, pnl_sol=pnl,
                          held_s=round(time.time() - pos.opened_at))
        await self.notifier.send(f"🏁 closed {pos.symbol}: {pnl:+.4f} SOL ({pos.close_reason}) — "
                                 f"today {self.ledger.realized_today():+.4f} SOL")
        await self.stream.unwatch_token(pos.mint)

    # ---------- main ----------

    async def run(self) -> None:
        d, e = self.cfg.discovery, self.cfg.endpoints
        mode = "SCAN-ONLY" if self.scan_only else ("LIVE" if self.live else "PAPER")
        who = f" wallet {self.own_wallet}" if self.live else ""
        await self.notifier.send(f"🚀 sniper starting in {mode} mode{who}")
        tasks = [asyncio.create_task(self.stream.run())]
        if d.geckoterminal_networks:
            tasks.append(asyncio.create_task(GeckoTerminalScanner(
                e.geckoterminal_api, d.geckoterminal_networks, d.geckoterminal_poll_seconds,
                self.on_candidate, self.http).run()))
        if d.dexscreener_profiles:
            tasks.append(asyncio.create_task(DexScreenerScanner(
                e.dexscreener_api, d.dexscreener_poll_seconds, self.on_candidate, self.http).run()))
        tasks += [asyncio.create_task(self.worker()) for _ in range(4)]
        tasks += [asyncio.create_task(self.exit_loop()), asyncio.create_task(self.quote_poller())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            open_pos = [p for p in self.positions.values() if not p.closed]
            if open_pos:
                log.warning("stopping with %d open position(s): %s", len(open_pos),
                            ", ".join(f"{p.symbol} {p.mint}" for p in open_pos))
            await self.http.aclose()
