"""Wallet check: how a copied / tracked wallet has really been trading lately.

Reads its last few days of swaps from the chain once (when it's added), then keeps them up
to date from the trades the bot sees live anyway (PumpPortal and the chain watcher), with a
light top-up of anything missed every few hours. From those it works out per coin what the
wallet put in and took out: trades closed, win rate, profit, hold times, size, pace."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

from .copywatch import trades_in

if TYPE_CHECKING:
    from .solana_rpc import SolanaRpc
    from .store import Store

log = logging.getLogger(__name__)

MAX_TX_PER_CHECK = 300   # newest transactions read per check (spares a free RPC plan)
PARALLEL = 5
SOLD_OUT = 0.9           # sold this share of what it bought = a finished trade


@dataclass
class WalletReport:
    wallet: str
    days: float
    made: float              # unix time
    swaps: int = 0
    coins: int = 0
    closed: int = 0
    wins: int = 0
    pnl_sol: float = 0.0
    open_coins: int = 0
    avg_buy_sol: float = 0.0
    avg_hold_s: float = 0.0
    partial: bool = False    # hit the read limit: the oldest part of the window is missing

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed * 100 if self.closed else 0.0

    def text(self) -> str:
        if not self.swaps:
            return (f"No pump.fun / SOL swaps found in the last {self.days:g} days."
                    + (" (read limit hit)" if self.partial else ""))
        hold = self.avg_hold_s
        hold_s = f"{hold / 60:.0f} min" if hold < 7200 else f"{hold / 3600:.1f} h"
        verdict = ("🟢 profitable" if self.pnl_sol > 0 and self.win_rate >= 40 else
                   "🔴 losing" if self.pnl_sol < 0 else "🟡 mixed")
        return "\n".join([
            f"Last {self.days:g} days: {verdict}",
            f"  Closed trades: {self.closed}   Win rate: {self.win_rate:.0f}%",
            f"  Profit: {self.pnl_sol:+.2f} SOL",
            f"  Avg buy: {self.avg_buy_sol:.2f} SOL   Avg hold: {hold_s}",
            f"  {self.coins / max(self.days, 0.1):.0f} coins/day · {self.open_coins} still held",
        ] + (["  (very active: only its newest trades were read)"] if self.partial else []))


def build_report(wallet: str, rows: Iterable[tuple], days: float, now: float,
                 partial: bool = False) -> WalletReport:
    """rows: (sig, mint, ts, side, sol, tokens), oldest first. Only coins first bought
    inside the window count (a sell of something bought earlier has no known cost)."""
    r = WalletReport(wallet, days, now, partial=partial)
    per: dict[str, dict] = {}
    buys = []
    for _sig, mint, ts, side, sol, tokens in rows:
        if side not in ("buy", "sell") or not mint:
            continue
        r.swaps += 1
        c = per.get(mint)
        if c is None:
            if side != "buy":
                continue
            c = per[mint] = {"in": 0.0, "out": 0.0, "bought": 0.0, "sold": 0.0,
                             "first": ts, "last_sell": None}
        if side == "buy":
            c["in"] += sol
            c["bought"] += tokens
            buys.append(sol)
        else:
            c["out"] += sol
            c["sold"] += tokens
            c["last_sell"] = ts
    holds = []
    for c in per.values():
        if c["bought"] > 0 and c["sold"] >= c["bought"] * SOLD_OUT:
            r.closed += 1
            pnl = c["out"] - c["in"]
            r.pnl_sol += pnl
            r.wins += pnl > 0
            holds.append(c["last_sell"] - c["first"])
        else:
            r.open_coins += 1
    r.coins = len(per)
    r.avg_buy_sol = sum(buys) / len(buys) if buys else 0.0
    r.avg_hold_s = sum(holds) / len(holds) if holds else 0.0
    return r


class WalletChecker:
    def __init__(self, rpc: "SolanaRpc", store: "Store"):
        self.rpc, self.store = rpc, store
        self.reports: dict[str, WalletReport] = {}
        self._running: dict[str, asyncio.Task] = {}

    def record(self, msg: dict) -> None:
        """A live trade from PumpPortal or the chain watcher: keeps the check current."""
        sig, mint, wallet = msg.get("signature"), msg.get("mint"), msg.get("traderPublicKey")
        side = msg.get("txType")
        if not (sig and mint and wallet) or side not in ("buy", "sell"):
            return
        try:
            sol, tokens = float(msg.get("solAmount") or 0), float(msg.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            return
        self.store.add_wallet_trade(wallet, sig, mint, time.time(), side, sol, tokens)

    async def _tx(self, sig: str) -> Optional[dict]:
        return await self.rpc.call("getTransaction", [sig, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed"}])

    async def fill(self, wallet: str, days: float) -> bool:
        """Read the wallet's swaps of the last `days` the database doesn't have yet.
        Returns True if the read limit cut the window short."""
        cutoff = time.time() - days * 86400
        todo: list[dict] = []
        before = None
        partial = False
        while len(todo) < MAX_TX_PER_CHECK:
            opts: dict = {"limit": 1000, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            page = await self.rpc.call("getSignaturesForAddress", [wallet, opts]) or []
            if not page:
                break
            done = False
            for s in page:
                bt = s.get("blockTime")
                if isinstance(bt, (int, float)) and bt < cutoff:
                    done = True
                    break
                if s.get("err") is None and not self.store.has_wallet_sig(wallet, s["signature"]):
                    todo.append(s)
                    if len(todo) >= MAX_TX_PER_CHECK:
                        partial = True
                        break
            if done or partial or len(page) < 1000:
                break
            before = page[-1]["signature"]
        sem = asyncio.Semaphore(PARALLEL)

        async def one(s: dict) -> None:
            async with sem:
                try:
                    tx = await self._tx(s["signature"])
                except Exception:
                    return   # left unmarked: read again next time
            ts = float(s.get("blockTime") or time.time())
            trades = trades_in(tx or {}, wallet, s["signature"])
            for t in trades:
                self.store.add_wallet_trade(wallet, s["signature"], t["mint"], ts, t["txType"],
                                            t["solAmount"], t["tokenAmount"])
            if not trades:   # remember it was read: not a swap
                self.store.add_wallet_trade(wallet, s["signature"], "", ts, "none", 0.0, 0.0)
        await asyncio.gather(*(one(s) for s in todo))
        return partial

    async def check(self, wallet: str, days: float) -> WalletReport:
        partial = await self.fill(wallet, days)
        now = time.time()
        rep = build_report(wallet, self.store.wallet_trades(wallet, now - days * 86400), days,
                           now, partial)
        self.reports[wallet] = rep
        return rep

    def start(self, wallet: str, days: float, done=None) -> None:
        """Check in the background (one at a time per wallet); `done(report)` afterwards."""
        t = self._running.get(wallet)
        if t and not t.done():
            return

        async def run():
            try:
                rep = await self.check(wallet, days)
            except Exception as e:
                log.warning("wallet check %s failed: %s", wallet[:8], e)
                return
            if done:
                await done(rep)
        self._running[wallet] = asyncio.ensure_future(run())

    async def loop(self, wallets, days, every_hours) -> None:
        """Refresh every wallet's check every few hours (staggered), and prune old trades."""
        while True:
            for w in list(wallets()):
                rep = self.reports.get(w)
                if rep is None or time.time() - rep.made >= every_hours() * 3600:
                    try:
                        await self.check(w, days())
                    except Exception as e:
                        log.debug("wallet check %s failed: %s", w[:8], e)
                    await asyncio.sleep(5)
            self.store.prune_wallet_trades(time.time() - (days() + 1) * 86400)
            await asyncio.sleep(300)
