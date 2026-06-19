# Lab — MLOps Lifecycle: Anomaly Detection Model from Train to Retrain

**Individual.**

This lab is engineering work. You build a complete MLOps pipeline — train a model, register it, serve it, monitor drift, trigger retraining, and swap in the new version. There is no single correct answer. The deliverable is what an MLOps engineer hands off to the on-call team to maintain a model in production.

---

## 1. Scenario

You just joined the Platform team at a fintech company. They deployed an anomaly detection model to production 2 months ago — the model detects anomalies in latency and error rate of the payment gateway. At the time of deployment the model achieved 91% precision and 88% recall on the validation set. Last month, the on-call team reported that the model was missing many real incidents and generating more false positives than before.

The root cause has been identified: **model decay**. The production data distribution has shifted from the training distribution — traffic increased 35% after a campaign, the latency baseline rose due to adding 3rd-party integrations, and the error rate pattern changed following a new payment processor rollout. Model v1 no longer represents current reality.

The CTO requires 2 things, both mandatory:

1. **Build drift monitoring** — detect when the production data distribution diverges from the training distribution.
2. **Build a retrain pipeline** — when drift is detected, automatically or semi-automatically train a new model, register it, and swap it into production with a blue-green rollout.

Both, simultaneously. No downtime. No loss of observability. No complex manual rollback.

> 3 things the team will reject:
> - A retrain pipeline with no approval gate — "fully automatic with no control" is not MLOps, it is chaos.
> - A hardcoded drift threshold with no justification. A threshold of 0.05 or 0.30 can both be correct depending on context — you must defend your choice.
> - Versioning with only "latest" — no rollback path is not acceptable in production.

---

## 2. Lifecycle diagram

```
baseline.csv          drifted.csv
     │                     │
     ▼                     ▼
┌─────────────┐     ┌──────────────────┐
│  pipeline.py │     │ drift_detector.py │
│  Train v1   │     │ Evidently batch   │
│  IsoForest  │     │ drift score       │
└──────┬──────┘     └────────┬─────────┘
       │                     │ score > threshold?
       ▼                     ▼
┌─────────────┐     ┌──────────────────┐
│   MLflow    │◄────│   retrain.py     │
│  Registry   │     │ Orchestrator:    │
│  v1 "prod"  │     │ train → register │
└──────┬──────┘     │ → staging → swap │
       │             └──────────────────┘
       ▼
┌─────────────┐
│   serve.py  │  GET /predict
│   FastAPI   │  GET /health/active-version
│  port 8000  │
└─────────────┘
```

Complete flow:

1. `pipeline.py` — train model on `baseline.csv`, log metrics to MLflow, register artifact in MLflow Registry with alias `production`
2. `serve.py` — FastAPI loads model from registry alias `production`, exposes `/predict` and `/health/active-version`
3. `drift_detector.py` — receives a batch of production data, compares against the reference distribution (baseline), returns drift score and flag
4. `retrain.py` — orchestrator: polls drift_detector → if drift detected → trains new model on sliding window data → registers v2 with alias `staging` → waits for approval signal → promotes `staging` → `production` → serve.py reloads
5. Blue-green swap: `/health/active-version` allows version verification before full cutover

---

## 3. Stack

| Component | Role | Port |
|---|---|---|
| MLflow Tracking Server | Experiment log, artifact store, model registry | 5000 |
| PostgreSQL | MLflow backend store | 5432 |
| FastAPI (serve.py) | Model serving, blue-green endpoint | 8000 |
| Evidently (drift_detector.py) | Data drift computation (no separate container needed) | — |
| Prometheus | Metrics scraping + time-series store | 9090 |
| Pushgateway | Batch job metrics ingestion (drift_detector, retrain) | 9091 |
| Grafana | Observability dashboard | 3000 |

The stack runs via Docker Compose (`configs/docker-compose.yml`). FastAPI and Evidently run directly on the host (`uv run python`).

Start: `bash scripts/start_stack.sh`
Stop: `bash scripts/stop_stack.sh`
Generate drift data: `bash scripts/generate_drift.sh`

### Observability dashboard

Grafana: http://localhost:3000 — anonymous Viewer access, no login required.
Dashboard: **AIOps MLOps Lifecycle** (auto-provisioned when the stack starts).

Panels and their meaning:

| Panel | Description |
|---|---|
| Drift Score Timeline | Time series of drift score (0–1) with a red threshold line — when the score crosses the red line, retrain is triggered |
| Drift Status | Stat panel: "No drift" (green) / "DRIFT" (red) — current state snapshot |
| Precision & Recall per Model Version | Multi-line chart comparing precision and recall between v1 (baseline) and v2+ (retrained) |
| F1 Score per Model Version | F1 over time, makes regression after retraining easy to spot |
| Active Model Version — Alias State | Table of alias → version: production / staging / archived |
| Retrain Count | Total number of retrain events triggered |
| Auto-Rollback Count | Total number of auto-rollback events (red if > 0) |
| Production / Staging Version Number | Current version number of the production and staging aliases |
| Serve Request Rate | Throughput of the /predict endpoint (req/s) |
| Predict Latency (p99 / p50) | 99th and 50th percentile latency of /predict |
| Serve Active Version | Model version currently loaded by serve.py |
| Lifecycle Event Rate | Bar chart of retrain_triggered and auto_rollback frequency over 5-minute windows |

The dashboard is a debugging and observation tool — not an acceptance criterion. The pipeline must satisfy all 6 acceptance criteria first; the dashboard helps you understand what is happening internally.

**Additional prerequisite:** `prometheus_client` must be installed in the Python environment running `serve.py`, `drift_detector.py`, and `retrain.py`:
```
uv pip install prometheus_client
```
If the pushgateway is not running when you call `drift_detector.py` or `retrain.py`, the metrics calls will print a warning and be skipped — the pipeline will not crash.

---

## 4. Data

### `data/baseline.csv` — 30 days of normal operation
- 4320 rows (one row every 10 minutes)
- Columns: `timestamp`, `latency_p99` (ms), `error_rate` (%), `rps` (requests/sec)
- Distribution: latency ~ N(120, 15), error_rate ~ N(0.8, 0.3), rps ~ N(450, 80)

### `data/drifted.csv` — 7 days after campaign + integration changes
- 1008 rows (one row every 10 minutes)
- Same schema but shifted distribution: latency mean +30% (~156ms), error_rate doubled (~1.6%), rps up 40% (~630)
- This is the data you feed into drift_detector to trigger retraining

Regenerate data: `uv run python data/generate_data.py`

---

## 5. Required deliverables

### P1. `pipeline.py`

Train IsolationForest on `baseline.csv`, log the experiment to MLflow:

- Log parameters: `contamination`, `n_estimators`, `random_state`
- Log metrics: `train_anomaly_rate`, `feature_count`
- Log artifact: model serialized with `mlflow.sklearn.log_model`
- Register model in MLflow Registry under the name `anomaly-detector`, set alias `production`

### P2. `serve.py`

FastAPI application:

- Startup: load model from MLflow Registry alias `models:/anomaly-detector@production`
- `POST /predict` — accepts JSON `{features: [...]}`, returns `{prediction: int, score: float, version: str}`
- `GET /health/active-version` — returns the version currently being served
- `POST /reload` — reloads model from registry (used after a swap)

### P3. `drift_detector.py`

Wrapper for Evidently DataDriftPreset:

- `detect_drift(reference_df, current_df, threshold)` → `DriftResult(score, is_drift, report_path)`
- Save Evidently HTML report to `outputs/drift_reports/`
- Log drift score to MLflow as a metric (to visualize the trend over time)

### P4. `retrain.py`

Orchestrator script:

- Load `drifted.csv` using a rolling window (default: most recent 7 days)
- Call `drift_detector.py` to compare against baseline
- If drift detected: train a new model via `pipeline.py` on the new data window, register v2 with alias `staging`
- Print approval prompt: "Drift detected. Model v2 registered as staging. Promote to production? [y/N]"
- If approved: promote `staging` → `production`, call `POST /reload` on serve.py
- Log the full decision trail to the MLflow run (parameters + metrics + tags)

### P5. `DESIGN.md`

You must address the following **4 sub-checkpoints**, each with at least 3-4 sentences and specific numbers:

1. **Drift threshold** — What value did you choose (e.g., 0.15)? Why? Did you test it against drifted.csv? What happens if the threshold is too low?
2. **Drift type** — Is this data drift, concept drift, or performance drift? Which type does your `drift_detector.py` detect? Why is this type appropriate for the payment anomaly problem?
3. **Retrain trigger configuration** — Manual or automatic? If manual: who approves? What is the approval timeout? If you use a cadence (e.g., weekly retrain regardless of drift), defend your reasoning.
4. **Versioning + rollback** — Do you use aliases or version numbers? What does rollback look like when v2 underperforms? Who has the authority to trigger a rollback?

### P6. `SUBMIT.md`

Short reflection — 5 questions, at least 3-4 sentences each, referencing your code and numbers:

1. What drift threshold did you choose and why? Did you validate that threshold against real data?
2. What happens if model v2 after retraining performs worse than v1 in production? How does your pipeline handle this case?
3. What is the difference between data drift and concept drift? Which type does Evidently detect in this lab?
4. Why is a blue-green swap more important than simply replacing the model file directly?
5. If you had to automate the approval gate (no human required), what metric and threshold would you use?

---

## 6. Stress scenarios — acceptance phases 4-6

The following three scenarios test pipeline resilience under more complex real-world conditions. Each requires a small change and has specific test criteria.

### Stress 1 — Drift type misclassification trap

**Context:** `drifted.csv` contains both data drift (feature distribution shift) and concept drift (25% of labels are flipped — same input features but the relationship with `anomaly_label` has changed). A single `DataDriftPreset` will detect data drift but **completely miss** concept drift because the feature values appear normal.

**Acceptance criterion 4:** Run `drift_detector.py --check-mode combined --labeled-current data/drifted.csv --model-uri models:/anomaly-detector@production`. Output must print both `Drift score` (data) and `Perf precision` (performance). Running with `--check-mode data` only will not surface the precision drop — this is evidence that the two mechanisms detect different drift types. `DESIGN.md` must explain why combined mode is necessary with at least 1 concrete numerical example.

### Stress 2 — Retrain data selection

**Context:** If `retrain.py` trains v2 only on the drift window (7 days), v2 overfits to the new distribution and performs worse than v1 on `data/holdout.csv` (500 rows from the old pattern). A sliding window strategy (baseline + drift window) preserves performance across both regimes.

**Acceptance criterion 5:** Run `retrain.py --reference data/baseline.csv --current data/drifted.csv --holdout data/holdout.csv`. Output must print the line `Holdout validation — v2 precision: X.XXXX  recall: X.XXXX`. The precision value must be ≥ v1 precision measured on the same holdout. `DESIGN.md` must compare the sliding window approach against at least 1 alternative strategy.

### Stress 3 — Auto-rollback on post-deploy degradation

**Context:** After v2 is promoted to `@production`, the pipeline continues monitoring v2 on `data/post_deploy_eval.csv` (200 rows, clearly labeled). If v2 precision falls below 0.65 within 24 polling cycles, v2 is demoted to `@archived` and v1 is automatically restored to `@production`.

**Acceptance criterion 6:** Run the pipeline end-to-end with `--post-deploy-eval data/post_deploy_eval.csv`. After promotion, the terminal must print lines `post_deploy_monitor Cycle XX/24`. If rollback occurs, the final line must be `Rollback complete. v1 restored to @production. v2 → @archived`. File `outputs/audit_log.jsonl` must contain event `auto_rollback_v2_to_v1` with fields `demoted_version`, `restored_version`, `trigger_precision`, `cycle`.

---

## 7. Rubric (30 points)

| Criterion | Points | Description |
|---|---|---|
| Train + Register (pipeline.py) | 5 | Model trains successfully, MLflow logs full params/metrics/artifact, registers with alias production |
| Serve quality (serve.py) | 5 | /predict works, loads the correct version, /health/active-version returns version string, /reload reloads successfully |
| Drift detection (drift_detector.py) | 5 | Evidently DataDriftPreset runs, score is computed, flag raised when threshold exceeded, HTML report saved |
| Retrain pipeline (retrain.py) | 5 | Orchestrator runs end-to-end: detect → train v2 → register staging → prompt approval → promote → reload |
| Defense in DESIGN.md | 5 | All 4 original sub-checkpoints answered with specific numbers, reasoning consistent with code |
| Lifecycle Robustness | 5 | **1**: pipeline runs but handles no stress cases; **2**: handles 1/3 stress cases with acceptance criterion met; **3**: handles 2/3 stress cases; **4**: handles all 3, DESIGN.md addresses all 3 new sub-checkpoints (4/5/6); **5**: all 3 stress cases pass, DESIGN.md includes real numbers from run, audit log valid |

Tier: ≥27 excellent, ≥22 passing, ≥15 needs revision.

---

## 8. Submission

A single directory containing:

```
your-name/
├── pipeline.py
├── serve.py
├── drift_detector.py
├── retrain.py
├── DESIGN.md
├── SUBMIT.md
└── README.md    (1 paragraph: how to run your pipeline from start to finish)
```

---

## 9. Out of scope

- You **do not need** to deploy to the cloud. The entire lab runs locally with Docker Compose + localhost.
- You **do not need** to write a full test suite — validate by running the pipeline end-to-end and observing the output.
- You **do not need** a GPU or a large model. IsolationForest on 4320 rows runs in < 1 second.
- You **do not need** to implement authentication for the FastAPI endpoint.

---

## 10. Reference

### Concepts

- **MLflow Tracking**: `mlflow.start_run()`, `mlflow.log_param()`, `mlflow.log_metric()`, `mlflow.sklearn.log_model()` — MLflow docs: [mlflow.org/docs](https://mlflow.org/docs/latest/)
- **MLflow Registry**: Model aliases (`production`, `staging`), `MlflowClient.set_registered_model_alias()`, `mlflow.pyfunc.load_model("models:/name@alias")` — see MLflow Model Registry guide
- **Evidently DataDriftPreset**: `from evidently.report import Report`, `from evidently.metric_preset import DataDriftPreset` — [docs.evidentlyai.com](https://docs.evidentlyai.com)
- **FastAPI lifespan**: `@asynccontextmanager` pattern to load the model once at startup — [fastapi.tiangolo.com/advanced/events](https://fastapi.tiangolo.com/advanced/events/)

### Drift theory

- **Data drift** — input feature distribution changes: P(X) changes, P(Y|X) unchanged. Detected via statistical tests on feature values.
- **Concept drift** — input-output relationship changes: P(Y|X) changes. Detected by comparing model performance over time.
- **Performance drift** — proxy for concept drift when ground truth is unavailable: anomaly rate, prediction confidence distribution.
- Jensen-Shannon divergence and Wasserstein distance: 2 common metrics Evidently uses to measure distribution distance.

### Design hints

- **Threshold is not a magic number.** Run drift_detector on baseline data (70/30 split) first to get a baseline drift score, and use that as the upper bound for "no drift". Threshold = baseline score × 1.5 is a reasonable heuristic.
- **The approval gate does not need to be complex.** A `[y/N]` input in the terminal is sufficient for this lab. What matters is that the gate *exists* in the code — not automatic unconditional promotion.
- **MLflow aliases are better than version numbers** for production routing because you can swap an alias without changing code in serve.py. `models:/anomaly-detector@production` always points to the correct version.
- **Blue-green = 2 endpoints, not 2 servers.** In this lab, `/predict` serves the production model, `/predict-shadow` (optional) serves the staging model. Swap = change alias in registry + reload.

---
