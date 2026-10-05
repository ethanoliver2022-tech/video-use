"""Settings that can be changed from Telegram, with parsing and validation.

Changes are stored as overrides in the database and re-applied on top of
config.yaml + preset at every start, so the chat is the source of truth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Optional

from .config import Config, TakeProfitLevel


@dataclass(frozen=True)
class Setting:
    key: str            # "section.field"
    label: str
    kind: str           # float | int | bool | tp | wallets | words | choice
    lo: float = 0.0
    hi: float = 1e9
    unit: str = ""
    help: str = ""
    options: tuple = ()  # for kind == "choice"


SETTINGS: list[Setting] = [
    # trading
    Setting("trading.buy_amount_sol", "Buy size", "float", 0.001, 100, "SOL"),
    Setting("trading.slippage_pct", "Slippage", "float", 0.5, 100, "%"),
    Setting("trading.max_open_positions", "Max positions", "int", 1, 50),
    Setting("trading.daily_loss_limit_sol", "Daily loss limit", "float", 0, 1000, "SOL",
            help="stop new buys for the rest of the UTC day after this much loss; 0 = no limit"),
    Setting("trading.min_sol_reserve", "SOL reserve", "float", 0, 100, "SOL"),
    # exits
    Setting("exits.take_profit", "Take profits", "tp", help="format: up%:sell%, e.g. 40:40,100:30,250:20"),
    Setting("exits.stop_loss_pct", "Stop loss", "float", 1, 99, "%"),
    Setting("exits.trailing_activate_pct", "Trailing arms at", "float", 0, 10000, "%"),
    Setting("exits.trailing_stop_pct", "Trailing stop", "float", 1, 99, "%"),
    Setting("exits.breakeven_after_first_tp", "Breakeven after TP", "bool"),
    Setting("exits.max_hold_seconds", "Max hold", "int", 10, 86400 * 7, "s"),
    Setting("exits.exit_on_dev_sell", "Exit on dev sell", "bool"),
    Setting("exits.sell_on_migration", "Sell on migration", "bool",
            help="sell everything the moment a token graduates off the pump.fun curve"),
    Setting("exits.kol_wallets", "KOL wallets", "wallets", help="comma-separated addresses, or 'none'"),
    Setting("exits.sell_initials_at_pct", "Sell initials at", "float", 0, 100000, "%",
            help="profit % at which to take your SOL back, e.g. 100 = at 2x; 0 = off"),
    Setting("exits.moonbag_pct", "Moonbag", "float", 0, 50, "%",
            help="% of the original bag to keep after taking profit; 0 = off"),
    Setting("exits.moonbag_trailing_pct", "Moonbag trailing", "float", 5, 99, "%"),
    Setting("exits.moonbag_max_hold_hours", "Moonbag max hold", "float", 1, 24 * 30, "h"),
    # snipers
    Setting("discovery.auto_snipe", "Snipe mode", "choice", options=("all", "targeted", "off"),
            help="all = any launch passing filters; targeted = watched devs + keywords only"),
    Setting("discovery.snipe_keywords", "Keywords", "words",
            help="comma-separated words to match in name/ticker, or 'none'"),
    Setting("discovery.dev_watchlist", "Dev watchlist", "wallets",
            help="comma-separated dev wallets whose next launch to buy instantly, or 'none'"),
    Setting("discovery.dev_snipe_sol", "Dev snipe size", "float", 0, 100, "SOL",
            help="0 = your normal buy size"),
    Setting("discovery.dev_snipe_skip_filters", "Dev snipes skip filters", "bool"),
    # entry / filters
    Setting("discovery.pumpfun_new_tokens", "Snipe new launches", "bool"),
    Setting("discovery.pumpfun_migrations", "Snipe migrations", "bool"),
    Setting("entry.confirm_seconds", "Confirm window", "float", 0, 120, "s"),
    Setting("entry.min_unique_buyers", "Min early buyers", "int", 0, 1000),
    Setting("filters.max_creator_initial_buy_pct", "Max dev buy", "float", 0, 100, "%"),
    Setting("filters.max_top10_holder_pct", "Max top-10 hold", "float", 1, 100, "%"),
    Setting("filters.min_socials", "Min socials", "int", 0, 3),
    Setting("filters.max_creator_launches_24h", "Max dev launches/24h", "int", 0, 1000,
            help="skip devs who launched more than this many tokens in 24h; 0 = off"),
    Setting("filters.min_liquidity_usd", "Min liquidity", "float", 0, 1e9, "$"),
    Setting("filters.honeypot_check", "Honeypot check", "bool"),
    # speed
    Setting("speed.jito_enabled", "Jito bundles", "bool"),
    Setting("speed.jito_tip_sol", "Jito tip", "float", 0.00001, 0.1, "SOL"),
    Setting("speed.auto_priority_fee", "Auto priority fee", "bool"),
    Setting("speed.max_priority_fee_sol", "Max priority fee", "float", 0, 0.1, "SOL"),
    # momentum scanner
    Setting("discovery.momentum_enabled", "Momentum scanner", "bool",
            help="alert on Solana tokens pumping right now (free data, ~10-30s behind)"),
    Setting("discovery.momentum_action", "On a signal", "choice", options=("alert", "buy"),
            help="alert = just tell me; buy = also buy it (your filters and limits still apply)"),
    Setting("discovery.momentum_min_change_5m_pct", "Min rise in 5 min", "float", 5, 10000, "%"),
    Setting("discovery.momentum_min_volume_5m_usd", "Min 5-min volume", "float", 0, 1e9, "$"),
    Setting("discovery.momentum_min_buyers_5m", "Min buyers in 5 min", "int", 0, 100000),
    Setting("discovery.momentum_min_buy_ratio", "Min buys per sell", "float", 0.5, 100),
    Setting("discovery.momentum_min_liquidity_usd", "Min liquidity", "float", 0, 1e9, "$"),
    Setting("discovery.momentum_buy_sol", "Momentum buy size", "float", 0, 100, "SOL",
            help="0 = your normal buy size"),
    Setting("discovery.momentum_alerts_per_hour", "Max signals per hour", "int", 1, 500),
    Setting("discovery.momentum_poll_seconds", "Check every", "int", 15, 3600, "s"),
    # copy trade
    Setting("copytrade.enabled", "Copy trading", "bool"),
    Setting("copytrade.min_leader_buy_sol", "Min leader buy", "float", 0, 1000, "SOL"),
]
BY_KEY = {s.key: s for s in SETTINGS}
GROUPS = {
    "snipe": "🎯 Snipers",
    "trading": "💰 Trading",
    "exits": "🚪 Exits",
    "entry": "🔍 Entry & filters",
    "speed": "⚡ Speed",
    "momentum": "🚀 Momentum",
    "copytrade": "👥 Copy trade",
}


SNIPE_KEYS = {"discovery.auto_snipe", "discovery.snipe_keywords", "discovery.dev_watchlist",
              "discovery.dev_snipe_sol", "discovery.dev_snipe_skip_filters",
              "discovery.pumpfun_new_tokens", "discovery.pumpfun_migrations"}


def group_of(s: Setting) -> str:
    if s.key in SNIPE_KEYS:
        return "snipe"
    if s.key.startswith("discovery.momentum_"):
        return "momentum"
    section = s.key.split(".")[0]
    return "entry" if section in ("entry", "filters", "discovery") else section


def get_value(cfg: Config, key: str) -> Any:
    section, name = key.split(".")
    return getattr(getattr(cfg, section), name)


def format_value(s: Setting, v: Any) -> str:
    if s.kind == "bool":
        return "✅ on" if v else "❌ off"
    if s.kind == "tp":
        return ", ".join(f"+{lvl.at_pct:g}%→{lvl.sell_pct:g}%" for lvl in v) or "none"
    if s.kind == "wallets":
        return f"{len(v)} wallet(s)"
    if s.kind == "words":
        return ", ".join(v) if v else "none"
    if s.kind == "choice":
        return str(v)
    return f"{v:g}{s.unit}" if s.unit in ("%", "s") else f"{v:g} {s.unit}".strip()


def parse_value(s: Setting, raw: Any) -> Any:
    """Parse user input (or a stored override) into the field's type. Raises ValueError."""
    if s.kind == "bool":
        if isinstance(raw, bool):
            return raw
        t = str(raw).strip().lower()
        if t in ("1", "on", "true", "yes", "y"):
            return True
        if t in ("0", "off", "false", "no", "n"):
            return False
        raise ValueError("send on or off")
    if s.kind == "tp":
        if isinstance(raw, list):
            return [lvl if isinstance(lvl, TakeProfitLevel) else TakeProfitLevel(**lvl) for lvl in raw]
        if str(raw).strip().lower() in ("none", "off", ""):
            return []
        levels = []
        for part in str(raw).replace(" ", "").split(","):
            at, _, sell = part.partition(":")
            lvl = TakeProfitLevel(float(at.rstrip("%")), float(sell.rstrip("%")))
            if (not math.isfinite(lvl.at_pct) or lvl.at_pct <= 0
                    or not 0 < lvl.sell_pct <= 100):  # also rejects nan / inf
                raise ValueError(f"bad level {part}")
            levels.append(lvl)
        if sum(lvl.sell_pct for lvl in levels) > 100:
            raise ValueError("take-profit sells add up to more than 100%")
        return sorted(levels, key=lambda lvl: lvl.at_pct)
    if s.kind == "choice":
        v = str(raw).strip().lower()
        if v not in s.options:
            raise ValueError("choose one of: " + ", ".join(s.options))
        return v
    if s.kind == "words":
        if isinstance(raw, list):
            return [str(w) for w in raw]
        if str(raw).strip().lower() in ("none", "off", ""):
            return []
        words = [w.strip() for w in str(raw).split(",") if w.strip()]
        if any(len(w) < 2 for w in words):
            raise ValueError("keywords need at least 2 characters")
        return words
    if s.kind == "wallets":
        if isinstance(raw, list):
            return raw
        if str(raw).strip().lower() in ("none", "off", ""):
            return []
        from solders.pubkey import Pubkey
        out = [w.strip() for w in str(raw).split(",") if w.strip()]
        for w in out:
            Pubkey.from_string(w)
        return out
    num = float(str(raw).strip().rstrip("%").replace("SOL", "").replace("$", "").replace(",", ""))
    if not s.lo <= num <= s.hi:
        raise ValueError(f"must be between {s.lo:g} and {s.hi:g}")
    return int(num) if s.kind == "int" else num


def to_storable(s: Setting, v: Any) -> Any:
    if s.kind == "tp":
        return [{"at_pct": lvl.at_pct, "sell_pct": lvl.sell_pct} for lvl in v]
    return v


def apply_setting(cfg: Config, key: str, raw: Any) -> Any:
    s = BY_KEY[key]
    value = parse_value(s, raw)
    section, name = key.split(".")
    setattr(getattr(cfg, section), name, value)
    return value


def apply_overrides(cfg: Config, overrides: dict) -> list[str]:
    """Apply stored overrides; returns keys that were invalid and skipped."""
    bad = []
    for key, raw in overrides.items():
        try:
            apply_setting(cfg, key, raw)
        except (KeyError, ValueError, TypeError):
            bad.append(key)
    return bad


def update_in_place(target: Any, source: Any, skip: Optional[set[str]] = None) -> None:
    """Copy every field of `source` into `target`, recursing into nested dataclasses, so
    components holding a reference to a config section see the new values."""
    for f in fields(target):
        if skip and f.name in skip:
            continue
        new = getattr(source, f.name)
        cur = getattr(target, f.name)
        if is_dataclass(cur) and is_dataclass(new) and not isinstance(cur, TakeProfitLevel):
            update_in_place(cur, new)
        else:
            setattr(target, f.name, new)
