# coding=utf-8
"""The translation cost caps: where they live, how they are written, and the ``CostLimits`` built from them.

A cap is whole micro-dollars in config.yaml (``translator.month_cap_micros`` and ``translator.job_cap_micros``),
``None`` for no cap, beside a revision that goes up on every save. The typed limits route checks the revision
the page loaded, the way an engine or connection save does, so two people editing the caps cannot overwrite
each other unseen. The usage ledger takes its caps as a ``CostLimits`` and reads no settings itself; this is
the one place that builds one from them.

Money crosses the API as dollars (a JSON number) and is converted once, at this edge: a cap rounds down, so
rounding never lets the ledger admit more than the person typed.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .usage import CostLimits, UsageError, to_micros

logger = logging.getLogger(__name__)

# Stands in for a store that has no lock of its own (a test double).
_FALLBACK_LOCK = threading.RLock()

# The most a cap can be. Far above any real bill, and low enough that micro-dollars stay a safe integer in
# every JSON reader and in the database's integer column.
MAX_CAP_DOLLARS = 1_000_000
CAP_KEYS = ("month_cap", "job_cap")


class LimitsError(ValueError):
    """A limits request the server refuses. ``field_errors`` says which cap and why, never repeating a value."""

    def __init__(self, message: str, field_errors: Mapping[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.field_errors = dict(field_errors or {})


class StaleLimitsError(LimitsError):
    """The caps changed since the caller read them; nothing was written."""

    def __init__(self, revision: int):
        super().__init__("The cost limits changed since they were loaded.")
        self.revision = revision


@dataclass(frozen=True)
class StoredLimits:
    month_cap_micros: int | None
    job_cap_micros: int | None
    revision: int


class ConfigLimits:
    """The caps as they live in config.yaml. Tests swap this for an in-memory double."""

    def lock(self):
        """The lock every writer of config.yaml takes. Hold it across a whole read, check and write."""
        from discover.metadata import CONFIG_LOCK

        return CONFIG_LOCK

    def get(self) -> StoredLimits:
        """Both caps and the revision as one snapshot. A reader waits for a write in progress, so it never sees
        a cap that a failed write is about to take back."""
        from app.config import settings

        with self.lock():
            section = settings.translator
            revision = getattr(section, "limits_revision", 1)
            return StoredLimits(_micros_or_none(getattr(section, "month_cap_micros", None)),
                                _micros_or_none(getattr(section, "job_cap_micros", None)),
                                revision if isinstance(revision, int) and not isinstance(revision, bool) else 1)

    def set(self, month_cap_micros: int | None, job_cap_micros: int | None, revision: int) -> bool:
        """Write both caps and the new revision. False, with the live values put back, when the file could not be written."""
        from app.config import settings, write_config

        # The caps are published and persisted under the config lock, which readers take too: a reader sees the
        # old caps or the new ones, never a half-written pair or values a failed write is about to put back.
        with self.lock():
            section = settings.translator
            previous = self.get()
            try:
                section.month_cap_micros = month_cap_micros
                section.job_cap_micros = job_cap_micros
                section.limits_revision = revision
                saved = write_config()
            except Exception:
                logger.exception("Could not save the translation cost limits")
                saved = False
            if saved is not True:
                section.month_cap_micros = previous.month_cap_micros
                section.job_cap_micros = previous.job_cap_micros
                section.limits_revision = previous.revision
                return False
            return True


def _micros_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def micros_to_dollars(micros: int | None) -> float | None:
    """A ledger amount as the dollars an API carries."""
    return None if micros is None else float(Decimal(micros) / Decimal(1_000_000))


def parse_cap(name: str, value: Any) -> int | None:
    """A cap from a request: ``None``, or a finite number of 0 or more, as whole micro-dollars rounded down."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitsError("The values are not valid.", {name: "must be a number or null"})
    if isinstance(value, float) and not math.isfinite(value):
        raise LimitsError("The values are not valid.", {name: "must be a finite number"})
    if value < 0:
        raise LimitsError("The values are not valid.", {name: "must be 0 or more"})
    if value > MAX_CAP_DOLLARS:
        raise LimitsError("The values are not valid.", {name: f"must be at most {MAX_CAP_DOLLARS}"})
    try:
        return to_micros(value, charge=False)
    except UsageError:
        raise LimitsError("The values are not valid.", {name: "must be a number or null"}) from None


class TranslationLimits:
    def __init__(self, store: ConfigLimits | None = None):
        self._store = store or ConfigLimits()

    def read(self) -> StoredLimits:
        return self._store.get()

    def update(self, body: Mapping[str, Any], expected_revision: Any = None) -> StoredLimits:
        """Replace both caps. ``body`` carries both keys; ``None`` clears a cap.

        With ``expected_revision`` the write is refused when the caps moved on since the caller read them.
        Without one it is a plain overwrite, which is what a client that does not track revisions gets.
        """
        errors: dict[str, str] = {}
        parsed: dict[str, int | None] = {}
        for name in CAP_KEYS:
            if name not in body:
                errors[name] = "is required, send a number or null"
                continue
            try:
                parsed[name] = parse_cap(name, body[name])
            except LimitsError as error:
                errors.update(error.field_errors)
        extra = sorted(set(body) - set(CAP_KEYS) - {"expected_revision"})
        for name in extra:
            errors[str(name)] = "is not a cost limit"
        if expected_revision is not None and (isinstance(expected_revision, bool)
                                              or not isinstance(expected_revision, int)):
            errors["expected_revision"] = "must be a number"
        if errors:
            raise LimitsError("The values are not valid.", errors)
        with self._lock():
            current = self._store.get()
            if expected_revision is not None and current.revision != expected_revision:
                raise StaleLimitsError(current.revision)
            revision = current.revision + 1
            if not self._store.set(parsed["month_cap"], parsed["job_cap"], revision):
                raise LimitsError("The cost limits could not be saved.")
            return StoredLimits(parsed["month_cap"], parsed["job_cap"], revision)

    def _lock(self):
        return getattr(self._store, "lock", lambda: _FALLBACK_LOCK)()

    def cost_limits(self, translation_limit: int) -> CostLimits:
        """The caps in force, for the usage ledger."""
        stored = self._store.get()
        return CostLimits(stored.month_cap_micros, stored.job_cap_micros, max(1, int(translation_limit)))


def translation_limit() -> int:
    """Translations that may run at once: the setting the jobs queue applies to translations."""
    from app.config import settings

    value = getattr(settings.translator, "openrouter_max_concurrent", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1


def current_cost_limits() -> CostLimits:
    """The ``CostLimits`` the dispatcher and the ledger apply right now."""
    return TranslationLimits().cost_limits(translation_limit())
