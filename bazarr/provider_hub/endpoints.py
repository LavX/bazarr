# coding=utf-8
"""Host-wide service addresses that Provider Hub plugins can be handed.

One address per capability, kept in the ``endpoints`` settings section under
``<capability, dots as underscores>_url``. FlareSolverr is the first one: a
plugin that can solve its Cloudflare challenges through FlareSolverr reads its
own ``flaresolverr_url``, and when that is empty the host fills it from here.

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
FLARESOLVERR_SIBLING_URL = "http://flaresolverr:8191"
SIBLING_PROBE_TIMEOUT_SECONDS = 2.0
# The whole attempt, name lookup included. The request timeout alone does not
# bound the lookup.
SIBLING_PROBE_DEADLINE_SECONDS = 3.0
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
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    return text


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
    declares ``"flaresolverr": true``, never to an untrusted plugin that merely
    has a field of the right name.
    """
    if getattr(installation, "trusted", False) is True:
        return True
    manifest = getattr(installation, "manifest", None)
    return isinstance(manifest, dict) and manifest.get("flaresolverr") is True


def fill_flaresolverr_url(config: dict, properties: dict, installation, settings) -> None:
    """Give an empty ``flaresolverr_url`` of an eligible plugin the global address.

    Runs after the stored config, the schema defaults and the plugin's own
    settings section are merged, so a value the user set for the provider is
    never replaced. With no address to give, the field is set to an empty
    string so that a pool holding an older address drops it. A plugin whose
    schema has no such field is left as it is.
    """
    if FLARESOLVERR_PROVIDER_FIELD not in properties:
        return
    if config.get(FLARESOLVERR_PROVIDER_FIELD) not in (None, "", [], {}):
        return
    if not may_receive_flaresolverr(installation):
        return
    # Assigned even when there is no address. A persistent pool merges the new
    # config over the one it holds, so a key that is simply absent would keep
    # the address filled in before the setting was cleared. An explicit empty
    # string is what replaces it.
    config[FLARESOLVERR_PROVIDER_FIELD] = resolve_flaresolverr_url(settings)


def _probe_flaresolverr(url: str) -> bool:
    import requests

    try:
        with requests.Session() as session:
            # A container name is looked up directly: a proxy set for the
            # environment must not decide whether the sibling answers.
            session.trust_env = False
            response = session.get(url, timeout=SIBLING_PROBE_TIMEOUT_SECONDS, allow_redirects=False)
        if response.status_code != 200:
            return False
        payload = response.json()
    except Exception:
        return False
    return isinstance(payload, dict) and "flaresolverr" in str(payload.get("msg", "")).casefold()


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
            found = bool(_probe_flaresolverr(FLARESOLVERR_SIBLING_URL))
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
    ``detecting`` is true while nothing has been learned yet and while a refresh
    of a stale answer is running, because the page polls only while it is true.
    """
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
        url = _sibling_cache["url"]
    fresh = checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS
    if not fresh:
        schedule_sibling_refresh()
    with _sibling_lock:
        flight = _probe_flight
        refreshing = flight is not None and not flight["recorded"]
    return url, checked_at is None or (not fresh and refreshing)


def start_flaresolverr_hint_probe() -> None:
    """Probe the sibling once in the background so the settings page has a hint ready."""
    schedule_sibling_refresh()


def flaresolverr_status(settings=None, refresh: bool = False) -> dict:
    """What the Settings page shows: the effective address, its source and the hint.

    Without ``refresh`` this answers from the cache and never waits on the
    network. With ``refresh`` (an explicit Detect) it probes within the deadline.
    """
    if refresh:
        detected, detecting = detect_flaresolverr_sibling(refresh=True), False
    else:
        detected, detecting = sibling_hint()
    # Read after the probe: a Detect can wait seconds, and the setting may have
    # been saved meanwhile. The hint is compared with what is in use now.
    url, source = flaresolverr_url_source(settings)
    return {
        "url": url,
        "source": source,
        # Once an address is in use there is nothing to accept.
        "detected": detected if detected and detected != url else None,
        "detecting": detecting,
        # Whether the sibling answered at all, even when it is the address
        # already in use and so has nothing left to offer.
        "sibling_found": detected is not None,
    }
