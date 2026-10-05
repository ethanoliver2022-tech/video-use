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
| 📡 **Multi-region broadcast** | Every bundle goes to all five Jito regions (NY, Amsterdam, Frankfurt, Tokyo, SLC) at once, plus any extra RPCs you add. | "Turbo mode" |
| 💸 **Auto priority fee** | Pays a percentile of the network's recent priority fees, with a floor and a cap. | "Auto fee" |
| 🎯 **Multi-chain discovery** | pump.fun launches and graduations (live websocket), plus new pools on Solana, Base, BSC and ETH from GeckoTerminal and DexScreener. | Photon/BullX "new pairs" |
| 🎯 **Targeted snipers** | Three modes: snipe every launch that passes your filters, only *targeted* launches, or off. Targeted means a watchlist of devs whose next pump.fun launch is bought instantly, plus keywords matched in the name or ticker. | Banana Gun / Maestro "dev sniper", Trojan "auto-snipe" |
| 👥 **Copy trading** | Mirrors buys from wallets you follow, can follow their sells out, and sets a size per wallet. Wallets can be added or removed live from Telegram. | GMGN/Trojan copy trade |
| 🔔 **Wallet tracker** | Alerts you when a tracked wallet buys or sells, with one-tap Buy and Token-card buttons, without copying anything. Switch any wallet between track and copy with one tap. | GMGN / Cielo wallet tracking |
| 📋 **Limit orders** | Buy on a dip or a breakout, or sell at a profit or a custom stop. They expire on their own, survive restarts, and are created and cancelled from Telegram. | Trojan / Photon / BullX limit orders |
| 🔍 **Token card** | Paste any address to get price, market cap, liquidity, volume, % change, buys vs sells, age, bonding-curve progress, dev holdings, and a verdict from your own filters, with buy and limit buttons. | Photon / Axiom token page |
| 🔍 **Early-flow confirmation** | Watches the first N seconds of trading before buying. It catches bundled launches (several wallets buying identical sizes), whale-dominated launches, dev dumps and thin interest. | Axiom/BullX "bundle checker" |
| 🧠 **Dev reputation** | Records every pump.fun launch and rejects serial launchers. Devs who dumped on you are blocklisted automatically, and the blocklist persists. | GMGN "dev history" |
| 🌐 **Socials check** | Reads the token metadata, can require Twitter/Telegram/website links, and rejects copycats that reuse another launch's socials. | Photon "socials filter" |
| 🍯 **Honeypot check** | Quotes a buy and then a sell before entering. A token that can't be sold, or loses too much on the round trip, is rejected. | "Honeypot / tax check" |
| 🛡 **On-chain rug filters** | Rejects tokens where mint or freeze authority isn't revoked, or with dangerous Token-2022 extensions. Also checks top-10 *wallet* concentration (curve/LP vaults excluded), dev buy size and RugCheck flags. | Standard on all paid bots |
| 📈 **Smart exits** | Exits on: dev sells, copied wallet sells, stop loss, breakeven stop after the first take-profit, trailing stop, sell pressure, KOL buys, a take-profit ladder, max hold time, a token going quiet, or (optional) the moment it migrates off the curve. | "Auto sell", "trailing stop", Axiom "auto-sell on migration" |
| 💰 **Sell initials** | At a target gain (for example 2x), sells just enough to get your SOL back, so the rest rides for free. | Trojan / BullX "sell initials" |
| 🌙 **Moonbag** | After taking profit, keeps a slice (for example 10%) that ignores time and stale exits. It still leaves on a dev dump, its own wide trailing stop, or a max hold, and doesn't use up a position slot. | BullX / Photon "moonbag" |
| 📱 **Telegram-first** | The whole bot runs from a chat: wallet, deposit, withdraw, paper/live, every setting, presets, copy trading, manual trades, stats. | Trojan/BonkBot UI |
| 💾 **Restart-safe** | Positions, settings, copy wallets and reputation data are stored in SQLite. Open positions are reloaded after a restart and checked against the wallet. | Hosted bots |
| 🎚 **Presets** | `degen`, `balanced` or `safe`, each with one-line overrides. | "Strategy presets" |
| 📊 **Analytics** | Win rate, PnL, average win/loss, and breakdowns by exit reason and by source (including dev snipes, keyword snipes and limit orders). A daily report arrives in Telegram every morning (UTC). | GMGN PnL cards |

### Where it beats the paid bots

- **No fee on your trades.** Paid bots typically take around 1% of every buy and
  sell. This bot charges nothing on top of network, Jito, pump.fun and PumpPortal costs.
- **Your key never leaves your server.** Hosted bots hold your private key on
  their servers.
- **Every rule is visible and adjustable.** Filters and exits are plain settings,
  not a black box, and the stats show which rule makes or loses you money.
- **Filters stack.** Dev reputation, socials reuse and bundle detection run on
  every automatic buy. Migrations, copy trades and other DEX tokens also get the
  honeypot round-trip, Token-2022, holder and RugCheck checks. Brand-new pump.fun
  launches skip those: the pump.fun program already guarantees revoked
  authorities, and the lookups would only cost speed.
- **Paper mode with the same logic.** Test any strategy risk-free before going live.
  Paper fills pay what live trades pay: price impact, pump.fun's fee, the priority
  fee, the Jito tip and PumpPortal's fee. What paper can't show is latency: live, you
  sometimes fill a little later (and higher) than the price paper uses.

### How fast is it?

What a snipe waits on, from the launch message to the transaction leaving:
the pump.fun launch arrives on PumpPortal's websocket, the filters run (in
memory and SQLite, plus at most 0.7s for the optional copycat-socials check;
nothing when socials aren't checked), PumpPortal builds the transaction (one
HTTP round trip), and it's signed locally and sent to all five Jito regions at
once. The wallet balance and the priority fee are kept fresh in the background,
so neither costs a round trip on the buy itself. Fills are polled every 0.4s.

The fastest paid bots are still a little quicker at the very first block: they
build pump.fun transactions themselves instead of asking PumpPortal (saving that
round trip, roughly 50–200ms), read the chain through Yellowstone gRPC or
ShredStream on servers next to the validators, and some pay for private relays.
That matters most for a block-0 snipe of a hyped launch; for everything after
the first second (the confirmation window, copy trades, exits) the difference
is small. To get as close as possible: a paid RPC (Helius, Triton, QuickNode), a
server in Frankfurt or New York near PumpPortal and Jito, and a higher
`jito_tip_sol` when you're competing for the first block.

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
| Research a token | Paste its address: you get the full token card with a verdict from your filters |
| Buy a token yourself | Token card → pick an amount (runs your filters; ⚠️ option skips them) |
| Buy on a dip / breakout | Token card → 📋 Limit buy → `0.1 -30` (buy 0.1 SOL after a 30% dip) |
| Take profit / custom stop | Position → 📋 Limit sell → `50 +100` (half at 2x) or `100 -20` |
| See or cancel orders | 📋 Orders → ✖️ Cancel |
| Snipe only certain devs or narratives | 🎯 Snipers → Snipe mode: targeted → set Dev watchlist / Keywords |
| Get your SOL back at 2x, keep a moonbag | ⚙️ Settings → 🚪 Exits → Sell initials at `100`, Moonbag `10` |
| Copy a wallet | 👥 Copy & track → ➕ Add wallet → `<address> [label] [sol]` |
| Just watch a wallet | 👥 Copy & track → ➕ Add wallet → `<address> [label] track`, or `/track <address> [label]` |
| Stop everything | ⏸ Pause sniping (open positions are still managed and exited) |

Every change is saved on the server and survives restarts: wallet, settings,
preset, paper/live mode, pause state, positions, limit orders, copy and tracked
wallets, and the blocklist.

### Security

Who can do what:

- **Only you control the bot.** It obeys one private chat: yours, paired with a
  one-time code that only appears in the server log. Groups are refused (every
  member would be an owner), and every message and button press is also checked
  against your Telegram user. Anyone else who finds the bot gets no reply.
- **Your key stays on your server.** It's stored in `data/wallet.key` (permissions
  `0600`, folder `0700`) and every transaction is signed locally; it's never sent
  anywhere and never written to a log. The database (your trades, your chat id) is
  private to the server's owner too. Whoever can log into the server as root can
  read the key, so use SSH keys (not passwords) and keep the server updated.
- **Lock it down further from `.env`** (Telegram can't change these, so they hold
  even if someone got into your Telegram account):
  `WITHDRAW_ALLOWLIST=<your Phantom address>` allows withdrawals only there, and
  `ALLOW_KEY_EXPORT=false` stops the key ever being shown in chat. Recommended
  once you're set up.
- **Turn on Telegram two-step verification** (Settings → Privacy and Security):
  your Telegram account is the remote control.
- **Pasted keys are deleted.** A private key sent to the chat (outside Import) is
  deleted at once; an exported key is shown behind a spoiler and deleted after 60s.
- **Replacing a wallet never deletes the old key.** It's kept as `wallet.key.bak-*`,
  and it's blocked while any LIVE position is still held by the current wallet.
- **Pinned dependencies.** The image installs exact, hash-checked versions, so a
  compromised or broken new release of a library can't slip in on rebuild.
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
- **Exits get through a crash.** A sell that fails because the price moved past
  your slippage is retried within seconds, with more slippage each time (up to
  50%). Dev-dump and stop-loss exits start at 1.5x your slippage.
- **No orphaned buys.** If an RPC error hides a buy's outcome, the bot never
  writes it off as failed: it keeps watching the wallet (across restarts) until the
  transaction can no longer land, and adopts the tokens the moment they show up.
- **One slow trade never blocks the others.** Each exit runs on its own, and
  the live trade stream never waits on a sell or on Telegram.
- **Jito down or rate limited?** Transactions fall back to plain RPC, because
  getting out matters more than MEV protection.
- **Crashes restart themselves.** Each internal loop is supervised, and Docker
  restarts the whole bot if it ever exits. Positions reload on start.
- **Memory stays flat** however long it runs, and nothing polls while paused.
- **Secrets stay out of messages.** RPC URLs and the bot token never appear in
  logs or Telegram errors, and token names are escaped so they can't inject links.
  A private key pasted into the chat outside the import flow is deleted at once.
- **Nothing runs twice after a restart.** Buttons tapped or commands sent while
  the bot was offline are ignored on startup, so an old "Buy" tap never fires late.
- **No false "graduated" calls.** A brand-new token whose bonding curve an RPC
  node hasn't indexed yet keeps being priced from the curve, instead of being
  written off as dead.

## Not included (and why)

- **Trading on Base / BSC / ETH.** You fund the bot with SOL. Trading EVM chains
  would mean bridging to ETH or BNB first, and a bridge takes minutes, which is
  far too slow for sniping. Those chains stay alert-only.
- **X/Twitter monitoring.** Watching KOL tweets needs X's paid API, which costs
  far more than the bot itself.
- **A web dashboard / charts terminal.** Telegram covers control. For charts, the
  token card links to DexScreener.
- **Multi-wallet buying.** It only helps hide size, and it multiplies fees and key
  management for a hot wallet that should stay small.

## Known limitations

- Copy trading sees pump.fun and PumpSwap trades from PumpPortal, and needs a
  PumpPortal API key. A followed wallet trading on other DEXes isn't seen.
- Dev reputation only knows about launches seen while the bot was running.
  Leave it running paused for a day before trading to build up history (it keeps
  learning while paused).
- No web dashboard; Telegram is the UI.
- EVM chains are alert-only (capped at 20 alerts an hour).
- The trading code has been tested offline (150+ tests, including crash, chaos, clock-jump and malformed-data fuzzing) and the Docker image has
  been built and run, but it hasn't placed real trades against mainnet yet.
  Start in paper mode, then do a first live run with a tiny `buy_amount_sol`.

## Tests

```bash
pip install -e '.[dev]' && pytest     # 150+ tests, no network needed
```
