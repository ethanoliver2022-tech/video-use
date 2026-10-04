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
| 📱 **Telegram-first** | The whole bot runs from a chat: wallet, deposit, withdraw, paper/live, every setting, presets, copy trading, manual trades, stats. | Trojan/BonkBot UI |
| 💾 **Restart-safe** | Positions, settings, copy wallets and reputation data are stored in SQLite. Open positions are reloaded after a restart and checked against the wallet. | Hosted bots |
| 🎚 **Presets** | `degen`, `balanced` or `safe`, each with one-line overrides. | "Strategy presets" |
| 📊 **Analytics** | Win rate, PnL, average win/loss, and breakdowns by exit reason and by source. | GMGN PnL cards |

## Run it all from Telegram

The whole bot is driven from a Telegram chat, like Trojan or BonkBot. You start
one process on a server once, and everything after that happens in the chat:
wallet, deposits, withdrawals, starting and stopping, paper/live, settings and
copy trading.

```
🎯 Memecoin Sniper
📝 PAPER · ▶️ sniping · preset balanced
💼 7xKX…9fQ2
💰 1.2500 SOL
📊 Open 1/3 · today +0.0312 SOL · buy 0.05 SOL

[ ⏸ Pause sniping            ]
[ 💼 Wallet   ][ 📊 Positions ]
[ ⚙️ Settings ][ 👥 Copy trade]
[ 📈 Stats    ][ 🔴 Go LIVE   ]
[ 🔄 Refresh                  ]
```

### One-time setup (about 5 minutes)

1. **Create a Telegram bot.** In Telegram, message **@BotFather**, send `/newbot`
   and copy the token it gives you.
2. **Start the bot on a server** that stays on, such as a small VPS. On a fresh
   Ubuntu server, one command does everything (Docker, firewall, download, start):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/ethanoliver2022-tech/video-use/claude/memecoin-sniping-bot-x4vgsq/memecoin-sniper/install.sh | sudo bash
   ```
   It asks for your bot token and optional keys (see **Keys and costs** below),
   then prints a pairing code. Running it again later updates the bot and keeps
   your wallet, settings and history.

   Manual alternative:
   ```bash
   cd memecoin-sniper
   cp .env.example .env              # paste TELEGRAM_BOT_TOKEN (+ optional keys)
   docker compose up -d --build      # or: pip install -e . && python -m sniper bot
   docker compose logs | grep /start # shows: Send this to your bot:  /start 3F9A1C27B0
   ```
3. **Pair your chat.** Open your bot in Telegram and send the `/start …` line.
   From then on, only your chat can control the bot and everyone else is ignored.
   After 10 wrong codes the code changes and the new one appears in the server log.

### Keys and costs

Only the Telegram token is required. Everything else is optional; each key goes
in `.env` on the server, then run `docker compose up -d` to apply it.

| Key | What it unlocks | Cost |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | The bot itself | Free |
| `SOLANA_RPC_URL` | Fast, reliable trading. The public RPC is fine for paper testing but too slow and rate-limited for live trades | Helius and others have free tiers; paid plans for heavy use |
| `PUMPPORTAL_API_KEY` | The live trade stream: instant prices, instant dev-dump detection, KOL and sell-pressure exits, bundle detection in the confirmation window, and **copy trading** | PumpPortal bills the wallet linked to your key per message received (0.01 SOL per 10,000 at the time of writing), and requires that wallet to hold at least 0.02 SOL. Check pumpportal.fun for current pricing |
| `JUPITER_API_KEY` | Jupiter's supported API (`api.jup.ag`). The keyless API still works today but is being retired | Free key from portal.jup.ag |

Without a PumpPortal key the bot still runs fully: it reads pump.fun prices and
graduations straight from the bonding curve on-chain, and detects dev dumps by
watching the dev's token balance, every 2 seconds. That's slower than the
stream, and copy trading, KOL exits and sell-pressure exits stay off.

Per-trade costs to know about: Solana network fees, your priority fee, the Jito
tip (when Jito is on), pump.fun's own trading fee, and PumpPortal's fee on
trades it builds (pump.fun tokens). Check pumpportal.fun for its current rate.

### Everything else happens in the chat

| You want to… | In Telegram |
|---|---|
| Get a wallet | 💼 Wallet → ✨ Create new wallet (or 📥 Import a private key; your message is deleted right after it's read) |
| Deposit | 💼 Wallet shows the deposit address. Tap it to copy, then send SOL from Phantom or an exchange |
| Back up the key | 💼 Wallet → 🔑 Export key. It's shown behind a spoiler and auto-deleted after 60s |
| Withdraw | 💼 Wallet → 📤 Withdraw → `<address> <amount|all>` → confirm |
| Test safely | It starts **paused** in 📝 PAPER mode. Tap ▶️ Start sniping and check 📈 Stats |
| Trade real SOL | 🔴 Go LIVE → review the summary → confirm (only allowed with no open paper positions) |
| Change any setting | ⚙️ Settings → category → tap a setting (on/off toggles flip instantly, numbers ask for a value) |
| Switch strategy | ⚙️ Settings → 🎚 Presets → degen / balanced / safe (your custom settings still win) |
| Sell | 📊 Positions → Sell 25% / 50% / 100%, or use the buttons on any buy alert |
| Buy a token yourself | Paste its address into the chat → pick an amount (runs your filters; ⚠️ option skips them) |
| Copy a wallet | 👥 Copy trade → ➕ Add wallet → `<address> [label] [sol]` |
| Stop everything | ⏸ Pause sniping (open positions are still managed and exited) |

Every change is saved on the server and survives restarts: wallet, settings,
preset, paper/live mode, pause state, positions, copy wallets and the blocklist.

### Security

- **Your key stays on your server.** It's stored in `data/wallet.key` (permissions `0600`)
  and every transaction is signed locally. Whoever can log into the server
  can read the key, so lock the server down, and back up `data/`.
- **Only the paired chat is obeyed.** To change owner, delete the `owner_chat_id`
  setting, or set `TELEGRAM_CHAT_ID` in `.env`.
- **Replacing a wallet never deletes the old key.** It's kept as `wallet.key.bak-*`.
- **Wallet changes are blocked while LIVE.** Switch to paper first.
- **Use a dedicated hot wallet** holding only what you can afford to lose.

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
 Telegram UI  ◄────────────────────────────  exit engine (every trade + 1s tick) ─► SELL
```

Execution is Solana-only, funded in SOL. Base, BSC and ETH tokens are alerts
only, because buying them would need ETH or BNB for gas plus a bridge, which is
too slow for sniping.

## Without Telegram (CLI)

```
python -m sniper [-p degen|balanced|safe] run [--live]   # settings from config.yaml
python -m sniper scan                                    # filters only, no trading
python -m sniper stats [--live] [--days N]
python -m sniper wallet | keygen
python -m sniper sell <mint>                             # emergency: sell whole balance
```
`config.example.yaml` documents every setting. Settings changed in Telegram are
stored as overrides on top of it.

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

## Reliability

Built to run unattended:
- **No position is dropped on a hiccup.** A failed sell retries with backoff
  (up to once a minute) and re-checks the wallet each time, so a sell that landed
  late is recognized, not repeated. A position is only written off after about
  40 failed attempts (30+ minutes), and you get a Telegram message telling you so.
- **No double sells.** Once a transaction is sent, the bot never sends a second
  one for the same exit; it waits for the first to land or expire.
- **No orphaned buys.** If an RPC error hides a buy's outcome, the bot checks
  the wallet and starts managing any tokens that arrived.
- **One slow trade never blocks the others.** Each exit runs on its own, and
  the live trade stream never waits on a sell or on Telegram.
- **Jito down or rate limited?** Transactions fall back to plain RPC, because
  getting out matters more than MEV protection.
- **Crashes restart themselves.** Each internal loop is supervised, and Docker
  restarts the whole bot if it ever exits. Positions reload on start.
- **Memory stays flat** however long it runs, and nothing polls while paused.
- **Secrets stay out of messages.** RPC URLs and the bot token never appear in
  logs or Telegram errors, and token names are escaped so they can't inject links.

## Known limitations

- Copy trading sees pump.fun and PumpSwap trades from PumpPortal, and needs a
  PumpPortal API key. A followed wallet trading on other DEXes isn't seen.
- Dev reputation only knows about launches seen while the bot was running.
  Leave it running paused for a day before trading to build up history (it keeps
  learning while paused).
- No web dashboard; Telegram is the UI.
- EVM chains are alert-only (capped at 20 alerts an hour).
- The trading code has been tested offline (73 tests) and the Docker image has
  been built and run, but it hasn't placed real trades against mainnet yet.
  Start in paper mode, then do a first live run with a tiny `buy_amount_sol`.

## Tests

```bash
pip install -e '.[dev]' && pytest     # 73 tests, no network needed
```
