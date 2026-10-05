"""Feed hostile text into every Telegram prompt: nothing may crash or store a bad value."""
import json
import math

import httpx
from solders.keypair import Keypair

from sniper.config import load_config
from sniper.engine import Engine
from sniper.models import Position
from sniper.settings import SETTINGS
from sniper.telegram_bot import TelegramControl

NASTY = ["", " ", "abc", "-1", "0", "nan", "NaN", "inf", "-inf", "1e309", "1e-320", "9" * 400,
         "0.1 nan", "nan -30", "0.1 inf", "50 nan", "50 +inf", "100 -100", "100 -1e309",
         "<b>x</b>", "'; DROP TABLE orders;--", "🚀🚀", "1,000", "0.5 SOL", "50%", "-30%",
         "1 2 3 4 5", "\x00", "all", "track", str(Keypair().pubkey())]


def finite_everywhere(obj):
    if isinstance(obj, float):
        assert math.isfinite(obj), obj
    elif isinstance(obj, dict):
        for v in obj.values():
            finite_everywhere(v)
    elif isinstance(obj, list):
        for v in obj:
            finite_everywhere(v)


async def test_prompts_reject_garbage(tmp_path):
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    eng = Engine(cfg, live=False)

    async def ws(p):
        pass
    eng.stream._send = ws
    eng.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    async def price(m):
        return 1e-6
    eng.price_of = price

    async def bal(*a):
        return 1.0
    eng.rpc.get_balance_sol = bal
    tg = TelegramControl(eng, "T", "3", eng.http)
    sent = []

    async def cap(text, buttons=None, chat_id=None):
        sent.append(text)
    eng.notifier.telegram = cap

    async def api(method, **p):
        return {"message_id": 1}
    tg.api = api
    mint = str(Keypair().pubkey())
    eng.positions[mint] = Position(mint=mint, symbol="Z", source="manual", creator=None,
                                   entry_price=1e-6, tokens_initial=1e6, tokens_remaining=1e6,
                                   sol_in=1.0)
    eng.wallet.create()
    prompts = [("limit_buy", mint), ("limit_sell", mint), ("buy_custom", mint),
               ("withdraw", None), ("copy_add", None)] + [("edit", s.key) for s in SETTINGS]
    for kind, data in prompts:
        for text in NASTY:
            tg._ask(kind, data)
            await tg.handle_update({"message": {"chat": {"id": 3}, "text": text, "message_id": 2}})
            await eng.settle()
            tg.pending = None
    # nothing non-finite reached the database or the live config
    for o in eng.store.open_orders():
        finite_everywhere(o)
    finite_everywhere(eng.store.overrides())
    for s in SETTINGS:
        v = getattr(getattr(eng.cfg, s.key.split(".")[0]), s.key.split(".")[1])
        if isinstance(v, float):
            assert math.isfinite(v), s.key
        if s.kind in ("float", "int"):
            assert s.lo <= v <= s.hi or v == getattr(
                getattr(load_config("config.example.yaml"), s.key.split(".")[0]), s.key.split(".")[1]), s.key
    for w in eng.store.copy_wallets():
        assert 0 <= w["buy_sol"] <= 100
    json.dumps(eng.store.overrides())  # still valid JSON
    await eng.http.aclose()
