# memecoin-sniper

A self-hosted Solana memecoin sniper with the feature set of the paid bots
(Trojan, BonkBot, Photon, BullX, GMGN, Axiom). It has no subscription and no
1% fee on every trade, and your private key never leaves your machine.

> **Read this first.** Most new memecoins go to zero. On pump.fun the first
> block usually goes to the dev's own bundled buys and to pro bots with
> co-located infrastructure. The paid bots don't beat them either. What they
> sell is fast landing, rug filtering and disciplined exits, and that's what
> this bot does. Start in paper mode, use a dedicated hot wallet, and only fund
> it with what you can afford to lose.

## Features

| | What it does | Paid-bot equivalent |
|---|---|---|
| ⚡ **Jito bundles** | The swap and a validator tip go out as one atomic bundle. It can't be sandwiched, gets block priority, and if the swap fails you don't pay. | Trojan/BonkBot "MEV protection", Banana Gun "anti-rug" |
| 📡 **Multi-RPC broadcast** | Every transaction goes to all of your RPCs and Jito regions at the same time. | "Turbo mode" |
| 💸 **Auto priority fee** | Pays a percentile of the network's recent priority fees, with a floor and a cap. | "Auto fee" |
| 🎯 **Multi-chain discovery** | pump.fun launches and graduations (live websocket), plus new pools on Solana, Base, BSC and ETH from GeckoTerminal and DexScreener. | Photon/BullX "new pairs" |
| 👥 **Copy trading** | Mirrors buys from wallets you follow, can follow their sells out, and sets a size per wallet. Wallets can be added or removed live from Telegram. | GMGN/Trojan copy trade |
| 🔍 **Early-flow confirmation** | Watches the first N seconds of trading before buying. It catches bundled launches (several wallets buying identical sizes), whale-dominated launches, dev dumps and thin interest. | Axiom/BullX "bundle checker" |
| 🧠 **Dev reputation** | Records every pump.fun launch and rejects serial launchers. Devs who dumped on you are blocklisted automatically, and the blocklist persists. | GMGN "dev history" |
| 🌐 **Socials check** | Reads the token metadata, can require Twitter/Telegram/website links, and rejects copycats that reuse another launch's socials. | Photon "socials filter" |
| 🍯 **Honeypot check** | Quotes a buy and then a sell before entering. A token that can't be sold, or loses too much on the round trip, is rejected. | "Honeypot / tax check" |
| 🛡 **On-chain rug filters** | Rejects tokens where mint or freeze authority isn't revoked, or with dangerous Token-2022 extensions. Also checks top-10 *wallet* concentration (curve/LP vaults excluded), dev buy size and RugCheck flags. | Standard on all paid bots |
| 📈 **Smart exits** | Exits on: dev sells, copied wallet sells, stop loss, breakeven stop after the first take-profit, trailing stop, sell pressure, KOL buys, a take-profit ladder, max hold time, or a token going quiet. | "Auto sell", "trailing stop" |
| 📱 **Telegram control panel** | Status, positions with Sell 25/50/100% buttons, manual buy/sell, pause, buy size, copy wallets, blocklist and stats, all from your phone. | Trojan/BonkBot UI |
| 💾 **Restart-safe** | Positions, settings, copy wallets and reputation data are stored in SQLite. Open positions are reloaded after a restart and checked against the wallet. | Hosted bots |
| 🎚 **Presets** | `degen`, `balanced` or `safe`, each with one-line overrides. | "Strategy presets" |
| 📊 **Analytics** | Win rate, PnL, average win/loss, and breakdowns by exit reason and by source. | GMGN PnL cards |

## How it works

```
 PumpPortal WS ─────┐  launches · migrations · trades · copy-wallet trades
 GeckoTerminal ─────┤
 DexScreener  ──────┘
        │
        ▼
 dedup ─► filters (on-chain, reputation, socials, honeypot, RugCheck)
        │       └─ non-Solana: Telegram alert only
        ▼
 early-flow confirmation (optional) ─► risk limits ─► BUY
        │                                             │ PumpPortal (pump.fun) / Jupiter
        │                                             │ signed locally → Jito bundle + RPC fan-out
        ▼                                             ▼
 Telegram control panel  ◄────────────────  exit engine (every trade + 1s tick) ─► SELL
```

Execution is Solana-only, funded in SOL. Base, BSC and ETH tokens are alerts
only, because buying them would need ETH or BNB for gas plus a bridge, which is
too slow for sniping.

## Setup

```bash
cd memecoin-sniper
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
cp .env.example .env
```

1. **Paper trade first.** No wallet is needed:
   ```bash
   python -m sniper scan                  # only shows what passes the filters
   python -m sniper run                   # paper trading
   python -m sniper -p safe run           # try another preset
   python -m sniper stats                 # see how it did
   ```
2. **Telegram** (strongly recommended): create a bot with @BotFather. Message
   the bot once, then get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` and set `notify.telegram: true`.
   Only your chat id can control the bot.
3. **Create a hot wallet** and fund it with a small amount:
   ```bash
   python -m sniper keygen                # paste the line into .env
   python -m sniper wallet
   ```
4. **Paid RPC.** Set `SOLANA_RPC_URL` (Helius, Triton, QuickNode, ...). You can
   also add more RPCs to `speed.broadcast_rpcs` and the nearest Jito region to
   `speed.jito_block_engines`.
5. **Go live:**
   ```bash
   python -m sniper run --live            # asks you to type "yes"
   ```

### Telegram commands

```
/status                  mode, balance, open positions, PnL today
/positions               each position with Sell 25% / 50% / 100% buttons
/buy <mint> [sol] [force] manual buy (force skips filters)
/sell <mint|SYMBOL> [pct] manual sell
/pause  /resume          stop or restart new entries (open positions are still managed)
/setbuy <sol>            change the auto-buy size (persists)
/copy list | add <wallet> [label] [sol] | rm <wallet>
/block <creator>         never buy from this dev
/stats                   performance summary
```

### CLI

```
python -m sniper [-p degen|balanced|safe] run [--live]
python -m sniper scan
python -m sniper stats [--live] [--days N]
python -m sniper wallet | keygen
python -m sniper sell <mint>           # emergency: sell the whole wallet balance
```

## Tuning

- Run paper mode for a few hundred trades, then use `python -m sniper stats`.
  The by-exit-reason breakdown shows which rule makes or loses money.
- The `safe` preset's 6-second confirmation window gives up the very first
  entry, but filters out most bundled and farmed launches.
- To leave before the KOLs and the crowd, fill `exits.kol_wallets` with wallets
  you've watched pump coins. The bot sells part of your bag into their buys.
  Tighten `trailing_stop_pct` or `sell_pressure_ratio` to exit earlier.
- Copy trading works best with a few wallets that have been profitable for a
  while, not wallets that had one lucky hit. Leave `run_safety_checks` on.
- Jito tips and priority fees are paid on every buy and every sell, so check
  that your average win covers about 4 times that.

## Known limitations

- Copy trading sees pump.fun and PumpSwap trades from PumpPortal. A followed
  wallet trading on other DEXes isn't seen yet. A Helius/Yellowstone gRPC
  feed would add that.
- Dev reputation only knows about launches seen while the bot was running.
  Leave it running in `scan` mode for a day before trading to build up history.
- No web dashboard; Telegram is the UI.
- EVM chains are alert-only.
- This code hasn't been run against live mainnet APIs yet. Start in paper mode,
  then do a first live run with a tiny `buy_amount_sol`.

## Tests

```bash
pip install -e '.[dev]' && pytest     # 40 tests, no network needed
```
