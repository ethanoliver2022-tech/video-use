"""Tests for the paid-bot feature set: initials, moonbag, snipers, limit orders,
wallet tracker, token card, daily report."""
import asyncio
import struct
import time
from datetime import datetime, timezone

import httpx
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper import exits
from sniper.config import ExitConfig, TakeProfitLevel, load_config
from sniper.engine import Engine
from sniper.models import Candidate, Fill, Position
from sniper.pump_curve import parse_curve
from sniper.settings import BY_KEY, parse_value
from sniper.telegram_bot import TelegramControl


def wallet():
    return str(Keypair().pubkey())


def pos(**kw):
    base = dict(mint="M", symbol="T", source="pumpfun", creator=None, entry_price=1.0,
                tokens_initial=1000, tokens_remaining=1000, sol_in=1000.0)
    base.update(kw)
    return Position(**base)


# ---------- sell initials ----------

def test_sell_initials_recovers_cost_once():
    cfg = ExitConfig(take_profit=[], sell_initials_at_pct=100, trailing_activate_pct=1e9)
    p = pos()
    p.update_price(1.5)
    assert exits.evaluate(p, cfg) is None
    p.update_price(2.0)  # 2x
    d = exits.evaluate(p, cfg)
    assert d.kind == "initials" and not d.sell_all
    assert d.tokens == pytest.approx(1000 / 2.0 * exits.INITIALS_BUFFER)  # ~half the bag
    exits.apply_fill(p, d, d.tokens, 1030.0, cfg)
    assert p.initials_taken and p.sol_out >= p.sol_in
    assert exits.evaluate(p, cfg) is None  # only once


# ---------- moonbag ----------

def moon_cfg(**kw):
    base = dict(take_profit=[TakeProfitLevel(50, 50), TakeProfitLevel(100, 50)], moonbag_pct=10,
                moonbag_trailing_pct=50, moonbag_max_hold_hours=24, max_hold_seconds=600,
                trailing_activate_pct=1e9, breakeven_after_first_tp=True)
    base.update(kw)
    return ExitConfig(**base)


def test_take_profits_stop_at_the_moonbag():
    cfg = moon_cfg()
    p = pos()
    p.update_price(1.6)
    d = exits.evaluate(p, cfg)
    assert d.kind == "tp" and d.tokens == pytest.approx(500)
    exits.apply_fill(p, d, 500, 800, cfg)
    p.update_price(2.1)
    d = exits.evaluate(p, cfg)  # second TP would sell the other 500: capped to keep 100
    assert d.tokens == pytest.approx(400) and not d.sell_all
    exits.apply_fill(p, d, 400, 840, cfg)
    assert p.tokens_remaining == pytest.approx(100) and exits.in_moonbag(p, cfg)


def test_moonbag_ignores_time_exits_but_respects_its_trailing_stop_and_danger():
    cfg = moon_cfg()
    p = pos(tokens_remaining=100, opened_at=time.time() - 3600)
    p.tp_levels_hit = {0, 1}
    p.update_price(3.0)
    p.last_update = time.time() - 10_000  # stale + past max_hold for a normal position
    assert exits.evaluate(p, cfg) is None  # moonbag rides
    p.update_price(1.4)  # 53% off the 3.0 peak
    assert "moonbag trailing" in exits.evaluate(p, cfg).reason
    p.update_price(3.0)
    p.peak_price = 3.0
    p.dev_sold = True
    assert exits.evaluate(p, cfg).reason == "dev sold"
    q = pos(tokens_remaining=100, opened_at=time.time() - 25 * 3600)
    q.tp_levels_hit = {0}
    assert exits.evaluate(q, cfg).reason == "moonbag max hold"


def test_soft_exit_after_profit_keeps_the_bag_but_losers_sell_everything():
    cfg = moon_cfg()
    p = pos(tokens_remaining=500, opened_at=time.time() - 700)  # past max hold
    p.tp_levels_hit = {0}
    p.update_price(1.2)
    d = exits.evaluate(p, cfg)
    assert d.reason == "max hold time" and d.tokens == pytest.approx(400) and not d.sell_all
    loser = pos(opened_at=time.time() - 700)  # no profit taken: no moonbag
    d = exits.evaluate(loser, cfg)
    assert d.sell_all


async def test_moonbag_does_not_use_a_position_slot(tmp_path):
    eng = make_engine(tmp_path, exits={"moonbag_pct": 10}, trading={"max_open_positions": 1})
    bag = pos(mint="BAG", tokens_remaining=100)
    bag.tp_levels_hit = {0}
    eng.positions["BAG"] = bag
    assert await eng.risk_block(0.1) is None
    eng.positions["FULL"] = pos(mint="FULL")
    assert await eng.risk_block(0.1) == "max open positions"
    await eng.http.aclose()


async def test_take_profit_above_moonbag_is_bookkeeping_only(tmp_path):
    eng = make_engine(tmp_path)
    calls = []

    class Ex:
        async def sell(self, *a, **k):
            calls.append(a)
            return Fill(tokens=1, sol=1)
    eng.executor = Ex()
    p = pos(tokens_remaining=100)
    eng.positions["M"] = p
    d = exits.ExitDecision(0.0, False, "take profit +100%", tp_index=1, kind="tp")
    assert await eng.execute_sell(p, d) == "nothing to sell"
    assert calls == [] and p.tp_levels_hit == {0, 1}
    await eng.http.aclose()


# ---------- snipers ----------

def make_engine(tmp_path, **sections) -> Engine:
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = sections.pop("api_key", "k")
    cfg.filters.reject_reused_socials = False
    for section, values in sections.items():
        for k, v in values.items():
            setattr(getattr(cfg, section), k, v)
    eng = Engine(cfg, live=False)
    eng.sent_ws, eng.messages = [], []

    async def fake_ws(payload):
        eng.sent_ws.append(payload)
    eng.stream._send = fake_ws

    async def capture(text, buttons=None, chat_id=None):
        eng.messages.append((text, buttons))
    eng.notifier.telegram = capture
    eng.notifier.token, eng.notifier.chat = "T", "1"
    return eng


def launch(creator=None, name="Frog", symbol="FROG"):
    return Candidate(chain="solana", mint=wallet(), source="pumpfun", creator=creator or wallet(),
                     name=name, symbol=symbol, v_sol=30.0, v_tokens=1.07e9, route="pump")


async def test_snipe_modes(tmp_path):
    dev = wallet()
    eng = make_engine(tmp_path, discovery={"auto_snipe": "targeted", "dev_watchlist": [dev],
                                           "snipe_keywords": ["trump"], "dev_snipe_sol": 0.3})
    await eng.on_candidate(launch(name="Some Frog"))
    assert eng.queue.empty()  # targeted: random launches ignored
    await eng.on_candidate(launch(name="TRUMP 2028", symbol="T28"))
    c = eng.queue.get_nowait()
    assert c.trigger == "keyword:trump" and not c.force  # keywords still run filters
    await eng.on_candidate(launch(creator=dev))
    c = eng.queue.get_nowait()
    assert c.trigger == "dev" and c.force and c.buy_sol == 0.3  # trusted dev: instant

    eng.cfg.discovery.auto_snipe = "off"
    await eng.on_candidate(launch(creator=dev))
    assert eng.queue.empty()
    eng.cfg.discovery.auto_snipe = "all"
    await eng.on_candidate(launch(name="anything"))
    assert not eng.queue.empty()
    await eng.http.aclose()


async def test_dev_snipe_buys_and_tags_source(tmp_path):
    dev = wallet()
    eng = make_engine(tmp_path, discovery={"dev_watchlist": [dev]})
    c = launch(creator=dev)
    await eng.on_candidate(c)
    res = await eng.handle_candidate(eng.queue.get_nowait())
    assert res.startswith("🟢") and "watched dev" in res
    assert eng.positions[c.mint].source == "pumpfun/dev"
    await eng.http.aclose()


# ---------- limit orders ----------

class PriceEngineExecutor:
    def __init__(self):
        self.buys = []

    async def buy(self, c, sol, curve):
        self.buys.append((c.mint, sol))
        return Fill(tokens=1000.0, sol=sol)

    async def sell(self, mint, tokens, sell_all, pump, curve):
        return Fill(tokens=tokens, sol=0.5)

    async def quote_sell(self, mint, tokens):
        return None


async def test_limit_buy_triggers_on_dip_and_persists(tmp_path):
    eng = make_engine(tmp_path)
    eng.executor = PriceEngineExecutor()
    mint = wallet()
    price = {"v": 1e-6}

    async def price_of(m):
        return price["v"]
    eng.price_of = price_of

    async def ok(c):
        from sniper.models import SafetyReport
        return SafetyReport(passed=True)
    eng.safety.evaluate = ok

    msg = await eng.place_limit_buy(mint, 0.2, -30, hours=1)
    assert "Order #1" in msg and "dips 30%" in msg
    await eng.check_orders()
    await eng.settle()
    assert eng.executor.buys == []  # not yet
    price["v"] = 0.69e-6
    await eng.check_orders()
    await eng.settle()
    assert eng.executor.buys == [(mint, 0.2)]
    assert eng.store.open_orders() == []
    assert eng.positions[mint].source == "limit/limit"
    await eng.http.aclose()


async def test_limit_buy_works_while_paused_and_orders_expire(tmp_path):
    eng = make_engine(tmp_path)
    eng.set_paused(True)
    eng.executor = PriceEngineExecutor()

    async def price_of(m):
        return 1.0
    eng.price_of = price_of

    async def ok(c):
        from sniper.models import SafetyReport
        return SafetyReport(passed=True)
    eng.safety.evaluate = ok
    await eng.place_limit_buy(wallet(), 0.1, +10)
    await eng.place_limit_buy(wallet(), 0.1, -10, hours=0.0001)
    await asyncio.sleep(0.5)
    await eng.check_orders()
    await eng.settle()
    assert eng.executor.buys == []  # +10% not reached; second one expired
    assert len(eng.store.open_orders()) == 1
    assert any("expired" in m[0] for m in eng.messages)
    with pytest.raises(ValueError):
        await eng.place_limit_buy(wallet(), 0, -10)
    with pytest.raises(ValueError):
        await eng.place_limit_buy(wallet(), 0.1, 0)
    await eng.http.aclose()


async def test_limit_sell_from_entry_profit(tmp_path):
    eng = make_engine(tmp_path)
    eng.executor = PriceEngineExecutor()
    p = pos(mint="L", entry_price=1.0, symbol="LL")
    eng.positions["L"] = p
    msg = await eng.place_limit_sell("LL", 50, +100)
    assert "sell 50% of LL at +100% from entry" in msg
    p.update_price(1.9)
    await eng.check_orders()
    await eng.settle()
    assert p.tokens_remaining == 1000
    p.update_price(2.05)
    await eng.check_orders()
    await eng.settle()
    assert p.tokens_remaining == pytest.approx(500)
    assert eng.store.open_orders() == []
    stop = await eng.place_limit_sell("L", 100, -20)  # works as a custom stop too
    assert "-20%" in stop
    p.update_price(0.79)
    await eng.check_orders()
    await eng.settle()
    assert p.closed and p.close_reason == "limit sell"
    await eng.http.aclose()


async def test_sell_orders_cancel_when_position_closes(tmp_path):
    eng = make_engine(tmp_path)
    p = pos(mint="C", symbol="CC")
    eng.positions["C"] = p
    await eng.place_limit_sell("C", 100, 500)
    p.closed = True
    await eng.check_orders()
    assert eng.store.open_orders() == []
    await eng.http.aclose()


# ---------- wallet tracker ----------

async def test_tracker_alerts_without_copying(tmp_path):
    eng = make_engine(tmp_path, copytrade={"enabled": False})
    w = wallet()
    await eng.add_copy_wallet(w, "whale", mode="alert")
    assert not eng.cfg.copytrade.enabled  # tracking doesn't switch copying on
    assert {"method": "subscribeAccountTrade", "keys": [w]} in eng.sent_ws
    mint = wallet()
    await eng.on_trade({"mint": mint, "txType": "buy", "traderPublicKey": w, "solAmount": 2.5,
                        "signature": "t1"})
    await eng.settle()
    text, buttons = eng.messages[-1]
    assert "whale" in text and "bought" in text and "2.500 SOL" in text
    assert ("Buy 0.1", f"b:{mint}:0.1") in buttons[0] and ("🔍 Token card", f"tc:{mint}") in buttons[1]
    assert mint not in eng.positions  # alert only, no buy

    await eng.set_wallet_mode(w, "copy")  # switch to copying
    assert eng.cfg.copytrade.enabled and eng.copy_wallets()[0].mode == "copy"
    await eng.http.aclose()


async def test_tracked_wallets_survive_restart_with_copying_off(tmp_path):
    eng = make_engine(tmp_path)
    w = wallet()
    await eng.add_copy_wallet(w, "t", mode="alert")
    await eng.http.aclose()
    eng2 = make_engine(tmp_path, copytrade={"enabled": False})
    assert [x.address for x in eng2.copy_wallets()] == [w]
    await eng2.http.aclose()


# ---------- token card ----------

def curve_bytes(real_tokens_ui, complete=False, creator=None):
    data = b"\x00" * 8 + struct.pack("<QQQQQ?", int(1e15), int(40e9), int(real_tokens_ui * 1e6), 0,
                                     int(1e15), complete)
    return data + (bytes(Pubkey.from_string(creator)) if creator else b"")


def test_bonding_curve_progress():
    assert parse_curve(curve_bytes(793_100_000)).progress_pct == pytest.approx(0)
    assert parse_curve(curve_bytes(396_550_000)).progress_pct == pytest.approx(50)
    assert parse_curve(curve_bytes(0, complete=True)).progress_pct == 100


async def test_token_card_combines_market_chain_and_safety(tmp_path):
    eng = make_engine(tmp_path)
    mint, dev = wallet(), wallet()

    def handler(req):
        return httpx.Response(200, json=[
            {"baseToken": {"name": "Frog", "symbol": "FROG"}, "priceUsd": "0.00012",
             "marketCap": 120000, "liquidity": {"usd": 30000}, "volume": {"h1": 5000, "h24": 90000},
             "priceChange": {"m5": 3.2, "h1": -10, "h24": 250}, "txns": {"h1": {"buys": 80, "sells": 40}},
             "pairCreatedAt": (time.time() - 7200) * 1000, "dexId": "pumpswap"}])
    eng.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def account_bytes(addr):
        return curve_bytes(200_000_000, creator=dev)

    async def balance(owner, m):
        return 25_000_000.0  # 2.5%
    eng.rpc.get_account_bytes, eng.rpc.get_token_balance = account_bytes, balance

    async def verdict(c):
        from sniper.models import SafetyReport
        r = SafetyReport(passed=True, notes=["top10 wallets hold 18.0%"])
        return r
    eng.safety.evaluate = verdict

    from sniper.token_card import build_card
    text, buttons = await build_card(eng, mint)
    assert "Frog" in text and "$120.00K" in text and "$30.00K" in text
    assert "1h -10.0%" in text and "80 buys / 40 sells" in text and "2.0h" in text
    assert "Bonding curve 75%" in text and "Dev holds 2.50%" in text
    assert "Passes your filters" in text and "top10 wallets hold 18.0%" in text
    flat = [d for row in buttons for _, d in row]
    assert f"b:{mint}:0.1" in flat and f"lb:{mint}" in flat and f"tc:{mint}" in flat
    assert f"https://dexscreener.com/solana/{mint}" in flat
    assert not any(d.startswith("bf:") for d in flat)  # passes: no skip-filters button
    await eng.http.aclose()


async def test_token_card_survives_every_lookup_failing(tmp_path):
    eng = make_engine(tmp_path)
    eng.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    async def boom(*a, **k):
        raise RuntimeError("down")
    eng.rpc.get_account_bytes = boom
    eng.safety.evaluate = boom
    from sniper.token_card import build_card
    text, buttons = await build_card(eng, wallet())
    assert "No DEX pair data" in text and buttons
    await eng.http.aclose()


def test_url_buttons():
    from sniper.notify import keyboard
    kb = keyboard([[("Site", "https://x.io"), ("Act", "a:1")]])
    assert kb["inline_keyboard"][0] == [{"text": "Site", "url": "https://x.io"},
                                        {"text": "Act", "callback_data": "a:1"}]


# ---------- daily report ----------

async def test_daily_report_once_per_day(tmp_path):
    eng = make_engine(tmp_path)
    day1 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    assert not await eng.daily_report(day1)  # first run only starts the clock
    eng.store.event("close", "m", "T", pnl_sol=0.05, reason="take profit +40%", source="pumpfun",
                    held_s=60)
    eng.store.db.execute("UPDATE events SET ts = ?", (day1,))
    day2 = datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc).timestamp()
    assert await eng.daily_report(day2)
    assert "Daily report" in eng.messages[-1][0] and "Trades: 1" in eng.messages[-1][0]
    assert not await eng.daily_report(day2 + 600)  # not twice
    await eng.http.aclose()


# ---------- settings + telegram ----------

def test_new_setting_kinds():
    assert parse_value(BY_KEY["discovery.auto_snipe"], "Targeted") == "targeted"
    with pytest.raises(ValueError):
        parse_value(BY_KEY["discovery.auto_snipe"], "sometimes")
    assert parse_value(BY_KEY["discovery.snipe_keywords"], "trump, pepe ,ai") == ["trump", "pepe", "ai"]
    assert parse_value(BY_KEY["discovery.snipe_keywords"], "none") == []
    with pytest.raises(ValueError):
        parse_value(BY_KEY["exits.moonbag_pct"], "80")


async def test_telegram_snipers_orders_and_tracking(tmp_path):
    eng = make_engine(tmp_path)
    tg = TelegramControl(eng, "TOKEN", "7", eng.http)
    sent = []

    async def capture(text, buttons=None, chat_id=None):
        sent.append((text, buttons))
    eng.notifier.telegram = capture

    async def api(method, **params):
        if method == "editMessageText":
            raise RuntimeError("no edit in tests")
        return {"message_id": 1}
    tg.api = api

    async def tap(data):
        await tg.handle_update({"callback_query": {"id": "q", "data": data,
                                                   "message": {"chat": {"id": 7}, "message_id": 3}}})
        await eng.settle()

    async def say(text):
        await tg.handle_update({"message": {"chat": {"id": 7}, "text": text, "message_id": 9}})
        await eng.settle()

    await say("/menu")
    flat = [d for row in sent[-1][1] for _, d in row]
    assert "sn" in flat and "o" in flat
    await tap("sn")
    idx = next(i for i, d in enumerate(flat) if d == "sn")
    labels = [label for row in sent[-1][1] for label, _ in row]
    assert any(label.startswith("Snipe mode: all") for label in labels)
    mode_idx = [i for i, s in enumerate(__import__("sniper.settings", fromlist=["SETTINGS"]).SETTINGS)
                if s.key == "discovery.auto_snipe"][0]
    await tap(f"e:{mode_idx}")
    assert eng.cfg.discovery.auto_snipe == "targeted"  # tap cycles the choice

    p = pos(mint="Q", symbol="QQ")
    eng.positions["Q"] = p
    await tap("ls:Q")
    await say("50 +100")
    assert "Order #1" in sent[-1][0]
    await tap("o")
    assert "#1" in sent[-1][0] and ("✖️ Cancel #1", "oc:1") in sent[-1][1][0]
    await tap("oc:1")
    assert eng.store.open_orders() == []

    w = wallet()
    await say(f"/track {w} whale")
    assert "Tracking whale" in sent[-1][0]
    await tap("c")
    assert "🔔 whale" in sent[-1][0]
    await eng.http.aclose()
