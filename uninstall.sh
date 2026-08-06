#!/usr/bin/env bash
set -euo pipefail

DEFAULT_INSTALL_DIR="/mnt/user/appdata/vfe-bot-t"
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
PURGE=false
REMOVE_IMAGE=false
ASSUME_YES=false

usage() {
  cat <<'USAGE'
Usage: bash uninstall.sh [options]

Options:
  --install-dir PATH  Installation directory (default: /mnt/user/appdata/vfe-bot-t)
  --remove-image      Also remove the VFE-BOT-T Docker image
  --purge             Permanently delete .env, database, data, and repository files
  --yes               Do not ask for confirmation
  -h, --help          Show this help

Without --purge, the container and Compose network are removed but configuration,
database, and repository files are kept for an easy reinstall.
USAGE
}

while (($#)); do
  case "$1" in
    --install-dir)
      [[ $# -ge 2 ]] || { echo "--install-dir requires a path" >&2; exit 2; }
      INSTALL_DIR="$2"
      shift 2
      ;;
    --remove-image)
      REMOVE_IMAGE=true
      shift
      ;;
    --purge)
      PURGE=true
      REMOVE_IMAGE=true
      shift
      ;;
    --yes|-y)
      ASSUME_YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# When run from the checked-out repository, prefer that location unless the user
# explicitly supplied INSTALL_DIR.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P || true)"
if [[ "${INSTALL_DIR:-}" == "$DEFAULT_INSTALL_DIR" && -f "$SCRIPT_DIR/docker-compose.yml" ]]; then
  INSTALL_DIR="$SCRIPT_DIR"
fi
INSTALL_DIR="$(realpath -m -- "$INSTALL_DIR")"

case "$INSTALL_DIR" in
  /|/mnt|/mnt/user|/mnt/user/appdata|/home|/root|/tmp)
    echo "Refusing unsafe installation directory: $INSTALL_DIR" >&2
    exit 1
    ;;
esac

if [[ ! -d "$INSTALL_DIR" ]]; then
  echo "VFE-BOT-T installation directory does not exist: $INSTALL_DIR"
  exit 0
fi

if $PURGE && { [[ ! -f "$INSTALL_DIR/docker-compose.yml" ]] || [[ ! -d "$INSTALL_DIR/vfe_bot" ]]; }; then
  echo "Refusing to purge a directory that does not look like VFE-BOT-T: $INSTALL_DIR" >&2
  exit 1
fi

if ! $ASSUME_YES; then
  echo "This will stop and remove the VFE-BOT-T container."
  if $PURGE; then
    echo "WARNING: --purge also permanently deletes:"
    echo "  $INSTALL_DIR/.env"
    echo "  $INSTALL_DIR/data"
    echo "  all repository files under $INSTALL_DIR"
    read -r -p "Type PURGE to continue: " answer </dev/tty
    [[ "$answer" == "PURGE" ]] || { echo "Cancelled."; exit 0; }
  else
    read -r -p "Continue? [y/N]: " answer </dev/tty
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
  fi
fi

container_name="vfe-bot-t"
if [[ -f "$INSTALL_DIR/.env" ]]; then
  configured_name="$(awk -F= '$1 == "BOT_CONTAINER_NAME" {sub(/^[^=]*=/, ""); print; exit}' "$INSTALL_DIR/.env" 2>/dev/null || true)"
  [[ -n "$configured_name" ]] && container_name="$configured_name"
fi

image_ids=""
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1 && [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
    image_ids="$(cd "$INSTALL_DIR" && docker compose images -q 2>/dev/null | sort -u || true)"
    if ! (cd "$INSTALL_DIR" && docker compose down --remove-orphans); then
      echo "Compose cleanup failed; trying direct container removal." >&2
      docker rm -f "$container_name" >/dev/null 2>&1 || true
    fi
  else
    docker rm -f "$container_name" >/dev/null 2>&1 || true
  fi

  if $REMOVE_IMAGE; then
    while IFS= read -r image_id; do
      [[ -n "$image_id" ]] && docker image rm "$image_id" >/dev/null 2>&1 || true
    done <<< "$image_ids"
    docker image rm ghcr.io/flavius-vfe/vfe-bot-t:latest >/dev/null 2>&1 || true
  fi
else
  echo "Docker was not found; skipping container cleanup." >&2
fi

if $PURGE; then
  cd /
  rm -rf -- "$INSTALL_DIR"
  echo "VFE-BOT-T was completely removed."
else
  echo "VFE-BOT-T container removed. Configuration and data were kept in:"
  echo "  $INSTALL_DIR"
  echo "Reinstall: cd '$INSTALL_DIR' && bash install.sh"
  echo "Permanent removal: bash '$INSTALL_DIR/uninstall.sh' --purge"
fi
