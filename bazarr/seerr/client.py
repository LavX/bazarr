# coding=utf-8
"""Raw calls to the Seerr family API (Overseerr, Jellyseerr, Seerr).

Nothing here interprets an answer. The status is returned with the decoded
body so operations.py can tell a duplicate (409) from a refusal (403) and a
no-op (202), which the shared transport's success-only helpers cannot.
"""

import json
from typing import Optional

from media_servers.http import MediaServerError, MediaServerHTTP

_MEDIA_PATHS = {"movie": "/api/v1/movie/", "tv": "/api/v1/tv/"}


class SeerrClient:
    def __init__(self, url: str, apikey: str, verify_ssl: bool = True):
        if not isinstance(apikey, str) or not apikey.strip() or any(ord(char) < 32 for char in apikey):
            raise MediaServerError("missing_credentials")
        self.http = MediaServerHTTP(url, verify_ssl=verify_ssl, headers={"X-Api-Key": apikey}, timeout=(5, 15))

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _decoded(self, method, path, *, json_body=None) -> tuple[int, Optional[dict]]:
        status, body = self.http.request_result(method, path, json=json_body)
        try:
            decoded = json.loads(body) if body else None
        except (ValueError, UnicodeError, RecursionError):
            decoded = None
        return status, decoded if isinstance(decoded, dict) else None

    def status(self) -> dict:
        info = self.http.request_json("GET", "/api/v1/status")
        if not isinstance(info, dict):
            raise MediaServerError("invalid_response")
        return info

    def public_settings(self) -> dict:
        info = self.http.request_json("GET", "/api/v1/settings/public")
        if not isinstance(info, dict):
            raise MediaServerError("invalid_response")
        return info

    def me(self) -> tuple[int, Optional[dict]]:
        return self._decoded("GET", "/api/v1/auth/me")

    def media(self, media_type: str, tmdb_id: int) -> tuple[int, Optional[dict]]:
        prefix = _MEDIA_PATHS.get(media_type)
        if prefix is None or type(tmdb_id) is not int or tmdb_id <= 0:
            raise MediaServerError("internal_error")
        return self._decoded("GET", f"{prefix}{tmdb_id}")

    def create_request(self, body: dict) -> tuple[int, Optional[dict]]:
        return self._decoded("POST", "/api/v1/request", json_body=body)
