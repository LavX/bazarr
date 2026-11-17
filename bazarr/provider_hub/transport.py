# coding=utf-8
"""Origins for the official catalog and the rules for failing over between them.

The primary origin is ``raw.githubusercontent.com``. A static mirror on GitHub
Pages is the second. This module is the only place that knows either of them:
an origin turns a :class:`FetchKey` into bytes, ``classify_failure`` turns any
error into a typed :class:`Failure`, and :func:`fetch_with_fallback` decides
whether a failed fetch may be retried on the next origin.

The module owns one process-wide record of what it has learnt about each
origin: cooldowns, the primary's remembered Retry-After and probe marks. Every
read and write of it happens under one private lock, so reserving a probe,
lifting a restriction and cutting back a bad expiry each act on the same
record whichever thread calls. It never takes the Provider Hub state lock and
never calls into the state module. A caller loads what was saved with
:func:`restore_state` and, inside its own state write, stores
:func:`export_state` back. It never edits the record itself.

A failure is only ever routed on its type, never on display text.

Open cases:

* Redirects are never followed. Any 3xx answer (a 304 aside) is typed
  ``REDIRECT`` and does not fail over: the origins serve fixed paths and answer
  them directly, so a redirect means something is wrong with the origin. The
  answer is refused by a response hook before ``requests`` can read its body to
  build the next request, so a redirect body is never downloaded.
* When the mirror answers 404 after the primary failed, the primary's failure
  is the one raised: a mirror that lags behind the repository must not read as
  the source not having the file.
* A 304 is only valid for a request that sent an ETag; otherwise it is a
  ``CLIENT_ERROR``.
* A TLS error met while reading the body of an error response is still ``TLS``
  and does not fail over.
* When every origin a job may use is parked, the failure is
  ``UNAVAILABLE``, which is not a trigger.
* A TLS failure of any shape, a handshake ``SSLEOFError`` included, is ``TLS``
  and does not fail over. An abrupt TLS close is indistinguishable here from a
  middlebox tampering with the connection, and the mirror must not be a way
  around a certificate problem.
* A DNS failure fails over only when the resolver said "try again later"
  (``EAI_AGAIN``). A name that does not exist (``EAI_NONAME``, the NXDOMAIN
  case) or any other resolver answer is ``NAME_NOT_FOUND`` and does not fail
  over. The errno of a ``socket.gaierror`` anywhere in the exception chain
  decides; message text is read only when no ``gaierror`` is present.
* A 503 is ``SERVER_ERROR`` and fails over. Its ``Retry-After`` is not
  remembered: only 429 and a 403 with rate-limit markers park the whole
  origin until a reset. A 503 parks the one surface for ``DEFAULT_COOLDOWN``.
* A reset time that is not in the future when the failure is recorded is
  ignored and the ordinary ``RATE_LIMIT_COOLDOWN`` applies.
* A fetch key is validated for the official catalog: a 40-hex commit and one
  segment for the channel. A user source on a branch whose name contains a
  slash would be refused, so its wiring passes the resolved commit, never the
  branch name, or relaxes the check for that source.
* An ETag belongs to the origin that issued it. Callers keep one per origin
  (``FetchResult.origin`` names it) and pass them as a mapping.
"""
from __future__ import annotations

import copy
import json
import re
import socket
import ssl
import threading
import time

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import requests

from .bundle import BundleValidationError


RAW_ORIGIN = "raw"
PAGES_ORIGIN = "pages"

# The one repository the mirror carries.
OFFICIAL_REPO = "LavX/bazarr-provider-catalog"
RAW_HOST = "raw.githubusercontent.com"
# The Pages mirror of the official catalog repository. Project pages of its
# GitHub account are served from this host under the repository's name.
PAGES_HOST = "lavx.github.io"
PAGES_BASE_URL = f"https://{PAGES_HOST}/bazarr-provider-catalog"

SURFACE_CATALOG = "catalog"
SURFACE_BUNDLE = "bundle"
SURFACE_POINTER = "pointer"

DEFAULT_TIMEOUT = 30.0
# Largest body any origin hands back. A catalog is well under a megabyte and a
# single bundle file is a few hundred kilobytes.
MAX_FETCH_BYTES = 16 * 1024 * 1024
# How much of an error body is kept so a secondary-limit message can be read.
ERROR_BODY_LIMIT = 8192

# Cooldown seconds per failure kind when GitHub gave no reset time.
DEFAULT_COOLDOWN = 60.0
RATE_LIMIT_COOLDOWN = 300.0
# No reset time or Retry-After is trusted past this, so a bad header cannot
# park an origin for a day.
MAX_COOLDOWN = 3600.0
# A second caller does not probe a recovering origin while one probe is fresh.
PROBE_WINDOW = 30.0


class FailureKind(str, Enum):
    # Kinds on which the next origin may be tried.
    RATE_LIMITED = "rate_limited"
    REQUEST_TIMEOUT = "request_timeout"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    DNS = "dns"
    # Kinds on which it may not.
    NOT_FOUND = "not_found"
    GONE = "gone"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CLIENT_ERROR = "client_error"
    TLS = "tls"
    NAME_NOT_FOUND = "name_not_found"
    REDIRECT = "redirect"
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    INTEGRITY = "integrity"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


TRIGGER_KINDS = frozenset({
    FailureKind.RATE_LIMITED,
    FailureKind.REQUEST_TIMEOUT,
    FailureKind.SERVER_ERROR,
    FailureKind.TIMEOUT,
    FailureKind.CONNECTION,
    FailureKind.DNS,
})

# Kinds that mean GitHub could not be reached at all, the one the refresh
# message words as a network problem.
UNREACHABLE_KINDS = frozenset({
    FailureKind.TIMEOUT,
    FailureKind.CONNECTION,
    FailureKind.DNS,
    FailureKind.NAME_NOT_FOUND,
    FailureKind.TLS,
})


class TransportError(Exception):
    """A failure the transport detected itself, with its type attached."""

    def __init__(self, kind: FailureKind, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    status: int | None = None
    # Epoch seconds at which the origin says it will answer again.
    retry_at: float | None = None
    # True when the failure was an HTTP answer rather than a transport error.
    http: bool = False

    @property
    def trigger(self) -> bool:
        return self.kind in TRIGGER_KINDS


@dataclass(frozen=True)
class FetchKey:
    repo: str
    channel: str
    commit: str
    path: str


@dataclass(frozen=True)
class FetchResult:
    """What an origin served. ``etag`` is that origin's own: it is only valid
    for a later request to ``origin``."""

    content: bytes
    origin: str
    etag: str | None = None
    not_modified: bool = False


_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_@+=,~-][A-Za-z0-9._@+=,~-]*")


def key_is_addressable(key: FetchKey) -> bool:
    """Whether the origins can address ``key``. A path they cannot is read from GitHub the old way."""
    try:
        _validate_key(key)
    except TransportError:
        return False
    return True


def _validate_key(key: FetchKey) -> None:
    """Refuse a key that could address anything but a file of the catalog.

    Every part ends up in a URL, so each is held to a narrow shape: the
    repository is ``owner/name`` and the channel one segment, each of letters,
    digits, dots, dashes and underscores; the commit is 40 lowercase hex
    digits, so a branch name never gets this far; the path is non-empty
    segments of a plain character set. Dot segments, a query, a fragment, a
    percent escape and a backslash cannot pass, so no key can steer the
    request elsewhere on the same host.

    This is the check for the official catalog. A user source on a branch whose
    name contains a slash would be refused as a channel: the wiring must pass
    the resolved commit, never the branch name, or relax the check for that
    source.
    """
    parts = key.repo.split("/")
    ok = (
        len(parts) == 2 and all(_SEGMENT.fullmatch(part) and part not in (".", "..") for part in parts)
        and (not key.channel or (_SEGMENT.fullmatch(key.channel) and ".." not in key.channel))
        and _COMMIT.fullmatch(key.commit) is not None
        and bool(key.path)
        and all(_PATH_SEGMENT.fullmatch(part) and part not in (".", "..") for part in key.path.split("/"))
    )
    if not ok:
        raise TransportError(FailureKind.MALFORMED, "the fetch key is not a catalog file")


def _causes(error: BaseException) -> Iterable[BaseException]:
    """The error and every exception it wraps, once each."""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        context = None if current.__suppress_context__ else current.__context__
        links = (
            current.__cause__, context, getattr(current, "reason", None),
            getattr(current, "_reason", None), current.args[0] if current.args else None,
        )
        pending.extend(link for link in links if isinstance(link, BaseException))


def _gaierror(error: BaseException) -> socket.gaierror | None:
    return next((cause for cause in _causes(error) if isinstance(cause, socket.gaierror)), None)


def _is_name_resolution_error(cause: BaseException) -> bool:
    return any(kind.__name__ == "NameResolutionError" for kind in type(cause).__mro__)


def _is_dns_failure(error: BaseException) -> bool:
    return _gaierror(error) is not None or any(
        _is_name_resolution_error(cause) for cause in _causes(error)
    )


def _dns_is_transient(error: BaseException) -> bool:
    """Whether the resolver asked for a retry. The errno of a ``gaierror``
    anywhere in the chain decides; text is read only when there is none."""
    resolver = _gaierror(error)
    if resolver is not None:
        return resolver.errno == socket.EAI_AGAIN
    for cause in _causes(error):
        if _is_name_resolution_error(cause):
            text = str(cause).lower()
            return "temporary failure" in text or "try again" in text
    return False


def _retry_at(headers: Any, now: float) -> float | None:
    try:
        if headers.get("retry-after") is not None:
            moment = now + max(0, int(headers["retry-after"]))
        elif headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset") is not None:
            moment = int(headers["x-ratelimit-reset"])
        else:
            return None
        # A value no clock can show is as good as none.
        time.gmtime(moment)
        return float(moment)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _is_secondary_limit(response: Any) -> bool:
    content = getattr(response, "content", b"") or b""
    if len(content) > ERROR_BODY_LIMIT:
        return False
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return False
    message = payload.get("message", "") if isinstance(payload, dict) else ""
    return isinstance(message, str) and message.lower().startswith(
        "you have exceeded a secondary rate limit")


def _classify_http(error: requests.exceptions.HTTPError, now: float) -> Failure:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {}) or {}
    if status in (403, 429):
        limited = (
            status == 429 or _is_secondary_limit(response)
            or headers.get("x-ratelimit-remaining") == "0"
            or headers.get("retry-after") is not None
        )
        if limited:
            return Failure(FailureKind.RATE_LIMITED, status, _retry_at(headers, now), http=True)
        return Failure(FailureKind.FORBIDDEN, status, http=True)
    if not isinstance(status, int):
        return Failure(FailureKind.UNKNOWN, None, http=True)
    if status == 408:
        return Failure(FailureKind.REQUEST_TIMEOUT, status, http=True)
    if status >= 500:
        return Failure(FailureKind.SERVER_ERROR, status, http=True)
    if status == 401:
        return Failure(FailureKind.UNAUTHORIZED, status, http=True)
    if status == 404:
        return Failure(FailureKind.NOT_FOUND, status, http=True)
    if status == 410:
        return Failure(FailureKind.GONE, status, http=True)
    return Failure(FailureKind.CLIENT_ERROR, status, http=True)


def classify_failure(
    error: BaseException, *, cancel_types: tuple[type[BaseException], ...] = (),
    now: float | None = None,
) -> Failure:
    """The typed failure behind ``error``.

    TLS is typed before the connectivity kinds: ``requests.SSLError`` is a
    ``ConnectionError``, and a certificate problem is not a transient outage.
    Anything the classifier does not recognise is not a trigger.
    """
    now = time.time() if now is None else now
    if cancel_types and isinstance(error, cancel_types):
        return Failure(FailureKind.CANCELLED)
    if isinstance(error, TransportError):
        return Failure(error.kind, error.status, getattr(getattr(error, "failure", None), "retry_at", None))
    if isinstance(error, ssl.SSLError):
        # Ahead of ValueError: a certificate verification error is both.
        return Failure(FailureKind.TLS)
    if isinstance(error, BundleValidationError):
        return Failure(FailureKind.INTEGRITY)
    if isinstance(error, ValueError):
        return Failure(FailureKind.MALFORMED)
    if isinstance(error, requests.exceptions.HTTPError):
        return _classify_http(error, now)
    if isinstance(error, requests.exceptions.TooManyRedirects):
        return Failure(FailureKind.REDIRECT)
    if isinstance(error, requests.exceptions.SSLError) or any(
        isinstance(cause, ssl.SSLError) or type(cause).__name__ == "SSLError"
        for cause in _causes(error)
    ):
        return Failure(FailureKind.TLS)
    if isinstance(error, (requests.exceptions.Timeout, TimeoutError)):
        return Failure(FailureKind.TIMEOUT)
    if _is_dns_failure(error):
        return Failure(FailureKind.DNS if _dns_is_transient(error) else FailureKind.NAME_NOT_FOUND)
    if isinstance(error, (
        requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
        ConnectionError,
    )):
        return Failure(FailureKind.CONNECTION)
    return Failure(FailureKind.UNKNOWN)


def _read_capped(response: Any, limit: int) -> bytes:
    """The body of ``response``, refusing more than ``limit`` bytes while it
    is read so an oversized body stops the download."""
    declared = str((getattr(response, "headers", None) or {}).get("Content-Length", "")).strip()
    if declared.isdigit() and int(declared) > limit:
        raise TransportError(FailureKind.OVERSIZED, "the response is larger than allowed")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > limit:
            raise TransportError(FailureKind.OVERSIZED, "the response is larger than allowed")
        chunks.append(chunk)
    return b"".join(chunks)


class Origin:
    """One place the official catalog can be read from."""

    name = ""

    def url(self, key: FetchKey) -> str:
        raise NotImplementedError

    def fetch(
        self, key: FetchKey, etag: str | None = None, *, timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_FETCH_BYTES,
    ) -> FetchResult:
        """One request, no redirects. ``etag`` must be one this origin issued."""
        return self._request(self.url(key), etag, timeout=timeout, max_bytes=max_bytes)

    def _request(
        self, url: str, etag: str | None, *, timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_FETCH_BYTES,
    ) -> FetchResult:
        headers = {"If-None-Match": etag} if etag else {}
        response = requests.get(
            url, timeout=timeout, headers=headers, stream=True, allow_redirects=False,
            hooks={"response": [_refuse_redirect]},
        )
        try:
            status = response.status_code
            if status == 304:
                if not etag:
                    raise TransportError(
                        FailureKind.CLIENT_ERROR, "not modified, but no ETag was sent", status=status)
                return FetchResult(b"", self.name, etag, not_modified=True)
            if 300 <= status < 400:
                raise TransportError(FailureKind.REDIRECT, "the origin redirected", status=status)
            if status >= 400:
                response._content = _read_error_body(response, ERROR_BODY_LIMIT)
                response.raise_for_status()
            if status != 200:
                # Only a 200 is a whole document. A 206 or 204 is a piece or
                # nothing, and must never be taken for the file.
                raise TransportError(
                    FailureKind.CLIENT_ERROR, f"unexpected HTTP status {status}", status=status)
            return FetchResult(
                _read_capped(response, max_bytes), self.name, (response.headers or {}).get("ETag"),
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


def _refuse_redirect(response: Any, **_kwargs: Any) -> Any:
    """Response hook: turn a redirect away before ``requests`` reads its body.

    ``allow_redirects=False`` is not enough. ``Session.send`` still builds the
    next request of a redirect, and doing so reads the whole redirect body,
    whatever its size, before this module sees the response.
    """
    status = getattr(response, "status_code", 0)
    if 300 <= status < 400 and status != 304:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise TransportError(FailureKind.REDIRECT, "the origin redirected", status=status)
    return response


def _read_error_body(response: Any, limit: int) -> bytes:
    """Up to ``limit`` bytes of an error response, so a secondary rate limit
    message can be read. A failure to read it is not the failure being
    reported, unless it is a TLS one, which must not be turned into a status."""
    retained = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=limit):
            retained += chunk
            if len(retained) >= limit:
                break
    except Exception as error:
        if classify_failure(error).kind is FailureKind.TLS:
            raise
        return b""
    return bytes(retained[:limit])


class RawOrigin(Origin):
    name = RAW_ORIGIN

    def url(self, key: FetchKey) -> str:
        _validate_key(key)
        return f"https://{RAW_HOST}/{key.repo}/{key.commit}/{key.path}"


class PagesOrigin(Origin):
    name = PAGES_ORIGIN

    def __init__(self, base_url: str = PAGES_BASE_URL, repo: str = OFFICIAL_REPO):
        self.base_url = base_url.rstrip("/")
        self.repo = repo
        if urlparse(self.base_url).scheme != "https":
            raise ValueError("the mirror must be served over https")

    def url(self, key: FetchKey) -> str:
        _validate_key(key)
        if key.repo.lower() != self.repo.lower():
            # The mirror carries one repository, and a key for another one
            # would be served as if it were this one's.
            raise TransportError(FailureKind.MALFORMED, "the mirror does not carry this repository")
        if not key.channel:
            raise TransportError(FailureKind.MALFORMED, "the mirror is addressed by channel")
        return f"{self.base_url}/v1/{key.channel}/{key.commit}/{key.path}"

    def pointer_url(self, channel: str) -> str:
        """Where the mirror says which commit a channel is at."""
        if not _SEGMENT.fullmatch(channel) or ".." in channel:
            raise TransportError(FailureKind.MALFORMED, "the mirror pointer is addressed by channel")
        return f"{self.base_url}/v1/{channel}/current"


def default_origins() -> list[Origin]:
    """Raw first, the mirror second."""
    return [RawOrigin(), PagesOrigin()]


# -- state ------------------------------------------------------------------

# Shape: ``cooldowns`` maps ``origin:surface`` to ``{until, kind, status, at,
# gen}`` and ``retry_after`` maps an origin to ``{until, at, gen}``: the second
# the restriction lifts, the second it was recorded and the generation it was
# recorded in. ``probes`` maps an origin to ``{at, token}``, when its recovery
# probe was reserved and who holds the reservation.
_STATE: dict[str, dict[str, Any]] = {"cooldowns": {}, "retry_after": {}, "probes": {}}

# Guards every read and write of ``_STATE`` and of the two counters below,
# including each check-and-set step. It is never held across a request or while
# taking any other lock.
_RESTRICTIONS_LOCK = threading.RLock()

# Bumped each time a restriction is recorded. A request notes the value when it
# is admitted, and a success lifts only restrictions recorded at or before it:
# one recorded while the request was in flight is newer than what the request
# saw, whatever the clock says.
_GENERATION = 0
_PROBE_TOKENS = 0
_RESTORED = False


@dataclass(frozen=True)
class Admission:
    """What a request was let through on: the generation it saw and, when it
    is the one recovery probe of an origin, the token of that reservation."""

    generation: int
    probe_token: int | None = None


def current_generation() -> int:
    with _RESTRICTIONS_LOCK:
        return _GENERATION


def _until(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("until")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _number(value: Any, name: str) -> float:
    if isinstance(value, dict):
        value = value.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _recorded_at(value: Any) -> float:
    return _number(value, "at")


def _entry(value: Any) -> dict[str, Any] | None:
    """A saved restriction in its stored shape, or None when it is unusable."""
    until = _until(value)
    if until <= 0:
        return None
    entry = {"until": until, "at": _recorded_at(value)}
    if isinstance(value, dict):
        for name in ("kind", "status"):
            if name in value and isinstance(value[name], (str, int, type(None))):
                entry[name] = value[name]
    return entry


def reset_state() -> None:
    """Forget everything, and allow :func:`restore_state` to load again. For
    tests and for a user resetting the transport."""
    global _RESTORED
    with _RESTRICTIONS_LOCK:
        for section in _STATE.values():
            section.clear()
        _RESTORED = False


def restore_state(saved: Any) -> None:
    """Fold a saved ``transport`` section into the process-wide record.

    A startup call, made once: the process record is authoritative from then
    on and is what gets saved, so a later call is a no-op. Loading never
    shortens a restriction the process already knows (the later expiry wins
    per key), anything that is not a well-formed entry is dropped, and a
    restored restriction counts as recorded now, so a request admitted before
    the load does not lift it.
    """
    global _GENERATION, _RESTORED
    with _RESTRICTIONS_LOCK:
        if _RESTORED:
            return
        _RESTORED = True
        if not isinstance(saved, dict):
            return
        _GENERATION += 1
        for name in ("cooldowns", "retry_after"):
            incoming = saved.get(name)
            for key, value in (incoming.items() if isinstance(incoming, dict) else ()):
                entry = _entry(value)
                if entry is not None and entry["until"] > _until(_STATE[name].get(key)):
                    entry["gen"] = _GENERATION
                    _STATE[name][key] = entry
        incoming = saved.get("probes")
        for key, value in (incoming.items() if isinstance(incoming, dict) else ()):
            mark = _number(value, "at")
            held = _STATE["probes"].get(key)
            if mark > 0 and mark > _number(held, "at"):
                _STATE["probes"][key] = {"at": mark, "token": None}


def export_state() -> dict[str, Any]:
    """A copy of the process-wide record, to store in the Provider Hub state."""
    with _RESTRICTIONS_LOCK:
        return {name: copy.deepcopy(section) for name, section in _STATE.items()}


def _cooldown_key(origin: str, surface: str) -> str:
    return f"{origin}:{surface}"


def record_failure(
    origin: str, surface: str, failure: Failure, now: float, admission: Admission | None = None,
) -> None:
    """Park ``origin`` after a trigger failure, as of ``now``, a reading taken
    after the request ended.

    A rate limit with a reset time still ahead parks the whole origin until
    the reset, the limit being the address's and not the surface's. Any other
    trigger, and a reset that is already past, parks that surface of the origin
    for a short while. A failure of the recovery probe, which only its holder
    can report, retires the reset it was probing.
    """
    global _GENERATION
    with _RESTRICTIONS_LOCK:
        _GENERATION += 1
        owns_probe = admission is not None and _owns_probe(origin, admission)
        if owns_probe:
            # Whatever the failure is, the probe has reported back.
            del _STATE["probes"][origin]
            stale = _STATE["retry_after"].get(origin)
            if stale is not None and _until(stale) <= now:
                del _STATE["retry_after"][origin]
        if failure.kind is FailureKind.RATE_LIMITED and failure.retry_at is not None and failure.retry_at > now:
            until = min(failure.retry_at, now + MAX_COOLDOWN)
            if until > _until(_STATE["retry_after"].get(origin)):
                _STATE["retry_after"][origin] = {"until": until, "at": now, "gen": _GENERATION}
            return
        wait = RATE_LIMIT_COOLDOWN if failure.kind is FailureKind.RATE_LIMITED else DEFAULT_COOLDOWN
        key = _cooldown_key(origin, surface)
        until = now + wait
        if until > _until(_STATE["cooldowns"].get(key)):
            _STATE["cooldowns"][key] = {
                "until": until, "kind": failure.kind.value, "status": failure.status,
                "at": now, "gen": _GENERATION,
            }


def _owns_probe(origin: str, admission: Admission) -> bool:
    held = _STATE["probes"].get(origin)
    return (
        admission.probe_token is not None and isinstance(held, dict)
        and held.get("token") == admission.probe_token
    )


def record_success(origin: str, surface: str, admission: Admission) -> None:
    """Lift what a request admitted as ``admission`` could have resolved.

    A restriction recorded after the admission comes from a request that
    failed while this one was in flight, so a success that left the house
    earlier says nothing about it and leaves it standing. The reset an origin
    is waiting out is lifted only by the request that holds its recovery
    probe: a probe that went stale and was replaced proves nothing about what
    the new holder is testing.
    """
    with _RESTRICTIONS_LOCK:
        key = _cooldown_key(origin, surface)
        if key in _STATE["cooldowns"] and _number(_STATE["cooldowns"][key], "gen") <= admission.generation:
            del _STATE["cooldowns"][key]
        if _owns_probe(origin, admission):
            del _STATE["probes"][origin]
            held = _STATE["retry_after"].get(origin)
            if held is not None and _number(held, "gen") <= admission.generation:
                del _STATE["retry_after"][origin]


def release_probe(origin: str, admission: Admission) -> None:
    """Give up the recovery probe reservation after an answer that is neither
    a success nor a trigger, so the next caller may probe in its place. Only
    the request that holds the reservation can release it: a probe whose mark
    went stale and was taken over by another leaves that one alone."""
    with _RESTRICTIONS_LOCK:
        if _owns_probe(origin, admission):
            _STATE["probes"].pop(origin, None)


def _clamped(section: str, key: str, now: float) -> float:
    """The expiry stored under ``key``, with an impossible one cut back.

    No writer here stores an expiry more than ``MAX_COOLDOWN`` ahead or a
    recording time ahead of the clock, so such a value came from a damaged or
    hand-edited state. The expiry is rewritten to ``now`` plus the maximum and
    the recording time to ``now``, once, so the entry still expires.
    """
    value = _STATE[section].get(key)
    until = _until(value)
    ceiling = now + MAX_COOLDOWN
    if isinstance(value, dict):
        repaired = dict(value)
        if until > ceiling:
            repaired["until"] = ceiling
        if _recorded_at(value) > now:
            repaired["at"] = now
        if repaired != value:
            _STATE[section][key] = repaired
    elif until > ceiling:
        _STATE[section][key] = ceiling
    return min(until, ceiling)


def _admit(origin: str, surface: str, now: float) -> Admission | None:
    """Let a request through to ``origin`` for ``surface``, or None when it
    must be skipped right now.

    Once a remembered reset has passed, exactly one caller is let through as
    the probe, holding a token; the others keep skipping until that probe
    reports back or its mark goes stale. A mark counts as fresh only when it
    lies within ``PROBE_WINDOW`` before ``now``: one from the future is not a
    probe in flight. The check, the reservation and the note of the generation
    are one step under the lock.
    """
    global _PROBE_TOKENS
    with _RESTRICTIONS_LOCK:
        if _clamped("cooldowns", _cooldown_key(origin, surface), now) > now:
            return None
        if origin not in _STATE["retry_after"]:
            return Admission(_GENERATION)
        if _clamped("retry_after", origin, now) > now:
            return None
        held = _STATE["probes"].get(origin)
        last_probe = _number(held, "at")
        if now - PROBE_WINDOW < last_probe <= now:
            return None
        _PROBE_TOKENS += 1
        _STATE["probes"][origin] = {"at": now, "token": _PROBE_TOKENS}
        return Admission(_GENERATION, _PROBE_TOKENS)


# -- fallback ---------------------------------------------------------------

@dataclass
class JobRoute:
    """The origin choice of one job. A job fails over at most once: after it
    has, it stays on the origin it moved to and never moves again. The fields
    change only under the module's lock, in the same step as admission."""

    sticky: str | None = None
    failovers: int = 0


def _admit_for_route(
    route: JobRoute, origin: str, primary: str, surface: str, now: float,
) -> Admission | None:
    """Admission of a request to ``origin`` as part of the job on ``route``.

    The route is read, and the failover claimed, in the same step as the
    restriction checks, so a sibling request that failed over a moment ago
    keeps this one off the primary, and a job moves to the mirror only once
    the mirror is actually admitted.
    """
    with _RESTRICTIONS_LOCK:
        if route.sticky is not None and route.sticky != origin:
            return None
        admission = _admit(origin, surface, now)
        if admission is not None and origin != primary and route.sticky is None:
            route.sticky = origin
            route.failovers += 1
        return admission


class NoOriginAvailable(TransportError):
    """Every origin the job may use is parked or has failed.

    ``failure`` is what the primary is waiting out, when it is, so a caller can show why and until when.
    """

    def __init__(self, message: str, failure: "Failure | None" = None):
        super().__init__(FailureKind.UNAVAILABLE, message)
        self.failure = failure


def parked_failure(origin: str, surface: str, now: float) -> "Failure | None":
    """What ``origin`` is waiting out for ``surface`` as of ``now``, or None when it is not parked.

    A reset time held for the whole origin shows as a rate limit until it. A cooldown of the surface shows as
    the failure that set it.
    """
    with _RESTRICTIONS_LOCK:
        held = _clamped("retry_after", origin, now) if origin in _STATE["retry_after"] else 0.0
        if held > now:
            return Failure(FailureKind.RATE_LIMITED, None, held)
        key = _cooldown_key(origin, surface)
        until = _clamped("cooldowns", key, now) if key in _STATE["cooldowns"] else 0.0
        if until <= now:
            return None
        entry = _STATE["cooldowns"][key]
        try:
            kind = FailureKind(entry.get("kind")) if isinstance(entry, dict) else FailureKind.UNKNOWN
        except ValueError:
            kind = FailureKind.UNKNOWN
        status = entry.get("status") if isinstance(entry, dict) else None
        return Failure(kind, status if isinstance(status, int) else None, until)


def _parked_message(failure: "Failure | None") -> str:
    text = "GitHub is not answering requests right now, and the mirror does not have this file yet."
    if failure is not None and failure.retry_at is not None:
        try:
            moment = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(failure.retry_at))
            text += f" GitHub allows requests again at {moment} UTC."
        except (ValueError, OverflowError, OSError):
            pass
    return text + " Try again later."


def fetch_with_fallback(
    origins: list[Origin], key: FetchKey, *, surface: str, official: bool,
    allow_fallback: bool = True, route: JobRoute | None = None,
    etags: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_FETCH_BYTES, cancel_types: tuple[type[BaseException], ...] = (),
    clock: Callable[[], float] = time.time,
    observer: Callable[[str, str, Failure], None] | None = None,
) -> FetchResult:
    """Fetch ``key`` from the first origin that serves it.

    ``observer(origin, surface, failure)`` is told of each trigger failure
    after it has been recorded, so a caller can show why a fetch moved on.

    Only the official source with the setting on ever leaves the primary, and
    only on a trigger failure of the one before it. With either off, or with
    a single origin, this is one fetch from the primary that never fails over
    and neither reads nor writes the record; the fetch itself is still the
    origin's, with its size cap and its refusal of redirects. ``etags`` maps
    an origin's name to the ETag that origin issued.
    """
    primary = origins[0]
    if not official or not allow_fallback or len(origins) == 1:
        return primary.fetch(key, (etags or {}).get(primary.name), timeout=timeout, max_bytes=max_bytes)

    route = route if route is not None else JobRoute()
    last_error: BaseException | None = None
    for origin in origins:
        try:
            origin.url(key)
        except TransportError as refusal:
            # An origin that cannot address this key is no origin for it. The
            # primary refusing is the caller's error. Another one is skipped
            # before any failover is claimed on its account.
            if origin is primary:
                raise
            last_error = last_error or refusal
            continue
        admission = _admit_for_route(route, origin.name, primary.name, surface, clock())
        if admission is None:
            continue
        try:
            result = origin.fetch(
                key, (etags or {}).get(origin.name), timeout=timeout, max_bytes=max_bytes)
        except Exception as error:
            # The request took time: reset times and cooldowns count from now.
            finished = clock()
            failure = classify_failure(error, cancel_types=cancel_types, now=finished)
            if not failure.trigger:
                release_probe(origin.name, admission)
                if origin is not primary and failure.kind is FailureKind.NOT_FOUND:
                    # A mirror that lags behind the repository has not got the
                    # file yet. That must not read as the source lacking it.
                    if last_error is not None:
                        # The primary's own failure is the one to report.
                        raise last_error from error
                    # The primary was not asked because it is waiting out a
                    # limit or a failure, so the mirror's 404 is the only
                    # answer there is. Report that state, with its reset time.
                    parked = parked_failure(primary.name, surface, finished)
                    raise NoOriginAvailable(_parked_message(parked), parked) from error
                raise
            record_failure(origin.name, surface, failure, finished, admission)
            if observer is not None:
                observer(origin.name, surface, failure)
            last_error = error
            continue
        record_success(origin.name, surface, admission)
        return result
    if last_error is not None:
        raise last_error
    raise NoOriginAvailable("every origin is waiting out a limit or a failure")


# -- the mirror's pointer ------------------------------------------------------

# The pointer is a small JSON document, and a history longer than this is not
# one the mirror publishes.
POINTER_MAX_BYTES = 64 * 1024
POINTER_MAX_HISTORY = 64
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


@dataclass(frozen=True)
class MirrorPointer:
    """What ``/v1/<channel>/current`` says: the commit the channel is at, when
    the mirror published it, and the retained commits, newest first, so
    ``history[0]`` is ``commit``."""

    channel: str
    commit: str
    published_at: str
    history: tuple[str, ...]


def parse_pointer(content: bytes, channel: str) -> MirrorPointer:
    """The pointer ``content`` holds, or ``TransportError`` (malformed) when it
    is anything else: not an object, another channel, a commit that is not 40
    lowercase hex digits, a history that does not start at the commit or
    repeats one, or a time that is not ``YYYY-MM-DDTHH:MM:SSZ``."""
    def refuse(reason: str) -> TransportError:
        return TransportError(FailureKind.MALFORMED, f"the mirror pointer is not valid: {reason}")

    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise refuse("not JSON") from error
    if not isinstance(document, dict):
        raise refuse("not an object")
    commit, published_at, history = document.get("commit"), document.get("published_at"), document.get("history")
    if document.get("channel") != channel:
        raise refuse("another channel")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise refuse("commit")
    if not isinstance(published_at, str) or not _TIMESTAMP.fullmatch(published_at):
        raise refuse("published_at")
    if (
        not isinstance(history, list) or not 1 <= len(history) <= POINTER_MAX_HISTORY
        or history[0] != commit or len(set(history)) != len(history)
        or not all(isinstance(item, str) and _COMMIT.fullmatch(item) for item in history)
    ):
        raise refuse("history")
    return MirrorPointer(channel, commit, published_at, tuple(history))


def check_pointer_advance(
    pointer: MirrorPointer, stored_commit: str | None, accepted_at: str | None,
) -> bool:
    """Whether ``pointer`` names the commit already stored.

    Returns True when it does (nothing to fetch) and False when it is strictly
    ahead of it. Raises ``TransportError`` (kind ``INTEGRITY``) for a pointer
    that cannot be shown to be ahead: one whose commit is older than the
    stored one, whose history does not hold the stored commit, or whose
    ``published_at`` is not later than the last pointer accepted. With no
    stored commit there is nothing to compare, so the pointer stands and the
    caller's own verification is the gate.

    The timestamps are fixed-width UTC strings, so they compare as text.
    """
    if stored_commit is not None:
        stored = stored_commit.lower()
        if pointer.commit == stored:
            return True
        if stored not in pointer.history[1:]:
            raise TransportError(
                FailureKind.INTEGRITY,
                "the mirror cannot prove that its catalog is newer than the one held")
    if accepted_at is not None and not pointer.published_at > accepted_at:
        raise TransportError(
            FailureKind.INTEGRITY, "the mirror pointer is not newer than the last one accepted")
    return False


def fetch_pointer(
    channel: str, *, origin: PagesOrigin | None = None, timeout: float = DEFAULT_TIMEOUT,
    cancel_types: tuple[type[BaseException], ...] = (), clock: Callable[[], float] = time.time,
    observer: Callable[[str, str, Failure], None] | None = None,
) -> MirrorPointer:
    """Read and parse the mirror's pointer for ``channel``.

    The mirror stands in here for the source of branch heads. It obeys the
    mirror's own cooldowns for the pointer surface, and a failure is raised as
    it is. The caller decides whether this source may use the mirror at all.
    """
    origin = origin or PagesOrigin()
    url = origin.pointer_url(channel)
    admission = _admit(origin.name, SURFACE_POINTER, clock())
    if admission is None:
        raise NoOriginAvailable("the mirror is waiting out a failure")
    try:
        result = origin._request(url, None, timeout=timeout, max_bytes=POINTER_MAX_BYTES)
        pointer = parse_pointer(result.content, channel)
    except Exception as error:
        finished = clock()
        failure = classify_failure(error, cancel_types=cancel_types, now=finished)
        if failure.trigger:
            record_failure(origin.name, SURFACE_POINTER, failure, finished, admission)
            if observer is not None:
                observer(origin.name, SURFACE_POINTER, failure)
        else:
            release_probe(origin.name, admission)
        raise
    record_success(origin.name, SURFACE_POINTER, admission)
    return pointer
