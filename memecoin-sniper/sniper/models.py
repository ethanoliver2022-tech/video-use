"""Core data types shared across scanners, safety checks, execution and exits."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

import math

SOL_MINT = "So11111111111111111111111111111111111111112"


def num(v, allow_zero: bool = False) -> Optional[float]:
    """A finite, non-negative float from untrusted data, or None. Rejects NaN, Infinity,
    negatives, booleans and anything that isn't a number or a numeric string."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v.strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0 or (f == 0 and not allow_zero):
        return None
    return f
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
    uri: Optional[str] = None       # token metadata JSON (pump.fun)
    route: str = "jupiter"          # "pump" (PumpPortal, curve or pump-amm) | "jupiter"
    leader: Optional[str] = None    # copy-trade wallet that triggered this, if any
    buy_sol: Optional[float] = None # per-candidate size override (copy trades, manual buys)
    force: bool = False             # manual buy: skip filters
    trigger: str = ""               # why it was picked: "dev", "keyword:<word>", "limit", ...
    queued_at: float = 0.0            # monotonic time it entered the buy queue
    # a buy transaction built while the filters ran (live pump.fun only), and its size
    prebuilt: Optional[object] = field(default=None, repr=False, compare=False)
    prebuilt_sol: float = 0.0
    window_s: float = 0.0             # seconds deliberately spent in a confirmation window

    @property
    def on_bonding_curve(self) -> bool:
        return self.route == "pump" and self.source != "pumpfun-migration"

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
    from_wallet: bool = False  # tokens is a whole-wallet balance, not this trade's delta
    sol_known: bool = True     # False: the trade landed but its SOL amount couldn't be read
    emptied: bool = False      # the sell took everything left in the wallet
    pre: Optional[float] = None  # buys: tokens of this mint held before (None = unknown)
    timings: dict = field(default_factory=dict)  # seconds per step (live): build, send, confirm


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
    # (time, price) for the live chart; runtime only, like recent_trades
    price_history: deque = field(default_factory=lambda: deque(maxlen=1500))
    dev_sold: bool = False
    whale_dump_pct: float = 0.0  # biggest holder (% of supply) seen dumping since the buy
    kol_bought: list[str] = field(default_factory=list)
    kol_exit_done: bool = False
    initials_taken: bool = False
    migrated: bool = False
    seen_on_curve: bool = False  # held while still on the bonding curve (so a later
    #                              graduation really happened while we held it)
    dev_tokens: Optional[float] = None   # creator's balance at entry (RPC dev-sell detection)
    route: str = "jupiter"
    leader: Optional[str] = None
    leader_sold: bool = False
    # write-ahead of the exit being executed, so a sell that lands while the bot is down is
    # booked as that exit (its TP level / initials / KOL flag) when it restarts
    pending_exit: Optional[dict] = None
    closed: bool = False
    close_reason: str = ""

    def __post_init__(self) -> None:
        if not self.last_price:
            self.last_price = self.entry_price
        if not self.peak_price:
            self.peak_price = self.entry_price
        if not self.price_history and self.entry_price > 0:
            self.price_history.append((self.opened_at, self.entry_price))

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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tp_levels_hit"] = sorted(self.tp_levels_hit)
        d["recent_trades"] = []  # transient flow data isn't worth persisting
        d.pop("price_history", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d = dict(d)
        d["tp_levels_hit"] = set(d.get("tp_levels_hit", []))
        d.pop("recent_trades", None)
        d.pop("price_history", None)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def update_price(self, price: float, ts: Optional[float] = None) -> None:
        """Record a new price. `last_update` only moves when the price does, so polling a
        token nobody trades still lets the dead-token exit fire."""
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            return  # never let a bad quote or feed message corrupt the position
        if abs(price - self.last_price) > self.last_price * 1e-9:
            self.last_update = ts or time.time()
        self.last_price = price
        self.peak_price = max(self.peak_price, price)
        now = ts or time.time()
        h = self.price_history
        if h and now - h[-1][0] < 1.0:
            h[-1] = (h[-1][0], price)   # at most one point a second, keeping the latest
        else:
            h.append((now, price))
