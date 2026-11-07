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
_refresh_started_at: float | None = None


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
    never replaced. A plugin whose schema has no such field is left as it is.
    """
    if FLARESOLVERR_PROVIDER_FIELD not in properties:
        return
    if config.get(FLARESOLVERR_PROVIDER_FIELD) not in (None, "", [], {}):
        return
    if not may_receive_flaresolverr(installation):
        return
    url = resolve_flaresolverr_url(settings)
    if url:
        config[FLARESOLVERR_PROVIDER_FIELD] = url


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


def _probe_bounded(url: str) -> bool:
    """Probe with a deadline that covers the whole attempt, name lookup included.

    ``requests`` timeouts do not bound ``getaddrinfo``, and a slow search domain
    can hold a lookup for far longer than the request timeout. The probe runs in
    its own daemon thread and the caller stops waiting at the deadline, so a
    stuck lookup costs a parked thread, never a stalled caller.
    """
    outcome = {"found": False}
    done = threading.Event()

    def run():
        try:
            outcome["found"] = bool(_probe_flaresolverr(url))
        except Exception:
            outcome["found"] = False
        finally:
            done.set()

    threading.Thread(target=run, name="flaresolverr-probe", daemon=True).start()
    return bool(done.wait(SIBLING_PROBE_DEADLINE_SECONDS) and outcome["found"])


def _record_sibling(found: bool) -> str | None:
    url = FLARESOLVERR_SIBLING_URL if found else None
    with _sibling_lock:
        _sibling_cache["checked_at"] = time.monotonic()
        _sibling_cache["url"] = url
    return url


def detect_flaresolverr_sibling(refresh: bool = False) -> str | None:
    """Look for the sibling now, within the probe deadline, and cache the answer.

    Meant for an explicit Detect press and for the background refresh. A page
    load must use :func:`sibling_hint` instead, which never waits on a probe.
    This only reports: it never writes the setting.
    """
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
        if not refresh and checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS:
            return _sibling_cache["url"]
    return _record_sibling(_probe_bounded(FLARESOLVERR_SIBLING_URL))


def schedule_sibling_refresh() -> threading.Thread | None:
    """Refresh the cached answer in the background, one refresh at a time.

    Returns the thread that was started, or ``None`` when a refresh is already
    running. A refresh that outlives its deadline plus a margin no longer blocks
    the next one.
    """
    global _refresh_started_at
    with _sibling_lock:
        started = _refresh_started_at
        if started is not None and time.monotonic() - started < SIBLING_PROBE_DEADLINE_SECONDS + 5.0:
            return None
        _refresh_started_at = time.monotonic()

    def run():
        global _refresh_started_at
        try:
            detect_flaresolverr_sibling(refresh=True)
        except Exception:
            logger.debug("FlareSolverr sibling probe failed", exc_info=True)
        finally:
            with _sibling_lock:
                _refresh_started_at = None

    thread = threading.Thread(target=run, name="flaresolverr-hint", daemon=True)
    thread.start()
    return thread


def sibling_hint() -> tuple[str | None, bool]:
    """The cached sibling address and whether an answer is still being worked out.

    Never probes in the caller. A missing or stale answer starts a background
    refresh; until it lands the caller gets the last known address (or none) and
    ``detecting`` is true when nothing has been learned yet.
    """
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
        url = _sibling_cache["url"]
    fresh = checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS
    if not fresh:
        schedule_sibling_refresh()
    return url, checked_at is None


def start_flaresolverr_hint_probe() -> None:
    """Probe the sibling once in the background so the settings page has a hint ready."""
    schedule_sibling_refresh()


def flaresolverr_status(settings=None, refresh: bool = False) -> dict:
    """What the Settings page shows: the effective address, its source and the hint.

    Without ``refresh`` this answers from the cache and never waits on the
    network. With ``refresh`` (an explicit Detect) it probes within the deadline.
    """
    url, source = flaresolverr_url_source(settings)
    if refresh:
        detected, detecting = detect_flaresolverr_sibling(refresh=True), False
    else:
        detected, detecting = sibling_hint()
    return {
        "url": url,
        "source": source,
        # Once an address is in use there is nothing to accept.
        "detected": detected if detected and detected != url else None,
        "detecting": detecting,
    }
