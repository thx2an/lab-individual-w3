#!/usr/bin/env bash
# inject_synthetic_alert.sh — POST a synthetic alert straight to Alertmanager.
#
# Used for acceptance test #6 (LLM hallucination defense): fire an alert with
# alertname=TestHallucination so the orchestrator's decision-validation path
# rejects the (non-existent) mapped runbook.
#
#   bash inject_synthetic_alert.sh [alertname] [service]
#
# Defaults: alertname=TestHallucination, service=payment-svc
set -euo pipefail

ALERTNAME="${1:-TestHallucination}"
SERVICE="${2:-payment-svc}"
AM_URL="${AM_URL:-http://localhost:9093}"

# endsAt 10 minutes in the future so the alert stays "active" long enough to poll.
ENDS_AT=$(date -u -v+10M +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
          || date -u -d "+10 minutes" +"%Y-%m-%dT%H:%M:%SZ")

echo "[inject_synthetic_alert] Firing $ALERTNAME on $SERVICE → $AM_URL (endsAt=$ENDS_AT)"
curl -sf -XPOST "$AM_URL/api/v2/alerts" \
  -H "Content-Type: application/json" \
  -d "[{
        \"labels\": {
          \"alertname\": \"$ALERTNAME\",
          \"service\": \"$SERVICE\",
          \"severity\": \"critical\"
        },
        \"annotations\": { \"summary\": \"synthetic $ALERTNAME for acceptance test\" },
        \"endsAt\": \"$ENDS_AT\"
      }]"
echo
echo "[inject_synthetic_alert] Sent. Verify with:"
echo "  curl -s $AM_URL/api/v2/alerts | python3 -m json.tool"
