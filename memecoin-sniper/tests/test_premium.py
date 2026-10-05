import asyncio
import base64
import json
import time

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from sniper import exits
from sniper.config import EntryConfig, ExitConfig, FilterConfig, SpeedConfig, load_config
from sniper.engine import Engine
from sniper.execution.sender import JITO_TIP_ACCOUNTS, TxSender, fee_from_samples, tip_transaction
from sniper.intel import EarlyFlow, extract_socials, normalize_social
from sniper.models import Candidate, Position, SafetyReport
from sniper.safety import SafetyChecker
from sniper.solana_rpc import SolanaRpc
from sniper.stats import format_summary, summarize
from sniper.store import Store
from sniper.telegram_bot import TelegramControl

DEV = str(Keypair().pubkey())
WHALE = str(Keypair().pubkey())


def wallet():
    return str(Keypair().pubkey())


# ---------- speed ----------

def test_fee_percentile():
    assert fee_from_samples([0, 0], 75) == 0
    # 100k micro-lamports/CU * 200k CU = 2e10 micro-lamports = 20k lamports
    assert fee_from_samples([10, 100_000, 50], 100) == pytest.approx(0.00002)
    assert fee_from_samples([1, 2, 3, 4, 5], 50) == pytest.approx(3 * 0.2 / 1e9)


def test_tip_transaction_is_signed_transfer_to_jito():
    kp = Keypair()
    tx = tip_transaction(kp, 0.001, Hash.default())
    assert tx.message.recent_blockhash == Hash.default()
    keys = [str(k) for k in tx.message.account_keys]
    assert keys[0] == str(kp.pubkey()) and any(k in JITO_TIP_ACCOUNTS for k in keys)
    assert tx.verify_with_results() == [True]


def _signed_tx(kp: Keypair) -> VersionedTransaction:
    return tip_transaction(kp, 0.0001, Hash.new_unique())


async def test_sender_bundles_swap_and_tip():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((str(req.url), body))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = SolanaRpc("https://rpc.example", http)
    kp = Keypair()
    swap = _signed_tx(kp)
    cfg = SpeedConfig(jito_enabled=True, auto_priority_fee=False,
                      jito_block_engines=["https://a.jito", "https://b.jito"])
    sig = await TxSender(cfg, rpc, http, 0.001).send(swap, kp)
    assert sig == str(swap.signatures[0])
    assert sorted(u for u, _ in calls) == ["https://a.jito/api/v1/bundles", "https://b.jito/api/v1/bundles"]
    bundle = calls[0][1]["params"][0]
    assert len(bundle) == 2 and base64.b64decode(bundle[0]) == bytes(swap)
    tip = VersionedTransaction.from_bytes(base64.b64decode(bundle[1]))
    assert tip.message.recent_blockhash == swap.message.recent_blockhash  # same block window


async def test_sender_broadcasts_to_all_rpcs_without_jito():
    hits = []

    def handler(req: httpx.Request):
        hits.append(req.url.host)
        if req.url.host == "bad.rpc":
            return httpx.Response(500)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "sig"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = SpeedConfig(jito_enabled=False, broadcast_rpcs=["https://bad.rpc", "https://b.rpc"])
    kp = Keypair()
    await TxSender(cfg, SolanaRpc("https://a.rpc", http), http, 0.001).send(_signed_tx(kp), kp)
    assert sorted(hits) == ["a.rpc", "b.rpc", "bad.rpc"]  # one failure is tolerated


# ---------- intel ----------

def test_socials_normalized():
    assert normalize_social("https://X.com/Frog/?s=20") == "twitter.com/frog"
    assert normalize_social("https://www.t.me/frogchat") == "t.me/frogchat"
    meta = {"twitter": "https://x.com/frog", "telegram": "", "website": "frog.io"}
    assert extract_socials(meta) == ["twitter.com/frog", "frog.io"]


def flow_with(buys, sells=(), creator=DEV):
    f = EarlyFlow(creator=creator)
    for w, sol in buys:
        f.add({"txType": "buy", "traderPublicKey": w, "solAmount": sol, "marketCapSol": 40})
    for w, sol in sells:
        f.add({"txType": "sell", "traderPublicKey": w, "solAmount": sol})
    return f


def test_early_flow_healthy_passes():
    f = flow_with([(wallet(), s) for s in (0.3, 0.5, 0.21, 1.1, 0.7, 0.42)])
    assert f.evaluate(EntryConfig(min_unique_buyers=5)) == []


def test_early_flow_detects_bundle():
    f = flow_with([(wallet(), 0.5) for _ in range(4)] + [(wallet(), 0.3), (wallet(), 0.9)])
    assert any("bundle" in p for p in f.evaluate(EntryConfig()))


def test_early_flow_whale_dev_dump_and_thin():
    big = flow_with([(WHALE, 5.0)] + [(wallet(), 0.1 + i / 100) for i in range(5)])
    assert any("one wallet" in p for p in big.evaluate(EntryConfig()))
    dump = flow_with([(wallet(), 0.2 + i / 10) for i in range(6)], sells=[(DEV, 1.0)])
    assert "dev sold during confirmation window" in dump.evaluate(EntryConfig())
    thin = flow_with([(wallet(), 0.3)])
    assert any("unique buyers" in p for p in thin.evaluate(EntryConfig()))


# ---------- store / reputation ----------

def test_store_reputation_socials_positions(tmp_path):
    st = Store(str(tmp_path), "paper")
    for i in range(3):
        st.record_launch(f"m{i}", DEV)
    assert st.launches_since(DEV, time.time() - 60, exclude_mint="m0") == 2
    assert st.record_socials("m1", ["twitter.com/frog"]) == []
    assert st.record_socials("m2", ["twitter.com/frog", "frog.io"]) == ["twitter.com/frog"]
    st.block(DEV, "rugged")
    assert st.is_blocked(DEV) == "rugged"

    p = Position(mint="M", symbol="T", source="copy", creator=None, entry_price=1.0,
                 tokens_initial=10, tokens_remaining=5, sol_in=1, route="pump", leader=WHALE)
    p.tp_levels_hit.add(0)
    st.save_position(p)
    [back] = Store(str(tmp_path), "paper").open_positions()
    assert (back.route, back.leader, back.tp_levels_hit, back.tokens_remaining) == ("pump", WHALE, {0}, 5)


async def test_safety_serial_launcher_and_blocklist(tmp_path):
    st = Store(str(tmp_path), "paper")
    for i in range(4):
        st.record_launch(f"old{i}", DEV)
    chk = SafetyChecker(FilterConfig(max_creator_launches_24h=3), None, None, "", store=st)
    c = Candidate(chain="solana", mint="new", source="pumpfun", creator=DEV)
    r = await chk.evaluate(c)
    assert not r.passed and "serial launcher" in r.reasons[0]
    st.block(DEV, "dumped")
    r = await chk.evaluate(c)
    assert "blocklisted" in r.reasons[0]


async def test_safety_socials_required_and_reused(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"name": "Frog", "twitter": "https://x.com/frog"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    st = Store(str(tmp_path), "paper")
    chk = SafetyChecker(FilterConfig(min_socials=1), None, http, "", store=st)
    c1 = Candidate(chain="solana", mint="a", source="pumpfun", creator=wallet(), uri="https://ipfs.io/ipfs/1")
    assert (await chk.evaluate(c1)).passed
    c2 = Candidate(chain="solana", mint="b", source="pumpfun", creator=wallet(), uri="https://ipfs.io/ipfs/2")
    r = await chk.evaluate(c2)
    assert not r.passed and "reused" in r.reasons[0]
    chk.cfg.min_socials = 2
    c3 = Candidate(chain="solana", mint="c", source="pumpfun", creator=wallet(), uri="https://ipfs.io/ipfs/3")
    assert "socials (< 2)" in (await chk.evaluate(c3)).reasons[0]


class FakeJupiter:
    def __init__(self, back_ratio):
        self.back_ratio = back_ratio

    async def quote(self, i, o, amount, slip):
        return {"in": i, "amount": amount}

    async def out_ui(self, q):
        if q["in"].startswith("So111"):
            return 1000.0
        return 0.05 * self.back_ratio


async def test_honeypot_roundtrip():
    chk = SafetyChecker(FilterConfig(), None, None, "", jupiter=FakeJupiter(0.5), probe_sol=0.05)
    r = SafetyReport(passed=True)
    await chk._honeypot(Candidate(chain="solana", mint="M", source="dexscreener"), r)
    assert not r.passed and "round-trip loses 50%" in r.reasons[0]
    chk.jupiter = FakeJupiter(0.93)
    r = SafetyReport(passed=True)
    await chk._honeypot(Candidate(chain="solana", mint="M", source="dexscreener"), r)
    assert r.passed


# ---------- exits ----------

def test_breakeven_and_leader_exit():
    cfg = ExitConfig(take_profit=[], breakeven_after_first_tp=True)
    p = Position(mint="M", symbol="T", source="copy", creator=None, entry_price=1.0,
                 tokens_initial=100, tokens_remaining=60, sol_in=1, leader=WHALE)
    p.update_price(0.99)
    assert exits.evaluate(p, cfg) is None          # no TP yet: normal stop applies
    p.tp_levels_hit.add(0)
    assert exits.evaluate(p, cfg).reason == "breakeven stop"
    p.update_price(1.2)
    exits.record_trade(p, {"txType": "sell", "traderPublicKey": WHALE, "solAmount": 3}, set())
    assert exits.evaluate(p, cfg).reason == "copied wallet sold"


# ---------- config presets ----------

def test_presets(tmp_path):
    safe = load_config(None, preset="safe")
    assert safe.entry.confirm_seconds == 6 and safe.filters.min_socials == 1
    assert [lvl.at_pct for lvl in safe.exits.take_profit] == [25, 60]
    f = tmp_path / "c.yaml"
    f.write_text("preset: degen\nexits:\n  stop_loss_pct: 10\n")
    cfg = load_config(f)
    assert cfg.preset == "degen" and cfg.exits.stop_loss_pct == 10
    assert cfg.exits.trailing_stop_pct == 30  # still from preset
    with pytest.raises(ValueError):
        load_config(None, preset="yolo")


# ---------- engine: copy trading, confirmation, restore, telegram ----------

def make_engine(tmp_path, **cfg_over) -> Engine:
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.filters.reject_reused_socials = False
    cfg.pumpportal_api_key = cfg_over.pop("api_key", "test-key")
    for section, values in cfg_over.items():
        for k, v in values.items():
            setattr(getattr(cfg, section), k, v)
    eng = Engine(cfg, live=False)

    async def no_ws(payload):
        eng.sent.append(payload)
    eng.sent = []
    eng.stream._send = no_ws
    return eng


def pump_trade(mint, side, trader, sol, v_sol, sig=None):
    return {"mint": mint, "txType": side, "traderPublicKey": trader, "solAmount": sol,
            "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": 32.19e9 / v_sol,
            "signature": sig or f"{mint}-{trader}-{side}-{v_sol}"}


async def test_copy_trade_follows_buy_and_sell(tmp_path):
    eng = make_engine(tmp_path, copytrade={"enabled": True, "run_safety_checks": False})
    await eng.add_copy_wallet(WHALE, "whale", 0.1)
    assert {"method": "subscribeAccountTrade", "keys": [WHALE]} in eng.sent

    await eng.on_trade(pump_trade("CopyMint", "buy", WHALE, 2.0, 32.0))
    await eng.settle()
    p = eng.positions["CopyMint"]
    assert 0.1 < p.sol_in < 0.103 and p.leader == WHALE and p.route == "pump"  # + costs

    # duplicate delivery (token + account subscription) is ignored
    msg = pump_trade("CopyMint", "buy", wallet(), 0.5, 33.0, sig="dup")
    await eng.on_trade(msg)
    await eng.on_trade(msg)
    await eng.settle()
    assert len(p.recent_trades) == 1

    await eng.on_trade(pump_trade("CopyMint", "sell", WHALE, 2.0, 31.0))
    await eng.settle()
    assert p.closed and p.close_reason == "copied wallet sold"
    await eng.http.aclose()


async def test_copy_ignores_dust(tmp_path):
    eng = make_engine(tmp_path, copytrade={"enabled": True, "run_safety_checks": False,
                                           "min_leader_buy_sol": 0.5})
    await eng.add_copy_wallet(WHALE)
    await eng.on_trade(pump_trade("Dust", "buy", WHALE, 0.01, 30.0))
    await eng.settle()
    assert "Dust" not in eng.positions
    await eng.http.aclose()


CREATE = {"mint": "NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump", "traderPublicKey": DEV, "txType": "create", "initialBuy": 20_000_000,
          "bondingCurveKey": "c", "vTokensInBondingCurve": 1_053_000_000, "vSolInBondingCurve": 30.57,
          "name": "Frog", "symbol": "FROG", "uri": "", "signature": "create"}


async def test_confirmation_window_rejects_bundle_and_accepts_organic(tmp_path):
    eng = make_engine(tmp_path, entry={"confirm_seconds": 0.05, "min_unique_buyers": 3})
    await eng.stream._dispatch(json.dumps(CREATE))
    c = eng.queue.get_nowait()
    task = asyncio.create_task(eng.handle_candidate(c))
    await asyncio.sleep(0.01)
    for i in range(4):
        await eng.on_trade(pump_trade("NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump", "buy", wallet(), 0.5, 31 + i))
    result = await task
    assert "bundle" in result and "NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump" not in eng.positions
    assert {"method": "unsubscribeTokenTrade", "keys": ["NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"]} in eng.sent

    organic = dict(CREATE, mint="Organicxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump", signature="c2", traderPublicKey=wallet())
    await eng.stream._dispatch(json.dumps(organic))
    c = eng.queue.get_nowait()
    task = asyncio.create_task(eng.handle_candidate(c))
    await asyncio.sleep(0.01)
    for i, sol in enumerate((0.3, 0.55, 0.45, 0.62)):
        await eng.on_trade(pump_trade("Organicxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump", "buy", wallet(), sol, 31 + i))
    assert (await task).startswith("🟢")
    await eng.http.aclose()


async def test_restore_after_restart_and_telegram_controls(tmp_path):
    eng = make_engine(tmp_path)
    await eng.stream._dispatch(json.dumps(CREATE))
    await eng.handle_candidate(eng.queue.get_nowait())
    assert "NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump" in eng.positions
    await eng.http.aclose()

    eng2 = make_engine(tmp_path)
    await eng2.restore()
    p = eng2.positions["NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"]
    assert not p.closed and {"method": "subscribeTokenTrade", "keys": ["NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"]} in eng2.sent

    replies = []
    tg = TelegramControl(eng2, "token", "42", eng2.http)

    async def fake_telegram(text, buttons=None, chat_id=None):
        replies.append((text, buttons))
    eng2.notifier.telegram = fake_telegram

    await tg.handle_update({"message": {"chat": {"id": 999}, "text": "/pause"}})
    assert not eng2.paused  # strangers are ignored
    await tg.handle_update({"message": {"chat": {"id": 42}, "text": "/pause"}})
    assert eng2.paused and eng2.store.get_setting("paused") == "1"
    await tg.handle_update({"message": {"chat": {"id": 42}, "text": "/positions"}})
    assert replies[-1][1][0][2] == ("Sell 100%", "s:NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump:100")

    eng2.curves["NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"] = eng2.curves.get("NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump") or __import__(
        "sniper.execution.executors", fromlist=["CurveState"]).CurveState(31.0, 1.04e9)
    await tg.handle_update({"callback_query": {"id": "1", "data": "s:NewPumpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump:50",
                                               "message": {"chat": {"id": 42}}}})
    await eng2.settle()
    assert 0 < p.tokens_remaining < p.tokens_initial
    await tg.handle_update({"message": {"chat": {"id": 42}, "text": "/sell FROG"}})
    await eng2.settle()
    assert p.closed and p.close_reason == "manual"
    await tg.handle_update({"message": {"chat": {"id": 42}, "text": "/stats"}})
    assert "Trades: 1" in replies[-1][0]
    await eng2.http.aclose()


async def test_stats_summary(tmp_path):
    st = Store(str(tmp_path), "paper")
    for pnl, reason in [(0.05, "take profit +40%"), (-0.02, "stop loss (-26%)"), (0.01, "dev sold")]:
        st.event("close", "m", "T", pnl_sol=pnl, reason=reason, source="pumpfun", held_s=60)
    s = summarize(st)
    assert s["trades"] == 3 and s["win_rate"] == pytest.approx(66.67, rel=1e-3)
    assert s["by_reason"]["stop loss"] == (1, -0.02)
    assert "Win rate: 67%" in format_summary(s)
