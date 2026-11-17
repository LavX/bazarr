"""One shared deadline for everything that happens after a request ends or is cancelled."""

from __future__ import annotations

import threading
import time


class Grace:
    """A time budget that starts once and is then spent by every step that follows.

    The steps are cancelling leftover tasks, closing async generators, joining
    executor threads, closing the loop and a plugin's own settle calls. Each asks
    :meth:`remaining` for its share, so the total is bounded, not just each step.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = float(seconds)
        self._deadline: float | None = None
        self._lock = threading.Lock()
        self._abandoned: list[str] = []

    def start(self) -> None:
        """Start the clock. Later calls change nothing."""
        with self._lock:
            if self._deadline is None:
                self._deadline = time.monotonic() + self.seconds

    @property
    def started(self) -> bool:
        return self._deadline is not None

    def remaining(self) -> float:
        """Seconds left; all of them until the clock has started."""
        deadline = self._deadline
        return self.seconds if deadline is None else max(0.0, deadline - time.monotonic())

    def expired(self) -> bool:
        return self.started and self.remaining() <= 0.0

    def abandon(self, what: str) -> None:
        """Record that something may still be running: it overran the budget, or its cleanup step failed."""
        with self._lock:
            self._abandoned.append(what)

    @property
    def abandoned(self) -> list[str]:
        """What was left running, in the order it was given up on. Empty when everything ended."""
        with self._lock:
            return list(self._abandoned)
