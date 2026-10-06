"""Performance analytics over closed positions."""
from __future__ import annotations

from collections import defaultdict

from .store import Store


def summarize(store: Store, since: float = 0, until: float = float("inf")) -> dict:
    closes = store.events("close", since, until)
    pnls = [float(c.get("pnl_sol", 0)) for c in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_reason: dict[str, list[float]] = defaultdict(list)
    by_source: dict[str, list[float]] = defaultdict(list)
    for c, p in zip(closes, pnls):
        by_reason[_bucket(c.get("reason", "?"))].append(p)
        by_source[c.get("source", "?")].append(p)
    holds = [float(c.get("held_s", 0)) for c in closes]
    return {
        "trades": len(pnls),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "total_pnl": sum(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "best": max(pnls, default=0.0),
        "worst": min(pnls, default=0.0),
        "avg_hold_s": sum(holds) / len(holds) if holds else 0.0,
        "by_reason": {k: (len(v), sum(v)) for k, v in sorted(by_reason.items())},
        "by_source": {k: (len(v), sum(v)) for k, v in sorted(by_source.items())},
        "speed": speed(store, since, until),
    }


SPEED_STEPS = ("decide", "build", "send", "confirm", "total")


def speed(store: Store, since: float = 0, until: float = float("inf"), last: int = 0) -> dict:
    """Median seconds per step for buys and sells that recorded timings (live trades record
    them all; paper only the decision). `last` = only the most recent N of each."""
    import statistics
    out = {}
    for side in ("buy", "sell"):
        rows = [e["timing"] for e in store.events(side, since, until)
                if isinstance(e.get("timing"), dict) and e["timing"]]
        if last:
            rows = rows[-last:]
        steps = {}
        for k in SPEED_STEPS:
            vals = [float(r[k]) for r in rows if isinstance(r.get(k), (int, float))]
            if vals and not (side == "sell" and k == "decide"):
                steps[k] = statistics.median(vals)
        if steps:
            out[side] = {"n": len(rows), **steps}
    return out


def _speed_text(sp: dict) -> list[str]:
    lines = []
    for side, label in (("buy", "Buys"), ("sell", "Sells")):
        if side in sp:
            parts = " · ".join(f"{k} {sp[side][k]:.2f}s" for k in SPEED_STEPS if k in sp[side])
            lines.append(f"  {label} ({sp[side]['n']}): {parts}")
    return lines


def speed_line(store: Store) -> str:
    """One short line for /health: the last 20 trades."""
    sp = speed(store, last=20)
    return ("Speed, median of recent trades:\n" + "\n".join(_speed_text(sp))) if sp else ""


def _bucket(reason: str) -> str:
    """'stop loss (-27%)' -> 'stop loss', 'KOL buy 7xKXtg…' -> 'KOL buy'."""
    if reason.startswith("KOL buy"):
        return "KOL buy"
    return reason.split(" (")[0].split(" +")[0]


def format_summary(s: dict) -> str:
    if not s["trades"]:
        return "No closed trades yet."
    lines = [
        f"Trades: {s['trades']}   Win rate: {s['win_rate']:.0f}%",
        f"Total PnL: {s['total_pnl']:+.4f} SOL",
        f"Avg win: {s['avg_win']:+.4f}   Avg loss: {s['avg_loss']:+.4f}",
        f"Best: {s['best']:+.4f}   Worst: {s['worst']:+.4f}",
        f"Avg hold: {s['avg_hold_s'] / 60:.1f} min",
        "",
        "By exit reason:",
        *[f"  {k:<24} {n:>4}  {p:+.4f}" for k, (n, p) in s["by_reason"].items()],
        "",
        "By source:",
        *[f"  {k:<24} {n:>4}  {p:+.4f}" for k, (n, p) in s["by_source"].items()],
    ]
    if s.get("speed"):
        lines += ["", "Speed (median seconds):", *_speed_text(s["speed"])]
    return "\n".join(lines)
