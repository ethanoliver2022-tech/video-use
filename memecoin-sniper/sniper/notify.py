"""Log + Telegram alerts (with optional inline buttons)."""
from __future__ import annotations

import logging
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

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    async def send(self, text: str, level: int = logging.INFO, buttons: Optional[Buttons] = None,
                   telegram: bool = True) -> None:
        log.log(level, text)
        if telegram and self.enabled:
            await self.telegram(text, buttons)

    async def telegram(self, text: str, buttons: Optional[Buttons] = None,
                       chat_id: Optional[str] = None) -> None:
        payload = {"chat_id": chat_id or self.chat, "text": text[:4000],
                   "disable_web_page_preview": True, "parse_mode": "HTML"}
        markup = keyboard(buttons)
        if markup:
            payload["reply_markup"] = markup
        try:
            resp = await self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                        json=payload, timeout=5)
            if resp.status_code == 400:  # usually an HTML parse error; retry as plain text
                payload.pop("parse_mode")
                await self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                     json=payload, timeout=5)
        except Exception as e:
            log.debug("telegram send failed: %s", e)
