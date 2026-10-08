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
    cooldown_after_loss_seconds: int = 60  # pause new entries this long after a losing trade


@dataclass
class SpeedConfig:
    jito_enabled: bool = True
    jito_tip_sol: float = 0.0005
    jito_block_engines: list[str] = field(
        # every region at once, like the fastest paid bots: whichever leader is next, a nearby
        # block engine has the bundle (identical bundles can only land once)
        default_factory=lambda: [f"https://{r}.mainnet.block-engine.jito.wtf"
                                 for r in ("ny", "amsterdam", "frankfurt", "tokyo", "slc")]
    )
    jito_also_send_rpc: bool = False       # faster landing, but gives up sandwich protection
    broadcast_rpcs: list[str] = field(default_factory=list)  # extra RPCs to fan out to
    auto_priority_fee: bool = True
    priority_fee_percentile: float = 75.0
    min_priority_fee_sol: float = 0.0001
    max_priority_fee_sol: float = 0.003

    def tip_sol(self) -> float:
        """The Jito tip a trade pays (the one rule paper, live and risk checks share)."""
        return self.jito_tip_sol if self.jito_enabled else 0.0


@dataclass
class DiscoveryConfig:
    pumpfun_new_tokens: bool = True        # snipe brand-new pump.fun launches
    pumpfun_migrations: bool = False       # snipe tokens graduating off the bonding curve
    geckoterminal_networks: list[str] = field(
        default_factory=lambda: ["solana", "base", "bsc", "eth"]
    )
    geckoterminal_enabled: bool = True     # on/off switch (Telegram), keeps the network list
    geckoterminal_poll_seconds: int = 20
    dexscreener_profiles: bool = True
    dexscreener_poll_seconds: int = 30
    max_candidate_age_seconds: int = 1800  # ignore anything older than this
    auto_snipe: str = "all"                # all | targeted (watchlist + keywords only) | off
    snipe_keywords: list[str] = field(default_factory=list)  # match in name / ticker
    dev_watchlist: list[str] = field(default_factory=list)   # buy these devs' next launch
    dev_snipe_sol: float = 0.0             # size for watched-dev snipes (0 = buy_amount_sol)
    dev_snipe_skip_filters: bool = True    # you trust these devs: skip filters for speed
    # momentum scanner: Solana tokens pumping right now (free GeckoTerminal + DexScreener
    # data, ~10-30s behind the chain). Off until you switch it on (Telegram main menu).
    momentum_enabled: bool = False
    momentum_action: str = "alert"         # alert = tell me | buy = also buy (filters still run)
    momentum_poll_seconds: int = 30
    momentum_min_change_5m_pct: float = 25.0     # price up at least this much in 5 minutes
    momentum_min_volume_5m_usd: float = 10_000.0
    momentum_min_buyers_5m: int = 25
    momentum_min_buy_ratio: float = 1.5          # buys at least this many times sells (5m)
    momentum_min_liquidity_usd: float = 10_000.0 # enough to get back out
    momentum_buy_sol: float = 0.0                # buy size in buy mode (0 = buy_amount_sol)
    momentum_alerts_per_hour: int = 20
    momentum_repeat_minutes: int = 120           # don't signal the same token again for this long


@dataclass
class EntryConfig:
    """Early-flow confirmation for pump.fun launches (0 seconds = instant snipe)."""
    confirm_seconds: float = 0.0
    min_unique_buyers: int = 5
    max_single_buyer_pct: float = 35.0     # of early buy volume, excluding the dev
    max_identical_buys: int = 3            # same SOL size from different wallets = bundle
    min_net_flow_sol: float = 0.0          # early buys minus sells
    max_market_cap_sol: float = 0.0        # 0 = no cap; skip if it already ran too far
    # top 10 wallets (dev included) % of supply; 0 = off. Off by default: seconds after a
    # launch there are only a handful of holders, so this mostly measures how much has been
    # bought, and would skip the launches with the strongest early buying
    max_top_holders_pct: float = 0.0
    max_launch_bundle_pct: float = 25.0    # % held by wallets that bought in the first second
    #                                        (the dev's bundled wallets); 0 = off


@dataclass
class FilterConfig:
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top10_holder_pct: float = 30.0     # excludes program-owned accounts (curve / LP vaults)
    max_creator_initial_buy_pct: float = 8.0
    min_liquidity_usd: float = 8000.0      # for AMM pools (not pump.fun curve)
    max_fdv_usd: float = 2_000_000.0      # skip tokens already worth more (AMM tokens); 0 = no cap
    use_rugcheck: bool = True
    rugcheck_reject_danger: bool = True
    honeypot_check: bool = True            # quote buy->sell round trip before entering
    max_roundtrip_loss_pct: float = 25.0
    min_socials: int = 0                   # pump.fun metadata: twitter / telegram / website
    reject_reused_socials: bool = True     # same twitter/telegram as an earlier launch
    max_creator_launches_24h: int = 3      # serial launchers are almost always farming; 0 = off
    auto_blocklist_ruggers: bool = True    # creators who dev-dump on us get blocklisted
    name_blocklist: list[str] = field(default_factory=lambda: ["test", "rug", "scam"])
    # dev wallet background (pump.fun launches; on-chain lookups just before buying)
    dev_min_wallet_age_min: float = 60.0    # deployer's first transaction at least this old
    dev_min_funder_age_min: float = 60.0    # whoever funded it must not be brand new either
    dev_funder_blocklist: list[str] = field(default_factory=list)  # mixers / rug funders
    dev_curve_sell_hours: float = 24.0      # look back this far for pump.fun curve sells
    dev_max_curve_sells: int = 0            # reject above this many sells in that time
    dev_history_max_txs: int = 15           # newest transactions scanned for those sells
    dev_check_on_error: str = "allow"       # lookup failed: "allow" the buy or "skip" it
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
    exit_on_whale_sell_pct: float = 4.0     # sell all when a wallet holding at least this % of
    #                                         supply dumps half its bag or more; 0 = off
    sell_on_migration: bool = False        # sell everything when the token leaves the curve
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
class CopyExitConfig:
    """Exits for copied positions, used instead of the main ones while
    copytrade.own_exits is on (everything else comes from the main exit settings)."""
    take_profit: list[TakeProfitLevel] = field(
        default_factory=lambda: [
            TakeProfitLevel(at_pct=40, sell_pct=40),
            TakeProfitLevel(at_pct=100, sell_pct=30),
            TakeProfitLevel(at_pct=250, sell_pct=20),
        ]
    )
    stop_loss_pct: float = 25.0
    breakeven_after_first_tp: bool = True
    trailing_activate_pct: float = 30.0
    trailing_stop_pct: float = 20.0
    max_hold_seconds: int = 900
    sell_initials_at_pct: float = 0.0
    moonbag_pct: float = 0.0
    moonbag_trailing_pct: float = 50.0
    moonbag_max_hold_hours: float = 24.0


@dataclass
class CallsConfig:
    """Buying contract addresses posted in Telegram groups you're in (your own account)."""
    enabled: bool = False
    action: str = "buy"             # "buy" or "alert" (just tell me)
    min_groups: int = 1             # buy only once the CA was posted in this many groups
    group_window_minutes: float = 30.0   # ...within this long of the first post
    max_market_cap_sol: float = 0.0      # skip coins already bigger than this; 0 = no cap


@dataclass
class CopyTradeConfig:
    enabled: bool = False
    wallets: list[CopyWallet] = field(default_factory=list)
    min_leader_buy_sol: float = 0.05        # ignore dust buys / tests
    run_safety_checks: bool = True
    max_market_cap_sol: float = 0.0         # don't copy into coins bigger than this; 0 = no cap
    own_exits: bool = False                 # copied positions use the copyexits section


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
    calls: CallsConfig = field(default_factory=CallsConfig)
    copyexits: CopyExitConfig = field(default_factory=CopyExitConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    endpoints: Endpoints = field(default_factory=Endpoints)
    data_dir: str = "data"

    # secrets, from environment only
    private_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    pumpportal_api_key: str = ""   # needed for live trade / copy-trade streams
    jupiter_api_key: str = ""      # free at portal.jup.ag; switches to api.jup.ag
    # .env-only locks: Telegram can't change them, so they hold even if the chat is compromised
    withdraw_allowlist: list[str] = field(default_factory=list)  # WITHDRAW_ALLOWLIST
    allow_key_export: bool = True                                # ALLOW_KEY_EXPORT
    extra_allowed_programs: list[str] = field(default_factory=list)  # EXTRA_ALLOWED_PROGRAMS


# Presets sit underneath your config.yaml: anything you set there wins.
PRESETS: dict[str, dict[str, Any]] = {
    "degen": {
        "entry": {"confirm_seconds": 0},
        # speed first: no waiting on token metadata (the copycat-socials check) before buying
        "filters": {"max_creator_initial_buy_pct": 15, "max_top10_holder_pct": 45,
                    "min_socials": 0, "reject_reused_socials": False,
                    "max_creator_launches_24h": 10, "min_liquidity_usd": 3000},
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
    name = preset or raw.get("preset") or "balanced"  # a bare 'preset:' means the default
    if name not in PRESETS:
        choices = ", ".join(PRESETS)
        if preset:  # passed in by code
            raise ValueError(f"unknown preset '{name}' (choose from {choices})")
        raise SystemExit(f"unknown preset '{name}' in {path} (choose from {choices})")
    raw = _deep_merge(PRESETS[name], raw)
    raw["preset"] = name
    cfg: Config = _build(Config, raw)

    if os.getenv("SOLANA_RPC_URL"):
        cfg.endpoints.rpc_url = os.environ["SOLANA_RPC_URL"]
    cfg.private_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    cfg.pumpportal_api_key = os.getenv("PUMPPORTAL_API_KEY", "").strip()
    cfg.withdraw_allowlist = [a.strip() for a in os.getenv("WITHDRAW_ALLOWLIST", "").split(",")
                              if a.strip()]
    cfg.extra_allowed_programs = [a.strip() for a in
                                  os.getenv("EXTRA_ALLOWED_PROGRAMS", "").split(",") if a.strip()]
    # more RPCs every trade is also sent through (more paths to a validator = more trades land)
    extra = [u.strip() for u in os.getenv("EXTRA_RPC_URLS", "").split(",")
             if u.strip().startswith(("https://", "http://"))]
    cfg.speed.broadcast_rpcs = list(dict.fromkeys([*cfg.speed.broadcast_rpcs, *extra]))
    cfg.allow_key_export = os.getenv("ALLOW_KEY_EXPORT", "true").strip().lower() not in (
        "0", "false", "no", "off")
    cfg.jupiter_api_key = os.getenv("JUPITER_API_KEY", "").strip()
    if cfg.jupiter_api_key and "lite-api.jup.ag" in cfg.endpoints.jupiter_api:
        cfg.endpoints.jupiter_api = cfg.endpoints.jupiter_api.replace("lite-api.jup.ag", "api.jup.ag")

    cfg.exits.take_profit.sort(key=lambda lvl: lvl.at_pct)
    return cfg
