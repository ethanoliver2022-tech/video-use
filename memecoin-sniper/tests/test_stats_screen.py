"""The stats screen: periods, strategies, recent trades, costs, recap, chart, you vs your
wallets, and what coins did after you sold."""
import time
from datetime import datetime, timezone

import pytest

from sniper import report
from sniper.models import Candidate, Position
from tests.test_telegram import Harness

W = "Wa11et1111111111111111111111111111111111111"


def _close(st, sym, pnl, sol_in=0.1, ago=0.0, **kw):
    st.event("close", f"M{sym}", sym, pnl_sol=pnl, sol_in=sol_in, sol_out=sol_in + pnl,
             reason=kw.pop("reason", "take profit +40%"), source=kw.pop("source", "pumpfun"),
             held_s=kw.pop("held_s", 300), **kw)
    if ago:
        st.db.execute("UPDATE events SET ts = ? WHERE id = (SELECT MAX(id) FROM events)",
                      (time.time() - ago,))


def test_strategy_names_from_new_and_old_records():
    name = report.strategy_name
    wn = {"W": "Cupsey"}.get
    assert name(report.strategy_key("copy", "W"), lambda a: wn(a) or "?") == "👥 Cupsey"
    assert name(report.strategy_key("pumpfun/dev"), str) == "🎯 Sniping (dev watch)"
    assert name(report.strategy_key("pumpfun"), str) == "🎯 Sniping"
    assert name(report.strategy_key("x", stored="call:Alpha Calls"), str) == "📣 Alpha Calls"
    assert name(report.strategy_key("pumpfun-migration"), str) == "🎓 Migrations"
    assert name(report.strategy_key("manual"), str) == "✋ Manual"


def test_buys_record_their_strategy():
    s = __import__("sniper.engine", fromlist=["Engine"]).Engine._strategy_of
    assert s(Candidate(chain="solana", mint="M", source="copy", copied_from="W")) == "copy:W"
    assert s(Candidate(chain="solana", mint="M", source="call", trigger="call:Alpha")) == "call:Alpha"
    assert s(Candidate(chain="solana", mint="M", source="pumpfun", trigger="dev")) == "snipe:dev"
    assert s(Candidate(chain="solana", mint="M", source="pumpfun")) == "snipe"
    assert s(Candidate(chain="solana", mint="M", source="manual")) == "manual"


async def test_overview_by_period_strategy_recent_and_best_worst(tmp_path):
    h = Harness(tmp_path)
    st = h.eng.store
    _close(st, "OLD", -0.5, ago=3 * 86400)                      # outside 24h, inside 7 days
    _close(st, "WIN", 0.08, source="copy", copied_from=W, strategy=f"copy:{W}",
           reason="copied wallet sold (50% of its bag)", held_s=840)
    _close(st, "LOSS", -0.02, reason="stop loss (-40%)", held_s=180)
    _close(st, "CALL", 0.01, source="call", strategy="call:Alpha")
    st.event("buy", "MWIN", "WIN", sol=0.1)
    st.event("sell", "MWIN", "WIN", sol=0.18)
    await h.eng.add_copy_wallet(W, "Cupsey", 0.05)

    await h.tap("sv:24h")
    t = h.last
    assert "Last 24 hours" in t and "+0.0700 SOL" in t and "3 trades" in t and "67% won" in t
    assert "OLD" not in t
    assert t.index("👥 Cupsey") < t.index("📣 Alpha") < t.index("🎯 Sniping")   # best first
    assert "WIN +0.0800 (+80%) · 👥 Cupsey · sold with them · 14m" in t
    assert "LOSS -0.0200 (-20%) · 🎯 Sniping · stop loss · 3m" in t
    assert "🏆 WIN +0.0800" in t and "💀 LOSS -0.0200" in t
    assert "Costs ≈" in t
    assert "tc:MWIN" in h.buttons() and "sc:24h" in h.buttons() and "✅ 24h" in str(h.sent[-1][1])
    await h.tap("sv:7d")
    assert "4 trades" in h.last and "OLD" in h.last and "-0.4300" in h.last
    await h.tap("sv:24h")
    await h.close()


def test_costs_estimate_counts_every_transaction(tmp_path):
    from sniper.config import load_config
    from sniper.store import Store
    st = Store(str(tmp_path))
    cfg = load_config("config.example.yaml")
    cfg.speed.jito_enabled, cfg.speed.jito_tip_sol = True, 0.001
    cfg.trading.priority_fee_sol = 0.0005
    st.event("buy", "M", "X", sol=0.1)
    st.event("sell", "M", "X", sol=0.15)
    c = report.costs(st, 0, cfg)
    assert c["txs"] == 2
    assert c["fees"] == pytest.approx(2 * (0.000005 + 0.0005 + cfg.speed.tip_sol()))
    assert c["trading"] == pytest.approx(0.25 * report.PUMP_FEES)


async def test_daily_recap_arrives_at_your_midnight(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    eng.cfg.notify.utc_offset_hours = -5
    sent = []

    async def capture(text, *a, **k):
        sent.append(text)
    eng.notifier.send = capture
    day1 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    assert not await eng.daily_report(day1)
    _close(eng.store, "LATE", 0.03)       # 23:00 local (04:00 UTC next day): still Oct 5
    eng.store.db.execute("UPDATE events SET ts = ?",
                         (datetime(2026, 10, 6, 4, tzinfo=timezone.utc).timestamp(),))
    # 00:05 UTC Oct 6 is still Oct 5 evening for you: no recap yet
    assert not await eng.daily_report(datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc).timestamp())
    assert await eng.daily_report(datetime(2026, 10, 6, 5, 5, tzinfo=timezone.utc).timestamp())
    assert "Daily recap</b> 2026-10-05" in sent[-1] and "LATE" in sent[-1]
    await h.close()


async def test_after_you_sold_checks_the_price_later(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    p = Position(mint="MX", symbol="ZOOM", source="pumpfun", creator=None, entry_price=1e-8,
                 tokens_initial=1, tokens_remaining=0, sol_in=0.1, sol_out=0.15)
    p.last_price, p.closed, p.close_reason = 2e-8, True, "take profit +50%"
    await eng._closed(p)
    prices = iter([3e-8, 1e-8])

    async def price_of(mint):
        return next(prices)
    eng.price_of = price_of
    t0 = time.time()
    await eng.after_sell_tick(t0 + 60)
    assert eng.store.after_sell_rows(0)[0][3] is None          # not due yet
    await eng.after_sell_tick(t0 + 901)
    await eng.after_sell_tick(t0 + 3601)
    row = eng.store.after_sell_rows(0)[0]
    assert row[3] == 3e-8 and row[4] == 1e-8
    await h.tap("sa:7d")
    assert any("ZOOM: 15m +50% · 1h -50%" in t for t, *_ in h.sent)
    await h.close()


async def test_you_vs_the_wallet_on_the_same_coins(tmp_path):
    h = Harness(tmp_path)
    st = h.eng.store
    await h.eng.add_copy_wallet(W, "Cupsey", 0.05)
    _close(st, "PEPE", 0.01, source="copy", copied_from=W, strategy=f"copy:{W}")
    st.add_wallet_trade(W, "b1", "MPEPE", time.time() - 600, "buy", 1.0, 1000)
    st.add_wallet_trade(W, "s1", "MPEPE", time.time() - 60, "sell", 2.0, 1000)
    _close(st, "HOLD", -0.02, source="copy", copied_from=W, strategy=f"copy:{W}")
    st.add_wallet_trade(W, "b2", "MHOLD", time.time() - 600, "buy", 1.0, 1000)
    await h.tap("sw:7d")
    t = h.last
    assert "<b>Cupsey</b>: 2 copies" in t
    assert "PEPE you +10% · them +100%" in t and "HOLD you -20% · them: still in" in t
    assert "they do much better" in t
    await h.close()


async def test_chart_and_details_views(tmp_path):
    h = Harness(tmp_path)
    _close(h.eng.store, "A", 0.02)
    _close(h.eng.store, "B", -0.01)
    photos = []

    async def api_files(method, files, **params):
        photos.append((method, files["photo"][1][:4], params["caption"]))
        return {"message_id": 1}
    h.tg.api_files = api_files
    await h.tap("sc:7d")
    assert photos and photos[0][0] == "sendPhoto" and photos[0][1] == b"\x89PNG"
    assert "+0.0100 SOL over 2 trades" in photos[0][2]
    await h.tap("sd:7d")
    assert "details" in h.last and "Trades: 2" in h.last
    pts = report.cumulative(report.closed_trades(h.eng.store, 0), 0)
    assert pts[-1][1] == pytest.approx(0.01)
    await h.close()
