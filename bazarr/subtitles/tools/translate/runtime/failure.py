# coding=utf-8
"""Why a translation attempt failed, and what Bazarr+ does about each reason.

A plugin reports a failure on the shared error vocabulary (``auth_required``,
``rate_limited`` and so on). The host adds a few classes of its own: the sandbox
refusing a connection, the worker not being there, a job running out of money or
time, an output that fails the checks. This module turns whatever the worker
session or the pool raised into one ``AttemptFailure`` with a class, and holds the
policy that depends on the class alone:

* ``FAILOVER_CLASSES``: the failures after which the next connection is tried.
  Content and validation errors never fail over, and neither does a connect
  that the sandbox refused (it says nothing about the server).
* ``retries_later``: whether a failed job is tried again later (and keeps its
  checkpoints for the retention period) or ends for good.

Pure functions and small value types. Nothing here touches a database.
"""
from __future__ import annotations

from typing import Any

# The shared vocabulary, as the plugins send it.
AUTH_REQUIRED = "auth_required"
INVALID_CONFIG = "invalid_config"
RATE_LIMITED = "rate_limited"
QUOTA_EXHAUSTED = "quota_exhausted"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
DECLINED = "declined"
CANCELLED = "cancelled"
PARTIAL = "partial"
INTERNAL = "internal"
# A connect the sandbox refused (Landlock answers EACCES for a port the worker does not admit). It says
# nothing about the server, so it is its own class, worded as a Bazarr+ network rule.
SANDBOX_BLOCKED = "sandbox_blocked"

# Classes the host decides itself.
WORKER_FAILED = "worker_failed"            # the worker died, stopped answering or broke the protocol
WORKER_UNAVAILABLE = "worker_unavailable"  # no worker could take the request (pool full, no disk, no binding)
OUTPUT_CHECK_FAILED = "output_check_failed"
MONTH_CAP = "month_cap"                    # the month's money is spent
JOB_CAP = "job_cap"                        # the job's own cap is spent
BUDGET_EXHAUSTED = "budget_exhausted"      # the job's budget ran out while it ran
NO_USABLE_CONNECTION = "no_usable_connection"
GATE_CLOSED = "gate_closed"
PUBLISH_FAILED = "publish_failed"          # the translation came back but the subtitle could not be written
INPUT_CHANGED = "input_changed"            # the source or the destination subtitle changed while the translation ran

FAILOVER_CLASSES = frozenset({RATE_LIMITED, QUOTA_EXHAUSTED, AUTH_REQUIRED, DEPENDENCY_UNAVAILABLE})

# Failures after which the job is tried again later. Everything else ends the job: asking again would
# only repeat it (a content or validation error, a refused connect, a decline) or is not a failure of the job
# at all.
_RETRY_LATER = frozenset({
    RATE_LIMITED, QUOTA_EXHAUSTED, AUTH_REQUIRED, DEPENDENCY_UNAVAILABLE, WORKER_FAILED, WORKER_UNAVAILABLE,
    MONTH_CAP, BUDGET_EXHAUSTED, NO_USABLE_CONNECTION, INTERNAL, PUBLISH_FAILED, INPUT_CHANGED,
})

# What a person is told. A class that carries the plugin's own message keeps it; these cover the host's own.
MESSAGES = {
    SANDBOX_BLOCKED: ("Bazarr+ blocked this connection. A translator plugin may only reach the ports its "
                      "settings list, and this address uses another one. Check the address and its port, "
                      "then try again. This is a Bazarr+ network rule, not a fault of the server."),
    WORKER_FAILED: "The translator plugin stopped before it finished. The job will be tried again later.",
    WORKER_UNAVAILABLE: "The translator plugin could not be started. The job will be tried again later.",
    OUTPUT_CHECK_FAILED: "The translation was refused because it did not pass Bazarr+'s checks.",
    MONTH_CAP: "The monthly translation cost cap is spent. The job will be tried again later.",
    JOB_CAP: "This job's cost cap is spent.",
    BUDGET_EXHAUSTED: "The job's cost budget ran out before it finished. The job will be tried again later.",
    NO_USABLE_CONNECTION: "No back end of this engine can take the job right now.",
    GATE_CLOSED: "Translation is not available, so the normal subtitle search runs instead.",
    PUBLISH_FAILED: "The translation finished but the subtitle could not be written. The job will be tried again later.",
    INPUT_CHANGED: ("The source or the destination subtitle changed while the translation ran, so nothing was written. "
                    "The job will be tried again later."),
}


class AttemptFailure(Exception):
    """One failed attempt on one connection (or on a connectionless engine)."""

    def __init__(self, error_class: str, message: str = "", retry_after: float | None = None,
                 reason: str | None = None):
        super().__init__(error_class, message)
        self.error_class = error_class
        self.message = message
        self.retry_after = retry_after
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.error_class}: {self.message}" if self.message else self.error_class

    @property
    def fails_over(self) -> bool:
        return self.error_class in FAILOVER_CLASSES

    @property
    def retries_later(self) -> bool:
        return retries_later(self.error_class)


def retries_later(error_class: str) -> bool:
    return error_class in _RETRY_LATER


def _retry_after(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and 0 < value < float("inf") else None


# Pool codes (provider_hub.runtime.pool.PoolCodes) that mean the worker that serves a connection could not be
# used, so another connection may be served by another one, as opposed to the host being short of a resource.
_WORKER_BOUND_POOL_CODES = frozenset({"worker_unavailable", "backoff", "circuit_open", "start_failed",
                                      "sandbox_refused", "unknown_worker", "unverified_limit"})


def classify(error: BaseException, *, budget_refusal: str | None = None) -> AttemptFailure:
    """The attempt failure a worker or pool exception stands for.

    ``budget_refusal`` is the reason the host refused the attempt's last reservation, when it did: a plugin
    that is refused a hold ends its attempt as ``cancelled``, and this is how a budget stop is told from the
    user pressing Stop.
    """
    from provider_hub.runtime import errors as session_errors
    from provider_hub.runtime import frames
    from provider_hub.runtime import pool as pool_module

    if isinstance(error, AttemptFailure):
        return error
    if isinstance(error, session_errors.RequestFailed):
        code = error.code
        message = str(error)
        retry_after = _retry_after(error.retry_after)
        reason = getattr(error, "reason", None)
        if code == frames.Codes.SANDBOX_BLOCKED:
            return AttemptFailure(SANDBOX_BLOCKED, MESSAGES[SANDBOX_BLOCKED])
        if code == CANCELLED and budget_refusal:
            return AttemptFailure(_budget_class(budget_refusal), MESSAGES[_budget_class(budget_refusal)])
        if code == CANCELLED and "budget_exhausted" in message:
            # The plugin stopped itself because a call did not fit what it was given.
            return AttemptFailure(BUDGET_EXHAUSTED, MESSAGES[BUDGET_EXHAUSTED])
        if code in (AUTH_REQUIRED, INVALID_CONFIG, RATE_LIMITED, QUOTA_EXHAUSTED, DEPENDENCY_UNAVAILABLE,
                    DECLINED, CANCELLED, PARTIAL):
            return AttemptFailure(code, message, retry_after, reason)
        if code in (frames.Codes.INVALID_REQUEST, frames.Codes.UNKNOWN_OP):
            # The worker refused the request's shape: a fault of the plugin or of the contract, not of the job.
            return AttemptFailure(INTERNAL, message, retry_after, code)
        return AttemptFailure(INTERNAL, message, retry_after, reason or code)
    if isinstance(error, session_errors.RequestCancelled):
        if budget_refusal:
            return AttemptFailure(_budget_class(budget_refusal), MESSAGES[_budget_class(budget_refusal)])
        return AttemptFailure(CANCELLED, "The translation was cancelled.")
    if isinstance(error, session_errors.RequestTimeout):
        return AttemptFailure(WORKER_FAILED, MESSAGES[WORKER_FAILED], reason="timeout")
    if isinstance(error, session_errors.ResultTooLarge):
        return AttemptFailure(INTERNAL, "The plugin's answer was larger than the request allows.",
                              reason="result_too_large")
    if isinstance(error, pool_module.PoolError):
        code = getattr(error, "code", "")
        retry_after = _retry_after(getattr(error, "retry_after", None))
        if code == frames.Codes.SANDBOX_BLOCKED:
            return AttemptFailure(SANDBOX_BLOCKED, MESSAGES[SANDBOX_BLOCKED])
        if code in _WORKER_BOUND_POOL_CODES:
            return AttemptFailure(DEPENDENCY_UNAVAILABLE, MESSAGES[WORKER_UNAVAILABLE], retry_after, code)
        if isinstance(error, pool_module.WorkerKilled):
            return AttemptFailure(WORKER_FAILED, MESSAGES[WORKER_FAILED], retry_after, code)
        return AttemptFailure(WORKER_UNAVAILABLE, MESSAGES[WORKER_UNAVAILABLE], retry_after, code)
    if isinstance(error, session_errors.SessionBusy):
        return AttemptFailure(WORKER_UNAVAILABLE, MESSAGES[WORKER_UNAVAILABLE], reason=error.code)
    if isinstance(error, session_errors.SessionError):
        return AttemptFailure(WORKER_FAILED, MESSAGES[WORKER_FAILED], reason=getattr(error, "code", None))
    return AttemptFailure(INTERNAL, "The translation failed unexpectedly.", reason=type(error).__name__)


def _budget_class(refusal: str) -> str:
    """The class of a refused reservation. ``month_spent`` is the month's cap, anything else the job's."""
    return MONTH_CAP if refusal == "month_spent" else BUDGET_EXHAUSTED
