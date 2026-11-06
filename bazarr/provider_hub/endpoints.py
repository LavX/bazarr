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
SIBLING_CACHE_SECONDS = 300.0

_sibling_lock = threading.Lock()
_sibling_cache: dict = {"checked_at": None, "url": None}


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


def detect_flaresolverr_sibling(refresh: bool = False) -> str | None:
    """The sibling FlareSolverr address when one answers, else ``None``.

    Bounded by a short timeout and cached, so repeated reads of the settings page
    do not probe every time. This only reports: it never writes the setting.
    """
    with _sibling_lock:
        checked_at = _sibling_cache["checked_at"]
        if not refresh and checked_at is not None and time.monotonic() - checked_at < SIBLING_CACHE_SECONDS:
            return _sibling_cache["url"]
    found = FLARESOLVERR_SIBLING_URL if _probe_flaresolverr(FLARESOLVERR_SIBLING_URL) else None
    with _sibling_lock:
        _sibling_cache["checked_at"] = time.monotonic()
        _sibling_cache["url"] = found
    return found


def start_flaresolverr_hint_probe() -> None:
    """Probe the sibling once in the background so the settings page has a hint ready."""
    def run():
        try:
            detect_flaresolverr_sibling(refresh=True)
        except Exception:
            logger.debug("FlareSolverr sibling probe failed", exc_info=True)

    threading.Thread(target=run, name="flaresolverr-hint", daemon=True).start()


def flaresolverr_status(settings=None, refresh: bool = False) -> dict:
    """What the Settings page shows: the effective address, its source and the hint."""
    url, source = flaresolverr_url_source(settings)
    detected = detect_flaresolverr_sibling(refresh=refresh)
    # Once an address is in use there is nothing to accept.
    return {
        "url": url,
        "source": source,
        "detected": detected if detected and detected != url else None,
    }
