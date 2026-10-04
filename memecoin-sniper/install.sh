#!/usr/bin/env bash
# One-paste installer for a fresh Ubuntu server:
#   curl -fsSL https://raw.githubusercontent.com/ethanoliver2022-tech/video-use/claude/memecoin-sniping-bot-x4vgsq/memecoin-sniper/install.sh | sudo bash
# Safe to re-run: it updates the code and keeps your .env, config and data.
set -euo pipefail

REPO="${SNIPER_REPO:-https://github.com/ethanoliver2022-tech/video-use.git}"
BRANCH="${SNIPER_BRANCH:-claude/memecoin-sniping-bot-x4vgsq}"
DIR="${SNIPER_DIR:-/opt/sniper}"
JITO_REGION="${SNIPER_JITO:-frankfurt}"   # ny | amsterdam | frankfurt | tokyo | slc

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
  read -rp "Solana RPC URL (Enter to skip; strongly recommended before going live): " RPC </dev/tty
  umask 077
  cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_CHAT_ID=
SOLANA_RPC_URL=$RPC
EOF
  umask 022
fi

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  sed -i "s#^    - https://mainnet.block-engine.jito.wtf#    - https://${JITO_REGION}.mainnet.block-engine.jito.wtf#" config.yaml
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
docker compose up -d --build

say "Waiting for the bot to start"
CODE=""
for _ in $(seq 1 45); do
  CODE="$(docker compose logs 2>/dev/null | grep -o '/start [0-9A-F]\{6\}' | tail -1 || true)"
  if [ -n "$CODE" ] || docker compose logs 2>/dev/null | grep -q "sniper starting"; then
    sleep 2
    CODE="$(docker compose logs 2>/dev/null | grep -o '/start [0-9A-F]\{6\}' | tail -1 || true)"
    break
  fi
  sleep 2
done

echo
if [ -n "$CODE" ]; then
  printf '\033[1;32m✅ Running! Open your bot in Telegram and send:\n\n    %s\033[0m\n\n' "$CODE"
elif docker compose logs 2>/dev/null | grep -q "sniper starting"; then
  printf '\033[1;32m✅ Running and already paired. Open your bot in Telegram and send /menu\033[0m\n\n'
else
  echo "⚠️  The bot didn't report in yet. Check the logs with:"
  echo "    cd $DIR/memecoin-sniper && docker compose logs --tail 50"
fi
echo "Useful commands (run in $DIR/memecoin-sniper):"
echo "  docker compose logs -f --tail 50    # live logs"
echo "  docker compose restart              # restart"
echo "  sudo bash install.sh                # update to the latest version"
