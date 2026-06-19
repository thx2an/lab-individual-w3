#!/usr/bin/env bash
# scale_replicas.sh — scale a Ronki service to N replicas.
#
#   bash scale_replicas.sh --service <name> [--replicas <N>] [--dry-run]
#
# Note: lab services use a fixed container_name, so N>1 is illustrative of the
# pattern (real prod would drop container_name or use Swarm/K8s).
# Exit 0 = success / dry-run, non-zero = failure.
set -euo pipefail

SERVICE=""
REPLICAS=2
DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../../data-pack/configs/docker-compose.yml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)  SERVICE="$2";  shift 2 ;;
    --replicas) REPLICAS="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true;  shift ;;
    *) echo "[scale_replicas] Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[scale_replicas] ERROR: --service <name> required"; exit 1
fi

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: docker compose -f $COMPOSE_FILE up -d --scale ${SERVICE}=${REPLICAS} --no-recreate"
  exit 0
fi

echo "[scale_replicas] Scaling $SERVICE to $REPLICAS replicas..."
docker compose -f "$COMPOSE_FILE" up -d --scale "${SERVICE}=${REPLICAS}" --no-recreate
echo "[scale_replicas] Scale command sent for $SERVICE → $REPLICAS."
exit 0
