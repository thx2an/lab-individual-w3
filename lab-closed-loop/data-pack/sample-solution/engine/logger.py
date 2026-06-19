"""Structured JSON logger for the closed-loop orchestrator."""

import json
from datetime import datetime, timezone


class JsonLogger:
    """Emit structured JSON log records to stdout."""

    def __init__(self, name: str):
        self._name = name

    def _emit(self, level: str, event_type: str, **kwargs):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event_type": event_type,
            **kwargs,
        }
        print(json.dumps(record), flush=True)

    def info(self, event_type: str, **kwargs):
        self._emit("INFO", event_type, **kwargs)

    def warning(self, event_type: str, **kwargs):
        self._emit("WARNING", event_type, **kwargs)

    def error(self, event_type: str, **kwargs):
        self._emit("ERROR", event_type, **kwargs)
