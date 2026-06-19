"""
drift_detector.py — Evidently DataDriftPreset wrapper + performance (concept) drift check.

Deliverable P3 + Stress 1 (combined mode):
  detect_drift(reference, current, threshold) -> DriftResult(score, is_drift, report_path)
  check_performance_drift(labeled_df, model_uri) -> (precision, recall, is_degraded)

  --check-mode data        : data drift only  (P(X) shift, Evidently)
  --check-mode performance : precision/recall on labeled data (P(Y|X) shift proxy)
  --check-mode combined    : both (default) — data drift OR performance degradation

Exit code is 1 when any drift is flagged, 0 otherwise (handy for shell scripting).

Usage:
    uv run python tien-thanh/drift_detector.py \
        --reference data/baseline.csv --current data/drifted.csv \
        --check-mode combined \
        --labeled-current data/drifted.csv \
        --model-uri models:/anomaly-detector@production
"""

import argparse
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import mlflow
import pandas as pd
from evidently.metric_preset import DataDriftPreset
from evidently.report import Report

FEATURES = ["latency_p99", "error_rate", "rps"]
DEFAULT_THRESHOLD = 0.15        # share of drifted columns above which we flag data drift
DEFAULT_PERF_THRESHOLD = 0.70   # minimum acceptable precision on labeled data
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outputs", "drift_reports")


@dataclass
class DriftResult:
    score: float                 # fraction of features drifted (0.0–1.0)
    is_drift: bool
    threshold: float
    drifted_features: list[str]
    report_path: str
    timestamp: str
    perf_precision: Optional[float] = None
    perf_recall: Optional[float] = None
    perf_is_degraded: bool = False
    perf_threshold: float = DEFAULT_PERF_THRESHOLD


def detect_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    threshold: float = DEFAULT_THRESHOLD,
    report_label: str = "",
) -> DriftResult:
    """Run Evidently DataDriftPreset and save the HTML report. Data-drift only."""
    ref = reference_df[FEATURES].copy()
    cur = current_df[FEATURES].copy()

    report = Report(metrics=[DataDriftPreset()])
    report.run(reference_data=ref, current_data=cur)

    # DataDriftPreset emits two metrics: [0] DatasetDriftMetric (dataset-level summary),
    # [1] DataDriftTable (per-column detail with drift_by_columns).
    metrics = report.as_dict()["metrics"]
    summary = metrics[0]["result"]
    share_drifted = float(summary.get("share_of_drifted_columns", 0.0))
    per_feature = {}
    for m in metrics:
        if "drift_by_columns" in m.get("result", {}):
            per_feature = m["result"]["drift_by_columns"]
            break
    drifted_features = [f for f, info in per_feature.items() if info.get("drift_detected", False)]

    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    label = f"-{report_label}" if report_label else ""
    report_path = os.path.join(REPORT_DIR, f"drift-report{label}-{ts}.html")
    report.save_html(report_path)

    return DriftResult(
        score=share_drifted,
        is_drift=share_drifted > threshold,
        threshold=threshold,
        drifted_features=drifted_features,
        report_path=report_path,
        timestamp=ts,
    )


def check_performance_drift(
    labeled_df: pd.DataFrame,
    model_uri: str,
    perf_threshold: float = DEFAULT_PERF_THRESHOLD,
) -> tuple[float, float, bool]:
    """Evaluate model precision/recall on labeled data — proxy for concept drift.

    labeled_df needs an `anomaly_label` column (0=normal, 1=anomaly).
    Returns (precision, recall, is_degraded). is_degraded = precision < perf_threshold.
    """
    import mlflow.pyfunc

    if "anomaly_label" not in labeled_df.columns:
        raise ValueError("labeled_df must contain 'anomaly_label' column (0=normal, 1=anomaly)")

    model = mlflow.pyfunc.load_model(model_uri)
    X = labeled_df[FEATURES].dropna()
    y_true = labeled_df.loc[X.index, "anomaly_label"].values

    raw = model.predict(pd.DataFrame(X, columns=FEATURES))
    if hasattr(raw, "values"):
        raw = raw.values
    # IsolationForest emits -1/1; remap to anomaly=1/normal=0.
    if set(pd.unique(raw)).issubset({-1, 1}):
        y_pred = (raw == -1).astype(int)
    else:
        y_pred = raw.astype(int)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall, precision < perf_threshold


def log_to_mlflow(result: DriftResult, experiment_name: str = "anomaly-detection-drift") -> None:
    """Log the drift score (and perf metrics) to MLflow to chart the trend over time."""
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)

    with mlflow.start_run(run_name=f"drift-check-{result.timestamp}"):
        mlflow.log_metric("drift_score", result.score)
        mlflow.log_metric("is_drift", float(result.is_drift))
        mlflow.log_param("threshold", result.threshold)
        mlflow.log_param("drifted_features", ",".join(result.drifted_features) or "none")
        if result.report_path and os.path.exists(result.report_path):
            mlflow.log_artifact(result.report_path, artifact_path="drift_reports")
        if result.perf_precision is not None:
            mlflow.log_metric("perf_precision", result.perf_precision)
            mlflow.log_metric("perf_recall", result.perf_recall)
            mlflow.log_metric("perf_is_degraded", float(result.perf_is_degraded))


def main():
    parser = argparse.ArgumentParser(description="Detect data/concept drift between two CSVs")
    parser.add_argument("--reference", required=True, help="Reference (baseline) CSV")
    parser.add_argument("--current", required=True, help="Current (production window) CSV")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--check-mode", choices=["data", "performance", "combined"], default="combined")
    parser.add_argument("--labeled-current", default=None,
                        help="CSV with anomaly_label — required for performance/combined mode")
    parser.add_argument("--model-uri", default="models:/anomaly-detector@production")
    parser.add_argument("--perf-threshold", type=float, default=DEFAULT_PERF_THRESHOLD)
    parser.add_argument("--log-mlflow", action="store_true", default=False)
    args = parser.parse_args()

    ref_df = pd.read_csv(args.reference)
    cur_df = pd.read_csv(args.current)

    # --- Data drift ---
    if args.check_mode in ("data", "combined"):
        result = detect_drift(ref_df, cur_df, threshold=args.threshold)
        print(f"[drift_detector] check_mode      : {args.check_mode}")
        print(f"[drift_detector] Drift score     : {result.score:.4f}")
        print(f"[drift_detector] Threshold       : {result.threshold}")
        print(f"[drift_detector] Drift detected  : {result.is_drift}")
        print(f"[drift_detector] Drifted features: {result.drifted_features}")
        print(f"[drift_detector] Report saved    : {result.report_path}")
    else:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        result = DriftResult(0.0, False, args.threshold, [], "", ts)

    # --- Performance (concept) drift ---
    if args.check_mode in ("performance", "combined"):
        if not args.labeled_current:
            parser.error("--labeled-current is required for performance/combined mode")
        labeled_df = pd.read_csv(args.labeled_current)
        precision, recall, is_degraded = check_performance_drift(
            labeled_df, args.model_uri, perf_threshold=args.perf_threshold
        )
        result.perf_precision = precision
        result.perf_recall = recall
        result.perf_is_degraded = is_degraded
        result.perf_threshold = args.perf_threshold
        print(f"[drift_detector] Perf precision  : {precision:.4f}  (threshold {args.perf_threshold})")
        print(f"[drift_detector] Perf recall     : {recall:.4f}")
        print(f"[drift_detector] Perf degraded   : {is_degraded}")

    any_drift = result.is_drift or result.perf_is_degraded

    if args.log_mlflow:
        log_to_mlflow(result)
        print("[drift_detector] Logged to MLflow.")

    # Best-effort metrics push (no-op if pushgateway not running).
    try:
        from metrics_util import push_drift_score, push_model_eval
        push_drift_score(result.score, result.threshold)
        if result.perf_precision is not None:
            denom = result.perf_precision + result.perf_recall
            f1 = 2 * result.perf_precision * result.perf_recall / denom if denom > 0 else 0.0
            push_model_eval("current", result.perf_precision, result.perf_recall, f1)
    except ImportError:
        pass

    raise SystemExit(1 if any_drift else 0)


if __name__ == "__main__":
    main()
