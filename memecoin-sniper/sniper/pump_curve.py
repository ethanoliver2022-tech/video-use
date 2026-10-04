"""Read pump.fun bonding curves straight from the chain.

Used for prices and graduation detection when no PumpPortal API key is set
(PumpPortal's trade stream is paid), and as a gap filler when the stream is quiet.

Account layout (Anchor): 8-byte discriminator, then little-endian
virtual_token_reserves u64 | virtual_sol_reserves u64 | real_token_reserves u64 |
real_sol_reserves u64 | token_total_supply u64 | complete bool | creator pubkey ...
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from solders.pubkey import Pubkey

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_DECIMALS = 6
_LAYOUT = struct.Struct("<QQQQQ?")  # starts after the 8-byte discriminator


@dataclass
class CurveInfo:
    v_sol: float        # SOL
    v_tokens: float     # tokens (UI units)
    complete: bool      # True once the token has graduated off the curve
    creator: Optional[str] = None

    @property
    def price(self) -> float:
        return self.v_sol / self.v_tokens if self.v_tokens else 0.0


def bonding_curve_address(mint: str) -> str:
    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                         PUMP_PROGRAM)
    return str(pda)


def parse_curve(data: bytes) -> Optional[CurveInfo]:
    if len(data) < 8 + _LAYOUT.size:
        return None
    v_tok, v_sol, _real_tok, _real_sol, _supply, complete = _LAYOUT.unpack_from(data, 8)
    creator = None
    start = 8 + _LAYOUT.size
    if len(data) >= start + 32:
        creator = str(Pubkey.from_bytes(data[start:start + 32]))
    return CurveInfo(v_sol=v_sol / 1e9, v_tokens=v_tok / 10 ** TOKEN_DECIMALS,
                     complete=bool(complete), creator=creator)


async def fetch_curve(rpc, mint: str) -> Optional[CurveInfo]:
    data = await rpc.get_account_bytes(bonding_curve_address(mint))
    return parse_curve(data) if data else None
