# coding=utf-8
"""The last health result of a service the user pointed Bazarr+ at, for its card.

A remote service is not Bazarr+'s to run, so all Bazarr+ can say about it is whether it answered the last time it
was asked. Each capability declares the check that asks (``CHECKS``). A result is recorded when the user presses
Test, and again, in the background, while the service is in use and the last answer is older than
``FRESH_SECONDS``. The consumer's own errors stay authoritative: a service that answered the check can still fail
a request.

Results live in memory and are tied to the address they were measured against by a hash, so an address that was
changed shows no result until it is tested, and no address is kept here.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time

from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

# How long an answer stands before the next use asks again.
FRESH_SECONDS = 120.0

STATE_READY = "ready"
STATE_UNREACHABLE = "unreachable"

_lock = threading.Lock()
# capability -> {"key": address hash, "result": the public part, "at": monotonic time}
_records: dict[str, dict[str, Any]] = {}
_flights: dict[str, threading.Thread] = {}


def _checks() -> Mapping[str, Callable[[str], dict]]:
    from . import endpoints

    return {endpoints.FLARESOLVERR_CAPABILITY: endpoints.check_flaresolverr_address}


def _key(capability: str, address: str) -> str:
    """Names the service an address points at. FlareSolverr's root and its /v1 endpoint are one service."""
    text = str(address).strip().rstrip("/")
    if text.lower().endswith("/v1"):
        text = text[:-3].rstrip("/")
    return hashlib.sha256(f"{capability}\0{text.lower()}".encode("utf-8")).hexdigest()


def _public(result: Mapping[str, Any]) -> dict[str, Any]:
    ok = result.get("ok") is True
    out: dict[str, Any] = {
        "state": STATE_READY if ok else STATE_UNREACHABLE,
        "code": str(result.get("code") or ("ok" if ok else "unreachable")),
        "checked_at": int(time.time()),
    }
    version = result.get("version")
    if ok and isinstance(version, str) and version:
        out["version"] = version
    return out


def record(capability: str, address: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the answer of a check of ``address``. Replaces the capability's earlier answer."""
    public = _public(result)
    with _lock:
        _records[capability] = {"key": _key(capability, address), "result": public, "at": time.monotonic()}
    return dict(public)


def last(capability: str, address: str) -> dict[str, Any] | None:
    """The last answer for this address, or None when it was never checked or the address has changed since."""
    with _lock:
        held = _records.get(capability)
    if held is None or held["key"] != _key(capability, address):
        return None
    return dict(held["result"])


def _stale(capability: str, address: str) -> bool:
    with _lock:
        held = _records.get(capability)
    return held is None or held["key"] != _key(capability, address) or time.monotonic() - held["at"] >= FRESH_SECONDS


def note_use(capability: str, address: str) -> threading.Thread | None:
    """A call is about to use the service at ``address``: ask it again in the background when the last answer is old.

    Never waits and never raises; one check runs at a time per capability. Returns the thread, for a caller that
    wants to wait for it.
    """
    try:
        check = _checks().get(capability)
        if check is None or not _stale(capability, address):
            return None
        with _lock:
            running = _flights.get(capability)
            if running is not None and running.is_alive():
                return None
            thread = threading.Thread(target=_run, args=(capability, address, check),
                                      name=f"hub-health-{capability}", daemon=True)
            _flights[capability] = thread
        thread.start()
        return thread
    except Exception as error:
        logger.warning("Could not start the health check of %s (%s)", capability, type(error).__name__)
        return None


def _run(capability: str, address: str, check: Callable[[str], dict]) -> None:
    try:
        record(capability, address, check(address))
    except Exception as error:
        # Only the kind of failure is logged: the address may carry what the user typed.
        logger.warning("The health check of %s failed (%s)", capability, type(error).__name__)
        record(capability, address, {"ok": False, "code": "unreachable"})


def forget(capability: str | None = None) -> None:
    """Drop the recorded answers (all, or one capability's)."""
    with _lock:
        if capability is None:
            _records.clear()
        else:
            _records.pop(capability, None)
