#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGS_DIR="$SCRIPT_DIR/../configs"

echo "[start_stack] Starting Ronki lab stack..."
docker compose -f "$CONFIGS_DIR/docker-compose.yml" up -d --build

echo "[start_stack] Waiting for services to become healthy (30s)..."
sleep 30

echo "[start_stack] Checking service health..."
SERVICES=(
  "frontend:http://localhost:8080/health"
  "api-gateway:http://localhost:8081/health"
  "payment-svc:http://localhost:8082/health"
  "inventory-svc:http://localhost:8083/health"
  "checkout-svc:http://localhost:8084/health"
  "prometheus:http://localhost:9090/-/healthy"
  "alertmanager:http://localhost:9093/-/healthy"
)

ALL_OK=true
for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  url="${entry#*:}"
  if curl -sf "$url" > /dev/null 2>&1; then
    echo "  [OK]   $name — $url"
  else
    echo "  [FAIL] $name — $url (may still be starting up)"
    ALL_OK=false
  fi
done

if $ALL_OK; then
  echo ""
  echo "[start_stack] All services up."
else
  echo ""
  echo "[start_stack] Some services not yet ready — wait 15s and retry curl manually."
fi

echo ""
echo "  Prometheus UI   : http://localhost:9090"
echo "  Alertmanager UI : http://localhost:9093"
echo "  Active alerts   : curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool"
