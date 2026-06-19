"""Generic FastAPI mock service. Reads SERVICE_NAME from env."""
import os
import random
import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "unknown")
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.01"))
BASE_LAT_MS = float(os.environ.get("BASE_LATENCY_MS", "50"))
JITTER_MS = float(os.environ.get("JITTER_MS", "10"))

app = FastAPI()
REQUEST_COUNT = Counter("http_requests_total", "Total requests", ["service", "status"])
REQUEST_LAT = Histogram(
    "http_request_duration_seconds",
    "Latency",
    ["service"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE}


@app.get("/")
def root() -> dict:
    lat = max(0.001, random.gauss(BASE_LAT_MS / 1000.0, JITTER_MS / 1000.0))
    time.sleep(lat)
    if random.random() < FAIL_RATE:
        REQUEST_COUNT.labels(SERVICE, "500").inc()
        REQUEST_LAT.labels(SERVICE).observe(lat)
        return Response(content='{"error":"upstream"}', status_code=500, media_type="application/json")
    REQUEST_COUNT.labels(SERVICE, "200").inc()
    REQUEST_LAT.labels(SERVICE).observe(lat)
    return {"service": SERVICE, "latency_s": round(lat, 4)}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
