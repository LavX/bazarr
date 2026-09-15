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

from app.config import settings
from media_servers.http import MediaServerError
from .client import SeerrClient

logger = logging.getLogger(__name__)

_STATUS = {1: "unknown", 2: "pending", 3: "processing", 4: "partially_available", 5: "available"}
_REQUEST_STATUS = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
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
            return candidate.strip().rstrip("/")
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
    result["request"] = non_4k[-1] if non_4k else None
    open_non_4k = any(r["status"] in _OPEN_REQUEST for r in non_4k)
    open_4k = any(r["status"] in _OPEN_REQUEST for r in requests if r["is4k"])
    lane_4k = cap["movie_4k"] if media_type == "movie" else cap["series_4k"]
    if media_type == "movie":
        result["requestable"] = result["status"] in ("unknown", "deleted") and not open_non_4k
        result["requestable_4k"] = lane_4k and result["status_4k"] in ("unknown", "deleted") and not open_4k
        return result
    # Shows: only blocklisted hides everything; every other state leaves the
    # remaining seasons requestable. Every season Seerr already knows about
    # is reported: taken (available) or not yet requested (requestable), and
    # an open request bumps a season to requested unless it is already available.
    taken = {}
    for season in (info.get("seasons") if known else None) or []:
        if isinstance(season, dict) and type(season.get("seasonNumber")) is int:
            state = "available" if season.get("status") not in (None, 1) else "requestable"
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


def request_outcome(status, body, cap) -> dict:
    message = body.get("message") if isinstance(body, dict) else None
    if status == 201 and isinstance(body, dict):
        rows = _requests({"requests": [body]})
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
