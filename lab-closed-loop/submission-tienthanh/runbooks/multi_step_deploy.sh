#!/usr/bin/env bash
# multi_step_deploy.sh — 3-step transactional deploy with ordered rollback.
#
# Forward steps (A→B→C):
#   step-A  drain traffic        (docker stop)
#   step-B  apply new config     (docker restart/start, assert running)
#   step-C  re-enable traffic    (docker start, assert running)
#
# Rollback steps (run in REVERSE order of completion by the orchestrator):
#   rollback-A  restore traffic
#   rollback-B  revert config
#
#   bash multi_step_deploy.sh --service <name> --step-a|--step-b|--step-c [--dry-run]
#   bash multi_step_deploy.sh --service <name> --rollback-a|--rollback-b   [--dry-run]
#
# Deterministic testing (scenario 4): export FAIL_STEP=C (or A/B) before running
# the orchestrator to force that forward step to exit 1 — no container-kill race.
# Exit 0 = success / dry-run, non-zero = failure.
set -euo pipefail

SERVICE=""
DRY_RUN=false
STEP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)    SERVICE="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --step-a)     STEP="A";  shift ;;
    --step-b)     STEP="B";  shift ;;
    --step-c)     STEP="C";  shift ;;
    --rollback-a) STEP="RA"; shift ;;
    --rollback-b) STEP="RB"; shift ;;
    *) echo "[multi_step_deploy] Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[multi_step_deploy] ERROR: --service <name> required"; exit 1
fi
CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] multi_step_deploy step=${STEP:-ALL} on $CONTAINER"
  exit 0
fi

# Deterministic fault injection for acceptance test #4.
if [[ -n "${FAIL_STEP:-}" && "$STEP" == "$FAIL_STEP" ]]; then
  echo "[multi_step_deploy] step-$STEP forced to FAIL via FAIL_STEP=$FAIL_STEP"
  exit 1
fi

assert_running() {
  local s; s=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)
  [[ "$s" == "running" ]] || { echo "[multi_step_deploy] ERROR: $CONTAINER status=$s"; return 1; }
}

case "$STEP" in
  A)  echo "[multi_step_deploy] step-A: draining traffic ($CONTAINER)...";
      docker stop "$CONTAINER" 2>/dev/null || true; echo "step-A complete." ;;
  B)  echo "[multi_step_deploy] step-B: applying config ($CONTAINER)...";
      docker restart "$CONTAINER" 2>/dev/null || docker start "$CONTAINER"; sleep 3; assert_running; echo "step-B complete." ;;
  C)  echo "[multi_step_deploy] step-C: re-enabling traffic ($CONTAINER)...";
      docker start "$CONTAINER" 2>/dev/null || true; sleep 2; assert_running; echo "step-C complete." ;;
  RA) echo "[multi_step_deploy] rollback-A: restoring traffic ($CONTAINER)...";
      docker start "$CONTAINER" 2>/dev/null || true; sleep 2; echo "rollback-A complete." ;;
  RB) echo "[multi_step_deploy] rollback-B: reverting config ($CONTAINER)...";
      docker restart "$CONTAINER" 2>/dev/null || docker start "$CONTAINER"; sleep 3; echo "rollback-B complete." ;;
  *)  echo "[multi_step_deploy] ERROR: specify --step-a/b/c or --rollback-a/b"; exit 1 ;;
esac
exit 0
