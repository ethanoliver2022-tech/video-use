"""Transaction landing: Jito bundles, multi-RPC broadcast and automatic priority fees.

This is what the paid bots advertise as "turbo mode" / "MEV protection":

* **Jito bundle** — the swap plus a SOL tip to a Jito validator travel as one
  atomic bundle straight to the block engine. Bundles skip the public mempool,
  so nobody can sandwich the trade, and the tip buys a place near the top of the block.
* **Multi-RPC broadcast** — the same signed transaction goes to every RPC you
  configure at the same time, and the fastest one to forward it wins.
* **Auto priority fee** — reads recent prioritization fees from the network and
  pays a chosen percentile, capped, instead of a fixed guess.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from collections import OrderedDict

import httpx
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from ..config import SpeedConfig
from ..solana_rpc import SolanaRpc

log = logging.getLogger(__name__)

JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]
TYPICAL_SWAP_CU = 200_000  # used to convert micro-lamports/CU into a total SOL fee


def tip_transaction(payer: Keypair, tip_sol: float, blockhash) -> VersionedTransaction:
    ix = transfer(TransferParams(
        from_pubkey=payer.pubkey(),
        to_pubkey=Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS)),
        lamports=int(tip_sol * 1e9),
    ))
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], blockhash)
    return VersionedTransaction(msg, [payer])


def fee_from_samples(samples: list[int], percentile: float, cu: int = TYPICAL_SWAP_CU) -> float:
    """micro-lamports-per-CU samples -> total priority fee in SOL at the given percentile."""
    vals = sorted(s for s in samples if s > 0)
    if not vals:
        return 0.0
    idx = min(len(vals) - 1, max(0, int(round(percentile / 100 * (len(vals) - 1)))))
    return vals[idx] * cu / 1e6 / 1e9


class TxSender:
    def __init__(self, cfg: SpeedConfig, rpc: SolanaRpc, http: httpx.AsyncClient,
                 default_priority_fee):
        """default_priority_fee: SOL, or a callable returning it (read live from settings)."""
        self.cfg, self.rpc, self.http = cfg, rpc, http
        self._default_fee = default_priority_fee
        self._fee_cache: tuple[float, float] = (0.0, float("-inf"))  # (value, monotonic time)
        self._extra: tuple[tuple[str, ...], list[SolanaRpc]] = ((), [])
        # swap signature -> its bundle's tip signature; oldest evicted (in-flight are newest)
        self.tip_sigs: OrderedDict[str, str] = OrderedDict()

    @property
    def default_fee(self) -> float:
        f = self._default_fee
        return f() if callable(f) else f

    @property
    def extra(self) -> list[SolanaRpc]:
        """Broadcast RPCs, following config changes (presets reload it in place)."""
        urls = tuple(u for u in self.cfg.broadcast_rpcs if u != self.rpc.url)
        if urls != self._extra[0]:
            self._extra = (urls, [SolanaRpc(u, self.http) for u in urls])
        return self._extra[1]

    async def priority_fee(self) -> float:
        if not self.cfg.auto_priority_fee:
            return self.default_fee
        value, at = self._fee_cache
        if time.monotonic() - at < 10:
            return value
        try:
            res = await self.rpc.call("getRecentPrioritizationFees", [[]])
            fee = fee_from_samples([r["prioritizationFee"] for r in res or []],
                                   self.cfg.priority_fee_percentile)
            fee = min(max(fee, self.cfg.min_priority_fee_sol), self.cfg.max_priority_fee_sol)
        except Exception as e:
            log.debug("priority fee estimate failed: %s", e)
            fee = self.default_fee
        self._fee_cache = (fee, time.monotonic())
        return fee

    async def send(self, tx: VersionedTransaction, payer: Keypair) -> str:
        sig = str(tx.signatures[0])
        raw = bytes(tx)
        jobs = []
        use_jito = self.cfg.jito_enabled and bool(self.cfg.jito_block_engines)
        if use_jito:
            tip = tip_transaction(payer, self.cfg.jito_tip_sol, tx.message.recent_blockhash)
            self.tip_sigs[sig] = str(tip.signatures[0])
            while len(self.tip_sigs) > 1000:
                self.tip_sigs.popitem(last=False)
            bundle = [base64.b64encode(raw).decode(), base64.b64encode(bytes(tip)).decode()]
            jobs += [self._send_bundle(url, bundle) for url in self.cfg.jito_block_engines]
        if not use_jito or self.cfg.jito_also_send_rpc:
            jobs += [r.send_raw_transaction(raw) for r in [self.rpc, *self.extra]]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        ok = [r for r in results if not isinstance(r, Exception)]
        if not ok and use_jito and not self.cfg.jito_also_send_rpc:
            # Jito unreachable / rate limited: getting the trade out matters more than
            # MEV protection (think: exiting a rug), so fall back to plain RPC.
            log.warning("jito submission failed (%s); sending through RPC instead", results[0])
            results = await asyncio.gather(
                *[r.send_raw_transaction(raw) for r in [self.rpc, *self.extra]],
                return_exceptions=True)
            ok = [r for r in results if not isinstance(r, Exception)]
        if not ok:
            raise RuntimeError(f"every submission path failed: {results[0] if results else '-'}")
        for r in results:
            if isinstance(r, Exception):
                log.debug("one submission path failed: %s", r)
        return sig

    async def _send_bundle(self, engine: str, bundle: list[str]) -> str:
        resp = await self.http.post(f"{engine.rstrip('/')}/api/v1/bundles", json={
            "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
            "params": [bundle, {"encoding": "base64"}],
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"jito {engine}: {data['error']}")
        return data.get("result", "")
