# coding=utf-8
"""Native Emby movie and episode refresh across the shared resolution ladder."""

from urllib.parse import quote
from typing import Callable

from media_servers import resolution
from media_servers.http import MediaServerError, MediaServerHTTP
from media_servers.paths import media_path_contains, media_paths_equal

# Emby stores both libraries as items refreshed the same way. Only the type it
# is asked to resolve differs, and an unlisted media type never becomes one.
_ITEM_TYPES = {"movie": "Movie", "episode": "Episode"}
# An episode carries no identifier and no title of its own, so the identifier
# and title rungs resolve its series and then descend to the file.
_PARENT_TYPES = {"movie": "Movie", "episode": "Series"}
_COLLECTION_TYPES = {"movie": "movies", "episode": "tvshows"}


def _accepted_items(result, item_type):
    """The items of one type from a lookup, or nothing this can vouch for."""
    if not isinstance(result, dict) or not isinstance(result.get("Items"), list):
        raise MediaServerError("invalid_response")
    items = result["Items"]
    total = result.get("TotalRecordCount", len(items))
    if type(total) is not int or total != len(items):
        # A partial response cannot establish unique item ownership.
        raise MediaServerError("invalid_response")
    accepted = []
    for item in items:
        if not isinstance(item, dict):
            raise MediaServerError("invalid_response")
        if item.get("Type") == item_type:
            accepted.append(item)
    return accepted


def _encoded_id(item, key="Id"):
    """The item's own opaque id, encoded as exactly one path segment."""
    item_id = item.get(key) if isinstance(item, dict) else None
    if not isinstance(item_id, str) or not item_id or item_id in (".", ".."):
        raise MediaServerError("invalid_response")
    try:
        return quote(item_id, safe='')
    except UnicodeError:
        raise MediaServerError("invalid_response") from None


class EmbyClient:
    # Emby answers every rung: /Items filters on AnyProviderIdEquals and on
    # NameStartsWith plus Years, resolves an exact Path, and /Library/VirtualFolders
    # names the library item to rescan when the file itself is not indexed yet.
    REFRESH_STEPS = resolution.CHAIN

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

    def _refresh(self, encoded_id, ensure_current, *, recursive=False) -> dict:
        if ensure_current:
            ensure_current()
        self.http.request_empty("POST", f"/Items/{encoded_id}/Refresh", params={
            "Recursive": "true" if recursive else "false",
            "MetadataRefreshMode": "ValidationOnly", "ImageRefreshMode": "ValidationOnly",
            "ReplaceAllMetadata": "false", "ReplaceAllImages": "false",
        }, json={"ReplaceThumbnailImages": False}, success_statuses=(200, 204))
        if ensure_current:
            ensure_current()
        return {"status": "requested"}

    def _find_parent(self, media_type, params, ensure_current, *, name=None):
        """One movie or series, or nothing. A tie is never broken by guessing."""
        item_type = _PARENT_TYPES[media_type]
        if ensure_current:
            ensure_current()
        items = _accepted_items(self.http.request_json("GET", "/Items", params={
            "Recursive": "true", "IncludeItemTypes": item_type,
            "Fields": "Path,ProviderIds", **params}), item_type)
        if name is not None:
            items = [item for item in items
                     if isinstance(item.get("Name"), str) and item["Name"].lower() == name.lower()]
        return items[0] if len(items) == 1 else None

    def _find_episode(self, series, season, episode, ensure_current):
        if ensure_current:
            ensure_current()
        items = _accepted_items(self.http.request_json(
            "GET", f"/Shows/{_encoded_id(series)}/Episodes",
            params={"Season": str(season), "Fields": "Path"}), "Episode")
        matches = [item for item in items
                   if type(item.get("IndexNumber")) is int and item["IndexNumber"] == episode
                   and type(item.get("ParentIndexNumber")) is int and item["ParentIndexNumber"] == season]
        return matches[0] if len(matches) == 1 else None

    def _refresh_resolved(self, media_type, parent, metadata, ensure_current):
        if parent is None:
            return None
        if media_type == "movie":
            return self._refresh(_encoded_id(parent), ensure_current)
        episode = self._find_episode(parent, metadata.season, metadata.episode, ensure_current)
        return None if episode is None else self._refresh(_encoded_id(episode), ensure_current)

    def refresh_by_provider_id(self, media_type, metadata, *,
                               ensure_current: Callable[[], None] | None = None) -> dict | None:
        """Resolve the item by IMDB, then TMDB or TVDB, in Jellyfin's order.

        Identifiers are asked one at a time rather than as the comma-delimited
        list Emby also accepts, because a combined query answers "one of these
        matched" and loses which one, and precedence is the whole point of
        trying IMDB first.
        """
        if media_type not in _PARENT_TYPES:
            raise MediaServerError("internal_error")
        if not metadata.locatable(media_type):
            return None
        for provider, value in metadata.provider_ids(media_type):
            parent = self._find_parent(media_type, {"AnyProviderIdEquals": f"{provider}.{value}"}, ensure_current)
            result = self._refresh_resolved(media_type, parent, metadata, ensure_current)
            if result is not None:
                return result
        return None

    def refresh_by_title_year(self, media_type, metadata, *,
                              ensure_current: Callable[[], None] | None = None) -> dict | None:
        """Resolve the item by an exact title, narrowed by production year.

        NameStartsWith is a prefix filter, so it bounds the response rather
        than deciding anything; the name still has to match exactly, case
        insensitively, the way Jellyfin's title fallback matches.
        """
        if media_type not in _PARENT_TYPES:
            raise MediaServerError("internal_error")
        if not metadata.title or not metadata.locatable(media_type):
            return None
        params = {"NameStartsWith": metadata.title}
        if metadata.year:
            params["Years"] = str(metadata.year)
        parent = self._find_parent(media_type, params, ensure_current, name=metadata.title)
        return self._refresh_resolved(media_type, parent, metadata, ensure_current)

    def refresh_item(self, media_type: str, video_path: str, *,
                     ensure_current: Callable[[], None] | None = None) -> dict:
        item_type = _ITEM_TYPES.get(media_type) if isinstance(media_type, str) else None
        if item_type is None:
            raise MediaServerError("internal_error")
        if not media_paths_equal(video_path, video_path):
            raise MediaServerError("path_invalid")
        if ensure_current:
            ensure_current()
        items = _accepted_items(self.http.request_json("GET", "/Items", params={
            "Path": video_path, "IncludeItemTypes": item_type, "Recursive": "true",
            "Fields": "Path,ProviderIds,MediaStreams,MediaSources",
        }), item_type)
        matches = []
        for item in items:
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
        return self._refresh(_encoded_id(matches[0]), ensure_current)

    def refresh_library(self, media_type: str, video_path: str, *,
                        ensure_current: Callable[[], None] | None = None) -> dict | None:
        """Rescan the library holding this file, when the file itself is unknown.

        Scoped to the library the path mapping points into, not to every
        library of that type: a library scan is expensive, and Bazarr has no
        setting through which a user sanctioned scanning the rest of the
        server. A path in no library leaves nothing sensible to refresh.
        """
        collection_type = _COLLECTION_TYPES.get(media_type) if isinstance(media_type, str) else None
        if collection_type is None:
            raise MediaServerError("internal_error")
        if not media_paths_equal(video_path, video_path):
            raise MediaServerError("path_invalid")
        if ensure_current:
            ensure_current()
        folders = self.http.request_json("GET", "/Library/VirtualFolders")
        if not isinstance(folders, list):
            raise MediaServerError("invalid_response")
        targets = []
        for folder in folders:
            if not isinstance(folder, dict):
                raise MediaServerError("invalid_response")
            locations = folder.get("Locations")
            if locations is None:
                locations = []
            if not isinstance(locations, list):
                raise MediaServerError("invalid_response")
            if (folder.get("CollectionType") or "").lower() != collection_type:
                continue
            if not any(media_path_contains(root, video_path) for root in locations):
                continue
            targets.append(_encoded_id(folder, "ItemId"))
        if not targets:
            return None
        for encoded_id in targets:
            self._refresh(encoded_id, ensure_current, recursive=True)
        return {"status": "requested"}
