"""PumpPortal websocket: new pump.fun launches, migrations, and live trades.

One connection is shared for everything (PumpPortal asks clients not to open a
socket per token). Subscriptions are replayed automatically after reconnects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Awaitable, Callable, Optional

import websockets

from ..models import Candidate, num

log = logging.getLogger(__name__)

CandidateHandler = Callable[[Candidate], Awaitable[None]]
TradeHandler = Callable[[dict], Awaitable[None]]
MigrationHandler = Callable[[str], Awaitable[None]]


def candidate_from_create(msg: dict) -> Candidate:
    return Candidate(
        chain="solana",
        mint=msg["mint"],
        source="pumpfun",
        symbol=msg.get("symbol") if isinstance(msg.get("symbol"), str) else "",
        name=msg.get("name") if isinstance(msg.get("name"), str) else "",
        creator=msg.get("traderPublicKey"),
        created_at=time.time(),
        pool=msg.get("bondingCurveKey") if isinstance(msg.get("bondingCurveKey"), str) else None,
        creator_initial_buy_tokens=_f(msg.get("initialBuy")),
        v_sol=num(msg.get("vSolInBondingCurve")),
        v_tokens=num(msg.get("vTokensInBondingCurve")),
        url=f"https://pump.fun/coin/{msg['mint']}",
        uri=msg.get("uri") if isinstance(msg.get("uri"), str) else None,
        route="pump",
    )


def trade_price(msg: dict) -> Optional[float]:
    """SOL per token implied by a trade message (None if the message is malformed)."""
    v_sol, v_tok = num(msg.get("vSolInBondingCurve")), num(msg.get("vTokensInBondingCurve"))
    if v_sol and v_tok:
        return v_sol / v_tok
    sol, tok = num(msg.get("solAmount")), num(msg.get("tokenAmount"))
    if sol and tok:
        return sol / tok
    return None


def _f(v) -> Optional[float]:
    return num(v, allow_zero=True)


# PumpPortal error texts that mean "no paid trade feed for you" (balance / key problems)
REFUSED = re.compile(r"balance|api.?key|unauthori|insufficient|fund|invalid key|not authori", re.I)


def _valid_address(v) -> bool:
    return isinstance(v, str) and 32 <= len(v) <= 44 and v.isalnum()


class PumpPortalStream:
    """New-token and migration events are free. Token and account trade streams need a
    PumpPortal API key whose linked wallet holds SOL (PumpPortal bills per message), so
    without a key those subscriptions are tracked but never sent."""

    def __init__(self, url: str, new_tokens: bool, migrations: bool, api_key: str = ""):
        self.url = url
        self.api_key = api_key
        self.want_new_tokens = new_tokens
        self.want_migrations = migrations
        self.token_subs: set[str] = set()
        self.account_subs: set[str] = set()
        self.on_candidate: Optional[CandidateHandler] = None
        self.on_trade: Optional[TradeHandler] = None
        self.on_migration: Optional[MigrationHandler] = None
        self._ws = None
        self._send_lock = asyncio.Lock()
        # PumpPortal can accept the connection yet refuse the paid trade feed (e.g. the
        # wallet linked to the key is below its minimum balance). Then nothing arrives, and
        # the bot must not rely on it: it falls back to on-chain checks until trades flow.
        self.feed_ok = True
        self.feed_error = ""
        self.on_feed_change: Optional[Callable[[bool, str], None]] = None

    @property
    def trades_enabled(self) -> bool:
        """A key is configured: trade subscriptions are sent."""
        return bool(self.api_key)

    @property
    def trades_live(self) -> bool:
        """The trade feed can be relied on (a key, and PumpPortal isn't refusing it)."""
        return self.trades_enabled and self.feed_ok

    def _set_feed(self, ok: bool, error: str = "") -> None:
        if ok == self.feed_ok:
            return
        self.feed_ok, self.feed_error = ok, error
        if self.on_feed_change:
            self.on_feed_change(ok, error)

    async def resubscribe(self) -> None:
        """Ask for the trade feeds again (e.g. after the PumpPortal wallet was topped up)."""
        if self.trades_enabled:
            if self.token_subs:
                await self._send({"method": "subscribeTokenTrade", "keys": sorted(self.token_subs)})
            if self.account_subs:
                await self._send({"method": "subscribeAccountTrade",
                                  "keys": sorted(self.account_subs)})

    @property
    def connect_url(self) -> str:
        if not self.api_key:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}api-key={self.api_key}"

    async def watch_token(self, mint: str) -> None:
        if mint in self.token_subs:
            return
        self.token_subs.add(mint)
        if self.trades_enabled:
            await self._send({"method": "subscribeTokenTrade", "keys": [mint]})

    async def unwatch_token(self, mint: str) -> None:
        if mint not in self.token_subs:
            return
        self.token_subs.discard(mint)
        if self.trades_enabled:
            await self._send({"method": "unsubscribeTokenTrade", "keys": [mint]})

    async def set_feeds(self, new_tokens: bool, migrations: bool) -> None:
        if new_tokens != self.want_new_tokens:
            self.want_new_tokens = new_tokens
            await self._send({"method": "subscribeNewToken" if new_tokens else "unsubscribeNewToken"})
        if migrations != self.want_migrations:
            self.want_migrations = migrations
            await self._send({"method": "subscribeMigration" if migrations else "unsubscribeMigration"})

    async def watch_accounts(self, wallets: list[str]) -> None:
        new = [w for w in wallets if w not in self.account_subs]
        if new:
            self.account_subs.update(new)
            if self.trades_enabled:
                await self._send({"method": "subscribeAccountTrade", "keys": new})

    async def unwatch_account(self, wallet: str) -> None:
        if wallet in self.account_subs:
            self.account_subs.discard(wallet)
            if self.trades_enabled:
                await self._send({"method": "unsubscribeAccountTrade", "keys": [wallet]})

    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            return  # will be replayed on (re)connect
        async with self._send_lock:
            try:
                await ws.send(json.dumps(payload))
            except Exception as e:  # connection dropping; reconnect loop replays subs
                log.debug("pumpportal send failed: %s", e)

    async def _subscribe_all(self) -> None:
        if self.want_new_tokens:
            await self._send({"method": "subscribeNewToken"})
        if self.want_migrations:
            await self._send({"method": "subscribeMigration"})
        if self.token_subs and self.trades_enabled:
            await self._send({"method": "subscribeTokenTrade", "keys": sorted(self.token_subs)})
        if self.account_subs and self.trades_enabled:
            await self._send({"method": "subscribeAccountTrade", "keys": sorted(self.account_subs)})

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.connect_url, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, max_size=2**22) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("pumpportal connected")
                    await self._subscribe_all()
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = str(e).replace(self.api_key, "***") if self.api_key else str(e)
                log.warning("pumpportal disconnected: %s (retry in %.0fs)", err, backoff)
            finally:
                self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _dispatch(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "mint" in msg and not _valid_address(msg["mint"]):
            return  # malformed: never let it reach the engine
        for key in ("traderPublicKey", "signature"):
            if key in msg and not isinstance(msg[key], str):
                msg.pop(key)
        if "mint" not in msg:
            err = msg.get("errors") or msg.get("error")
            if err:  # e.g. bad / unfunded API key
                text = str(err)[:200]
                log.warning("pumpportal: %s", text)
                if self.trades_enabled and REFUSED.search(text):
                    self._set_feed(False, text)
            return  # subscription acks etc.
        tx_type = msg.get("txType")
        if tx_type in ("buy", "sell") and not self.feed_ok:
            self._set_feed(True)  # trades are flowing again
        try:
            if tx_type == "create" and self.on_candidate and self.want_new_tokens:
                await self.on_candidate(candidate_from_create(msg))
            elif tx_type in ("buy", "sell") and self.on_trade:
                await self.on_trade(msg)
            elif tx_type == "migrate" or (tx_type is None and msg.get("pool")):
                if self.on_migration:
                    await self.on_migration(msg["mint"])
                if self.want_migrations and self.on_candidate:
                    await self.on_candidate(Candidate(
                        chain="solana", mint=msg["mint"], source="pumpfun-migration", route="pump",
                        url=f"https://pump.fun/coin/{msg['mint']}",
                    ))
        except Exception:
            log.exception("pumpportal handler error")
