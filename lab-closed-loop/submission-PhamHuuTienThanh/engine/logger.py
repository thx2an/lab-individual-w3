"""
engine/logger.py — structured JSON logger for the closed-loop orchestrator.

Every event is a single-line JSON object so it can be:
  - read by humans on stdout,
  - tailed by Promtail → Loki → Grafana ("Audit Log Tail" panel).

Each record always carries: ts, level, event_type, plus any kwargs the caller
passes (service, action, result, runbook, ...). This satisfies the HANDOUT
requirement that "every event has ts, event_type, service, action, result".

If the env var AUDIT_LOG_PATH is set, every record is ALSO appended to that file
as JSON-lines (audit_log.jsonl) for Promtail to scrape.
"""

import json
import os
import threading
from datetime import datetime, timezone

_AUDIT_PATH = os.environ.get("AUDIT_LOG_PATH")
_file_lock = threading.Lock()  # serialize concurrent writers (per-service threads)


class JsonLogger:
    """Emit structured JSON log records to stdout (and optionally to a file)."""

    def __init__(self, component: str):
        self._component = component

    def _emit(self, level: str, event_type: str, **fields):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "component": self._component,
            "event_type": event_type,
            # Keep the canonical keys present even when caller omits them so the
            # Grafana audit panel filters (service / event_type) never break.
            "service": fields.pop("service", None),
            "action": fields.pop("action", None),
            "result": fields.pop("result", None),
            **fields,
        }
        line = json.dumps(record)
        print(line, flush=True)
        if _AUDIT_PATH:
            try:
                with _file_lock, open(_AUDIT_PATH, "a") as fh:
                    fh.write(line + "\n")
            except Exception:
                # Never let audit-file I/O crash the control loop.
                pass

    def info(self, event_type: str, **fields):
        self._emit("INFO", event_type, **fields)

    def warning(self, event_type: str, **fields):
        self._emit("WARNING", event_type, **fields)

    def error(self, event_type: str, **fields):
        self._emit("ERROR", event_type, **fields)
