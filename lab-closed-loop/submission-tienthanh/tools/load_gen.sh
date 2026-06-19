#!/usr/bin/env bash
# load_gen.sh — generate steady traffic so the mock services emit latency/error
# histograms (Prometheus only sees data when requests actually hit the services).
#
# Run this in its own terminal for the whole lab session. ~20 req/s total,
# matching the baseline capture profile.
#
#   bash load_gen.sh            # hit all 5 services
#   bash load_gen.sh 8082       # hit only payment-svc
set -euo pipefail

PORTS=("${@:-8080 8081 8082 8083 8084}")
# Flatten if passed as a single space-separated arg.
read -r -a PORTS <<< "${PORTS[*]}"

echo "[load_gen] Generating load on ports: ${PORTS[*]} (Ctrl-C to stop)"
while true; do
  for p in "${PORTS[@]}"; do
    curl -s "http://localhost:${p}/" > /dev/null 2>&1 || true
  done
  sleep 0.2
done
