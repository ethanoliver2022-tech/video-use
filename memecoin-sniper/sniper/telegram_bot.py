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

from .settings import BY_KEY, GROUPS, SETTINGS, format_value, get_value, group_of

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
PENDING_TTL = 300
EXPORT_TTL = 60
MAX_PAIR_ATTEMPTS = 10   # wrong codes before the pairing code is replaced


class TelegramError(RuntimeError):
    """Telegram API error. Never includes the request URL (it contains the bot token)."""

HELP = """<b>Everything is in the menu</b>, tap /menu.

Shortcuts:
• Paste a token address to get a buy card
/positions to see open positions with sell buttons
/buy &lt;mint&gt; [sol] [force]
/sell &lt;mint|symbol&gt; [pct]
/pause, /resume
/setbuy &lt;sol&gt;
/copy list | add &lt;wallet&gt; [label] [sol] | rm &lt;wallet&gt;
/withdraw &lt;address&gt; &lt;sol|all&gt;
/block &lt;creator&gt;
/stats"""

WELCOME = """👋 <b>Paired!</b> This chat now controls your sniper.

<b>Getting started</b>
1. 💼 <b>Wallet</b> → create a new wallet (or import one)
2. Send SOL to the deposit address shown
3. ▶️ <b>Start sniping</b> in 📝 PAPER mode first to see how it trades
4. When you're happy, tap 🔁 <b>Go LIVE</b>

Use a dedicated wallet holding only what you can afford to lose."""


class TelegramControl:
    def __init__(self, engine: "Engine", token: str, chat_id: str, http: httpx.AsyncClient):
        self.engine, self.token, self.http = engine, token, http
        self.owner = str(chat_id or engine.store.get_setting("owner_chat_id") or "")
        self.pair_code = "" if self.owner else self._new_code()
        self.pair_failures = 0
        self.offset = 0
        self.pending: Optional[dict] = None
        engine.notifier.token = token
        engine.notifier.chat = self.owner

    # ---------- transport ----------

    @staticmethod
    def _new_code() -> str:
        return secrets.token_hex(5).upper()  # 10 hex chars: not brute-forceable over Telegram

    async def api(self, method: str, **params):
        try:
            resp = await self.http.post(f"https://api.telegram.org/bot{self.token}/{method}",
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
        if self.pair_code:
            log.warning("📱 Telegram not paired yet. Send this to your bot:  /start %s", self.pair_code)
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
                    ("settings", "Settings"), ("stats", "Performance"), ("help", "Help")]])
        except Exception as e:
            log.debug("setMyCommands failed: %s", e)
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

    # ---------- routing ----------

    async def handle_update(self, u: dict) -> None:
        try:
            await self._route(u)
        except Exception as e:
            log.exception("telegram update failed")
            if self.owner:
                await self.send(f"⚠️ {html.escape(str(e))}", [[("🏠 Menu", "m")]])

    async def _route(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            if not self.owner or str(msg.get("chat", {}).get("id")) != self.owner:
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
            await self._try_pair(chat, text)
            return
        if chat != self.owner:
            return
        if self.pending and not text.startswith("/"):
            if time.time() > self.pending["expires"]:
                self.pending = None
            else:
                await self.handle_pending(text, msg.get("message_id"))
                return
        if text.startswith("/"):
            self.pending = None
            await self.handle_command(text)
        elif ADDRESS_RE.match(text):
            await self.token_card(text)
        else:
            await self.main_menu()

    async def _try_pair(self, chat: str, text: str) -> None:
        parts = text.split()
        if len(parts) != 2 or parts[0].split("@")[0] not in ("/start", "/pair"):
            return
        if not secrets.compare_digest(parts[1].upper().encode(), self.pair_code.encode()):
            self.pair_failures += 1
            if self.pair_failures >= MAX_PAIR_ATTEMPTS:
                self.pair_failures = 0
                self.pair_code = self._new_code()
                log.warning("too many wrong pairing codes; new code:  /start %s", self.pair_code)
            return
        self.owner = chat
        self.pair_code = ""
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
        elif head == "bc":
            self._ask("buy_custom", rest)
            await self.send("Send the amount of SOL to buy:", [[("✖️ Cancel", "x")]])
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
            await self.send("Send the wallet to copy:\n<code>&lt;address&gt; [label] [sol per trade]</code>",
                            [[("✖️ Cancel", "x")]])
        elif data.startswith("c:rm:"):
            await e.remove_copy_wallet(data[5:])
            await self.copy_menu(msg_id)
        elif data == "wd!":
            await self.do_withdraw()

    def _bg_reply(self, coro) -> None:
        """Run a slow action (a trade can take ~90s to confirm) without freezing the chat;
        the result arrives as its own message."""
        async def runner():
            try:
                result = await coro
                if result:
                    await self.send(result)
            except Exception as e:
                log.exception("telegram action failed")
                await self.send(f"⚠️ {html.escape(str(e))}", [[("🏠 Menu", "m")]])
        self.engine._spawn(runner())

    # ---------- pending text replies ----------

    def _ask(self, kind: str, data=None) -> None:
        self.pending = {"kind": kind, "data": data, "expires": time.time() + PENDING_TTL}

    async def handle_pending(self, text: str, msg_id: Optional[int]) -> None:
        p, self.pending = self.pending, None
        kind = p["kind"]
        e = self.engine
        try:
            if kind == "import":
                if msg_id:  # never leave a private key sitting in chat history
                    try:
                        await self.api("deleteMessage", chat_id=self.owner, message_id=msg_id)
                    except Exception:
                        pass
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
            elif kind == "buy_custom":
                self._bg_reply(e.manual_buy(p["data"], float(text.replace("SOL", "").strip())))
        except Exception as ex:
            self.pending = p  # let them try again
            p["expires"] = time.time() + PENDING_TTL
            await self.send(f"⚠️ {html.escape(str(ex))}\nTry again, or tap Cancel.", [[("✖️ Cancel", "x")]])

    # ---------- screens ----------

    async def main_menu(self, msg_id: Optional[int] = None) -> None:
        e = self.engine
        kp = e.wallet.keypair()
        mode = "🔴 LIVE" if e.live else "📝 PAPER"
        state = "⏸ paused" if e.paused else "▶️ sniping"
        lines = [f"🎯 <b>Memecoin Sniper</b>", f"{mode} · {state} · preset <b>{e.cfg.preset}</b>"]
        if kp:
            bal = await self._balance(str(kp.pubkey()))
            lines.append(f"💼 <code>{kp.pubkey()}</code>\n💰 {bal}")
        else:
            lines.append("💼 No wallet yet. Tap <b>Wallet</b> to create one.")
        open_n = sum(1 for p in e.positions.values() if not p.closed)
        lines.append(f"📊 Open {open_n}/{e.cfg.trading.max_open_positions} · today "
                     f"{e.store.realized_today():+.4f} SOL · buy {e.cfg.trading.buy_amount_sol:g} SOL")
        if not e.has_trade_stream:
            lines.append("ℹ️ No PumpPortal key: prices use on-chain polling, and copy trading is off.")
        if "api.mainnet-beta.solana.com" in e.cfg.endpoints.rpc_url:
            lines.append("⚠️ Public Solana RPC: set SOLANA_RPC_URL before going live.")
        toggle = ("⏸ Pause sniping", "stop") if not e.paused else ("▶️ Start sniping", "go")
        switch = ("📝 Go PAPER", "mode:paper") if e.live else ("🔴 Go LIVE", "mode:live")
        await self.show("\n".join(lines), [
            [toggle],
            [("💼 Wallet", "w"), ("📊 Positions", "p")],
            [("⚙️ Settings", "set"), ("👥 Copy trade", "c")],
            [("📈 Stats", "st"), switch],
            [("🔄 Refresh", "m")],
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
        rows = [[("📤 Withdraw SOL", "w:wd"), ("🔑 Export key", "w:exp")]]
        if not w.from_env:
            rows.append([("✨ New wallet", "w:new"), ("📥 Import", "w:imp")])
        rows.append([("🔄 Refresh", "w"), ("⬅️ Back", "m")])
        await self.show(
            f"💼 <b>Wallet</b>\n\n<b>Deposit address</b> (tap to copy):\n<code>{kp.pubkey()}</code>\n\n"
            f"Balance: {await self._balance(str(kp.pubkey()))}\n"
            f"https://solscan.io/account/{kp.pubkey()}{lock}", rows, msg_id)

    async def wallet_action(self, action: str, msg_id: Optional[int]) -> None:
        w = self.engine.wallet
        if action == "new":
            if w.keypair():
                await self.show("⚠️ Replace your current wallet with a new one?\n"
                                "The old key is kept as a backup file on the server, but withdraw "
                                "its funds first.",
                                [[("✅ Yes, create new", "w:new!"), ("✖️ Cancel", "w")]], msg_id)
                return
            action = "new!"
        if action == "new!":
            if self._live_blocked():
                await self.send("Switch to PAPER mode before changing wallets.")
                return
            kp = w.create()
            await self.send(f"✨ New wallet created.\n\nDeposit SOL to:\n<code>{kp.pubkey()}</code>\n\n"
                            "Back up the key: 💼 Wallet → 🔑 Export key.")
            await self._wallet_changed()
        elif action == "imp":
            if self._live_blocked():
                await self.send("Switch to PAPER mode before changing wallets.")
                return
            self._ask("import")
            await self.send("Send the private key (base58 or JSON array).\n"
                            "I'll delete your message as soon as I've read it.", [[("✖️ Cancel", "x")]])
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

    def _live_blocked(self) -> bool:
        return self.engine.live

    async def _wallet_changed(self) -> None:
        await self.wallet_menu()

    async def _delete_later(self, msg_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self.api("deleteMessage", chat_id=self.owner, message_id=msg_id)
        except Exception:
            pass

    async def confirm_withdraw(self, address: str, amount: str) -> None:
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)
        sol = None if amount.lower() == "all" else float(amount)
        if sol is not None and sol <= 0:
            raise ValueError("amount must be positive")
        self.pending = {"kind": "withdraw_confirm", "data": (address, sol),
                        "expires": time.time() + PENDING_TTL}
        open_n = sum(1 for p in self.engine.positions.values() if not p.closed)
        warn = f"\n⚠️ You have {open_n} open position(s); keep SOL for sell fees." if open_n else ""
        await self.send(f"Send <b>{'ALL' if sol is None else f'{sol:g} SOL'}</b> to\n"
                        f"<code>{address}</code>?{warn}",
                        [[("✅ Confirm withdraw", "wd!"), ("✖️ Cancel", "x")]])

    async def do_withdraw(self) -> None:
        p, self.pending = self.pending, None
        if not p or p["kind"] != "withdraw_confirm" or time.time() > p["expires"]:
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
        for p in open_pos:
            value = p.tokens_remaining * p.last_price
            await self.send(
                f"<b>{html.escape(p.symbol)}</b> <code>{p.mint}</code>\n"
                f"PnL {p.pnl_pct:+.0f}% · value {value:.4f} SOL · in {p.sol_in:.4f} · "
                f"out {p.sol_out:.4f}\nPeak {((p.peak_price / p.entry_price) - 1) * 100:+.0f}% · "
                f"via {html.escape(p.source)}",
                buttons=[[("Sell 25%", f"s:{p.mint}:25"), ("Sell 50%", f"s:{p.mint}:50"),
                          ("Sell 100%", f"s:{p.mint}:100")], [("🔄 Refresh", "refresh")]])

    async def token_card(self, mint: str) -> None:
        e = self.engine
        held = e.positions.get(mint)
        if held and not held.closed:
            await self.positions()
            return
        amounts = [0.05, 0.1, 0.25, 0.5, 1.0]
        await self.send(
            f"🪙 <code>{mint}</code>\nhttps://dexscreener.com/solana/{mint}\n\n"
            f"Buy runs your filters first ({'🔴 LIVE' if e.live else '📝 PAPER'}).",
            [[(f"{a:g} SOL", f"b:{mint}:{a:g}") for a in amounts[:3]],
             [(f"{a:g} SOL", f"b:{mint}:{a:g}") for a in amounts[3:]] + [("✏️ Custom", f"bc:{mint}")],
             [(f"⚠️ Buy {e.cfg.trading.buy_amount_sol:g}, skip filters", f"bf:{mint}:")]])

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
        lines = [f"👥 <b>Copy trading</b> ({status})"]
        if not e.has_trade_stream:
            lines.append("Needs a PumpPortal API key: add PUMPPORTAL_API_KEY to .env on the "
                         "server and restart.")
        rows = []
        for w in wallets:
            lines.append(f"• {html.escape(w.label or '-')} <code>{w.address}</code> · "
                         f"{w.buy_sol or e.cfg.trading.buy_amount_sol:g} SOL")
            rows.append([(f"🗑 {w.label or w.address[:8]}", f"c:rm:{w.address}")])
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
            label = args[2] if len(args) > 2 else ""
            sol = float(args[3]) if len(args) > 3 else 0.0
            await e.add_copy_wallet(args[1], label, sol)
            await self.send(f"👥 Copying {html.escape(label or args[1])}")
        elif args[0] in ("rm", "remove") and len(args) >= 2:
            ok = await e.remove_copy_wallet(args[1])
            await self.send("Removed." if ok else "Not found.")
        else:
            await self.send("/copy list | add &lt;wallet&gt; [label] [sol] | rm &lt;wallet&gt;")

    async def stats(self) -> None:
        from .stats import format_summary, summarize
        await self.send(f"📈 <b>{self.engine.mode.upper()} results</b>\n"
                        f"<pre>{html.escape(format_summary(summarize(self.engine.store)))}</pre>",
                        [[("⬅️ Menu", "m")]])
