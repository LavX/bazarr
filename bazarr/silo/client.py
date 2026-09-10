# coding=utf-8
"""Native Silo authentication, library selection and observed file scans."""

from media_servers import resolution
from media_servers.http import MediaServerError, MediaServerHTTP
from typing import Callable

from .scans import refresh_file, refresh_library


class SiloClient:
    # No identifier rungs, and not for want of asking: Silo's Jellyfin
    # compatibility layer (10.12.0) answers /Items with no ProviderIds key on
    # any item, so there is nothing a provider-id or title match could compare
    # against. Bazarr talks to the native API, which addresses media by path
    # and library, so the path is the first rung Silo can actually climb.
    REFRESH_STEPS = (resolution.PATH, resolution.LIBRARY)

    def __init__(self, url: str, apikey: str, verify_ssl: bool = True):
        if not isinstance(apikey, str) or not apikey.strip() or any(ord(char) < 32 for char in apikey):
            raise MediaServerError("missing_credentials")
        self.http = MediaServerHTTP(url, verify_ssl=verify_ssl, headers={"Authorization": f"Bearer {apikey}"})

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def test_connection(self) -> dict:
        info = self.http.request_json("GET", "/api/v1/health")
        if (not isinstance(info, dict) or info.get("status") != "ok"
                or any(not isinstance(info.get(key), str) or not info[key] for key in ("server_id", "server_name"))):
            raise MediaServerError("invalid_response")
        # Health is public. Only the admin-only native libraries endpoint proves authentication.
        self.get_libraries()
        return {"success": True, "server_id": info["server_id"], "server_name": info["server_name"]}

    def get_libraries(self) -> list[dict]:
        rows = self.http.request_json("GET", "/api/v1/libraries")
        if not isinstance(rows, list):
            raise MediaServerError("invalid_response")
        result, seen = [], set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                    or row["id"] in seen or not isinstance(row.get("name"), str) or not row["name"]
                    or not isinstance(row.get("type"), str) or not isinstance(row.get("paths"), list)
                    or any(not isinstance(path, str) or not path for path in row["paths"])
                    or type(row.get("enabled")) is not bool):
                raise MediaServerError("invalid_response")
            seen.add(row["id"])
            if row["enabled"] and row["type"] in {"movies", "series"}:
                result.append({"id": str(row["id"]), "name": row["name"], "type": row["type"], "paths": row["paths"]})
        return result

    def refresh_file(self, library_id: str, video_path: str, *, timeout: float = 90.0,
                     ensure_current: Callable[[], None] | None = None) -> dict:
        return refresh_file(self.http, library_id, video_path, timeout=timeout, ensure_current=ensure_current)

    def refresh_library(self, library_id: str, *,
                        ensure_current: Callable[[], None] | None = None) -> dict:
        return refresh_library(self.http, library_id, ensure_current=ensure_current)
