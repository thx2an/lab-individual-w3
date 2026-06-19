"""
drift_detector.py — Evidently DataDriftPreset wrapper cho anomaly detection pipeline.

Computes per-feature drift score giữa reference (baseline) và current (production window).
Flags drift khi dataset-level drift score vượt threshold.
Lưu HTML report vào outputs/drift_reports/.
Log drift score vào MLflow (optional, requires MLFLOW_TRACKING_URI).

Usage:
    uv run python drift_detector.py \
        --reference data/baseline.csv \
        --current   data/drifted.csv \
        --threshold 0.15

    # Performance check (concept drift detection):
    uv run python drift_detector.py \
        --reference data/baseline.csv \
        --current   data/drifted.csv \
        --check-mode performance \
        --labeled-current data/drifted.csv \
        --model-uri models:/anomaly-detector@production

    # Combined (default): runs both data + performance checks
    uv run python drift_detector.py \
        --reference data/baseline.csv \
        --current   data/drifted.csv \
        --check-mode combined \
        --labeled-current data/drifted.csv \
        --model-uri models:/anomaly-detector@production
"""

import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import mlflow
import pandas as pd
from evidently.metric_preset import DataDriftPreset
from evidently.report import Report

FEATURES = ["latency_p99", "error_rate", "rps"]
DEFAULT_THRESHOLD = 0.15
DEFAULT_PERF_THRESHOLD = 0.70   # minimum acceptable precision on labeled holdout
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outputs", "drift_reports")


@dataclass
class DriftResult:
    score: float            # fraction of features drifted (0.0–1.0)
    is_drift: bool
    threshold: float
    drifted_features: list[str]
    report_path: str
    timestamp: str
    # Performance check fields (populated by check_performance_drift)
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
    """
    Chạy Evidently DataDriftPreset, trả về DriftResult.

    reference_df: training distribution (baseline)
    current_df:   production window data
    threshold:    drift score ngưỡng (0.0–1.0). Score = fraction of features drifted.
    """
    ref = reference_df[FEATURES].copy()
    cur = current_df[FEATURES].copy()

    report = Report(metrics=[DataDriftPreset()])
    report.run(reference_data=ref, current_data=cur)

    result_dict = report.as_dict()
    drift_metrics = result_dict["metrics"][0]["result"]

    # DataDriftPreset result structure
    share_drifted = drift_metrics.get("share_of_drifted_columns", 0.0)
    per_feature = drift_metrics.get("drift_by_columns", {})
    drifted_features = [
        feat for feat, info in per_feature.items()
        if info.get("drift_detected", False)
    ]

    # Save HTML report
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    label = f"-{report_label}" if report_label else ""
    report_filename = f"drift-report{label}-{ts}.html"
    report_path = os.path.join(REPORT_DIR, report_filename)
    report.save_html(report_path)

    return DriftResult(
        score=float(share_drifted),
        is_drift=float(share_drifted) > threshold,
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
    """Evaluate model precision/recall trên labeled holdout để phát hiện concept drift.

    labeled_df phải có cột `anomaly_label` (0=normal, 1=anomaly).
    model_uri: MLflow model URI, e.g. 'models:/anomaly-detector@production'.

    Returns (precision, recall, is_degraded).
    is_degraded = True nếu precision < perf_threshold.
    """
    import mlflow.pyfunc

    if "anomaly_label" not in labeled_df.columns:
        raise ValueError("labeled_df must contain 'anomaly_label' column (0=normal, 1=anomaly)")

    model = mlflow.pyfunc.load_model(model_uri)
    X = labeled_df[FEATURES].dropna()
    y_true = labeled_df.loc[X.index, "anomaly_label"].values

    # IsolationForest predict: -1=anomaly, 1=normal → remap to 1/0
    raw_preds = model.predict(pd.DataFrame(X, columns=FEATURES))
    if hasattr(raw_preds, "values"):
        raw_preds = raw_preds.values
    # Handle both sklearn (-1/1) and already-remapped (0/1) outputs
    if set(raw_preds).issubset({-1, 1}):
        y_pred = (raw_preds == -1).astype(int)
    else:
        y_pred = raw_preds.astype(int)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    is_degraded = precision < perf_threshold

    return precision, recall, is_degraded


def log_to_mlflow(result: DriftResult, experiment_name: str = "anomaly-detection-drift") -> None:
    """Log drift score vào MLflow để visualize trend theo thời gian."""
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)

    with mlflow.start_run(run_name=f"drift-check-{result.timestamp}"):
        mlflow.log_metric("drift_score", result.score)
        mlflow.log_metric("is_drift", float(result.is_drift))
        mlflow.log_param("threshold", result.threshold)
        mlflow.log_param("drifted_features", ",".join(result.drifted_features) or "none")
        mlflow.log_artifact(result.report_path, artifact_path="drift_reports")
        if result.perf_precision is not None:
            mlflow.log_metric("perf_precision", result.perf_precision)
            mlflow.log_metric("perf_recall", result.perf_recall)
            mlflow.log_metric("perf_is_degraded", float(result.perf_is_degraded))


def main():
    parser = argparse.ArgumentParser(description="Detect data drift between two CSVs")
    parser.add_argument("--reference", required=True, help="Path to reference (baseline) CSV")
    parser.add_argument("--current", required=True, help="Path to current (production window) CSV")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Drift score threshold (default: {DEFAULT_THRESHOLD})")
    parser.add_argument(
        "--check-mode", choices=["data", "performance", "combined"], default="combined",
        help="data: Evidently DataDriftPreset only; performance: precision/recall on labeled data; "
             "combined: both (default)",
    )
    parser.add_argument("--labeled-current", default=None,
                        help="CSV with anomaly_label column — required for performance/combined mode")
    parser.add_argument("--model-uri", default="models:/anomaly-detector@production",
                        help="MLflow model URI for performance evaluation")
    parser.add_argument("--perf-threshold", type=float, default=DEFAULT_PERF_THRESHOLD,
                        help=f"Minimum acceptable precision (default: {DEFAULT_PERF_THRESHOLD})")
    parser.add_argument("--log-mlflow", action="store_true", default=False,
                        help="Log drift score to MLflow tracking server")
    args = parser.parse_args()

    ref_df = pd.read_csv(args.reference)
    cur_df = pd.read_csv(args.current)

    # --- Data drift check ---
    if args.check_mode in ("data", "combined"):
        result = detect_drift(ref_df, cur_df, threshold=args.threshold)
        print(f"[drift_detector] check_mode      : {args.check_mode}")
        print(f"[drift_detector] Drift score     : {result.score:.4f}")
        print(f"[drift_detector] Threshold       : {result.threshold}")
        print(f"[drift_detector] Drift detected  : {result.is_drift}")
        print(f"[drift_detector] Drifted features: {result.drifted_features}")
        print(f"[drift_detector] Report saved    : {result.report_path}")
    else:
        # performance-only mode: create a stub result with no data drift info
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        result = DriftResult(
            score=0.0, is_drift=False, threshold=args.threshold,
            drifted_features=[], report_path="", timestamp=ts,
        )

    # --- Performance (concept drift) check ---
    if args.check_mode in ("performance", "combined"):
        if not args.labeled_current:
            parser.error("--labeled-current is required for performance/combined mode")
        labeled_df = pd.read_csv(args.labeled_current)
        precision, recall, is_degraded = check_performance_drift(
            labeled_df, args.model_uri, perf_threshold=args.perf_threshold,
        )
        result.perf_precision = precision
        result.perf_recall = recall
        result.perf_is_degraded = is_degraded
        result.perf_threshold = args.perf_threshold
        print(f"[drift_detector] Perf precision  : {precision:.4f}  (threshold {args.perf_threshold})")
        print(f"[drift_detector] Perf recall     : {recall:.4f}")
        print(f"[drift_detector] Perf degraded   : {is_degraded}")

    # combined is_drift: either data drift OR performance degradation
    any_drift = result.is_drift or result.perf_is_degraded

    if args.log_mlflow:
        log_to_mlflow(result)
        print("[drift_detector] Drift score logged to MLflow.")

    # Push metrics to Prometheus Pushgateway (no-op if pushgateway not running)
    try:
        from metrics_util import push_drift_score, push_model_eval
        push_drift_score(result.score, result.threshold)
        if result.perf_precision is not None:
            f1 = 0.0
            if (result.perf_precision + result.perf_recall) > 0:
                f1 = 2 * result.perf_precision * result.perf_recall / (result.perf_precision + result.perf_recall)
            push_model_eval("current", result.perf_precision, result.perf_recall, f1)
    except ImportError:
        pass

    # Exit code 1 if drift detected — allows shell scripting
    raise SystemExit(1 if any_drift else 0)


if __name__ == "__main__":
    main()
