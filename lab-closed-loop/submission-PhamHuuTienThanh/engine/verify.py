"""
engine/verify.py — Prometheus-based verification of post-action health.

Design choice: instead of always checking every metric, we pick the metric(s)
that are RELEVANT to the alert that fired, plus a baseline liveness gate (up==1):

    HighLatency   → latency_p99 < latency_p99_max_ms   (+ up==1)
    HighErrorRate → error_rate  < error_rate_max_pct   (+ up==1)
    InstanceDown  → up == up_required

Why: the mock services only emit latency/error samples when they receive
traffic. After an InstanceDown→restart, the latency histogram may legitimately
be empty (no requests yet) and would read as None — checking latency there
would produce false rollbacks. Tying the check to the alert avoids that.

A verify PASS requires `min_samples` CONSECUTIVE healthy reads inside
`timeout_s`, polling every `poll_interval_s`. This guards against a single
lucky scrape.
"""

import time

import requests

from engine.logger import JsonLogger

log = JsonLogger("verify")

# alertname → which checks to run during verify.
ALERT_CHECKS: dict[str, list[str]] = {
    "HighLatency": ["latency", "up"],
    "HighErrorRate": ["error_rate", "up"],
    "InstanceDown": ["up"],
}


def query_prometheus(prometheus_url: str, promql: str) -> float | None:
    """Run an instant query; return the scalar value or None on error/no-data."""
    try:
        resp = requests.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": promql},
            timeout=5,
        )
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        log.error("PROMETHEUS_QUERY_ERROR", query=promql, error=str(exc))
    return None


def _eval_checks(
    prometheus_url: str,
    service: str,
    checks: list[str],
    thresholds: dict,
    queries: dict,
) -> tuple[bool, dict]:
    """Evaluate all required checks once. Returns (all_ok, measured_values)."""
    measured: dict = {}
    all_ok = True

    if "latency" in checks:
        q = queries["latency_p99"].replace("{service}", service)
        val = query_prometheus(prometheus_url, q)
        measured["latency_p99_ms"] = val
        ok = val is not None and val < thresholds["latency_p99_max_ms"]
        all_ok = all_ok and ok

    if "error_rate" in checks:
        q = queries["error_rate_pct"].replace("{service}", service)
        val = query_prometheus(prometheus_url, q)
        measured["error_rate_pct"] = val
        ok = val is not None and val < thresholds["error_rate_max_pct"]
        all_ok = all_ok and ok

    if "up" in checks:
        q = queries["up"].replace("{service}", service)
        val = query_prometheus(prometheus_url, q)
        measured["up"] = val
        ok = val is not None and val >= thresholds["up_required"]
        all_ok = all_ok and ok

    return all_ok, measured


def verify_service(
    prometheus_url: str,
    service: str,
    alertname: str,
    baseline: dict,
    timeout_s: int,
    poll_interval_s: int,
    min_samples: int,
    event_prefix: str = "VERIFY",
) -> bool:
    """Poll Prometheus until `min_samples` consecutive healthy reads, or timeout.

    `event_prefix` lets the caller reuse this for rollback verification
    (event_prefix="ROLLBACK_VERIFY") so the rollback result is itself verified.
    """
    thresholds = baseline["verify_thresholds"]
    queries = baseline["prometheus_queries"]
    checks = ALERT_CHECKS.get(alertname, ["up"])

    deadline = time.time() + timeout_s
    passes = 0
    samples = 0

    log.info(f"{event_prefix}_START", service=service, alertname=alertname,
             checks=checks, timeout_s=timeout_s)

    while time.time() < deadline:
        ok, measured = _eval_checks(prometheus_url, service, checks, thresholds, queries)
        samples += 1
        log.info(f"{event_prefix}_SAMPLE", service=service, sample=samples,
                 healthy=ok, **measured)

        if ok:
            passes += 1
            if passes >= min_samples:
                log.info(f"{event_prefix}_PASS", service=service, samples=samples)
                return True
        else:
            passes = 0  # require CONSECUTIVE healthy reads

        time.sleep(poll_interval_s)

    log.warning(f"{event_prefix}_FAIL", service=service, samples=samples)
    return False
