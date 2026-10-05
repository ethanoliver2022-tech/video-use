"""Momentum scanner: tokens pumping right now, switched on and off from Telegram."""
import asyncio

import httpx
import pytest
from solders.keypair import Keypair

from sniper.config import DiscoveryConfig
from sniper.models import Position
from sniper.scanners.momentum import (MomentumScanner, best_pairs, build_signal,
                                      parse_trending, why_not)
from tests.test_telegram import Harness

SOL = "So11111111111111111111111111111111111111112"


def gecko_pool(mint, dex="raydium", m5=None, base_is_sol=False):
    """A GeckoTerminal trending pool in the documented JSON:API shape."""
    attrs = {"name": "FROG / SOL", "address": str(Keypair().pubkey()), "reserve_in_usd": "50000",
             "fdv_usd": "900000", "market_cap_usd": None,
             "price_change_percentage": {"h1": "80", "h24": "300"},
             "transactions": {"h1": {"buys": 900, "sells": 300, "buyers": 400, "sellers": 150}},
             "volume_usd": {"h1": "300000", "h24": "900000"}}
    if m5:
        attrs["price_change_percentage"]["m5"] = str(m5["change"])
        attrs["transactions"]["m5"] = {"buys": m5["buys"], "sells": m5["sells"],
                                       "buyers": m5["buyers"], "sellers": 10}
        attrs["volume_usd"]["m5"] = str(m5["volume"])
    base, quote = (f"solana_{SOL}", f"solana_{mint}") if base_is_sol else (f"solana_{mint}",
                                                                           f"solana_{SOL}")
    return {"id": f"solana_{attrs['address']}", "type": "pool", "attributes": attrs,
            "relationships": {"base_token": {"data": {"id": base, "type": "token"}},
                              "quote_token": {"data": {"id": quote, "type": "token"}},
                              "dex": {"data": {"id": dex, "type": "dex"}}}}


def dex_pair(mint, change=40, volume=30000, buys=200, sells=60, liq=60000):
    return {"chainId": "solana", "dexId": "pumpswap", "url": f"https://dexscreener.com/solana/{mint}",
            "pairAddress": str(Keypair().pubkey()), "pairCreatedAt": 1_700_000_000_000,
            "baseToken": {"address": mint, "name": "Froggo", "symbol": "FROG"},
            "priceChange": {"m5": change, "h1": 120}, "volume": {"m5": volume, "h1": 200000},
            "txns": {"m5": {"buys": buys, "sells": sells}, "h1": {"buys": 900, "sells": 300}},
            "liquidity": {"usd": liq}, "fdv": 800000, "marketCap": 750000}


def strong(mint):
    return build_signal({"mint": mint, "name": "FROG / SOL", "dex": "raydium",
                         "buyers_5m": 120}, dex_pair(mint))


# ---- parsing (untrusted data) ----

def test_trending_pools_parse_with_and_without_5m_fields():
    a, b, c = (str(Keypair().pubkey()) for _ in range(3))
    payload = {"data": [gecko_pool(a, m5={"change": 30, "buys": 90, "sells": 20, "buyers": 70,
                                          "volume": 25000}),
                        gecko_pool(b, dex="pump-fun"),                 # no 5m fields at all
                        gecko_pool(c, base_is_sol=True),               # listed as SOL / NEW
                        gecko_pool(a),                                 # same token twice
                        gecko_pool("abc:def" + "1" * 30),             # not an address
                        {"junk": 1}, "x", None]}
    rows = parse_trending(payload)
    assert [r["mint"] for r in rows] == [a, b, c]
    assert rows[0]["buyers_5m"] == 70 and rows[0]["change_5m"] == 30.0
    assert rows[1]["buyers_5m"] is None and rows[1]["dex"] == "pump-fun"
    for bad in (None, [], {"data": "x"}, {"data": [None]}, "nope"):
        assert parse_trending(bad) == []


def test_dexscreener_numbers_win_and_gecko_fills_gaps():
    m = str(Keypair().pubkey())
    t = {"mint": m, "name": "FROG / SOL", "dex": "raydium", "buyers_5m": 77, "change_5m": 5.0,
         "volume_5m": 100.0, "buys_5m": 3, "sells_5m": 1, "liquidity_usd": 1.0}
    s = build_signal(t, dex_pair(m))
    assert (s.change_5m, s.volume_5m, s.buys_5m, s.sells_5m) == (40.0, 30000.0, 200, 60)
    assert s.buyers_5m == 77 and s.symbol == "FROG" and s.liquidity_usd == 60000
    assert s.pump_route  # PumpSwap: tradeable through PumpPortal
    alone = build_signal(t, None)                      # DexScreener down: GeckoTerminal only
    assert (alone.change_5m, alone.symbol, alone.dex) == (5.0, "FROG", "raydium")
    assert alone.url.endswith(m)
    evil = dex_pair(m)
    evil["url"] = "javascript:alert(1)"
    assert build_signal(t, evil).url == f"https://dexscreener.com/solana/{m}"
    weird = build_signal(t, {"priceChange": {"m5": "NaN"}, "txns": {"m5": "x"}, "volume": None})
    assert weird.change_5m == 5.0 and weird.buys_5m == 3


def test_most_liquid_pair_is_used():
    m = str(Keypair().pubkey())
    pairs = best_pairs([dex_pair(m, liq=1000), dex_pair(m, liq=90000), {"bad": 1}, None])
    assert pairs[m]["liquidity"]["usd"] == 90000


# ---- the signal rule ----

def test_each_condition_can_block_a_signal():
    d = DiscoveryConfig()
    m = str(Keypair().pubkey())
    assert why_not(strong(m), d) is None
    s = strong(m)
    s.change_5m = 10
    assert "up 10%" in why_not(s, d)
    s = strong(m)
    s.volume_5m = 500
    assert "volume" in why_not(s, d)
    s = strong(m)
    s.buyers_5m = 5
    assert "5 buyers" in why_not(s, d)
    s = strong(m)
    s.buyers_5m, s.buys_5m = None, 10                  # no unique count: buy transactions
    assert "10 buyers" in why_not(s, d)
    s = strong(m)
    s.sells_5m = 190                                   # 200 buys vs 190 sells: no edge
    assert "sells" in why_not(s, d)
    s = strong(m)
    s.liquidity_usd = 2000
    assert "liquidity" in why_not(s, d)


# ---- the scanner loop ----

def _transport(calls, gecko_status=200, mint=None):
    def handler(req):
        calls.append(req.url.host)
        if "geckoterminal" in req.url.host:
            if gecko_status != 200:
                return httpx.Response(gecko_status, json={})
            return httpx.Response(200, json={"data": [gecko_pool(mint), gecko_pool(SOL)]})
        return httpx.Response(200, json=[dex_pair(mint)])
    return httpx.MockTransport(handler)


async def test_scan_finds_a_pump_and_reports_it():
    m, calls, got = str(Keypair().pubkey()), [], []

    async def on_signal(s):
        got.append(s)
    http = httpx.AsyncClient(transport=_transport(calls, mint=m))
    d = DiscoveryConfig(momentum_enabled=True)
    scanner = MomentumScanner(d, "https://api.geckoterminal.com/api/v2",
                              "https://api.dexscreener.com", http, on_signal)
    hits = await scanner.tick()
    assert [s.mint for s in hits] == [m] and got == hits
    assert calls == ["api.geckoterminal.com", "api.dexscreener.com"]  # 2 calls a round
    await http.aclose()


async def test_rate_limited_scan_is_skipped_quietly():
    calls = []
    http = httpx.AsyncClient(transport=_transport(calls, gecko_status=429, mint="x"))
    scanner = MomentumScanner(DiscoveryConfig(momentum_enabled=True), "https://g", "https://d",
                              http, None)
    assert await scanner.tick() == []
    await http.aclose()


async def test_switched_off_means_no_requests_at_all(monkeypatch):
    import sniper.scanners.momentum as mm
    monkeypatch.setattr(mm, "MIN_POLL_SECONDS", 0.01)
    calls = []
    http = httpx.AsyncClient(transport=_transport(calls, mint=str(Keypair().pubkey())))
    d = DiscoveryConfig(momentum_enabled=False, momentum_poll_seconds=0)
    got = []

    async def on_signal(s):
        got.append(s)
    task = asyncio.ensure_future(MomentumScanner(
        d, "https://api.geckoterminal.com/api/v2", "https://api.dexscreener.com", http,
        on_signal).run())
    await asyncio.sleep(0.05)
    assert calls == []                               # off: costs nothing
    d.momentum_enabled = True                        # switched on live
    await asyncio.sleep(0.05)
    assert calls and got
    d.momentum_enabled = False
    await asyncio.sleep(0.03)
    n = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == n                           # and off again
    task.cancel()
    await http.aclose()


# ---- what the bot does with a signal ----

async def test_signal_alerts_once_with_buy_buttons(tmp_path):
    h = Harness(tmp_path)
    m = str(Keypair().pubkey())
    await h.eng.on_momentum(strong(m))
    await h.eng.on_momentum(strong(m))               # next scan: same pump, no repeat
    alerts = [t for t, b, _ in h.sent if "Momentum" in t]
    assert len(alerts) == 1 and "+40% in 5m" in alerts[0] and m in alerts[0]
    assert f"b:{m}:0.1" in h.buttons() and f"tc:{m}" in h.buttons()
    await h.close()


async def test_no_signal_for_held_or_blocklisted_tokens_and_hourly_cap(tmp_path):
    h = Harness(tmp_path)
    held = str(Keypair().pubkey())
    h.eng.positions[held] = Position(mint=held, symbol="H", source="x", creator=None,
                                     entry_price=1.0, tokens_initial=1.0, tokens_remaining=1.0,
                                     sol_in=0.1)
    await h.eng.on_momentum(strong(held))
    rug = strong(str(Keypair().pubkey()))
    rug.symbol, rug.name = "RUG", "rug pull"
    await h.eng.on_momentum(rug)
    assert not [t for t, _, _ in h.sent if "Momentum" in t]
    h.eng.cfg.discovery.momentum_alerts_per_hour = 2
    for _ in range(4):
        await h.eng.on_momentum(strong(str(Keypair().pubkey())))
    assert len([t for t, _, _ in h.sent if "Momentum" in t]) == 2
    await h.close()


async def test_buy_mode_buys_through_the_normal_filters(tmp_path):
    h = Harness(tmp_path)
    d = h.eng.cfg.discovery
    d.momentum_action, d.momentum_buy_sol = "buy", 0.07
    h.eng.paused = False
    seen = []

    async def handle(c):
        seen.append(c)
        return "❌ rejected: top 10 wallets hold 80% of supply"
    h.eng.handle_candidate = handle
    m = str(Keypair().pubkey())
    await h.eng.on_momentum(strong(m))
    await h.eng.settle()
    assert len(seen) == 1
    c = seen[0]
    assert (c.source, c.buy_sol, c.route, c.force) == ("momentum", 0.07, "pump", False)
    assert any("not bought" in t and "top 10" in t for t, _, _ in h.sent)
    await h.close()


async def test_buy_mode_only_alerts_while_paused(tmp_path):
    h = Harness(tmp_path)
    h.eng.cfg.discovery.momentum_action = "buy"
    h.eng.paused = True
    seen = []

    async def handle(c):
        seen.append(c)
    h.eng.handle_candidate = handle
    await h.eng.on_momentum(strong(str(Keypair().pubkey())))
    await h.eng.settle()
    assert not seen and "buy mode is paused" in h.sent[-1][0]
    await h.close()


# ---- on / off from Telegram ----

async def test_one_tap_toggle_on_the_main_menu_and_it_persists(tmp_path):
    h = Harness(tmp_path)
    await h.text("/menu")
    assert "mo" in h.buttons() and "Momentum scanner" not in h.last
    await h.tap("mo")
    assert h.eng.cfg.discovery.momentum_enabled
    assert "Momentum scanner: on (alerts)" in h.last
    assert h.eng.store.overrides()["discovery.momentum_enabled"] is True  # survives restarts
    await h.tap("mo")
    assert not h.eng.cfg.discovery.momentum_enabled
    await h.close()


async def test_momentum_command_and_settings_page(tmp_path):
    h = Harness(tmp_path)
    await h.text("/momentum on")
    assert h.eng.cfg.discovery.momentum_enabled and "on" in h.last
    await h.text("/momentum off")
    assert not h.eng.cfg.discovery.momentum_enabled
    await h.text("/momentum")
    assert "Momentum" in h.last and any(b.startswith("e:") for b in h.buttons())
    from sniper.settings import BY_KEY, parse_value
    with pytest.raises(ValueError):
        parse_value(BY_KEY["discovery.momentum_action"], "yolo")
    with pytest.raises(ValueError):
        parse_value(BY_KEY["discovery.momentum_poll_seconds"], "1")  # would hammer the free API
    await h.close()
