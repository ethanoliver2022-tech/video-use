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
    Setting("notify.utc_offset_hours", "Your time zone", "float", -12, 14, "h",
            help="hours from UTC, e.g. -5 for US Eastern (winter) or -4 (summer): the daily "
                 "recap arrives at your midnight and covers your day"),
    Setting("trading.slippage_pct", "Buy slippage", "float", 0.5, 100, "%",
            help="how far the price may move up on you while a buy lands"),
    Setting("trading.sell_slippage_pct", "Sell slippage", "float", 0, 100, "%",
            help="how far the price may move down on you while a sell lands; 0 = same as "
                 "buy slippage. Failed sells retry with more, and rug exits start higher"),
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
    Setting("exits.exit_on_whale_sell_pct", "Exit on whale dump", "float", 0, 100, "%",
            help="sell all when a wallet holding this % of supply or more dumps half its "
                 "bag; 0 = off"),
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
    Setting("discovery.geckoterminal_enabled", "GeckoTerminal new pools", "bool",
            help="new pools from GeckoTerminal (Solana buys; Base/BSC/ETH alerts only)"),
    Setting("discovery.dexscreener_profiles", "DexScreener new tokens", "bool",
            help="newly listed tokens from DexScreener"),
    Setting("entry.confirm_seconds", "Confirm window", "float", 0, 120, "s"),
    Setting("entry.min_unique_buyers", "Min early buyers", "int", 0, 1000),
    Setting("entry.max_single_buyer_pct", "Max single buyer", "float", 1, 100, "%",
            help="skip if one wallet (not the dev) made more than this % of the early buying"),
    Setting("entry.max_identical_buys", "Max identical buys", "int", 0, 100,
            help="skip if this many wallets bought the exact same SOL amount (a bundle); 0 = off"),
    Setting("entry.min_net_flow_sol", "Min net buying", "float", 0, 1000, "SOL",
            help="early buys minus sells must be more than this"),
    Setting("entry.max_top_holders_pct", "Max top-10 (launches)", "float", 0, 100, "%",
            help="skip a new launch if its top 10 wallets (dev included) hold more than "
                 "this % of supply at the end of the confirm window; 0 = off. Seconds after "
                 "launch the top 10 is nearly every holder, so keep it high (50+) or off"),
    Setting("entry.max_launch_bundle_pct", "Max launch bundle", "float", 0, 100, "%",
            help="skip if wallets that bought in the launch block hold more than this % "
                 "(how bundled rugs start); 0 = off"),
    Setting("filters.max_creator_initial_buy_pct", "Max dev buy", "float", 0, 100, "%"),
    Setting("filters.max_top10_holder_pct", "Max top-10 hold", "float", 1, 100, "%"),
    Setting("filters.min_socials", "Min socials", "int", 0, 3),
    Setting("filters.dev_min_wallet_age_min", "Min dev wallet age", "float", 0, 100000, "min",
            help="skip devs whose wallet made its first transaction less than this long ago; 0 = off"),
    Setting("filters.dev_min_funder_age_min", "Min dev funder age", "float", 0, 100000, "min",
            help="skip devs funded by a wallet younger than this (a throwaway parent); 0 = off"),
    Setting("filters.dev_funder_blocklist", "Dev funder blocklist", "wallets",
            help="skip devs funded by these wallets (mixers, known rug funders), or 'none'. "
                 "Funders of devs who rugged you are blocked automatically"),
    Setting("filters.dev_curve_sell_hours", "Dev sell lookback", "float", 0, 168, "h",
            help="check the dev's recent pump.fun curve sells over this many hours; 0 = off"),
    Setting("filters.dev_max_curve_sells", "Max dev curve sells", "int", 0, 1000,
            help="skip devs who sold into pump.fun curves more than this many times in the "
                 "lookback, on any coin; 0 = any sell rejects"),
    Setting("filters.dev_history_max_txs", "Dev history depth", "int", 5, 200,
            help="how many of the dev's newest transactions to scan for curve sells"),
    Setting("filters.dev_check_on_error", "If dev check fails", "choice",
            options=("allow", "skip"),
            help="the RPC lookup failed: allow = buy anyway, skip = don't buy"),
    Setting("filters.max_creator_launches_24h", "Max dev launches/24h", "int", 0, 1000,
            help="skip devs who launched more than this many tokens in 24h; 0 = off"),
    Setting("filters.min_liquidity_usd", "Min liquidity", "float", 0, 1e9, "$"),
    Setting("filters.max_fdv_usd", "Max market cap", "float", 0, 1e12, "$",
            help="skip tokens already worth more than this (traded tokens, incl. momentum "
                 "buys; brand-new pump.fun launches aren't affected); 0 = no limit"),
    Setting("filters.honeypot_check", "Honeypot check", "bool"),
    # speed
    Setting("speed.jito_enabled", "Jito bundles", "bool"),
    Setting("speed.jito_tip_sol", "Jito tip", "float", 0.00001, 0.1, "SOL"),
    Setting("speed.jito_also_send_rpc", "Also send via RPC", "bool",
            help="send every trade through Jito AND your RPC at once: more trades land, but "
                 "the RPC copy has no sandwich protection"),
    Setting("speed.auto_priority_fee", "Auto priority fee", "bool"),
    Setting("speed.max_priority_fee_sol", "Max priority fee", "float", 0, 0.1, "SOL"),
    Setting("speed.min_priority_fee_sol", "Min priority fee", "float", 0, 0.1, "SOL",
            help="the auto priority fee never goes below this; raise it if buys expire "
                 "without landing (it's paid on every trade)"),
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
    # call sniper (Telegram groups)
    Setting("calls.enabled", "Call sniper", "bool",
            help="buy contract addresses posted in your watched Telegram groups"),
    Setting("calls.action", "On a call", "choice", options=("buy", "alert"),
            help="buy = buy it (filters and limits apply); alert = just tell me"),
    Setting("calls.min_groups", "Min groups", "int", 1, 20,
            help="only act once this many of your groups posted the same CA"),
    Setting("calls.group_window_minutes", "Groups window", "float", 1, 1440, "min",
            help="...within this long of the first post"),
    Setting("calls.max_market_cap_usd", "Max call market cap", "float", 0, 1e12, "$",
            help="skip called coins already worth more than this, e.g. 20k or 1.5m; "
                 "0 = no limit"),
    # copy trade
    Setting("copytrade.enabled", "Copy trading", "bool"),
    Setting("copytrade.min_leader_buy_sol", "Min leader buy", "float", 0, 1000, "SOL",
            help="only copy buys of at least this much SOL (skips their dust/test buys)"),
    Setting("copytrade.max_market_cap_usd", "Max copy market cap", "float", 0, 1e12, "$",
            help="don't copy into coins already worth more than this, e.g. 20k or 1.5m; "
                 "0 = no limit. A fresh pump.fun coin starts near $4-5k"),
    Setting("copytrade.slippage_pct", "Copy buy slippage", "float", 0, 100, "%",
            help="slippage for copy buys (copied wallets' buys pull in followers fast, so "
                 "normal slippage often fails); 0 = your normal buy slippage"),
    Setting("copytrade.sell_slippage_pct", "Copy sell slippage", "float", 0, 100, "%",
            help="slippage for selling copies (e.g. when the wallet dumps with its "
                 "followers); 0 = your normal sell slippage"),
    Setting("copytrade.max_positions", "Max copy positions", "int", 0, 100,
            help="copies get this many slots of their own, apart from sniping's max "
                 "positions; 0 = copies share the normal max positions"),
    Setting("copytrade.first_buy_only", "First buy only", "bool",
            help="copy each coin once: not their add-on buys, nor a coin re-bought later"),
    Setting("copytrade.max_chase_pct", "Don't chase above", "float", 0, 10000, "%",
            help="skip a copy if the price is already this % above what they paid "
                 "(their own buy pushes it up a bit); 0 = off"),
    Setting("copytrade.pause_after_losses", "Pause after losses", "int", 0, 100,
            help="pause a wallet after this many losing copies in a row; 0 = never"),
    Setting("copytrade.pause_daily_loss_sol", "Pause after daily loss", "float", 0, 1000, "SOL",
            help="pause a wallet whose copies lost this much today; 0 = never"),
    Setting("copytrade.check_days", "Wallet check days", "float", 1, 14, "days",
            help="how far back the wallet check looks"),
    Setting("copytrade.check_every_hours", "Wallet check every", "float", 1, 48, "h",
            help="how often each wallet's check is refreshed"),
    Setting("copytrade.rpc_watch", "Backup wallet watcher", "bool",
            help="also watch copied wallets straight from the chain (your RPC), so copies "
                 "happen even when PumpPortal's feed misses a trade (~2-4s slower)"),
    Setting("copytrade.only_their_sells", "Copies sell only with them", "bool",
            help="on = a copied position sells only when that wallet sells (mirrored by %), "
                 "never on take profits, stop loss, max hold, migration etc."),
    Setting("copytrade.copy_rug_exits", "Copies: rug exits", "bool",
            help="with the above on: still sell at once if the dev sells or a big holder "
                 "dumps (rug signs)"),
    Setting("copytrade.copy_stop_loss_pct", "Copies: emergency stop", "float", 0, 99, "%",
            help="with the above on: still sell if a copy is down this much; 0 = none"),
    Setting("copytrade.own_exits", "Own exits for copies", "bool",
            help="on = copied positions use the 👥 Copy exits settings (TP, SL, moonbag) "
                 "instead of your main exits"),
    # profit taking on copies that otherwise only sell with their wallet
    Setting("copyprofit.take_profit", "Take profits", "tp",
            help="sell part of a copy at these gains, e.g. 100:30,300:30 = sell 30% of the bag "
                 "at 2x and 30% at 4x; none = off"),
    Setting("copyprofit.sell_initials_at_pct", "Take initials at", "float", 0, 100000, "%",
            help="sell enough to get your SOL back at this gain, e.g. 100 = at 2x; 0 = off"),
    Setting("copyprofit.trailing_activate_pct", "Trailing stop arms at", "float", 0, 100000, "%",
            help="once a copy is up this much, sell if it falls back (below); 0 = off"),
    Setting("copyprofit.trailing_stop_pct", "Trailing stop drop", "float", 5, 95, "%",
            help="the fall from its peak that sells it, once armed"),
    Setting("copyprofit.moonbag_pct", "Moonbag", "float", 0, 50, "%",
            help="% of the original bag your profit taking never sells (their sells still "
                 "can); 0 = off"),
    Setting("copyprofit.moonbag_trailing_pct", "Moonbag trailing", "float", 5, 99, "%"),
    Setting("copyprofit.moonbag_max_hold_hours", "Moonbag max hold", "float", 1, 24 * 30, "h"),
    # exits for copied positions (with copytrade.own_exits on)
    Setting("copyexits.take_profit", "Take profits", "tp",
            help="format: up%:sell%, e.g. 50:50,150:30"),
    Setting("copyexits.stop_loss_pct", "Stop loss", "float", 1, 99, "%"),
    Setting("copyexits.trailing_activate_pct", "Trailing arms at", "float", 0, 10000, "%"),
    Setting("copyexits.trailing_stop_pct", "Trailing stop", "float", 1, 99, "%"),
    Setting("copyexits.breakeven_after_first_tp", "Breakeven after TP", "bool"),
    Setting("copyexits.max_hold_seconds", "Max hold", "int", 10, 86400 * 7, "s"),
    Setting("copyexits.sell_initials_at_pct", "Sell initials at", "float", 0, 100000, "%",
            help="profit % at which to take your SOL back, e.g. 100 = at 2x; 0 = off"),
    Setting("copyexits.moonbag_pct", "Moonbag", "float", 0, 50, "%",
            help="% of the original bag to keep after taking profit; 0 = off"),
    Setting("copyexits.moonbag_trailing_pct", "Moonbag trailing", "float", 5, 99, "%"),
    Setting("copyexits.moonbag_max_hold_hours", "Moonbag max hold", "float", 1, 24 * 30, "h"),
    Setting("copytrade.run_safety_checks", "Filters on copies", "bool",
            help="off = copy every buy straight away, skipping your filters (rug checks, "
                 "dev wallet, holders). Your max positions and daily loss limit still apply"),
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
    "calls": "📣 Call sniper",
    "copyexits": "👥 Copy exits",
    "copyprofit": "💰 Copy profit taking",
}


SNIPE_KEYS = {"discovery.auto_snipe", "discovery.snipe_keywords", "discovery.dev_watchlist",
              "discovery.dev_snipe_sol", "discovery.dev_snipe_skip_filters",
              "discovery.pumpfun_new_tokens", "discovery.pumpfun_migrations",
              "discovery.geckoterminal_enabled", "discovery.dexscreener_profiles"}


def group_of(s: Setting) -> str:
    if s.key in SNIPE_KEYS:
        return "snipe"
    if s.key == "notify.utc_offset_hours":
        return "trading"
    if s.key in ("copytrade.own_exits", "copytrade.only_their_sells", "copytrade.copy_rug_exits",
                 "copytrade.copy_stop_loss_pct"):
        return "copyexits"
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
    if s.unit == "$":
        if s.key.endswith(("max_fdv_usd", "max_market_cap_usd")) and not v:
            return "no limit"
        return f"${v:,.0f}"
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
    text = str(raw).strip().rstrip("%").replace("SOL", "").replace("$", "").replace(",", "").strip()
    scale = 1.0
    if s.unit == "$" and text[-1:].lower() in ("k", "m", "b"):  # "500k", "2m": easy on a phone
        scale = {"k": 1e3, "m": 1e6, "b": 1e9}[text[-1].lower()]
        text = text[:-1]
    try:
        num = float(text) * scale
    except ValueError:
        raise ValueError("send a number" + (", e.g. 500k or 2m" if s.unit == "$" else "")) from None
    if not math.isfinite(num):
        raise ValueError("send a normal number")
    if not s.lo <= num <= s.hi:
        if s.unit == "$":
            raise ValueError(f"must be between ${s.lo:,.0f} and ${s.hi:,.0f}")
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
