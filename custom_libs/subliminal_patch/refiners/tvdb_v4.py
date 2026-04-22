"""TVDB v4 minimal client, shipped at subliminal_patch level so any
Bazarr code path can use it without reaching into a compat-specific
module.

Exists because subliminal (even latest 2.6.0 upstream) still uses TVDB
v1 via a hardcoded public key, and v1's search_series endpoint only
indexes series by imdb_id - it returns nothing for an episode's own
imdb_id. OS.com-compat clients routinely send the episode's imdb_id,
so we need v4's /search/remoteid/{imdb} endpoint which natively
resolves episodes, series, and movies from any remote id.

A public project-tier TVDB v4 key is baked in. No user configuration
required (or exposed) - this is a maintainer-managed detail.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Project-tier TVDB v4 API key negotiated for Bazarr+.
_APIKEY = "9761986e-3402-4658-bb1c-3ac289c50093"
_BASE_URL = "https://api4.thetvdb.com/v4"
# TVDB v4 tokens are valid for ~30 days; refresh at 28 to be safe.
_TOKEN_TTL_SECONDS = 28 * 86400


class TVDBv4Client:
    """Thread-safe TVDB v4 client with lazy JWT refresh.

    Public surface is intentionally tiny - just search_by_imdb_id(). The
    refine path uses the returned series_tvdb_id to hydrate via
    subliminal's existing v1 get_series() for any fields v4's remoteid
    search didn't include.
    """

    def __init__(self):
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()

    def _login(self) -> Optional[str]:
        try:
            r = self._session.post(
                f"{_BASE_URL}/login",
                json={"apikey": _APIKEY},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning("TVDB v4 login failed: HTTP %s", r.status_code)
                return None
            body = r.json()
            token = (body.get("data") or {}).get("token")
            if not token:
                return None
            self._token = token
            self._token_exp = time.time() + _TOKEN_TTL_SECONDS
            return token
        except Exception as e:
            logger.warning("TVDB v4 login error: %s", e)
            return None

    def _ensure_token(self) -> Optional[str]:
        with self._lock:
            if self._token and time.time() < self._token_exp:
                return self._token
            return self._login()

    def search_by_imdb_id(self, imdb_id) -> Optional[dict]:
        """Return the first /search/remoteid/{imdb} match, or None.

        The match is a dict with either a 'series', 'episode', or
        'movie' sub-object depending on what the imdb_id maps to. Not
        every imdb_id is indexed on TVDB - missing entries return None,
        never raise.
        """
        token = self._ensure_token()
        if not token:
            return None
        if imdb_id is None:
            return None
        s = str(imdb_id).strip()
        if not s:
            return None
        if not s.startswith("tt"):
            s = f"tt{s}"
        try:
            r = self._session.get(
                f"{_BASE_URL}/search/remoteid/{s}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if r.status_code != 200:
                logger.debug("TVDB v4 remoteid %s -> HTTP %s", s, r.status_code)
                return None
            items = (r.json().get("data") or [])
            return items[0] if items else None
        except Exception as e:
            logger.debug("TVDB v4 remoteid %s error: %s", s, e)
            return None


_singleton: Optional[TVDBv4Client] = None
_singleton_lock = threading.Lock()


def get_client() -> TVDBv4Client:
    """Process-wide lazy singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = TVDBv4Client()
        return _singleton


def reset_client() -> None:
    """Drop the singleton. Used by tests."""
    global _singleton
    with _singleton_lock:
        _singleton = None
