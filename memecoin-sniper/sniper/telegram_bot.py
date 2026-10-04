"""Telegram control panel: run the bot from your phone, like Trojan / BonkBot.

Only messages from TELEGRAM_CHAT_ID are obeyed. Everyone else is ignored.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

HELP = """<b>Commands</b>
/status — mode, balance, PnL today
/positions — open positions with sell buttons
/buy &lt;mint&gt; [sol] [force] — manual buy (force skips filters)
/sell &lt;mint|symbol&gt; [pct] — sell (default 100%)
/pause, /resume — stop / restart new entries
/setbuy &lt;sol&gt; — change auto-buy size
/copy list | add &lt;wallet&gt; [label] [sol] | rm &lt;wallet&gt;
/block &lt;creator&gt; — never buy this dev again
/stats — performance summary"""


class TelegramControl:
    def __init__(self, engine: "Engine", token: str, chat_id: str, http: httpx.AsyncClient):
        self.engine, self.token, self.chat_id, self.http = engine, token, str(chat_id), http
        self.offset = 0

    async def api(self, method: str, **params):
        resp = await self.http.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                    json=params, timeout=35)
        resp.raise_for_status()
        return resp.json().get("result")

    async def run(self) -> None:
        try:
            await self.api("setMyCommands", commands=[
                {"command": c, "description": d} for c, d in [
                    ("status", "Status & PnL"), ("positions", "Open positions"),
                    ("buy", "Manual buy"), ("sell", "Sell a position"),
                    ("pause", "Pause entries"), ("resume", "Resume entries"),
                    ("stats", "Performance"), ("copy", "Copy-trade wallets"), ("help", "Help")]])
        except Exception as e:
            log.debug("setMyCommands failed: %s", e)
        while True:
            try:
                updates = await self.api("getUpdates", offset=self.offset, timeout=25,
                                         allowed_updates=["message", "callback_query"])
                for u in updates or []:
                    self.offset = u["update_id"] + 1
                    await self.handle_update(u)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll failed: %s", e)
                await asyncio.sleep(5)

    async def reply(self, text: str, buttons=None) -> None:
        await self.engine.notifier.telegram(text, buttons, chat_id=self.chat_id)

    async def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                return
            try:
                await self.api("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            await self.handle_callback(cq.get("data", ""))
            return
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != self.chat_id:
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            try:
                await self.handle_command(text)
            except Exception as e:
                log.exception("telegram command failed")
                await self.reply(f"⚠️ {html.escape(str(e))}")

    async def handle_callback(self, data: str) -> None:
        parts = data.split(":")
        if parts[0] == "s" and len(parts) == 3:
            await self.reply(await self.engine.manual_sell(parts[1], float(parts[2])))
        elif parts[0] == "b" and len(parts) == 2:
            await self.reply(await self.engine.manual_buy(parts[1], None, force=False))
        elif parts[0] == "refresh":
            await self.cmd_positions()

    async def handle_command(self, text: str) -> None:
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        e = self.engine
        if cmd in ("/start", "/help"):
            await self.reply(HELP)
        elif cmd == "/status":
            await self.reply(await e.status_text())
        elif cmd == "/positions":
            await self.cmd_positions()
        elif cmd == "/buy" and args:
            sol = float(args[1]) if len(args) > 1 and args[1] != "force" else None
            await self.reply(await e.manual_buy(args[0], sol, force="force" in args))
        elif cmd == "/sell" and args:
            pct = float(args[1].rstrip("%")) if len(args) > 1 else 100.0
            await self.reply(await e.manual_sell(args[0], pct))
        elif cmd == "/pause":
            e.set_paused(True)
            await self.reply("⏸ New entries paused. Open positions are still managed.")
        elif cmd == "/resume":
            e.set_paused(False)
            await self.reply("▶️ Entries resumed.")
        elif cmd == "/setbuy" and args:
            sol = float(args[0])
            if not 0 < sol <= 100:
                raise ValueError("buy size must be between 0 and 100 SOL")
            e.cfg.trading.buy_amount_sol = sol
            e.store.set_setting("buy_amount_sol", str(sol))
            await self.reply(f"Auto-buy size set to {sol} SOL")
        elif cmd == "/copy":
            await self.cmd_copy(args)
        elif cmd == "/block" and args:
            e.store.block(args[0], "manual")
            await self.reply(f"🚫 blocked {html.escape(args[0])}")
        elif cmd == "/stats":
            from .stats import format_summary, summarize
            await self.reply(f"<pre>{html.escape(format_summary(summarize(e.store)))}</pre>")
        else:
            await self.reply(HELP)

    async def cmd_positions(self) -> None:
        open_pos = [p for p in self.engine.positions.values() if not p.closed]
        if not open_pos:
            await self.reply("No open positions.")
            return
        for p in open_pos:
            value = p.tokens_remaining * p.last_price
            await self.reply(
                f"<b>{html.escape(p.symbol)}</b> <code>{p.mint}</code>\n"
                f"PnL {p.pnl_pct:+.0f}% · value {value:.4f} SOL · in {p.sol_in:.4f} · "
                f"out {p.sol_out:.4f}\nPeak {((p.peak_price / p.entry_price) - 1) * 100:+.0f}%",
                buttons=[[("Sell 25%", f"s:{p.mint}:25"), ("Sell 50%", f"s:{p.mint}:50"),
                          ("Sell 100%", f"s:{p.mint}:100")], [("🔄 Refresh", "refresh")]])

    async def cmd_copy(self, args: list[str]) -> None:
        e = self.engine
        if not args or args[0] == "list":
            wallets = e.copy_wallets()
            if not wallets:
                await self.reply("No copy-trade wallets. /copy add &lt;wallet&gt; [label] [sol]")
                return
            await self.reply("\n".join(
                f"• {html.escape(w.label or '-')} <code>{w.address}</code> "
                f"{w.buy_sol or e.cfg.trading.buy_amount_sol} SOL" for w in wallets))
        elif args[0] == "add" and len(args) >= 2:
            label = args[2] if len(args) > 2 else ""
            sol = float(args[3]) if len(args) > 3 else 0.0
            await e.add_copy_wallet(args[1], label, sol)
            await self.reply(f"👥 copying {html.escape(label or args[1])}")
        elif args[0] in ("rm", "remove") and len(args) >= 2:
            ok = await e.remove_copy_wallet(args[1])
            await self.reply("removed" if ok else "not found")
        else:
            await self.reply("/copy list | add &lt;wallet&gt; [label] [sol] | rm &lt;wallet&gt;")
