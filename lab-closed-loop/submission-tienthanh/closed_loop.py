#!/usr/bin/env python3
"""
closed_loop.py — Ronki closed-loop auto-remediation orchestrator.

    uv run python closed_loop.py --config config.yaml [--dry-run]

Pattern: DETECT → DECIDE → DRY-RUN → ACT → VERIFY → ROLLBACK, guarded by the
5 mandatory sub-checkpoints:

  1. dry-run        every runbook is invoked with --dry-run first
  2. blast-radius   per-minute + per-service-per-hour action caps
  3. verify         poll Prometheus >=3x/60s, compare to baseline thresholds
  4. auto-rollback  verify fail -> rollback runbook automatically (and re-verify)
  5. circuit-break  3 consecutive failures -> halt automation (manual reset)

Stress extensions:
  - per-service mutex     different services run in parallel; the same service
                          is serialized (second concurrent alert -> LOCK_BUSY)
  - transactional deploy  multi-step A->B->C; on failure roll back completed
                          steps in reverse order
  - decision validation   runbook names absent from runbook_registry are
                          refused before any subprocess is spawned
"""

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import requests
import yaml

from engine.logger import JsonLogger
from engine.metrics import (
    action_counter,
    blast_radius_gauge,
    circuit_breaker_gauge,
    mutex_gauge,
    start_metrics_server,
    verify_status_gauge,
)
from engine.safety import BlastRadiusGuard, CircuitBreaker
from engine.verify import verify_service

log = JsonLogger("orchestrator")

# Resolve paths relative to this file so runbooks/ are found no matter the CWD.
BASE_DIR = Path(__file__).resolve().parent

# ── Per-service mutex registry ────────────────────────────────────────────────
_service_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def get_service_lock(service: str) -> threading.Lock:
    with _locks_meta:
        return _service_locks.setdefault(service, threading.Lock())


# ── Config / baseline loading ─────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_baseline(cfg: dict, config_path: str) -> dict:
    baseline_path = Path(config_path).parent / cfg["baseline_path"]
    with open(baseline_path) as f:
        return json.load(f)


# ── DETECT ────────────────────────────────────────────────────────────────────

def fetch_active_alerts(alertmanager_url: str) -> list[dict]:
    """Poll Alertmanager for active, non-silenced, non-inhibited alerts."""
    try:
        resp = requests.get(
            f"{alertmanager_url}/api/v2/alerts",
            params={"active": "true", "silenced": "false", "inhibited": "false"},
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.error("ALERTMANAGER_FETCH_ERROR", error=str(exc))
        return []


def extract_service(alert: dict) -> str:
    labels = alert.get("labels", {})
    return labels.get("service") or labels.get("job") or "unknown"


def resolve_script(rel_path: str) -> str:
    """Make a runbook path absolute relative to this file's directory."""
    p = Path(rel_path)
    return str(p if p.is_absolute() else BASE_DIR / p)


# ── ACT helpers ───────────────────────────────────────────────────────────────

def run_runbook(script: str, service: str, dry_run: bool,
                timeout_s: int, extra_args: list[str] | None = None) -> bool:
    """Execute a runbook script. Returns True iff exit code 0."""
    cmd = ["/bin/bash", resolve_script(script), "--service", service]
    if extra_args:
        cmd += extra_args
    if dry_run:
        cmd.append("--dry-run")
    log.info("RUNBOOK_EXEC", script=script, service=service, action=script,
             dry_run=dry_run, args=extra_args or [])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout_s, env=os.environ.copy())
        log.info("RUNBOOK_RESULT", script=script, service=service,
                 returncode=result.returncode, result=("ok" if result.returncode == 0 else "fail"),
                 stdout=result.stdout.strip(), stderr=result.stderr.strip())
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.error("RUNBOOK_TIMEOUT", script=script, service=service, timeout_s=timeout_s)
        return False
    except Exception as exc:
        log.error("RUNBOOK_ERROR", script=script, service=service, error=str(exc))
        return False


def run_step(step: dict, service: str, dry_run: bool, timeout_s: int) -> bool:
    """Run one transactional step described as {name, script, args}."""
    return run_runbook(step["script"], service, dry_run, timeout_s,
                       extra_args=step.get("args", []))


# ── DECIDE: validation (stress 3) ─────────────────────────────────────────────

def validate_runbook(runbook: str, cfg: dict, alertname: str) -> bool:
    """Reject runbook names that are not in the registry (LLM-hallucination guard)."""
    registry = cfg.get("runbook_registry") or list(cfg.get("runbook_map", {}).values())
    if runbook in registry:
        return True
    log.error(
        "DECISION_VALIDATION_FAILED",
        bad_runbook=runbook,
        alertname=alertname,
        raw_decision=runbook,
        action="escalate_no_auto_action",
    )
    return False


# ── Transactional deploy (stress 1) ───────────────────────────────────────────

def run_transactional(alertname: str, service: str, cfg: dict, timeout_s: int) -> bool:
    """Execute the multi-step chain for `alertname`. Returns overall success.

    On a step failure, roll back the steps that completed, in reverse order.
    Emits TRANSACTIONAL_STEP_FAIL / TRANSACTIONAL_ROLLBACK_STEP /
    TRANSACTIONAL_ROLLBACK_COMPLETE.
    """
    steps: list[dict] = cfg["multi_step_map"][alertname]
    rollbacks: list[dict] = cfg.get("multi_step_rollback_map", {}).get(alertname, [])

    completed: list[str] = []
    for step in steps:
        if not run_step(step, service, dry_run=False, timeout_s=timeout_s):
            log.error("TRANSACTIONAL_STEP_FAIL", step=step["name"], service=service,
                      completed_before_failure=completed)
            # Roll back completed steps in reverse order.
            rolled_back: list[str] = []
            for rb in reversed(rollbacks[: len(completed)]):
                log.warning("TRANSACTIONAL_ROLLBACK_STEP", step=rb["name"], service=service)
                run_step(rb, service, dry_run=False, timeout_s=timeout_s)
                rolled_back.append(rb["name"])
            log.info("TRANSACTIONAL_ROLLBACK_COMPLETE", service=service,
                     rolled_back=rolled_back)
            return False
        completed.append(step["name"])
        log.info("TRANSACTIONAL_STEP", step=step["name"], service=service, result="ok")
    return True


# ── Per-alert processing (all checkpoints) ────────────────────────────────────

def process_alert(alert: dict, cfg: dict, baseline: dict,
                  guard: BlastRadiusGuard, cb: CircuitBreaker, global_dry_run: bool):
    alertname = alert.get("labels", {}).get("alertname", "")
    service = extract_service(alert)

    # Never auto-remediate the orchestrator itself / monitoring plane (see
    # ignore_services in config). Its own :9100 target flaps up==0 at startup.
    if service in cfg.get("ignore_services", []):
        log.info("ALERT_IGNORED", alertname=alertname, service=service,
                 reason="service in ignore_services (self/monitoring plane)")
        return

    log.info("ALERT_DETECTED", alertname=alertname, service=service,
             severity=alert.get("labels", {}).get("severity", ""))

    # DECIDE — map alert → runbook
    runbook = cfg["runbook_map"].get(alertname)
    if not runbook:
        log.warning("NO_RUNBOOK", alertname=alertname, service=service)
        return

    # Checkpoint: decision validation (must run BEFORE any subprocess / dry-run)
    if not validate_runbook(runbook, cfg, alertname):
        return  # circuit breaker intentionally NOT touched here
    log.info("DECIDE_RUNBOOK", alertname=alertname, service=service, action=runbook)

    # Checkpoint: blast-radius
    ok, reason = guard.check(service)
    if not ok:
        log.warning("BLAST_RADIUS_EXCEEDED", service=service, reason=reason,
                    action="escalate")
        return
    log.info("BLAST_RADIUS_OK", service=service,
             remaining=guard.remaining_per_minute())

    # Checkpoint: per-service mutex (serialize same service, parallelize others)
    lock = get_service_lock(service)
    if not lock.acquire(blocking=False):
        log.warning("SERVICE_LOCK_BUSY", service=service,
                    message="runbook already running for this service; skipping")
        return
    mutex_gauge.labels(service=service).set(1)
    try:
        _process_locked(alert, alertname, service, runbook, cfg, baseline,
                        guard, cb, global_dry_run)
    finally:
        mutex_gauge.labels(service=service).set(0)
        lock.release()


def _process_locked(alert, alertname, service, runbook, cfg, baseline,
                    guard, cb, global_dry_run):
    timeout_s = cfg["runbook_timeout_seconds"]
    is_multi = alertname in cfg.get("multi_step_map", {})

    # Checkpoint 1: DRY-RUN (always, independent of the global --dry-run flag)
    if not run_runbook(runbook, service, dry_run=True, timeout_s=timeout_s):
        log.error("DRY_RUN_FAIL", runbook=runbook, service=service)
        return
    log.info("DRY_RUN_PASS", runbook=runbook, service=service)

    if global_dry_run:
        action_counter.labels(service=service, runbook=runbook, outcome="dry_run").inc()
        log.info("GLOBAL_DRY_RUN_SKIP", service=service,
                 message="--dry-run set; no real action executed")
        return

    # Reserve blast-radius budget for this action.
    guard.record(service)
    blast_radius_gauge.labels(service=service).set(guard.remaining_per_minute())

    # ACT
    if is_multi:
        if not run_transactional(alertname, service, cfg, timeout_s):
            action_counter.labels(service=service, runbook=runbook, outcome="rollback").inc()
            cb.record_failure()
            circuit_breaker_gauge.labels(service=service).set(1 if cb.is_open() else 0)
            return
    else:
        if not run_runbook(runbook, service, dry_run=False, timeout_s=timeout_s):
            log.error("ACTION_EXEC_FAIL", runbook=runbook, service=service)
            action_counter.labels(service=service, runbook=runbook, outcome="fail").inc()
            cb.record_failure()
            circuit_breaker_gauge.labels(service=service).set(1 if cb.is_open() else 0)
            return
    log.info("ACTION_EXECUTED", runbook=runbook, service=service, action=runbook)

    # Checkpoint 3: VERIFY
    t = baseline["verify_thresholds"]
    verify_status_gauge.labels(service=service, runbook=runbook).set(2)  # in_progress
    verify_ok = verify_service(
        prometheus_url=cfg["prometheus_url"], service=service, alertname=alertname,
        baseline=baseline, timeout_s=t["verify_timeout_seconds"],
        poll_interval_s=t["verify_poll_interval_seconds"],
        min_samples=t["verify_min_samples"],
    )

    if verify_ok:
        verify_status_gauge.labels(service=service, runbook=runbook).set(1)
        action_counter.labels(service=service, runbook=runbook, outcome="success").inc()
        cb.record_success()
        circuit_breaker_gauge.labels(service=service).set(0)
        log.info("ACTION_SUCCESS", alertname=alertname, service=service,
                 action=runbook, result="resolved")
        return

    # Checkpoint 4: AUTO-ROLLBACK (and re-verify the rollback)
    verify_status_gauge.labels(service=service, runbook=runbook).set(0)
    action_counter.labels(service=service, runbook=runbook, outcome="rollback").inc()
    rollback = cfg.get("rollback_map", {}).get(alertname, runbook)
    log.warning("ROLLBACK_TRIGGERED", service=service, action=rollback)
    rb_ok = run_runbook(rollback, service, dry_run=False, timeout_s=timeout_s)
    log.info("ROLLBACK_EXECUTED", service=service, action=rollback,
             result=("ok" if rb_ok else "fail"))

    # Top-mark requirement: the rollback result is itself verified.
    rb_verified = verify_service(
        prometheus_url=cfg["prometheus_url"], service=service, alertname="InstanceDown",
        baseline=baseline, timeout_s=t["verify_timeout_seconds"],
        poll_interval_s=t["verify_poll_interval_seconds"],
        min_samples=t["verify_min_samples"], event_prefix="ROLLBACK_VERIFY",
    )
    log.info("ROLLBACK_RESULT", service=service,
             result=("safe_state" if rb_verified else "still_unhealthy"))

    tripped = cb.record_failure()
    circuit_breaker_gauge.labels(service=service).set(1 if cb.is_open() else 0)
    if tripped:
        log.error("CIRCUIT_OPEN", service=service,
                  message="breaker opened; automation halted")


# ── Main control loop ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ronki closed-loop orchestrator")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect+decide+dry-run only; never execute real actions")
    args = parser.parse_args()

    cfg = load_config(args.config)
    baseline = load_baseline(cfg, args.config)

    guard = BlastRadiusGuard(
        max_per_minute=cfg["blast_radius"]["max_actions_per_minute"],
        max_restarts_per_hour=cfg["blast_radius"]["max_restarts_per_service_per_hour"],
    )
    cb = CircuitBreaker(threshold=cfg["circuit_breaker"]["consecutive_failure_threshold"])

    start_metrics_server()
    log.info("ORCHESTRATOR_START", config=args.config, dry_run=args.dry_run,
             poll_interval_s=cfg["poll_interval_seconds"],
             audit_log=os.environ.get("AUDIT_LOG_PATH"))

    seen: set[str] = set()  # fingerprints currently being / already handled
    poll = cfg["poll_interval_seconds"]

    while True:
        if cb.is_open():
            log.error("CIRCUIT_OPEN", message="circuit open — polling suspended (manual reset)")
            time.sleep(poll)
            continue

        alerts = fetch_active_alerts(cfg["alertmanager_url"])
        active_fps = {a.get("fingerprint", "") for a in alerts}
        # Forget alerts that have resolved so a later re-fire is processed again
        # (essential for the circuit-breaker scenario: kill→recover→kill→...).
        seen &= active_fps

        for alert in alerts:
            fp = alert.get("fingerprint", "")
            if fp and fp in seen:
                continue
            if fp:
                seen.add(fp)
            # One worker thread per alert → different services proceed in parallel;
            # the per-service mutex serializes same-service alerts.
            threading.Thread(
                target=process_alert,
                args=(alert, cfg, baseline, guard, cb, args.dry_run),
                daemon=True,
            ).start()

        time.sleep(poll)


if __name__ == "__main__":
    main()
