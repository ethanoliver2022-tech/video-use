"""Check a transaction built by PumpPortal or Jupiter before the wallet's signature goes out.

The bot signs swaps those services build. If one of them were ever compromised it could
hand back a transaction that drains the wallet instead of swapping. This guard rejects
anything a real swap never needs, so a hostile transaction is refused, not signed:

- the bot's wallet must be the fee payer and the only signer;
- only known programs may be called (a swap's DEX hops run inside Jupiter or pump.fun);
- no system instruction that could hand the wallet over (Assign, nonce authority);
- no token Approve / SetAuthority, no top-level token transfer out of the wallet, and no
  account close that pays anyone but the wallet;
- the SOL moved out by top-level transfers is capped to what the trade needs (the swap
  amount on a buy, the PumpPortal fee on a sell).

Programs a future pump.fun or PumpPortal update starts using can be allowed without a code
change through EXTRA_ALLOWED_PROGRAMS in .env (a trusted file Telegram can't change).
"""
from __future__ import annotations

import struct

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

ALLOWED_PROGRAMS = {
    SYSTEM,
    "ComputeBudget111111111111111111111111111111",
    TOKEN,
    TOKEN_2022,
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",   # associated token accounts
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",    # memo
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",    # memo (v1)
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",    # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",    # PumpSwap AMM (graduated tokens)
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",    # pump.fun fees
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",    # Jupiter v6 aggregator
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium AMM v4 (older migrations)
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",   # Raydium CPMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",    # Raydium LaunchLab
    "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95",    # Lighthouse (assertions only)
}
for _p in ALLOWED_PROGRAMS:  # fail at import on a typo, never at trade time
    Pubkey.from_string(_p)

# system program instructions a swap can use: create account (0), transfer (2), create
# with seed (3), allocate (8, 9). Everything else (Assign, nonce authority...) is refused.
SYSTEM_OK = {0, 2, 3, 8, 9}
# token instructions that hand control to someone else
TOKEN_APPROVE, TOKEN_SET_AUTHORITY, TOKEN_APPROVE_CHECKED = 4, 6, 13
TOKEN_TRANSFER, TOKEN_TRANSFER_CHECKED, TOKEN_CLOSE = 3, 12, 9


class UnsafeTransaction(ValueError):
    """The built transaction does something a swap never needs; it was not sent."""


def _lamports_out(data: bytes, kind: int) -> int:
    """SOL a system instruction moves out of its funding account."""
    if kind in (0, 2):  # CreateAccount / Transfer: u32 kind, u64 lamports
        return struct.unpack_from("<Q", data, 4)[0]
    if kind == 3:  # CreateAccountWithSeed: kind, base(32), seed(u64 len + bytes), lamports
        seed_len = struct.unpack_from("<Q", data, 36)[0]
        return struct.unpack_from("<Q", data, 44 + seed_len)[0]
    return 0


def check_transaction(tx: VersionedTransaction, owner: Pubkey, max_sol_out: float,
                      extra_programs: frozenset[str] = frozenset()) -> None:
    """Raise UnsafeTransaction unless `tx` looks like a swap for `owner`."""
    msg = tx.message
    keys = list(msg.account_keys)  # program ids are always static keys, never from lookups
    if not keys or keys[0] != owner:
        raise UnsafeTransaction("the fee payer isn't the bot's wallet")
    if msg.header.num_required_signatures != 1:
        raise UnsafeTransaction("it needs another signer besides the bot's wallet")
    allowed = ALLOWED_PROGRAMS | set(extra_programs)
    moved = 0
    for ix in msg.instructions:
        program = str(keys[ix.program_id_index])
        if program not in allowed:
            raise UnsafeTransaction(f"it calls an unknown program {program}")
        data = bytes(ix.data)
        accts = list(ix.accounts)

        def key(i: int):
            # an account index can point into a lookup table (beyond the static keys):
            # those are never the wallet itself, which is the fee payer (static, index 0)
            return keys[accts[i]] if i < len(accts) and accts[i] < len(keys) else None

        if program == SYSTEM:
            if len(data) < 4:
                raise UnsafeTransaction("malformed system instruction")
            kind = struct.unpack_from("<I", data)[0]
            if kind not in SYSTEM_OK:
                raise UnsafeTransaction(f"system instruction {kind} could hand over the wallet")
            if key(0) == owner:
                try:
                    moved += _lamports_out(data, kind)
                except struct.error:
                    raise UnsafeTransaction("malformed system instruction") from None
        elif program in (TOKEN, TOKEN_2022) and data:
            kind = data[0]
            if kind in (TOKEN_APPROVE, TOKEN_APPROVE_CHECKED, TOKEN_SET_AUTHORITY):
                raise UnsafeTransaction("it would give someone else control of your tokens")
            if kind == TOKEN_TRANSFER and key(2) == owner:
                raise UnsafeTransaction("it transfers your tokens out directly")
            if kind == TOKEN_TRANSFER_CHECKED and key(3) == owner:
                raise UnsafeTransaction("it transfers your tokens out directly")
            if kind == TOKEN_CLOSE and key(1) != owner:
                raise UnsafeTransaction("it closes a token account into someone else's wallet")
    if moved > max_sol_out * 1e9:
        raise UnsafeTransaction(f"it moves {moved / 1e9:.4f} SOL out of the wallet, more than "
                                f"this trade needs ({max_sol_out:.4f})")
