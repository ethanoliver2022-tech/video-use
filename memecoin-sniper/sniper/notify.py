"""Logging, optional Telegram alerts, and an append-only trade ledger."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("sniper")


class Notifier:
    def __init__(self, http: httpx.AsyncClient, bot_token: str = "", chat_id: str = ""):
        self.http, self.token, self.chat = http, bot_token, chat_id

    async def send(self, text: str, level: int = logging.INFO) -> None:
        log.log(level, text)
        if not (self.token and self.chat):
            return
        try:
            await self.http.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat, "text": text, "disable_web_page_preview": True},
                timeout=5,
            )
        except Exception as e:
            log.debug("telegram send failed: %s", e)


class Ledger:
    def __init__(self, data_dir: str, mode: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"trades-{mode}.jsonl"

    def write(self, event: str, **fields) -> None:
        row = {"ts": time.time(), "event": event, **fields}
        with self.path.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def realized_today(self) -> float:
        """Sum of realized PnL (SOL) for positions closed today (UTC)."""
        if not self.path.exists():
            return 0.0
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for line in self.path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") != "close":
                continue
            if datetime.fromtimestamp(row["ts"], timezone.utc).date() == today:
                total += float(row.get("pnl_sol", 0))
        return total
