# Lab — Closed-Loop Auto-Remediation

Read `HANDOUT.md` first. This README only covers files + quick start.

## Pack inventory

```
data-pack/
├── HANDOUT.md                          ← lab brief (read first)
├── README.md                           ← this file
├── configs/
│   ├── docker-compose.yml              5 FastAPI mocks + Prometheus + Alertmanager + Grafana + Loki + Promtail
│   ├── services/service.py             generic mock service, mounted into all 5 containers (env-driven)
│   ├── prometheus.yml                  scrape config
│   ├── alert_rules.yml                 3 alert rules (HighLatency / HighErrorRate / InstanceDown)
│   ├── alertmanager.yml                routing config
│   ├── loki/loki-config.yaml
│   ├── promtail/promtail-config.yaml
│   └── grafana/
│       ├── provisioning/datasources/   auto-load Prometheus + Loki + Alertmanager
│       ├── provisioning/dashboards/    auto-load JSON below
│       └── dashboards/closed-loop.json main dashboard
├── scripts/
│   ├── start_stack.sh                  bring up entire stack
│   ├── stop_stack.sh                   tear down + remove volumes
│   └── inject_fault.sh                 chaos commands: latency / kill / pause / concurrent
├── data/
│   ├── baseline.json                   healthy-state metric snapshot + PromQL queries for verify
│   └── expected.json                   expected log events for all 6 acceptance scenarios
└── sample-solution/                    only look here AFTER you finish your own
    ├── closed_loop.py                  orchestrator entry point
    ├── config.yaml                     runbook map, blast-radius, circuit-breaker config
    ├── engine/                         logger / safety / verify / metrics modules
    ├── runbooks/                       4 bash runbooks (all support --dry-run)
    ├── DESIGN.md                       example design defense (Vietnamese)
    └── SUBMIT.md                       example reflection (Vietnamese)
```

## Quick start

```bash
# 1) Bring up the stack
bash scripts/start_stack.sh

# 2) Verify each layer
curl -s http://localhost:8080/health            # frontend mock
curl -s http://localhost:9090/-/healthy         # Prometheus
curl -s http://localhost:9093/-/healthy         # Alertmanager
curl -s http://localhost:3000/api/health        # Grafana

# 3) Open the dashboard (anonymous viewer enabled)
#    http://localhost:3000 → dashboard "AIOps Closed-Loop"

# 4) Read data/baseline.json (metric thresholds you must verify against)

# 5) Start writing closed_loop.py per HANDOUT.md acceptance criteria
```

## Service port map

| Component | Host port | Notes |
|---|---|---|
| frontend | 8080 | FastAPI mock |
| api-gateway | 8081 | FastAPI mock |
| payment-svc | 8082 | FastAPI mock |
| inventory-svc | 8083 | FastAPI mock |
| checkout-svc | 8084 | FastAPI mock |
| Prometheus | 9090 | scrapes all 5 mocks on container port :8080 |
| Alertmanager | 9093 | |
| Grafana | 3000 | anonymous viewer |
| Loki | 3100 | log store for audit_log.jsonl |
| closed_loop.py (your orchestrator) | 9100 | exposes Prometheus metrics |

If port 8080 is taken on your host, edit `configs/docker-compose.yml` (`frontend.ports`) accordingly.

## Orchestrator dependencies

```bash
uv pip install prometheus_client requests pyyaml
```

Run from `sample-solution/` directory after starting the stack:

```bash
cd sample-solution
uv run python closed_loop.py
```

Metrics endpoint exposed on `:9100` — Prometheus auto-scrapes via `host.docker.internal:9100`. On Linux Docker, ensure `--add-host=host.docker.internal:host-gateway` works (Docker Engine ≥ 20.10).

## Stop

```bash
bash scripts/stop_stack.sh
```

Removes volumes — fresh start every run.

## Notes

- All Python invocations use `uv run python` (not bare `python`/`python3`).
- `sample-solution/` is intentionally shipped alongside the lab — look at it only after attempting your own.
- Stack runs entirely on `localhost`, no cloud account required.
