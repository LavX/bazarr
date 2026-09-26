# coding=utf-8
"""Turn Seerr answers into the small vocabulary the Discover page renders.

Facts this rests on (verified against source per tag, not the OpenAPI spec):
MediaStatus 1..5 is stable; 6 means DELETED on Overseerr and BLOCKLISTED on
Jellyseerr and Seerr, which also use 7 for DELETED. The version string cannot
tell flavours apart (develop images report "develop-<sha>"), but the
/settings/public field set can. Request status is 1 pending, 2 approved,
3 declined, 4 failed, 5 completed.
"""

import logging
import time

from app.config import settings
from media_servers.http import MediaServerError
from utilities.locked_lru import LockedLRU
from .client import SeerrClient

logger = logging.getLogger(__name__)

_STATUS = {1: "unknown", 2: "pending", 3: "processing", 4: "partially_available", 5: "available"}
_REQUEST_STATUS = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
# Failed stays open on purpose: Seerr refuses a new movie request (409) and
# drops the seasons of a show request while a failed one exists, and offers
# its own Retry instead. Only declined and completed requests free the title.
_OPEN_REQUEST = {"pending", "approved", "failed"}

ADMIN, REQUEST, REQUEST_4K = 2, 32, 1024
REQUEST_4K_MOVIE, REQUEST_4K_TV, REQUEST_MOVIE, REQUEST_TV = 2048, 4096, 262144, 524288


def _flag(public, key, default):
    value = public.get(key)
    return value if isinstance(value, bool) else default


def capability(public: dict) -> dict:
    """Every field is best effort: Seerr does not validate responses."""
    public = public if isinstance(public, dict) else {}
    blocklist = any(key in public for key in ("hideBlocklisted", "mediaServerType", "enableSpecialEpisodes"))
    url = public.get("applicationUrl")
    title = public.get("applicationTitle")
    return {"blocklist": blocklist,
            "movie_4k": _flag(public, "movie4kEnabled", False),
            "series_4k": _flag(public, "series4kEnabled", False),
            "partial_requests": _flag(public, "partialRequestsEnabled", True),
            "special_episodes": _flag(public, "enableSpecialEpisodes", False),
            "application_url": url.strip() if isinstance(url, str) else "",
            "application_title": title.strip() if isinstance(title, str) else ""}


def link_base(cap: dict) -> str:
    for candidate in (settings.seerr.external_url, cap.get("application_url"), settings.seerr.url):
        if isinstance(candidate, str) and candidate.strip():
            candidate = candidate.strip().rstrip("/")
            if candidate.lower().startswith(("http://", "https://")):
                return candidate
    return ""


def user_capabilities(permissions) -> dict:
    bits = permissions if type(permissions) is int else 0

    def has(*needed):
        return bool(bits & ADMIN) or any(bits & bit for bit in needed)

    return {"can_request_movie": has(REQUEST, REQUEST_MOVIE), "can_request_tv": has(REQUEST, REQUEST_TV),
            "can_request_4k_movie": has(REQUEST_4K, REQUEST_4K_MOVIE), "can_request_4k_tv": has(REQUEST_4K, REQUEST_4K_TV)}


def _decode_status(code, cap):
    if code in _STATUS:
        return _STATUS[code]
    if code == 6:
        return "blocklisted" if cap["blocklist"] else "deleted"
    if code == 7 and cap["blocklist"]:
        return "deleted"
    return "unknown"


def _requests(media_info):
    rows = media_info.get("requests") if isinstance(media_info, dict) else None
    result = []
    for row in rows or []:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            continue
        seasons = [s["seasonNumber"] for s in row.get("seasons") or []
                   if isinstance(s, dict) and type(s.get("seasonNumber")) is int]
        result.append({"id": row["id"], "status": _REQUEST_STATUS.get(row.get("status"), "pending"),
                       "is4k": row.get("is4k") is True, "seasons": seasons})
    return result


def normalize_media(media_type, tmdb_id, status, body, cap, base_link) -> dict:
    link = f"{base_link}/{media_type}/{tmdb_id}" if base_link else None
    result = {"known": False, "status": "unknown", "status_4k": "unknown", "request": None, "seasons": [],
              "requestable": False, "requestable_4k": False, "partial_requests": cap["partial_requests"],
              "special_episodes": cap["special_episodes"], "link": link}
    if status != 200 or not isinstance(body, dict):
        raise MediaServerError("upstream_error")
    info = body.get("mediaInfo")
    known = isinstance(info, dict)
    result["known"] = known
    if known:
        result["status"] = _decode_status(info.get("status"), cap)
        result["status_4k"] = _decode_status(info.get("status4k"), cap)
    requests = _requests(info) if known else []
    non_4k = [r for r in requests if not r["is4k"]]
    result["request"] = max(non_4k, key=lambda row: row["id"]) if non_4k else None
    open_non_4k = any(r["status"] in _OPEN_REQUEST for r in non_4k)
    open_4k = any(r["status"] in _OPEN_REQUEST for r in requests if r["is4k"])
    lane_4k = cap["movie_4k"] if media_type == "movie" else cap["series_4k"]
    if media_type == "movie":
        result["requestable"] = result["status"] in ("unknown", "deleted") and not open_non_4k
        result["requestable_4k"] = lane_4k and result["status_4k"] in ("unknown", "deleted") and not open_4k
        return result
    # Shows: only blocklisted hides everything; every other state leaves the
    # remaining seasons requestable. Every season Seerr already knows about
    # is reported: available only at status 5, in flight (requested) at 2/3/4,
    # otherwise requestable; an open request bumps a season to requested
    # unless it is already available, and never demotes an available season.
    taken = {}
    for season in (info.get("seasons") if known else None) or []:
        if isinstance(season, dict) and type(season.get("seasonNumber")) is int:
            season_status = season.get("status")
            if season_status == 5:
                state = "available"
            elif season_status in (2, 3, 4):
                state = "requested"
            else:
                state = "requestable"
            taken[season["seasonNumber"]] = state
    for request in non_4k:
        if request["status"] in _OPEN_REQUEST:
            for number in request["seasons"]:
                if taken.get(number) != "available":
                    taken[number] = "requested"
    result["seasons"] = [{"number": number, "state": state} for number, state in sorted(taken.items())]
    result["requestable"] = result["status"] != "blocklisted"
    result["requestable_4k"] = lane_4k and result["status_4k"] != "blocklisted"
    return result


def request_outcome(status, body) -> dict:
    message = body.get("message") if isinstance(body, dict) else None
    if status == 201:
        # Seerr created the request either way: a body we cannot read is not
        # a reason to report failure and send the user into a retry that then
        # reads back as already_requested.
        rows = _requests({"requests": [body]}) if isinstance(body, dict) else []
        return {"outcome": "requested", "request": rows[0] if rows else None}
    if status == 202:
        return {"outcome": "nothing_to_request"}
    if status == 409:
        return {"outcome": "already_requested"}
    if status == 403:
        if body is None:
            return {"error_code": "csrf_blocked"}
        if isinstance(body, dict) and "error" in body and "message" not in body:
            return {"error_code": "rejected_key"}
        text = (message or "").lower()
        if "quota" in text:
            return {"error_code": "quota"}
        if "blocklisted" in text or "blacklisted" in text:
            return {"error_code": "blocklisted"}
        return {"error_code": "permission"}
    if status == 400:
        return {"error_code": "validation"}
    return {"error_code": "upstream_error"}


def error_code_for(error: MediaServerError) -> str:
    """Map a transport failure to the small vocabulary the API responds with."""
    return "upstream_error" if error.code in ("upstream_error", "invalid_response", "server_error") else "unreachable"


# A hit is stable for an hour; a miss expires far sooner, because a show TMDB
# has not mapped to a TVDB id yet must not stay unrequestable for the lifetime
# of the process. Both sit behind the same LockedLRU the Discover metadata
# caches use, since Waitress serves these routes from many threads.
_FIND_TTL = 3600
_FIND_MISS_TTL = 300
_find_cache = LockedLRU(maxsize=256)

# Capability comes from /settings/public, which every media view and every
# request needs. The key carries only the Bazarr-side settings, so a change
# made inside Seerr (a 4K lane, partial requests, the application URL) is
# retired by the TTL alone: that is what the TTL is for, and why it is minutes
# rather than hours.
_CAPABILITY_TTL = 300
_capability_cache = LockedLRU(maxsize=4)
# Fields only Seerr's own /settings/public carries. An answer with none of
# them is not Seerr's, whatever its status code.
_PUBLIC_MARKERS = ("hideBlocklisted", "mediaServerType", "enableSpecialEpisodes", "movie4kEnabled",
                   "series4kEnabled", "partialRequestsEnabled", "applicationTitle", "applicationUrl")


def _server_key():
    """Identity of the configured server: any settings change retires its cache entries."""
    return (settings.seerr.url, settings.seerr.apikey, bool(settings.seerr.verify_ssl))


def cached_capability(client: SeerrClient) -> dict:
    """Capability for the configured server, re-probed at most every few minutes."""
    key = _server_key()
    cached = _capability_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    public = client.public_settings()
    cap = capability(public)
    # Only a recognisable answer is worth keeping. Some other JSON served with
    # a 200, which is what an auth proxy's login payload looks like, yields
    # all-defaults: caching that would hide both 4K lanes and read status 6 as
    # deleted rather than blocklisted, for every title, until the TTL expired.
    if isinstance(public, dict) and any(marker in public for marker in _PUBLIC_MARKERS):
        _capability_cache[key] = (time.monotonic() + _CAPABILITY_TTL, cap)
    return cap


def reset_caches() -> None:
    """Drop every cached probe.

    Settings changes already retire entries structurally, because the keys
    carry the server identity and the metadata revision. This exists for tests,
    which reuse one process across several fake servers.
    """
    _capability_cache.clear()
    _find_cache.clear()


def tmdb_id_for_tvdb(tvdb_id) -> int | None:
    """TMDB id for a show Bazarr knows only by TVDB id, through TMDB's find endpoint."""
    from discover import metadata
    if type(tvdb_id) is not int or tvdb_id <= 0:
        return None
    try:
        config = metadata.configuration()
        # Keyed on the metadata revision, which changes when the effective TMDB
        # credential or locale does, so a reader who fixes a broken key is not
        # held to the answer it produced.
        key = (config.revision, tvdb_id)
        cached = _find_cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        raw = metadata._request(config, f"/find/{tvdb_id}", {"external_source": "tvdb_id"})
        rows = raw.get("tv_results") if isinstance(raw, dict) else None
        found = rows[0].get("id") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        result = found if type(found) is int and found > 0 else None
    except metadata.UpstreamFailure:
        # TMDB itself refused or is in its own cooldown, which that layer has
        # already reported. A rate limit holds for as long as Retry-After says,
        # so logging each view at WARNING with a traceback would fill the log
        # with the same frame for hours.
        logger.debug("TMDB declined a find for TVDB id %s", tvdb_id)
        return None
    except Exception:
        # Anything else is unexpected, and nothing else names it: the symptom
        # on its own is every TVDB-only show reporting unresolved.
        logger.warning("TMDB find failed for TVDB id %s", tvdb_id, exc_info=True)
        return None
    _find_cache[key] = (time.monotonic() + (_FIND_TTL if result else _FIND_MISS_TTL), result)
    return result


def get_seerr_client() -> SeerrClient:
    url, apikey = settings.seerr.url, settings.seerr.apikey
    if not url or not apikey:
        raise ValueError("Seerr is not configured.")
    return SeerrClient(url, apikey, verify_ssl=bool(settings.seerr.verify_ssl))


def test_connection(url, apikey, verify_ssl) -> dict:
    try:
        with SeerrClient(url or "", apikey or "", verify_ssl=verify_ssl if verify_ssl is not None else True) as client:
            version = client.status().get("version")
            cap = capability(client.public_settings())
            status, me = client.me()
            if status == 403:
                return {"success": False, "error_code": "rejected_key"}
            if status != 200 or not isinstance(me, dict):
                return {"success": False, "error_code": "connection_failed"}
            return {"success": True, "version": version if isinstance(version, str) else "",
                    "blocklist_capable": cap["blocklist"], "application_title": cap["application_title"],
                    "application_url": cap["application_url"], "movie_4k": cap["movie_4k"],
                    "series_4k": cap["series_4k"], "partial_requests": cap["partial_requests"],
                    "special_episodes": cap["special_episodes"],
                    "acting_user": {"id": me.get("id"), "display_name": me.get("displayName") or "",
                                    **user_capabilities(me.get("permissions"))}}
    except MediaServerError as error:
        if error.code == "missing_credentials" or error.code == "invalid_url":
            return {"success": False, "error_code": "configuration"}
        logger.debug("Seerr connection test failed: %s", error.code)
        return {"success": False, "error_code": "connection_failed"}
    except Exception:
        logger.debug("Seerr connection test failed", exc_info=False)
        return {"success": False, "error_code": "connection_failed"}
