#!/usr/bin/env bash
# One-paste installer for a fresh Ubuntu server:
#   curl -fsSL https://raw.githubusercontent.com/ethanoliver2022-tech/video-use/claude/memecoin-sniping-bot-x4vgsq/memecoin-sniper/install.sh | sudo bash
# Safe to re-run: it updates the code and keeps your .env, config and data.
set -euo pipefail
# The whole script is one block, so bash reads all of it before running anything: updating
# replaces this very file (git reset), which must never change what's already running.
{

REPO="${SNIPER_REPO:-https://github.com/ethanoliver2022-tech/video-use.git}"
BRANCH="${SNIPER_BRANCH:-claude/memecoin-sniping-bot-x4vgsq}"
DIR="${SNIPER_DIR:-/opt/sniper}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (prefix the command with sudo)." >&2
  exit 1
fi

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ufw curl >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi

say "Locking down the firewall (SSH only; the bot needs no open ports)"
ufw allow OpenSSH >/dev/null
# also every port sshd really listens on, so a custom SSH port can't lock you out
for port in $(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}'); do
  ufw allow "$port/tcp" >/dev/null
done
ufw --force enable >/dev/null

if [ -d "$DIR/.git" ]; then
  say "Updating the bot"
  git -C "$DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$DIR" reset -q --hard FETCH_HEAD
else
  say "Downloading the bot"
  git clone -q --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR/memecoin-sniper"

if [ ! -f .env ]; then
  say "Settings"
  # read from the terminal even when this script is piped from curl
  read -rp "Telegram bot token from @BotFather: " TOKEN </dev/tty
  while ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; do
    read -rp "That doesn't look like a bot token, paste it again: " TOKEN </dev/tty
  done
  read -rp "Solana RPC URL, e.g. from Helius (Enter to skip; needed before going live): " RPC </dev/tty
  read -rp "PumpPortal API key (Enter to skip; enables live trade feed + copy trading): " PP </dev/tty
  read -rp "Jupiter API key from portal.jup.ag (Enter to skip): " JUP </dev/tty
  umask 077
  cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_CHAT_ID=
SOLANA_RPC_URL=$RPC
PUMPPORTAL_API_KEY=$PP
JUPITER_API_KEY=$JUP
EOF
  umask 022
fi
# every optional setting gets a line (with what it does), so it's there to fill in later;
# older installs get the ones they're missing. Existing values are never touched.
add_key() {  # add_key KEY DEFAULT "explanation"
  if ! grep -q "^$1=" .env; then
    printf '# %s\n%s=%s\n' "$3" "$1" "$2" >> .env
  fi
}
add_key PUMPPORTAL_API_KEY "" "Optional: live trade feed + copy trading (pumpportal.fun, billed per message)"
add_key JUPITER_API_KEY "" "Recommended: free key from portal.jup.ag (faster Jupiter quotes)"
add_key JUPITER_RPM "" "Only on a paid Jupiter plan: its requests per minute"
add_key WITHDRAW_ALLOWLIST "" "Security: withdrawals only to these addresses (comma-separated), e.g. your Phantom wallet"
add_key ALLOW_KEY_EXPORT "true" "Security: set to false so the private key can never be shown in Telegram"
add_key EXTRA_RPC_URLS "" "Optional: more RPC URLs (comma-separated) every trade is also sent through, so more trades land"
add_key EXTRA_ALLOWED_PROGRAMS "" "Advanced: extra program ids the transaction check may allow (only if the bot asks)"
chmod 600 .env                     # bot token, API keys: owner only
mkdir -p data && chmod 700 data    # wallet key + database: owner only

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
fi

# mount config.yaml into the container (kept out of the main compose file so a
# missing config.yaml can never be turned into a directory by Docker)
cat > docker-compose.override.yml <<'EOF'
services:
  sniper:
    volumes:
      - ./config.yaml:/app/config.yaml:ro
EOF

mkdir -p data
say "Building and starting the bot (first build takes a minute or two)"
# always recreate so the logs below are from this run (positions persist across restarts)
docker compose up -d --build --force-recreate

say "Waiting for the bot to start"
CODE=""
READY=""
for _ in $(seq 1 60); do   # up to 2 minutes; ends on the first definite answer
  LOGS="$(docker compose logs 2>/dev/null || true)"
  CODE="$(printf '%s' "$LOGS" | grep -o '/start [0-9A-F]\{10\}' | tail -1 || true)"
  if printf '%s' "$LOGS" | grep -q "telegram ready: paired"; then READY=1; fi
  if [ -n "$CODE" ] || [ -n "$READY" ] || printf '%s' "$LOGS" | grep -q "rejected the bot token"; then
    break
  fi
  sleep 2
done

echo
if docker compose logs 2>/dev/null | grep -q "rejected the bot token"; then
  echo "❌ Telegram rejected the bot token. Fix TELEGRAM_BOT_TOKEN in $DIR/memecoin-sniper/.env"
  echo "   (nano .env), then run:  cd $DIR/memecoin-sniper && docker compose up -d"
  exit 1
fi
if [ -n "$CODE" ]; then
  printf '\033[1;32m✅ Running! Open your bot in Telegram and send:\n\n    %s\033[0m\n\n' "$CODE"
elif [ -n "$READY" ]; then
  printf '\033[1;32m✅ Running and already paired. Open your bot in Telegram and send /menu\033[0m\n\n'
else
  echo "⚠️  The bot didn't report in yet. Check the logs with:"
  echo "    cd $DIR/memecoin-sniper && docker compose logs --tail 50"
fi
echo "Add or change keys later:  nano $DIR/memecoin-sniper/.env   then: docker compose up -d"
echo "Useful commands (run in $DIR/memecoin-sniper):"
echo "  docker compose logs -f --tail 50    # live logs"
echo "  docker compose restart              # restart"
echo "  sudo bash install.sh                # update to the latest version"
exit 0
}
