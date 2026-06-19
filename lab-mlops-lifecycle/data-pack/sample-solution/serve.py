"""
serve.py — FastAPI model serving với blue-green support.

Startup: load model từ MLflow Registry alias 'production'.
Endpoints:
  POST /predict               — score một batch features
  GET  /health/active-version — version hiện tại đang serve
  POST /reload                — reload model từ registry (sau khi swap alias)

Usage:
    export MLFLOW_TRACKING_URI=http://localhost:5000
    uv run python serve.py
    uv run python serve.py --host 0.0.0.0 --port 8000
"""

import argparse
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import mlflow
import mlflow.sklearn
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel

# Prometheus metrics for serve.py
_serve_requests = Counter("serve_requests_total", "Total predict requests")
_serve_latency = Histogram(
    "serve_predict_latency_seconds",
    "Predict endpoint latency in seconds",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)
_serve_active_version = Gauge("serve_active_version", "Currently loaded model version number")

MODEL_NAME = "anomaly-detector"
MODEL_URI = f"models:/{MODEL_NAME}@production"
FEATURES = ["latency_p99", "error_rate", "rps"]

# Global model state
_state: dict[str, Any] = {
    "model": None,
    "version": None,
    "model_uri": None,
}


def _load_model() -> None:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    mlflow.set_tracking_uri(tracking_uri)

    client = mlflow.MlflowClient(tracking_uri=tracking_uri)
    alias_mv = client.get_model_version_by_alias(MODEL_NAME, "production")

    model = mlflow.sklearn.load_model(MODEL_URI)
    _state["model"] = model
    _state["version"] = alias_mv.version
    _state["model_uri"] = MODEL_URI
    print(f"[serve] Loaded {MODEL_NAME} v{alias_mv.version} from alias 'production'")
    try:
        _serve_active_version.set(int(alias_mv.version))
    except (ValueError, TypeError):
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    yield
    _state["model"] = None


app = FastAPI(title="Anomaly Detector API", lifespan=lifespan)


class PredictRequest(BaseModel):
    # Each item is [latency_p99, error_rate, rps]
    features: list[list[float]]


class PredictResponse(BaseModel):
    predictions: list[int]      # -1 = anomaly, 1 = normal
    scores: list[float]         # raw anomaly score (more negative = more anomalous)
    version: str
    model_name: str


class VersionResponse(BaseModel):
    model_name: str
    version: str
    alias: str
    model_uri: str


@app.get("/metrics")
def metrics():
    """Expose Prometheus metrics for scraping."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    if _state["model"] is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if not req.features:
        raise HTTPException(status_code=422, detail="features must not be empty")

    X = np.array(req.features)
    if X.shape[1] != len(FEATURES):
        raise HTTPException(
            status_code=422,
            detail=f"Expected {len(FEATURES)} features per row ({FEATURES}), got {X.shape[1]}",
        )

    _serve_requests.inc()
    t0 = time.perf_counter()
    predictions = _state["model"].predict(X).tolist()
    scores = _state["model"].score_samples(X).tolist()
    _serve_latency.observe(time.perf_counter() - t0)

    return PredictResponse(
        predictions=predictions,
        scores=scores,
        version=str(_state["version"]),
        model_name=MODEL_NAME,
    )


@app.get("/health/active-version", response_model=VersionResponse)
def active_version():
    if _state["model"] is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return VersionResponse(
        model_name=MODEL_NAME,
        version=str(_state["version"]),
        alias="production",
        model_uri=str(_state["model_uri"]),
    )


@app.post("/reload")
def reload():
    """Reload model từ registry — gọi sau khi alias 'production' được swap."""
    try:
        _load_model()
        return {"status": "reloaded", "version": str(_state["version"])}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description="Run anomaly detector API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload-on-start", action="store_true", default=False)
    args = parser.parse_args()

    uvicorn.run("serve:app", host=args.host, port=args.port, reload=args.reload_on_start)


if __name__ == "__main__":
    main()
