# coding=utf-8

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# Jellyfin item / series / library / episode IDs are 32-char lowercase hex
# (UUIDs without dashes) in modern builds, with a small population of dash-
# separated UUIDs in older installs. Anything else (slashes, dots, querystring
# characters) means a malicious or corrupt response and must not be substituted
# into a URL path.
_JELLYFIN_ID_RE = re.compile(r'\A[0-9a-fA-F]{32}\Z|\A[0-9a-fA-F\-]{36}\Z')

# Cap response size to defend against a hostile / MITM'd Jellyfin returning
# pathologically large payloads. 16 MiB is generous for the largest Items
# response we expect (a multi-thousand-item library listing) and small enough
# to keep memory bounded on resource-constrained NAS deployments.
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# Split connect/read timeouts so a wedged Jellyfin doesn't hold the subtitle
# workflow for a full 30s; 5s to TCP-handshake, 15s to wait for the response
# body. Tunes to user expectations from sonarr/radarr.
_DEFAULT_TIMEOUT = (5, 15)


def _validate_id(item_id: str) -> str:
    """Reject IDs that aren't shaped like a Jellyfin GUID before substituting
    them into a URL path. Defends against malicious Jellyfin responses with
    slashes / dots / querystring metacharacters."""
    if not isinstance(item_id, str) or not _JELLYFIN_ID_RE.match(item_id):
        raise ValueError(f"refusing unsafe Jellyfin id {item_id!r}")
    return item_id


def _redact_secret(text: str, secret: str) -> str:
    """Strip the configured api_key (and any `Token="..."` substring) from
    log/error text so accidental echoing of headers or URLs cannot leak the
    credential."""
    if not text:
        return text
    if isinstance(secret, str) and secret:
        text = text.replace(secret, '***')
    text = re.sub(r'Token="[^"]*"', 'Token="***"', text)
    return text


def _bounded_body(response: requests.Response) -> bytes:
    """Read up to _MAX_RESPONSE_BYTES from the response, raising if the
    server tries to send more. Mirrors how the compat endpoint guards
    streaming downloads."""
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError(
                f"Jellyfin response exceeded {_MAX_RESPONSE_BYTES} bytes"
            )
        chunks.append(chunk)
    return b''.join(chunks)


class JellyfinClient:
    """Thin HTTP client for the Jellyfin REST API."""

    def __init__(self, url: str, api_key: str, verify_ssl: bool | None = None):
        self.base_url = url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        # Default to verifying TLS. Users with self-signed certs flip the
        # opt-out validator (`jellyfin.verify_ssl=false`). Never silently
        # disable. Settings read happens lazily so importing this module
        # does not require the bazarr config to be loaded.
        if verify_ssl is None:
            try:
                from app.config import settings
                verify_ssl = bool(settings.jellyfin.verify_ssl)
            except Exception:
                verify_ssl = True
        self.session.verify = verify_ssl

        bazarr_version = os.environ.get('BAZARR_VERSION', 'unknown')
        self.session.headers.update({
            'Authorization': (
                f'MediaBrowser Client="Bazarr", Device="Bazarr", '
                f'DeviceId="bazarr", Version="{bazarr_version}", '
                f'Token="{api_key}"'
            ),
            'Content-Type': 'application/json',
        })

    def _url(self, path: str) -> str:
        return f'{self.base_url}{path}'

    def get(self, path: str, params: dict = None) -> requests.Response:
        response = self.session.get(self._url(path), params=params,
                                    timeout=_DEFAULT_TIMEOUT, stream=True)
        # `stream=True` keeps the underlying connection open until the body
        # is fully consumed. If raise_for_status() (4xx/5xx) or _bounded_body
        # (>16 MiB hostile payload) raises, the connection would leak back
        # into the pool half-read; close it explicitly so repeated failures
        # cannot exhaust file descriptors / pool slots.
        try:
            response.raise_for_status()
            # Pre-load the bounded body so callers' .json() / .text reads do
            # not exceed the cap.
            response._content = _bounded_body(response)
        except Exception:
            response.close()
            raise
        return response

    def post(self, path: str, json: dict = None, params: dict = None) -> requests.Response:
        response = self.session.post(self._url(path), json=json, params=params,
                                     timeout=_DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response

    def get_system_info(self) -> dict:
        """GET /System/Info — returns server name, version, id."""
        return self.get('/System/Info').json()

    def get_libraries(self) -> list:
        """GET /Library/VirtualFolders — returns all library folders."""
        return self.get('/Library/VirtualFolders').json()

    def get_items(self, params: dict) -> list:
        """GET /Items — search/browse items with query parameters."""
        data = self.get('/Items', params=params).json()
        return data.get('Items', [])

    def get_episodes(self, series_id: str, season: int) -> list:
        """GET /Shows/{seriesId}/Episodes — list episodes for a season."""
        data = self.get(f'/Shows/{_validate_id(series_id)}/Episodes', params={
            'season': season,
            'fields': 'ProviderIds',
        }).json()
        return data.get('Items', [])

    def refresh_item(self, item_id: str) -> None:
        """POST /Items/{itemId}/Refresh — trigger metadata refresh for a specific item."""
        self.post(f'/Items/{_validate_id(item_id)}/Refresh', params={
            'metadataRefreshMode': 'ValidationOnly',
            'imageRefreshMode': 'None',
            'replaceAllMetadata': 'false',
            'replaceAllImages': 'false',
        })

    def report_media_updated(self, path: str) -> None:
        """POST /Library/Media/Updated — notify Jellyfin of filesystem changes.

        Triggers filesystem change detection for the given path.
        """
        self.post('/Library/Media/Updated', json={
            'Updates': [{'Path': path, 'UpdateType': 'Modified'}],
        })
