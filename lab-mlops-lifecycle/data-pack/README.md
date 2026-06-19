# Lab — MLOps Lifecycle

Read `HANDOUT.md` first. This README only covers files + quick start.

## Pack inventory

```
data-pack/
├── HANDOUT.md                          ← lab brief (read first)
├── README.md                           ← this file
├── configs/
│   ├── docker-compose.yml              MLflow + PostgreSQL + Prometheus + Pushgateway + Grafana
│   ├── mlflow-config.txt               reference doc: MLflow env vars + port info (NOT loaded by compose)
│   ├── prometheus.yml                  scrape config: pushgateway + serve.py /metrics
│   └── grafana/
│       ├── provisioning/datasources/   auto-load Prometheus
│       ├── provisioning/dashboards/    auto-load JSON below
│       └── dashboards/mlops-lifecycle.json   main dashboard
├── scripts/
│   ├── start_stack.sh                  bring up MLflow + Postgres + observability
│   ├── stop_stack.sh                   tear down stack
│   └── generate_drift.sh               wrapper around data/generate_data.py
├── data/
│   ├── generate_data.py                deterministic generator (seed=42), emits all 4 CSVs
│   ├── baseline.csv                    30 days normal (4320 rows)
│   ├── drifted.csv                     7 days with data drift + concept drift (1008 rows, 25% labels flipped)
│   ├── holdout.csv                     500 rows of old-pattern data for v2 sanity check
│   └── post_deploy_eval.csv            200 rows ground truth for post-deploy monitoring
└── sample-solution/                    only look here AFTER you finish your own
    ├── pipeline.py                     train IsolationForest + MLflow register @production
    ├── serve.py                        FastAPI /predict + /health/active-version + /metrics
    ├── drift_detector.py               Evidently DataDriftPreset + performance drift (--check-mode data|performance|combined)
    ├── retrain.py                      orchestrator: drift→train v2→staging→approval→promote→post-deploy monitor 24 cycles→auto-rollback
    ├── metrics_util.py                 Pushgateway helpers for dashboard
    ├── DESIGN.md                       example design defense (Vietnamese)
    └── SUBMIT.md                       example reflection (Vietnamese)
```

## Quick start

```bash
# 1) Bring up the stack
bash scripts/start_stack.sh
# Wait ~30s on first run (Postgres init + MLflow pip-installs psycopg2-binary inside container)

# 2) Verify
curl -s http://localhost:5000/health             # MLflow
curl -s http://localhost:9090/-/healthy          # Prometheus
curl -s http://localhost:9091/-/healthy          # Pushgateway
curl -s http://localhost:3000/api/health         # Grafana

# 3) Open the dashboard (anonymous viewer enabled)
#    http://localhost:3000 → dashboard "AIOps MLOps Lifecycle"

# 4) Generate datasets (deterministic, seed=42)
uv run python data/generate_data.py

# 5) Train v1 + register
export MLFLOW_TRACKING_URI=http://localhost:5000
uv run python sample-solution/pipeline.py --data data/baseline.csv

# 6) Serve
uv run python sample-solution/serve.py
# In another shell:
curl -s http://localhost:8000/health/active-version

# 7) Run drift detection
uv run python sample-solution/drift_detector.py \
  --reference data/baseline.csv \
  --current data/drifted.csv \
  --check-mode combined \
  --model-uri models:/anomaly-detector@production \
  --labeled-current data/drifted.csv
```

## Service port map

| Component | Host port | Notes |
|---|---|---|
| MLflow | 5000 | tracking + registry + serve-artifacts proxy |
| PostgreSQL | 5432 | MLflow backend store |
| Prometheus | 9090 | scrapes pushgateway + serve.py |
| Pushgateway | 9091 | receives metrics from drift_detector + retrain |
| Grafana | 3000 | anonymous viewer |
| serve.py (your model server) | 8000 | FastAPI /predict + /metrics |

## Python dependencies

Pin MLflow client to match the server (2.13.2) — newer client versions hit endpoints the server does not expose:

```bash
uv pip install 'mlflow==2.13.2' 'evidently==0.4.40' scikit-learn pandas numpy fastapi uvicorn prometheus_client requests
```

If your environment requires Python 3.11 (some MLflow 2.13.2 wheels do not build on 3.14), use:

```bash
uv run --python 3.11 --no-project --with 'mlflow==2.13.2' --with 'evidently==0.4.40' --with scikit-learn --with pandas --with numpy python <script>
```

## Stop

```bash
bash scripts/stop_stack.sh
```

Volumes (`postgres_data`, `mlflow_artifacts`) are preserved by default — drop with `docker compose -f configs/docker-compose.yml down -v` for a clean reset.

## Notes

- All Python invocations use `uv run python` (not bare `python`/`python3`).
- `sample-solution/` shipped alongside — look at it only after attempting your own.
- Stack runs entirely on `localhost`, no cloud account required.
- `mlflow-config.txt` is reference material only; the compose file has env vars inline.
