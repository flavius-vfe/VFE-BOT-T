#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true
echo "VFE Docker Bot updated."
