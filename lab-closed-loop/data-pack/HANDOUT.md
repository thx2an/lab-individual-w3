# Lab — Closed-Loop Auto-Remediation

**Individual.**

This is a technical lab. You build a working orchestrator — not a simulation, not a diagram. By the end you run 3 chaos scenarios and the orchestrator must handle them automatically following the pattern Detect → Decide → Act → Verify → Rollback.

---

## 1. Context

You are an AIOps engineer at **Ronki** — an e-commerce platform processing ~80,000 orders/day. The production stack has 5 services:

```
Internet
    │
    ▼
┌─────────────┐
│  frontend   │  (React SPA, static assets)
└──────┬──────┘
       │ HTTP
       ▼
┌─────────────┐
│ api-gateway │  (reverse proxy + rate limit)
└──────┬──────┘
       │
   ┌───┴────────────┐
   │                │
   ▼                ▼
┌──────────┐  ┌───────────────┐
│payment-  │  │ inventory-svc │
│   svc    │  └───────┬───────┘
└──────────┘          │
                      ▼
               ┌─────────────┐
               │ checkout-svc│
               └─────────────┘
```

The ops team currently handles incidents manually: receive alert → SSH into server → restart → verify. The process takes 15–45 minutes. During peak hours (11:00–13:00, 19:00–22:00), cascade failures occur on average twice per week. Each 15-minute downtime window costs ~1,000 orders.

**Lab objective**: build a closed-loop orchestrator capable of detecting incidents, deciding on an action, executing it, verifying the result — and automatically rolling back if the action does not resolve the issue.

---

## 2. What You Receive

| Artifact | Description |
|---|---|
| `configs/docker-compose.yml` | 5-service stack + Prometheus + Alertmanager |
| `configs/prometheus.yml` | Scrape config for all services |
| `configs/alert_rules.yml` | 3 alert rules: high latency, high error rate, instance down |
| `scripts/start_stack.sh` | Start the full stack |
| `scripts/stop_stack.sh` | Stop the stack, remove volumes |
| `scripts/inject_fault.sh` | Inject faults into containers (latency, kill, etc.) |
| `data/baseline.json` | Normal baseline metrics for the verify step |
| `data/expected.json` | Expected behavior per chaos scenario |

All data in `data/` is **real** — captured from the stack running under normal conditions. Not mocked.

---

### Observability dashboard

Grafana runs at **http://localhost:3000** (anonymous viewer, no login required). Main dashboard: **"AIOps Closed-Loop"**.

| Row | Panel | Content |
|-----|-------|---------|
| Service Health | 5 stat panels | p99 latency (ms) per service, color-coded at 200 ms / 500 ms thresholds |
| Service Health | Global error rate | Aggregate error rate across the full stack |
| Alert State | Active alerts | List of alerts currently firing from Alertmanager |
| Alert State | Alert timeline | Chart of firing alert count over time |
| Orchestrator State | Actions by outcome | Total actions by result: success / rollback / fail / dry_run |
| Orchestrator State | Circuit-breaker | CLOSED / OPEN state per service |
| Orchestrator State | Blast-radius remaining | Gauge of actions remaining in the current window |
| Orchestrator State | Mutex state | State timeline: FREE / LOCKED per service |
| Action Timeline | Action executions | Actions/minute chart, broken down by service + runbook + outcome |
| Audit Log Tail | Audit log | 100 most recent events from `audit_log.jsonl`, filterable by `event_type` or `service` |

**Prerequisite for dashboard to work**: the orchestrator must import `engine.metrics` and call `start_metrics_server()` — the sample solution already does this. The audit log is read from file `audit_log.jsonl`; for Promtail to read it, run the orchestrator with the environment variable `AUDIT_LOG_PATH=/audit/audit_log.jsonl` or mount the `audit_logs` volume into the closed-loop container if running inside Docker.

Additional requirement: `prometheus_client` must be installed in the orchestrator's Python environment:
```bash
uv pip install prometheus_client
```

The dashboard supports debugging; it is not an acceptance criterion — scoring is based on the 6 scenarios, not on dashboard appearance.

---

## 3. Closed-Loop Safety Pattern

The orchestrator **must** pass all 5 sub-checkpoints for every action. Missing any single checkpoint → the action is not executed.

```
Alert Fired
    │
    ▼
┌─────────────────────────────────────────────────┐
│  1. DETECT — poll Alertmanager API               │
│     → parse alert name, service, severity        │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  2. DECIDE — match alert → runbook               │
│     → check blast-radius limit                   │
│     (max actions/minute, max restarts/hour)       │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  3. DRY-RUN — simulate action, no side effect    │
│     → if dry-run fail → refuse + log             │
└──────────────────┬──────────────────────────────┘
                   │
              dry-run pass
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  4. ACT — execute runbook script                 │
│     → subprocess call with timeout               │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  5a. VERIFY — poll Prometheus 60s, compare       │
│     baseline vs threshold                        │
│                                                  │
│     verify PASS → log success → done             │
│     verify FAIL → trigger rollback               │
└──────────────────┬──────────────────────────────┘
                   │ verify fail
                   ▼
┌─────────────────────────────────────────────────┐
│  5b. ROLLBACK — execute rollback runbook         │
│     → increment failure_count                    │
│     → if failure_count ≥ 3 → CIRCUIT BREAKER    │
│        halt automation, log HALT                 │
└─────────────────────────────────────────────────┘
```

### 5 Sub-checkpoints in detail

| # | Checkpoint | Minimum requirement |
|---|---|---|
| 1 | **Dry-run mode** | Every runbook script must support the `--dry-run` flag; the orchestrator always calls dry-run first |
| 2 | **Blast-radius config** | Config file: `max_actions_per_minute`, `max_restarts_per_service_per_hour`; if exceeded → escalate, do not act |
| 3 | **Verify post-act** | After the action, poll the Prometheus query ≥3 times within 60s, compare against the threshold in `baseline.json` |
| 4 | **Auto-rollback** | Verify fail → call the rollback runbook automatically; no manual intervention required |
| 5 | **Circuit breaker** | 3 consecutive failures (action fail or verify fail) → orchestrator halts automatically; log state `CIRCUIT_OPEN` |

---

## 4. What You Must Build

### Submission directory structure

```
your-name/
├── closed_loop.py          ← main orchestrator
├── runbooks/
│   ├── restart_service.sh
│   ├── scale_replicas.sh
│   └── clear_cache.sh      (minimum 3 scripts)
├── DESIGN.md
└── SUBMIT.md
```

### `closed_loop.py` — orchestrator

Required behavior:

- Poll Alertmanager API `http://localhost:9093/api/v2/alerts` every 15 seconds
- Decide: map `alertname` → runbook script
- All 5 sub-checkpoints (see section 3)
- `--dry-run` flag at orchestrator level (disables all execution, log only)
- Structured JSON log to stdout: every event has `ts`, `event_type`, `service`, `action`, `result`
- Config read from a YAML file (no hardcoded thresholds)

You may choose **one of two** decision engines:

**Option A — Rule-based** (recommended if you do not have an Anthropic API key):
```python
RUNBOOK_MAP = {
    "HighLatency":   "runbooks/restart_service.sh",
    "HighErrorRate": "runbooks/clear_cache.sh",
    "InstanceDown":  "runbooks/restart_service.sh",
}
```

**Option B — LLM-based** (uses Anthropic API):
- Send alert context to Claude, receive JSON `{"action": "restart_service", "confidence": 0.87}`
- Execute only if `confidence >= 0.6`
- Fallback to rule-based if API is unreachable

Whichever option you choose, you must defend the decision in `DESIGN.md`.

### `runbooks/*.sh` — automation scripts

Each script must:

- Accept `--service <name>` and `--dry-run` flags
- Dry-run: only print `[DRY-RUN] would execute: <action>`, exit 0
- Real run: perform the actual action (Docker Compose restart, scale, etc.)
- Exit code 0 = success, non-zero = failure

### `DESIGN.md` — required

Answer all 4 questions:

1. Did you choose a rule-based or LLM-based decision engine? Why? What are the trade-offs?
2. Your blast-radius config: specific values and the reason you chose them
3. What metric does your verify step check? What is the threshold? What is the timeout?
4. When does your circuit breaker reset? Manual or automatic? Why?

### `SUBMIT.md` — required

Record the results of running the 3 chaos scenarios (see section 5).

---

## 5. Acceptance: 3 chaos scenarios

After building, run these 3 tests in order. Paste the log output into `SUBMIT.md`.

### Scenario 1 — Action succeeds

```bash
# Terminal 1: run orchestrator
uv run python closed_loop.py --config config.yaml

# Terminal 2: inject latency
bash data-pack/scripts/inject_fault.sh latency payment-svc 500ms

# Expected:
# - Orchestrator detects alert "HighLatency" on payment-svc
# - Dry-run pass
# - Blast-radius OK
# - Action: restart payment-svc
# - Verify: latency returns to normal
# - Log: event_type=ACTION_SUCCESS
```

### Scenario 2 — Action fails → rollback

```bash
# Terminal 2: fully kill service + block restart
bash data-pack/scripts/inject_fault.sh kill checkout-svc

# Expected:
# - Orchestrator detects "InstanceDown"
# - Action: restart checkout-svc → fail (container stays down)
# - Verify: service still down → FAIL
# - Rollback triggered
# - Log: event_type=ROLLBACK_TRIGGERED
```

To simulate a verify fail: you can temporarily set the verify threshold very high (e.g., latency < 10ms) so verify always fails, then test the rollback logic.

### Scenario 3 — Circuit breaker

```bash
# Run inject_fault 3 times consecutively to produce 3 consecutive verify failures
# (See instructions in data/expected.json)

# Expected:
# - After the 3rd failure: orchestrator logs CIRCUIT_OPEN
# - No further actions are executed
# - Log: event_type=CIRCUIT_BREAKER_HALT
```

---

### Stress scenarios (acceptance tests #4, #5, #6)

The following three stress scenarios test orchestrator robustness under realistic production conditions: multi-step deploys, concurrent alerts, and invalid decisions from an LLM. Implement all three to complete the lab at the excellent level.

---

#### Acceptance test #4 — Multi-step transactional rollback

```bash
# Terminal 1: run orchestrator
uv run python closed_loop.py --config config.yaml

# Terminal 2: inject alert that triggers a multi-step deploy
# (config.yaml must have multi_step_map and multi_step_rollback_map for this alert)
# Force step-C to fail by stopping the container before step-C runs:
bash data-pack/scripts/inject_fault.sh kill ronki-api-gateway

# Expected observable outcomes:
# - Log TRANSACTIONAL_STEP_FAIL at step-C
# - Log TRANSACTIONAL_ROLLBACK_STEP × 2 (rollback-B first, then rollback-A)
# - Log TRANSACTIONAL_ROLLBACK_COMPLETE with rolled_back=[rollback-B, rollback-A]
# - No partial state: service returns to pre-deploy state
# - Audit trail: each rollback step has timestamp, script name, exit code in log
```

**Observable outcomes:**
- `TRANSACTIONAL_STEP_FAIL` appears with field `completed_before_failure`
- `TRANSACTIONAL_ROLLBACK_STEP` appears exactly 2 times, in order rollback-B → rollback-A
- `TRANSACTIONAL_ROLLBACK_COMPLETE` lists the correct rolled-back steps
- No `ACTION_SUCCESS` — a failed deploy must not be marked as successful

---

#### Acceptance test #5 — Concurrent alert race

```bash
# Inject fault on 2 different services simultaneously
bash data-pack/scripts/inject_fault.sh --concurrent ronki-payment-svc ronki-inventory-svc

# Expected observable outcomes:
# - Both ALERT_DETECTED events appear in the same poll cycle
# - DRY_RUN_PASS timestamps for payment-svc and inventory-svc differ by < 1s
#   (running in parallel, not blocking each other)
# - If a second alert is injected on payment-svc while its runbook is still running:
#   log SERVICE_LOCK_BUSY instead of running 2 runbooks concurrently on the same service
```

**Observable outcomes:**
- `SERVICE_LOCK_BUSY` appears when and only when the same service receives a second alert while the first is still in progress
- Two different services do NOT block each other: both log `DRY_RUN_PASS` without `SERVICE_LOCK_BUSY` between them
- Logs show 2 independent processing chains, each ending with `ACTION_SUCCESS` or `ROLLBACK_EXECUTED`

---

#### Acceptance test #6 — LLM hallucination defense

```bash
# Add a temporary mapping to runbook_map in config.yaml:
#   TestHallucination: "runbooks/nonexistent_runbook.sh"
# Ensure runbook_registry does NOT contain "runbooks/nonexistent_runbook.sh"
# Inject a synthetic alert with alertname=TestHallucination

# Expected observable outcomes:
# - Log DECISION_VALIDATION_FAILED with fields:
#     bad_runbook: "runbooks/nonexistent_runbook.sh"
#     alertname: "TestHallucination"
#     action: "escalate_no_auto_action"
# - NO DRY_RUN_PASS, ACTION_EXECUTED, or RUNBOOK_EXEC in log
# - NO subprocess spawned
# - Circuit breaker counter NOT incremented (validation failure ≠ action failure)
```

**Observable outcomes:**
- `DECISION_VALIDATION_FAILED` appears with all 4 fields: `bad_runbook`, `alertname`, `raw_decision`, `action`
- Absolutely no `RUNBOOK_EXEC` event after `DECISION_VALIDATION_FAILED`
- Circuit breaker state does not change after a validation failure

---

## 6. Rubric (6 criteria, scale 1–5)

| # | Criterion | Score 1 | Score 3 | Score 5 |
|---|---|---|---|---|
| 1 | **Detect quality** | Alertmanager poll does not work or parses the wrong format | Polls successfully, parses alert name + service | Poll + parse correct + complete structured log for every event |
| 2 | **Decide logic** | No runbook map or incorrect map | Rule-based works for ≥2 alert types | Rule-based or LLM-based with fallback, defended in DESIGN.md |
| 3 | **Act safety (5 sub-checkpoints)** | Missing ≥2 sub-checkpoints | All 5 present but ≥1 does not work correctly | All 5 work: dry-run / blast-radius / verify / rollback / circuit breaker |
| 4 | **Verify + rollback** | No verify, or verify does not use Prometheus | Verify uses Prometheus, rollback exists but does not auto-trigger | Verify + auto-rollback + rollback result is also verified |
| 5 | **Defense in DESIGN.md** | Answers < 2/4 questions | Answers all 4 questions but answers are vague | 4 questions with specific numbers (threshold, timeout, config value) and clear reasoning |
| 6 | **Concurrency + Hallucination Safety** | Does not handle concurrent alerts; no validation | Has mutex or has validation, but not both | Per-service mutex correct (2 different services do not block each other) + validation rejects runbooks outside the registry + complete audit log |

Passing level: total ≥ 12/25. Excellent level: ≥ 20/25. For criterion #6: total ≥ 15/30 to pass; ≥ 24/30 for excellent.

---

## 7. Prerequisites

Your machine requires:

- Docker Desktop (or Docker Engine + Compose plugin)
- Python ≥ 3.11 + `uv` package manager
- `curl` (to test API endpoints)
- Ports 9090 (Prometheus), 9093 (Alertmanager), 8080–8084 (services) must be free

Install Python libraries:
```bash
uv pip install requests pyyaml
```

---

## 8. Starting the Stack

```bash
# Start
bash data-pack/scripts/start_stack.sh

# Check services are up
curl http://localhost:9090/-/healthy    # Prometheus
curl http://localhost:9093/-/healthy    # Alertmanager
curl http://localhost:8080/health       # api-gateway

# Stop
bash data-pack/scripts/stop_stack.sh
```

Prometheus UI: http://localhost:9090  
Alertmanager UI: http://localhost:9093

---

## 9. Out of Scope

- You do **not** need to deploy to AWS or any real cloud.
- You do **not** need to build a custom Prometheus exporter — the mock services already export metrics.
- You do **not** need to write unit tests for every function — the 3 chaos scenarios are the primary verification method.
- You do **not** need to implement LLM if you do not have an API key — rule-based earns full marks equivalent to LLM-based when well-defended.

---
