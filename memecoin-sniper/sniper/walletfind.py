"""Wallet finder: who bought a coin early and sold it well.

Paste a coin that ran: this reads its first trades from the chain, takes the wallets that
bought early (leaving out the dev and the launch-block bots), then looks at what each one
took out of this coin, and ranks them by profit. All plain RPC reads, paced."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .copywatch import trades_in

if TYPE_CHECKING:
    from .solana_rpc import ReadPool

log = logging.getLogger(__name__)

MAX_PAGES = 5            # signature pages (1000 each) read back towards the launch
EARLY_TXS = 120          # first transactions read to find the early buyers
MAX_CANDIDATES = 15      # early buyers whose results are worked out
WALLET_SIGS = 300        # each one's newest transactions searched for its sells of the coin
LAUNCH_BOT_SECONDS = 2   # buying this soon after the launch = a bundle or sniper bot


@dataclass
class EarlyBuyer:
    wallet: str
    first_ts: float
    secs_after_launch: float
    sol_in: float = 0.0
    tokens_in: float = 0.0
    sol_out: float = 0.0
    tokens_out: float = 0.0
    holding: Optional[float] = None
    sigs: set = field(default_factory=set)
    launch_known: bool = True

    @property
    def pnl(self) -> float:
        return self.sol_out - self.sol_in

    @property
    def multiple(self) -> float:
        return self.sol_out / self.sol_in if self.sol_in else 0.0

    @property
    def bot_like(self) -> bool:
        return self.launch_known and self.secs_after_launch <= LAUNCH_BOT_SECONDS


@dataclass
class FinderResult:
    mint: str
    launch_ts: Optional[float]
    reached_launch: bool
    buyers: list[EarlyBuyer]
    looked_at: int

    def text(self) -> str:
        if not self.buyers:
            return "No early buyers found for that coin."
        lines = []
        if not self.reached_launch:
            lines.append("(very busy coin: only its oldest reachable trades were read, which may "
                         "not be the very first ones)")
        for i, b in enumerate(self.buyers, 1):
            when = f"{b.secs_after_launch:.0f}s" if b.secs_after_launch < 120 else \
                f"{b.secs_after_launch / 60:.0f}m"
            when = f"{when} after launch" if b.launch_known else "early"
            held = "" if not b.holding else " · may still hold some"
            flag = " ⚠️ bot-like (bought at launch)" if b.bot_like else ""
            lines.append(f"{i}. {b.wallet[:4]}…{b.wallet[-4:]}  in {b.sol_in:.2f} → out "
                         f"{b.sol_out:.2f} SOL ({b.pnl:+.2f}, {b.multiple:.1f}x) · bought "
                         f"{when}{held}{flag}")
        lines.append("Profit counts the sells found in each wallet's newest transactions. Tap "
                     "one to add it: its 3-day wallet check shows if it's good beyond this coin.")
        return "\n".join(lines)


async def find_wallets(rpc: "ReadPool", mint: str, exclude: set[str] = frozenset(),
                       top: int = 8) -> FinderResult:
    # 1) the coin's signatures, back towards its launch
    sigs: list[dict] = []
    before = None
    reached = False
    for _ in range(MAX_PAGES):
        opts: dict = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        page = await rpc.call("getSignaturesForAddress", [mint, opts]) or []
        sigs += page
        if len(page) < 1000:
            reached = True
            break
        before = page[-1]["signature"]
    ok = [s for s in sigs if s.get("err") is None]
    if not ok:
        return FinderResult(mint, None, reached, [], 0)
    ok.sort(key=lambda s: s.get("blockTime") or 0)
    launch = ok[0].get("blockTime") if reached else None
    all_sigs = {s["signature"] for s in ok}

    # 2) early buyers: the signer of each early transaction, if it bought this coin
    early = ok[1:EARLY_TXS + 1] if reached else ok[:EARLY_TXS]   # [0] is the launch itself
    creator = None
    buyers: dict[str, EarlyBuyer] = {}
    sem = asyncio.Semaphore(3)

    async def read(sig: str) -> Optional[dict]:
        async with sem:
            try:
                return await rpc.call("getTransaction", [sig, {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed"}])
            except Exception as e:
                log.debug("wallet finder: tx %s unreadable: %s", sig[:8], e)
                return None

    if reached:
        first = await read(ok[0]["signature"])
        creator = _signer(first) if first else None
    txs = await asyncio.gather(*(read(s["signature"]) for s in early))
    base = launch or (early[0].get("blockTime") if early else time.time()) or time.time()
    for s, tx in zip(early, txs):
        who = _signer(tx) if tx else None
        if not who or who == creator or who in exclude:
            continue
        for t in trades_in(tx, who, s["signature"]):
            if t["mint"] != mint or t["txType"] != "buy":
                continue
            ts = float(s.get("blockTime") or base)
            b = buyers.get(who)
            if b is None:
                b = buyers[who] = EarlyBuyer(who, ts, max(0.0, ts - base),
                                             launch_known=launch is not None)
            b.sol_in += t["solAmount"]
            b.tokens_in += t["tokenAmount"]
            b.sigs.add(s["signature"])
    picked = sorted(buyers.values(), key=lambda b: b.first_ts)[:MAX_CANDIDATES]

    # 3) what each took out: its own recent transactions that touched this coin
    async def settle(b: EarlyBuyer) -> None:
        try:
            mine = await rpc.call("getSignaturesForAddress",
                                  [b.wallet, {"limit": WALLET_SIGS, "commitment": "confirmed"}]) or []
        except Exception:
            return
        later = [s["signature"] for s in mine
                 if s.get("err") is None and s["signature"] in all_sigs
                 and s["signature"] not in b.sigs]
        for sig in later:
            tx = await read(sig)
            for t in trades_in(tx or {}, b.wallet, sig):
                if t["mint"] != mint:
                    continue
                if t["txType"] == "sell":
                    b.sol_out += t["solAmount"]
                    b.tokens_out += t["tokenAmount"]
                else:
                    b.sol_in += t["solAmount"]
                    b.tokens_in += t["tokenAmount"]
        b.holding = max(0.0, b.tokens_in - b.tokens_out)
    await asyncio.gather(*(settle(b) for b in picked))
    ranked = sorted(picked, key=lambda b: (b.bot_like, -b.pnl))[:top]
    return FinderResult(mint, launch, reached, ranked, len(early))


def _signer(tx: dict) -> Optional[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return None
    k = keys[0]
    return k["pubkey"] if isinstance(k, dict) else k
