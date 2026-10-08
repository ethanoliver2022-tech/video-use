"""Copy trading: mirrored sells, per-wallet settings, no chasing, first buy only,
per-wallet results with auto-pause, and the wallet check."""
import time

import pytest
from solders.keypair import Keypair

import sniper.engine as engine_mod
from sniper import exits, pump_curve
from sniper.config import ExitConfig
from sniper.models import Fill, Position
from sniper.walletcheck import WalletChecker, build_report
from tests.test_telegram import Harness

STD = 1_073_000_000 * 30


def _addr():
    return str(Keypair().pubkey())


def _pos(mode="mirror"):
    return Position(mint="M", symbol="X", source="copy", creator=None, entry_price=1e-8,
                    tokens_initial=1000, tokens_remaining=1000, sol_in=0.1, leader="L",
                    copied_from="L", leader_mode=mode)


def _sell(sold, left, trader="L"):
    return {"txType": "sell", "traderPublicKey": trader, "solAmount": 1, "tokenAmount": sold,
            "newTokenBalance": left}


def test_mirrored_sells_follow_their_percentage():
    cfg = ExitConfig()
    p = _pos()
    exits.record_trade(p, _sell(500, 500), set())               # they sold half
    d = exits.evaluate(p, cfg, time.time())
    assert not d.sell_all and d.tokens == pytest.approx(500) and "50%" in d.reason
    exits.apply_fill(p, d, d.tokens, 0.05, cfg)
    assert p.leader_sell_frac == 0 and exits.evaluate(p, cfg, time.time()) is None
    exits.record_trade(p, _sell(450, 50), set())               # 90% of the rest: they're out
    assert exits.evaluate(p, cfg, time.time()).sell_all

    q = _pos()
    exits.record_trade(q, _sell(50, 50), set())                 # 50%, then 50% of the rest
    exits.record_trade(q, _sell(25, 25), set())                 # before ours landed
    assert exits.evaluate(q, cfg, time.time()).tokens == pytest.approx(750)
    a = _pos("all")
    exits.record_trade(a, _sell(10, 990), set())                # "all": any sell = leave
    assert exits.evaluate(a, cfg, time.time()).sell_all


@pytest.fixture
def h(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.eng.paused = False
    h.curve = pump_curve.CurveInfo(v_sol=32.0, v_tokens=STD / 32, complete=False)

    async def curve(rpc, mint):
        return h.curve
    monkeypatch.setattr(engine_mod, "fetch_curve", curve)
    h.bought = []

    async def buy(c, sol, curve):
        h.bought.append(sol)
        return Fill(tokens=1_000_000.0, sol=sol)
    h.eng.executor.buy = buy
    yield h


def _buy(leader, mint, sol=2.0, price=None):
    price = price or 32.0 / (STD / 32)            # what they paid = the curve price now
    return {"mint": mint, "txType": "buy", "solAmount": sol, "tokenAmount": sol / price,
            "pool": "pump", "traderPublicKey": leader, "marketCapSol": 35.0,
            "vSolInBondingCurve": 32.0, "vTokensInBondingCurve": STD / 32}


async def _wallet(eng, **opts):
    a = _addr()
    await eng.add_copy_wallet(a, "whale", 0.0)
    if opts:
        await eng.update_wallet(a, **opts)
    return eng._copy[a]


async def test_size_as_a_share_of_their_buy_with_a_cap(h):
    w = await _wallet(h.eng, size_pct=5, max_sol=0.08)
    await h.eng.handle_copy(_buy(w.address, _addr(), sol=1.0), w)     # 5% of 1 = 0.05
    await h.eng.handle_copy(_buy(w.address, _addr(), sol=4.0), w)     # 5% of 4 = 0.2 -> 0.08
    assert h.bought == [pytest.approx(0.05), pytest.approx(0.08)]
    await h.close()


async def test_per_wallet_min_buy_market_cap_and_filters(h):
    eng = h.eng
    eng.cfg.copytrade.min_leader_buy_sol = 0.05
    w = await _wallet(eng, min_leader_sol=1.5)
    await eng.handle_copy(_buy(w.address, _addr(), sol=1.0), w)
    assert "min for this wallet is 1.5" in h.last
    w = await eng.update_wallet(w.address, min_leader_sol=None, max_mcap_usd=1000.0)
    eng._sol_usd = (150.0, 1e18)
    await eng.handle_copy(_buy(w.address, _addr()), w)
    assert "over $1,000" in h.last

    async def reject(c):
        from sniper.models import SafetyReport
        return SafetyReport(passed=False, reasons=["filtered"])
    eng.safety.evaluate = reject
    eng.cfg.copytrade.run_safety_checks = True
    w = await eng.update_wallet(w.address, max_mcap_usd=None, filters=False)
    m = _addr()
    await eng.handle_copy(_buy(w.address, m), w)                 # trusted wallet: no filters
    assert m in eng.positions
    await h.close()


async def test_no_chasing_a_pump_after_their_buy(h):
    w = await _wallet(h.eng)
    their = 32.0 / (STD / 32)
    h.curve = pump_curve.CurveInfo(v_sol=45.0, v_tokens=STD / 45, complete=False)   # +98%
    m = _addr()
    await h.eng.handle_copy(_buy(w.address, m, price=their), w)
    assert m not in h.eng.positions and "+98% above the price right after their buy" in h.last
    h.eng.cfg.copytrade.max_chase_pct = 0
    await h.eng.handle_copy(_buy(w.address, m, price=their), w)
    assert m in h.eng.positions
    await h.close()


async def test_first_buy_only(h):
    eng = h.eng
    w = await _wallet(eng)
    m = _addr()
    await eng.handle_copy(_buy(w.address, m), w)
    eng.positions[m].closed = True
    await eng.handle_copy(_buy(w.address, m), w)                 # they re-buy: not again
    assert len(h.bought) == 1
    eng.cfg.copytrade.first_buy_only = False
    await eng.handle_copy(_buy(w.address, m), w)
    assert len(h.bought) == 2
    await h.close()


async def test_losing_wallets_pause_themselves(h):
    eng = h.eng
    eng.cfg.copytrade.pause_after_losses = 2
    w = await _wallet(eng)
    for pnl_out in (0.05, 0.01):        # two losing copies (0.1 in)
        p = Position(mint=_addr(), symbol="L", source="copy", creator=None, entry_price=1e-8,
                     tokens_initial=1, tokens_remaining=0, sol_in=0.1, sol_out=pnl_out,
                     copied_from=w.address)
        p.closed, p.close_reason = True, "stop loss"
        await eng._closed(p)
        await eng.settle()
    r = eng.wallet_results(w.address)
    assert r["n"] == 2 and r["loss_streak"] == 2 and r["pnl"] == pytest.approx(-0.14)
    assert eng._copy[w.address].paused
    assert any("Paused copying" in t and "2 losing copies" in t for t, *_ in h.sent)
    m = _addr()
    await eng.handle_copy(_buy(w.address, m), eng._copy[w.address])
    assert m not in eng.positions
    await eng.update_wallet(w.address, paused=False)
    assert not eng._copy[w.address].paused
    await h.close()


def test_wallet_report_from_its_trades():
    now = time.time()
    rows = [("s1", "A", now - 900, "buy", 1.0, 100), ("s2", "A", now - 300, "sell", 1.6, 100),
            ("s3", "B", now - 800, "buy", 0.5, 50), ("s4", "B", now - 100, "sell", 0.2, 50),
            ("s5", "C", now - 50, "buy", 0.3, 30),
            ("s6", "D", now - 40, "sell", 9.0, 10),          # bought before the window: ignored
            ("s7", "", now - 30, "none", 0, 0)]
    r = build_report("W", rows, 3, now)
    assert (r.closed, r.wins, r.open_coins) == (2, 1, 1)
    assert r.pnl_sol == pytest.approx(0.3) and r.win_rate == 50
    assert "Closed trades: 2" in r.text() and "+0.30 SOL" in r.text()


async def test_wallet_check_reads_the_chain_once_then_keeps_up_from_live_trades(tmp_path):
    from sniper.store import Store
    from tests.test_copywatch import _tx, W
    st = Store(str(tmp_path))
    now = time.time()

    class Rpc:
        calls = 0

        async def call(self, method, params):
            Rpc.calls += 1
            if method == "getSignaturesForAddress":
                return [{"signature": "S2", "blockTime": now - 60, "err": None},
                        {"signature": "S1", "blockTime": now - 3600, "err": None},
                        {"signature": "OLD", "blockTime": now - 5 * 86400, "err": None}]
            return {"S1": _tx(sol_change=-1.0), "S2": _tx(sol_change=1.5, tokens_before=1e6,
                                                           tokens_after=0)}.get(params[0])
    wc = WalletChecker(Rpc(), st)
    rep = await wc.check(W, 3)
    assert rep.closed == 1 and rep.pnl_sol == pytest.approx(0.5)
    first = Rpc.calls
    await wc.check(W, 3)
    assert Rpc.calls == first + 1                    # nothing new to read: one listing call
    wc.record({"signature": "LIVE", "mint": "N", "traderPublicKey": W, "txType": "buy",
               "solAmount": 0.4, "tokenAmount": 10})
    assert (await wc.check(W, 3)).open_coins == 1


async def test_wallet_page_and_its_buttons(h, monkeypatch):
    eng = h.eng
    w = await _wallet(eng)
    a = w.address

    async def no_check(*args, **kw):
        return None
    monkeypatch.setattr(eng.wallet_checker, "start", lambda *x, **k: None)
    await h.tap("c")
    assert f"wp:{a}" in h.buttons()
    await h.tap(f"wp:{a}")
    assert "Their sells: 🔁 mirror" in h.last and f"wpx:{a}" in h.buttons()
    await h.tap(f"wpx:{a}")
    assert eng._copy[a].sells == "all"
    await h.tap(f"wpz:{a}")
    await h.text("5% 0.3")
    assert (eng._copy[a].size_pct, eng._copy[a].max_sol) == (5, 0.3)
    assert "5% of their buy (max 0.3)" in h.last
    await h.tap(f"wpc:{a}")
    await h.text("30k")
    assert eng._copy[a].max_mcap_usd == 30000
    await h.tap(f"wpf:{a}")
    assert eng._copy[a].filters is True
    await h.tap(f"wpp:{a}")
    assert eng._copy[a].paused
    # settings survive a restart (stored per wallet)
    eng._load_copy_wallets()
    assert eng._copy[a].size_pct == 5 and eng._copy[a].sells == "all" and eng._copy[a].paused
    await h.close()


async def test_changing_sells_updates_open_copies(h):
    eng = h.eng
    w = await _wallet(eng)
    m = _addr()
    await eng.handle_copy(_buy(w.address, m), w)
    pos = eng.positions[m]
    assert pos.copied_from == w.address and pos.leader == w.address and pos.leader_mode == "mirror"
    await eng.update_wallet(w.address, sells="off")
    assert pos.leader is None
    await h.close()


async def test_their_own_big_buy_is_not_counted_as_chasing(h):
    """A 10 SOL buy on a young curve lifts the price ~75% by itself: measure from the price
    right after it (PumpPortal reports the curve), not from their average."""
    w = await _wallet(h.eng)
    m = _addr()
    after_sol = 40.0
    h.curve = pump_curve.CurveInfo(v_sol=after_sol, v_tokens=STD / after_sol, complete=False)
    msg = {"mint": m, "txType": "buy", "solAmount": 10.0, "tokenAmount": STD / 30 - STD / 40,
           "pool": "pump", "traderPublicKey": w.address, "marketCapSol": 50.0,
           "vSolInBondingCurve": after_sol, "vTokensInBondingCurve": STD / after_sol}
    await h.eng.handle_copy(msg, w)
    assert m in h.eng.positions
    await h.close()
