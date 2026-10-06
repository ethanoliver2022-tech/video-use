"""Build pump.fun bonding-curve buys and sells directly, without a third-party builder.

PumpPortal's built trades started routing through an unverified, upgradeable program
instead of calling pump.fun. This builds the same trade the way pump.fun's own instructions
define it: compute budget, the wallet's token account (created if missing), and one call to
the pump.fun program. Every address is derived here from the mint, the curve's creator and
the wallet, so nothing an outside service hands back ends up in the transaction.

pump.fun changes its program now and then (new accounts, new fee rules). That's why the
first transaction built this way is test-run on the chain (simulateTransaction: free, nothing
is sent) before any is used, and why anything unexpected falls back to the other routes.

Account order follows pump.fun's published IDL:
  buy:  global, fee_recipient, mint, bonding_curve, associated_bonding_curve, associated_user,
        user, system_program, token_program, creator_vault, event_authority, program,
        global_volume_accumulator, user_volume_accumulator, fee_config, fee_program
  sell: global, fee_recipient, mint, bonding_curve, associated_bonding_curve, associated_user,
        user, system_program, creator_vault, token_program, event_authority, program,
        fee_config, fee_program
then, for both (pump.fun's fee update): bonding_curve_v2 ["bonding-curve-v2", mint] and a
buyback fee recipient: pump.fun's fee recipient, read from its Global account on the chain.
"""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Optional

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from ..models import SOL_MINT
from ..pump_curve import TOKEN_DECIMALS

P = Pubkey.from_string
PUMP = P("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
FEE_PROGRAM = P("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM = P("11111111111111111111111111111111")
TOKEN = P("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022 = P("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = P("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

BUY = bytes.fromhex("66063d1201daebea")    # buy(amount, max_sol_cost, track_volume)
SELL = bytes.fromhex("33e685a4017f83ad")   # sell(amount, min_sol_output)
CURVE_DISC = bytes.fromhex("17b7f83760d8ac60")
GLOBAL_DISC = bytes.fromhex("a7e8e8b1c86c727f")
FEE_RECIPIENT_AT = 8 + 1 + 32              # discriminator, initialized, authority
CURVE_CREATOR_AT = 8 + 5 * 8 + 1           # discriminator, 5 x u64, complete
CURVE_MAYHEM_AT = CURVE_CREATOR_AT + 32    # is_mayhem_mode (different fee recipients)
CURVE_CASHBACK_AT = CURVE_MAYHEM_AT + 1    # is_cashback_coin (sells take another account)
CURVE_QUOTE_AT = CURVE_CASHBACK_AT + 1     # quote_mint: SOL, or another coin for newer ones
FEE_ESTIMATE = 0.0125                      # protocol + creator fee, for the token estimate
COMPUTE_UNITS = 150_000


def _pda(*seeds: bytes, program: Pubkey = PUMP) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), program)[0]


GLOBAL = _pda(b"global")
EVENT_AUTHORITY = _pda(b"__event_authority")
GLOBAL_VOLUME = _pda(b"global_volume_accumulator")
FEE_CONFIG = _pda(b"fee_config", bytes(PUMP), program=FEE_PROGRAM)


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return _pda(bytes(owner), bytes(token_program), bytes(mint), program=ATA_PROGRAM)


class NotNative(Exception):
    """This trade can't be built directly (graduated, unusual token...): use another route."""


@dataclass
class Curve:
    v_tokens: int      # raw units
    v_sol: int         # lamports
    creator: Pubkey
    token_program: Pubkey


def parse(curve_data: bytes, mint_owner: str) -> Curve:
    if len(curve_data) < CURVE_CREATOR_AT + 32 or curve_data[:8] != CURVE_DISC:
        raise NotNative("not a pump.fun bonding curve")
    v_tok, v_sol, _real_tok, _real_sol, _supply, complete = struct.unpack_from("<QQQQQ?",
                                                                               curve_data, 8)
    if complete:
        raise NotNative("graduated off the curve")
    if len(curve_data) > CURVE_MAYHEM_AT and curve_data[CURVE_MAYHEM_AT] == 1:
        raise NotNative("mayhem-mode token (other fee rules)")
    if len(curve_data) > CURVE_CASHBACK_AT and curve_data[CURVE_CASHBACK_AT] == 1:
        raise NotNative("cashback token (other sell accounts)")
    if len(curve_data) >= CURVE_QUOTE_AT + 32:
        quote = curve_data[CURVE_QUOTE_AT:CURVE_QUOTE_AT + 32]
        if any(quote) and quote != bytes(P(SOL_MINT)):
            raise NotNative("traded against another coin than SOL")
    if v_tok <= 0 or v_sol <= 0:
        raise NotNative("empty curve")
    program = {str(TOKEN): TOKEN, str(TOKEN_2022): TOKEN_2022}.get(mint_owner)
    if program is None:
        raise NotNative("unknown token program")
    creator = Pubkey.from_bytes(curve_data[CURVE_CREATOR_AT:CURVE_CREATOR_AT + 32])
    return Curve(v_tok, v_sol, creator, program)


def tokens_for(curve: Curve, lamports: int) -> int:
    """Raw tokens a buy of `lamports` (fees included) gets on this curve."""
    net = int(lamports / (1 + FEE_ESTIMATE))
    return curve.v_tokens * net // (curve.v_sol + net)


def sol_for(curve: Curve, raw_tokens: int) -> int:
    """Lamports a sell of `raw_tokens` gets on this curve, after fees."""
    gross = curve.v_sol * raw_tokens // (curve.v_tokens + raw_tokens)
    return int(gross * (1 - FEE_ESTIMATE))


def _meta(key: Pubkey, writable: bool = False, signer: bool = False) -> AccountMeta:
    return AccountMeta(key, signer, writable)


def _v2_tail(mint: Pubkey, fee_recipient: Pubkey) -> list[AccountMeta]:
    """What pump.fun now wants after the IDL's accounts (pump-fun/pump-public-docs,
    BREAKING_FEE_RECIPIENT.md): the bonding-curve-v2 account, then one of pump.fun's fee
    recipients as the buyback fee recipient. That one is pump.fun's own fee recipient as read
    from its Global account on the chain: no address here comes from anywhere else."""
    return [_meta(_pda(b"bonding-curve-v2", bytes(mint)), True), _meta(fee_recipient, True)]


def buy_instruction(user: Pubkey, mint: Pubkey, curve: Curve, fee_recipient: Pubkey,
                    amount: int, max_sol_cost: int) -> Instruction:
    bc = _pda(b"bonding-curve", bytes(mint))
    accts = [
        _meta(GLOBAL), _meta(fee_recipient, True), _meta(mint), _meta(bc, True),
        _meta(ata(bc, mint, curve.token_program), True), _meta(ata(user, mint, curve.token_program), True),
        _meta(user, True, True), _meta(SYSTEM), _meta(curve.token_program),
        _meta(_pda(b"creator-vault", bytes(curve.creator)), True), _meta(EVENT_AUTHORITY), _meta(PUMP),
        _meta(GLOBAL_VOLUME, True), _meta(_pda(b"user_volume_accumulator", bytes(user)), True),
        _meta(FEE_CONFIG), _meta(FEE_PROGRAM), *_v2_tail(mint, fee_recipient),
    ]
    return Instruction(PUMP, BUY + struct.pack("<QQ", amount, max_sol_cost) + b"\x00", accts)


def sell_instruction(user: Pubkey, mint: Pubkey, curve: Curve, fee_recipient: Pubkey,
                     amount: int, min_sol_output: int) -> Instruction:
    bc = _pda(b"bonding-curve", bytes(mint))
    accts = [
        _meta(GLOBAL), _meta(fee_recipient, True), _meta(mint), _meta(bc, True),
        _meta(ata(bc, mint, curve.token_program), True), _meta(ata(user, mint, curve.token_program), True),
        _meta(user, True, True), _meta(SYSTEM),
        _meta(_pda(b"creator-vault", bytes(curve.creator)), True), _meta(curve.token_program),
        _meta(EVENT_AUTHORITY), _meta(PUMP), _meta(FEE_CONFIG), _meta(FEE_PROGRAM),
        *_v2_tail(mint, fee_recipient),
    ]
    return Instruction(PUMP, SELL + struct.pack("<QQ", amount, min_sol_output), accts)


def create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey, program: Pubkey) -> Instruction:
    return Instruction(ATA_PROGRAM, bytes([1]), [
        _meta(payer, True, True), _meta(ata(owner, mint, program), True), _meta(owner),
        _meta(mint), _meta(SYSTEM), _meta(program)])


def budget(priority_fee_sol: float) -> list[Instruction]:
    micro = int(priority_fee_sol * 1e9 * 1e6 / COMPUTE_UNITS)  # micro-lamports per unit
    return [set_compute_unit_limit(COMPUTE_UNITS), set_compute_unit_price(max(0, micro))]


def unsigned(payer: Pubkey, ixs: list[Instruction], blockhash) -> bytes:
    msg = MessageV0.try_compile(payer, ixs, [], blockhash)
    return bytes(VersionedTransaction.populate(msg, [Signature.default()]))


class PumpNative:
    def __init__(self, rpc):
        self.rpc = rpc
        self._fee_recipient: Optional[Pubkey] = None

    async def _state(self, mint: Pubkey) -> tuple[Curve, Pubkey, object]:
        want = [str(_pda(b"bonding-curve", bytes(mint))), str(mint)]
        if self._fee_recipient is None:
            want.append(str(GLOBAL))
        accounts, blockhash = await asyncio.gather(self.rpc.get_accounts_raw(want),
                                                   self.rpc.get_latest_blockhash())
        if accounts[0] is None or accounts[1] is None:
            raise NotNative("no bonding curve for this token")
        if self._fee_recipient is None:
            glob = accounts[2]
            if glob is None or len(glob[1]) < FEE_RECIPIENT_AT + 32 or glob[1][:8] != GLOBAL_DISC:
                raise NotNative("couldn't read pump.fun's settings")
            self._fee_recipient = Pubkey.from_bytes(glob[1][FEE_RECIPIENT_AT:FEE_RECIPIENT_AT + 32])
        if accounts[0][0] != str(PUMP):
            raise NotNative("bonding curve not owned by pump.fun")
        return parse(accounts[0][1], accounts[1][0]), self._fee_recipient, blockhash

    async def buy_tx(self, user: Pubkey, mint: str, sol: float, slippage_pct: float,
                     priority_fee_sol: float) -> bytes:
        m = P(mint)
        curve, fee_recipient, blockhash = await self._state(m)
        lamports = int(sol * 1e9)
        amount = tokens_for(curve, lamports)
        if amount <= 0:
            raise NotNative("buy too small")
        max_cost = int(lamports * (1 + max(0.0, slippage_pct) / 100))
        return unsigned(user, [*budget(priority_fee_sol),
                               create_ata_idempotent(user, user, m, curve.token_program),
                               buy_instruction(user, m, curve, fee_recipient, amount, max_cost)],
                        blockhash)

    async def sell_tx(self, user: Pubkey, mint: str, raw_tokens: int, slippage_pct: float,
                      priority_fee_sol: float) -> bytes:
        m = P(mint)
        curve, fee_recipient, blockhash = await self._state(m)
        floor = int(sol_for(curve, raw_tokens) * (1 - min(100.0, max(0.0, slippage_pct)) / 100))
        return unsigned(user, [*budget(priority_fee_sol),
                               sell_instruction(user, m, curve, fee_recipient, raw_tokens, max(0, floor))],
                        blockhash)


__all__ = ["PumpNative", "NotNative", "TOKEN_DECIMALS"]
