"""Telegram UI: the whole bot is run from a chat, Trojan / BonkBot style.

* Pairing: the first chat to send `/start <code>` (code printed in the server log)
  becomes the owner. Every other chat is ignored. Setting TELEGRAM_CHAT_ID pins the owner.
* Menus: wallet (create / import / export / deposit / withdraw), start/pause,
  paper <-> live, positions with sell buttons, every strategy setting, presets,
  copy-trade wallets, stats.
* Paste a token address to get a buy card.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Optional

import httpx

from .notify import TELEGRAM_API
from .calls import CallGroup
from .settings import BY_KEY, GROUPS, SETTINGS, format_value, get_value, group_of

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SECRET_RE = re.compile(r"^([1-9A-HJ-NP-Za-km-z]{80,90}|\[\s*\d+(\s*,\s*\d+){63}\s*\])$")
PENDING_TTL = 300
EXPORT_TTL = 60
MAX_PAIR_ATTEMPTS = 10   # wrong codes before the pairing code is replaced


class TelegramError(RuntimeError):
    """Telegram API error. Never includes the request URL (it contains the bot token)."""

HELP = """<b>Everything is in the menu</b>, tap /menu.

Shortcuts:
• Paste a token address to get a buy card
/positions to see open positions with sell buttons
/orders to see limit orders
/card &lt;mint&gt; for a token research card
/track &lt;wallet&gt; [label] to get alerts when a wallet trades
/buy &lt;mint&gt; [sol] [force]
/sell &lt;mint|symbol&gt; [pct]
/pause, /resume
/setbuy &lt;sol&gt;
/copy list | add &lt;wallet&gt; [label] [sol] [track] | rm &lt;wallet&gt;
/withdraw &lt;address&gt; &lt;sol|all&gt;
/block &lt;creator&gt;
/momentum on | off (tokens pumping right now)
/health to check every service the bot depends on
/why to see why recent launches weren't bought
/calls to buy CAs posted in Telegram groups you're in
/reclaim to take back the ~0.002 SOL rent from empty token accounts
/stats"""

ANNOUNCED = ("🟢 BUY", "🔴 SELL")

WELCOME = """👋 <b>Paired!</b> This chat now controls your sniper.

<b>Getting started</b>
1. 💼 <b>Wallet</b> → create a new wallet (or import one)
2. Send SOL to the deposit address shown
3. ▶️ <b>Start sniping</b> in 📝 PAPER mode first to see how it trades
4. When you're happy, tap 🔴 <b>Go LIVE</b>

Use a dedicated wallet holding only what you can afford to lose."""


WHY_BUTTONS = [[("🔄 Again", "why"), ("🔁 Reset counts", "whyz")], [("🏠 Menu", "m")]]


def wallet_addresses(text: str, limit: int = 20) -> list[str]:
    """Wallet addresses in pasted text or links (solscan.io/account/…, gmgn.ai/sol/address/…)."""
    from solders.pubkey import Pubkey
    out: list[str] = []
    for m in re.findall(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])",
                        text or ""):
        try:
            Pubkey.from_string(m)
        except ValueError:
            continue
        if m not in out:
            out.append(m)
    return out[:limit]


class TelegramControl:
    def __init__(self, engine: "Engine", token: str, chat_id: str, http: httpx.AsyncClient):
        self.engine, self.token, self.http = engine, token, http
        self.owner = str(chat_id or engine.store.get_setting("owner_chat_id") or "")
        # the same code survives restarts until it's used, so the one in the log always works
        self.pair_code = "" if self.owner else (
            engine.store.get_setting("pair_code") or self._save_code(self._new_code()))
        self.pair_failures = 0
        self.offset = 0
        self.pending: Optional[dict] = None
        self.unhandled: list[str] = []
        self.charts: dict[str, asyncio.Task] = {}   # mint -> live chart updater
        self.wallet_wiz: Optional[dict] = None       # a copy / track wallet being added
        engine.notifier.token = token
        engine.notifier.chat = self.owner

    # ---------- transport ----------

    def _save_code(self, code: str) -> str:
        self.engine.store.set_setting("pair_code", code)
        return code

    @staticmethod
    def _new_code() -> str:
        return secrets.token_hex(5).upper()  # 10 hex chars: not brute-forceable over Telegram

    async def api(self, method: str, **params):
        try:
            resp = await self.http.post(f"{TELEGRAM_API}/bot{self.token}/{method}",
                                        json=params, timeout=35)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok", False):
            raise TelegramError(f"{method}: {data.get('description') or resp.status_code}")
        return data.get("result")

    async def api_files(self, method: str, files: dict, **params):
        """Like api(), for uploads (photos): sent as multipart form data."""
        import json
        form = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in params.items()}
        try:
            resp = await self.http.post(f"{TELEGRAM_API}/bot{self.token}/{method}",
                                        data=form, files=files, timeout=35)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not data.get("ok", False):
            raise TelegramError(f"{method}: {data.get('description') or resp.status_code}")
        return data.get("result")

    async def send(self, text: str, buttons=None) -> None:
        await self.engine.notifier.telegram(text, buttons, chat_id=self.owner)

    async def show(self, text: str, buttons=None, msg_id: Optional[int] = None) -> None:
        """Edit the menu message in place when possible, otherwise send a new one."""
        if msg_id:
            from .notify import keyboard
            try:
                params = {"chat_id": self.owner, "message_id": msg_id, "text": text,
                          "parse_mode": "HTML", "disable_web_page_preview": True}
                if buttons:
                    params["reply_markup"] = keyboard(buttons)
                await self.api("editMessageText", **params)
                return
            except TelegramError as e:
                if "not modified" in str(e):  # tapped Refresh and nothing changed
                    return
            except Exception:
                pass
        await self.send(text, buttons)

    async def run(self) -> None:
        try:
            me = await self.api("getMe")
            log.info("telegram bot @%s ready", (me or {}).get("username", "?"))
        except Exception as e:
            if any(x in str(e) for x in ("Unauthorized", "Not Found", "401", "404")):
                log.error("Telegram rejected the bot token (%s). Check TELEGRAM_BOT_TOKEN in .env.", e)
            else:
                log.warning("can't reach Telegram yet (%s); will keep retrying", e)
        try:
            await self.api("setMyCommands", commands=[
                {"command": c, "description": d} for c, d in [
                    ("menu", "Main menu"), ("positions", "Open positions"), ("wallet", "Wallet"),
                    ("settings", "Settings"), ("stats", "Performance"),
                    ("health", "Check every service"), ("why", "Why it isn't buying"),
                    ("help", "Help")]])
        except Exception as e:
            log.debug("setMyCommands failed: %s", e)
        wait = 2.0
        while not await self._drop_backlog():  # never run stale taps: retry until it works
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
        if self.pair_code:  # only now: a code sent from here on is never dropped as backlog
            log.warning("📱 Telegram not paired yet. Send this to your bot:  /start %s", self.pair_code)
        else:
            log.info("telegram ready: paired, send /menu to your bot")
        backoff = 5.0
        while True:
            try:
                updates = await self.api("getUpdates", offset=self.offset, timeout=25,
                                         allowed_updates=["message", "callback_query"])
                backoff = 5.0
                for u in updates or []:
                    self.offset = u["update_id"] + 1
                    await self.handle_update(u)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if "Conflict" in str(e):
                    log.error("Another copy of this bot is running with the same token. Stop it "
                              "(only one may poll Telegram at a time).")
                else:
                    log.warning("telegram poll failed: %s (retry in %.0fs)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _drop_backlog(self) -> bool:
        """Ignore taps and commands sent while the bot was down. Running a "Buy" tapped an
        hour ago, or re-running one handled just before a crash, would be dangerous.
        Returns False if Telegram couldn't be reached (the caller retries)."""
        try:
            last = await self.api("getUpdates", offset=-1, timeout=0)
            if last:
                self.offset = last[-1]["update_id"] + 1
                await self.api("getUpdates", offset=self.offset, timeout=0)  # confirm
                log.info("ignored Telegram updates sent while the bot was offline")
                if self.owner:
                    await self.send("ℹ️ Restarted. Anything tapped while I was offline was "
                                    "ignored for safety: tap /menu to continue.")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("can't reach Telegram yet (%s); retrying", e)
            return False

    # ---------- routing ----------

    async def handle_update(self, u: dict) -> None:
        try:
            await self._route(u)
        except Exception as e:
            log.exception("telegram update failed")
            if self.owner:
                await self.send(f"⚠️ {html.escape(str(e))}", [[("🏠 Menu", "m")]])

    def _from_owner(self, sender: dict) -> bool:
        """The owner chat is a private chat, whose id is the owner's user id. Checking the
        sender too means a group chat id can never hand the bot to every member."""
        sid = (sender or {}).get("id")
        return sid is None or str(sid) == self.owner

    async def _route(self, u: dict) -> None:
        if self.owner.startswith("-"):  # a group or channel id: refuse, everyone could trade
            if not getattr(self, "_warned_group", False):
                self._warned_group = True
                log.error("TELEGRAM_CHAT_ID / the paired chat is a group (%s). For safety the bot "
                          "only obeys a private chat: set your own chat id.", self.owner)
            return
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            if not self.owner or str(msg.get("chat", {}).get("id")) != self.owner:
                return
            if not self._from_owner(cq.get("from")):
                return
            try:
                await self.api("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            await self.handle_callback(cq.get("data", ""), msg.get("message_id"))
            return

        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not self.owner:
            await self._try_pair(chat, text, (msg.get("chat") or {}).get("type", "private"))
            return
        if chat != self.owner or not self._from_owner(msg.get("from")):
            return
        if self.pending and time.monotonic() > self.pending["expires"]:
            self.pending = None  # expire first, so a key sent to a stale prompt is still deleted
        if SECRET_RE.match(text) and not (self.pending and self.pending["kind"] == "import"):
            try:
                await self.api("deleteMessage", chat_id=self.owner, message_id=msg.get("message_id"))
            except Exception:
                pass
            await self.send("🔐 That looked like a private key, so I deleted it. To import a "
                            "wallet use 💼 Wallet → 📥 Import, then send it.")
            return
        if self.pending and not text.startswith("/"):
            if time.monotonic() > self.pending["expires"]:
                self.pending = None
            else:
                await self.handle_pending(text, msg.get("message_id"))
                return
        if text.startswith("/"):
            self.pending = None
            await self.handle_command(text)
        elif ADDRESS_RE.match(text):
            self._card(text)
        else:
            await self.main_menu()

    async def _try_pair(self, chat: str, text: str, chat_type: str = "private") -> None:
        parts = text.split()
        if len(parts) != 2 or parts[0].split("@")[0] not in ("/start", "/pair"):
            return
        if chat_type != "private":  # in a group, every member would control the wallet
            await self.engine.notifier.telegram(
                "🔒 For safety I only pair in a private chat. Open a direct chat with me and "
                "send the code there.", chat_id=chat)
            return
        if not secrets.compare_digest(parts[1].upper().encode(), self.pair_code.encode()):
            self.pair_failures += 1
            if self.pair_failures >= MAX_PAIR_ATTEMPTS:
                self.pair_failures = 0
                self.pair_code = self._save_code(self._new_code())
                log.warning("too many wrong pairing codes; new code:  /start %s", self.pair_code)
            return
        self.owner = chat
        self.pair_code = ""
        self.engine.store.set_setting("pair_code", "")  # used up
        self.engine.store.set_setting("owner_chat_id", chat)
        self.engine.notifier.chat = chat
        log.info("telegram paired with chat %s", chat)
        await self.send(WELCOME)
        await self.main_menu()

    # ---------- commands ----------

    async def handle_command(self, text: str) -> None:
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        e = self.engine
        if cmd in ("/start", "/menu", "/status"):
            await self.main_menu()
        elif cmd == "/help":
            await self.send(HELP)
        elif cmd == "/wallet":
            await self.wallet_menu()
        elif cmd == "/settings":
            await self.settings_menu()
        elif cmd == "/positions":
            await self.positions()
        elif cmd == "/orders":
            await self.orders_menu()
        elif cmd == "/card" and args:
            self._card(args[0])
        elif cmd == "/track" and args:
            await e.add_copy_wallet(args[0], args[1] if len(args) > 1 else "", 0.0, mode="alert")
            await self.send(f"🔔 Tracking {html.escape(args[1] if len(args) > 1 else args[0])}")
        elif cmd == "/buy" and args:
            sol = float(args[1]) if len(args) > 1 and args[1] != "force" else None
            self._bg_reply(e.manual_buy(args[0], sol, force="force" in args))
        elif cmd == "/sell" and args:
            pct = float(args[1].rstrip("%")) if len(args) > 1 else 100.0
            self._bg_reply(e.manual_sell(args[0], pct))
        elif cmd == "/pause":
            e.set_paused(True)
            await self.send("⏸ Sniping paused. Open positions are still managed.")
        elif cmd == "/resume":
            e.set_paused(False)
            await self.send("▶️ Sniping resumed.")
        elif cmd == "/setbuy" and args:
            await self.send(await e.set_setting("trading.buy_amount_sol", args[0]))
        elif cmd == "/copy":
            await self.copy_command(args)
        elif cmd == "/withdraw" and len(args) == 2:
            await self.confirm_withdraw(args[0], args[1])
        elif cmd == "/block" and args:
            e.store.block(args[0], "manual")
            await self.send(f"🚫 blocked {html.escape(args[0])}")
        elif cmd == "/momentum":
            if args and args[0].lower() in ("on", "off"):
                await self.send("🚀 " + html.escape(await e.set_setting(
                    "discovery.momentum_enabled", args[0].lower() == "on")))
            else:
                await self.settings_group("momentum")
        elif cmd == "/calls":
            await self.calls_menu()
        elif cmd == "/why":
            await self.send(self.engine.why_summary(), WHY_BUTTONS)
        elif cmd == "/health":
            await self.health()
        elif cmd == "/reclaim":
            await self.reclaim()
        elif cmd == "/stats":
            await self.stats()
        else:
            await self.send(HELP)

    # ---------- callbacks ----------

    async def handle_callback(self, data: str, msg_id: Optional[int]) -> None:
        e = self.engine
        if data != "wd!":
            self.pending = None  # navigating away abandons any half-finished prompt
        head, _, rest = data.partition(":")
        if data == "m":
            await self.main_menu(msg_id)
        elif data == "x":
            self.pending = None
            await e.calls.cancel_login()
            self.wallet_wiz = None
            await self.main_menu(msg_id)
        elif data == "go":
            e.set_paused(False)
            await self.main_menu(msg_id)
        elif data == "stop":
            e.set_paused(True)
            await self.main_menu(msg_id)
        elif data in ("p", "refresh"):
            await self.positions()
        elif head == "s":
            mint, _, pct = rest.rpartition(":")
            self._bg_reply(e.manual_sell(mint, float(pct)))
        elif head in ("b", "bf"):
            mint, _, sol = rest.partition(":")
            self._bg_reply(e.manual_buy(mint, float(sol) if sol else None, force=head == "bf"))
        elif head == "tc":
            self._card(rest)
        elif head == "ch":
            await self.start_chart(rest)
        elif head == "chx":
            if not self.stop_chart(rest):
                await self.send("That chart has already stopped.")
        elif head == "lb":
            self._ask("limit_buy", rest)
            await self.send("📋 <b>Limit buy</b>: send <code>&lt;SOL&gt; &lt;change %&gt; [hours]</code>\n"
                            "e.g. <code>0.1 -30</code> buys 0.1 SOL if the price dips 30%,\n"
                            "<code>0.1 +50 6</code> buys on a 50% breakout within 6 hours.",
                            [[("✖️ Cancel", "x")]])
        elif head == "ls":
            self._ask("limit_sell", rest)
            await self.send("📋 <b>Limit sell</b>: send <code>&lt;% of bag&gt; &lt;profit %&gt; [hours]</code>\n"
                            "e.g. <code>50 +100</code> sells half at 2x,\n"
                            "<code>100 -20</code> sells everything if it falls to -20% from entry.",
                            [[("✖️ Cancel", "x")]])
        elif data == "o":
            await self.orders_menu(msg_id)
        elif head == "oc":
            ok = e.cancel_order(int(rest))
            await self.send(f"Order #{rest} cancelled." if ok else f"Order #{rest} is no longer open.")
            await self.orders_menu()
        elif data == "sn":
            await self.settings_group("snipe", msg_id)
        elif head == "cm":
            w = next((x for x in e.copy_wallets() if x.address == rest), None)
            stored = {x["address"]: x for x in e.store.copy_wallets()}
            current = (w.mode if w else stored.get(rest, {}).get("mode", "copy"))
            await e.set_wallet_mode(rest, "alert" if current == "copy" else "copy")
            await self.wallet_page(rest, msg_id)
        elif head == "bc":
            self._ask("buy_custom", rest)
            await self.send("Send the amount of SOL to buy:", [[("✖️ Cancel", "x")]])
        elif data == "mo":  # one-tap on/off from the main menu
            await e.set_setting("discovery.momentum_enabled", not e.cfg.discovery.momentum_enabled)
            await self.main_menu(msg_id)
        elif data == "hl":
            await self.health()
        elif data == "st":
            await self.stats()
        elif data == "cl" or head in ("cl", "ca", "cg", "cgs", "cgf", "cgo", "cgr"):
            await self.calls_action(head if data != "cl" else "cl", rest, msg_id)
        elif data == "whyz":
            e.reset_why()
            await self.send("🔁 Why-no-buys counts reset. Tap 🔄 Again in a few minutes to see "
                            "how your current settings are doing.", WHY_BUTTONS)
        elif data == "why":
            await self.send(self.engine.why_summary(), WHY_BUTTONS)
        elif data == "sts":
            await self.show(f"Start a new {e.mode.upper()} PnL counter from now? All-time results "
                            "stay exactly as they are; the stats page just adds a 'since reset' "
                            "section on top.",
                            [[("✅ Yes, reset counter", "sts!"), ("✖️ Cancel", "st")]], msg_id)
        elif data == "sts!":
            e.store.set_setting(self._stats_since_key(), str(time.time()))
            await self.send(f"🔁 {e.mode.upper()} PnL counter reset. All-time results are kept.",
                            [[("📈 Stats", "st"), ("🏠 Menu", "m")]])
        elif data == "stz":
            await self.show("Clear all PAPER results and start counting from zero? Live results "
                            "and open positions aren't touched.",
                            [[("✅ Yes, clear paper results", "stz!"), ("✖️ Cancel", "st")]], msg_id)
        elif data == "stz!":
            await self.send(self.engine.reset_paper_results(), [[("📈 Stats", "st"), ("🏠 Menu", "m")]])
        elif data == "w":
            await self.wallet_menu(msg_id)
        elif data.startswith("w:"):
            await self.wallet_action(rest, msg_id)
        elif head == "mode":
            await self.mode_action(rest, msg_id)
        elif data == "set":
            await self.settings_menu(msg_id)
        elif head == "set":
            await self.settings_group(rest, msg_id)
        elif head == "e":
            await self.edit_setting(int(rest), msg_id)
        elif data == "pre":
            await self.preset_menu(msg_id)
        elif head == "pre":
            await self.send(await e.apply_preset(rest))
            await self.settings_menu()
        elif data == "rst":
            await self.show("Reset every custom setting back to the preset?",
                            [[("✅ Yes, reset", "rst!"), ("✖️ Cancel", "set")]], msg_id)
        elif data == "rst!":
            await self.send(await e.reset_settings())
            await self.settings_menu()
        elif data == "c":
            await self.copy_menu(msg_id)
        elif data == "c:add":
            self._ask("copy_add")
            await self.send("➕ <b>Add a wallet</b>\n\nPaste its address, or a link to it "
                            "(Solscan, GMGN, Birdeye…). Several at once is fine, one per line.",
                            [[("✖️ Cancel", "x")]])
        elif head == "cw":
            await self.wallet_wizard(rest)
        elif head == "wp":
            await self.wallet_page(rest, msg_id)
        elif head in ("wpp", "wpx", "wpf", "wpk", "wpz", "wpl", "wpc"):
            await self.copy_wallet_action(head, rest, msg_id)
        elif data.startswith("c:rm:"):
            await e.remove_copy_wallet(data[5:])
            await self.copy_menu(msg_id)
        elif data == "wd!":
            await self.do_withdraw()
        else:
            self.unhandled = self.unhandled[-49:] + [data]  # bounded: kept for diagnostics
            log.warning("unhandled Telegram button: %s", data)

    def _bg_reply(self, coro) -> None:
        """Run a slow action (a trade can take ~90s to confirm) without freezing the chat;
        the result arrives as its own message."""
        async def runner():
            try:
                result = await coro
                # a fill already went out as its own 🟢 BUY / 🔴 SELL message: don't repeat it
                if result and not result.startswith(ANNOUNCED):
                    await self.send(result)
            except Exception as e:
                log.exception("telegram action failed")
                await self.send(f"⚠️ {html.escape(str(e))}", [[("🏠 Menu", "m")]])
        self.engine._spawn(runner())

    # ---------- pending text replies ----------

    def _ask(self, kind: str, data=None) -> None:
        self.pending = {"kind": kind, "data": data, "expires": time.monotonic() + PENDING_TTL}

    async def handle_pending(self, text: str, msg_id: Optional[int]) -> None:
        p, self.pending = self.pending, None
        kind = p["kind"]
        e = self.engine
        if kind == "withdraw_confirm":  # a button press is needed, not text: keep it open
            self.pending = p
            await self.send("Tap ✅ Confirm withdraw above, or ✖️ Cancel.",
                            [[("✅ Confirm withdraw", "wd!"), ("✖️ Cancel", "x")]])
            return
        try:
            if kind == "import":
                if msg_id:  # never leave a private key sitting in chat history
                    try:
                        await self.api("deleteMessage", chat_id=self.owner, message_id=msg_id)
                    except Exception:
                        pass
                blocked = self._live_blocked()
                if blocked:
                    await self.send(blocked)
                    return
                kp = e.wallet.import_secret(text)
                await self.send(f"✅ Wallet imported: <code>{kp.pubkey()}</code>\n"
                                "Your message with the key was deleted.")
                await self._wallet_changed()
            elif kind == "withdraw":
                parts = text.split()
                if len(parts) != 2:
                    raise ValueError("send: <address> <amount|all>")
                await self.confirm_withdraw(*parts)
            elif kind == "edit":
                await self.send("✅ " + html.escape(await e.set_setting(p["data"], text)))
                await self.settings_group(group_of(BY_KEY[p["data"]]))
            elif kind == "copy_add":
                await self.copy_add_text(text)
            elif kind == "copy_name":
                await self.wallet_wizard("n", text)
            elif kind in ("wallet_size", "wallet_min", "wallet_mcap"):
                await self.wallet_text(kind, text, p["data"])
            elif kind == "copy_size":
                await self.wallet_wizard("s:", text.replace("SOL", "").strip())
            elif kind == "limit_buy":
                parts = text.replace("%", "").replace("SOL", "").split()
                if len(parts) not in (2, 3):
                    raise ValueError("send: <SOL> <change %> [hours], e.g. 0.1 -30")
                hours = float(parts[2]) if len(parts) == 3 else 24.0
                await self.send(await e.place_limit_buy(p["data"], float(parts[0]), float(parts[1]), hours))
            elif kind == "limit_sell":
                parts = text.replace("%", "").split()
                if len(parts) not in (2, 3):
                    raise ValueError("send: <% of bag> <profit %> [hours], e.g. 50 +100")
                hours = float(parts[2]) if len(parts) == 3 else 24.0
                await self.send(await e.place_limit_sell(p["data"], float(parts[0]), float(parts[1]), hours))
            elif kind == "buy_custom":
                self._bg_reply(e.manual_buy(p["data"], float(text.replace("SOL", "").strip())))
            elif kind.startswith("call_"):
                await self.calls_text(kind, text, msg_id, p)
        except Exception as ex:
            self.pending = p  # let them try again
            p["expires"] = time.monotonic() + PENDING_TTL
            await self.send(f"⚠️ {html.escape(str(ex))}\nTry again, or tap Cancel.", [[("✖️ Cancel", "x")]])

    # ---------- screens ----------

    async def main_menu(self, msg_id: Optional[int] = None) -> None:
        e = self.engine
        kp = e.wallet.keypair()
        mode = "🔴 LIVE" if e.live else "📝 PAPER"
        state = "⏸ paused" if e.paused else "▶️ sniping"
        lines = ["🎯 <b>Memecoin Sniper</b>", f"{mode} · {state} · preset <b>{e.cfg.preset}</b>"]
        if kp:
            bal = await self._balance(str(kp.pubkey()))
            lines.append(f"💼 <code>{kp.pubkey()}</code>\n💰 {bal}")
        else:
            lines.append("💼 No wallet yet. Tap <b>Wallet</b> to create one.")
        open_n = sum(1 for p in e.positions.values() if not p.closed)
        lines.append(f"📊 Open {open_n}/{e.cfg.trading.max_open_positions} · today "
                     f"{e.store.realized_today():+.4f} SOL · buy {e.cfg.trading.buy_amount_sol:g} SOL")
        d = e.cfg.discovery
        snipe = {"all": "all launches passing filters",
                 "targeted": f"targeted ({len(d.dev_watchlist)} devs, {len(d.snipe_keywords)} keywords)",
                 "off": "off (manual, copy and limit orders only)"}.get(d.auto_snipe, d.auto_snipe)
        lines.append(f"🎯 Auto-snipe: {snipe}")
        if d.momentum_enabled:
            lines.append("🚀 Momentum scanner: on ("
                         + ("auto-buy" if d.momentum_action == "buy" else "alerts") + ")")
        orders = len(e.store.open_orders())
        if orders:
            lines.append(f"📋 {orders} open limit order(s)")
        pending = len(e.pending_buys())
        if pending:
            lines.append(f"⏳ {pending} unconfirmed buy(s): checking the wallet")
        if not e.has_pumpportal_key:
            lines.append("ℹ️ No PumpPortal key: prices use on-chain polling, and copy trading is off.")
        elif not e.has_trade_stream:
            lines.append("⚠️ PumpPortal is refusing the live trade feed: using on-chain checks. "
                         "Top up the wallet linked to your PumpPortal key (min 0.02 SOL).")
        if "api.mainnet-beta.solana.com" in e.cfg.endpoints.rpc_url:
            lines.append("⚠️ Public Solana RPC: set SOLANA_RPC_URL before going live.")
        toggle = ("⏸ Pause sniping", "stop") if not e.paused else ("▶️ Start sniping", "go")
        switch = ("📝 Go PAPER", "mode:paper") if e.live else ("🔴 Go LIVE", "mode:live")
        await self.show("\n".join(lines), [
            [toggle],
            [("💼 Wallet", "w"), ("📊 Positions", "p")],
            [("🎯 Snipers", "sn"), ("📋 Orders", "o")],
            [("👥 Copy & track", "c"), ("⚙️ Settings", "set")],
            [("📈 Stats", "st"), switch],
            [("📣 Call sniper", "cl")],
            [("🚀 Momentum: " + ("✅ on" if d.momentum_enabled else "off"), "mo"),
             ("🩺 Health", "hl"), ("🔄 Refresh", "m")],
        ], msg_id)

    async def _balance(self, address: str) -> str:
        try:
            return f"{await self.engine.rpc.get_balance_sol(address):.4f} SOL"
        except Exception:
            return "balance unavailable"

    async def wallet_menu(self, msg_id: Optional[int] = None) -> None:
        w = self.engine.wallet
        kp = w.keypair()
        if not kp:
            await self.show("💼 <b>Wallet</b>\n\nNo wallet yet.", [
                [("✨ Create new wallet", "w:new")], [("📥 Import private key", "w:imp")],
                [("⬅️ Back", "m")]], msg_id)
            return
        lock = "\n🔒 Set via .env, so it can't be changed from chat." if w.from_env else ""
        rows = [[("📤 Withdraw SOL", "w:wd"), ("🔑 Export key", "w:exp")],
                [("🧹 Reclaim rent from empty token accounts", "w:rc")]]
        if not w.from_env:
            rows.append([("✨ New wallet", "w:new"), ("📥 Import", "w:imp")])
        rows.append([("🔄 Refresh", "w"), ("⬅️ Back", "m")])
        await self.show(
            f"💼 <b>Wallet</b>\n\n<b>Deposit address</b> (tap to copy):\n<code>{kp.pubkey()}</code>\n\n"
            f"Balance: {await self._balance(str(kp.pubkey()))}\n"
            f"https://solscan.io/account/{kp.pubkey()}{lock}", rows, msg_id)

    async def wallet_action(self, action: str, msg_id: Optional[int]) -> None:
        w = self.engine.wallet
        if action == "rc":
            await self.reclaim()
            return
        if action == "new":
            if w.keypair():
                await self.show("⚠️ Replace your current wallet with a new one?\n"
                                "The old key is kept as a backup file on the server, but withdraw "
                                "its funds first.",
                                [[("✅ Yes, create new", "w:new!"), ("✖️ Cancel", "w")]], msg_id)
                return
            action = "new!"
        if action == "new!":
            blocked = self._live_blocked()
            if blocked:
                await self.send(blocked)
                return
            kp = w.create()
            await self.send(f"✨ New wallet created.\n\nDeposit SOL to:\n<code>{kp.pubkey()}</code>\n\n"
                            "Back up the key: 💼 Wallet → 🔑 Export key.")
            await self._wallet_changed()
        elif action == "imp":
            blocked = self._live_blocked()
            if blocked:
                await self.send(blocked)
                return
            self._ask("import")
            await self.send("Send the private key (base58 or JSON array).\n"
                            "I'll delete your message as soon as I've read it.", [[("✖️ Cancel", "x")]])
        elif action in ("exp", "exp!") and not self.engine.cfg.allow_key_export:
            await self.send("🔒 Key export is turned off (ALLOW_KEY_EXPORT=false in .env). "
                            "On the server the key is in data/wallet.key.")
        elif action == "exp":
            await self.show("⚠️ Anyone who sees your private key can take your funds.\n"
                            f"Show it here? It will be deleted after {EXPORT_TTL}s.",
                            [[("🔑 Show key", "w:exp!"), ("✖️ Cancel", "w")]], msg_id)
        elif action == "exp!":
            res = await self.api("sendMessage", chat_id=self.owner, parse_mode="HTML",
                                 text=f"🔑 <tg-spoiler><code>{w.export()}</code></tg-spoiler>\n"
                                      f"<i>Deleting in {EXPORT_TTL}s.</i>")
            mid = (res or {}).get("message_id")
            if mid:
                self.engine._spawn(self._delete_later(mid, EXPORT_TTL))
        elif action == "wd":
            self._ask("withdraw")
            await self.send("Send: <code>&lt;destination address&gt; &lt;amount in SOL | all&gt;</code>",
                            [[("✖️ Cancel", "x")]])

    def _live_blocked(self) -> Optional[str]:
        """Why the wallet can't be replaced right now, or None. Live positions (even while in
        paper mode) are tied to the current wallet: a new one would orphan them."""
        e = self.engine
        if e.live:
            return "Switch to PAPER mode before changing wallets."
        n = e.store.db.execute("SELECT COUNT(*) FROM positions WHERE mode = 'live' AND closed = 0"
                               ).fetchone()[0]
        if n or e.store.get_setting("pending_buys:live") not in (None, "{}"):
            return (f"You still have {n or 'pending'} LIVE position(s) held by this wallet. Go live "
                    "and sell them first, or they'd be left behind in the old wallet.")
        return None

    async def _wallet_changed(self) -> None:
        await self.wallet_menu()

    async def _delete_later(self, msg_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self.api("deleteMessage", chat_id=self.owner, message_id=msg_id)
        except Exception:
            pass

    async def confirm_withdraw(self, address: str, amount: str) -> None:
        from .engine import solana_address
        solana_address(address)
        try:
            sol = None if amount.lower() == "all" else float(amount)
        except ValueError:
            raise ValueError("the amount must be a number of SOL (e.g. 0.1) or 'all'") from None
        if sol is not None and not (0 < sol < 1e9):  # also rejects nan / inf
            raise ValueError("amount must be a positive number of SOL, or 'all'")
        self.pending = {"kind": "withdraw_confirm", "data": (address, sol),
                        "expires": time.monotonic() + PENDING_TTL}
        open_n = sum(1 for p in self.engine.positions.values() if not p.closed)
        warn = f"\n⚠️ You have {open_n} open position(s); keep SOL for sell fees." if open_n else ""
        await self.send(f"Send <b>{'ALL' if sol is None else f'{sol:g} SOL'}</b> to\n"
                        f"<code>{address}</code>?{warn}",
                        [[("✅ Confirm withdraw", "wd!"), ("✖️ Cancel", "x")]])

    async def do_withdraw(self) -> None:
        p, self.pending = self.pending, None
        if not p or p["kind"] != "withdraw_confirm" or time.monotonic() > p["expires"]:
            await self.send("That withdrawal expired. Start again from 💼 Wallet.")
            return
        address, sol = p["data"]
        await self.send("⏳ Sending…")
        self._bg_reply(self.engine.withdraw(address, sol))

    async def mode_action(self, action: str, msg_id: Optional[int]) -> None:
        e = self.engine
        if action == "paper":
            await self.send(await e.switch_mode(False))
            await self.main_menu()
        elif action == "live":
            kp = e.wallet.keypair()
            if not kp:
                await self.show("Create or import a wallet first.", [[("💼 Wallet", "w")]], msg_id)
                return
            t = e.cfg.trading
            await self.show(
                "🔴 <b>Switch to LIVE trading?</b>\n\nThe bot will spend real SOL from\n"
                f"<code>{kp.pubkey()}</code> ({await self._balance(str(kp.pubkey()))})\n\n"
                f"• {t.buy_amount_sol:g} SOL per trade, up to {t.max_open_positions} positions\n"
                f"• stops for the day after {t.daily_loss_limit_sol:g} SOL realized loss\n"
                f"• sniping is <b>{'paused' if e.paused else 'running'}</b>",
                [[("✅ Go LIVE", "mode:live!"), ("✖️ Cancel", "m")]], msg_id)
        elif action == "live!":
            await self.send(await e.switch_mode(True))
            await self.main_menu()

    async def positions(self) -> None:
        open_pos = [p for p in self.engine.positions.values() if not p.closed]
        if not open_pos:
            await self.send("No open positions.", [[("⬅️ Menu", "m")]])
            return
        from . import exits
        for p in open_pos:
            value = p.tokens_remaining * p.last_price
            tags = []
            if exits.in_moonbag(p, self.engine.exit_cfg(p)):
                tags.append("🌙 moonbag")
            if p.initials_taken:
                tags.append("💰 initials out")
            await self.send(
                f"<b>{html.escape(p.symbol)}</b> <code>{p.mint}</code> {' · '.join(tags)}\n"
                f"PnL {p.pnl_pct:+.0f}% · value {value:.4f} SOL · in {p.sol_in:.4f} · "
                f"out {p.sol_out:.4f}\nPeak {((p.peak_price / p.entry_price - 1) * 100) if p.entry_price else 0:+.0f}% · "
                f"via {html.escape(p.source)}",
                buttons=[[("Sell 25%", f"s:{p.mint}:25"), ("Sell 50%", f"s:{p.mint}:50"),
                          ("Sell 100%", f"s:{p.mint}:100")],
                         [("📈 Live chart", f"ch:{p.mint}"), ("📋 Limit sell", f"ls:{p.mint}")],
                         [("🔍 Card", f"tc:{p.mint}"), ("🔄 Refresh", "refresh")]])

    # ---------- call sniper (Telegram groups) ----------

    async def calls_menu(self, msg_id: Optional[int] = None) -> None:
        e = self.engine
        w, c = e.calls, e.cfg.calls
        if not w.logged_in:
            await self.show(
                "📣 <b>Call sniper</b>\n\nBuys contract addresses (CAs) posted in Telegram "
                "groups and channels you're in. A bot can't read those, so this logs in as a "
                "Telegram account. <b>Use a second account made for this</b>, not your main "
                "one: the login is full access to that account (it stays on your server).\n\n"
                "You'll need from <b>my.telegram.org</b> → API development tools: the "
                "<b>api_id</b> and <b>api_hash</b> (any app name works).",
                [[("🔑 Log in", "cl:login")], [("⬅️ Menu", "m")]], msg_id)
            return
        groups = w.groups.all()
        state = ("✅ on" if c.enabled else "off") + (" · alerts only" if c.action != "buy" else "")
        lines = [f"📣 <b>Call sniper</b>: {state}",
                 f"Account: {html.escape(w.me or 'connecting…')}",
                 f"Act after: {c.min_groups} group(s)" + (
                     f" within {c.group_window_minutes:g} min" if c.min_groups > 1 else ""),
                 "", f"<b>Watching {len(groups)} group(s)</b>" + (":" if groups else
                                                                  ". Tap ➕ Add group.")]
        rows = []
        for g in groups:
            size = f"{g.sol:g} SOL" if g.sol else "normal size"
            flags = ("" if g.on else "⏸ ") + ("" if g.filters else "⚠️ no filters · ")
            lines.append(f"• {html.escape(g.title)}: {flags}{size}")
            rows.append([(f"⚙️ {g.title[:30]}", f"cg:{g.id}")])
        rows = [[("⏸ Turn off" if c.enabled else "▶️ Turn on", "cl:on"),
                 ("➕ Add group", "cl:add")]] + rows + [
            [("⚙️ Options", "cl:opt"), ("🚪 Log out", "cl:out")], [("⬅️ Menu", "m")]]
        await self.show("\n".join(lines), rows, msg_id)

    async def calls_action(self, head: str, rest: str, msg_id: Optional[int]) -> None:
        e = self.engine
        w = e.calls
        if head == "cl" and rest in ("", "back"):
            await self.calls_menu(msg_id)
        elif head == "cl" and rest == "login":
            self._ask("call_api")
            await self.send("1️⃣ On my.telegram.org (log in with the account you'll use) → "
                            "<b>API development tools</b>, create an app with any name.\n\n"
                            "Send its <b>api_id</b> and <b>api_hash</b> here, separated by a "
                            "space:\n<code>1234567 0123456789abcdef0123456789abcdef</code>",
                            [[("✖️ Cancel", "x")]])
        elif head == "cl" and rest == "on":
            await e.set_setting("calls.enabled", not e.cfg.calls.enabled)
            await self.calls_menu(msg_id)
        elif head == "cl" and rest == "opt":
            await self.settings_group("calls", msg_id)
        elif head == "cl" and rest == "out":
            await self.show("Log the call sniper's Telegram account out of this bot? Your "
                            "watched groups are kept.",
                            [[("✅ Yes, log out", "cl:out!"), ("✖️ Cancel", "cl")]], msg_id)
        elif head == "cl" and rest == "out!":
            w.logout()
            await self.send("🚪 Logged out. Tip: also end the session in that account's "
                            "Telegram → Settings → Devices.", [[("📣 Call sniper", "cl")]])
        elif head == "cl" and rest == "add":
            try:
                chats = await w.list_chats()
            except Exception as ex:
                await self.send(f"⚠️ {html.escape(str(ex))}", [[("📣 Call sniper", "cl")]])
                return
            have = {g.id for g in w.groups.all()}
            rows = [[(t[:40], f"ca:{i}")] for i, t in chats if i not in have][:30]
            if not rows:
                await self.send("No other groups or channels found. Join the group with that "
                                "account first, then try again.", [[("📣 Call sniper", "cl")]])
                return
            await self.send("Which one should I watch?", rows + [[("⬅️ Back", "cl")]])
        elif head == "ca":
            gid = int(rest)
            try:
                chats = dict(await w.list_chats())
            except Exception:
                chats = {}
            w.groups.upsert(CallGroup(id=gid, title=chats.get(gid, str(gid))))
            await self.call_group_menu(gid)
        elif head in ("cg", "cgf", "cgo", "cgr", "cgs"):
            gid = int(rest)
            g = w.groups.get(gid)
            if g is None:
                await self.calls_menu(msg_id)
                return
            if head == "cgf":
                g.filters = not g.filters
                w.groups.upsert(g)
            elif head == "cgo":
                g.on = not g.on
                w.groups.upsert(g)
            elif head == "cgr":
                w.groups.remove(gid)
                await self.calls_menu(msg_id)
                return
            elif head == "cgs":
                self._ask("call_size", gid)
                await self.send(f"Buy size for calls from <b>{html.escape(g.title)}</b>, in SOL "
                                "(0 = your normal buy size):", [[("✖️ Cancel", "x")]])
                return
            await self.call_group_menu(gid, msg_id)

    async def call_group_menu(self, gid: int, msg_id: Optional[int] = None) -> None:
        g = self.engine.calls.groups.get(gid)
        if g is None:
            await self.calls_menu(msg_id)
            return
        size = f"{g.sol:g} SOL" if g.sol else "your normal buy size"
        text = (f"📣 <b>{html.escape(g.title)}</b>\n"
                f"Status: {'▶️ watching' if g.on else '⏸ paused'}\n"
                f"Buy size: {size}\n"
                f"Filters: {'✅ on (recommended)' if g.filters else '⚠️ OFF: buys anything posted'}")
        await self.show(text, [
            [("💰 Buy size", f"cgs:{gid}"),
             ("🛡 Filters: " + ("on" if g.filters else "OFF"), f"cgf:{gid}")],
            [("⏸ Pause" if g.on else "▶️ Watch", f"cgo:{gid}"), ("🗑 Remove", f"cgr:{gid}")],
            [("⬅️ Call sniper", "cl")]], msg_id)

    async def _forget(self, msg_id: Optional[int]) -> None:
        if msg_id:   # login details never stay in the chat history
            try:
                await self.api("deleteMessage", chat_id=self.owner, message_id=msg_id)
            except Exception:
                pass

    async def calls_text(self, kind: str, text: str, msg_id: Optional[int], p: dict) -> None:
        from .calls import LoginNeedsPassword
        w = self.engine.calls
        text = text.strip()
        if kind == "call_size":
            sol = float(text.replace("SOL", "").strip())
            if not 0 <= sol <= 100:
                raise ValueError("between 0 and 100 SOL")
            g = w.groups.get(int(p["data"]))
            if g:
                g.sol = sol
                w.groups.upsert(g)
                await self.call_group_menu(g.id)
            return
        if kind == "call_api":
            await self._forget(msg_id)
            parts = text.split()
            if len(parts) != 2 or not parts[0].isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", parts[1]):
                raise ValueError("send the api_id (digits) and api_hash (32 letters/digits), "
                                 "separated by a space")
            self._ask("call_phone", {"api_id": int(parts[0]), "api_hash": parts[1]})
            await self.send("2️⃣ Now the account's <b>phone number</b>, with country code, "
                            "e.g. <code>+15551234567</code>", [[("✖️ Cancel", "x")]])
        elif kind == "call_phone":
            await self._forget(msg_id)
            phone = re.sub(r"[^\d+]", "", text)
            if not re.fullmatch(r"\+?\d{7,15}", phone):
                raise ValueError("that doesn't look like a phone number")
            d = p["data"]
            await w.start_login(d["api_id"], d["api_hash"], phone)
            self._ask("call_code")
            await self.send("3️⃣ Telegram just sent a login code to that account. Send it here "
                            "<b>with spaces between the digits</b>, like <code>1 2 3 4 5</code>"
                            ". (Telegram cancels a code that's sent in a chat as it is.)",
                            [[("✖️ Cancel", "x")]])
        elif kind == "call_code":
            await self._forget(msg_id)
            try:
                who = await w.finish_code(text)
            except LoginNeedsPassword:
                self._ask("call_pw")
                await self.send("4️⃣ That account has a two-step verification password. "
                                "Send it (I delete the message right away).",
                                [[("✖️ Cancel", "x")]])
                return
            await self._logged_in(who)
        elif kind == "call_pw":
            await self._forget(msg_id)
            await self._logged_in(await w.finish_password(text))

    async def _logged_in(self, who: str) -> None:
        await self.send(f"✅ Call sniper logged in as {html.escape(who)}. Now tap ➕ Add group "
                        "and pick up to a few groups to watch. It starts off: turn it on when "
                        "the groups are set.", [[("📣 Call sniper", "cl")]])

    # ---------- live chart ----------

    CHART_EVERY = 2.0         # seconds between updates: as fast as Telegram safely allows
    CHART_MAX_SECONDS = 900   # stops by itself after 15 minutes; tap again to restart
    CHART_LIMIT = 1           # one chart at a time keeps edits under Telegram's limit

    async def _chart_png(self, p) -> bytes:
        """Drawn off the event loop so it never delays a trade."""
        from .chart import render
        ex = self.engine.exit_cfg(p)
        tps = [lvl.at_pct for lvl in ex.take_profit]
        return await asyncio.to_thread(render, list(p.price_history), p.entry_price,
                                       ex.stop_loss_pct, tps)

    def _chart_caption(self, p, note: str = "") -> str:
        peak = (p.peak_price / p.entry_price - 1) * 100 if p.entry_price else 0
        value = p.tokens_remaining * p.last_price
        mins = (time.time() - p.opened_at) / 60
        return (f"📈 <b>{html.escape(p.symbol)}</b> PnL <b>{p.pnl_pct:+.1f}%</b> · "
                f"value {value:.4f} SOL\nPeak {peak:+.0f}% · held {mins:.0f} min"
                + (f"\n{note}" if note else ""))

    def _chart_buttons(self, mint: str, live: bool):
        rows = [[("⏹ Stop chart", f"chx:{mint}")] if live else [("📈 Restart chart", f"ch:{mint}")]]
        rows.append([("Sell 50%", f"s:{mint}:50"), ("Sell 100%", f"s:{mint}:100")])
        rows.append([("📊 DexScreener", f"https://dexscreener.com/solana/{mint}")])
        return rows

    async def start_chart(self, mint: str) -> None:
        from .notify import keyboard
        p = self.engine.positions.get(mint)
        if not p or p.closed:
            await self.send("That position is closed, so there's no live chart for it.")
            return
        self.stop_chart(mint)
        while len(self.charts) >= self.CHART_LIMIT:
            self.stop_chart(next(iter(self.charts)))
        try:
            msg = await self.api_files(
                "sendPhoto", {"photo": ("chart.png", await self._chart_png(p), "image/png")},
                chat_id=self.owner, parse_mode="HTML",
                caption=self._chart_caption(p, f"🔴 live · updates every {self.CHART_EVERY:.0f}s"),
                reply_markup=keyboard(self._chart_buttons(mint, True)))
        except Exception as e:
            await self.send(f"Couldn't draw the chart: {html.escape(str(e))}")
            return
        msg_id = (msg or {}).get("message_id")
        if msg_id:
            self.charts[mint] = asyncio.create_task(self._chart_loop(mint, msg_id))

    def stop_chart(self, mint: str) -> bool:
        """The loop finishes the message itself (final picture, restart button)."""
        t = self.charts.pop(mint, None)
        if t and not t.done():
            t.cancel()
            return True
        return False

    async def _chart_edit(self, p, msg_id: int, note: str, live: bool) -> None:
        from .notify import keyboard
        media = {"type": "photo", "media": "attach://photo", "parse_mode": "HTML",
                 "caption": self._chart_caption(p, note)}
        await self.api_files("editMessageMedia",
                             {"photo": ("chart.png", await self._chart_png(p), "image/png")},
                             chat_id=self.owner, message_id=msg_id, media=media,
                             reply_markup=keyboard(self._chart_buttons(p.mint, live)))

    async def _chart_loop(self, mint: str, msg_id: int) -> None:
        started = time.time()
        pos = self.engine.positions.get(mint)
        last = None
        note = "⏹ chart stopped"
        try:
            while True:
                await asyncio.sleep(self.CHART_EVERY)
                p = self.engine.positions.get(mint) or pos
                if p is None or p.closed:
                    note = "✅ position closed" + (f" ({html.escape(p.close_reason)})"
                                                  if p and p.close_reason else "")
                    break
                if time.time() - started > self.CHART_MAX_SECONDS:
                    note = "⏹ stopped after 15 min, tap Restart to keep watching"
                    break
                state = (p.last_price, p.tokens_remaining, len(p.price_history))
                if state == last:
                    continue    # nothing new: an identical edit would only be refused
                last = state
                try:
                    await self._chart_edit(p, msg_id,
                                           f"🔴 live · updates every {self.CHART_EVERY:.0f}s", True)
                except TelegramError as e:
                    m = re.search(r"retry after (\d+)", str(e))
                    if m:
                        await asyncio.sleep(int(m.group(1)))
                    elif "not modified" not in str(e):
                        log.info("chart update failed: %s", e)
                        if "not found" in str(e) or "deleted" in str(e):
                            return
        except asyncio.CancelledError:
            pass
        finally:
            if self.charts.get(mint) is asyncio.current_task():
                self.charts.pop(mint, None)
        p = self.engine.positions.get(mint) or pos
        if p:
            try:
                await self._chart_edit(p, msg_id, note, False)
            except Exception:
                pass

    async def token_card(self, mint: str) -> None:
        from .token_card import build_card
        text, buttons = await build_card(self.engine, mint)
        await self.send(text, buttons)

    def _card(self, mint: str) -> None:
        """Building a card takes a few lookups: do it in the background."""
        if not ADDRESS_RE.match(mint):
            self.engine._spawn(self.send("That doesn't look like a token address."))
            return

        async def run():
            try:
                if await self._is_wallet(mint):
                    await self.offer_wallet([mint])
                    return
                await self.token_card(mint)
            except Exception as e:
                log.exception("token card failed")
                await self.send(f"⚠️ couldn't build the card: {html.escape(str(e))}")
        self.engine._spawn(run())

    async def orders_menu(self, msg_id: Optional[int] = None) -> None:
        orders = self.engine.store.open_orders()
        if not orders:
            await self.show("📋 <b>Limit orders</b>\n\nNone open. Create one from a token card "
                            "(paste an address) or from a position (📋 Limit sell).",
                            [[("⬅️ Menu", "m")]], msg_id)
            return
        lines, rows = ["📋 <b>Limit orders</b>"], []
        now = time.time()
        for o in orders:
            left = max(0, (o["expires"] - now) / 3600)
            change = (o["trigger_price"] / o["base_price"] - 1) * 100 if o["base_price"] else 0
            if o["side"] == "buy":
                what = f"buy {o['sol']:g} SOL at {change:+.0f}% from order price"
            else:
                what = f"sell {o['pct']:g}% at {change:+.0f}% from entry"
            lines.append(f"#{o['id']} <code>{o['mint'][:8]}…</code> {what} · {left:.1f}h left")
            rows.append([(f"✖️ Cancel #{o['id']}", f"oc:{o['id']}")])
        rows.append([("⬅️ Menu", "m")])
        await self.show("\n".join(lines), rows, msg_id)

    async def settings_menu(self, msg_id: Optional[int] = None) -> None:
        e = self.engine
        n = len(e.store.overrides())
        await self.show(
            f"⚙️ <b>Settings</b>: preset <b>{e.cfg.preset}</b>"
            + (f", {n} custom" if n else "") + "\nPick a category:",
            [[(label, f"set:{g}")] for g, label in GROUPS.items()]
            + [[("🎚 Presets", "pre"), ("♻️ Reset custom", "rst")], [("⬅️ Back", "m")]], msg_id)

    async def settings_group(self, group: str, msg_id: Optional[int] = None) -> None:
        rows = []
        for i, s in enumerate(SETTINGS):
            if group_of(s) == group:
                rows.append([(f"{s.label}: {format_value(s, get_value(self.engine.cfg, s.key))}", f"e:{i}")])
        note = "Tap a setting to change it."
        if group == "copyexits":
            note = ("These apply to copied positions only. ✅ Using them." if
                    self.engine.cfg.copytrade.own_exits else
                    "Off: copied positions use your main exits. Turn on <b>Own exits for "
                    "copies</b> to use these (they start as a copy of your main exits).")
            rows.append([("👥 Copy & track", "c")])
        rows.append([("⬅️ Settings", "set"), ("🏠 Menu", "m")])
        await self.show(f"{GROUPS.get(group, group)}\n{note}", rows, msg_id)

    async def edit_setting(self, idx: int, msg_id: Optional[int]) -> None:
        s = SETTINGS[idx]
        cur = get_value(self.engine.cfg, s.key)
        if s.kind == "bool":
            await self.engine.set_setting(s.key, not cur)
            if s.key in ("copytrade.enabled", "copytrade.run_safety_checks"):
                await self.copy_menu(msg_id)
            else:
                await self.settings_group(group_of(s), msg_id)
            return
        if s.kind == "choice":  # cycle through the options
            nxt = s.options[(s.options.index(cur) + 1) % len(s.options)] if cur in s.options \
                else s.options[0]
            await self.engine.set_setting(s.key, nxt)
            await self.settings_group(group_of(s), msg_id)
            return
        self._ask("edit", s.key)
        hint = s.help or (f"{s.lo:g} to {s.hi:g} {s.unit}".strip() if s.kind in ("float", "int") else "")
        await self.send(f"<b>{s.label}</b>: currently {html.escape(format_value(s, cur))}\n"
                        f"Send the new value{f' ({html.escape(hint)})' if hint else ''}:",
                        [[("✖️ Cancel", "x")]])

    async def preset_menu(self, msg_id: Optional[int] = None) -> None:
        cur = self.engine.cfg.preset
        desc = {"degen": "instant snipes, loose filters, let winners run",
                "balanced": "the defaults",
                "safe": "6s confirmation, strict filters, quick profits"}
        await self.show("🎚 <b>Presets</b>\n\n" + "\n".join(
            f"{'✅' if k == cur else '•'} <b>{k}</b>: {v}" for k, v in desc.items()),
            [[(("✅ " if k == cur else "") + k, f"pre:{k}") for k in desc], [("⬅️ Settings", "set")]],
            msg_id)

    async def copy_menu(self, msg_id: Optional[int] = None) -> None:
        e = self.engine
        wallets = e.copy_wallets()
        status = "✅ on" if e.cfg.copytrade.enabled else "❌ off"
        lines = [f"👥 <b>Copy trading & wallet tracker</b> (copying {status})",
                 "👥 copy = mirror their buys · 🔔 track = just alert me"]
        if not e.has_pumpportal_key:
            lines.append("No PumpPortal key: copies come from the chain backup watcher only "
                         "(a few seconds slower).")
        elif not e.has_trade_stream:
            lines.append("⚠️ PumpPortal is refusing the trade feed, so the bot can't see their "
                         "buys. Top up the wallet linked to your PumpPortal key (min 0.02 SOL).")
        if e.paused:
            lines.append("⏸ Sniping is paused: nothing is copied until you tap ▶️ Start.")
        ct = e.cfg.copytrade
        feeds = ["PumpPortal " + ("✅" if e.has_trade_stream else "⚠️")]
        if ct.rpc_watch:
            feeds.append("chain backup ✅")
        lines.append("Watching their trades via: " + " + ".join(feeds))
        lines.append(f"Copies buys of {ct.min_leader_buy_sol:g}+ SOL"
                     + (f" in coins under ${ct.max_market_cap_usd:,.0f} market cap"
                        if ct.max_market_cap_usd else "")
                     + (f", not over +{ct.max_chase_pct:g}% above their price"
                        if ct.max_chase_pct else "")
                     + (", first buy only" if ct.first_buy_only else "")
                     + " (defaults; each wallet can differ: tap ⚙️)")
        rows = []
        stored = {x["address"]: x for x in e.store.copy_wallets()}
        shown = {w.address: w for w in wallets}
        for addr, x in stored.items():  # also list copy wallets while copying is off
            if addr not in shown:
                from .config import CopyWallet
                shown[addr] = CopyWallet(**x)
        for w in shown.values():
            icon = "⏸" if w.paused else ("🔔" if w.mode == "alert" else "👥")
            size = "" if w.mode == "alert" else f" · {self._size_text(w)}"
            r = e.wallet_results(w.address)
            res = (f" · {r['n']} copies, {r['wins'] / r['n'] * 100:.0f}% won, {r['pnl']:+.3f} SOL"
                   if r["n"] else "")
            lines.append(f"{icon} <b>{html.escape(w.label or w.address[:8])}</b>{size}{res}"
                         f"\n    {self._seen_text(w.address)}")
            rows.append([(f"⚙️ {w.label or w.address[:8]}", f"wp:{w.address}")])
        wallets = list(shown.values())
        if not wallets:
            lines.append("No wallets yet.")
        toggle_idx = next(i for i, s in enumerate(SETTINGS) if s.key == "copytrade.enabled")
        filters_idx = next(i for i, s in enumerate(SETTINGS) if s.key == "copytrade.run_safety_checks")
        checks = e.cfg.copytrade.run_safety_checks
        if not checks:
            lines.append("⚠️ Filters are OFF for copies: anything they buy is copied.")
        rows.append([("➕ Add wallet", "c:add"), (f"Copy: {status}", f"e:{toggle_idx}")])
        rows.append([("🛡 Filters on copies: " + ("✅ on" if checks else "⚠️ OFF"), f"e:{filters_idx}")])
        own = ct.own_exits
        rows.append([("⚙️ Copy options", "set:copytrade"),
                     ("🎯 Copy TP/SL/moonbag" if own else "🎯 Exits: same as sniping", "set:copyexits")])
        rows.append([("⬅️ Back", "m")])
        await self.show("\n".join(lines), rows, msg_id)

    def _size_text(self, w) -> str:
        if w.size_pct:
            return f"{w.size_pct:g}% of their buy" + (f" (max {w.max_sol:g})" if w.max_sol else "")
        return f"{w.buy_sol or self.engine.cfg.trading.buy_amount_sol:g} SOL"

    def _find_wallet(self, address: str):
        e = self.engine
        w = next((x for x in e.copy_wallets() if x.address == address), None)
        if w is None:
            from .config import CopyWallet
            x = next((x for x in e.store.copy_wallets() if x["address"] == address), None)
            if x is not None:
                w = CopyWallet(**{k: v for k, v in x.items() if k in CopyWallet.__dataclass_fields__})
        return w

    async def wallet_page(self, address: str, msg_id: Optional[int] = None) -> None:
        e = self.engine
        w = self._find_wallet(address)
        if w is None:
            await self.copy_menu(msg_id)
            return
        ct = e.cfg.copytrade
        sells = w.sells if w.copy_sells else "off"
        sells_txt = {"mirror": "🔁 mirror (sell the same % they sell)",
                     "all": "🚪 sell all when they sell", "off": "ignored"}[sells]
        min_buy = (f"{w.min_leader_sol:g} SOL" if w.min_leader_sol is not None
                   else f"{ct.min_leader_buy_sol:g} SOL (default)")
        cap = w.max_mcap_usd if w.max_mcap_usd is not None else ct.max_market_cap_usd
        cap_txt = (f"${cap:,.0f}" if cap else "no limit") + (
            "" if w.max_mcap_usd is not None else " (default)")
        filt = ("on" if ct.run_safety_checks else "OFF") + " (default)" if w.filters is None \
            else ("on" if w.filters else "⚠️ OFF")
        r = e.wallet_results(address)
        lines = [f"{'🔔' if w.mode == 'alert' else '👥'} <b>{html.escape(w.label or '-')}</b>"
                 f"\n<code>{address}</code>",
                 ("⏸ <b>Paused</b>" + (f": {html.escape(w.paused_reason)}" if w.paused_reason
                                        else "")) if w.paused else
                 ("Copying its buys" if w.mode == "copy" else "Tracking only (alerts)"),
                 "", f"💰 Size: {self._size_text(w)}", f"🔁 Their sells: {sells_txt}",
                 f"📉 Copies their buys of: {min_buy}+", f"🧢 Max market cap: {cap_txt}",
                 f"🛡 Filters: {filt}", f"👀 {self._seen_text(address)}", "",
                 "<b>Your copies of it</b>: " + (
                     f"{r['n']} closed, {r['wins']} won, {r['pnl']:+.4f} SOL"
                     + (f", {r['loss_streak']} losses in a row" if r["loss_streak"] else "")
                     if r["n"] else "none closed yet")]
        rep = e.wallet_checker.reports.get(address)
        lines.append("")
        if rep:
            ago = (time.time() - rep.made) / 3600
            lines.append(f"<b>🔍 Wallet check</b> (updated {ago:.0f}h ago, refreshes every "
                         f"{ct.check_every_hours:g}h)\n<pre>{html.escape(rep.text())}</pre>")
        else:
            lines.append("🔍 Wallet check: not done yet (tap Re-check).")
        a = address
        await self.show("\n".join(lines), [
            [("🔔 Track only" if w.mode == "copy" else "👥 Copy its buys", f"cm:{a}"),
             ("▶️ Resume" if w.paused else "⏸ Pause", f"wpp:{a}")],
            [("💰 Size", f"wpz:{a}"), ("🔁 Sells: " + sells, f"wpx:{a}")],
            [("📉 Min buy", f"wpl:{a}"), ("🧢 Max mcap", f"wpc:{a}")],
            [("🛡 Filters: " + ("default" if w.filters is None else "on" if w.filters else "OFF"),
              f"wpf:{a}")],
            [("🔍 Re-check wallet", f"wpk:{a}"), ("🗑 Remove", f"c:rm:{a}")],
            [("⬅️ Copy & track", "c")]], msg_id)

    async def copy_wallet_action(self, head: str, address: str, msg_id: Optional[int]) -> None:
        e = self.engine
        w = self._find_wallet(address)
        if w is None:
            await self.copy_menu(msg_id)
            return
        if head == "wpp":
            await e.update_wallet(address, paused=not w.paused, paused_reason="")
        elif head == "wpx":
            cur = w.sells if w.copy_sells else "off"
            nxt = {"mirror": "all", "all": "off", "off": "mirror"}[cur]
            await e.update_wallet(address, sells=nxt)
        elif head == "wpf":
            nxt = {None: True, True: False, False: None}[w.filters]
            await e.update_wallet(address, filters=nxt)
        elif head == "wpk":
            self._check_wallet(address)
            await self.send("🔍 Checking its last few days on the chain… (up to a minute)")
            return
        elif head in ("wpz", "wpl", "wpc"):
            kind, prompt = {
                "wpz": ("wallet_size", "Send the size per copy:\n<code>0.1</code> = 0.1 SOL each\n"
                        "<code>5%</code> = 5% of what they buy\n<code>5% 0.3</code> = 5%, at most "
                        "0.3 SOL"),
                "wpl": ("wallet_min", "Only copy their buys of at least how much SOL? (e.g. "
                        "<code>0.5</code>, or <code>default</code>)"),
                "wpc": ("wallet_mcap", "Max market cap for this wallet's copies (e.g. "
                        "<code>30k</code>, <code>0</code> = no limit, or <code>default</code>)"),
            }[head]
            self._ask(kind, address)
            await self.send(prompt, [[("✖️ Cancel", "x")]])
            return
        await self.wallet_page(address, msg_id)

    async def wallet_text(self, kind: str, text: str, address: str) -> None:
        e = self.engine
        t = text.strip().lower().replace("sol", "").strip()
        if kind == "wallet_size":
            parts = t.replace("max", " ").split()
            if parts and parts[0].endswith("%"):
                pct = float(parts[0].rstrip("%"))
                cap = float(parts[1]) if len(parts) > 1 else 0.0
                if not 0 < pct <= 1000 or not 0 <= cap <= 100:
                    raise ValueError("e.g. 5% or 5% 0.3")
                await e.update_wallet(address, size_pct=pct, max_sol=cap)
            else:
                sol = float(parts[0]) if parts else -1
                if not 0 <= sol <= 100:
                    raise ValueError("between 0 and 100 SOL (0 = your normal buy size)")
                await e.update_wallet(address, buy_sol=sol, size_pct=0.0, max_sol=0.0)
        elif kind == "wallet_min":
            val = None if t == "default" else float(t)
            if val is not None and not 0 <= val <= 1000:
                raise ValueError("between 0 and 1000 SOL")
            await e.update_wallet(address, min_leader_sol=val)
        elif kind == "wallet_mcap":
            from .settings import BY_KEY, parse_value
            val = None if t == "default" else parse_value(BY_KEY["copytrade.max_market_cap_usd"], t)
            await e.update_wallet(address, max_mcap_usd=val)
        await self.wallet_page(address)

    def _check_wallet(self, address: str) -> None:
        e = self.engine
        w = self._find_wallet(address)
        name = (w.label if w and w.label else address[:8])

        async def done(rep):
            await self.send(f"🔍 <b>Wallet check: {html.escape(name)}</b>\n"
                            f"<pre>{html.escape(rep.text())}</pre>",
                            [[("⚙️ Wallet settings", f"wp:{address}")]])
        e.wallet_checker.start(address, e.cfg.copytrade.check_days, done)

    def _seen_text(self, address: str) -> str:
        seen = self.engine.copy_seen.get(address)
        if not seen:
            return "no trades seen since the bot started"
        ts, via, side, mint = seen
        ago = max(0, time.time() - ts)
        when = f"{ago:.0f}s" if ago < 120 else f"{ago / 60:.0f}m" if ago < 7200 else f"{ago / 3600:.0f}h"
        src = "PumpPortal" if via == "pumpportal" else "chain"
        return f"last trade seen {when} ago ({side} {mint[:6]}…, via {src})"

    async def copy_command(self, args: list[str]) -> None:
        e = self.engine
        if not args or args[0] == "list":
            await self.copy_menu()
        elif args[0] == "add" and len(args) >= 2:
            rest = args[1:]
            mode = "copy"
            if rest[-1].lower() in ("track", "alert"):
                mode, rest = "alert", rest[:-1]
            if not rest:
                raise ValueError("send: <address> [label] [sol per trade] [track]")
            label, sol = "", 0.0
            for tok in rest[1:]:
                try:
                    sol = float(tok.replace("SOL", ""))
                except ValueError:
                    label = tok
            await e.add_copy_wallet(rest[0], label, sol, mode=mode)
            await self.send(f"{'🔔 Tracking' if mode == 'alert' else '👥 Copying'} "
                            f"{html.escape(label or rest[0])}")
        elif args[0] in ("rm", "remove") and len(args) >= 2:
            ok = await e.remove_copy_wallet(args[1])
            await self.send("Removed." if ok else "Not found.")
        else:
            await self.send("/copy list | add &lt;wallet&gt; [label] [sol] | rm &lt;wallet&gt;")

    # ---------- adding copy / track wallets ----------

    WALLET_SIZES = (0.01, 0.03, 0.05, 0.1)

    async def _is_wallet(self, address: str) -> bool:
        """A person's wallet (a plain SOL account), not a token: offer to copy it instead."""
        try:
            acct = (await self.engine.rpc.get_accounts_raw([address]))[0]
        except Exception:
            return False
        return bool(acct) and acct[0] == "11111111111111111111111111111111"

    async def copy_add_text(self, text: str) -> None:
        parts = text.split()
        # the old one-line form still works: <address> [label] [sol] [track]
        if len(parts) > 1 and ADDRESS_RE.match(parts[0]) and "\n" not in text.strip() \
                and not any(ADDRESS_RE.match(p) for p in parts[1:]):
            await self.copy_command(["add", *parts])
            await self.copy_menu()
            return
        addrs = wallet_addresses(text)
        if not addrs:
            raise ValueError("no wallet address found in that")
        await self.offer_wallet(addrs)

    async def offer_wallet(self, addrs: list[str]) -> None:
        if not self.engine.has_pumpportal_key and not self.engine.cfg.copytrade.rpc_watch:
            await self.send("Copying and tracking wallets needs a PumpPortal API key (add "
                            "PUMPPORTAL_API_KEY to .env on the server) or ⚙️ Copy options → "
                            "Backup wallet watcher on.")
            return
        known = {x["address"] for x in self.engine.store.copy_wallets()}
        self.wallet_wiz = {"addrs": addrs, "expires": time.monotonic() + PENDING_TTL}
        if len(addrs) == 1:
            a = addrs[0]
            note = "\n(already in your list: this updates it)" if a in known else ""
            await self.send(f"👛 Wallet <code>{a}</code>{note}\n\nWhat should I do with it?",
                            [[("👥 Copy its buys", "cw:m:copy")],
                             [("🔔 Just alert me", "cw:m:alert")],
                             [("✖️ Cancel", "x")]])
        else:
            await self.send(f"👛 {len(addrs)} wallets found. Add them all with your normal buy "
                            "size (change each later from the list)?",
                            [[("👥 Copy all", "cw:m:copy"), ("🔔 Alert all", "cw:m:alert")],
                             [("✖️ Cancel", "x")]])

    async def wallet_wizard(self, rest: str, text: str = "") -> None:
        wiz = getattr(self, "wallet_wiz", None)
        if not wiz or time.monotonic() > wiz["expires"]:
            self.wallet_wiz = None
            await self.send("That expired: tap ➕ Add wallet again.", [[("👥 Copy & track", "c")]])
            return
        wiz["expires"] = time.monotonic() + PENDING_TTL
        e = self.engine
        step, _, val = rest.partition(":")
        if step == "m":
            wiz["mode"] = "alert" if val == "alert" else "copy"
            if len(wiz["addrs"]) > 1 or wiz["mode"] == "alert":
                wiz["sol"] = 0.0
                return await self._wallet_name_step(wiz)
            default = e.cfg.trading.buy_amount_sol
            await self.send("How much SOL per copied buy?",
                            [[(f"{v:g}", f"cw:s:{v:g}") for v in self.WALLET_SIZES],
                             [(f"Normal size ({default:g})", "cw:s:0"), ("✏️ Other", "cw:s:?")],
                             [("✖️ Cancel", "x")]])
        elif step == "s":
            if val == "?":
                self._ask("copy_size")
                await self.send("Send the SOL per buy, e.g. <code>0.02</code>",
                                [[("✖️ Cancel", "x")]])
                return
            sol = float(val or text)
            if not 0 <= sol <= 100:
                raise ValueError("between 0 and 100 SOL")
            wiz["sol"] = sol
            await self._wallet_name_step(wiz)
        elif step == "n":
            await self._wallet_save(wiz, "" if val == "skip" else text.strip()[:30])

    async def _wallet_name_step(self, wiz: dict) -> None:
        if len(wiz["addrs"]) > 1:
            return await self._wallet_save(wiz, "")
        self._ask("copy_name")
        await self.send("Give it a name so you know who it is (e.g. <code>whale 1</code>), or "
                        "skip:", [[("⏭ Skip", "cw:n:skip")], [("✖️ Cancel", "x")]])

    async def _wallet_save(self, wiz: dict, label: str) -> None:
        self.wallet_wiz, self.pending = None, None
        mode, sol = wiz.get("mode", "copy"), wiz.get("sol", 0.0)
        for a in wiz["addrs"]:
            await self.engine.add_copy_wallet(a, label, sol, mode=mode)
        what = "🔔 Tracking" if mode == "alert" else "👥 Copying"
        who = html.escape(label) if label else (
            f"{len(wiz['addrs'])} wallets" if len(wiz["addrs"]) > 1 else wiz["addrs"][0][:8] + "…")
        size = "" if mode == "alert" else (f" · {sol:g} SOL per buy" if sol else " · normal size")
        await self.send(f"✅ {what} {who}{size}\n🔍 Checking its last "
                        f"{self.engine.cfg.copytrade.check_days:g} days on the chain…")
        for a in wiz["addrs"][:5]:
            self._check_wallet(a)
        await self.copy_menu()

    async def reclaim(self) -> None:
        await self.send("🧹 Looking for empty token accounts…")
        try:
            text = await self.engine.reclaim_rent()
        except Exception as e:
            text = f"⚠️ Couldn't close the empty accounts: {html.escape(str(e)[:200])}"
        await self.send(text, [[("💼 Wallet", "w"), ("🏠 Menu", "m")]])

    async def health(self) -> None:
        from .health import report
        await self.send("🩺 Checking every service…")
        await self.send(await report(self.engine), [[("🔄 Check again", "hl"), ("🏠 Menu", "m")]])

    def _stats_since_key(self) -> str:
        return f"stats_since_{self.engine.mode}"

    def stats_since(self) -> Optional[float]:
        try:
            return float(self.engine.store.get_setting(self._stats_since_key()) or 0) or None
        except ValueError:
            return None

    async def stats(self) -> None:
        from .stats import format_since, format_summary, summarize
        rows = [[("🔁 Reset PnL counter", "sts")],
                [("🔎 Why no buys?", "why"), ("⬅️ Menu", "m")]]
        if not self.engine.live:
            rows.insert(1, [("🗑 Start paper results over", "stz")])
        store = self.engine.store
        since = self.stats_since()
        top = ""
        if since:
            top = f"<pre>{html.escape(format_since(summarize(store, since), since))}</pre>\n<b>All time</b>\n"
        await self.send(f"📈 <b>{self.engine.mode.upper()} results</b>\n{top}"
                        f"<pre>{html.escape(format_summary(summarize(store)))}</pre>",
                        rows)
