"""Crawl every Telegram screen, tap every button, and make sure each one is handled."""
import time

import httpx
from solders.keypair import Keypair

from sniper.config import load_config
from sniper.engine import Engine
from sniper.models import Fill, Position, SafetyReport
from sniper.telegram_bot import TelegramControl


def wallet():
    return str(Keypair().pubkey())


class Ex:
    async def buy(self, c, sol, curve):
        return Fill(tokens=1000.0, sol=sol)

    async def sell(self, mint, tokens, sell_all, pump, curve, slippage_pct=None):
        return Fill(tokens=tokens, sol=0.1)

    async def quote_sell(self, mint, tokens):
        return None


async def test_every_button_on_every_screen_is_handled(tmp_path, monkeypatch):
    import sniper.telegram_bot as tb
    monkeypatch.setattr(tb, "EXPORT_TTL", 0)
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    eng = Engine(cfg, live=False)

    async def ws(payload):
        pass
    eng.stream._send = ws
    eng.executor = Ex()
    eng.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    async def ok(c):
        return SafetyReport(passed=False, reasons=["test"])
    eng.safety.evaluate = ok

    async def none(*a, **k):
        return None
    eng.rpc.get_account_bytes = none

    async def bal(*a, **k):
        return 1.0
    eng.rpc.get_balance_sol = bal
    eng.rpc.get_token_balance = bal

    tg = TelegramControl(eng, "T", "9", eng.http)
    screens = []

    async def capture(text, buttons=None, chat_id=None):
        screens.append(buttons or [])
    eng.notifier.telegram = capture

    async def api(method, **p):
        if method == "editMessageText":
            raise RuntimeError("force send")
        return {"message_id": 1}
    tg.api = api

    # give every screen something to show
    eng.wallet.create()
    mint = wallet()
    pos = Position(mint=mint, symbol="AAA", source="pumpfun", creator=None, entry_price=1.0,
                   tokens_initial=1000, tokens_remaining=1000, sol_in=1.0)
    eng.positions[mint] = pos
    await eng.add_copy_wallet(wallet(), "w1")
    await eng.add_copy_wallet(wallet(), "w2", mode="alert")
    await eng.place_limit_sell(mint, 50, 100)

    async def upd(u):
        await tg.handle_update(u)
        await eng.settle()

    for cmd in ("/menu", "/wallet", "/settings", "/positions", "/orders", "/copy", "/stats",
                "/help", f"/card {mint}"):
        await upd({"message": {"chat": {"id": 9}, "text": cmd, "message_id": 1,
                               "date": int(time.time())}})

    seen, queue = set(), []
    def collect():
        for rows in screens:
            for row in rows:
                for _, data in row:
                    if not data.startswith("http") and data not in seen:
                        seen.add(data)
                        queue.append(data)
        screens.clear()
    collect()
    # destructive / mode-changing taps are covered by their own tests; here we only
    # need to know they reach a handler, so keep state stable around them
    while queue:
        data = queue.pop(0)
        await upd({"callback_query": {"id": "q", "data": data,
                                      "message": {"chat": {"id": 9}, "message_id": 1}}})
        tg.pending = None
        if not eng.positions.get(mint) or eng.positions[mint].closed:
            pos.closed, pos.tokens_remaining = False, 1000
            eng.positions[mint] = pos
        if eng.live:
            await eng.switch_mode(False)
        collect()
    print(sorted({d.split(":")[0] for d in seen}), len(seen))
    assert len(seen) > 40, sorted(seen)
    assert tg.unhandled == [], tg.unhandled
    await eng.http.aclose()
