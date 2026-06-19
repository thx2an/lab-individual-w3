#!/usr/bin/env bash
# Scenario 3 driver: 3 consecutive kill→recover cycles on checkout-svc.
# Verify is forced to always fail (baseline up_required=2), so each cycle
# produces VERIFY_FAIL → ROLLBACK_TRIGGERED. After the 3rd: CIRCUIT_BREAKER_HALT.
set -u
cd "$(dirname "$0")/.."
INJECT="../data-pack/scripts/inject_fault.sh"
AUDIT="audit_log.jsonl"
AM="http://localhost:9093/api/v2/alerts"

# wait for orchestrator startup
n=0; until grep -q ORCHESTRATOR_START "$AUDIT" || [ $n -ge 30 ]; do sleep 1; n=$((n+1)); done
sleep 5
: > "$AUDIT"   # clean capture starts here

instancedown_active() {
  curl -s "$AM" | /tmp/clvenv/bin/python -c "import sys,json; a=json.load(sys.stdin); print(any(x['labels'].get('alertname')=='InstanceDown' and x['labels'].get('service')=='checkout-svc' and x['status']['state']=='active' for x in a))"
}

for cyc in 1 2 3; do
  echo ">>> cycle $cyc: kill checkout"
  bash "$INJECT" kill ronki-checkout-svc >/dev/null 2>&1
  # wait until this cycle's failure is recorded (ROLLBACK_RESULT count >= cyc) or breaker halts
  m=0
  until [ "$(grep -c ROLLBACK_RESULT "$AUDIT")" -ge "$cyc" ] || grep -q CIRCUIT_BREAKER_HALT "$AUDIT" || [ $m -ge 100 ]; do sleep 3; m=$((m+1)); done
  if grep -q CIRCUIT_BREAKER_HALT "$AUDIT"; then echo ">>> breaker halted at cycle $cyc"; break; fi
  echo ">>> cycle $cyc: recover checkout + wait for alert to resolve"
  bash "$INJECT" recover ronki-checkout-svc >/dev/null 2>&1
  r=0
  until [ "$(instancedown_active)" = "False" ] || [ $r -ge 40 ]; do sleep 3; r=$((r+1)); done
  sleep 5
done
echo ">>> scen3 driver done"
