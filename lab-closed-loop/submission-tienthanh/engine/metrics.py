"""
engine/metrics.py — Prometheus metrics exposed by the orchestrator on :9100.

Prometheus scrapes this endpoint as job=closed-loop
(target host.docker.internal:9100, see configs/prometheus.yml). These feed the
"Orchestrator State" and "Action Timeline" rows of the Grafana dashboard.
"""

from prometheus_client import Counter, Gauge, start_http_server

# Total actions executed, broken down by outcome.
# outcome ∈ {success, rollback, fail, dry_run}
action_counter = Counter(
    "closed_loop_actions_total",
    "Total closed-loop actions executed",
    ["service", "runbook", "outcome"],
)

# Circuit-breaker state per service: 0=CLOSED, 1=OPEN.
circuit_breaker_gauge = Gauge(
    "closed_loop_circuit_breaker_state",
    "Circuit-breaker state per service (0=closed 1=open)",
    ["service"],
)

# Remaining actions allowed in the current blast-radius (per-minute) window.
blast_radius_gauge = Gauge(
    "closed_loop_blast_radius_remaining",
    "Remaining actions allowed in the current blast-radius window",
    ["service"],
)

# Per-service mutex: 0=FREE, 1=LOCKED.
mutex_gauge = Gauge(
    "closed_loop_mutex_locked",
    "Per-service mutex state (0=free 1=locked)",
    ["service"],
)

# Last verify result: 0=fail, 1=pass, 2=in_progress.
verify_status_gauge = Gauge(
    "closed_loop_verify_status",
    "Last verify result per service+runbook (0=fail 1=pass 2=in_progress)",
    ["service", "runbook"],
)

_DEFAULT_PORT = 9100
_started = False


def start_metrics_server(port: int = _DEFAULT_PORT) -> None:
    """Start the Prometheus HTTP server once (idempotent)."""
    global _started
    if _started:
        return
    start_http_server(port)
    _started = True
