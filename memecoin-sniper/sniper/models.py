"""Core data types shared across scanners, safety checks, execution and exits."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

SOL_MINT = "So11111111111111111111111111111111111111112"
PUMP_TOTAL_SUPPLY = 1_000_000_000  # every pump.fun token mints exactly 1B (6 decimals)


@dataclass
class Candidate:
    """A token a scanner thinks is worth looking at."""

    chain: str                      # "solana", "base", "bsc", "eth", ...
    mint: str                       # token address
    source: str                     # "pumpfun", "pumpfun-migration", "geckoterminal", "dexscreener"
    symbol: str = ""
    name: str = ""
    creator: Optional[str] = None   # deployer wallet when known
    created_at: float = field(default_factory=time.time)
    pool: Optional[str] = None      # pool / pair address when known
    liquidity_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    creator_initial_buy_tokens: Optional[float] = None
    # pump.fun bonding-curve virtual reserves (UI units) at discovery time
    v_sol: Optional[float] = None
    v_tokens: Optional[float] = None
    url: Optional[str] = None

    @property
    def on_bonding_curve(self) -> bool:
        return self.source == "pumpfun"

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)


@dataclass
class SafetyReport:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why it failed
    notes: list[str] = field(default_factory=list)     # informational

    def fail(self, reason: str) -> None:
        self.passed = False
        self.reasons.append(reason)


@dataclass
class Fill:
    """Result of an executed (or simulated) swap."""

    tokens: float          # UI units of the memecoin bought or sold
    sol: float             # SOL spent (buy) or received (sell), net of fees where known
    signature: str = "paper"


@dataclass
class TradeTick:
    side: str              # "buy" | "sell"
    trader: str
    sol: float
    ts: float


@dataclass
class Position:
    mint: str
    symbol: str
    source: str
    creator: Optional[str]
    entry_price: float                 # SOL per token
    tokens_initial: float
    tokens_remaining: float
    sol_in: float
    opened_at: float = field(default_factory=time.time)
    sol_out: float = 0.0
    last_price: float = 0.0
    peak_price: float = 0.0
    last_update: float = field(default_factory=time.time)
    tp_levels_hit: set[int] = field(default_factory=set)
    recent_trades: deque = field(default_factory=lambda: deque(maxlen=50))
    dev_sold: bool = False
    kol_bought: list[str] = field(default_factory=list)
    kol_exit_done: bool = False
    migrated: bool = False
    closed: bool = False
    close_reason: str = ""

    def __post_init__(self) -> None:
        if not self.last_price:
            self.last_price = self.entry_price
        if not self.peak_price:
            self.peak_price = self.entry_price

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.last_price / self.entry_price - 1.0) * 100.0

    @property
    def drawdown_from_peak_pct(self) -> float:
        if self.peak_price <= 0:
            return 0.0
        return (1.0 - self.last_price / self.peak_price) * 100.0

    @property
    def realized_pnl_sol(self) -> float:
        """Only meaningful once closed; while open it ignores unsold tokens."""
        return self.sol_out - self.sol_in

    def update_price(self, price: float, ts: Optional[float] = None) -> None:
        if price <= 0:
            return
        self.last_price = price
        self.peak_price = max(self.peak_price, price)
        self.last_update = ts or time.time()
