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
                    ("health", "Check every service"), ("help", "Help")]])
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
            await self.copy_menu(msg_id)
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
            await self.send("Send the wallet:\n<code>&lt;address&gt; [label] [sol per trade] [track]</code>\n"
                            "Add <code>track</code> at the end to only get alerts instead of copying.",
                            [[("✖️ Cancel", "x")]])
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
                await self.copy_command(["add", *text.split()])
                await self.copy_menu()
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
            if exits.in_moonbag(p, self.engine.cfg.exits):
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
                         [("📋 Limit sell", f"ls:{p.mint}"), ("🔍 Card", f"tc:{p.mint}"),
                          ("🔄 Refresh", "refresh")]])

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
        rows.append([("⬅️ Settings", "set"), ("🏠 Menu", "m")])
        await self.show(f"{GROUPS.get(group, group)}\nTap a setting to change it.", rows, msg_id)

    async def edit_setting(self, idx: int, msg_id: Optional[int]) -> None:
        s = SETTINGS[idx]
        cur = get_value(self.engine.cfg, s.key)
        if s.kind == "bool":
            await self.engine.set_setting(s.key, not cur)
            if s.key == "copytrade.enabled":
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
            lines.append("Needs a PumpPortal API key: add PUMPPORTAL_API_KEY to .env on the "
                         "server and restart.")
        rows = []
        stored = {x["address"]: x for x in e.store.copy_wallets()}
        shown = {w.address: w for w in wallets}
        for addr, x in stored.items():  # also list copy wallets while copying is off
            if addr not in shown:
                from .config import CopyWallet
                shown[addr] = CopyWallet(**x)
        for w in shown.values():
            icon = "🔔" if w.mode == "alert" else "👥"
            size = "" if w.mode == "alert" else f" · {w.buy_sol or e.cfg.trading.buy_amount_sol:g} SOL"
            lines.append(f"{icon} {html.escape(w.label or '-')} <code>{w.address}</code>{size}")
            rows.append([(f"{'👥 Copy' if w.mode == 'alert' else '🔔 Track only'}", f"cm:{w.address}"),
                         (f"🗑 {w.label or w.address[:8]}", f"c:rm:{w.address}")])
        wallets = list(shown.values())
        if not wallets:
            lines.append("No wallets yet.")
        toggle_idx = next(i for i, s in enumerate(SETTINGS) if s.key == "copytrade.enabled")
        rows.append([("➕ Add wallet", "c:add"), (f"Copy: {status}", f"e:{toggle_idx}")])
        rows.append([("⬅️ Back", "m")])
        await self.show("\n".join(lines), rows, msg_id)

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

    async def stats(self) -> None:
        from .stats import format_summary, summarize
        await self.send(f"📈 <b>{self.engine.mode.upper()} results</b>\n"
                        f"<pre>{html.escape(format_summary(summarize(self.engine.store)))}</pre>",
                        [[("⬅️ Menu", "m")]])
