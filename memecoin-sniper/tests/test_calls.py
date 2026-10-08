"""Call sniper: CAs posted in watched Telegram groups are bought (or alerted), once."""
import json
import os
import stat

import pytest
from solders.keypair import Keypair

import sniper.engine as engine_mod
from sniper import pump_curve
from sniper.calls import CallGroup, CallTracker, CallWatcher, LoginNeedsPassword, extract_cas
from sniper.config import CallsConfig
from sniper.models import Fill
from tests.test_telegram import Harness

CA = str(Keypair().pubkey())
CA2 = str(Keypair().pubkey())


def test_cas_are_found_in_plain_text_and_links():
    text = (f"🚀 new gem!! CA: {CA}\nchart https://dexscreener.com/solana/{CA}\n"
            f"pump.fun/coin/{CA2} buy with So11111111111111111111111111111111111111112")
    assert extract_cas(text) == [CA, CA2]
    assert extract_cas("no address here, just 1234567890 and lol") == []
    assert extract_cas("x" * 40) == []                      # 'x' isn't the right alphabet... nor valid
    many = " ".join(str(Keypair().pubkey()) for _ in range(5))
    assert extract_cas(many) == []                          # a list of coins isn't a call
    assert extract_cas(f"{CA}abc") == []                    # part of a longer string


def test_a_ca_fires_once_and_multi_group_waits_for_the_second_group(tmp_path):
    from sniper.store import Store
    st = Store(str(tmp_path))
    t = CallTracker(st)
    one = CallsConfig(enabled=True)
    assert t.note(CA, 1, one, now=1000) == 1
    assert t.note(CA, 2, one, now=1001) is None             # already acted on
    assert CallTracker(st).note(CA, 3, one, now=1100) is None  # survives a restart

    two = CallsConfig(enabled=True, min_groups=2, group_window_minutes=10)
    assert t.note(CA2, 1, two, now=1000) is None
    assert t.note(CA2, 1, two, now=1010) is None            # same group again: still one
    assert t.note(CA2, 2, two, now=1000 + 11 * 60) is None  # too late: the first post expired
    assert t.note(CA2, 3, two, now=1000 + 12 * 60) == 2


async def test_only_watched_active_groups_count(tmp_path):
    from sniper.store import Store
    calls = []

    async def on_call(mint, group, n):
        calls.append((mint, group.title, n))
    w = CallWatcher(str(tmp_path), Store(str(tmp_path)), on_call)
    w.groups.upsert(CallGroup(id=-100, title="Alpha"))
    w.groups.upsert(CallGroup(id=-200, title="Paused", on=False))
    cfg = CallsConfig(enabled=True)
    await w.handle_message(-999, f"CA {CA}", cfg)            # not watched
    await w.handle_message(-200, f"CA {CA}", cfg)            # paused group
    await w.handle_message(-100, f"CA {CA}", CallsConfig(enabled=False))
    assert calls == []
    await w.handle_message(-100, f"CA {CA}", cfg)
    assert calls == [(CA, "Alpha", 1)]


async def test_login_is_saved_owner_only(tmp_path):
    from sniper.store import Store

    class FakeSession:
        def save(self):
            return "SESSION-STRING"

    class FakeClient:
        session = FakeSession()

        async def get_me(self):
            return type("Me", (), {"username": "caller", "first_name": "C", "id": 1})()

        async def disconnect(self):
            pass

    async def on_call(*a):
        pass
    w = CallWatcher(str(tmp_path), Store(str(tmp_path)), on_call)
    w._login = {"client": FakeClient(), "api_id": 1, "api_hash": "h" * 32, "phone": "+1"}
    assert await w._logged_in() == "@caller"
    assert w.logged_in and json.loads(w.path.read_text())["session"] == "SESSION-STRING"
    assert stat.S_IMODE(os.stat(w.path).st_mode) == 0o600
    w.logout()
    assert not w.logged_in


def _curve(**kw):
    base = dict(v_sol=32.0, v_tokens=1_073_000_000 * 30 / 32, complete=False,
                creator=str(Keypair().pubkey()))
    base.update(kw)
    return pump_curve.CurveInfo(**base)


@pytest.fixture
def h(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.eng.paused = False

    async def curve(rpc, mint):
        return _curve()
    monkeypatch.setattr(engine_mod, "fetch_curve", curve)

    async def buy(c, sol, curve):
        return Fill(tokens=1_000_000.0, sol=sol)
    h.eng.executor.buy = buy
    yield h


async def test_a_call_from_a_trusted_group_is_bought_with_its_size(h):
    g = CallGroup(id=-1, title="Alpha Calls", sol=0.05, filters=False)
    await h.eng.on_call(CA, g, 1)
    pos = h.eng.positions[CA]
    assert pos.source == "call" and pos.sol_in == pytest.approx(0.05)
    assert any("called in Alpha Calls" in t for t, *_ in h.sent)
    await h.close()


async def test_a_call_with_filters_goes_through_them(h):
    async def reject(c):
        from sniper.models import SafetyReport
        assert c.source == "call" and not c.force and c.creator
        return SafetyReport(passed=False, reasons=["top 10 wallets hold 80.0% of supply"])
    h.eng.safety.evaluate = reject
    await h.eng.on_call(CA, CallGroup(id=-1, title="Alpha"), 1)
    assert CA not in h.eng.positions
    assert "📣 Call in Alpha" in h.last and "top 10 wallets" in h.last
    await h.close()


async def test_alert_mode_paused_and_market_cap_cap(h):
    eng = h.eng
    g = CallGroup(id=-1, title="Alpha", filters=False)
    eng.cfg.calls.action = "alert"
    await eng.on_call(CA, g, 1)
    assert "alert only" in h.last and f"b:{CA}:" in str(h.sent[-1][1])
    eng.cfg.calls.action = "buy"
    eng.paused = True
    await eng.on_call(CA, g, 1)
    assert "paused" in h.last
    eng.paused = False
    eng.cfg.calls.max_market_cap_sol = 20       # this curve is ~32 SOL in: mcap ~ 34 SOL
    await eng.on_call(CA, g, 2)
    assert "market cap already" in h.last and "posted in 2 groups" in h.last
    assert CA not in eng.positions
    await h.close()


async def test_login_flow_in_chat_deletes_every_secret(h, monkeypatch):
    w = h.eng.calls
    steps = []

    async def start_login(api_id, api_hash, phone):
        steps.append(("start", api_id, api_hash, phone))

    async def finish_code(code):
        steps.append(("code", code))
        raise LoginNeedsPassword()

    async def finish_password(pw):
        steps.append(("pw", pw))
        return "@caller"
    monkeypatch.setattr(w, "start_login", start_login)
    monkeypatch.setattr(w, "finish_code", finish_code)
    monkeypatch.setattr(w, "finish_password", finish_password)

    await h.tap("cl")
    assert "cl:login" in h.buttons()
    await h.tap("cl:login")
    await h.text("1234567 0123456789abcdef0123456789abcdef", msg_id=11)
    await h.text("+1 555 123 4567", msg_id=12)
    assert "with spaces" in h.last
    await h.text("1 2 3 4 5", msg_id=13)
    assert "two-step" in h.last
    await h.text("hunter2", msg_id=14)
    assert "logged in as @caller" in h.last
    assert steps == [("start", 1234567, "0123456789abcdef0123456789abcdef", "+15551234567"),
                     ("code", "1 2 3 4 5"), ("pw", "hunter2")]
    deleted = {p["message_id"] for m, p in h.api_calls if m == "deleteMessage"}
    assert {11, 12, 13, 14} <= deleted
    await h.close()


async def test_groups_are_added_and_tuned_from_chat(h, monkeypatch):
    w = h.eng.calls
    w.path.write_text(json.dumps({"api_id": 1, "api_hash": "x", "session": "S"}))

    async def chats(limit=40):
        return [(-1001, "Alpha Calls"), (-1002, "Beta Gems")]
    monkeypatch.setattr(w, "list_chats", chats)
    await h.tap("cl")
    await h.tap("cl:add")
    assert "ca:-1001" in h.buttons()
    await h.tap("ca:-1001")
    assert "Alpha Calls" in h.last and "cgf:-1001" in h.buttons()
    await h.tap("cgf:-1001")
    assert not w.groups.get(-1001).filters
    await h.tap("cgs:-1001")
    await h.text("0.02")
    assert w.groups.get(-1001).sol == 0.02
    await h.tap("cl")
    assert "Alpha Calls" in h.last and "no filters" in h.last
    await h.tap("cl:on")
    assert h.eng.cfg.calls.enabled
    await h.tap("cgr:-1001")
    assert w.groups.get(-1001) is None
    await h.close()


async def test_copy_trades_can_skip_the_filters(h):
    """Telegram toggle for copytrade.run_safety_checks; off = copies are bought unfiltered."""
    from sniper.config import CopyWallet
    from sniper.models import SafetyReport
    eng = h.eng
    await h.tap("c")
    assert "🛡 Filters on copies: ✅ on" in str(h.sent[-1][1])
    idx = [d for d in h.buttons() if d.startswith("e:")][-1]
    await h.tap(idx)
    assert not eng.cfg.copytrade.run_safety_checks and "Filters are OFF" in h.last

    async def reject(c):
        return SafetyReport(passed=False, reasons=["would have been filtered"])
    eng.safety.evaluate = reject
    leader = CopyWallet(address=str(Keypair().pubkey()), label="whale")
    await eng.handle_copy({"mint": CA, "txType": "buy", "solAmount": 1.0, "pool": "pump",
                           "traderPublicKey": leader.address}, leader)
    assert CA in eng.positions and eng.positions[CA].source == "copy"

    await h.tap(idx)                                   # back on: filters apply again
    assert eng.cfg.copytrade.run_safety_checks
    await eng.handle_copy({"mint": CA2, "txType": "buy", "solAmount": 1.0, "pool": "pump",
                           "traderPublicKey": leader.address}, leader)
    assert CA2 not in eng.positions
    await h.close()


def _buy_msg(leader, mint, sol=0.5, mcap=40.0, pool="pump"):
    return {"mint": mint, "txType": "buy", "solAmount": sol, "pool": pool,
            "traderPublicKey": leader, "marketCapSol": mcap,
            "vSolInBondingCurve": 32.0, "vTokensInBondingCurve": 1_073_000_000 * 30 / 32}


async def test_copies_of_curve_coins_skip_holder_and_rugcheck_lookups(h):
    """A copied coin still on its curve: mint/freeze are guaranteed, and holder/rugcheck
    lookups reject almost every young coin. They were silently blocking copies."""
    from sniper.config import CopyWallet
    eng = h.eng
    lookups = []

    async def mint_info(mint):
        lookups.append(mint)
        return None                                  # would fail "mint account not found"
    eng.rpc.get_mint_info = mint_info
    leader = CopyWallet(address=str(Keypair().pubkey()), label="whale")
    await eng.handle_copy(_buy_msg(leader.address, CA), leader)
    assert CA in eng.positions and lookups == []
    assert any("copied whale" in t for t, *_ in h.sent)
    await h.close()


async def test_every_skipped_copy_says_why_in_telegram(h):
    from sniper.config import CopyWallet
    eng = h.eng
    leader = CopyWallet(address=str(Keypair().pubkey()), label="whale")
    await eng.handle_copy(_buy_msg(leader.address, CA, sol=0.01), leader)
    assert "not copied" in h.last and "only bought 0.010 SOL" in h.last
    eng.cfg.copytrade.max_market_cap_sol = 100
    await eng.handle_copy(_buy_msg(leader.address, CA, mcap=250.0), leader)
    assert "market cap 250 SOL is over your copy limit (100 SOL)" in h.last
    assert CA not in eng.positions
    await eng.handle_copy(_buy_msg(leader.address, CA2, mcap=60.0), leader)
    assert CA2 in eng.positions                       # under the cap: copied
    eng.paused = True
    await eng.handle_copy(_buy_msg(leader.address, str(Keypair().pubkey())), leader)
    assert "paused" in h.last
    await h.close()


async def test_copied_positions_can_have_their_own_tp_sl_and_moonbag(h):
    from sniper import exits
    from sniper.config import CopyWallet, TakeProfitLevel
    eng = h.eng
    eng.cfg.exits.stop_loss_pct = 15
    leader = CopyWallet(address=str(Keypair().pubkey()), label="whale")
    await eng.handle_copy(_buy_msg(leader.address, CA), leader)
    pos = eng.positions[CA]
    assert eng.exit_cfg(pos) is eng.cfg.exits                 # off: main exits

    await eng.set_setting("copytrade.own_exits", True)
    assert eng.cfg.copyexits.stop_loss_pct == 15              # seeded from the main exits
    await eng.set_setting("copyexits.stop_loss_pct", 40)
    await eng.set_setting("copyexits.take_profit", "50:50,150:30")
    await eng.set_setting("copyexits.moonbag_pct", 20)
    ex = eng.exit_cfg(pos)
    assert ex.stop_loss_pct == 40 and ex.moonbag_pct == 20
    assert ex.take_profit == [TakeProfitLevel(50, 50), TakeProfitLevel(150, 30)]
    assert ex.exit_on_dev_sell == eng.cfg.exits.exit_on_dev_sell   # the rest: main exits
    assert eng.cfg.exits.stop_loss_pct == 15                  # main exits untouched

    pos.update_price(pos.entry_price * 0.75)                  # -25%: main SL would sell
    assert exits.evaluate(pos, eng.exit_cfg(pos)) is None
    assert exits.evaluate(pos, eng.cfg.exits).reason.startswith("stop loss")
    sniped = type(pos)(**{**pos.__dict__, "source": "pumpfun", "mint": "X"})
    assert eng.exit_cfg(sniped) is eng.cfg.exits

    await h.tap("set:copyexits")
    assert "Using them" in h.last
    await h.close()
