"""
retrain.py — Orchestrator: detect drift -> train v2 -> staging -> approval -> promote -> monitor.

Deliverable P4 + Stress 2 (sliding window) + Stress 3 (auto-rollback):
  1. Load reference (baseline) + current (production window)
  2. Run drift detection; if no drift, exit early
  3. Train v2 on a SLIDING WINDOW (baseline + drift window) — avoids overfitting the
     new distribution and keeps performance on old-pattern data (validated on --holdout)
  4. Register v2 with alias 'staging'
  5. Human approval gate ([y/N]) — promotion is never unconditional
  6. On approval: swap alias 'production' -> v2, POST /reload to serve.py
  7. Post-deploy monitor on --post-deploy-eval; auto-rollback to v1 if precision drops
  All decisions written to MLflow + outputs/audit_log.jsonl

Usage:
    export MLFLOW_TRACKING_URI=http://localhost:5000
    uv run python tien-thanh/retrain.py \
        --reference data/baseline.csv --current data/drifted.csv \
        --holdout data/holdout.csv \
        --post-deploy-eval data/post_deploy_eval.csv \
        --serve-url http://localhost:8000
    # CI / non-interactive: add --auto-approve
"""

import argparse
import json
import os
import pickle
import sys
import tempfile
from datetime import datetime

import mlflow
import mlflow.sklearn
import pandas as pd
import requests
from mlflow import MlflowClient
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drift_detector import check_performance_drift, detect_drift, log_to_mlflow  # noqa: E402

MODEL_NAME = "anomaly-detector"
EXPERIMENT_NAME = "anomaly-detection"
FEATURES = ["latency_p99", "error_rate", "rps"]
AUDIT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outputs", "audit_log.jsonl")
POST_DEPLOY_CYCLES = 24            # simulate 24h of post-deploy monitoring
POST_DEPLOY_PREC_THRESHOLD = 0.65  # auto-rollback if v2 precision drops below this


def append_audit(event: str, detail: dict) -> None:
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    entry = {"timestamp": datetime.utcnow().isoformat(), "event": event, **detail}
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def train_model_on_df(df: pd.DataFrame, contamination: float = 0.03, n_estimators: int = 100):
    """Train IsolationForest on a DataFrame -> (model, scaler, anomaly_rate, n_rows)."""
    X = df[FEATURES].dropna()
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model = IsolationForest(
        contamination=contamination, n_estimators=n_estimators, random_state=42, n_jobs=-1
    )
    model.fit(X_scaled)
    anomaly_rate = float((model.predict(X_scaled) == -1).mean())
    return model, scaler, anomaly_rate, len(X)


def score_precision_recall(model, scaler, labeled_df: pd.DataFrame) -> tuple[float, float]:
    """Score an in-memory (not-yet-registered) model on labeled data."""
    X = labeled_df[FEATURES].dropna()
    y_true = labeled_df.loc[X.index, "anomaly_label"].values
    y_pred = (model.predict(scaler.transform(X)) == -1).astype(int)
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall


def register_new_version(model, scaler, anomaly_rate, training_rows, drift_score,
                         current_data_path, tracking_uri) -> str:
    """Log v2 to MLflow, register it, set alias 'staging'. Returns version string."""
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT_NAME)
    X_sample = pd.read_csv(current_data_path)[FEATURES].head(3)

    with mlflow.start_run(run_name="retrain-triggered"):
        mlflow.log_param("trigger", "drift_detected")
        mlflow.log_param("drift_score", drift_score)
        mlflow.log_param("training_rows", training_rows)
        mlflow.log_param("features", ",".join(FEATURES))
        mlflow.log_metric("train_anomaly_rate", anomaly_rate)

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            pickle.dump(scaler, f)
            scaler_path = f.name
        mlflow.log_artifact(scaler_path, artifact_path="scaler")
        os.unlink(scaler_path)

        mlflow.sklearn.log_model(
            sk_model=model, artifact_path="model",
            registered_model_name=MODEL_NAME, input_example=X_sample,
        )

    client = MlflowClient(tracking_uri=tracking_uri)
    latest = max(client.search_model_versions(f"name='{MODEL_NAME}'"), key=lambda v: int(v.version))
    client.set_registered_model_alias(MODEL_NAME, "staging", latest.version)
    print(f"[retrain] Registered {MODEL_NAME} v{latest.version} -> alias 'staging'")
    return latest.version


def promote_to_production(version: str, tracking_uri: str) -> None:
    MlflowClient(tracking_uri=tracking_uri).set_registered_model_alias(MODEL_NAME, "production", version)
    print(f"[retrain] Promoted v{version} -> alias 'production'")


def reload_serve(serve_url: str) -> None:
    try:
        resp = requests.post(f"{serve_url}/reload", timeout=10)
        resp.raise_for_status()
        print(f"[retrain] serve.py reloaded -> now serving v{resp.json().get('version', '?')}")
    except requests.exceptions.ConnectionError:
        print(f"[retrain] WARNING: could not reach serve.py at {serve_url}. Reload skipped.")
    except Exception as exc:  # noqa: BLE001
        print(f"[retrain] WARNING: reload call failed: {exc}")


def post_deploy_monitor(v2_version, v1_version, post_deploy_eval_path, tracking_uri, serve_url,
                        cycles=POST_DEPLOY_CYCLES, prec_threshold=POST_DEPLOY_PREC_THRESHOLD) -> None:
    """Monitor v2 precision for N cycles; auto-rollback to v1 if it drops below threshold."""
    import mlflow.pyfunc

    eval_df = pd.read_csv(post_deploy_eval_path)
    if "anomaly_label" not in eval_df.columns:
        print("[post_deploy_monitor] WARNING: no anomaly_label — skipping.")
        return

    client = MlflowClient(tracking_uri=tracking_uri)
    model_uri = f"models:/{MODEL_NAME}@production"
    X = eval_df[FEATURES].dropna()
    y_true = eval_df.loc[X.index, "anomaly_label"].values

    print(f"[post_deploy_monitor] Starting {cycles}-cycle post-deploy evaluation of v{v2_version}...")
    model = mlflow.pyfunc.load_model(model_uri)  # v2 was promoted before monitoring starts
    for cycle in range(1, cycles + 1):
        raw = model.predict(pd.DataFrame(X, columns=FEATURES))
        if hasattr(raw, "values"):
            raw = raw.values
        y_pred = (raw == -1).astype(int) if set(pd.unique(raw)).issubset({-1, 1}) else raw.astype(int)
        tp = int(((y_pred == 1) & (y_true == 1)).sum())
        fp = int(((y_pred == 1) & (y_true == 0)).sum())
        fn = int(((y_pred == 0) & (y_true == 1)).sum())
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

        print(f"[post_deploy_monitor] Cycle {cycle:02d}/{cycles} — precision: {precision:.4f}  recall: {recall:.4f}")
        append_audit("post_deploy_cycle", {"cycle": cycle, "precision": precision, "recall": recall, "v2": v2_version})

        if precision < prec_threshold:
            print(f"[post_deploy_monitor] Precision {precision:.4f} < threshold {prec_threshold} — triggering AUTO-ROLLBACK.")
            client.set_registered_model_alias(MODEL_NAME, "archived", v2_version)
            client.set_registered_model_alias(MODEL_NAME, "production", v1_version)
            append_audit("auto_rollback_v2_to_v1", {
                "demoted_version": v2_version, "restored_version": v1_version,
                "trigger_precision": precision, "threshold": prec_threshold, "cycle": cycle,
            })
            reload_serve(serve_url)
            print(f"Rollback complete. v1 restored to @production. v2 → @archived")
            try:
                from metrics_util import push_active_version, push_event
                push_event("auto_rollback_v2_to_v1", v2_version)
                push_active_version(v1_version, "production")
                push_active_version(v2_version, "archived")
            except ImportError:
                pass
            return

    print(f"[post_deploy_monitor] v{v2_version} passed all {cycles} cycles. Stable in production.")
    append_audit("post_deploy_stable", {"version": v2_version, "cycles": cycles})


def main():
    parser = argparse.ArgumentParser(description="Drift-triggered retrain orchestrator")
    parser.add_argument("--reference", required=True, help="Baseline CSV (training reference)")
    parser.add_argument("--current", required=True, help="Current production window CSV")
    parser.add_argument("--threshold", type=float, default=0.15)
    parser.add_argument("--serve-url", default="http://localhost:8000")
    parser.add_argument("--auto-approve", action="store_true", default=False)
    parser.add_argument("--contamination", type=float, default=0.03)
    parser.add_argument("--n-estimators", type=int, default=100)
    parser.add_argument("--holdout", default=None, help="Old-pattern labeled CSV to validate v2")
    parser.add_argument("--post-deploy-eval", default=None, help="Labeled CSV for post-deploy auto-rollback")
    args = parser.parse_args()

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")

    # Step 1 — load data
    ref_df = pd.read_csv(args.reference)
    cur_df = pd.read_csv(args.current)
    print(f"[retrain] Reference rows : {len(ref_df)}")
    print(f"[retrain] Current rows   : {len(cur_df)}")

    # Step 2 — drift detection
    print(f"[retrain] Running drift detection (threshold={args.threshold})...")
    drift_result = detect_drift(ref_df, cur_df, threshold=args.threshold, report_label="retrain")
    log_to_mlflow(drift_result)
    print(f"[retrain] Drift score    : {drift_result.score:.4f}")
    print(f"[retrain] Drift detected : {drift_result.is_drift}")
    if not drift_result.is_drift:
        print("[retrain] No drift detected — retrain not triggered. Exiting.")
        return

    # Step 3 — Stress 2: train v2 on a sliding window (baseline + drift window).
    # Training on the drift window ONLY overfits the new distribution and regresses on
    # old-pattern data still present in production; concatenating both regimes generalises.
    print("[retrain] Drift confirmed. Building sliding-window training set (baseline + drift)...")
    combined_df = pd.concat([ref_df.copy(), cur_df.copy()], ignore_index=True)
    print(f"[retrain] Sliding window rows : {len(combined_df)} (baseline {len(ref_df)} + drift {len(cur_df)})")

    model, scaler, anomaly_rate, n_rows = train_model_on_df(
        combined_df, contamination=args.contamination, n_estimators=args.n_estimators
    )
    print(f"[retrain] New model anomaly rate: {anomaly_rate:.4f} on {n_rows} rows")

    # Validate v2 on the old-pattern holdout — must not regress vs v1.
    if args.holdout:
        holdout_df = pd.read_csv(args.holdout)
        if "anomaly_label" in holdout_df.columns:
            prec_v2, rec_v2 = score_precision_recall(model, scaler, holdout_df)
            v1_prec, v1_rec, _ = check_performance_drift(
                holdout_df, f"models:/{MODEL_NAME}@production", perf_threshold=0.0
            )
            print(f"[retrain] Holdout validation — v2 precision: {prec_v2:.4f}  recall: {rec_v2:.4f}")
            print(f"[retrain] Holdout baseline   — v1 precision: {v1_prec:.4f}  recall: {v1_rec:.4f}")
            append_audit("holdout_validation", {
                "v2_precision": prec_v2, "v2_recall": rec_v2,
                "v1_precision": v1_prec, "v1_recall": v1_rec,
            })

    # Step 4 — register staging
    new_version = register_new_version(
        model, scaler, anomaly_rate, n_rows, drift_result.score, args.current, tracking_uri
    )

    # Step 5 — approval gate
    if args.auto_approve:
        print("[retrain] Auto-approve mode — skipping human gate.")
        approved = True
    else:
        print("\n" + "=" * 60)
        print(f"  Drift score   : {drift_result.score:.4f}  (threshold {args.threshold})")
        print(f"  Drifted cols  : {drift_result.drifted_features}")
        print(f"  New version   : {MODEL_NAME} v{new_version} (alias: staging)")
        print(f"  Anomaly rate  : {anomaly_rate:.4f}")
        print("=" * 60)
        approved = input("  Promote staging → production? [y/N] ").strip().lower() == "y"

    if not approved:
        print(f"[retrain] Promotion declined. v{new_version} remains in staging.")
        return

    # Step 6 — remember v1, promote v2, reload
    client = MlflowClient(tracking_uri=tracking_uri)
    try:
        v1_version = client.get_model_version_by_alias(MODEL_NAME, "production").version
    except Exception:  # noqa: BLE001
        v1_version = "1"
    append_audit("promote_v2", {"v2_version": new_version, "v1_version": v1_version})

    promote_to_production(new_version, tracking_uri)
    reload_serve(args.serve_url)
    print(f"[retrain] Pipeline complete. {MODEL_NAME} v{new_version} is now in production.")

    try:
        from metrics_util import push_active_version, push_event
        push_event("retrain_triggered", new_version)
        push_active_version(new_version, "production")
        push_active_version(v1_version, "archived")
    except ImportError:
        pass

    # Step 7 — post-deploy monitor + auto-rollback
    if args.post_deploy_eval:
        post_deploy_monitor(
            v2_version=new_version, v1_version=v1_version,
            post_deploy_eval_path=args.post_deploy_eval,
            tracking_uri=tracking_uri, serve_url=args.serve_url,
        )


if __name__ == "__main__":
    main()
