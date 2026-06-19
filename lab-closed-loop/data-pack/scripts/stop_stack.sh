#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$SCRIPT_DIR/../configs"

echo "[stop_stack] Stopping Ronki lab stack..."
docker compose -f "$CONFIGS_DIR/docker-compose.yml" down --volumes --remove-orphans

echo "[stop_stack] Stack stopped and volumes removed."
