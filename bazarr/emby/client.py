# coding=utf-8
"""Native Emby movie and episode refresh with exact file-path resolution."""

from urllib.parse import quote
from typing import Callable

from media_servers.http import MediaServerError, MediaServerHTTP
from media_servers.paths import media_paths_equal

# Emby stores both libraries as items refreshed the same way. Only the type it
# is asked to resolve differs, and an unlisted media type never becomes one.
_ITEM_TYPES = {"movie": "Movie", "episode": "Episode"}


class EmbyClient:
    def __init__(self, url: str, apikey: str, verify_ssl: bool = True):
        if not isinstance(apikey, str) or not apikey.strip() or any(ord(char) < 32 for char in apikey):
            raise MediaServerError("missing_credentials")
        self.http = MediaServerHTTP(url, verify_ssl=verify_ssl, headers={"X-Emby-Token": apikey})

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def test_connection(self) -> dict:
        info = self.http.request_json("GET", "/System/Info")
        if (not isinstance(info, dict) or not isinstance(info.get("ServerName"), str)
                or not isinstance(info.get("Version"), str) or not info["Version"]):
            raise MediaServerError("invalid_response")
        return {"success": True, "server_name": info["ServerName"], "version": info["Version"]}

    def refresh_item(self, media_type: str, video_path: str, *,
                     ensure_current: Callable[[], None] | None = None) -> dict:
        item_type = _ITEM_TYPES.get(media_type) if isinstance(media_type, str) else None
        if item_type is None:
            raise MediaServerError("internal_error")
        if not media_paths_equal(video_path, video_path):
            raise MediaServerError("path_invalid")
        if ensure_current:
            ensure_current()
        result = self.http.request_json("GET", "/Items", params={
            "Path": video_path, "IncludeItemTypes": item_type, "Recursive": "true",
            "Fields": "Path,ProviderIds,MediaStreams,MediaSources",
        })
        if not isinstance(result, dict) or not isinstance(result.get("Items"), list):
            raise MediaServerError("invalid_response")
        items = result["Items"]
        total = result.get("TotalRecordCount", len(items))
        if type(total) is not int or total != len(items):
            # A partial response cannot establish unique item ownership.
            raise MediaServerError("invalid_response")
        matches = []
        for item in items:
            if not isinstance(item, dict):
                raise MediaServerError("invalid_response")
            if item.get("Type") != item_type:
                continue
            sources = item.get("MediaSources")
            if sources is None:
                sources = []
            if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
                raise MediaServerError("invalid_response")
            paths = [item.get("Path"), *(source.get("Path") for source in sources)]
            if any(media_paths_equal(path, video_path) for path in paths):
                matches.append(item)
        if not matches:
            raise MediaServerError("item_missing")
        if len(matches) != 1:
            raise MediaServerError("item_ambiguous")
        item_id = matches[0].get("Id")
        if not isinstance(item_id, str) or not item_id or item_id in (".", ".."):
            raise MediaServerError("invalid_response")
        try:
            encoded_id = quote(item_id, safe='')
        except UnicodeError:
            raise MediaServerError("invalid_response") from None
        if ensure_current:
            ensure_current()
        self.http.request_empty("POST", f"/Items/{encoded_id}/Refresh", params={
            "Recursive": "false", "MetadataRefreshMode": "ValidationOnly", "ImageRefreshMode": "ValidationOnly",
            "ReplaceAllMetadata": "false", "ReplaceAllImages": "false",
        }, json={"ReplaceThumbnailImages": False}, success_statuses=(200, 204))
        if ensure_current:
            ensure_current()
        return {"status": "requested"}
