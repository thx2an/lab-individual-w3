"""
engine/safety.py — blast-radius guard + circuit breaker.

Both are thread-safe because the orchestrator processes different services in
parallel threads (per-service mutex). All shared state is mutated under a lock.
"""

import threading
import time
from collections import defaultdict, deque

from engine.logger import JsonLogger

log = JsonLogger("safety")


class BlastRadiusGuard:
    """Limit how much damage the automation can do in a short window.

    Two independent limits:
      - max_per_minute        : total actions across ALL services / 60s
      - max_restarts_per_hour : actions on a SINGLE service / 3600s
    """

    def __init__(self, max_per_minute: int, max_restarts_per_hour: int):
        self._max_per_minute = max_per_minute
        self._max_restarts_per_hour = max_restarts_per_hour
        self._global_window: deque[float] = deque()
        self._service_window: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _prune(window: deque, horizon: float):
        while window and window[0] < horizon:
            window.popleft()

    def check(self, service: str) -> tuple[bool, str]:
        now = time.time()
        with self._lock:
            self._prune(self._global_window, now - 60)
            self._prune(self._service_window[service], now - 3600)
            if len(self._global_window) >= self._max_per_minute:
                return False, f"global actions/min limit ({self._max_per_minute}) reached"
            if len(self._service_window[service]) >= self._max_restarts_per_hour:
                return False, f"restarts/hour limit ({self._max_restarts_per_hour}) for {service}"
            return True, "ok"

    def record(self, service: str):
        now = time.time()
        with self._lock:
            self._global_window.append(now)
            self._service_window[service].append(now)

    def remaining_per_minute(self) -> int:
        now = time.time()
        with self._lock:
            self._prune(self._global_window, now - 60)
            return max(0, self._max_per_minute - len(self._global_window))


class CircuitBreaker:
    """Halt automation after N consecutive failures (action-exec or verify fail).

    Reset is MANUAL: only a process restart clears the open state. A validation
    failure (scenario 6) must NOT call record_failure — it is not an action
    failure, so it must never trip the breaker.
    """

    def __init__(self, threshold: int):
        self._threshold = threshold
        self._failures = 0
        self._open = False
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def consecutive_failures(self) -> int:
        with self._lock:
            return self._failures

    def record_failure(self) -> bool:
        """Increment the failure counter. Returns True if this trip opened the breaker."""
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold and not self._open:
                self._open = True
                log.error(
                    "CIRCUIT_BREAKER_HALT",
                    consecutive_failures=self._failures,
                    threshold=self._threshold,
                    reset_mode="manual",
                    message="Automation halted. Restart the orchestrator to reset.",
                )
                return True
            return False

    def record_success(self):
        with self._lock:
            self._failures = 0
