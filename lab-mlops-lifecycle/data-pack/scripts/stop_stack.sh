#!/usr/bin/env bash
# stop_stack.sh — Dừng MLflow + PostgreSQL stack, giữ volumes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../configs/docker-compose.yml"

echo "[stop_stack] Stopping stack..."
docker compose -f "$COMPOSE_FILE" down

echo "[stop_stack] Stack stopped. Volumes (postgres_data, mlflow_artifacts) preserved."
echo "[stop_stack] To also remove volumes: docker compose -f $COMPOSE_FILE down -v"
