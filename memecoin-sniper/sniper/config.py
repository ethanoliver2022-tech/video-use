"""Config loading: config.yaml for strategy, .env for secrets, presets for quick starts."""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml
from dotenv import load_dotenv


@dataclass
class TakeProfitLevel:
    at_pct: float      # trigger when price is up this % from entry
    sell_pct: float    # sell this % of the ORIGINAL position size


@dataclass
class CopyWallet:
    address: str
    label: str = ""
    buy_sol: float = 0.0        # 0 = use trading.buy_amount_sol
    copy_sells: bool = True     # exit when they exit
    mode: str = "copy"          # "copy" = mirror their buys, "alert" = just tell me


@dataclass
class TradingConfig:
    buy_amount_sol: float = 0.05
    max_open_positions: int = 3
    slippage_pct: float = 20.0
    priority_fee_sol: float = 0.0005       # used when speed.auto_priority_fee is off
    min_sol_reserve: float = 0.03          # never spend below this (rent + fees for sells)
    daily_loss_limit_sol: float = 0.5      # stop opening positions after this much realized loss; 0 = off
    cooldown_after_loss_seconds: int = 0


@dataclass
class SpeedConfig:
    jito_enabled: bool = True
    jito_tip_sol: float = 0.0005
    jito_block_engines: list[str] = field(
        default_factory=lambda: ["https://mainnet.block-engine.jito.wtf"]
    )
    jito_also_send_rpc: bool = False       # faster landing, but gives up sandwich protection
    broadcast_rpcs: list[str] = field(default_factory=list)  # extra RPCs to fan out to
    auto_priority_fee: bool = True
    priority_fee_percentile: float = 75.0
    min_priority_fee_sol: float = 0.0001
    max_priority_fee_sol: float = 0.003


@dataclass
class DiscoveryConfig:
    pumpfun_new_tokens: bool = True        # snipe brand-new pump.fun launches
    pumpfun_migrations: bool = False       # snipe tokens graduating off the bonding curve
    geckoterminal_networks: list[str] = field(
        default_factory=lambda: ["solana", "base", "bsc", "eth"]
    )
    geckoterminal_poll_seconds: int = 20
    dexscreener_profiles: bool = True
    dexscreener_poll_seconds: int = 30
    max_candidate_age_seconds: int = 1800  # ignore anything older than this
    auto_snipe: str = "all"                # all | targeted (watchlist + keywords only) | off
    snipe_keywords: list[str] = field(default_factory=list)  # match in name / ticker
    dev_watchlist: list[str] = field(default_factory=list)   # buy these devs' next launch
    dev_snipe_sol: float = 0.0             # size for watched-dev snipes (0 = buy_amount_sol)
    dev_snipe_skip_filters: bool = True    # you trust these devs: skip filters for speed


@dataclass
class EntryConfig:
    """Early-flow confirmation for pump.fun launches (0 seconds = instant snipe)."""
    confirm_seconds: float = 0.0
    min_unique_buyers: int = 5
    max_single_buyer_pct: float = 35.0     # of early buy volume, excluding the dev
    max_identical_buys: int = 3            # same SOL size from different wallets = bundle
    min_net_flow_sol: float = 0.0          # early buys minus sells
    max_market_cap_sol: float = 0.0        # 0 = no cap; skip if it already ran too far


@dataclass
class FilterConfig:
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top10_holder_pct: float = 30.0     # excludes program-owned accounts (curve / LP vaults)
    max_creator_initial_buy_pct: float = 8.0
    min_liquidity_usd: float = 8000.0      # for AMM pools (not pump.fun curve)
    max_fdv_usd: float = 2_000_000.0
    use_rugcheck: bool = True
    rugcheck_reject_danger: bool = True
    honeypot_check: bool = True            # quote buy->sell round trip before entering
    max_roundtrip_loss_pct: float = 25.0
    min_socials: int = 0                   # pump.fun metadata: twitter / telegram / website
    reject_reused_socials: bool = True     # same twitter/telegram as an earlier launch
    max_creator_launches_24h: int = 3      # serial launchers are almost always farming; 0 = off
    auto_blocklist_ruggers: bool = True    # creators who dev-dump on us get blocklisted
    name_blocklist: list[str] = field(default_factory=lambda: ["test", "rug", "scam"])
    creator_blocklist: list[str] = field(default_factory=list)


@dataclass
class ExitConfig:
    take_profit: list[TakeProfitLevel] = field(
        default_factory=lambda: [
            TakeProfitLevel(at_pct=40, sell_pct=40),
            TakeProfitLevel(at_pct=100, sell_pct=30),
            TakeProfitLevel(at_pct=250, sell_pct=20),
        ]
    )
    stop_loss_pct: float = 25.0             # sell all if down this much
    breakeven_after_first_tp: bool = True   # after the first TP, never let it go red
    trailing_activate_pct: float = 30.0     # trailing stop arms once up this much
    trailing_stop_pct: float = 20.0         # then sells all on this % drop from peak
    max_hold_seconds: int = 900
    stale_seconds: int = 120                # no price update for this long -> exit
    exit_on_dev_sell: bool = True
    kol_wallets: list[str] = field(default_factory=list)
    kol_buy_sell_pct: float = 50.0          # sell this % of remaining into the first KOL buy
    sell_pressure_window: int = 20          # last N trades
    sell_pressure_ratio: float = 0.75       # exit if >= this share of window are sells
    sell_pressure_min_trades: int = 12
    sell_initials_at_pct: float = 0.0       # e.g. 100 = at 2x, sell enough to get your SOL back
    moonbag_pct: float = 0.0                # keep this % of the original bag after taking profit
    moonbag_trailing_pct: float = 50.0      # the moonbag's own (wide) trailing stop
    moonbag_max_hold_hours: float = 24.0


@dataclass
class CopyTradeConfig:
    enabled: bool = False
    wallets: list[CopyWallet] = field(default_factory=list)
    min_leader_buy_sol: float = 0.2         # ignore dust buys / tests
    run_safety_checks: bool = True


@dataclass
class NotifyConfig:
    telegram: bool = False                  # needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env
    telegram_control: bool = True           # accept commands + buttons from your chat
    alert_other_chains: bool = True


@dataclass
class Endpoints:
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_api: str = "https://lite-api.jup.ag/swap/v1"
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    pumpportal_trade: str = "https://pumpportal.fun/api/trade-local"
    geckoterminal_api: str = "https://api.geckoterminal.com/api/v2"
    dexscreener_api: str = "https://api.dexscreener.com"
    rugcheck_api: str = "https://api.rugcheck.xyz/v1"
    ipfs_gateway: str = ""                  # rewrite ipfs.io metadata links to a faster gateway


@dataclass
class Config:
    preset: str = "balanced"
    trading: TradingConfig = field(default_factory=TradingConfig)
    speed: SpeedConfig = field(default_factory=SpeedConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    copytrade: CopyTradeConfig = field(default_factory=CopyTradeConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    endpoints: Endpoints = field(default_factory=Endpoints)
    data_dir: str = "data"

    # secrets, from environment only
    private_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    pumpportal_api_key: str = ""   # needed for live trade / copy-trade streams
    jupiter_api_key: str = ""      # free at portal.jup.ag; switches to api.jup.ag


# Presets sit underneath your config.yaml: anything you set there wins.
PRESETS: dict[str, dict[str, Any]] = {
    "degen": {
        "entry": {"confirm_seconds": 0},
        "filters": {"max_creator_initial_buy_pct": 15, "max_top10_holder_pct": 45,
                    "min_socials": 0, "max_creator_launches_24h": 10, "min_liquidity_usd": 3000},
        "exits": {"stop_loss_pct": 35, "trailing_activate_pct": 50, "trailing_stop_pct": 30,
                  "max_hold_seconds": 1800, "sell_initials_at_pct": 100, "moonbag_pct": 10,
                  "take_profit": [{"at_pct": 100, "sell_pct": 50}, {"at_pct": 400, "sell_pct": 30}]},
    },
    "balanced": {},
    "safe": {
        "entry": {"confirm_seconds": 6, "min_unique_buyers": 8, "max_single_buyer_pct": 25},
        "filters": {"max_creator_initial_buy_pct": 4, "max_top10_holder_pct": 20, "min_socials": 1,
                    "max_creator_launches_24h": 1, "min_liquidity_usd": 20000},
        "exits": {"stop_loss_pct": 15, "trailing_activate_pct": 20, "trailing_stop_pct": 12,
                  "max_hold_seconds": 600,
                  "take_profit": [{"at_pct": 25, "sell_pct": 50}, {"at_pct": 60, "sell_pct": 30}]},
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _build(cls: type, raw: dict[str, Any]) -> Any:
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:  # a typo shouldn't take a 24/7 bot offline: warn and carry on
        logging.getLogger("sniper").warning("config.yaml: ignoring unknown %s setting(s): %s",
                                            cls.__name__.replace("Config", "").lower() or "top-level",
                                            ", ".join(sorted(unknown)))
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in raw or raw[f.name] is None:  # "key:" with no value = use the default
            continue
        val = raw[f.name]
        typ = hints[f.name]
        if is_dataclass(typ) and isinstance(val, dict):
            val = _build(typ, val)
        elif f.name == "take_profit":
            val = [TakeProfitLevel(**lvl) for lvl in val]
        elif f.name == "wallets":
            val = [CopyWallet(address=w) if isinstance(w, str) else CopyWallet(**w) for w in val]
        kwargs[f.name] = val
    return cls(**kwargs)


def load_config(path: str | os.PathLike | None = None, preset: str | None = None) -> Config:
    load_dotenv()
    raw: dict[str, Any] = {}
    if path and Path(path).is_file():
        try:
            raw = yaml.safe_load(Path(path).read_text()) or {}
        except yaml.YAMLError as e:
            raise SystemExit(f"{path} is not valid YAML, fix it and restart:\n{e}") from None
        if not isinstance(raw, dict):
            raise SystemExit(f"{path} should contain settings like 'trading:', not {type(raw).__name__}")
    name = preset or raw.get("preset", "balanced")
    if name not in PRESETS:
        raise ValueError(f"unknown preset '{name}' (choose from {', '.join(PRESETS)})")
    raw = _deep_merge(PRESETS[name], raw)
    raw["preset"] = name
    cfg: Config = _build(Config, raw)

    if os.getenv("SOLANA_RPC_URL"):
        cfg.endpoints.rpc_url = os.environ["SOLANA_RPC_URL"]
    cfg.private_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    cfg.pumpportal_api_key = os.getenv("PUMPPORTAL_API_KEY", "").strip()
    cfg.jupiter_api_key = os.getenv("JUPITER_API_KEY", "").strip()
    if cfg.jupiter_api_key and "lite-api.jup.ag" in cfg.endpoints.jupiter_api:
        cfg.endpoints.jupiter_api = cfg.endpoints.jupiter_api.replace("lite-api.jup.ag", "api.jup.ag")

    cfg.exits.take_profit.sort(key=lambda lvl: lvl.at_pct)
    return cfg
