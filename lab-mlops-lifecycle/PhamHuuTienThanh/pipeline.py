"""
pipeline.py — Train IsolationForest on baseline data, log to MLflow, register model.

Deliverable P1:
  - Log params  : contamination, n_estimators, random_state (+ training_rows, features)
  - Log metrics : train_anomaly_rate, feature_count
  - Log artifact: model (mlflow.sklearn.log_model) + fitted StandardScaler (pickle)
  - Register    : model name 'anomaly-detector', set alias 'production'

Usage:
    export MLFLOW_TRACKING_URI=http://localhost:5000
    uv run python tien-thanh/pipeline.py --data data/baseline.csv
    uv run python tien-thanh/pipeline.py --data data/baseline.csv --contamination 0.05 --n-estimators 150
"""

import argparse
import os
import pickle
import tempfile

import mlflow
import mlflow.sklearn
import pandas as pd
from mlflow import MlflowClient
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

EXPERIMENT_NAME = "anomaly-detection"
MODEL_NAME = "anomaly-detector"
FEATURES = ["latency_p99", "error_rate", "rps"]


def load_features(csv_path: str) -> pd.DataFrame:
    """Read CSV, validate schema, return only the 3 model features (no NaN)."""
    df = pd.read_csv(csv_path)
    missing = [f for f in FEATURES if f not in df.columns]
    if missing:
        raise ValueError(f"Missing columns in {csv_path}: {missing}")
    return df[FEATURES].dropna()


def train(
    data_path: str,
    contamination: float = 0.03,
    n_estimators: int = 100,
    random_state: int = 42,
) -> str:
    """Train + log + register. Returns the registered version string."""
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT_NAME)

    X = load_features(data_path)

    # StandardScaler is fit on training data and must travel with the model:
    # serving and evaluation have to scale inputs the exact same way.
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(
        contamination=contamination,
        n_estimators=n_estimators,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(X_scaled)

    # IsolationForest.predict: -1 = anomaly, 1 = normal
    labels = model.predict(X_scaled)
    anomaly_rate = float((labels == -1).mean())

    with mlflow.start_run(run_name="train-baseline") as run:
        mlflow.log_param("contamination", contamination)
        mlflow.log_param("n_estimators", n_estimators)
        mlflow.log_param("random_state", random_state)
        mlflow.log_param("training_rows", len(X))
        mlflow.log_param("features", ",".join(FEATURES))

        mlflow.log_metric("train_anomaly_rate", anomaly_rate)
        mlflow.log_metric("feature_count", len(FEATURES))

        # Persist the scaler alongside the model artifact.
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            pickle.dump(scaler, f)
            scaler_path = f.name
        mlflow.log_artifact(scaler_path, artifact_path="scaler")
        os.unlink(scaler_path)

        mlflow.sklearn.log_model(
            sk_model=model,
            artifact_path="model",
            registered_model_name=MODEL_NAME,
            input_example=X.head(3),
        )

        print(f"[pipeline] Run ID      : {run.info.run_id}")
        print(f"[pipeline] Anomaly rate: {anomaly_rate:.4f}")

    # Promote the just-registered (latest) version to alias 'production'.
    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions(f"name='{MODEL_NAME}'")
    latest = max(versions, key=lambda v: int(v.version))
    client.set_registered_model_alias(MODEL_NAME, "production", latest.version)
    print(f"[pipeline] Registered  : {MODEL_NAME} v{latest.version} -> alias 'production'")
    print(f"[pipeline] MLflow UI   : {tracking_uri}/#/models/{MODEL_NAME}")
    return latest.version


def main():
    parser = argparse.ArgumentParser(description="Train anomaly detection model")
    parser.add_argument("--data", required=True, help="Path to training CSV")
    parser.add_argument("--contamination", type=float, default=0.03)
    parser.add_argument("--n-estimators", type=int, default=100)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    train(
        data_path=args.data,
        contamination=args.contamination,
        n_estimators=args.n_estimators,
        random_state=args.random_state,
    )


if __name__ == "__main__":
    main()
