import json
import time

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper import exits
from sniper.config import ExitConfig, FilterConfig, load_config
from sniper.engine import Engine
from sniper.execution.executors import CurveState
from sniper.execution.wallet import load_keypair, new_keypair
from sniper.models import Candidate, Position
from sniper.safety import check_mint, concentration_pct, static_checks
from sniper.scanners.multichain import parse_gecko_pools
from sniper.scanners.pumpportal import candidate_from_create, trade_price
from sniper.solana_rpc import balance_deltas
from sniper.models import SafetyReport

CREATOR = str(Keypair().pubkey())
KOL = str(Keypair().pubkey())


def pos(**kw) -> Position:
    base = dict(mint="M", symbol="T", source="pumpfun", creator=CREATOR, entry_price=1.0,
                tokens_initial=1000, tokens_remaining=1000, sol_in=1.0)
    base.update(kw)
    return Position(**base)


def trade(side, trader="x", sol=0.1):
    return {"txType": side, "traderPublicKey": trader, "solAmount": sol}


# ---------- exits ----------

def test_no_exit_when_flat():
    assert exits.evaluate(pos(), ExitConfig()) is None


def test_stop_loss():
    p = pos(); p.update_price(0.7)
    d = exits.evaluate(p, ExitConfig(stop_loss_pct=25))
    assert d.sell_all and "stop loss" in d.reason


def test_dev_sell_dumps_everything():
    p = pos()
    exits.record_trade(p, trade("sell", CREATOR), set())
    d = exits.evaluate(p, ExitConfig())
    assert d.sell_all and d.reason == "dev sold"


def test_own_trades_ignored():
    p = pos(creator="me")
    exits.record_trade(p, trade("sell", "me"), set(), own_wallet="me")
    assert not p.dev_sold and not p.recent_trades


def test_take_profit_ladder_folds_levels():
    cfg = ExitConfig()  # 40/40, 100/30, 250/20
    p = pos(); p.update_price(2.1)  # +110%: crosses first two levels at once
    d = exits.evaluate(p, cfg)
    assert not d.sell_all and d.tokens == pytest.approx(700) and d.tp_index == 1
    exits.apply_fill(p, d, d.tokens, 1.4, cfg)
    assert p.tokens_remaining == pytest.approx(300) and p.tp_levels_hit == {0, 1}
    assert exits.evaluate(p, cfg) is None  # still +110%, nothing new
    p.update_price(3.6)
    d = exits.evaluate(p, cfg)
    assert d.tokens == pytest.approx(200) and not d.sell_all


def test_partial_leaving_dust_becomes_full_exit():
    p = pos(tokens_remaining=30)
    d = exits._partial(p, 20, "x")
    assert d.sell_all and d.tokens == 30


def test_trailing_stop_arms_then_fires():
    cfg = ExitConfig(trailing_activate_pct=30, trailing_stop_pct=20, take_profit=[])
    p = pos(); p.update_price(1.2); p.update_price(1.0)
    assert exits.evaluate(p, cfg) is None  # never armed
    p.update_price(1.5); p.update_price(1.15)  # 23% off a +50% peak
    d = exits.evaluate(p, cfg)
    assert d.sell_all and "trailing" in d.reason


def test_sell_pressure():
    cfg = ExitConfig(sell_pressure_window=10, sell_pressure_min_trades=10, sell_pressure_ratio=0.7)
    p = pos()
    for i in range(10):
        exits.record_trade(p, trade("sell" if i < 8 else "buy"), set())
    assert "sell pressure" in exits.evaluate(p, cfg).reason


def test_kol_buy_sells_half_once():
    cfg = ExitConfig(kol_buy_sell_pct=50, take_profit=[])
    p = pos()
    exits.record_trade(p, trade("buy", KOL), {KOL})
    d = exits.evaluate(p, cfg)
    assert d.tokens == pytest.approx(500) and d.reason.startswith("KOL")
    exits.apply_fill(p, d, 500, 0.6, cfg)
    assert exits.evaluate(p, cfg) is None


def test_time_exits():
    cfg = ExitConfig(max_hold_seconds=60, stale_seconds=1000)
    p = pos(opened_at=time.time() - 61)
    assert exits.evaluate(p, cfg).reason == "max hold time"
    cfg = ExitConfig(max_hold_seconds=1000, stale_seconds=30)
    p = pos(); p.last_update = time.time() - 31
    assert "dead" in exits.evaluate(p, cfg).reason


# ---------- safety ----------

def test_static_rejects_fat_dev_buy():
    c = Candidate(chain="solana", mint="M", source="pumpfun", creator_initial_buy_tokens=120_000_000)
    r = static_checks(c, FilterConfig(max_creator_initial_buy_pct=8))
    assert not r.passed and "dev bought" in r.reasons[0]


def test_static_liquidity_and_name():
    c = Candidate(chain="base", mint="M", source="geckoterminal", liquidity_usd=500, name="Test coin")
    r = static_checks(c, FilterConfig())
    assert len(r.reasons) == 2


def test_check_mint_authorities_and_extensions():
    r = SafetyReport(passed=True)
    check_mint({"mintAuthority": "abc", "freezeAuthority": None,
                "extensions": [{"extension": "permanentDelegate"}]}, FilterConfig(), r)
    assert not r.passed and len(r.reasons) == 2


def test_concentration_skips_pda_owners():
    wallet = str(Keypair().pubkey())
    pda, _ = Pubkey.find_program_address([b"curve"], Pubkey.from_string("11111111111111111111111111111111"))
    largest = [{"address": "a", "uiAmount": 800}, {"address": "b", "uiAmount": 50}]
    owners = [{"data": {"parsed": {"info": {"owner": str(pda)}}}},
              {"data": {"parsed": {"info": {"owner": wallet}}}}]
    assert concentration_pct(largest, owners, 1000) == pytest.approx(5.0)


# ---------- parsing / math ----------

CREATE_MSG = {"signature": "s", "mint": "Mint1pump", "traderPublicKey": CREATOR, "txType": "create",
              "initialBuy": 30_000_000, "solAmount": 0.9, "bondingCurveKey": "curve",
              "vTokensInBondingCurve": 1_043_000_000, "vSolInBondingCurve": 30.86,
              "marketCapSol": 29.6, "name": "Frog", "symbol": "FROG", "uri": "x", "pool": "pump"}


def test_pumpportal_create_and_price():
    c = candidate_from_create(CREATE_MSG)
    assert c.source == "pumpfun" and c.creator == CREATOR and c.symbol == "FROG"
    assert trade_price(CREATE_MSG) == pytest.approx(30.86 / 1_043_000_000)
    assert trade_price({"solAmount": 1, "tokenAmount": 4}) == 0.25


def test_curve_roundtrip_loses_only_fees():
    curve = CurveState(30, 1_073_000_000)
    tokens = curve.buy_out(1.0)
    after = CurveState(30 + 1.0 * (1 - 0.0125), curve.v_tokens - tokens)
    back = after.sell_out(tokens)
    assert 0.97 < back < 0.98


def test_gecko_parse():
    payload = {"data": [{"attributes": {"name": "DOG / SOL", "address": "pool1",
                                        "pool_created_at": "2026-10-04T12:00:00Z",
                                        "reserve_in_usd": "12345.6", "fdv_usd": "99000"},
                         "relationships": {"base_token": {"data": {"id": "base_0xabc"}}}}]}
    [c] = parse_gecko_pools("base", payload)
    assert (c.chain, c.mint, c.symbol, c.liquidity_usd) == ("base", "0xabc", "DOG", 12345.6)


def test_balance_deltas():
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "me"}, {"pubkey": "x"}]}},
          "meta": {"preBalances": [2_000_000_000, 0], "postBalances": [1_899_000_000, 0],
                   "preTokenBalances": [],
                   "postTokenBalances": [{"mint": "M", "owner": "me", "uiTokenAmount": {"uiAmount": 1234.5}}]}}
    assert balance_deltas(tx, "me", "M") == (pytest.approx(1234.5), pytest.approx(-0.101))


def test_wallet_roundtrip():
    pub, secret = new_keypair()
    assert str(load_keypair(secret).pubkey()) == pub
    kp = Keypair()
    assert load_keypair(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()


def test_example_config_loads(tmp_path):
    cfg = load_config("config.example.yaml")
    assert cfg.trading.buy_amount_sol == 0.05 and len(cfg.exits.take_profit) == 3


# ---------- end-to-end paper flow (no network) ----------

async def test_paper_engine_snipe_and_exit(tmp_path):
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "test-key"  # trade stream on
    eng = Engine(cfg, live=False)
    sent = []
    async def no_ws(payload):
        sent.append(payload)
    eng.stream._send = no_ws

    await eng.stream._dispatch(json.dumps(CREATE_MSG))
    c = eng.queue.get_nowait()
    await eng.handle_candidate(c)
    p = eng.positions["Mint1pump"]
    assert p.sol_in == 0.05 and p.tokens_initial > 0
    assert {"method": "subscribeTokenTrade", "keys": ["Mint1pump"]} in sent

    # price pumps +60% -> first take-profit
    v_sol = 30.86 * 1.6 ** 0.5
    v_tok = 30.86 * 1_043_000_000 / v_sol
    await eng.on_trade({"mint": "Mint1pump", "txType": "buy", "traderPublicKey": "anon",
                        "solAmount": 5, "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tok})
    await eng.settle()  # exits run in the background
    assert p.tp_levels_hit == {0} and 0 < p.tokens_remaining < p.tokens_initial

    # dev dumps -> everything out
    await eng.on_trade({"mint": "Mint1pump", "txType": "sell", "traderPublicKey": CREATOR,
                        "solAmount": 1, "vSolInBondingCurve": v_sol - 1,
                        "vTokensInBondingCurve": v_tok * 1.03})
    await eng.settle()
    assert p.closed and p.close_reason == "dev sold"
    assert p.sol_out > p.sol_in  # took profit before the rug
    assert [e["event"] for e in eng.store.events()] == ["buy", "sell", "sell", "close"]
    assert eng.store.is_blocked(CREATOR) is None  # profitable, so no auto-block
    await eng.http.aclose()
