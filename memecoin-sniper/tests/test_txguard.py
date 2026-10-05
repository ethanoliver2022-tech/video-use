"""The transaction guard: real swap shapes pass, every way to drain the wallet is refused."""
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import AssignParams, TransferParams, assign, transfer
from solders.transaction import VersionedTransaction

from sniper.execution.txguard import UnsafeTransaction, check_transaction

ME = Keypair()
ATTACKER = Keypair().pubkey()
P = Pubkey.from_string
CB = P("ComputeBudget111111111111111111111111111111")
ATA = P("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
TOKEN = P("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
PUMP = P("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
JUP = P("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
FEE_WALLET = Keypair().pubkey()  # PumpPortal's fee account
WSOL_ATA = Keypair().pubkey()


def ix(program, data=b"", accounts=()):
    return Instruction(program, data, [AccountMeta(a, is_signer=(a == ME.pubkey()), is_writable=True)
                                       for a in accounts])


def tx(*ixs, payer=None, signers=None):
    payer = payer or ME.pubkey()
    msg = MessageV0.try_compile(payer, list(ixs), [], Hash.new_unique())
    return VersionedTransaction(msg, signers or [ME])


def sol(lamports):
    return transfer(TransferParams(from_pubkey=ME.pubkey(), to_pubkey=FEE_WALLET, lamports=lamports))


def budget():
    return ix(CB, bytes([2]) + struct.pack("<I", 200_000))


def pump_buy():
    return ix(PUMP, b"\x66" * 24, [ME.pubkey(), Keypair().pubkey()])


def check(t, cap=0.1, extra=frozenset()):
    check_transaction(t, ME.pubkey(), cap, extra)


def test_pumpportal_buy_shape_passes():
    # compute budget, create token account, pump.fun buy, PumpPortal's 0.5% fee
    check(tx(budget(), ix(ATA, b"\x01", [ME.pubkey()]), pump_buy(), sol(500_000)), cap=0.115)


def test_jupiter_swap_shape_passes():
    close_to_me = ix(TOKEN, bytes([9]), [WSOL_ATA, ME.pubkey(), ME.pubkey()])
    wrap = transfer(TransferParams(from_pubkey=ME.pubkey(), to_pubkey=WSOL_ATA, lamports=100_000_000))
    check(tx(budget(), ix(ATA, b"\x01", [ME.pubkey()]), wrap, ix(TOKEN, bytes([17]), [WSOL_ATA]),
             ix(JUP, b"\x01" * 16, [ME.pubkey()]), close_to_me), cap=0.115)


@pytest.mark.parametrize("name,bad", [
    ("drain transfer", lambda: tx(budget(), pump_buy(), sol(5 * 10**9))),
    ("assign wallet", lambda: tx(assign(AssignParams(pubkey=ME.pubkey(), owner=ATTACKER)))),
    ("unknown program", lambda: tx(ix(Keypair().pubkey(), b"\x00", [ME.pubkey()]))),
    ("approve", lambda: tx(ix(TOKEN, bytes([4]) + struct.pack("<Q", 2**64 - 1),
                              [WSOL_ATA, ATTACKER, ME.pubkey()]))),
    ("set authority", lambda: tx(ix(TOKEN, bytes([6, 2, 1]) + bytes(ATTACKER),
                                    [WSOL_ATA, ME.pubkey()]))),
    ("token transfer out", lambda: tx(ix(TOKEN, bytes([3]) + struct.pack("<Q", 10**9),
                                         [WSOL_ATA, ATTACKER, ME.pubkey()]))),
    ("close to attacker", lambda: tx(ix(TOKEN, bytes([9]), [WSOL_ATA, ATTACKER, ME.pubkey()]))),
    ("someone else pays", lambda: tx(pump_buy(), payer=ATTACKER, signers=None)),
])
def test_drains_are_refused(name, bad):
    try:
        t = bad()
    except Exception:
        # a transaction that can't even be signed by us alone (e.g. another payer): build it
        # unsigned-compatible by signing with both keys
        other = Keypair()
        t = VersionedTransaction(MessageV0.try_compile(other.pubkey(), [pump_buy()], [],
                                                       Hash.new_unique()), [other, ME])
    with pytest.raises(UnsafeTransaction):
        check(t)


def test_a_second_required_signer_is_refused():
    other = Keypair()
    t = VersionedTransaction(MessageV0.try_compile(
        ME.pubkey(), [ix(PUMP, b"\x01", [ME.pubkey(), other.pubkey()]),
                      Instruction(PUMP, b"\x02", [AccountMeta(other.pubkey(), True, True)])],
        [], Hash.new_unique()), [ME, other])
    with pytest.raises(UnsafeTransaction, match="another signer"):
        check(t)


def test_extra_programs_can_be_allowed_from_env():
    new = Keypair().pubkey()
    t = tx(ix(new, b"\x00", [ME.pubkey()]))
    with pytest.raises(UnsafeTransaction):
        check(t)
    check(t, extra=frozenset({str(new)}))


async def test_refused_pumpportal_sell_falls_back_to_jupiter():
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    ex, rpc = live_executor(load_config(None))
    ex.kp = ME
    ex.pubkey = str(ME.pubkey())
    rpc.balance_raw = 1_000_000
    evil = bytes(tx(pump_buy(), sol(10 * 10**9)))  # "sell" that drains 10 SOL

    async def pp(*a, **k):
        return evil
    ex._pumpportal_tx = pp
    used = []

    class Jup:
        async def quote(self, *a, **k):
            used.append("jupiter")
            return {"outAmount": "1"}

        async def swap_tx(self, *a, **k):
            return b"jupiter-tx"
    ex.jupiter = Jup()

    async def confirm(sig, timeout=90):
        return True
    rpc.confirm = confirm
    await ex.sell("M", 1.0, True, pump=True, curve=None, value_sol=0.5)
    assert used == ["jupiter"]  # the drain was never signed and sent


async def test_refused_buy_is_a_plain_failure():
    from tests.test_hardening import live_executor
    from sniper.config import load_config
    from sniper.execution.executors import LiveExecutor, NotSent
    from sniper.models import Candidate
    ex, rpc = live_executor(load_config(None))
    ex.kp = ME
    ex.pubkey = str(ME.pubkey())
    ex._sign = LiveExecutor._sign.__get__(ex)  # the real signing path, with the guard
    evil = bytes(tx(pump_buy(), sol(10 * 10**9)))

    async def pp(*a, **k):
        return evil
    ex._pumpportal_tx = pp
    with pytest.raises(NotSent, match="refused"):
        await ex.buy(Candidate(chain="solana", mint="M", source="pumpfun", route="pump"), 0.1, None)
    assert ex.sender.sent == 0
