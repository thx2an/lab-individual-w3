#!/usr/bin/env bash
# clear_cache.sh — flush a service's in-memory cache.
#
#   bash clear_cache.sh --service <name> [--dry-run]
#
# The mock services have no real cache, so we send SIGHUP (the conventional
# "reload/flush" signal). Swap this for a real POST /admin/cache/clear in prod.
# Exit 0 = success / dry-run, non-zero = failure.
set -euo pipefail

SERVICE=""
DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "[clear_cache] Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[clear_cache] ERROR: --service <name> required"; exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: docker kill --signal=SIGHUP $CONTAINER"
  exit 0
fi

if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  echo "[clear_cache] ERROR: container $CONTAINER not found."; exit 1
fi

echo "[clear_cache] Sending SIGHUP to $CONTAINER to flush cache..."
docker kill --signal=SIGHUP "$CONTAINER"
echo "[clear_cache] Cache flush triggered on $CONTAINER."
exit 0
