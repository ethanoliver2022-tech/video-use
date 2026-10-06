"""Token account rent: a buy opens a token account (~0.002 SOL rent); a full exit closes it
again so the rent comes back. Plus: why a transaction that expired didn't land."""
import asyncio

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import tests.test_chaos as chaos
from sniper.config import load_config
from sniper.execution.executors import NotLanded, explain_failure
from sniper.execution.txguard import TOKEN, TOKEN_2022
from sniper.models import Fill
from sniper.solana_rpc import SolanaRpc
from sniper.stats import summarize
from tests.test_hardening import live_executor


def _acct(owner, mint, program=TOKEN, amount="0", **info):
    return {"pubkey": str(Keypair().pubkey()), "account": {
        "lamports": 2_039_280, "owner": program,
        "data": {"program": "spl-token", "parsed": {"type": "account", "info": {
            "owner": owner, "mint": mint, "state": "initialized",
            "tokenAmount": {"amount": amount, "decimals": 6}, **info}}}}}


async def test_only_empty_closable_accounts_of_this_wallet_are_listed():
    owner, other = str(Keypair().pubkey()), str(Keypair().pubkey())
    good, good22 = str(Keypair().pubkey()), str(Keypair().pubkey())
    rows = {TOKEN: [
        _acct(owner, good),
        _acct(owner, "HELD", amount="5"),                      # still holds tokens
        _acct(other, "NOTMINE"),                               # someone else's
        _acct(owner, "FROZEN", state="frozen"),                # can't be closed
        _acct(owner, "LOCKED", closeAuthority=other),          # only `other` may close it
        {"pubkey": "junk"},                                    # malformed: skipped
    ], TOKEN_2022: [
        _acct(owner, good22, program=TOKEN_2022, extensions=[{"extension": "immutableOwner"}]),
        _acct(owner, "FEES", program=TOKEN_2022, extensions=[
            {"extension": "transferFeeAmount", "state": {"withheldAmount": 7}}]),
        _acct(owner, "ODD", program=TOKEN_2022, extensions=[{"extension": "confidentialTransferAccount"}]),
    ]}
    rpc = SolanaRpc("http://x")

    async def call(method, params):
        assert method == "getTokenAccountsByOwner"
        return {"value": rows[params[1]["programId"]]}
    rpc.call = call
    found = await rpc.empty_token_accounts(owner)
    assert sorted(a["mint"] for a in found) == sorted([good, good22])
    assert {a["program"] for a in found} == {TOKEN, TOKEN_2022}
    assert all(a["lamports"] == 2_039_280 for a in found)
    await rpc.http.aclose()


def _closing_executor(accounts, confirm=True):
    ex, rpc = live_executor(load_config(None))
    sent = []

    async def empty(owner):
        assert owner == ex.pubkey
        return accounts

    async def blockhash():
        from solders.hash import Hash
        return Hash.new_unique()

    async def send_raw(raw):
        tx = VersionedTransaction.from_bytes(raw)
        sent.append(tx)
        return str(tx.signatures[0])

    async def conf(sig, timeout=None):
        if isinstance(confirm, Exception):
            raise confirm
        return confirm
    rpc.empty_token_accounts, rpc.get_latest_blockhash = empty, blockhash
    rpc.send_raw_transaction, rpc.confirm = send_raw, conf
    return ex, sent


def _empty(owner_mints):
    return [{"address": str(Keypair().pubkey()), "mint": m, "program": TOKEN,
             "lamports": 2_039_280} for m in owner_mints]


async def test_close_empty_closes_into_this_wallet_and_reports_the_rent():
    accounts = _empty([str(Keypair().pubkey()) for _ in range(15)])
    ex, sent = _closing_executor(accounts)
    keep = frozenset({accounts[0]["mint"]})
    n, sol = await ex.close_empty(keep=keep)
    assert n == 14 and sol == pytest.approx(14 * 0.00203928)
    assert len(sent) == 2                                     # batches of 12
    me = ex.kp.pubkey()
    closed = set()
    for tx in sent:
        keys = list(tx.message.account_keys)
        assert keys[0] == me and tx.message.header.num_required_signatures == 1
        for ix in tx.message.instructions:
            if str(keys[ix.program_id_index]) == TOKEN:
                assert bytes(ix.data) == bytes([9])           # CloseAccount, nothing else
                acct, dest, auth = (keys[i] for i in ix.accounts)
                assert dest == me and auth == me
                closed.add(str(acct))
    assert closed == {a["address"] for a in accounts[1:]}  # the kept mint was left alone


async def test_close_empty_only_the_given_mint():
    accounts = _empty(["A", "B"])
    ex, sent = _closing_executor(accounts)
    assert await ex.close_empty(only={"B"}) == (1, pytest.approx(0.00203928))
    assert len(sent) == 1 and len(sent[0].message.instructions) == 3  # 2 budget + 1 close


async def test_a_failed_or_unconfirmed_close_reports_nothing_back():
    from sniper.solana_rpc import TxFailed
    for outcome in (False, TxFailed("failed on-chain")):
        ex, _ = _closing_executor(_empty(["A"]), confirm=outcome)
        assert await ex.close_empty() == (0, 0.0)


async def test_close_transaction_passes_the_tx_guard_cheaply():
    from sniper.execution.txguard import check_transaction
    ex, sent = _closing_executor(_empty(["A"] * 12))
    await ex.close_empty()
    # moves no SOL out, priority fee well under 0.0001 SOL
    check_transaction(sent[0], ex.kp.pubkey(), 0.0, max_fee_sol=0.0001, side="sell")


# ---- engine: full exits give the rent back, /reclaim sweeps old accounts ----

class _Ex:
    def __init__(self, back=0.002):
        self.back, self.calls = back, []

    async def sell(self, mint, tokens, sell_all, pump, curve, slippage_pct=None):
        f = Fill(tokens=tokens, sol=0.04, signature="S")
        f.emptied = sell_all
        return f

    async def close_empty(self, only=None, keep=frozenset()):
        self.calls.append((only, keep))
        return (1, self.back) if self.back else (0, 0.0)


async def _engine_with_position(tmp_path):
    eng = chaos.build(tmp_path, chaos.random.Random(1))
    eng.cfg.trading.max_open_positions = 50
    m = str(Keypair().pubkey())
    await eng.manual_buy(m, 0.05, force=True)
    eng.live = True
    sent = []

    async def send(text, *a, **k):
        sent.append(text)
    eng.notifier.send = send

    async def unwatch(mint):
        pass
    eng.stream.unwatch_token = unwatch
    return eng, m, sent


async def test_full_live_exit_closes_the_token_account_and_books_the_rent(tmp_path):
    eng, m, sent = await _engine_with_position(tmp_path)
    eng.executor = ex = _Ex(back=0.002)
    other = str(Keypair().pubkey())
    eng._buying[other] = 0.05                          # a buy in flight: its account stays
    sol_in = eng.positions[m].sol_in
    res = await eng.manual_sell(m, 100)
    await asyncio.sleep(0.05)
    assert res.startswith("🔴")
    only, keep = ex.calls[0]
    assert only == {m} and other in keep and m not in keep
    pos = eng.positions[m]
    assert pos.closed and pos.sol_out == pytest.approx(0.042)
    close = eng.store.events("close")[-1]
    assert close["pnl_sol"] == pytest.approx(0.042 - sol_in)
    assert any("0.0020 SOL account rent back" in t for t in sent)
    eng.live = False
    await eng.http.aclose()


async def test_partial_exits_and_failed_closes_never_block_the_sell(tmp_path):
    eng, m, sent = await _engine_with_position(tmp_path)
    eng.executor = ex = _Ex()
    await eng.manual_sell(m, 50)
    assert not ex.calls                                 # tokens left: the account stays

    class Boom(_Ex):
        async def close_empty(self, only=None, keep=frozenset()):
            raise RuntimeError("rpc down")
    eng.executor = Boom()
    res = await eng.manual_sell(m, 100)
    assert res.startswith("🔴") and eng.positions[m].closed
    assert eng.store.events("close")                    # booked all the same
    eng.live = False
    await eng.http.aclose()


async def test_reclaim_sweeps_old_accounts_and_counts_in_pnl(tmp_path):
    eng, m, _ = await _engine_with_position(tmp_path)
    eng.executor = ex = _Ex(back=0.006)
    before = eng.store.realized_today()
    text = await eng.reclaim_rent()
    assert "0.0060 SOL back" in text
    assert m in ex.calls[0][1]                          # an open position's account is kept
    assert eng.store.realized_today() == pytest.approx(before + 0.006)
    assert summarize(eng.store)["rent_back"] == pytest.approx(0.006)
    ex.back = 0
    assert "No empty token accounts" in await eng.reclaim_rent()
    eng.live = False
    assert "Paper mode" in await eng.reclaim_rent()
    await eng.http.aclose()


# ---- why a transaction didn't land ----

def test_failure_reasons_are_explained():
    slip = ["Program log: AnchorError ... Error Code: TooMuchSolRequired. Error Number: 6002."]
    assert "slippage" in explain_failure({"InstructionError": [3, {"Custom": 6002}]}, slip)
    assert "graduated" in explain_failure("x", ["Error Code: BondingCurveComplete."])
    assert "not enough SOL" in explain_failure("x", ["Transfer: insufficient lamports 5, need 9"])
    assert "not enough SOL" in explain_failure("InsufficientFundsForRent", None)
    assert "(Weird)" in explain_failure("x", ["Error Code: Weird."])
    assert "network refused" in explain_failure({"odd": 1}, "not a list")


async def test_an_expired_buy_says_why():
    ex, rpc = live_executor(load_config(None))

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_result = False

    async def simulate(raw):
        return {"err": {"InstructionError": [2, {"Custom": 6002}]},
                "logs": ["Error Code: TooMuchSolRequired."]}
    rpc.simulate = simulate
    from sniper.models import Candidate
    with pytest.raises(NotLanded, match="Likely reason: the price moved more than your slippage"):
        await ex.buy(Candidate(chain="solana", mint="M", source="x", route="pump"), 0.01, None)

    async def fine(raw):
        return {"err": None, "logs": []}
    rpc.simulate = fine
    with pytest.raises(NotLanded) as e:
        await ex.buy(Candidate(chain="solana", mint="M", source="x", route="pump"), 0.01, None)
    assert "no validator picked it up" in str(e.value)  # valid trade: it wasn't included

    async def silent(raw):
        return None
    rpc.simulate = silent
    with pytest.raises(NotLanded) as e:
        await ex.buy(Candidate(chain="solana", mint="M", source="x", route="pump"), 0.01, None)
    assert "Likely reason" not in str(e.value)          # no answer: no guess

    async def slow(raw):
        await asyncio.sleep(10)
    rpc.simulate = slow
    import sniper.execution.executors as ex_mod
    ex_mod.SIMULATE_TIMEOUT, old = 0.05, ex_mod.SIMULATE_TIMEOUT
    try:
        with pytest.raises(NotLanded):                   # a slow check never hangs the bot
            await ex.buy(Candidate(chain="solana", mint="M", source="x", route="pump"), 0.01, None)
    finally:
        ex_mod.SIMULATE_TIMEOUT = old


# ---- asking Jito what became of a bundle ----

class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


async def test_bundle_ids_are_remembered_and_their_fate_read_back():
    from sniper.config import SpeedConfig
    from sniper.execution.sender import TxSender
    from tests.test_speed import _Rpc, _signed
    statuses = {"https://ny": "Invalid", "https://tokyo": "Failed"}

    class Http:
        async def post(self, url, json=None, timeout=None):
            engine = url.split("/api")[0]
            if json["method"] == "sendBundle":
                return _Resp({"result": f"id-{engine}"})
            assert json["params"] == [[f"id-{engine}"]]
            if engine not in statuses:
                raise RuntimeError("down")
            return _Resp({"result": {"value": [{"status": statuses[engine]}]}})
    s = TxSender(SpeedConfig(jito_block_engines=["https://ny", "https://tokyo", "https://slc"]),
                 _Rpc(), Http(), 0.0001)
    tx, kp = _signed()
    sig = await s.send(tx, kp)
    await asyncio.sleep(0.05)                    # the slower regions finish in the background
    assert len(s.bundles[sig]) == 3
    assert await s.bundle_status(sig) == "Failed"   # the most telling answer wins
    statuses["https://slc"] = "Landed"
    assert await s.bundle_status(sig) == "Landed"
    assert await s.bundle_status("unknown") is None


async def test_an_expired_jito_buy_names_the_jito_reason():
    ex, rpc = live_executor(load_config(None))
    ex.cfg.speed.jito_enabled = True

    async def pp(*a, **k):
        return b"tx"
    ex._pumpportal_tx = pp
    rpc.confirm_result = False

    async def fine(raw):
        return {"err": None, "logs": []}
    rpc.simulate = fine
    ex.sender.bundles = {}
    from sniper.models import Candidate
    for status, expect in (("Failed", "tip lost"), ("Invalid", "dropped it before the auction"),
                           (None, "Try a higher Jito tip")):
        async def bundle_status(sig, _s=status):
            return _s
        ex.sender.bundle_status = bundle_status
        with pytest.raises(NotLanded, match=expect):
            await ex.buy(Candidate(chain="solana", mint="M", source="x", route="pump"), 0.01, None)

    async def slippage(raw):
        return {"err": "x", "logs": ["Error Code: TooMuchSolRequired."]}
    rpc.simulate = slippage                       # a real on-chain reason beats Jito's
    with pytest.raises(NotLanded, match="slippage"):
        await ex.buy(Candidate(chain="solana", mint="M", source="x", route="pump"), 0.01, None)


# ---- PumpPortal's buy refused by the guard: buy through Jupiter instead ----

def _tx_calling(ex, program: str) -> bytes:
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    ix = Instruction(Pubkey.from_string(program), b"\x01",
                     [AccountMeta(ex.kp.pubkey(), True, True)])
    return bytes(VersionedTransaction(MessageV0.try_compile(ex.kp.pubkey(), [ix], [], Hash.new_unique()),
                                      [ex.kp]))


class _Jup:
    def __init__(self, tx=None, fail=None):
        self.tx, self.fail, self.calls = tx, fail, 0

    async def quote(self, *a, **k):
        self.calls += 1
        if self.fail:
            raise self.fail
        return {"outAmount": "1"}

    async def swap_tx(self, q, user, fee):
        return self.tx


async def test_a_refused_pumpportal_buy_is_bought_through_jupiter_instead():
    from sniper.models import Candidate
    from sniper.execution.executors import NotSent
    from sniper.execution.txguard import COMPUTE_BUDGET
    ex, rpc = live_executor(load_config(None))
    arb = "FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"
    signed = []
    ex._sign = lambda unsigned: signed.append(unsigned) or unsigned

    async def pp(*a, **k):
        return _tx_calling(ex, arb)
    ex._pumpportal_tx = pp
    clean = _tx_calling(ex, COMPUTE_BUDGET)
    ex.jupiter = _Jup(tx=clean)
    rpc.balance_raw = 5_000_000
    c = Candidate(chain="solana", mint=str(Keypair().pubkey()), source="pumpfun", route="pump")
    await ex.buy(c, 0.01, None)
    assert ex.jupiter.calls == 1 and signed == [clean]   # PumpPortal's tx was never signed

    signed.clear()
    ex.jupiter = _Jup(fail=RuntimeError("no route"))   # too new for Jupiter: a clear failure
    with pytest.raises(NotSent, match="unknown program .* Jupiter couldn't build"):
        await ex.buy(c, 0.01, None)
    assert not signed

    async def good(*a, **k):
        return clean
    ex._pumpportal_tx = good                            # a normal PumpPortal buy: no detour
    ex.jupiter = _Jup(tx=b"never")
    await ex.buy(c, 0.01, None)
    assert ex.jupiter.calls == 0 and signed == [clean]


# ---- PumpPortal's trade with an extra untrusted call: the call is removed, the trade kept ----

ARB = "FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"


def _pp_trade(ex, mint, side="buy", with_arb=True, pump_ix=True):
    import struct
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from sniper.execution.txguard import (COMPUTE_BUDGET, PUMP_BUY, PUMP_SELL,
                                          _owner_atas)
    me = ex.kp.pubkey()
    ata = sorted(_owner_atas(str(me), mint), key=str)[0]
    ixs = [Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([2]) + struct.pack("<I", 120_000), []),
           Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([3]) + struct.pack("<Q", 1000), [])]
    data = (PUMP_BUY + struct.pack("<QQ", 10**12, 11_000_000) if side == "buy"
            else PUMP_SELL + struct.pack("<QQ", 10**12, 0)) + b"\x00"
    if pump_ix:
        accts = [Keypair().pubkey() for _ in range(5)] + [ata, me]
        ixs.append(Instruction(Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"), data,
                               [AccountMeta(a, a == me, True) for a in accts]))
    if with_arb:
        ixs.append(Instruction(Pubkey.from_string(ARB), b"\x07",
                               [AccountMeta(me, True, True), AccountMeta(Keypair().pubkey(), False, True)]))
    msg = MessageV0.try_compile(me, ixs, [], Hash.new_unique())
    return bytes(VersionedTransaction.populate(msg, [Signature.default()]))


def _programs(raw):
    t = VersionedTransaction.from_bytes(raw)
    keys = t.message.account_keys
    return [str(keys[i.program_id_index]) for i in t.message.instructions]


async def test_the_untrusted_call_is_removed_and_the_plain_pump_buy_signed():
    from sniper.models import Candidate
    from sniper.execution.txguard import check_transaction
    ex, rpc = live_executor(load_config(None))
    mint = str(Keypair().pubkey())
    signed = []
    ex._sign = lambda unsigned: signed.append(unsigned) or unsigned

    async def pp(*a, **k):
        return _pp_trade(ex, mint)
    ex._pumpportal_tx = pp
    ex.jupiter = _Jup(fail=AssertionError("Jupiter isn't needed"))
    rpc.balance_raw = 5_000_000
    await ex.buy(Candidate(chain="solana", mint=mint, source="pumpfun", route="pump"), 0.01, None)
    assert ex.jupiter.calls == 0 and len(signed) == 1
    progs = _programs(signed[0])
    assert ARB not in progs and "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in progs
    check_transaction(VersionedTransaction.from_bytes(signed[0]), ex.kp.pubkey(), 0.05, mint=mint,
                      max_curve_sol=0.02)
    # it really signs: the wallet's signature over the cleaned message verifies
    real = VersionedTransaction(VersionedTransaction.from_bytes(signed[0]).message, [ex.kp])
    assert real.verify_with_results() == [True]


async def test_without_a_plain_pump_trade_left_nothing_is_stripped():
    from sniper.execution.txguard import strip_untrusted
    ex, _ = live_executor(load_config(None))
    mint = str(Keypair().pubkey())
    only_arb = VersionedTransaction.from_bytes(_pp_trade(ex, mint, pump_ix=False))
    assert strip_untrusted(only_arb) is None           # the buy itself runs through it: refuse
    clean = VersionedTransaction.from_bytes(_pp_trade(ex, mint, with_arb=False))
    assert strip_untrusted(clean) is None              # nothing to remove
    buy = VersionedTransaction.from_bytes(_pp_trade(ex, mint))
    assert strip_untrusted(buy, side="sell") is None   # a "sell" that's really a buy: refuse
    assert strip_untrusted(buy, frozenset({ARB})) is None  # allowed in .env: kept as built


async def test_the_untrusted_call_is_removed_from_sells_too():
    ex, rpc = live_executor(load_config(None))
    mint = str(Keypair().pubkey())
    signed = []
    ex._sign = lambda unsigned: signed.append(unsigned) or unsigned

    async def pp(*a, **k):
        return _pp_trade(ex, mint, side="sell")
    ex._pumpportal_tx = pp
    ex.jupiter = _Jup(fail=AssertionError("Jupiter isn't needed"))
    rpc.balance_raw = 5_000_000
    await ex.sell(mint, 5.0, True, pump=True, curve=None, value_sol=0.01)
    assert ex.jupiter.calls == 0 and ARB not in _programs(signed[0])


def test_extra_rpc_urls_from_env_join_the_broadcast(monkeypatch):
    monkeypatch.setenv("EXTRA_RPC_URLS", " https://a.example/k1 , junk, https://b.example ,https://a.example/k1")
    cfg = load_config(None)
    assert cfg.speed.broadcast_rpcs == ["https://a.example/k1", "https://b.example"]


async def test_dropped_bundle_advice_matches_the_settings():
    ex, _ = live_executor(load_config(None))

    async def invalid(sig):
        return "Invalid"
    ex.sender.bundle_status = invalid
    ex.cfg.speed.jito_also_send_rpc = False
    assert "Turn on 'Also send via RPC'" in await ex._jito_verdict("S")
    ex.cfg.speed.jito_also_send_rpc = True
    text = await ex._jito_verdict("S")
    assert "EXTRA_RPC_URLS" in text and "Turn on" not in text


def test_describe_shows_the_shape_without_amounts():
    from sniper.execution.txguard import describe
    ex, _ = live_executor(load_config(None))
    text = describe(VersionedTransaction.from_bytes(_pp_trade(ex, str(Keypair().pubkey()))))
    parts = text.split()
    assert [p.split(":")[0] for p in parts] == ["Comp", "Comp", "6EF8", "FAdo"]
    assert parts[2] == "6EF8:66063d1201daebea/7" and parts[3] == "FAdo:07/2"


async def test_when_every_path_refuses_each_reason_is_shown_rpc_first():
    from sniper.config import SpeedConfig
    from sniper.execution.sender import TxSender
    from tests.test_speed import _signed

    class Rpc:
        url = "http://rpc"

        async def send_raw_transaction(self, raw):
            raise RuntimeError("sendTransaction: Transaction too large: 1300 > 1232")

    class Http:
        async def post(self, *a, **k):
            raise RuntimeError("Client error '400 Bad Request' for url "
                               "'https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles'"
                               "\nFor more information")
    cfg = SpeedConfig(jito_block_engines=["https://x"], jito_also_send_rpc=True)
    s = TxSender(cfg, Rpc(), Http(), 0.0001)
    tx, kp = _signed()
    with pytest.raises(RuntimeError) as e:
        await s.send(tx, kp)
    text = str(e.value)
    assert text.index("too large") < text.index("400 Bad Request") and "more information" not in text
