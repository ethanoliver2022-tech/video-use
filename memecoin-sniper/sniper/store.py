"""SQLite persistence: trade ledger, open positions, creator reputation, socials, blocklist.

Everything survives restarts. Paper and live keep separate databases so
simulated results never mix with real ones.
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
    id INTEGER PRIMARY KEY, ts REAL, event TEXT, mint TEXT, symbol TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS positions (mint TEXT PRIMARY KEY, data TEXT, closed INTEGER);
CREATE TABLE IF NOT EXISTS launches (mint TEXT PRIMARY KEY, creator TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS launches_creator ON launches(creator, ts);
CREATE TABLE IF NOT EXISTS socials (handle TEXT, mint TEXT, ts REAL, PRIMARY KEY (handle, mint));
CREATE TABLE IF NOT EXISTS blocklist (creator TEXT PRIMARY KEY, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS copy_wallets (
    address TEXT PRIMARY KEY, label TEXT, buy_sol REAL, copy_sells INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, data_dir: str, mode: str):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(data_dir) / f"sniper-{mode}.db"
        self.db = sqlite3.connect(self.path, isolation_level=None)  # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- ledger ----
    def event(self, event: str, mint: str = "", symbol: str = "", **data) -> None:
        self.db.execute("INSERT INTO events(ts, event, mint, symbol, data) VALUES (?,?,?,?,?)",
                        (time.time(), event, mint, symbol, json.dumps(data, default=str)))

    def events(self, event: Optional[str] = None, since: float = 0) -> list[dict]:
        q, args = "SELECT ts, event, mint, symbol, data FROM events WHERE ts >= ?", [since]
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
        self.db.execute("INSERT OR REPLACE INTO positions(mint, data, closed) VALUES (?,?,?)",
                        (pos.mint, json.dumps(pos.to_dict(), default=str), int(pos.closed)))

    def open_positions(self) -> list[Position]:
        rows = self.db.execute("SELECT data FROM positions WHERE closed = 0").fetchall()
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
        rows = self.db.execute("SELECT address, label, buy_sol, copy_sells FROM copy_wallets").fetchall()
        return [{"address": a, "label": l, "buy_sol": b, "copy_sells": bool(c)} for a, l, b, c in rows]

    def add_copy_wallet(self, address: str, label: str = "", buy_sol: float = 0.0,
                        copy_sells: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO copy_wallets VALUES (?,?,?,?)",
                        (address, label, buy_sol, int(copy_sells)))

    def remove_copy_wallet(self, address: str) -> bool:
        return self.db.execute("DELETE FROM copy_wallets WHERE address = ?", (address,)).rowcount > 0

    def get_setting(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
