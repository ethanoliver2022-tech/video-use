"""Hot-wallet loading. Use a dedicated wallet holding only what you can afford to lose."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import base58
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction


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


class WalletManager:
    """Hot wallet that can be created, imported and exported from Telegram.

    The key lives in <data_dir>/wallet.key (permissions 0600). SOLANA_PRIVATE_KEY in
    .env takes precedence and locks the wallet against changes from chat.
    Replacing a wallet never deletes the old key: it is moved to a timestamped backup.
    """

    def __init__(self, data_dir: str, env_secret: str = ""):
        self.dir = Path(data_dir)
        self.path = self.dir / "wallet.key"
        self.env_secret = env_secret.strip()

    @property
    def from_env(self) -> bool:
        return bool(self.env_secret)

    def keypair(self) -> Optional[Keypair]:
        if self.env_secret:
            return load_keypair(self.env_secret)
        if self.path.exists():
            return load_keypair(self.path.read_text())
        return None

    def _save(self, kp: Keypair) -> Keypair:
        if self.from_env:
            raise PermissionError("wallet is set by SOLANA_PRIVATE_KEY in .env; change it there")
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.time_ns()
        tmp = self.dir / f"wallet.key.new-{stamp}"
        _write_secret(tmp, base58.b58encode(bytes(kp)).decode())
        if self.path.exists():  # keep the old key: copy it aside first
            _write_secret(self.dir / f"wallet.key.bak-{stamp}", self.path.read_text())
        os.replace(tmp, self.path)  # atomic: wallet.key is always the old or the new key
        return kp

    def create(self) -> Keypair:
        return self._save(Keypair())

    def import_secret(self, secret: str) -> Keypair:
        return self._save(load_keypair(secret))

    def export(self) -> str:
        kp = self.keypair()
        if not kp:
            raise ValueError("no wallet yet")
        return base58.b58encode(bytes(kp)).decode()


def _write_secret(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def transfer_tx(payer: Keypair, to: str, lamports: int, blockhash) -> VersionedTransaction:
    ix = transfer(TransferParams(from_pubkey=payer.pubkey(), to_pubkey=Pubkey.from_string(to),
                                 lamports=lamports))
    return VersionedTransaction(MessageV0.try_compile(payer.pubkey(), [ix], [], blockhash), [payer])
