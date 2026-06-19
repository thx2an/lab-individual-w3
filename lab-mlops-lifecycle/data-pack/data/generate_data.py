"""
generate_data.py — Sinh baseline.csv, drifted.csv, holdout.csv, và post_deploy_eval.csv.

Baseline:         30 ngày normal operation, mỗi 10 phút → 4320 rows
Drifted:          7 ngày sau traffic surge + 3rd-party integration → 1008 rows
                  (bao gồm concept drift: một phần label bị flip)
Holdout:          500 rows từ OLD pattern (dùng để validate v2 không overfit)
Post-deploy eval: 200 rows model SHOULD predict well (dùng để monitor v2 sau promote)

Tất cả đều deterministic với SEED=42.

Usage:
    uv run python data/generate_data.py
    uv run python data/generate_data.py --output-dir /path/to/dir
"""

import argparse
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

SEED = 42
INTERVAL_MINUTES = 10
BASELINE_DAYS = 30
DRIFT_DAYS = 7
HOLDOUT_ROWS = 500
POST_DEPLOY_ROWS = 200
CONCEPT_DRIFT_FLIP_FRACTION = 0.25  # 25% labels trong drifted bị flip (concept drift)


def generate_baseline(rng: np.random.Generator, start: datetime) -> pd.DataFrame:
    """30 ngày normal operation — latency ~120ms, error_rate ~0.8%, rps ~450."""
    n = BASELINE_DAYS * 24 * 60 // INTERVAL_MINUTES  # 4320

    timestamps = [start + timedelta(minutes=i * INTERVAL_MINUTES) for i in range(n)]

    # Intraday pattern: latency peaks at business hours (9-18h)
    hours = np.array([t.hour for t in timestamps])
    hour_factor = 1.0 + 0.15 * np.sin((hours - 9) * np.pi / 9) * (hours >= 9) * (hours <= 18)

    latency_p99 = (
        120.0 * hour_factor
        + rng.normal(0, 15, n)
        + rng.exponential(5, n)          # occasional spikes
    ).clip(50, 400)

    error_rate = (
        0.8 + rng.normal(0, 0.3, n)
    ).clip(0.0, 5.0)

    rps = (
        450.0 * hour_factor
        + rng.normal(0, 80, n)
    ).clip(50, 1200)

    return pd.DataFrame({
        "timestamp": [t.isoformat() for t in timestamps],
        "latency_p99": latency_p99.round(2),
        "error_rate": error_rate.round(4),
        "rps": rps.round(1),
    })


def generate_drifted(rng: np.random.Generator, start: datetime) -> pd.DataFrame:
    """7 ngày sau drift: latency mean +30%, error_rate doubled, rps +40%.

    Bao gồm concept drift: CONCEPT_DRIFT_FLIP_FRACTION rows có label bị flip.
    Input features nhìn bình thường (nằm trong drifted range), nhưng
    mối quan hệ feature→anomaly_label đã thay đổi — data drift detector
    sẽ KHÔNG phát hiện điều này; chỉ performance-on-labeled-holdout mới thấy.
    """
    n = DRIFT_DAYS * 24 * 60 // INTERVAL_MINUTES  # 1008

    timestamps = [start + timedelta(minutes=i * INTERVAL_MINUTES) for i in range(n)]

    hours = np.array([t.hour for t in timestamps])
    hour_factor = 1.0 + 0.15 * np.sin((hours - 9) * np.pi / 9) * (hours >= 9) * (hours <= 18)

    # Drift parameters: gradual shift over first 3 days, then stable
    drift_ramp = np.minimum(np.arange(n) / (3 * 24 * 6), 1.0)  # ramp over 3 days

    latency_p99 = (
        120.0 * (1.0 + 0.30 * drift_ramp) * hour_factor
        + rng.normal(0, 20, n)           # higher variance post-integration
        + rng.exponential(8, n)
    ).clip(50, 600)

    error_rate = (
        0.8 * (1.0 + 1.0 * drift_ramp)  # doubles gradually
        + rng.normal(0, 0.5, n)
    ).clip(0.0, 10.0)

    rps = (
        450.0 * (1.0 + 0.40 * drift_ramp) * hour_factor
        + rng.normal(0, 100, n)
    ).clip(50, 1800)

    # Ground-truth anomaly label: 1 = anomaly, 0 = normal
    # Baseline rule: latency_p99 > 200 OR error_rate > 2.5 → anomaly
    anomaly_label = (
        (latency_p99 > 200) | (error_rate > 2.5)
    ).astype(int)

    # Concept drift injection: flip labels on a deterministic subset.
    # These rows have "normal-looking" features but wrong labels — simulating
    # a shift in what constitutes an anomaly (e.g., new payment processor
    # changed the error pattern, old correlation no longer holds).
    n_flip = int(n * CONCEPT_DRIFT_FLIP_FRACTION)
    flip_rng = np.random.default_rng(SEED + 1)
    flip_indices = flip_rng.choice(n, size=n_flip, replace=False)
    anomaly_label[flip_indices] = 1 - anomaly_label[flip_indices]

    return pd.DataFrame({
        "timestamp": [t.isoformat() for t in timestamps],
        "latency_p99": latency_p99.round(2),
        "error_rate": error_rate.round(4),
        "rps": rps.round(1),
        "anomaly_label": anomaly_label,
    })


def generate_holdout(rng: np.random.Generator, start: datetime) -> pd.DataFrame:
    """500 rows từ OLD pattern (baseline distribution) dùng để validate v2.

    v2 retrained chỉ trên drift window sẽ overfit và perform tệ hơn v1 trên holdout.
    Sliding-window strategy (baseline + drift) cần đạt precision/recall >= v1 ở đây.
    """
    timestamps = [start + timedelta(minutes=i * INTERVAL_MINUTES) for i in range(HOLDOUT_ROWS)]

    hours = np.array([t.hour for t in timestamps])
    hour_factor = 1.0 + 0.15 * np.sin((hours - 9) * np.pi / 9) * (hours >= 9) * (hours <= 18)

    latency_p99 = (
        120.0 * hour_factor
        + rng.normal(0, 15, HOLDOUT_ROWS)
        + rng.exponential(5, HOLDOUT_ROWS)
    ).clip(50, 400)

    error_rate = (
        0.8 + rng.normal(0, 0.3, HOLDOUT_ROWS)
    ).clip(0.0, 5.0)

    rps = (
        450.0 * hour_factor
        + rng.normal(0, 80, HOLDOUT_ROWS)
    ).clip(50, 1200)

    anomaly_label = (
        (latency_p99 > 200) | (error_rate > 2.5)
    ).astype(int)

    return pd.DataFrame({
        "timestamp": [t.isoformat() for t in timestamps],
        "latency_p99": latency_p99.round(2),
        "error_rate": error_rate.round(4),
        "rps": rps.round(1),
        "anomaly_label": anomaly_label,
    })


def generate_post_deploy_eval(rng: np.random.Generator, start: datetime) -> pd.DataFrame:
    """200 rows mà model SHOULD predict well sau khi được promote lên production.

    Đây là dữ liệu có pattern rõ ràng — anomaly label được gán theo rule đơn giản,
    không có concept drift. Dùng để monitor v2 sau promote: nếu precision < threshold
    trên tập này, auto-rollback sẽ kích hoạt.
    """
    timestamps = [start + timedelta(minutes=i * INTERVAL_MINUTES) for i in range(POST_DEPLOY_ROWS)]

    hours = np.array([t.hour for t in timestamps])
    hour_factor = 1.0 + 0.15 * np.sin((hours - 9) * np.pi / 9) * (hours >= 9) * (hours <= 18)

    # Mix: 60% clear-normal, 40% clear-anomaly (high latency or high error_rate)
    n_normal = int(POST_DEPLOY_ROWS * 0.6)
    n_anomaly = POST_DEPLOY_ROWS - n_normal

    # Normal segment
    lat_normal = (120.0 * hour_factor[:n_normal] + rng.normal(0, 10, n_normal)).clip(50, 170)
    err_normal = (0.7 + rng.normal(0, 0.2, n_normal)).clip(0.0, 1.5)
    rps_normal = (450.0 * hour_factor[:n_normal] + rng.normal(0, 60, n_normal)).clip(50, 700)
    label_normal = np.zeros(n_normal, dtype=int)

    # Anomaly segment: clearly high latency
    lat_anomaly = (280.0 + rng.normal(0, 30, n_anomaly)).clip(220, 600)
    err_anomaly = (3.5 + rng.normal(0, 0.5, n_anomaly)).clip(2.5, 10.0)
    rps_anomaly = (700.0 + rng.normal(0, 80, n_anomaly)).clip(400, 1800)
    label_anomaly = np.ones(n_anomaly, dtype=int)

    latency_p99 = np.concatenate([lat_normal, lat_anomaly])
    error_rate = np.concatenate([err_normal, err_anomaly])
    rps = np.concatenate([rps_normal, rps_anomaly])
    anomaly_label = np.concatenate([label_normal, label_anomaly])

    return pd.DataFrame({
        "timestamp": [t.isoformat() for t in timestamps],
        "latency_p99": latency_p99.round(2),
        "error_rate": error_rate.round(4),
        "rps": rps.round(1),
        "anomaly_label": anomaly_label,
    })


def main():
    parser = argparse.ArgumentParser(description="Generate lab data files")
    parser.add_argument(
        "--output-dir",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Directory to write all CSV files",
    )
    args = parser.parse_args()

    rng = np.random.default_rng(SEED)
    baseline_start = datetime(2024, 1, 1, 0, 0, 0)
    drift_start = baseline_start + timedelta(days=BASELINE_DAYS + 7)  # gap simulates normal period
    holdout_start = baseline_start + timedelta(days=10)  # mid-baseline slice for holdout
    post_deploy_start = drift_start + timedelta(days=DRIFT_DAYS + 1)

    baseline_df = generate_baseline(rng, baseline_start)
    drifted_df = generate_drifted(rng, drift_start)
    holdout_df = generate_holdout(rng, holdout_start)
    post_deploy_df = generate_post_deploy_eval(rng, post_deploy_start)

    baseline_path = os.path.join(args.output_dir, "baseline.csv")
    drifted_path = os.path.join(args.output_dir, "drifted.csv")
    holdout_path = os.path.join(args.output_dir, "holdout.csv")
    post_deploy_path = os.path.join(args.output_dir, "post_deploy_eval.csv")

    baseline_df.to_csv(baseline_path, index=False)
    drifted_df.to_csv(drifted_path, index=False)
    holdout_df.to_csv(holdout_path, index=False)
    post_deploy_df.to_csv(post_deploy_path, index=False)

    print(f"baseline.csv         : {len(baseline_df)} rows → {baseline_path}")
    print(f"drifted.csv          : {len(drifted_df)} rows → {drifted_path}")
    print(f"  concept drift rows : {int(len(drifted_df) * CONCEPT_DRIFT_FLIP_FRACTION)} flipped labels")
    print(f"holdout.csv          : {len(holdout_df)} rows → {holdout_path}")
    print(f"post_deploy_eval.csv : {len(post_deploy_df)} rows → {post_deploy_path}")
    print(f"Baseline latency mean : {baseline_df['latency_p99'].mean():.1f} ms")
    print(f"Drifted  latency mean : {drifted_df['latency_p99'].mean():.1f} ms")
    print(f"Baseline error_rate mean : {baseline_df['error_rate'].mean():.3f}")
    print(f"Drifted  error_rate mean : {drifted_df['error_rate'].mean():.3f}")


if __name__ == "__main__":
    main()
