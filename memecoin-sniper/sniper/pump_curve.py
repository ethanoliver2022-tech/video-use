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
INITIAL_REAL_TOKENS = 793_100_000  # tokens sold along the curve before it graduates
# A standard pump.fun coin starts at 30 SOL x 1,073,000,000 tokens (virtual) and trades on
# x * y = k, so v_sol * v_tokens stays at this value (rounding nudges it up a hair). Coins
# priced in another coin (USDC...) or with other starting reserves don't: their numbers mean
# something else, and pricing them as SOL would be wrong by orders of magnitude.
STANDARD_K = 30 * 1_073_000_000
K_TOLERANCE = 0.25  # wide: what it must catch is off by 100x+, not by rounding
SOL_MINT_BYTES = bytes(Pubkey.from_string("So11111111111111111111111111111111111111112"))
QUOTE_AT = 8 + 5 * 8 + 1 + 32 + 1 + 1     # ..., creator, is_mayhem_mode, is_cashback_coin
_LAYOUT = struct.Struct("<QQQQQ?")  # starts after the 8-byte discriminator


@dataclass
class CurveInfo:
    v_sol: float        # SOL
    v_tokens: float     # tokens (UI units)
    complete: bool      # True once the token has graduated off the curve
    creator: Optional[str] = None
    real_tokens: float = 0.0  # tokens still for sale on the curve
    sol_quoted: bool = True   # priced in SOL (newer coins can be priced in e.g. USDC)

    @property
    def progress_pct(self) -> float:
        """How far along the bonding curve the token is (100% = graduates)."""
        if self.complete:
            return 100.0
        return max(0.0, min(100.0, (1 - self.real_tokens / INITIAL_REAL_TOKENS) * 100))

    @property
    def price(self) -> float:
        return self.v_sol / self.v_tokens if self.v_tokens else 0.0


def is_standard(v_sol: float, v_tokens: float) -> bool:
    """A standard SOL pump.fun bonding curve (see STANDARD_K)."""
    try:
        return abs(v_sol * v_tokens / STANDARD_K - 1) <= K_TOLERANCE
    except (TypeError, ZeroDivisionError):
        return False


def bonding_curve_address(mint: str) -> str:
    pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                         PUMP_PROGRAM)
    return str(pda)


def parse_curve(data: bytes) -> Optional[CurveInfo]:
    if len(data) < 8 + _LAYOUT.size:
        return None
    v_tok, v_sol, real_tok, _real_sol, _supply, complete = _LAYOUT.unpack_from(data, 8)
    creator = None
    start = 8 + _LAYOUT.size
    if len(data) >= start + 32:
        creator = str(Pubkey.from_bytes(data[start:start + 32]))
    quote = data[QUOTE_AT:QUOTE_AT + 32] if len(data) >= QUOTE_AT + 32 else b""
    sol_quoted = not any(quote) or quote == SOL_MINT_BYTES  # unset on coins from before
    return CurveInfo(v_sol=v_sol / 1e9, v_tokens=v_tok / 10 ** TOKEN_DECIMALS,
                     complete=bool(complete), creator=creator,
                     real_tokens=real_tok / 10 ** TOKEN_DECIMALS, sol_quoted=sol_quoted)


async def fetch_curve(rpc, mint: str) -> Optional[CurveInfo]:
    data = await rpc.get_account_bytes(bonding_curve_address(mint))
    return parse_curve(data) if data else None
