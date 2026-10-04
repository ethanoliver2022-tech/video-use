"""Hot-wallet loading. Use a dedicated wallet holding only what you can afford to lose."""
from __future__ import annotations

import json

import base58
from solders.keypair import Keypair


def load_keypair(secret: str) -> Keypair:
    """Accepts a base58 secret key (Phantom/Solflare export) or a JSON byte array (solana-keygen)."""
    secret = secret.strip()
    if not secret:
        raise ValueError("SOLANA_PRIVATE_KEY is empty — set it in .env (live mode only)")
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_bytes(base58.b58decode(secret))


def new_keypair() -> tuple[str, str]:
    kp = Keypair()
    return str(kp.pubkey()), base58.b58encode(bytes(kp)).decode()
