# coding=utf-8
"""Host-wide service addresses that Provider Hub plugins can be handed.

One address per capability, kept in the ``endpoints`` settings section under
``<capability, dots as underscores>_url``. FlareSolverr is the first one: a
plugin that can solve its Cloudflare challenges through FlareSolverr reads its
own ``flaresolverr_url``. If it is empty and an address is set here, the host
hands it to the plugin for the duration of the call through a lease
(``capabilities.CallLeases``).

Nothing in this module adopts an address by itself. A FlareSolverr container
that answers on its usual sibling name is reported as a hint, and the setting
is only written when the user accepts the hint.
"""

import logging
import os
import threading
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ENDPOINTS_SECTION = "endpoints"

FLARESOLVERR_CAPABILITY = "flaresolverr.v1"
FLARESOLVERR_SETTING_KEY = "flaresolverr_v1_url"
FLARESOLVERR_SETTING_PATH = f"{ENDPOINTS_SECTION}.{FLARESOLVERR_SETTING_KEY}"
FLARESOLVERR_FORM_KEY = f"settings-{ENDPOINTS_SECTION}-{FLARESOLVERR_SETTING_KEY}"
FLARESOLVERR_ENV = "FLARESOLVERR_URL"
FLARESOLVERR_PROVIDER_FIELD = "flaresolverr_url"

# The name a FlareSolverr container gets next to Bazarr in the usual compose
# stack. It can also resolve through a search domain to a host nobody set up
# for this, which is why it is only ever offered, never used unasked.
FLARESOLVERR_SIBLING_ROOT = "http://flaresolverr:8191"
# What is probed is the service root, which answers a small JSON index. What is
# offered, and handed to providers, is the /v1 endpoint they actually call.
FLARESOLVERR_SIBLING_URL = FLARESOLVERR_SIBLING_ROOT + "/v1"
SIBLING_PROBE_TIMEOUT_SECONDS = 2.0
# The whole attempt, name lookup included. The request timeout alone does not
# bound the lookup.
SIBLING_PROBE_DEADLINE_SECONDS = 3.0
# FlareSolverr's index answers a few dozen bytes. Anything bigger is not it.
SIBLING_PROBE_MAX_BYTES = 8192
SIBLING_CACHE_SECONDS = 300.0

_sibling_lock = threading.Lock()
_sibling_cache: dict = {"checked_at": None, "url": None}
# The one probe that may be running, whoever asked for it. The background
# refresh and an explicit Detect share it, so two probes never run at once and
# a late negative can never overwrite a newer answer.
_probe_flight: dict | None = None


def normalize_endpoint_url(value) -> str:
    """The address when it is a usable http or https URL, else an empty string."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    # Parsers can disagree about backslashes and silently discard whitespace or controls.
    if not text or "\\" in text or any(char.isspace() or ord(char) < 32 for char in text):
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host or port == 0:
        return ""
    return text


def same_flaresolverr_service(first: str, second: str) -> bool:
    """Whether two addresses name the same FlareSolverr service.

    The service root and its ``/v1`` endpoint are one service, and so are
    spellings that differ by a trailing slash or by case in the scheme or host.
    """

    def key(value: str) -> str:
        text = value.strip().rstrip("/")
        if text.lower().endswith("/v1"):
            text = text[:-3].rstrip("/")
        return text.lower()

    return bool(first) and bool(second) and key(first) == key(second)


def flaresolverr_url_source(settings=None) -> tuple[str, str]:
    """The address FlareSolverr is reached at and where it came from.

    The setting wins, then the ``FLARESOLVERR_URL`` environment variable. There is
    no probe fallback: an address nobody typed is never used. The source is
    ``"setting"``, ``"env"`` or ``"none"``.
    """
    if settings is None:
        from app.config import settings
    section = settings.get(ENDPOINTS_SECTION, {}) if hasattr(settings, "get") else {}
    try:
        stored = section.get(FLARESOLVERR_SETTING_KEY)
    except AttributeError:
        stored = getattr(section, FLARESOLVERR_SETTING_KEY, None)
    url = normalize_endpoint_url(stored)
    if url:
        return url, "setting"
    url = normalize_endpoint_url(os.environ.get(FLARESOLVERR_ENV))
    if url:
        return url, "env"
    return "", "none"


def resolve_flaresolverr_url(settings=None) -> str:
    return flaresolverr_url_source(settings)[0]


def may_receive_flaresolverr(installation) -> bool:
    """Whether the host may hand this plugin the FlareSolverr address.

    FlareSolverr fetches any URL it is asked for, so its address is a LAN
    browser. It goes only to a trusted installation or to one whose manifest
    declares it (``"requires": ["flaresolverr.v1"]``, or the older flag
    ``"flaresolverr": true``), never to an untrusted plugin that merely has a
    field of the right name. The declaration is the plugin's own, not a check
    that anyone trusts it: it only keeps the address away from plugins that
    never asked for it.
    """
    trusted = installation.get("trusted") if isinstance(installation, dict) else getattr(installation, "trusted", False)
    if trusted is True:
        return True
    manifest = installation.get("manifest") if isinstance(installation, dict) else getattr(installation, "manifest", None)
    if not isinstance(manifest, dict):
        return False
    requires = manifest.get("requires")
    return manifest.get("flaresolverr") is True or (
        isinstance(requires, (list, tuple)) and FLARESOLVERR_CAPABILITY in requires)


def _read_json_object(response, limit: int, deadline: float | None = None):
    """The JSON object a 200 answer carries, or None when it is not one.

    Whatever answers is not trusted to be short: reading stops one chunk past
    ``limit`` and the answer is refused. The caller closes the response.

    With a ``deadline`` (a ``time.monotonic`` value) the body is read a byte at
    a time and the clock is checked between bytes: a server that drips its
    answer never trips a socket read timeout, so only this check bounds it. A
    body still arriving at the deadline raises ``requests.ReadTimeout``.
    """
    import json

    import requests

    if response.status_code != 200:
        return None
    chunks, size = [], 0
    for chunk in response.iter_content(limit if deadline is None else 1):
        if deadline is not None and time.monotonic() > deadline:
            raise requests.exceptions.ReadTimeout("the answer was still arriving at the deadline")
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return None
    try:
        payload = json.loads(b"".join(chunks))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_flaresolverr_index(payload) -> bool:
    return isinstance(payload, dict) and "flaresolverr" in str(payload.get("msg", "")).casefold()


def _fetch_index(session, url: str, timeout, headers=None, verify=True, deadline: float | None = None):
    """GET the service root and return its JSON object, or None when the answer is not one.

    Connection problems propagate as ``requests`` exceptions. The one probe
    behind both the sibling hint and the Test of a typed address.
    """
    response = session.get(url, timeout=timeout, allow_redirects=False, stream=True,
                           headers=headers, verify=verify)
    try:
        return _read_json_object(response, SIBLING_PROBE_MAX_BYTES, deadline)
    finally:
        response.close()


def _probe_flaresolverr(url: str) -> bool:
    import requests

    try:
        with requests.Session() as session:
            # A container name is looked up directly: a proxy set for the
            # environment must not decide whether the sibling answers.
            session.trust_env = False
            payload = _fetch_index(session, url, SIBLING_PROBE_TIMEOUT_SECONDS)
    except Exception:
        return False
    return _is_flaresolverr_index(payload)


def _record_sibling_locked(found: bool) -> None:
    _sibling_cache["checked_at"] = time.monotonic()
    _sibling_cache["url"] = FLARESOLVERR_SIBLING_URL if found else None


def _settle(flight: dict, found: bool | None, finished: bool = False) -> None:
    """Record the outcome of the flight and wake everyone waiting on it.

    ``found`` is ``None`` when the deadline passed with the network operation
    still running; that counts as a negative answer, but only if the flight has
    not already been settled. A real result always replaces it. ``finished``
    clears the flight, in the same step, because the network operation is over.
    """
    global _probe_flight
    with _sibling_lock:
        if found is None:
            if not flight["recorded"]:
                _record_sibling_locked(False)
        else:
            _record_sibling_locked(found)
        flight["recorded"] = True
        if finished and _probe_flight is flight:
            _probe_flight = None
    flight["settled"].set()


def _join_or_start_probe() -> tuple[dict, threading.Thread | None]:
    """The probe in flight, starting one when none is.

    The second item is the thread that was started, or ``None`` when an
    existing probe was joined.

    ``requests`` timeouts do not bound ``getaddrinfo``, and a slow search domain
    can hold a lookup far longer than the request timeout. The flight therefore
    lasts as long as the network operation does, so repeated requests never
    stack up threads and sockets behind a stuck lookup. Callers do not wait that
    long: at the probe deadline the flight is settled with a negative answer,
    and a later real result still replaces it.
    """
    global _probe_flight
    with _sibling_lock:
        existing = _probe_flight
        if existing is not None:
            return existing, None
        flight = {"settled": threading.Event(), "recorded": False, "started_at": time.monotonic()}
        _probe_flight = flight

    deadline = threading.Timer(SIBLING_PROBE_DEADLINE_SECONDS, _settle, args=(flight, None))
    deadline.name = "flaresolverr-deadline"
    deadline.daemon = True

    def run():
        found = False
        try:
            found = bool(_probe_flaresolverr(FLARESOLVERR_SIBLING_ROOT))
        except Exception:
            logger.debug("FlareSolverr sibling probe failed", exc_info=True)
        finally:
            deadline.cancel()
            _settle(flight, found, finished=True)

    thread = threading.Thread(target=run, name="flaresolverr-probe", daemon=True)
    deadline.start()
    thread.start()
    return flight, thread


def detect_flaresolverr_sibling(refresh: bool = False) -> str | None:
    """Look for the sibling now, within the probe deadline, and cache the answer.

    Meant for an explicit Detect press and for the background refresh. A page
    load must use :func:`sibling_hint` instead, which never waits on a probe.
    A probe already in flight is joined rather than raced. This only reports:
    it never writes the setting.
    """
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
        if not refresh and checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS:
            return _sibling_cache["url"]
    flight, _thread = _join_or_start_probe()
    flight["settled"].wait(SIBLING_PROBE_DEADLINE_SECONDS + 5.0)
    with _sibling_lock:
        return _sibling_cache["url"]


def schedule_sibling_refresh() -> threading.Thread | None:
    """Refresh the cached answer in the background, one probe at a time.

    Returns the thread that was started, or ``None`` when a probe is already
    running.
    """
    return _join_or_start_probe()[1]


def sibling_hint() -> tuple[str | None, bool]:
    """The cached sibling address and whether an answer is still being worked out.

    Never probes in the caller. A missing or stale answer starts a background
    refresh; until it lands the caller gets the last known address (or none).
    ``detecting`` is true while nothing has been learned yet, while a refresh of
    a stale answer is running and while a probe that hit its deadline is still
    out, because the page polls only while it is true.
    """
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
    fresh = checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS
    if not fresh:
        schedule_sibling_refresh()
    # Answer and flight state are read together, after scheduling: a refresh
    # that finished in between must not leave the old address paired with
    # "not detecting".
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
        url = _sibling_cache["url"]
        fresh = checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS
        active = _probe_flight is not None
        # The deadline recorded a negative while the network call is still out;
        # the real answer is yet to come, so the page must keep asking.
        late_answer = active and _probe_flight["recorded"]
    return url, checked_at is None or late_answer or (not fresh and active)


def start_flaresolverr_hint_probe() -> None:
    """Probe the sibling once in the background so the settings page has a hint ready."""
    schedule_sibling_refresh()


def flaresolverr_status(settings=None, refresh: bool = False) -> dict:
    """What the Settings page shows: the effective address, its source and the hint.

    Without ``refresh`` this answers from the cache and never waits on the
    network. With ``refresh`` (an explicit Detect) it waits for a probe, up to the
    deadline.
    """
    if refresh:
        detected = detect_flaresolverr_sibling(refresh=True)
        with _sibling_lock:
            # A probe that ran into its deadline is still out and may yet answer.
            detecting = _probe_flight is not None and _probe_flight["recorded"]
    else:
        detected, detecting = sibling_hint()
    # Read after the probe: a Detect can wait seconds, and the setting may have
    # been saved meanwhile. The hint is compared with what is in use now.
    url, source = flaresolverr_url_source(settings)
    return {
        "url": url,
        "source": source,
        # Once an address is in use there is nothing to accept.
        "detected": detected if detected and not same_flaresolverr_service(detected, url) else None,
        "detecting": detecting,
        # Whether the sibling answered at all, even when it is the address
        # already in use and so has nothing left to offer.
        "sibling_found": detected is not None,
    }


# The Test of an address the user typed. Same rules as the other connection
# tests in Settings (``app.ui``): loopback and LAN hosts are fine, multicast and
# unspecified targets and anything that smuggles a query, a fragment, ``..`` or
# credentials into the address are refused, redirects are never followed, and
# only what a version string can look like is echoed back.
ADDRESS_TEST_REQUEST_TIMEOUT_SECONDS = 4.0
# The whole Test across every address the host name resolves to. The name lookup
# itself is not covered by it, as with the other connection tests.
ADDRESS_TEST_DEADLINE_SECONDS = 10.0
# How long past the deadline the caller waits for the worker before it answers
# "timeout" on its own and cuts the worker's sockets.
ADDRESS_TEST_ANSWER_MARGIN_SECONDS = 0.5
# The /v1 answer to sessions.list carries a session name per open session.
ADDRESS_TEST_V1_MAX_BYTES = 65536
ADDRESS_TEST_MAX_LENGTH = 2048

ADDRESS_TEST_MESSAGES = {
    "ok": "FlareSolverr answered.",
    "not_flaresolverr": "Something answered, but it is not FlareSolverr.",
    "v1_not_flaresolverr": "FlareSolverr answered at the root, but its /v1 endpoint did not respond as expected.",
    "unreachable": "Could not connect. Check the host name, the port and that FlareSolverr is running.",
    "unresolved": "The host name could not be resolved.",
    "timeout": "The address did not answer in time.",
    "tls": "The secure connection failed. Check the certificate, or use http.",
    "invalid_address": ("Not a usable address. Use http or https with a host, an optional port and path, "
                        "and no credentials, query or fragment."),
    "empty": "Type an address to test.",
    "failed": "The test could not be completed.",
}
ADDRESS_TEST_CODES = ("ok", "not_flaresolverr", "unreachable", "timeout", "tls", "invalid_address")


def _address_result(code: str, message_key: str | None = None, version: str | None = None) -> dict:
    message = ADDRESS_TEST_MESSAGES[message_key or code]
    if code == "ok" and version:
        message = f"FlareSolverr {version} answered."
    return {"ok": code == "ok", "code": code, "message": message, "version": version if code == "ok" else None}


def _flaresolverr_root_path(path: str) -> str:
    """The service root's path: the address without a trailing slash or a /v1 endpoint."""
    path = (path or "").rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[:-3].rstrip("/")
    return path


class _SocketTracker:
    """The sockets one Test opened, so the waiting side can cut them.

    A far end that drips its status line or headers one byte at a time never
    trips a socket read timeout, because every byte restarts it, and neither the
    request timeout nor the body deadline can see inside ``getresponse``. Cutting
    the connection from outside makes the blocked read fail at once. Each socket
    is held through a ``dup``: sharing the one connection, it survives the TLS
    wrapper detaching the original, and shutting it down shuts the connection.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sockets: list = []
        self._cut = False

    def add(self, sock) -> None:
        import socket

        with self._lock:
            if not self._cut:
                self._sockets.append(sock.dup())
                return
        # Opened after the Test was given up on: never let it connect on.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _drain(self, shutdown: bool) -> None:
        import socket

        with self._lock:
            sockets, self._sockets = self._sockets, []
            self._cut = self._cut or shutdown
        for sock in sockets:
            try:
                if shutdown:
                    sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def cut(self) -> None:
        """Shut every connection down, and any opened from now on."""
        self._drain(shutdown=True)

    def release(self) -> None:
        """The Test is over: drop the duplicates and leave the connections alone."""
        self._drain(shutdown=False)


def _tracking_adapter(tracker: _SocketTracker):
    """A ``requests`` adapter whose connections report their sockets to ``tracker``."""
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class Connection(HTTPConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            tracker.add(sock)
            return sock

    class SecureConnection(HTTPSConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            tracker.add(sock)
            return sock

    class Pool(HTTPConnectionPool):
        ConnectionCls = Connection

    class SecurePool(HTTPSConnectionPool):
        ConnectionCls = SecureConnection

    class Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": Pool, "https": SecurePool}

    return Adapter(max_retries=0)


def _attempt_flaresolverr(base, ip, hostname, pin, deadline, version_of, tracker) -> tuple[dict, bool]:
    """Probe the root, then the /v1 endpoint, through one resolved address.

    Returns the result and whether the next address is worth trying: only when
    this one could not be connected to (refused, unreachable or a connect timeout).
    """
    import requests
    from urllib3.exceptions import ReadTimeoutError

    from app.ui import _build_request_url

    def remaining() -> tuple[float, float] | None:
        """The (connect, read) timeout for the next request, capped by what is left."""
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        capped = min(ADDRESS_TEST_REQUEST_TIMEOUT_SECONDS, left)
        return capped, capped

    try:
        with requests.Session() as session:
            # Direct, like the sibling probe: a proxy set for the environment
            # must not decide whether the address answers.
            session.trust_env = False
            adapter = _tracking_adapter(tracker)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            timeout = remaining()
            if timeout is None:
                return _address_result("timeout"), False
            # The root is probed as the person typed it. The path is not ours
            # to fix here, which is why it is acceptable: the session needs
            # authentication, the body is never echoed, and only a regex-bounded
            # version string and fixed sentences come back.
            url, headers = _build_request_url(base, "", ip, hostname, pin)
            index = _fetch_index(session, url, timeout, headers=headers, deadline=deadline)
            if not _is_flaresolverr_index(index):
                return _address_result("not_flaresolverr"), False
            version = version_of(index)

            timeout = remaining()
            if timeout is None:
                return _address_result("timeout"), False
            url, headers = _build_request_url(base, "/v1", ip, hostname, pin)
            # sessions.list only reads: it names the browser sessions that are
            # open and starts nothing.
            response = session.post(url, json={"cmd": "sessions.list"}, timeout=timeout,
                                    allow_redirects=False, stream=True, headers=headers, verify=True)
            try:
                answer = _read_json_object(response, ADDRESS_TEST_V1_MAX_BYTES, deadline)
            finally:
                response.close()
            if not isinstance(answer, dict) or answer.get("status") != "ok":
                return _address_result("not_flaresolverr", "v1_not_flaresolverr"), False
            return _address_result("ok", version=version or version_of(answer)), False
    except requests.exceptions.SSLError as error:
        logger.debug("FlareSolverr address test: TLS failure (%s)", type(error).__name__)
        return _address_result("tls"), False
    except requests.exceptions.ConnectTimeout as error:
        # Nothing answered at this address; another one the name resolves to may.
        logger.debug("FlareSolverr address test: connect timeout (%s)", type(error).__name__)
        return _address_result("timeout"), True
    except requests.Timeout as error:
        logger.debug("FlareSolverr address test: timeout (%s)", type(error).__name__)
        return _address_result("timeout"), False
    except requests.ConnectionError as error:
        # A body that stalls mid-read reaches here wrapped as a connection error.
        stalled = any(isinstance(arg, ReadTimeoutError) for arg in error.args)
        logger.debug("FlareSolverr address test: %s", "read timeout" if stalled else type(error).__name__)
        return (_address_result("timeout"), False) if stalled else (_address_result("unreachable"), True)
    except requests.RequestException as error:
        logger.debug("FlareSolverr address test: request failed (%s)", type(error).__name__)
        return _address_result("unreachable"), False
    except Exception as error:
        # Last resort, as the sibling tester has: an address that parses but
        # cannot be sent (a non-latin-1 host name in a header, for one) must
        # still end in a sentence, not a server error.
        logger.debug("FlareSolverr address test: failed (%s)", type(error).__name__)
        return _address_result("unreachable", "failed"), False


def check_flaresolverr_address(address) -> dict:
    """Test an address the user typed, saved or not, and say what answered.

    The result is ``{"ok", "code", "message", "version"}``, the code being one
    of ``ok``, ``not_flaresolverr``, ``unreachable``, ``timeout``, ``tls`` and
    ``invalid_address``. The message is a short sentence for the person. It never
    writes the setting, and this module never logs the address, which can name a
    private host. Library debug logs (urllib3) may name the host, as they do for
    any saved service address.

    What is bounded, exactly. The answer comes back within
    ``ADDRESS_TEST_DEADLINE_SECONDS`` plus ``ADDRESS_TEST_ANSWER_MARGIN_SECONDS``
    (``timeout`` if the check has not finished), whatever the far end does: a
    refused or silent connect, an answer held back, or one dripped byte by byte
    in the status line, the headers or the body. Inside, each request carries a
    connect and read timeout capped by the time left, and body reads check the
    clock. The check runs in a daemon worker thread, and when the caller gives up
    the worker's connections are shut down from this side, so a read it is
    blocked in fails at once, not when the far end tires.

    What is not bounded. The name lookup runs first, on the caller's thread, and
    has no timeout (``getaddrinfo``), as with the other connection tests. A
    socket is registered once urllib3 has connected it, so a connection attempt
    cannot be cut from this side: it ends at its own connect timeout, capped by
    the time left. A worker cut while in one therefore ends when that attempt
    returns, and the thread can outlive the answer by up to that connect
    timeout. The answer itself never waits for it.
    """
    import socket

    from app.ui import _BlockedTarget, _handshake_version, _resolve_and_validate_constrained, _validate_test_base_url

    text = address.strip() if isinstance(address, str) else ""
    if not text:
        return _address_result("invalid_address", "empty")
    if len(text) > ADDRESS_TEST_MAX_LENGTH or not normalize_endpoint_url(text):
        return _address_result("invalid_address")
    try:
        parsed = _validate_test_base_url(text)
        if parsed.username is not None or parsed.password is not None:
            return _address_result("invalid_address")
        base = parsed._replace(path=_flaresolverr_root_path(parsed.path))
        resolved_ips, hostname, _ = _resolve_and_validate_constrained(base.geturl())
    except socket.gaierror:
        return _address_result("unreachable", "unresolved")
    except ValueError as error:
        logger.debug("FlareSolverr address test: refused (%s)",
                     error.code if isinstance(error, _BlockedTarget) else type(error).__name__)
        return _address_result("invalid_address")

    def version_of(payload):
        return _handshake_version(payload) if isinstance(payload, dict) else None

    # Pin to the address that was checked, so a rebinding name cannot move the
    # request, except over https where the certificate check does that work.
    pin = base.scheme != "https"
    candidates = resolved_ips if pin else resolved_ips[:1]
    deadline = time.monotonic() + ADDRESS_TEST_DEADLINE_SECONDS
    tracker = _SocketTracker()

    def check() -> dict:
        result = _address_result("unreachable")
        timed_out = False
        for ip in candidates:
            if time.monotonic() >= deadline:
                return _address_result("timeout")
            result, try_next = _attempt_flaresolverr(base, ip, hostname, pin, deadline, version_of, tracker)
            timed_out = timed_out or result["code"] == "timeout"
            # A dual-stack name can answer on one family only: try the next
            # address only when this one could not be connected to at all.
            if not try_next:
                return result
        # Every address failed to connect: one that timed out is the better
        # report than one that was refused.
        return _address_result("timeout") if timed_out else result

    outcome: dict = {}

    def work() -> None:
        try:
            outcome["result"] = check()
        except Exception as error:
            logger.debug("FlareSolverr address test: failed (%s)", type(error).__name__)
            outcome["result"] = _address_result("unreachable", "failed")
        finally:
            tracker.release()

    worker = threading.Thread(target=work, name="flaresolverr-address-test", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()) + ADDRESS_TEST_ANSWER_MARGIN_SECONDS)
    if worker.is_alive():
        # Blocked somewhere a timeout cannot reach. Answer now, and cut its
        # connections so a blocked read ends at once.
        tracker.cut()
        return _address_result("timeout")
    # A worker that ended without a result cannot happen; if it did, say so.
    return outcome.get("result") or _address_result("unreachable", "failed")


def run_flaresolverr_test(typed, settings=None) -> dict:
    """The Test as the Settings page asks for it.

    A typed address is checked as typed. An empty field checks the address in
    use (the saved setting, then ``FLARESOLVERR_URL``), so a person who typed
    nothing still gets an answer for what providers will use. The result also
    carries ``source``: ``typed``, ``setting``, ``env`` or ``none`` (nothing to
    test).
    """
    if typed is None or (isinstance(typed, str) and not typed.strip()):
        address, source = flaresolverr_url_source(settings)
        typed = address or None
    else:
        source = "typed"
    result = check_flaresolverr_address(typed)
    result["source"] = source
    _record_health_of_address_in_use(typed, result, settings)
    return result


def _record_health_of_address_in_use(address, result: dict, settings=None) -> None:
    """A Test of the address providers use is also the health the cards show. A different typed address is not."""
    try:
        in_use = flaresolverr_url_source(settings)[0]
        if isinstance(address, str) and same_flaresolverr_service(address, in_use):
            from . import service_health

            service_health.record(FLARESOLVERR_CAPABILITY, in_use, result)
    except Exception as error:
        logger.warning("Could not keep the result of a FlareSolverr test (%s)", type(error).__name__)
