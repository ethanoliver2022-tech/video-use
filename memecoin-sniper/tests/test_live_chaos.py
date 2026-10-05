"""Live-mode chaos: the real LiveExecutor + engine against a simulated chain where
transactions randomly land, fail on-chain, expire, or return ambiguous errors."""
import asyncio
import collections
import json
import logging
import random

import pytest
from solders.keypair import Keypair

from sniper.config import load_config
from sniper.engine import Engine
from sniper.models import SafetyReport
from sniper.solana_rpc import RpcError, TxFailed


TOTALS: collections.Counter = collections.Counter()


class FakeChain:
    """Wallet state + transaction outcomes. Changes apply the moment a tx 'lands'."""

    def __init__(self, rng: random.Random, owner: str):
        self.rng, self.owner = rng, owner
        self.sol = 1000.0
        self.tokens: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.txs: dict[str, dict] = {}
        self.n = 0
        self.reliable = False
        self.sold_more_than_held = 0

    def price(self, mint):
        return self.prices.setdefault(mint, self.rng.uniform(1e-7, 1e-5))

    # ---- sender ----
    async def send(self, tx_bytes, payer):
        self.n += 1
        sig = f"sig{self.n}"
        intent = json.loads(tx_bytes)
        r = 1.0 if self.reliable else self.rng.random()
        outcome = ("ok" if r > 0.30 or self.reliable else "failed" if r > 0.20 else
                   "expired" if r > 0.10 else "landed_unclear" if r > 0.05 else "lost_unclear")
        pre_sol, pre_tok = self.sol, self.tokens.get(intent["mint"], 0.0)
        if outcome in ("ok", "landed_unclear"):
            if not self._apply(intent):
                outcome = "failed"
        self.txs[sig] = {"outcome": outcome, "mint": intent["mint"], "pre_sol": pre_sol,
                         "post_sol": self.sol, "pre_tok": pre_tok,
                         "post_tok": self.tokens.get(intent["mint"], 0.0)}
        return sig

    async def priority_fee(self):
        return 0.0001

    def _apply(self, intent) -> bool:
        mint, p = intent["mint"], self.price(intent["mint"])
        if intent["action"] == "buy":
            sol = float(intent["amount"])
            if sol > self.sol:
                return False
            self.sol -= sol
            self.tokens[mint] = self.tokens.get(mint, 0.0) + sol / p
            return True
        held = self.tokens.get(mint, 0.0)
        amount = held if intent["amount"] == "100%" else float(intent["amount"])
        if amount > held * (1 + 1e-9) or held <= 0:
            self.sold_more_than_held += 1
            return False  # the chain rejects it
        self.tokens[mint] = held - amount
        self.sol += amount * p
        return True

    # ---- rpc ----
    async def confirm(self, sig, timeout=90.0):
        o = self.txs[sig]["outcome"]
        if o == "failed":
            raise TxFailed(f"transaction {sig} failed on-chain")
        if o == "expired":
            return False
        if o in ("landed_unclear", "lost_unclear"):
            raise RpcError("getSignatureStatuses: HTTP 502")
        return True

    async def get_transaction(self, sig):
        t = self.txs[sig]
        if t["outcome"] not in ("ok", "landed_unclear"):
            return None
        tb = lambda amt: ([{"mint": t["mint"], "owner": self.owner,  # noqa: E731
                            "uiTokenAmount": {"uiAmount": amt}}] if amt else [])
        return {"transaction": {"message": {"accountKeys": [{"pubkey": self.owner}]}},
                "meta": {"preBalances": [int(t["pre_sol"] * 1e9)],
                         "postBalances": [int(t["post_sol"] * 1e9)],
                         "preTokenBalances": tb(t["pre_tok"]), "postTokenBalances": tb(t["post_tok"])}}

    async def get_token_balance(self, owner, mint):
        if not self.reliable and self.rng.random() < 0.05:
            raise RpcError("getTokenAccountsByOwner: HTTP 429 (rate limited)")
        return self.tokens.get(mint, 0.0)

    async def get_token_balance_raw(self, owner, mint):
        bal = await self.get_token_balance(owner, mint)
        return int(bal * 1e6), 6

    async def get_balance_sol(self, owner):
        return self.sol

    async def get_account_bytes(self, addr):
        return None

    async def call(self, *a, **k):
        return []


class ErrorCatcher(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        if "couldn't be sold" not in record.getMessage():
            self.records.append(record)


def build(tmp_path, rng, chain=None):
    cfg = load_config("config.example.yaml")
    cfg.data_dir = str(tmp_path)
    cfg.pumpportal_api_key = "k"
    cfg.entry.confirm_seconds = 0
    cfg.trading.daily_loss_limit_sol = 1e9
    cfg.trading.cooldown_after_loss_seconds = 0
    cfg.trading.max_open_positions = 4
    cfg.exits.max_hold_seconds = rng.choice([1, 3600])
    from sniper.execution.wallet import WalletManager
    wm = WalletManager(cfg.data_dir)
    if wm.keypair() is None:
        wm.create()
    eng = Engine(cfg, live=True)
    chain = chain or FakeChain(rng, eng.own_wallet)

    async def ws(p):
        pass
    eng.stream._send = ws

    async def tg(*a, **k):
        pass
    eng.notifier.telegram = tg
    ex = eng.executor
    eng.rpc = ex.rpc = chain
    ex.sender = chain
    ex._sign = lambda unsigned: unsigned

    async def pp(action, mint, amount, in_sol, slippage_pct=None):
        return json.dumps({"action": action, "mint": mint, "amount": amount}).encode()
    ex._pumpportal_tx = pp

    class FakeJupiter:  # routes through the same simulated chain
        async def quote(self, in_mint, out_mint, amount_ui, slippage, raw_amount=None):
            if in_mint.startswith("So111"):
                return {"action": "buy", "mint": out_mint, "amount": amount_ui}
            amt = raw_amount / 1e6 if raw_amount is not None else amount_ui
            return {"action": "sell", "mint": in_mint, "amount": amt}

        async def out_ui(self, q):
            return q["amount"]

        async def swap_tx(self, q, user, fee):
            return json.dumps(q).encode()
    ex.jupiter = eng.jupiter = FakeJupiter()

    async def ok(c):
        return SafetyReport(passed=True)
    eng.safety.evaluate = ok
    return eng, chain


@pytest.mark.parametrize("seed", range(20))
async def test_live_chaos(tmp_path, seed):
    rng = random.Random(seed)
    catcher = ErrorCatcher()
    logging.getLogger("sniper").addHandler(catcher)
    try:
        eng, chain = build(tmp_path, rng)
        mints = [str(Keypair().pubkey()) + "" for _ in range(8)]
        for step in range(250):
            m = rng.choice(mints)
            r = rng.random()
            if r < 0.25:
                await eng.manual_buy(m, rng.choice([0.05, 0.2]), force=True)
            elif r < 0.45:
                await eng.manual_sell(m, rng.choice([25, 50, 100]))
            elif r < 0.75:  # price moves -> exits
                chain.prices[m] = chain.price(m) * rng.uniform(0.5, 1.8)
                pos = eng.positions.get(m)
                if pos and not pos.closed:
                    pos.update_price(chain.prices[m])
                for p in list(eng.positions.values()):
                    if not p.closed:
                        eng._sell_next_try.pop(p.mint, None)
                        eng._spawn(eng.check_exit(p))
            elif r < 0.8:  # restart
                await eng.settle()
                await eng.http.aclose()
                eng, chain = build(tmp_path, rng, chain)
                await eng.restore()
            await asyncio.sleep(0)
            if step % 20 == 0:
                await eng.settle()
                # no orphans: every token in the wallet belongs to an open position
                for mint, bal in chain.tokens.items():
                    if bal > 1e-6:
                        pos = eng.positions.get(mint)
                        assert pos and not pos.closed, ("orphaned tokens", seed, step, mint, bal)
        # drain: a calm chain, and every position must end up fully sold
        chain.reliable = True
        eng.cfg.exits.max_hold_seconds = 0
        for _ in range(20):
            await eng.settle()
            for p in list(eng.positions.values()):
                if not p.closed:
                    eng._sell_next_try.pop(p.mint, None)
                    await eng.check_exit(p)
        await eng.settle()
        left = {m: b for m, b in chain.tokens.items() if b > 1e-6}
        assert not left, ("tokens left after drain", seed, left)
        assert all(p.closed for p in eng.positions.values()), seed
        assert chain.sold_more_than_held == 0, ("tried to sell more than held", seed)
        assert not eng.store.open_positions()
        # the run must actually have traded through every kind of outcome
        outcomes = collections.Counter(t["outcome"] for t in chain.txs.values())
        assert len(eng.store.events("buy")) >= 10, outcomes
        assert len(eng.store.events("close")) >= 5, outcomes
        TOTALS.update(outcomes)
        await eng.http.aclose()
    finally:
        logging.getLogger("sniper").removeHandler(catcher)
    assert not catcher.records, [r.getMessage() for r in catcher.records][:5]


def test_live_chaos_covered_every_outcome():
    """Runs after the seeds above: together they must hit every kind of chain outcome."""
    for kind in ("ok", "failed", "expired", "landed_unclear", "lost_unclear"):
        assert TOTALS[kind] >= 5, (kind, TOTALS)
