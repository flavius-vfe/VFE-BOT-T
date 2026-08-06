#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/flavius-vfe/VFE-BOT-T.git}"
INSTALL_DIR="${INSTALL_DIR:-/mnt/user/appdata/vfe-bot-t}"

if [[ ! -f "docker-compose.yml" || ! -d "vfe_bot" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "git is required." >&2
    exit 1
  fi
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" pull --ff-only
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR"
  exec bash ./install.sh
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required (docker compose)." >&2
  exit 1
fi

mkdir -p data
chmod 700 data

if [[ ! -f .env ]]; then
  echo "VFE Docker Bot setup"
  echo
  read -r -p "Telegram bot token: " TELEGRAM_TOKEN </dev/tty
  read -r -p "Pairing code (leave empty to generate one): " PAIRING_CODE </dev/tty
  if [[ -z "$TELEGRAM_TOKEN" ]]; then
    echo "Telegram token cannot be empty." >&2
    exit 1
  fi
  if [[ -z "$PAIRING_CODE" ]]; then
    PAIRING_CODE="$(openssl rand -hex 6 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(6))')"
  fi
  if [[ ! "$PAIRING_CODE" =~ ^[A-Za-z0-9_-]{6,64}$ ]]; then
    echo "Pairing code must be 6-64 letters, numbers, underscores, or hyphens." >&2
    exit 1
  fi
  cat > .env <<ENV
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
PAIRING_CODE=$PAIRING_CODE
TZ=Europe/Bucharest
BOT_CONTAINER_NAME=vfe-bot-t
PROTECTED_CONTAINERS=
NOTIFY_CONTAINER_CHANGES=true
POLL_INTERVAL_SECONDS=30
LOG_LINES_DEFAULT=50
LOG_LINES_MAX=300
DATABASE_PATH=/data/vfe-bot.db
HOST_STORAGE_PATH=/host-mnt/user
ENV
  chmod 600 .env
  echo
  echo "Pairing code: $PAIRING_CODE"
  echo "After startup, open your bot in Telegram and send: /pair $PAIRING_CODE"
fi

docker compose up -d --build

echo
echo "VFE Docker Bot is running."
echo "Logs: docker compose logs -f"
echo "Update later: git pull && docker compose up -d --build"
