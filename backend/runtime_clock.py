"""Server-owned elapsed time for dashboard lifecycle metrics."""

import time
from dataclasses import dataclass


@dataclass
class RuntimeClock:
    accumulated_seconds: float = 0.0
    started_at: float | None = None
    stopped_at: float | None = None

    def start(self) -> None:
        now = time.monotonic()
        if self.started_at is not None and self.stopped_at is None:
            return
        if self.started_at is not None and self.stopped_at is not None:
            self.accumulated_seconds += max(0.0, self.stopped_at - self.started_at)
        self.started_at = now
        self.stopped_at = None

    def stop(self) -> None:
        if self.started_at is not None and self.stopped_at is None:
            self.stopped_at = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        elapsed = self.accumulated_seconds
        if self.started_at is not None:
            end = self.stopped_at if self.stopped_at is not None else time.monotonic()
            elapsed += max(0.0, end - self.started_at)
        return max(0.0, elapsed)
