"""Rug protection: skip launches whose supply sits with a few wallets or the dev's bundle,
and sell at once when a big holder dumps on us."""
import time

from sniper import exits
from sniper.config import EntryConfig, ExitConfig
from sniper.intel import CURVE_START_TOKENS, EarlyFlow
from sniper.models import Position

M = 1_000_000  # tokens


def _buy(w, sol, tokens, balance=None, v_tokens=None):
    m = {"txType": "buy", "traderPublicKey": w, "solAmount": sol, "tokenAmount": tokens}
    if balance is not None:
        m["newTokenBalance"] = balance
    if v_tokens:
        m["vTokensInBondingCurve"] = v_tokens
    return m


def _clean(cfg, flow):
    return [p for p in flow.evaluate(cfg) if "top 10" in p or "launch" in p]


def test_a_spread_out_launch_passes():
    cfg = EntryConfig(min_unique_buyers=0)
    f = EarlyFlow(creator="DEV", dev_tokens=20 * M, v_tokens=CURVE_START_TOKENS - 20 * M)
    f.started -= 5                                   # these come after the launch second
    out = 20 * M
    for i in range(20):
        out += 5 * M
        f.add(_buy(f"W{i}", 0.2, 5 * M, 5 * M, CURVE_START_TOKENS - out))
    assert _clean(cfg, f) == []                      # top 10 = dev 2% + 10 x 0.5%


def test_a_few_wallets_holding_the_supply_is_skipped():
    cfg = EntryConfig(min_unique_buyers=0, max_top_holders_pct=35)
    f = EarlyFlow(creator="DEV", dev_tokens=60 * M)
    f.started -= 5
    for i in range(4):
        f.add(_buy(f"W{i}", 3.0, 80 * M, 80 * M))
    assert any("top 10 wallets hold 38%" in p for p in f.evaluate(cfg))


def test_balances_without_newTokenBalance_are_added_up():
    f = EarlyFlow(creator="DEV")
    f.add(_buy("A", 1, 10 * M))
    f.add(_buy("A", 1, 5 * M))
    f.add({"txType": "sell", "traderPublicKey": "A", "solAmount": 0.5, "tokenAmount": 3 * M})
    assert f.holdings["A"] == 12 * M


def test_wallets_buying_in_the_launch_second_are_a_bundle():
    cfg = EntryConfig(min_unique_buyers=0, max_top_holders_pct=0, max_launch_bundle_pct=25)
    f = EarlyFlow(creator="DEV")
    for i in range(6):
        f.add(_buy(f"B{i}", 1.5, 50 * M, 50 * M))   # all right at launch
    assert any("bundled launch" in p and "30%" in p for p in f.evaluate(cfg))
    cfg.max_launch_bundle_pct = 0
    assert _clean(cfg, f) == []


def test_tokens_bought_before_we_could_watch_count_as_the_bundle():
    cfg = EntryConfig(min_unique_buyers=0, max_top_holders_pct=0, max_launch_bundle_pct=25)
    # dev 5%; the curve already lost 40% more in the create block, to wallets we never saw
    f = EarlyFlow(creator="DEV", dev_tokens=50 * M, v_tokens=CURVE_START_TOKENS - 450 * M)
    f.started -= 5
    f.add(_buy("LATE", 0.1, 1 * M, 1 * M, CURVE_START_TOKENS - 451 * M))
    assert any("hold 40%" in p for p in f.evaluate(cfg))


def _pos():
    return Position(mint="M", symbol="X", source="t", creator="DEV", entry_price=1e-8,
                    tokens_initial=1e6, tokens_remaining=1e6, sol_in=0.03)


def _sell(w, tokens, left):
    return {"txType": "sell", "traderPublicKey": w, "solAmount": 1.0, "tokenAmount": tokens,
            "newTokenBalance": left}


def test_a_big_holder_dumping_sells_us_out():
    cfg = ExitConfig(exit_on_whale_sell_pct=4)
    p = _pos()
    exits.record_trade(p, _sell("SMALL", 10 * M, 0), set())          # 1% holder: ignored
    exits.record_trade(p, _sell("TRIM", 20 * M, 40 * M), set())      # 6% holder trims a third
    assert exits.evaluate(p, cfg, time.time()) is None
    exits.record_trade(p, _sell("WHALE", 40 * M, 10 * M), set())     # 5% holder dumps 80%
    d = exits.evaluate(p, cfg, time.time())
    assert d and d.sell_all and "big holder dumped (5% of supply)" in d.reason
    assert exits.evaluate(p, ExitConfig(exit_on_whale_sell_pct=0), time.time()) is None \
        or "big holder" not in exits.evaluate(p, ExitConfig(exit_on_whale_sell_pct=0)).reason


def test_our_own_sells_never_count_as_a_whale():
    p = _pos()
    exits.record_trade(p, _sell("ME", 80 * M, 0), set(), own_wallet="ME")
    assert p.whale_dump_pct == 0
