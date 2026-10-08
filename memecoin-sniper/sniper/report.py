"""The stats screen: results by period, by strategy, recent trades, winners and losers,
costs, you vs the wallets you copy, and what coins did after you sold.

Pure functions over the trade ledger (store events), so they're easy to test. Names of
copied wallets and call groups are looked up when shown, so a renamed wallet shows its
new name everywhere."""
from __future__ import annotations

import html
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

PERIODS = {"24h": "Last 24 hours", "7d": "Last 7 days", "rst": "Since reset", "all": "All time"}
PERIOD_SECONDS = {"24h": 86400, "7d": 7 * 86400}

# estimated per-trade costs (the same model paper trading charges)
NETWORK_FEE_SOL = 0.000005
PUMP_FEES = 0.0125 + 0.005     # pump.fun's fee + PumpPortal's, as a share of the trade


@dataclass
class Trade:
    """One closed position."""
    ts: float
    mint: str
    symbol: str
    sol_in: float
    sol_out: float
    reason: str
    held_s: float
    strategy: str          # key, e.g. "copy:<wallet>", "call:<group>", "snipe", "manual"
    copied_from: Optional[str]
    exit_price: Optional[float]

    @property
    def pnl(self) -> float:
        return self.sol_out - self.sol_in

    @property
    def pct(self) -> float:
        return (self.sol_out / self.sol_in - 1) * 100 if self.sol_in > 0 else 0.0


def strategy_key(source: str, copied_from: Optional[str] = None, stored: str = "") -> str:
    """Older trades have no stored strategy: work it out from where the buy came from."""
    if stored:
        return stored
    if copied_from:
        return f"copy:{copied_from}"
    base, _, tag = (source or "?").partition("/")
    if base == "pumpfun":
        return "snipe:dev" if tag == "dev" else "snipe:keyword" if tag == "keyword" else "snipe"
    return {"pumpfun-migration": "migration", "call": "call:", "copy": "copy:"}.get(base, base)


def strategy_name(key: str, wallet_name: Callable[[str], str]) -> str:
    kind, _, rest = key.partition(":")
    if kind == "copy":
        return f"👥 {wallet_name(rest)}" if rest else "👥 Copies"
    if kind == "call":
        return f"📣 {rest}" if rest else "📣 Calls"
    return {"snipe": "🎯 Sniping" + {"dev": " (dev watch)", "keyword": " (keywords)"}.get(rest, ""),
            "migration": "🎓 Migrations", "momentum": "🚀 Momentum", "manual": "✋ Manual",
            "limit": "📋 Limit orders", "geckoterminal": "🦎 New pools",
            "dexscreener": "🦅 DexScreener"}.get(kind, kind or "?")


def closed_trades(store: "Store", since: float, until: float = float("inf")) -> list[Trade]:
    out = []
    for e in store.events("close", since, until):
        sol_in = float(e.get("sol_in") or 0)
        sol_out = float(e.get("sol_out") or (sol_in + float(e.get("pnl_sol") or 0)))
        out.append(Trade(
            ts=float(e["ts"]), mint=e.get("mint") or "", symbol=e.get("symbol") or "?",
            sol_in=sol_in, sol_out=sol_out, reason=str(e.get("reason") or ""),
            held_s=float(e.get("held_s") or 0), copied_from=e.get("copied_from"),
            strategy=strategy_key(str(e.get("source") or ""), e.get("copied_from"),
                                  str(e.get("strategy") or "")),
            exit_price=e.get("exit_price")))
    return out


def period_start(period: str, reset_at: Optional[float], now: float) -> float:
    if period in PERIOD_SECONDS:
        return now - PERIOD_SECONDS[period]
    if period == "rst" and reset_at:
        return reset_at
    return 0.0


def costs(store: "Store", since: float, cfg: "Config", until: float = float("inf")) -> dict:
    """Estimated costs of the trades in a period (already inside the profit numbers):
    network + priority fees and Jito tips per transaction, and trading fees per SOL."""
    per_tx = NETWORK_FEE_SOL + cfg.trading.priority_fee_sol + cfg.speed.tip_sol()
    txs, volume = 0, 0.0
    for kind in ("buy", "sell"):
        for e in store.events(kind, since, until):
            txs += 1
            volume += float(e.get("sol") or 0)
    return {"txs": txs, "fees": txs * per_tx, "trading": volume * PUMP_FEES,
            "total": txs * per_tx + volume * PUMP_FEES}


def by_strategy(trades: list[Trade], wallet_name: Callable[[str], str]) -> list[tuple]:
    """[(name, trades, wins, pnl)] best first."""
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(t.strategy, []).append(t)
    rows = [(strategy_name(k, wallet_name), len(v), sum(1 for t in v if t.pnl > 0),
             sum(t.pnl for t in v)) for k, v in groups.items()]
    return sorted(rows, key=lambda r: -r[3])


def _hold(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 7200:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def _short_reason(reason: str) -> str:
    r = reason.lower()
    for key, label in (("copied wallet sold", "sold with them"), ("panic", "panic sell"),
                       ("take profit", "take profit"), ("stop loss", "stop loss"),
                       ("trailing", "trailing stop"), ("max hold", "max hold"),
                       ("migrat", "migrated"), ("dev sold", "dev sold"),
                       ("big holder", "whale dumped"), ("manual", "you sold"),
                       ("breakeven", "breakeven"), ("initials", "took initials"),
                       ("no price", "dead coin"), ("sell pressure", "sell pressure"),
                       ("moonbag", "moonbag exit"), ("limit", "limit order")):
        if key in r:
            return label
    return reason[:24]


def _n(n: int) -> str:
    return f"{n} trade" + ("" if n == 1 else "s")


def _sol(x: float) -> str:
    return f"{x:+.4f}"


def overview(trades: list[Trade], cost: dict, rent: float, title: str,
             wallet_name: Callable[[str], str], recent: int = 10) -> str:
    """The main stats message (HTML)."""
    e = html.escape
    if not trades:
        return (f"📈 <b>{e(title)}</b>\nNo closed trades in this period yet."
                + (f"\nRent back: {rent:+.4f} SOL" if rent else ""))
    total = sum(t.pnl for t in trades) + rent
    wins = sum(1 for t in trades if t.pnl > 0)
    icon = "🟢" if total > 0 else "🔴" if total < 0 else "⚪"
    lines = [f"📈 <b>{e(title)}</b>",
             f"{icon} <b>{total:+.4f} SOL</b> · {_n(len(trades))} · "
             f"{wins / len(trades) * 100:.0f}% won",
             f"💸 Costs ≈ {cost['total']:.4f} SOL (fees & tips {cost['fees']:.4f}, trading "
             f"fees {cost['trading']:.4f}), already counted in the profit"]
    if rent:
        lines.append(f"🧹 Rent back: {rent:+.4f} SOL")

    rows = by_strategy(trades, wallet_name)
    width = min(18, max(len(r[0]) for r in rows))
    table = [f"{name[:width]:<{width}} {n:>3} {w / n * 100:>3.0f}% {pnl:+.4f}"
             for name, n, w, pnl in rows]
    lines += ["", "<b>By strategy</b> (trades · won · SOL)", f"<pre>{e(chr(10).join(table))}</pre>"]

    latest = sorted(trades, key=lambda t: -t.ts)[:recent]
    lines.append("<b>Recent trades</b> (🔍1-5 below open the coin)")
    for i, t in enumerate(latest, 1):
        mark = "🟢" if t.pnl > 0 else "🔴"
        who = strategy_name(t.strategy, wallet_name)
        lines.append(f"{i}. {mark} {e(t.symbol[:12])} {_sol(t.pnl)} ({t.pct:+.0f}%) · {e(who)} · "
                     f"{e(_short_reason(t.reason))} · {_hold(t.held_s)}")

    best = sorted((t for t in trades if t.pnl > 0), key=lambda t: -t.pnl)[:3]
    worst = sorted((t for t in trades if t.pnl < 0), key=lambda t: t.pnl)[:3]
    if best:
        lines.append("🏆 " + " · ".join(f"{e(t.symbol[:10])} {_sol(t.pnl)}" for t in best))
    if worst:
        lines.append("💀 " + " · ".join(f"{e(t.symbol[:10])} {_sol(t.pnl)}" for t in worst))
    return "\n".join(lines)


def recap(trades: list[Trade], cost: dict, rent: float, day: str, mode: str,
          wallet_name: Callable[[str], str]) -> str:
    """The short daily message."""
    e = html.escape
    if not trades:
        return f"🗓 <b>Daily recap</b> {day} ({mode}): no closed trades."
    total = sum(t.pnl for t in trades) + rent
    wins = sum(1 for t in trades if t.pnl > 0)
    rows = by_strategy(trades, wallet_name)
    best = max(trades, key=lambda t: t.pnl)
    worst = min(trades, key=lambda t: t.pnl)
    lines = [f"🗓 <b>Daily recap</b> {day} ({mode})",
             f"{'🟢' if total >= 0 else '🔴'} <b>{total:+.4f} SOL</b> · {_n(len(trades))} · "
             f"{wins / len(trades) * 100:.0f}% won · costs ≈ {cost['total']:.4f}",
             f"🏆 Best trade: {e(best.symbol)} {_sol(best.pnl)} ({best.pct:+.0f}%)"]
    if worst is not best:
        lines.append(f"💀 Worst trade: {e(worst.symbol)} {_sol(worst.pnl)} ({worst.pct:+.0f}%)")
    lines.append(f"📊 Best strategy: {e(rows[0][0])} {_sol(rows[0][3])}")
    if len(rows) > 1:
        lines.append(f"📉 Worst strategy: {e(rows[-1][0])} {_sol(rows[-1][3])}")
    return "\n".join(lines)


def theirs_on_coin(store: "Store", wallet: str, mint: str) -> Optional[float]:
    """The copied wallet's own result on a coin, in %, from the trades the bot saw it make
    (None: unknown, or still holding most of it)."""
    sol_in = sol_out = got = sold = 0.0
    for _sig, m, _ts, side, sol, tokens in store.wallet_trades(wallet, 0):
        if m != mint:
            continue
        if side == "buy":
            sol_in += sol
            got += tokens
        elif side == "sell":
            sol_out += sol
            sold += tokens
    if sol_in <= 0 or got <= 0 or sold < got * 0.9:
        return None
    return (sol_out / sol_in - 1) * 100


def you_vs_wallets(trades: list[Trade], store: "Store", title: str,
                   wallet_name: Callable[[str], str], gap: Callable[[str], Optional[float]]) -> str:
    e = html.escape
    copies = [t for t in trades if t.copied_from]
    if not copies:
        return f"👥 <b>You vs your wallets</b> ({e(title)})\nNo closed copies in this period."
    lines = [f"👥 <b>You vs your wallets</b> ({e(title)})",
             "Your result on each copied coin next to the wallet's own (from the trades the "
             "bot saw it make)."]
    by: dict[str, list[Trade]] = {}
    for t in copies:
        by.setdefault(t.copied_from, []).append(t)
    for w, ts in sorted(by.items(), key=lambda kv: -sum(t.pnl for t in kv[1])):
        pairs = [(t, theirs_on_coin(store, w, t.mint)) for t in ts]
        known = [(t.pct, th) for t, th in pairs if th is not None]
        g = gap(w)
        head = f"\n<b>{e(wallet_name(w))}</b>: {len(ts)} copies, you {sum(t.pnl for t in ts):+.4f} SOL"
        if known:
            you = sum(a for a, _ in known) / len(known)
            them = sum(b for _, b in known) / len(known)
            head += f"\n  avg on the same coins: you {you:+.0f}% · them {them:+.0f}%"
            if them - you > 30:
                head += "\n  ⚠️ they do much better: you get in later or out earlier"
        if g is not None:
            head += f"\n  your entry vs theirs: {g:+.0f}% on average"
        lines.append(head)
        for t, th in sorted(pairs, key=lambda p: -p[0].ts)[:5]:
            them = f"them {th:+.0f}%" if th is not None else "them: still in / unknown"
            lines.append(f"  {'🟢' if t.pnl > 0 else '🔴'} {e(t.symbol[:12])} you {t.pct:+.0f}% · "
                         f"{them}")
    return "\n".join(lines)


def after_sold(rows: list[tuple], title: str) -> str:
    """rows: (close_ts, symbol, exit_price, p15, p60). A price of -1 = couldn't be read."""
    e = html.escape
    lines = [f"⏱ <b>After you sold</b> ({e(title)})",
             "How far each coin moved 15 minutes and 1 hour after you sold it."]
    done = [r for r in rows if r[4] is not None and r[4] > 0 and r[2]]
    if done:
        ups = [((r[4] / r[2]) - 1) * 100 for r in done]
        higher = sum(1 for u in ups if u > 20)
        lines.append(f"After 1h, {higher} of {len(done)} were 20%+ above your sell price "
                     f"(average {sum(ups) / len(ups):+.0f}%).")
        if higher / len(done) > 0.4:
            lines.append("💡 Many kept going up: your exits may be too early (take profits, "
                         "trailing stop, max hold).")
        elif sum(ups) / len(ups) < -30:
            lines.append("💡 Most fell after you sold: your exits are saving you money.")
    if not rows:
        lines.append("No sells in this period yet.")
    for close_ts, symbol, exit_price, p15, p60 in sorted(rows, key=lambda r: -r[0])[:12]:
        def move(p):
            if p is None:
                return "…"
            if p <= 0 or not exit_price:
                return "?"
            return f"{(p / exit_price - 1) * 100:+.0f}%"
        lines.append(f"• {e(symbol[:12])}: 15m {move(p15)} · 1h {move(p60)}")
    return "\n".join(lines)


def cumulative(trades: list[Trade], since: float, now: Optional[float] = None) -> list[tuple]:
    """(time, running total SOL) points for the profit chart, starting at 0."""
    now = now or time.time()
    pts = [(since or (min(t.ts for t in trades) - 60 if trades else now - 3600), 0.0)]
    run = 0.0
    for t in sorted(trades, key=lambda t: t.ts):
        run += t.pnl
        pts.append((t.ts, run))
    pts.append((now, run))
    return pts
