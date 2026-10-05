"""SQLite persistence: trade ledger, open positions, creator reputation, socials, blocklist,
copy wallets and bot settings.

Everything survives restarts. Trades and positions are tagged with the mode
(paper/live) so simulated results never mix with real ones, while reputation,
blocklist, copy wallets and settings are shared between modes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, mode TEXT, ts REAL, event TEXT, mint TEXT, symbol TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_mode_ts ON events(mode, ts);
CREATE TABLE IF NOT EXISTS positions (
    mode TEXT, mint TEXT, data TEXT, closed INTEGER, PRIMARY KEY (mode, mint));
CREATE TABLE IF NOT EXISTS launches (mint TEXT PRIMARY KEY, creator TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS launches_creator ON launches(creator, ts);
CREATE TABLE IF NOT EXISTS socials (handle TEXT, mint TEXT, ts REAL, PRIMARY KEY (handle, mint));
CREATE TABLE IF NOT EXISTS blocklist (creator TEXT PRIMARY KEY, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS copy_wallets (
    address TEXT PRIMARY KEY, label TEXT, buy_sol REAL, copy_sells INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, mode TEXT, mint TEXT, side TEXT, sol REAL, pct REAL,
    trigger_price REAL, direction TEXT, base_price REAL, created REAL, expires REAL,
    status TEXT, note TEXT);
"""


def _private(path: Path) -> None:
    """The data folder (wallet key, database) is for its owner only, even on a shared host."""
    try:
        path.chmod(0o700)
    except OSError:
        pass  # e.g. a read-only or foreign-owned mount: the files themselves are still 0600


class Store:
    def __init__(self, data_dir: str, mode: str = "paper"):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        _private(Path(data_dir))
        self.mode = mode
        self.path = Path(data_dir) / "sniper.db"
        self.db = sqlite3.connect(self.path, isolation_level=None)  # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL: crash-safe and consistent, without an fsync per insert (the bot
        # records every pump.fun launch, and a disk flush each time stalls the event loop)
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Upgrade databases created by older versions in place."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(copy_wallets)")}
        if "mode" not in cols:  # v0.5: wallets can be copied or only tracked (alerts)
            self.db.execute("ALTER TABLE copy_wallets ADD COLUMN mode TEXT DEFAULT 'copy'")

    # ---- ledger ----
    def event(self, event: str, mint: str = "", symbol: str = "", **data) -> None:
        self.db.execute("INSERT INTO events(mode, ts, event, mint, symbol, data) VALUES (?,?,?,?,?,?)",
                        (self.mode, time.time(), event, mint, symbol, json.dumps(data, default=str)))

    def events(self, event: Optional[str] = None, since: float = 0,
               until: float = float("inf")) -> list[dict]:
        q = "SELECT ts, event, mint, symbol, data FROM events WHERE mode = ? AND ts >= ? AND ts < ?"
        args: list = [self.mode, since, until if until != float("inf") else 1e18]
        if event:
            q += " AND event = ?"
            args.append(event)
        rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [{"ts": r[0], "event": r[1], "mint": r[2], "symbol": r[3], **json.loads(r[4])}
                for r in rows]

    def realized_today(self) -> float:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(float(e.get("pnl_sol", 0)) for e in self.events("close", midnight.timestamp()))

    # ---- positions ----
    def save_position(self, pos: Position) -> None:
        self.db.execute("INSERT OR REPLACE INTO positions(mode, mint, data, closed) VALUES (?,?,?,?)",
                        (self.mode, pos.mint, json.dumps(pos.to_dict(), default=str), int(pos.closed)))

    def open_positions(self) -> list[Position]:
        rows = self.db.execute("SELECT data FROM positions WHERE mode = ? AND closed = 0",
                               (self.mode,)).fetchall()
        return [Position.from_dict(json.loads(r[0])) for r in rows]

    # ---- creator reputation ----
    def record_launch(self, mint: str, creator: str, ts: Optional[float] = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO launches(mint, creator, ts) VALUES (?,?,?)",
                        (mint, creator, ts or time.time()))

    def launches_since(self, creator: str, since: float, exclude_mint: str = "") -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM launches WHERE creator = ? AND ts >= ? AND mint != ?",
            (creator, since, exclude_mint)).fetchone()[0]

    def prune_launches(self, older_than: float) -> None:
        self.db.execute("DELETE FROM launches WHERE ts < ?", (older_than,))
        self.db.execute("DELETE FROM socials WHERE ts < ?", (older_than,))

    def record_socials(self, mint: str, handles: list[str]) -> list[str]:
        """Store handles for this mint; return the ones already used by a different mint."""
        reused = []
        for h in handles:
            row = self.db.execute("SELECT 1 FROM socials WHERE handle = ? AND mint != ? LIMIT 1",
                                  (h, mint)).fetchone()
            if row:
                reused.append(h)
            self.db.execute("INSERT OR IGNORE INTO socials(handle, mint, ts) VALUES (?,?,?)",
                            (h, mint, time.time()))
        return reused

    def block(self, creator: str, reason: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO blocklist(creator, reason, ts) VALUES (?,?,?)",
                        (creator, reason, time.time()))

    def is_blocked(self, creator: Optional[str]) -> Optional[str]:
        if not creator:
            return None
        row = self.db.execute("SELECT reason FROM blocklist WHERE creator = ?", (creator,)).fetchone()
        return row[0] if row else None

    # ---- copy wallets & runtime settings (editable from Telegram) ----
    def copy_wallets(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT address, label, buy_sol, copy_sells, mode FROM copy_wallets").fetchall()
        return [{"address": a, "label": l, "buy_sol": b, "copy_sells": bool(c), "mode": m or "copy"}
                for a, l, b, c, m in rows]

    def add_copy_wallet(self, address: str, label: str = "", buy_sol: float = 0.0,
                        copy_sells: bool = True, mode: str = "copy") -> None:
        self.db.execute("INSERT OR REPLACE INTO copy_wallets(address, label, buy_sol, copy_sells, mode)"
                        " VALUES (?,?,?,?,?)", (address, label, buy_sol, int(copy_sells), mode))

    # ---- limit orders ----
    def add_order(self, mint: str, side: str, sol: float, pct: float, trigger_price: float,
                  direction: str, base_price: float, expires: float, note: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO orders(mode, mint, side, sol, pct, trigger_price, direction, base_price,"
            " created, expires, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)",
            (self.mode, mint, side, sol, pct, trigger_price, direction, base_price, time.time(),
             expires, note))
        return int(cur.lastrowid)

    def open_orders(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, mint, side, sol, pct, trigger_price, direction, base_price, created, expires,"
            " note FROM orders WHERE mode = ? AND status = 'open' ORDER BY id", (self.mode,)).fetchall()
        keys = ("id", "mint", "side", "sol", "pct", "trigger_price", "direction", "base_price",
                "created", "expires", "note")
        return [dict(zip(keys, r)) for r in rows]

    def set_order_status(self, order_id: int, status: str, from_status: str = "open") -> bool:
        return self.db.execute("UPDATE orders SET status = ? WHERE id = ? AND status = ?",
                               (status, order_id, from_status)).rowcount > 0

    def interrupted_orders(self) -> list[int]:
        """Orders that were executing when the bot stopped: never re-run them blindly."""
        rows = self.db.execute("SELECT id FROM orders WHERE mode = ? AND status = 'executing'",
                               (self.mode,)).fetchall()
        for (oid,) in rows:
            self.set_order_status(oid, "interrupted", from_status="executing")
        return [r[0] for r in rows]

    def remove_copy_wallet(self, address: str) -> bool:
        return self.db.execute("DELETE FROM copy_wallets WHERE address = ?", (address,)).rowcount > 0

    def get_setting(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def overrides(self) -> dict:
        return json.loads(self.get_setting("overrides") or "{}")

    def set_override(self, key: str, value) -> None:
        o = self.overrides()
        o[key] = value
        self.set_setting("overrides", json.dumps(o))

    def clear_overrides(self) -> None:
        self.set_setting("overrides", "{}")
