"""Direct pump.fun trades: built from the chain's own state, checked once before use."""
import struct

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.config import load_config
from sniper.execution import pump_native as pn
from sniper.execution.txguard import check_transaction
from sniper.models import Candidate
from tests.test_hardening import live_executor

CREATOR = Keypair().pubkey()
FEE_RECIPIENT = Keypair().pubkey()
BUYBACK = [Keypair().pubkey() for _ in range(8)]


def curve_bytes(v_tok=1_000_000_000_000_000, v_sol=30_000_000_000, complete=False, mayhem=False,
                cashback=False, quote=bytes(32)):
    return (pn.CURVE_DISC + struct.pack("<QQQQQ?", v_tok, v_sol, 793_000_000_000_000, 0,
                                        1_000_000_000_000_000, complete)
            + bytes(CREATOR) + bytes([1 if mayhem else 0, 1 if cashback else 0]) + quote
            + bytes(20))


def global_bytes(buyback=None):
    data = bytearray(pn.GLOBAL_DISC + b"\x01" + bytes(32) + bytes(FEE_RECIPIENT) + bytes(1200))
    for i, key in enumerate(BUYBACK if buyback is None else buyback):
        data[pn.BUYBACK_AT + 32 * i:pn.BUYBACK_AT + 32 * (i + 1)] = bytes(key)
    return bytes(data)


class ChainRpc:
    """Just enough chain for building and checking direct trades."""

    def __init__(self, curve=None, token_program=str(pn.TOKEN_2022), sim=None, glob=None):
        self.glob = glob if glob is not None else global_bytes()
        self.global_reads = 0
        self.curve = curve if curve is not None else curve_bytes()
        self.token_program, self.sim = token_program, sim
        self.simulated, self.balance_raw, self.confirm_result, self.tx = [], 5_000_000, True, None

    async def get_accounts_raw(self, addresses):
        out = []
        for a in addresses:
            if a == str(pn.GLOBAL):
                self.global_reads += 1
                out.append((str(pn.PUMP), self.glob))
            elif len(out) == 1:  # [bonding curve, mint, (global)]
                out.append((self.token_program, bytes(82)))
            else:
                out.append((str(pn.PUMP), self.curve) if self.curve else None)
        return out

    async def get_latest_blockhash(self):
        return Hash.new_unique()

    async def simulate(self, raw):
        self.simulated.append(raw)
        return self.sim if self.sim is not None else {"err": None, "logs": []}

    async def get_token_balance_raw(self, owner, mint):
        return self.balance_raw, 6

    async def get_token_balance(self, owner, mint):
        return self.balance_raw / 1e6

    async def confirm(self, sig, timeout=None):
        return self.confirm_result

    async def get_transaction(self, sig):
        return self.tx


def _executor(rpc):
    ex, _ = live_executor(load_config(None))
    ex.rpc = ex.native.rpc = rpc
    notices = []
    ex.notice = notices.append
    signed = []
    ex._sign = lambda unsigned: signed.append(unsigned) or unsigned

    async def pumpportal(*a, **k):  # what PumpPortal sends now: the trade wrapped in FAdo9
        raise AssertionError("PumpPortal isn't used once direct trading is checked")
    ex._pumpportal_tx = pumpportal
    return ex, notices, signed


def _ixs(raw):
    tx = VersionedTransaction.from_bytes(raw)
    keys = list(tx.message.account_keys)
    return tx, [(keys[ix.program_id_index], bytes(ix.data), [keys[i] for i in ix.accounts])
                for ix in tx.message.instructions]


async def test_direct_buy_has_pump_funs_exact_accounts_and_passes_the_guard():
    rpc = ChainRpc()
    user, mint = Keypair().pubkey(), Keypair().pubkey()
    raw = await pn.PumpNative(rpc).buy_tx(user, str(mint), 0.03, 20, 0.0005)
    tx, ixs = _ixs(raw)
    assert [str(p)[:4] for p, _, _ in ixs] == ["Comp", "Comp", "ATok", "6EF8"]
    program, data, accts = ixs[-1]
    assert data[:8] == pn.BUY
    amount, max_cost = struct.unpack_from("<QQ", data, 8)
    assert max_cost == int(0.03e9 * 1.2)
    # 0.03 SOL into a 30 SOL / 1e9 token curve, after ~1.25% fees
    assert amount == pytest.approx(1e15 * 0.0296 / 30.0296, rel=1e-3)
    bc = Pubkey.find_program_address([b"bonding-curve", bytes(mint)], pn.PUMP)[0]
    t22 = pn.TOKEN_2022
    assert accts == [
        pn.GLOBAL, FEE_RECIPIENT, mint, bc, pn.ata(bc, mint, t22), pn.ata(user, mint, t22), user,
        pn.SYSTEM, t22, Pubkey.find_program_address([b"creator-vault", bytes(CREATOR)], pn.PUMP)[0],
        pn.EVENT_AUTHORITY, pn.PUMP, pn.GLOBAL_VOLUME,
        Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], pn.PUMP)[0],
        pn.FEE_CONFIG, pn.FEE_PROGRAM,
        Pubkey.find_program_address([b"bonding-curve-v2", bytes(mint)], pn.PUMP)[0],
        accts[17]]
    assert len(accts) == 18
    assert accts[17] in BUYBACK         # a buyback fee recipient from pump.fun's own settings
    check_transaction(tx, user, 0.04, max_fee_sol=0.002, side="buy",
                      max_curve_sol=0.03 * 1.2 * 1.05 + 0.01, mint=str(mint))


async def test_direct_sell_accounts_and_floor():
    rpc = ChainRpc(token_program=str(pn.TOKEN))
    user, mint = Keypair().pubkey(), Keypair().pubkey()
    raw = await pn.PumpNative(rpc).sell_tx(user, str(mint), 1_000_000_000, 30, 0.0005)
    tx, ixs = _ixs(raw)
    assert [str(p)[:4] for p, _, _ in ixs] == ["Comp", "Comp", "6EF8"]
    _, data, accts = ixs[-1]
    amount, floor = struct.unpack_from("<QQ", data, 8)
    assert data[:8] == pn.SELL and amount == 1_000_000_000
    expected = 30_000_000_000 * 1_000_000_000 // (1_000_000_000_000_000 + 1_000_000_000)
    assert floor == int(int(expected * (1 - pn.FEE_ESTIMATE)) * 0.7)
    assert accts[8] == Pubkey.find_program_address([b"creator-vault", bytes(CREATOR)], pn.PUMP)[0]
    assert accts[9] == pn.TOKEN and len(accts) == 16
    assert accts[14] == Pubkey.find_program_address([b"bonding-curve-v2", bytes(mint)], pn.PUMP)[0]
    assert accts[15] in BUYBACK and accts[1] == FEE_RECIPIENT
    check_transaction(tx, user, 0.01, max_fee_sol=0.002, side="sell")


@pytest.mark.parametrize("curve,owner,why", [
    (curve_bytes(complete=True), str(pn.TOKEN), "graduated"),
    (curve_bytes(mayhem=True), str(pn.TOKEN), "mayhem"),
    (curve_bytes(cashback=True), str(pn.TOKEN), "cashback"),
    (curve_bytes(quote=bytes(Keypair().pubkey())), str(pn.TOKEN), "another coin"),
    (b"\x00" * 120, str(pn.TOKEN), "not a pump.fun"),
    (curve_bytes(), "SomethingElse111111111111111111111111111111", "token program"),
    (b"", str(pn.TOKEN), "no bonding curve"),
])
async def test_tokens_it_cant_trade_directly_go_another_way(curve, owner, why):
    rpc = ChainRpc(curve=curve, token_program=owner)
    with pytest.raises(pn.NotNative, match=why):
        await pn.PumpNative(rpc).buy_tx(Keypair().pubkey(), str(Keypair().pubkey()), 0.01, 20, 0)


def _pump_cand():
    return Candidate(chain="solana", mint=str(Keypair().pubkey()), source="pumpfun", route="pump")


async def test_first_direct_trade_is_checked_then_used_without_pumpportal():
    rpc = ChainRpc()
    ex, notices, signed = _executor(rpc)

    async def wrapped(*a, **k):  # PumpPortal's current buys: refused, can't be cleaned
        from tests.test_rent import _pp_trade
        return _pp_trade(ex, str(Keypair().pubkey()), pump_ix=False)
    ex._pumpportal_tx = wrapped
    await ex.buy(_pump_cand(), 0.01, None)
    assert ex.native_ok is True and len(rpc.simulated) == 1      # checked once
    assert "buys passed their check" in notices[0]
    assert str(pn.PUMP)[:4] in [str(p)[:4] for p, _, _ in _ixs(signed[-1])[1]]

    async def never(*a, **k):
        raise AssertionError("PumpPortal isn't asked once direct trading is checked")
    ex._pumpportal_tx = never
    await ex.buy(_pump_cand(), 0.01, None)
    assert len(rpc.simulated) == 1                               # no more test runs: fast
    mint = _pump_cand().mint
    await ex.sell(mint, 5.0, True, pump=True, curve=None, value_sol=0.01)
    assert ex.native_state["sell"] is True and len(rpc.simulated) == 2  # sells: their own check
    assert "sells passed their check" in notices[1]
    assert _ixs(signed[-1])[1][-1][1][:8] == pn.SELL
    await ex.sell(mint, 5.0, True, pump=True, curve=None, value_sol=0.01)
    assert len(rpc.simulated) == 2 and _ixs(signed[-1])[1][-1][1][:8] == pn.SELL


async def test_a_failed_check_turns_direct_trading_off_and_says_so():
    rpc = ChainRpc(sim={"err": {"InstructionError": [3, {"Custom": 2006}]},
                        "logs": ["Program log: AnchorError caused by account: creator_vault. "
                                 "Error Code: ConstraintSeeds."]})
    ex, notices, _ = _executor(rpc)
    jup_calls = []

    class Jup:
        async def quote(self, *a, **k):
            jup_calls.append(1)
            return {}

        async def swap_tx(self, *a):
            from tests.test_rent import _tx_calling
            from sniper.execution.txguard import COMPUTE_BUDGET
            return _tx_calling(ex, COMPUTE_BUDGET)
    ex.jupiter = Jup()

    async def wrapped(*a, **k):
        from tests.test_rent import _pp_trade
        return _pp_trade(ex, str(Keypair().pubkey()), pump_ix=False)
    ex._pumpportal_tx = wrapped
    await ex.buy(_pump_cand(), 0.01, None)
    assert ex.native_ok is False and jup_calls == [1]           # this trade: Jupiter
    assert "buys failed their check" in notices[0] and "ConstraintSeeds" in notices[0]
    await ex.buy(_pump_cand(), 0.01, None)
    assert len(rpc.simulated) == 1                               # never retried this session


async def test_a_slippage_failure_in_the_check_doesnt_turn_it_off():
    rpc = ChainRpc(sim={"err": "x", "logs": ["Error Code: TooMuchSolRequired."]})
    ex, notices, _ = _executor(rpc)
    assert await ex._native("buy", lambda: ex._native_buy(_pump_cand(), 0.01)) is None
    assert ex.native_ok is None and not notices                  # checked again next time
    rpc.sim = None
    assert await ex._native("buy", lambda: ex._native_buy(_pump_cand(), 0.01)) is not None
    assert ex.native_ok is True


async def test_sol_quoted_curves_old_and_new_layouts_trade_directly():
    from sniper.models import SOL_MINT
    for data in (curve_bytes(quote=bytes(Pubkey.from_string(SOL_MINT))), curve_bytes(),
                 curve_bytes()[:pn.CURVE_CREATOR_AT + 32]):   # before newer fields existed
        raw = await pn.PumpNative(ChainRpc(curve=data)).buy_tx(
            Keypair().pubkey(), str(Keypair().pubkey()), 0.01, 20, 0)
        assert raw


def test_buyback_recipients_are_read_at_the_idl_offset():
    fees = pn.parse_global(global_bytes())
    assert fees.recipient == FEE_RECIPIENT and fees.buyback == BUYBACK
    assert pn.BUYBACK_AT == 741


@pytest.mark.parametrize("glob", [
    global_bytes(buyback=[BUYBACK[0]] * 8),                  # not distinct
    global_bytes(buyback=BUYBACK[:7] + [Pubkey.default()]),  # one unset
    global_bytes()[:900],                                    # too short (older layout)
    b"\x00" * 8 + global_bytes()[8:],                       # not pump.fun's Global
])
async def test_unexpected_settings_never_build_a_trade(glob):
    rpc = ChainRpc(glob=glob)
    with pytest.raises(pn.NotNative, match="settings"):
        await pn.PumpNative(rpc).buy_tx(Keypair().pubkey(), str(Keypair().pubkey()), 0.01, 20, 0)


async def test_settings_are_cached_then_refreshed(monkeypatch):
    rpc = ChainRpc()
    native = pn.PumpNative(rpc)
    for _ in range(3):
        await native.buy_tx(Keypair().pubkey(), str(Keypair().pubkey()), 0.01, 20, 0)
    assert rpc.global_reads == 1
    monkeypatch.setattr(pn, "SETTINGS_TTL", -1)
    await native.buy_tx(Keypair().pubkey(), str(Keypair().pubkey()), 0.01, 20, 0)
    assert rpc.global_reads == 2


async def test_a_failed_sell_check_leaves_direct_buys_on():
    rpc = ChainRpc()
    ex, notices, signed = _executor(rpc)
    ex.native_state["buy"] = True
    rpc.sim = {"err": "x", "logs": ["Error Code: AccountNotEnoughKeys."]}

    class Jup:
        async def quote(self, *a, **k):
            return {}

        async def swap_tx(self, *a):
            from tests.test_rent import _tx_calling
            from sniper.execution.txguard import COMPUTE_BUDGET
            return _tx_calling(ex, COMPUTE_BUDGET)
    ex.jupiter = Jup()
    await ex.sell(_pump_cand().mint, 5.0, True, pump=True, curve=None, value_sol=0.01)
    assert ex.native_state == {"buy": True, "sell": False}
    assert "sells failed their check" in notices[0]
    assert _ixs(signed[-1])[1][-1][0] != pn.PUMP                 # this sell: Jupiter
