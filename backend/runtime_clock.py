"""Server-owned elapsed time for dashboard lifecycle metrics."""

import time
from dataclasses import dataclass


@dataclass
class RuntimeClock:
    started_at: float | None = None
    stopped_at: float | None = None

    def start(self) -> None:
        self.started_at = time.monotonic()
        self.stopped_at = None

    def stop(self) -> None:
        if self.started_at is not None and self.stopped_at is None:
            self.stopped_at = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at if self.stopped_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)
