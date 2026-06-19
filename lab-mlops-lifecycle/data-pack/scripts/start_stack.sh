#!/usr/bin/env bash
# start_stack.sh — Khởi động MLflow + PostgreSQL via Docker Compose
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../configs/docker-compose.yml"

echo "[start_stack] Starting MLflow + PostgreSQL..."
docker compose -f "$COMPOSE_FILE" up -d

echo "[start_stack] Waiting for MLflow to be ready..."
MAX_WAIT=60
ELAPSED=0
until curl -sf http://localhost:5000/health > /dev/null 2>&1; do
    if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
        echo "[start_stack] ERROR: MLflow did not become ready within ${MAX_WAIT}s"
        docker compose -f "$COMPOSE_FILE" logs mlflow
        exit 1
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

echo "[start_stack] MLflow is ready at http://localhost:5000"
echo "[start_stack] Set tracking URI: export MLFLOW_TRACKING_URI=http://localhost:5000"
