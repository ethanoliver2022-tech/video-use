"""CLI: python -m sniper {run,scan,wallet,keygen,sell}"""
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
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="trade (paper by default)")
    run.add_argument("--live", action="store_true", help="sign and send real transactions")
    sub.add_parser("scan", help="discovery + filters only, no trading")
    sub.add_parser("wallet", help="show hot-wallet address and SOL balance")
    sub.add_parser("keygen", help="create a fresh hot wallet")
    sell = sub.add_parser("sell", help="emergency: sell 100%% of a token back to SOL")
    sell.add_argument("mint")

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

    cfg = load_config(args.config)

    if args.cmd == "wallet":
        asyncio.run(_wallet(cfg))
    elif args.cmd == "sell":
        asyncio.run(_sell(cfg, args.mint))
    else:
        from .engine import Engine
        live = args.cmd == "run" and args.live
        if live:
            print(f"LIVE MODE: up to {cfg.trading.buy_amount_sol} SOL per trade, "
                  f"{cfg.trading.max_open_positions} positions, "
                  f"daily loss stop {cfg.trading.daily_loss_limit_sol} SOL.")
            if input("Type 'yes' to trade real funds: ").strip().lower() != "yes":
                sys.exit("aborted")
        try:
            asyncio.run(Engine(cfg, live=live, scan_only=args.cmd == "scan").run())
        except KeyboardInterrupt:
            pass


async def _wallet(cfg) -> None:
    from .execution.wallet import load_keypair
    from .solana_rpc import SolanaRpc
    kp = load_keypair(cfg.private_key)
    rpc = SolanaRpc(cfg.endpoints.rpc_url)
    print(f"address: {kp.pubkey()}\nbalance: {await rpc.get_balance_sol(str(kp.pubkey())):.6f} SOL")
    await rpc.http.aclose()


async def _sell(cfg, mint: str) -> None:
    import httpx
    from .execution.executors import Jupiter, LiveExecutor
    from .execution.wallet import load_keypair
    from .solana_rpc import SolanaRpc
    async with httpx.AsyncClient(timeout=15) as http:
        rpc = SolanaRpc(cfg.endpoints.rpc_url, http)
        ex = LiveExecutor(cfg, load_keypair(cfg.private_key), rpc,
                          Jupiter(cfg.endpoints.jupiter_api, rpc, http), http)
        bal = await rpc.get_token_balance(ex.pubkey, mint)
        if bal <= 0:
            sys.exit("no balance for that token")
        fill = await ex.sell(mint, bal, sell_all=True, pump=mint.endswith("pump"), curve=None)
        print(f"sold {fill.tokens:,.0f} for {fill.sol:.6f} SOL — {fill.signature}")


if __name__ == "__main__":
    main()
