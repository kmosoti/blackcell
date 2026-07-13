from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from threading import Lock
from typing import Protocol


class RequestQuotaPort(Protocol):
    def consume(self) -> bool: ...


class SlidingWindowRequestQuota:
    """Thread-safe process-local admission for the single API worker."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, int)
            or requests_per_minute < 1
        ):
            raise ValueError("requests_per_minute must be a positive integer")
        self._limit = requests_per_minute
        self._clock = monotonic_clock
        self._accepted: deque[float] = deque()
        self._lock = Lock()

    def consume(self) -> bool:
        now = self._clock()
        cutoff = now - 60.0
        with self._lock:
            while self._accepted and self._accepted[0] <= cutoff:
                self._accepted.popleft()
            if len(self._accepted) >= self._limit:
                return False
            self._accepted.append(now)
            return True


__all__ = ["RequestQuotaPort", "SlidingWindowRequestQuota"]
