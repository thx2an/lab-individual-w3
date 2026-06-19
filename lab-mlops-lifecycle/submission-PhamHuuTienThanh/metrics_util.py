"""metrics_util — push MLOps lifecycle metrics to Prometheus Pushgateway.

Best-effort: if the gateway is unreachable (e.g. stack not running) we print a
warning and never raise, so the pipeline keeps working without observability.
Gateway URL comes from PUSHGATEWAY_URL (default http://localhost:9091).
"""

import os

from prometheus_client import CollectorRegistry, Counter, Gauge, push_to_gateway

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://localhost:9091")


def _push(registry: CollectorRegistry, job: str) -> None:
    try:
        push_to_gateway(PUSHGATEWAY_URL, job=job, registry=registry)
    except Exception as exc:  # noqa: BLE001 - best effort, never crash the pipeline
        print(f"[metrics_util] WARNING: pushgateway unreachable — {exc}")


def push_drift_score(score: float, threshold: float) -> None:
    reg = CollectorRegistry()
    Gauge("mlops_drift_score", "Fraction of features drifted (0-1)", registry=reg).set(score)
    Gauge("mlops_drift_threshold", "Configured drift threshold", registry=reg).set(threshold)
    Gauge("mlops_drift_is_drift", "1 if drift detected, 0 otherwise", registry=reg).set(
        1.0 if score > threshold else 0.0
    )
    _push(reg, job="drift_detector")


def push_model_eval(version: str, precision: float, recall: float, f1: float) -> None:
    reg = CollectorRegistry()
    Gauge("mlops_model_precision", "Model precision on eval set", ["version"], registry=reg) \
        .labels(version=version).set(precision)
    Gauge("mlops_model_recall", "Model recall on eval set", ["version"], registry=reg) \
        .labels(version=version).set(recall)
    Gauge("mlops_model_f1", "Model F1 on eval set", ["version"], registry=reg) \
        .labels(version=version).set(f1)
    _push(reg, job="retrain")


def push_event(event_type: str, version: str) -> None:
    """event_type: 'retrain_triggered' | 'auto_rollback_v2_to_v1'."""
    reg = CollectorRegistry()
    c = Counter(f"mlops_{event_type}_total", f"Total {event_type} events", ["version"], registry=reg)
    c.labels(version=version).inc()
    _push(reg, job="retrain")


def push_active_version(version: str, alias: str) -> None:
    reg = CollectorRegistry()
    g_num = Gauge("mlops_active_version_number", "Version number for alias", ["alias"], registry=reg)
    g_info = Gauge("mlops_active_version_info", "alias->version map (value=1)", ["alias", "version"], registry=reg)
    try:
        g_num.labels(alias=alias).set(int(version))
    except (ValueError, TypeError):
        g_num.labels(alias=alias).set(0)
    g_info.labels(alias=alias, version=version).set(1)
    _push(reg, job="retrain")
