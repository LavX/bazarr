"""Process-local subtitle-provider health tracker.

Tracks per-provider consecutive failures (timeouts, exceptions, calls
abandoned at the wall-clock deadline) and temporarily auto-discards
providers that are unreliable, with exponential backoff. In-memory
only - state resets on bazarr restart by design, so a crashed provider
is given a fresh chance every time bazarr reboots.

Typical flow from compat/service._do_fanout:

    health = get_tracker()
    exclude = set(_SKIP_FOR_VIRTUAL_VIDEO) | health.currently_discarded()
    results = list_all_subtitles_parallel(
        ..., exclude_providers=exclude,
        on_result=lambda name, outcome, latency_ms: health.record(name, outcome),
    )
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Set

logger = logging.getLogger(__name__)

# Consecutive bad outcomes (timeout / exception / abandoned) before a
# provider is auto-discarded. Kept low so a broken provider doesn't drag
# down every fanout, but high enough to tolerate transient blips.
FAILURES_TO_DISCARD = 3

# Exponential backoff base and cap. First discard = base, each subsequent
# re-discard doubles up to the cap. Cap matters because a provider can
# genuinely recover within ~10 minutes after an outage.
COOLDOWN_BASE_SECONDS = 60
COOLDOWN_MAX_SECONDS = 600

# Outcomes that count as a failure toward auto-discard.
_BAD_OUTCOMES = frozenset({"timeout", "exception", "abandoned"})
# Outcomes that reset the failure counter.
_GOOD_OUTCOMES = frozenset({"ok", "slow"})


class _ProviderState:
    __slots__ = ("consecutive_failures", "discarded_until", "discard_level")

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.discarded_until = 0.0  # monotonic timestamp; 0 = not discarded
        self.discard_level = 0      # n-th discard for exponential backoff


class ProviderHealthTracker:
    """Thread-safe in-memory tracker. Single-lock: all ops are O(1)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, _ProviderState] = {}

    def record(self, name: str, outcome: str, latency_ms: int = 0) -> None:
        """Log one provider call outcome. Outcomes:

            ok / slow      -> success. Resets counters, clears any
                              active discard. "slow" is still a success
                              from health POV (got data, just late).
            timeout        -> fut.result(timeout=) raised.
            exception      -> provider raised anything else.
            abandoned      -> wall_timeout fired before the provider
                              returned. Treated as failure because the
                              caller didn't get data.

        Any other outcome string is ignored (forward-compat).
        """
        with self._lock:
            st = self._state.setdefault(name, _ProviderState())
            if outcome in _GOOD_OUTCOMES:
                st.consecutive_failures = 0
                st.discarded_until = 0.0
                st.discard_level = 0
                return
            if outcome not in _BAD_OUTCOMES:
                return
            st.consecutive_failures += 1
            if st.consecutive_failures >= FAILURES_TO_DISCARD:
                cooldown = min(
                    COOLDOWN_BASE_SECONDS * (2 ** st.discard_level),
                    COOLDOWN_MAX_SECONDS,
                )
                st.discarded_until = time.monotonic() + cooldown
                st.discard_level += 1
                logger.info(
                    "provider_health: discarding %s for %ds "
                    "(level=%d, last_outcome=%s)",
                    name, cooldown, st.discard_level, outcome,
                )
                # Reset the counter so a retry after cooldown gets a
                # fresh FAILURES_TO_DISCARD budget before re-discarding.
                # discard_level persists so the next cooldown is longer.
                st.consecutive_failures = 0

    def is_discarded(self, name: str) -> bool:
        """Is this provider currently under cooldown?"""
        with self._lock:
            st = self._state.get(name)
            if not st or not st.discarded_until:
                return False
            if time.monotonic() < st.discarded_until:
                return True
            # Cooldown elapsed. Clear the active-discard flag so the
            # provider is retried. Leave consecutive_failures in place -
            # one more bad outcome triggers immediate re-discard at the
            # next backoff level. A single "ok" resets everything.
            st.discarded_until = 0.0
            return False

    def currently_discarded(self) -> Set[str]:
        """Snapshot of provider names currently in cooldown."""
        now = time.monotonic()
        out: Set[str] = set()
        with self._lock:
            for name, st in self._state.items():
                if st.discarded_until and now < st.discarded_until:
                    out.add(name)
        return out

    def snapshot(self) -> dict:
        """Introspective view for tests / diagnostics."""
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "consecutive_failures": st.consecutive_failures,
                    "discarded": bool(st.discarded_until and now < st.discarded_until),
                    "discarded_for_seconds": max(0, int(st.discarded_until - now))
                                              if st.discarded_until else 0,
                    "discard_level": st.discard_level,
                }
                for name, st in self._state.items()
            }

    def reset(self) -> None:
        """Drop all state. Used by tests."""
        with self._lock:
            self._state.clear()


_singleton: "ProviderHealthTracker | None" = None
_singleton_lock = threading.Lock()


def get_tracker() -> ProviderHealthTracker:
    """Process-wide lazy singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ProviderHealthTracker()
        return _singleton


def reset_tracker() -> None:
    """Drop the singleton. Used by tests."""
    global _singleton
    with _singleton_lock:
        _singleton = None
