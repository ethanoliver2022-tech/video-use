"""Config loading: config.yaml for strategy, .env for secrets."""
from __future__ import annotations

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
class TradingConfig:
    buy_amount_sol: float = 0.05
    max_open_positions: int = 3
    slippage_pct: float = 20.0
    priority_fee_sol: float = 0.0005
    min_sol_reserve: float = 0.03          # never spend below this (rent + fees for sells)
    daily_loss_limit_sol: float = 0.5      # stop opening positions after this much realized loss
    cooldown_after_loss_seconds: int = 0


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


@dataclass
class FilterConfig:
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top10_holder_pct: float = 30.0     # excludes program-owned accounts (curve / LP vaults)
    max_creator_initial_buy_pct: float = 8.0
    min_creator_initial_buy_sol: float = 0.0
    min_liquidity_usd: float = 8000.0      # for AMM pools (not pump.fun curve)
    max_fdv_usd: float = 2_000_000.0
    use_rugcheck: bool = True
    rugcheck_reject_danger: bool = True
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


@dataclass
class NotifyConfig:
    telegram: bool = False                  # needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env
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


@dataclass
class Config:
    trading: TradingConfig = field(default_factory=TradingConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    endpoints: Endpoints = field(default_factory=Endpoints)
    data_dir: str = "data"

    # secrets, from environment only
    private_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def _build(cls: type, raw: dict[str, Any]) -> Any:
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    for f in fields(cls):
        if f.name not in raw:
            continue
        val = raw[f.name]
        typ = hints[f.name]
        if is_dataclass(typ) and isinstance(val, dict):
            val = _build(typ, val)
        elif f.name == "take_profit":
            val = [TakeProfitLevel(**lvl) for lvl in val]
        kwargs[f.name] = val
    return cls(**kwargs)


def load_config(path: str | os.PathLike | None = None) -> Config:
    load_dotenv()
    raw: dict[str, Any] = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg: Config = _build(Config, raw)

    if os.getenv("SOLANA_RPC_URL"):
        cfg.endpoints.rpc_url = os.environ["SOLANA_RPC_URL"]
    cfg.private_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    cfg.exits.take_profit.sort(key=lambda lvl: lvl.at_pct)
    return cfg
