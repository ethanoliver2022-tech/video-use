"""CLI: python -m sniper {bot,run,scan,wallet,keygen,sell,stats}"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config


def main() -> None:
    p = argparse.ArgumentParser(prog="sniper", description="Solana-funded memecoin sniper")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true", help="log rejected tokens too")
    p.add_argument("-p", "--preset", choices=["degen", "balanced", "safe"],
                   help="strategy preset (overrides config.yaml's preset)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bot", help="Telegram-first mode: control everything from your chat")
    run = sub.add_parser("run", help="trade (paper by default)")
    run.add_argument("--live", action="store_true", help="sign and send real transactions")
    sub.add_parser("scan", help="discovery + filters only, no trading")
    sub.add_parser("wallet", help="show hot-wallet address and SOL balance")
    sub.add_parser("keygen", help="create a fresh hot wallet")
    sell = sub.add_parser("sell", help="emergency: sell 100%% of a token back to SOL")
    sell.add_argument("mint")
    stats = sub.add_parser("stats", help="performance summary")
    stats.add_argument("--live", action="store_true", help="live results instead of paper")
    stats.add_argument("--days", type=float, default=0, help="only the last N days")

    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "keygen":
        from .execution.wallet import new_keypair
        pub, secret = new_keypair()
        print(f"address: {pub}\n\nAdd this line to .env (never share or commit it):\n"
              f"SOLANA_PRIVATE_KEY={secret}")
        return

    cfg = load_config(args.config, preset=args.preset)
    if args.cmd == "bot" and not args.preset:
        from .store import Store
        saved = Store(cfg.data_dir).get_setting("preset")  # chosen from Telegram earlier
        if saved and saved != cfg.preset:
            cfg = load_config(args.config, preset=saved)

    if args.cmd == "stats":
        import time
        from .stats import format_summary, summarize
        from .store import Store
        since = time.time() - args.days * 86400 if args.days else 0
        print(format_summary(summarize(Store(cfg.data_dir, "live" if args.live else "paper"), since)))
    elif args.cmd == "bot":
        if not cfg.telegram_bot_token:
            sys.exit("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather first).")
        from .engine import Engine
        eng = Engine(cfg, live=False, config_path=args.config, start_paused=True)
        eng.telegram_ui = True
        try:
            asyncio.run(eng.run())
        except KeyboardInterrupt:
            pass
    elif args.cmd == "wallet":
        asyncio.run(_wallet(cfg))
    elif args.cmd == "sell":
        asyncio.run(_sell(cfg, args.mint))
    else:
        from .engine import Engine
        live = args.cmd == "run" and args.live
        if live:
            print(f"LIVE MODE (preset {cfg.preset}): {cfg.trading.buy_amount_sol} SOL per trade, "
                  f"{cfg.trading.max_open_positions} positions, "
                  f"daily loss stop {cfg.trading.daily_loss_limit_sol} SOL.")
            if input("Type 'yes' to trade real funds: ").strip().lower() != "yes":
                sys.exit("aborted")
        try:
            asyncio.run(Engine(cfg, live=live, scan_only=args.cmd == "scan").run())
        except KeyboardInterrupt:
            pass


async def _wallet(cfg) -> None:
    from .execution.wallet import WalletManager
    from .solana_rpc import SolanaRpc
    kp = WalletManager(cfg.data_dir, cfg.private_key).keypair()
    if kp is None:
        sys.exit("no wallet: run `sniper keygen`, or create one from Telegram")
    rpc = SolanaRpc(cfg.endpoints.rpc_url)
    print(f"address: {kp.pubkey()}\nbalance: {await rpc.get_balance_sol(str(kp.pubkey())):.6f} SOL")
    await rpc.http.aclose()


async def _sell(cfg, mint: str) -> None:
    import httpx
    from .execution.executors import Jupiter, LiveExecutor
    from .execution.wallet import WalletManager
    from .solana_rpc import SolanaRpc
    kp = WalletManager(cfg.data_dir, cfg.private_key).keypair()
    if kp is None:
        sys.exit("no wallet configured")
    async with httpx.AsyncClient(timeout=15) as http:
        rpc = SolanaRpc(cfg.endpoints.rpc_url, http)
        ex = LiveExecutor(cfg, kp, rpc,
                          Jupiter(cfg.endpoints.jupiter_api, rpc, http), http)
        bal = await rpc.get_token_balance(ex.pubkey, mint)
        if bal <= 0:
            sys.exit("no balance for that token")
        fill = await ex.sell(mint, bal, sell_all=True, pump=mint.endswith("pump"), curve=None)
        print(f"sold {fill.tokens:,.0f} for {fill.sol:.6f} SOL — {fill.signature}")


if __name__ == "__main__":
    main()
