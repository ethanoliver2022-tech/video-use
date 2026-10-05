"""Hot-path latency: what a snipe waits on before its transaction is sent."""
import asyncio
import random

from solders.keypair import Keypair

import tests.test_chaos as chaos
from sniper.models import Candidate, Fill


def engine(tmp_path):
    eng = chaos.build(tmp_path, random.Random(1))
    eng.cfg.trading.max_open_positions = 50
    eng.cfg.trading.cooldown_after_loss_seconds = 0
    return eng


async def test_live_buys_use_the_background_balance_not_an_rpc_call(tmp_path):
    eng = engine(tmp_path)
    eng.live, eng.own_wallet = True, "W"
    reads = []

    async def bal(*a, **k):
        reads.append(1)
        return 10.0
    eng.rpc.get_balance_sol = bal

    async def buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol, signature="S")
    eng.executor.buy = buy
    task = asyncio.create_task(eng.balance_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    assert len(reads) == 1  # the background refresh
    eng._buys_finished = eng._bal_cache[2]
    await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="manual",
                                force=True))
    assert len(reads) == 1  # the snipe itself made no balance call
    # after a buy finished, the next one can't trust that balance: it reads afresh
    await eng.try_buy(Candidate(chain="solana", mint=str(Keypair().pubkey()), source="manual",
                                force=True))
    assert len(reads) == 2
    eng.live = False
    await eng.http.aclose()


def test_bundles_go_to_every_jito_region_by_default():
    from sniper.config import SpeedConfig
    regions = {u.split("//")[1].split(".")[0] for u in SpeedConfig().jito_block_engines}
    assert regions == {"ny", "amsterdam", "frankfurt", "tokyo", "slc"}


async def test_slow_ipfs_never_delays_a_snipe_when_socials_are_optional(monkeypatch):
    import time
    from sniper import intel, safety
    from sniper.config import FilterConfig

    async def public(uri):
        return True
    monkeypatch.setattr(intel, "_resolves_public", public)

    class SlowHttp:
        def stream(self, *a, **k):
            class Ctx:
                async def __aenter__(self_):
                    await asyncio.sleep(10)

                async def __aexit__(self_, *a):
                    return False
            return Ctx()
    chk = safety.SafetyChecker(FilterConfig(min_socials=0, reject_reused_socials=True), None,
                               SlowHttp(), "")
    from sniper.models import SafetyReport
    r = SafetyReport(passed=True)
    t0 = time.monotonic()
    await chk._socials(Candidate(chain="solana", mint="M", source="pumpfun",
                                 uri="https://ipfs.example/x"), r)
    assert time.monotonic() - t0 < 1.5 and r.passed and "not checked" in r.notes[0]


async def test_sell_on_migration(tmp_path):
    from sniper import exits
    eng = engine(tmp_path)
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    pos = eng.positions[m]
    pos.seen_on_curve = True  # held while it was still on the bonding curve
    eng.cfg.exits.sell_on_migration = False
    await eng.on_migration(m)
    assert exits.evaluate(pos, eng.cfg.exits) is None or \
        exits.evaluate(pos, eng.cfg.exits).reason != "migrated"
    eng.cfg.exits.sell_on_migration = True
    dec = exits.evaluate(pos, eng.cfg.exits)
    assert dec.sell_all and dec.reason == "migrated"
    await eng.http.aclose()


async def test_migration_sell_is_retried_when_another_sell_is_running(tmp_path):
    from sniper import exits
    eng = engine(tmp_path)
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    pos = eng.positions[m]
    pos.seen_on_curve = True
    eng.cfg.exits.sell_on_migration = True
    lock = eng.sell_locks.setdefault(m, asyncio.Lock())
    await lock.acquire()  # a take-profit sell is in flight when it graduates
    await eng.on_migration(m)
    await eng.check_exit(pos)
    assert not pos.closed
    lock.release()
    sold = []

    async def sell(mint, tokens, sell_all, pump, curve, slippage_pct=None):
        sold.append(sell_all)
        return Fill(tokens=tokens, sol=0.04, signature="S")
    eng.executor.sell = sell
    await eng.check_exit(pos)  # the next exit-loop tick
    assert sold == [True] and pos.closed and pos.close_reason == "migrated"
    assert exits  # (rule lives in the exit engine)
    await eng.http.aclose()


async def test_buying_an_already_graduated_token_never_triggers_a_migration_sell(tmp_path):
    from sniper import exits
    eng = engine(tmp_path)
    eng.cfg.exits.sell_on_migration = True
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    pos = eng.positions[m]
    assert not pos.seen_on_curve
    await eng.on_trade({"mint": m, "txType": "buy", "traderPublicKey": "x", "solAmount": 1,
                        "pool": "pump-amm", "signature": "s1"})  # trades on PumpSwap
    assert pos.migrated
    dec = exits.evaluate(pos, eng.cfg.exits)
    assert dec is None or dec.reason != "migrated"
    await eng.http.aclose()


# ---- the buy is built while the filters run ----

def _live_ex():
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    ex, rpc = live_executor(load_config(None))
    built = []

    async def pp(action, mint, amount, in_sol, slippage_pct=None):
        built.append((action, amount))
        await asyncio.sleep(0.01)
        return b"tx"
    ex._pumpportal_tx = pp
    return ex, built


def _pump(mint="M"):
    return Candidate(chain="solana", mint=mint, source="pumpfun", route="pump")


async def test_a_prebuilt_buy_is_sent_without_building_again():
    ex, built = _live_ex()
    c = _pump()
    c.prebuilt, c.prebuilt_sol = asyncio.ensure_future(ex.prepare_buy(c, 0.1)), 0.1
    await ex.buy(c, 0.1, None)
    assert built == [("buy", 0.1)] and ex.sender.sent == 1


async def test_a_stale_failed_or_different_prebuilt_buy_is_rebuilt(monkeypatch):
    import sniper.execution.executors as ex_mod
    ex, built = _live_ex()
    c = _pump()
    c.prebuilt, c.prebuilt_sol = asyncio.ensure_future(ex.prepare_buy(c, 0.1)), 0.2
    await ex.buy(c, 0.1, None)           # built for another size: never used
    assert len(built) == 2

    built.clear()
    c = _pump()
    c.prebuilt, c.prebuilt_sol = asyncio.ensure_future(ex.prepare_buy(c, 0.1)), 0.1
    monkeypatch.setattr(ex_mod, "PREBUILT_MAX_AGE", -1.0)  # too old by the time it's used
    await ex.buy(c, 0.1, None)
    assert len(built) == 2
    monkeypatch.undo()

    built.clear()
    c = _pump()

    async def broken(*a):
        raise RuntimeError("pumpportal 500")
    c.prebuilt, c.prebuilt_sol = asyncio.ensure_future(broken()), 0.1
    await ex.buy(c, 0.1, None)           # a failed prebuild costs nothing extra: just build
    assert built == [("buy", 0.1)] and ex.sender.sent == 3


class _PrebuildEx:
    def __init__(self):
        self.prepared, self.sent = [], []

    async def prepare_buy(self, c, sol):
        self.prepared.append(c.mint)
        await asyncio.sleep(0.05)  # PumpPortal round trip
        import time
        return b"tx", 0.0, time.monotonic()

    async def buy(self, c, sol, curve):
        self.sent.append(await c.prebuilt if c.prebuilt else None)
        return Fill(tokens=1000.0, sol=sol, signature="S")


async def _live_engine(tmp_path, passing):
    from sniper.models import SafetyReport
    eng = engine(tmp_path)
    eng.live, eng.own_wallet, eng.paused = True, "W", False
    eng.cfg.entry.confirm_seconds = 0
    eng.executor = ex = _PrebuildEx()
    seen = []

    async def bal(*a, **k):
        return 10.0
    eng.rpc.get_balance_sol = bal

    async def evaluate(c):
        await asyncio.sleep(0.02)  # metadata / socials
        seen.append(list(ex.prepared))
        return SafetyReport(passed=passing)
    eng.safety.evaluate = evaluate
    return eng, ex, seen


async def test_launch_buy_is_built_while_filters_run(tmp_path):
    eng, ex, seen = await _live_engine(tmp_path, passing=True)
    c = _pump(str(Keypair().pubkey()))
    res = await eng.handle_candidate(c)
    assert res.startswith("🟢")
    assert seen == [[c.mint]]            # the build had started before the filters finished
    assert ex.sent and ex.sent[0][0] == b"tx"
    eng.live = False
    await eng.http.aclose()


async def test_rejected_launch_never_sends_its_prebuilt_buy(tmp_path):
    eng, ex, seen = await _live_engine(tmp_path, passing=False)
    c = _pump(str(Keypair().pubkey()))
    res = await eng.handle_candidate(c)
    assert "rejected" in res and not ex.sent
    await asyncio.sleep(0)               # let the cancellation land
    assert c.prebuilt is not None and c.prebuilt.cancelled()
    eng.live = False
    await eng.http.aclose()


async def test_no_prebuild_in_paper_mode_or_for_instantly_rejected_tokens(tmp_path):
    eng, ex, seen = await _live_engine(tmp_path, passing=False)
    c = _pump(str(Keypair().pubkey()))
    c.name = "rug pull"                   # fails the instant name check: nothing is built
    await eng.handle_candidate(c)
    assert not ex.prepared
    eng.live = False
    await eng.handle_candidate(_pump(str(Keypair().pubkey())))
    assert not ex.prepared
    await eng.http.aclose()


async def test_confirmation_window_builds_the_buy_just_before_it_ends(tmp_path, monkeypatch):
    import sniper.engine as em
    monkeypatch.setattr(em, "PREBUILD_LEAD", 0.05)
    eng, ex, seen = await _live_engine(tmp_path, passing=True)
    eng.cfg.entry.confirm_seconds = 0.15
    c = _pump(str(Keypair().pubkey()))
    task = asyncio.ensure_future(eng._window(c))
    await asyncio.sleep(0.05)
    assert not ex.prepared               # not at the start of the window...
    await task
    assert ex.prepared == [c.mint]       # ...but before it ended
    eng.live = False
    await eng.http.aclose()


# ---- sending ----

class _Rpc:
    url = "http://rpc"

    def __init__(self):
        self.sent = 0

    async def send_raw_transaction(self, raw):
        self.sent += 1
        return "SIG"


def _sender(regions):
    from sniper.config import SpeedConfig
    from sniper.execution.sender import TxSender
    cfg = SpeedConfig(jito_block_engines=[f"https://{r}" for r in regions])
    rpc = _Rpc()
    return TxSender(cfg, rpc, None, 0.0001), rpc


def _signed():
    from solders.hash import Hash
    from sniper.execution.wallet import transfer_tx
    kp = Keypair()
    return transfer_tx(kp, str(Keypair().pubkey()), 1000, Hash.new_unique()), kp


async def test_send_returns_on_the_first_region_that_accepts():
    import time
    s, rpc = _sender(["fast", "slow"])
    finished = []

    async def bundle(engine, b):
        if "slow" in engine:
            await asyncio.sleep(0.3)
        finished.append(engine)
        return "ok"
    s._send_bundle = bundle
    tx, kp = _signed()
    t0 = time.monotonic()
    assert await s.send(tx, kp) == str(tx.signatures[0])
    assert time.monotonic() - t0 < 0.2   # didn't wait for the far region
    await asyncio.sleep(0.35)
    assert finished == ["https://fast", "https://slow"]  # which still got the bundle
    assert not s._inflight


async def test_send_falls_back_to_rpc_only_when_every_region_fails():
    s, rpc = _sender(["a", "b"])

    async def bundle(engine, b):
        raise RuntimeError("429")
    s._send_bundle = bundle
    tx, kp = _signed()
    await s.send(tx, kp)
    assert rpc.sent == 1


# ---- connections ----

async def test_trading_hosts_are_kept_warm_in_live_mode_only(tmp_path, monkeypatch):
    import sniper.engine as em
    monkeypatch.setattr(em, "WARM_SECONDS", 0.06)
    eng = engine(tmp_path)
    pinged = []

    async def head(url, **k):
        pinged.append(url)
    eng.http.head = head
    task = asyncio.ensure_future(eng.keep_warm())
    await asyncio.sleep(0.1)
    assert not pinged                     # paper: nothing to keep warm
    eng.live = True
    await asyncio.sleep(0.2)
    task.cancel()
    assert "https://pumpportal.fun/" in pinged
    assert "https://ny.mainnet.block-engine.jito.wtf/" in pinged
    assert len(set(pinged)) == 6          # PumpPortal + 5 Jito regions, one at a time
    eng.live = False
    await eng.http.aclose()


def test_connections_stay_open_between_snipes(tmp_path):
    eng = engine(tmp_path)
    assert eng.http._transport._pool._keepalive_expiry >= 60


def test_degen_preset_never_waits_on_metadata():
    from sniper.config import load_config
    assert load_config(None, preset="degen").filters.reject_reused_socials is False
    assert load_config(None, preset="balanced").filters.reject_reused_socials is True


# ---- exits never wait out a bundle nobody picked ----

class _EscSender:
    def __init__(self):
        self.sent, self.rebroadcasts = 0, 0

    async def priority_fee(self):
        return 0.0001

    async def send(self, tx, payer):
        self.sent += 1
        return "SIG"

    async def rebroadcast(self, tx):
        self.rebroadcasts += 1
        return True


def _esc_executor(confirm_after):
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    from solders.hash import Hash
    from sniper.execution.wallet import transfer_tx
    ex, rpc = live_executor(load_config(None))
    ex.sender = _EscSender()
    signed = transfer_tx(ex.kp, str(Keypair().pubkey()), 1000, Hash.new_unique())
    ex._sign = lambda unsigned: signed

    async def confirm(sig):
        await asyncio.sleep(confirm_after)
        return True
    rpc.confirm = confirm
    return ex


async def test_slow_sell_bundle_is_also_sent_through_rpc(monkeypatch):
    import sniper.execution.executors as ex_mod
    monkeypatch.setattr(ex_mod, "SELL_ESCALATE_SECONDS", 0.05)
    ex = _esc_executor(confirm_after=0.2)
    await ex._submit(b"not-a-tx", "M", "sell")
    assert ex.sender.sent == 1 and ex.sender.rebroadcasts == 1  # same tx, a second way


async def test_quick_sells_and_all_buys_stay_jito_only(monkeypatch):
    import sniper.execution.executors as ex_mod
    monkeypatch.setattr(ex_mod, "SELL_ESCALATE_SECONDS", 0.05)
    ex = _esc_executor(confirm_after=0.0)
    await ex._submit(b"not-a-tx", "M", "sell")
    await asyncio.sleep(0.1)
    assert ex.sender.rebroadcasts == 0                   # landed in time
    ex = _esc_executor(confirm_after=0.2)
    await ex._submit(b"not-a-tx", "M", "buy")
    assert ex.sender.rebroadcasts == 0                   # buys keep sandwich protection


async def test_rebroadcast_goes_through_every_rpc_only_when_jito_was_used():
    s, rpc = _sender(["a"])
    tx, kp = _signed()
    assert await s.rebroadcast(tx) is True and rpc.sent == 1
    s.cfg.jito_also_send_rpc = True                      # RPCs already had it
    assert await s.rebroadcast(tx) is False and rpc.sent == 1
