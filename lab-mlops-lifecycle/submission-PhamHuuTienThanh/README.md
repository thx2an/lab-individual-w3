# MLOps Lifecycle — Submission (PhamHuuTienThanh)

Anomaly-detection model for a payment gateway, from train → register → serve → drift-detect → retrain → blue-green swap → post-deploy auto-rollback.

## How to run end-to-end

All commands run from the `data-pack/` directory. The stack (MLflow, Postgres, Prometheus, Pushgateway, Grafana) runs in Docker; the Python scripts run on the host with `uv`. MLflow server is pinned to 2.13.2, so the client is pinned to match (newer clients call endpoints the server doesn't expose). Because some MLflow 2.13.2 wheels don't build on Python 3.14, the scripts are run on Python 3.11 via `uv run --python 3.11`.

```bash
# 0) Define the run command once (Python 3.11 + pinned deps)
RUN="uv run --python 3.11 --no-project \
  --with mlflow==2.13.2 --with evidently==0.4.40 \
  --with scikit-learn --with pandas --with numpy --with setuptools \
  --with fastapi --with uvicorn --with prometheus_client --with requests python"
# NOTE: --with setuptools is required — MLflow 2.13.2 imports pkg_resources,
# which is not bundled in a fresh Python 3.11 environment.
export MLFLOW_TRACKING_URI=http://localhost:5000

# 1) Bring the stack up (Grafana dashboard: http://localhost:3000)
bash scripts/start_stack.sh        # or: docker compose -f configs/docker-compose.yml up -d

# 2) Generate the 4 deterministic datasets (seed=42) — already shipped, optional
$RUN data/generate_data.py

# 3) Train v1 and register it under alias 'production'
$RUN tien-thanh/pipeline.py --data data/baseline.csv

# 4) Serve it (separate shell)
$RUN tien-thanh/serve.py --host 0.0.0.0 --port 8000
curl -s localhost:8000/health/active-version
curl -s -XPOST localhost:8000/predict \
  -H 'content-type: application/json' \
  -d '{"features": [[120, 0.8, 450], [320, 4.0, 900]]}'

# 5) Drift detection — combined mode surfaces BOTH data drift and the precision drop
$RUN tien-thanh/drift_detector.py \
  --reference data/baseline.csv --current data/drifted.csv \
  --check-mode combined \
  --labeled-current data/drifted.csv \
  --model-uri models:/anomaly-detector@production

# 6) Full retrain orchestrator: drift → train v2 (sliding window) → staging →
#    approval gate → promote → reload → 24-cycle post-deploy monitor + auto-rollback
$RUN tien-thanh/retrain.py \
  --reference data/baseline.csv --current data/drifted.csv \
  --holdout data/holdout.csv \
  --post-deploy-eval data/post_deploy_eval.csv \
  --serve-url http://localhost:8000
# add --auto-approve to skip the [y/N] prompt (CI / non-interactive)
```

## Files

| File | Role |
|---|---|
| `pipeline.py` | Train IsolationForest on baseline, log params/metrics/artifacts to MLflow, register `anomaly-detector` @production (P1) |
| `serve.py` | FastAPI: `/predict`, `/health/active-version`, `/reload`, `/metrics` — loads model from `@production` (P2) |
| `drift_detector.py` | Evidently `DataDriftPreset` + performance check; `--check-mode data\|performance\|combined` (P3, Stress 1) |
| `retrain.py` | Orchestrator: detect → sliding-window train v2 → staging → approval → promote → post-deploy auto-rollback (P4, Stress 2+3) |
| `metrics_util.py` | Best-effort Pushgateway helpers for the Grafana dashboard |
| `DESIGN.md` | Design defense — 7 sub-checkpoints with real numbers |
| `SUBMIT.md` | 5-question reflection |

Outputs are written to `data-pack/outputs/`: Evidently HTML reports under `drift_reports/`, and the decision trail in `audit_log.jsonl`.
