"""The error vocabulary every plugin kind shares.

A plugin raises :class:`PluginError` with one of the codes below. The host
reads the code to decide what to do next (ask the user to sign in, wait and
retry, fail over to another connection, give up), so a plugin should pick the
code that names the cause and keep the message for people.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ._protocol.frames import error_object
from ._protocol.worker_channel import OperationError

AUTH_REQUIRED = "auth_required"
INVALID_CONFIG = "invalid_config"
RATE_LIMITED = "rate_limited"
QUOTA_EXHAUSTED = "quota_exhausted"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
DECLINED = "declined"
CANCELLED = "cancelled"
PARTIAL = "partial"
INTERNAL = "internal"
# Raised by the host side, never by a plugin: a connection the sandbox refused.
SANDBOX_BLOCKED = "sandbox_blocked"

ERROR_CODES = frozenset(
    {
        AUTH_REQUIRED,
        INVALID_CONFIG,
        RATE_LIMITED,
        QUOTA_EXHAUSTED,
        DEPENDENCY_UNAVAILABLE,
        DECLINED,
        CANCELLED,
        PARTIAL,
        INTERNAL,
        SANDBOX_BLOCKED,
    }
)

# Reasons the host understands for a declined request. Other reasons are kept
# and shown, but only these two change how the host routes the job.
REASON_LANGUAGE_UNSUPPORTED = "language_unsupported"
REASON_SERVER_UNREACHABLE = "server_unreachable"

# Codes after which asking again later can work.
RETRYABLE_CODES = frozenset({RATE_LIMITED, DEPENDENCY_UNAVAILABLE})

_MAX_RETRY_AFTER = 86400.0
_MAX_MESSAGE = 2000
_MAX_DETAIL = 8000


def _clean_retry_after(value: Any) -> float | None:
    """Only a finite, positive number of seconds survives, capped at one day."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds <= 0:
        return None
    return min(_MAX_RETRY_AFTER, seconds)


class PluginError(OperationError):
    """A failure a plugin reports to the host.

    ``code`` is one of :data:`ERROR_CODES`. ``retry_after`` is the number of
    seconds the service asked the caller to wait. ``field_errors`` maps a
    settings field to a message for ``invalid_config``. ``reason`` refines
    ``declined`` (``language_unsupported``, ``server_unreachable``) and may
    qualify other codes.

    It is an :class:`OperationError` of the wire protocol, so a worker built
    directly on the channel handles it too.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retry_after: float | None = None,
        field_errors: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        retry_after = _clean_retry_after(retry_after)
        super().__init__(code, message, retryable=code in RETRYABLE_CODES, retry_after=retry_after)
        self.field_errors: dict[str, str] = {str(k): str(v) for k, v in (field_errors or {}).items()}
        self.reason = reason
        self.detail: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message != self.code else self.code

    def to_dict(self, scrub: Callable[[str], str] | None = None) -> dict[str, Any]:
        """The ``error`` object of a failed response. Absent optional parts are left out.

        ``scrub`` rewrites each text before it is cut to size, so a value it removes
        cannot survive as a fragment at the cut.
        """

        def text(value: str, limit: int, tail: bool = False) -> str:
            value = scrub(value) if scrub is not None else value
            return value[-limit:] if tail else value[:limit]

        data = error_object(
            self.code, text(self.message, _MAX_MESSAGE), retryable=self.retryable, retry_after=self.retry_after
        )
        if self.field_errors:
            data["field_errors"] = {text(key, _MAX_MESSAGE): text(value, _MAX_MESSAGE) for key, value in self.field_errors.items()}
        if self.reason is not None:
            data["reason"] = text(self.reason, _MAX_MESSAGE)
        if self.detail:
            data["traceback"] = text(self.detail, _MAX_DETAIL, tail=True)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PluginError":
        """Rebuild an error from its wire form. An unknown code becomes ``internal``."""
        code = data.get("code")
        message = data.get("message")
        reason = data.get("reason")
        if code not in ERROR_CODES:
            reason = reason if isinstance(reason, str) else (code if isinstance(code, str) else None)
            message = f"{code}: {message}" if message else str(code)
            code = INTERNAL
        field_errors = data.get("field_errors")
        error = cls(
            code,
            message if isinstance(message, str) else "",
            retry_after=data.get("retry_after"),
            field_errors=field_errors if isinstance(field_errors, Mapping) else None,
            reason=reason if isinstance(reason, str) else None,
        )
        detail = data.get("traceback")
        if isinstance(detail, str):
            error.detail = detail
        return error
