#!/usr/bin/env bash
# inject_fault.sh — inject chaos into a running container
#
# Usage:
#   bash inject_fault.sh <fault_type> <container_name> [param]
#
# Fault types:
#   latency  <container> <delay>   e.g. latency payment-svc 500ms
#   kill     <container>           stop the container (simulates crash)
#   pause    <container>           pause container (simulates freeze)
#   resume   <container>           resume a paused container
#   recover  <container>           restart a stopped/killed container
#
# Examples:
#   bash inject_fault.sh latency ronki-payment-svc 500ms
#   bash inject_fault.sh kill    ronki-checkout-svc
#   bash inject_fault.sh recover ronki-checkout-svc

set -euo pipefail

FAULT="${1:-}"
CONTAINER="${2:-}"
PARAM="${3:-}"

if [[ -z "$FAULT" || -z "$CONTAINER" ]]; then
  echo "Usage: $0 <fault_type> <container_name> [param]"
  echo "       fault_type: latency | kill | pause | resume | recover"
  exit 1
fi

# Resolve short name → full container name (prefix ronki- if needed)
if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  CONTAINER="ronki-${CONTAINER}"
fi

if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  echo "[inject_fault] ERROR: container '$CONTAINER' not found."
  echo "               Running containers:"
  docker ps --format '  {{.Names}}'
  exit 1
fi

case "$FAULT" in
  latency)
    DELAY="${PARAM:-200ms}"
    echo "[inject_fault] Adding ${DELAY} network latency to $CONTAINER via tc..."
    # Use nsenter to run tc inside the container's network namespace
    PID=$(docker inspect --format '{{.State.Pid}}' "$CONTAINER")
    if [[ "$PID" == "0" ]]; then
      echo "[inject_fault] ERROR: container is not running."
      exit 1
    fi
    # Convert e.g. "500ms" → "500" for tc
    DELAY_MS="${DELAY//ms/}"
    nsenter -t "$PID" -n -- tc qdisc add dev eth0 root netem delay "${DELAY_MS}ms" 2>/dev/null \
      || nsenter -t "$PID" -n -- tc qdisc change dev eth0 root netem delay "${DELAY_MS}ms"
    echo "[inject_fault] Latency ${DELAY} applied to $CONTAINER."
    echo "               To remove: nsenter -t $PID -n -- tc qdisc del dev eth0 root"
    ;;

  kill)
    echo "[inject_fault] Stopping container $CONTAINER (simulate crash)..."
    docker stop "$CONTAINER"
    echo "[inject_fault] $CONTAINER stopped."
    ;;

  pause)
    echo "[inject_fault] Pausing container $CONTAINER..."
    docker pause "$CONTAINER"
    echo "[inject_fault] $CONTAINER paused. Resume with: $0 resume $CONTAINER"
    ;;

  resume)
    echo "[inject_fault] Resuming container $CONTAINER..."
    docker unpause "$CONTAINER"
    echo "[inject_fault] $CONTAINER resumed."
    ;;

  recover)
    echo "[inject_fault] Starting container $CONTAINER..."
    docker start "$CONTAINER"
    echo "[inject_fault] $CONTAINER started."
    ;;

  clear-latency)
    echo "[inject_fault] Removing tc netem rules from $CONTAINER..."
    PID=$(docker inspect --format '{{.State.Pid}}' "$CONTAINER")
    nsenter -t "$PID" -n -- tc qdisc del dev eth0 root 2>/dev/null || true
    echo "[inject_fault] Latency rules cleared."
    ;;

  --concurrent)
    # Stress 2: inject the same fault on 2 services simultaneously (background subshells)
    SVC1="${CONTAINER}"   # positional arg 2 used as first service
    SVC2="${PARAM}"       # positional arg 3 used as second service
    if [[ -z "$SVC1" || -z "$SVC2" ]]; then
      echo "[inject_fault] --concurrent requires exactly 2 container names"
      echo "               Usage: $0 --concurrent <container1> <container2>"
      exit 1
    fi
    echo "[inject_fault] Injecting latency fault concurrently on $SVC1 and $SVC2..."
    (bash "$0" latency "$SVC1" 500ms) &
    PID1=$!
    (bash "$0" latency "$SVC2" 500ms) &
    PID2=$!
    wait "$PID1"
    wait "$PID2"
    echo "[inject_fault] Concurrent fault injection complete on $SVC1 and $SVC2."
    ;;

  *)
    echo "[inject_fault] Unknown fault type: $FAULT"
    echo "               Supported: latency | kill | pause | resume | recover | clear-latency | --concurrent"
    exit 1
    ;;
esac
