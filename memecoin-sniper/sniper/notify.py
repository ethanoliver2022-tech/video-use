"""Log + Telegram alerts (with optional inline buttons)."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Optional

import httpx

log = logging.getLogger("sniper")

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)


def keyboard(buttons: Optional[Buttons]) -> Optional[dict]:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


class Notifier:
    def __init__(self, http: httpx.AsyncClient, bot_token: str = "", chat_id: str = ""):
        self.http, self.token, self.chat = http, bot_token, chat_id
        self._lock = asyncio.Lock()   # one message at a time keeps us inside Telegram's limits

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    async def send(self, text: str, level: int = logging.INFO, buttons: Optional[Buttons] = None,
                   telegram: bool = True) -> None:
        log.log(level, _plain(text))
        if telegram and self.enabled:
            await self.telegram(text, buttons)

    async def telegram(self, text: str, buttons: Optional[Buttons] = None,
                       chat_id: Optional[str] = None) -> None:
        payload = {"chat_id": chat_id or self.chat, "text": text[:4000],
                   "disable_web_page_preview": True, "parse_mode": "HTML"}
        markup = keyboard(buttons)
        if markup:
            payload["reply_markup"] = markup
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with self._lock:
            for _ in range(3):
                try:
                    resp = await self.http.post(url, json=payload, timeout=10)
                except httpx.HTTPError as e:
                    log.debug("telegram send failed: %s", type(e).__name__)
                    return
                if resp.status_code == 429:  # rate limited: wait as told, then retry
                    try:
                        wait = float(resp.json().get("parameters", {}).get("retry_after", 1))
                    except ValueError:
                        wait = 1.0
                    await asyncio.sleep(min(wait, 30))
                    continue
                if resp.status_code == 400 and "parse_mode" in payload:
                    payload.pop("parse_mode")   # bad HTML: resend as plain text
                    payload["text"] = _plain(payload["text"])
                    continue
                if resp.status_code != 200:
                    log.debug("telegram send failed: HTTP %s", resp.status_code)
                return


def _plain(text: str) -> str:
    """Strip our HTML tags and unescape entities for logs / plain-text fallback."""
    return html.unescape(re.sub(r"</?(b|i|code|pre|tg-spoiler)>", "", text))
