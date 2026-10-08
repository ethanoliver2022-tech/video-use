"""Call sniper: watch Telegram groups and channels you're a member of, and buy the token
contract addresses (CAs) posted there.

A bot can only read groups it was added to, so this logs in as a Telegram *account*
(ideally a second one made for this) through Telegram's own client API (Telethon). The
login lives in data/call_session.json (owner-only, like the wallet): it is full access to
that Telegram account, so it never leaves the server.

Messages from groups are untrusted text: only Solana addresses are taken from them, and
those go through the same filters as every other buy (unless you switch filters off for
a group you trust)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from .config import CallsConfig
    from .store import Store

log = logging.getLogger(__name__)

SESSION_FILE = "call_session.json"
BASE58_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
MAX_PER_MESSAGE = 3      # a message listing dozens of addresses is a list, not a call
SEEN_TTL = 7 * 86400     # a CA is "first posted" once a week, at most
NOT_TOKENS = {           # addresses that show up in calls but are never the coin
    "So11111111111111111111111111111111111111112",   # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "11111111111111111111111111111111",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
}


def extract_cas(text: str) -> list[str]:
    """Solana addresses in a message, in order, without duplicates or known non-coins."""
    from solders.pubkey import Pubkey
    out: list[str] = []
    for m in BASE58_RE.findall(text or ""):
        if m in out or m in NOT_TOKENS:
            continue
        try:
            Pubkey.from_string(m)
        except ValueError:
            continue
        out.append(m)
    return out if len(out) <= MAX_PER_MESSAGE else []


@dataclass
class CallGroup:
    id: int
    title: str
    sol: float = 0.0          # buy size for its calls; 0 = your normal buy size
    filters: bool = True      # False = trust this group: skip the filters
    on: bool = True


class CallGroups:
    """The groups being watched, kept in the database (editable from Telegram)."""

    KEY = "call_groups"

    def __init__(self, store: "Store"):
        self.store = store

    def all(self) -> list[CallGroup]:
        try:
            rows = json.loads(self.store.get_setting(self.KEY) or "[]")
            return [CallGroup(**{k: r[k] for k in r if k in CallGroup.__dataclass_fields__})
                    for r in rows]
        except (ValueError, TypeError):
            return []

    def save(self, groups: list[CallGroup]) -> None:
        self.store.set_setting(self.KEY, json.dumps([asdict(g) for g in groups]))

    def get(self, gid: int) -> Optional[CallGroup]:
        return next((g for g in self.all() if g.id == gid), None)

    def upsert(self, group: CallGroup) -> None:
        groups = [g for g in self.all() if g.id != group.id] + [group]
        self.save(groups)

    def remove(self, gid: int) -> None:
        self.save([g for g in self.all() if g.id != gid])


class CallTracker:
    """First-post and multi-group bookkeeping (pure, so it is easy to test)."""

    def __init__(self, store: "Store"):
        self.store = store
        self.posts: dict[str, dict[int, float]] = {}   # mint -> {group id: first post time}
        self.fired: dict[str, float] = {}               # mint -> when it was acted on
        try:
            saved = json.loads(store.get_setting("call_fired") or "{}")
            self.fired = {k: float(v) for k, v in saved.items()}
        except (ValueError, TypeError):
            pass

    def note(self, mint: str, gid: int, cfg: "CallsConfig", now: Optional[float] = None) -> Optional[int]:
        """Record a post. Returns how many groups posted it, when this is the moment to act
        (first time it reaches `min_groups` within the window), else None."""
        now = now or time.time()
        if mint in self.fired and now - self.fired[mint] < SEEN_TTL:
            return None
        seen = self.posts.setdefault(mint, {})
        seen.setdefault(gid, now)
        window = cfg.group_window_minutes * 60
        recent = {g: t for g, t in seen.items() if now - t <= window}
        self.posts[mint] = recent
        if len(recent) < max(1, cfg.min_groups):
            return None
        self.fired[mint] = now
        self._prune(now)
        self.store.set_setting("call_fired", json.dumps(self.fired))
        return len(recent)

    def _prune(self, now: float) -> None:
        self.fired = {m: t for m, t in self.fired.items() if now - t < SEEN_TTL}
        if len(self.posts) > 5000:
            self.posts = dict(list(self.posts.items())[-2000:])


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class LoginNeedsPassword(Exception):
    """The account has two-step verification: its password is needed too."""


class CallWatcher:
    """The Telegram account connection: login, listing groups, and new-message handling."""

    def __init__(self, data_dir: str, store: "Store",
                 on_call: Callable[[str, CallGroup, int], Awaitable[None]]):
        self.path = Path(data_dir) / SESSION_FILE
        self.groups = CallGroups(store)
        self.tracker = CallTracker(store)
        self.on_call = on_call
        self.client = None
        self.me = ""
        self._login: dict = {}
        self._reconnect = asyncio.Event()

    # ---- saved login ----

    def saved(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    @property
    def logged_in(self) -> bool:
        return bool(self.saved().get("session"))

    def logout(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.me = ""
        self._reconnect.set()

    def _client(self, api_id: int, api_hash: str, session: str = ""):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session), api_id, api_hash,
                              device_model="memecoin-sniper", app_version="1.0")

    # ---- login, driven from the bot chat ----

    async def start_login(self, api_id: int, api_hash: str, phone: str) -> None:
        await self.cancel_login()
        client = self._client(api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        self._login = {"client": client, "api_id": api_id, "api_hash": api_hash,
                       "phone": phone, "hash": sent.phone_code_hash}

    async def finish_code(self, code: str) -> str:
        from telethon.errors import SessionPasswordNeededError
        lg = self._login
        if not lg:
            raise ValueError("no login in progress: start again")
        code = re.sub(r"\D", "", code)   # typed as "1 2 3 4 5" so Telegram doesn't void it
        try:
            await lg["client"].sign_in(phone=lg["phone"], code=code, phone_code_hash=lg["hash"])
        except SessionPasswordNeededError:
            raise LoginNeedsPassword() from None
        return await self._logged_in()

    async def finish_password(self, password: str) -> str:
        lg = self._login
        if not lg:
            raise ValueError("no login in progress: start again")
        await lg["client"].sign_in(password=password)
        return await self._logged_in()

    async def _logged_in(self) -> str:
        lg, self._login = self._login, {}
        client = lg["client"]
        me = await client.get_me()
        _write_private(self.path, json.dumps({"api_id": lg["api_id"], "api_hash": lg["api_hash"],
                                              "session": client.session.save()}))
        await client.disconnect()
        self._reconnect.set()   # the listener picks the new login up
        return _name(me)

    async def cancel_login(self) -> None:
        lg, self._login = self._login, {}
        if lg.get("client"):
            try:
                await lg["client"].disconnect()
            except Exception:
                pass

    # ---- groups ----

    async def list_chats(self, limit: int = 40) -> list[tuple[int, str]]:
        if self.client is None or not self.client.is_connected():
            raise ValueError("the Telegram account isn't connected yet")
        out = []
        async for d in self.client.iter_dialogs(limit=limit):
            if d.is_group or d.is_channel:
                out.append((d.id, d.name or str(d.id)))
        return out

    # ---- listening ----

    async def handle_message(self, chat_id: int, text: str, cfg: "CallsConfig") -> None:
        if not cfg.enabled:
            return
        group = self.groups.get(chat_id)
        if group is None or not group.on:
            return
        for mint in extract_cas(text):
            n = self.tracker.note(mint, chat_id, cfg)
            if n:
                await self.on_call(mint, group, n)

    async def run(self, cfg_ref: Callable[[], "CallsConfig"]) -> None:
        """Stay connected while a login exists; reconnect after a new login or a drop."""
        from telethon import events
        while True:
            saved = self.saved()
            if not saved.get("session"):
                self._reconnect.clear()
                await self._reconnect.wait()
                continue
            client = self._client(int(saved["api_id"]), saved["api_hash"], saved["session"])

            async def on_message(event):
                try:
                    await self.handle_message(event.chat_id, event.raw_text or "", cfg_ref())
                except Exception:
                    log.exception("call handling failed")
            client.add_event_handler(on_message, events.NewMessage(incoming=True))
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    log.warning("call sniper: the Telegram login was revoked; log in again")
                    self.logout()
                    continue
                self.client = client
                self.me = _name(await client.get_me())
                log.info("call sniper connected as %s", self.me)
                self._reconnect.clear()
                done = asyncio.ensure_future(client.run_until_disconnected())
                stop = asyncio.ensure_future(self._reconnect.wait())
                await asyncio.wait({done, stop}, return_when=asyncio.FIRST_COMPLETED)
                stop.cancel()
                if not done.done():
                    done.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("call sniper connection failed: %s", e)
                await asyncio.sleep(15)
            finally:
                self.client = None
                try:
                    await client.disconnect()
                except Exception:
                    pass


def _name(me) -> str:
    if me is None:
        return "?"
    user = getattr(me, "username", None)
    first = getattr(me, "first_name", None) or ""
    return f"@{user}" if user else (first or str(getattr(me, "id", "?")))
