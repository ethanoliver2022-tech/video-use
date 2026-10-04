# memecoin-sniper

A Solana-funded memecoin sniper. It watches for new launches across chains,
filters out the most obvious rugs, buys on Solana, and manages exits on its own
with take-profits, stop-losses, and dev-dump and KOL detection.

> **Read this first.** Most new memecoins go to zero. On pump.fun the first
> block is usually taken by the dev's own bundled buys and by pro bots using
> Jito bundles and co-located RPC. This bot will not beat them to block 0. Its
> edge is being early and leaving with discipline. Start in paper mode, run a
> dedicated hot wallet that only holds what you can afford to lose, and expect
> losing streaks.

## How it works

```
 PumpPortal WS ──┐   (new pump.fun launches, migrations, live trades)
 GeckoTerminal ──┼─► dedup ─► safety filters ─► risk limits ─► BUY (Solana)
 DexScreener  ───┘                  │                             │
   (solana/base/bsc/eth)            └─ non-Solana: alert only     ▼
                                                     exit engine (every trade + 1s tick)
                                                     dev sold · stop loss · trailing stop
                                                     sell pressure · max hold · KOL buy
                                                     take-profit ladder ─► SELL
```

**Discovery (multi-chain)**
- **pump.fun via PumpPortal websocket**: brand-new tokens within about a second of creation, plus graduations.
- **GeckoTerminal `new_pools`** on Solana, Base, BSC and Ethereum (configurable).
- **DexScreener** newly created token profiles, enriched with pair liquidity and FDV.

**Execution (Solana only, funded in SOL)**
- pump.fun tokens are routed through PumpPortal's *local* transaction API. It builds the transaction and the bot signs it locally, so your key never leaves your machine.
- All other tokens go through the Jupiter aggregator.
- Fills are read back from the confirmed transaction, so PnL includes real slippage and fees.

Tokens on Base, BSC and Ethereum are **alerts only**. Buying them needs ETH or BNB for gas plus a bridge. That can be added later, but it's slower and a poor fit for sniping.

**Safety filters (before buying)**
- Mint and freeze authority must be revoked.
- Dangerous Token-2022 extensions are rejected: permanent delegate, transfer hook, pausable, high transfer fee.
- Top-10 *wallet* concentration. Program-owned accounts such as the bonding curve and LP vaults are excluded.
- pump.fun: the dev's launch buy can't exceed N% of supply.
- AMM pools: minimum liquidity and maximum FDV.
- RugCheck.xyz "danger" flags.
- Blocklists for names and for creator wallets.

**Exits (first rule that matches wins)**
1. **Dev sold**: the creator wallet sells → sell everything immediately.
2. **Stop loss**: down N% from entry.
3. **Trailing stop**: arms after +X%, then sells everything on a Y% drop from the peak.
4. **Sell pressure**: most of the recent trades are sells, which usually means the crowd is leaving.
5. **Max hold / stale**: memecoin edge fades within minutes.
6. **KOL buy**: when a wallet on your `kol_wallets` list buys, sell part of your bag into the followers they bring.
7. **Take-profit ladder**: scale out at set multiples, as a % of the original bag.

**Risk limits**: maximum SOL per trade, maximum open positions, a SOL reserve kept for fees, a daily realized-loss stop and a cooldown after a loss.

## Setup

```bash
cd memecoin-sniper
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
cp .env.example .env
```

1. **Paper trade first** (no wallet needed):
   ```bash
   python -m sniper scan          # just show what passes the filters
   python -m sniper run           # paper trading; ledger in data/trades-paper.jsonl
   ```
2. **Create a dedicated hot wallet**:
   ```bash
   python -m sniper keygen        # paste the SOLANA_PRIVATE_KEY line into .env
   ```
   Send it a small amount of SOL, then check it with `python -m sniper wallet`.
3. **Get a paid RPC** (Helius, Triton, QuickNode, ...) and set `SOLANA_RPC_URL` in `.env`.
   The public RPC is too slow and too rate-limited for live trading.
4. **Go live**:
   ```bash
   python -m sniper run --live    # asks you to type "yes"
   ```
5. **Emergency exit** for a single token:
   ```bash
   python -m sniper sell <mint>
   ```

Telegram alerts: create a bot with @BotFather, put `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` in `.env`, and set `notify.telegram: true`.

## Tuning tips

- Run paper mode for at least a few hundred trades. Then read
  `data/trades-paper.jsonl` (`close` rows hold `pnl_sol` and `reason`) and adjust
  the filters and exits.
- `max_creator_initial_buy_pct` and `max_top10_holder_pct` are the filters that
  catch the most rugs.
- To leave before the KOLs and the crowd, fill `kol_wallets` with wallets you've
  watched pump coins. Tighten `trailing_stop_pct` or `sell_pressure_ratio` to
  exit sooner, at the cost of selling some winners early.
- Raising `priority_fee_sol` gets transactions in faster but costs money on every
  buy and sell.

## Known limitations / next steps

- No Jito bundles or private transaction submission yet, so you're competing in
  the public mempool path.
- Open positions are not reloaded after a restart. Use `sniper sell <mint>`, or
  check the ledger.
- There is no dev-history scoring yet (how many coins this wallet launched and
  how many it rugged). That would be the strongest filter to add next.
- Non-Solana chains are alert-only.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
