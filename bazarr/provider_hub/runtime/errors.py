# coding=utf-8
"""Host-side exceptions for the v2 worker session.

Every one carries a ``code`` from ``frames.Codes`` so a caller can branch on it
without matching classes or messages.
"""

from __future__ import annotations

from .frames import Codes, FrameError, FrameTooLarge, MalformedFrame

__all__ = [
    "FrameError",
    "FrameTooLarge",
    "MalformedFrame",
    "SessionError",
    "HandshakeError",
    "ProtocolViolation",
    "SessionClosed",
    "WorkerExited",
    "HeartbeatTimeout",
    "SessionBusy",
    "RequestError",
    "RequestFailed",
    "RequestCancelled",
    "RequestTimeout",
    "ResultTooLarge",
    "HostCallError",
]


class SessionError(Exception):
    code = Codes.PROTOCOL_ERROR

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class HandshakeError(SessionError):
    """The worker never said hello, said the wrong thing, or no version matched."""

    code = Codes.HANDSHAKE_FAILED


class ProtocolViolation(SessionError):
    """The worker broke the wire contract. The session is dead afterwards."""


class SessionClosed(SessionError):
    code = Codes.SESSION_CLOSED


class WorkerExited(SessionError):
    code = Codes.WORKER_EXITED

    def __init__(self, message, *, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code


class HeartbeatTimeout(SessionError):
    code = Codes.HEARTBEAT_TIMEOUT


class SessionBusy(SessionError):
    """The session is at its in-flight limit. Nothing was sent."""

    code = Codes.BUSY


class RequestError(Exception):
    code = Codes.PLUGIN_ERROR

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class RequestFailed(RequestError):
    """The worker answered a request with an error.

    The fields mirror ``frames.error_object``. The host scrubs ``message`` and
    ``traceback`` before they get here.
    """

    def __init__(self, message, *, code=None, retryable=False, retry_after=None, class_name=None,
                 traceback=None, errno=None):
        super().__init__(message, code=code)
        self.retryable = retryable
        self.retry_after = retry_after
        self.class_name = class_name
        self.traceback = traceback
        self.errno = errno


class RequestCancelled(RequestError):
    code = Codes.CANCELLED


class RequestTimeout(RequestError):
    code = Codes.TIMEOUT


class ResultTooLarge(RequestError):
    code = Codes.RESULT_TOO_LARGE


class HostCallError(Exception):
    """Raised by a host call handler to answer the worker with an error ack."""

    def __init__(self, code, message="", *, retryable=False):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
