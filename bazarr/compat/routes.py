from __future__ import annotations
from flask import Blueprint, request, jsonify, Response
from bazarr.compat.auth import compat_error

compat_stub_bp = Blueprint("compat_stub", __name__)


@compat_stub_bp.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@compat_stub_bp.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def _all_disabled(path):
    return compat_error("disabled", 404, "compat-disabled")


import datetime as _dt
from bazarr.compat import auth, service, response_mapper as M, rate_limiter
from bazarr.compat.auth import compat_auth


_SUPPORTED_SUB_FORMATS = frozenset({"srt"})


def _quota_config() -> tuple[int, int]:
    from bazarr.app.config import settings
    return (int(settings.compat_endpoint.downloads_per_window),
            int(settings.compat_endpoint.downloads_window_seconds))


def _iso_utc(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)\
                       .strftime("%Y-%m-%dT%H:%M:%SZ")


def _jti_from_request() -> str | None:
    """Pull jti from the (pre-validated) bearer. Returns None when there
    is no bearer or the bearer doesn't decode."""
    bearer = (request.headers.get("Authorization") or "")
    if not bearer.startswith("Bearer "):
        return None
    ok, claims = auth.validate_jwt(bearer[7:])
    return (claims.get("jti") if ok else None) or None

compat_bp = Blueprint("compat", __name__)


@compat_bp.after_request
def _strip_cors(resp):
    """Explicit CORS scope override (B4). No CORS for /api/v1/*."""
    for h in ("Access-Control-Allow-Origin", "Access-Control-Allow-Credentials",
              "Access-Control-Allow-Methods", "Access-Control-Allow-Headers"):
        resp.headers.pop(h, None)
    return resp


def _infer_client_base(req) -> tuple[str, str]:
    """Best-effort (scheme, host) reconstruction for the URL the client hit.

    Trusts X-Forwarded-Host/Proto (set by supervisor.py and any outer
    reverse proxy) because those reflect what the CLIENT used to reach us.
    Falls back to request.host/scheme for direct-to-Flask hits. Returns
    ("", "") only if neither yields a value.

    Historically this function filtered out 127.*/localhost hosts on the
    theory that they were the internal supervisor->flask loopback. That
    was wrong: it also rejected legitimate same-box clients (Jellyfin
    on the same docker host) and masked the real bug - supervisor
    wasn't setting X-Forwarded-Host at all.
    """
    scheme = (req.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip() or req.scheme
    host = (req.headers.get("X-Forwarded-Host") or "").split(",")[0].strip() or req.host
    if not host:
        return "", ""
    return scheme or "http", host


@compat_bp.route("/login", methods=["POST"])
@compat_auth(require_jwt=False)
def login():
    scheme, host = _infer_client_base(request)
    base_url = host or request.host
    limit, window = _quota_config()
    remaining, reset = rate_limiter.inspect("", limit, window)
    user_data = M.user_info_response(remaining=remaining, allowed=limit,
                                      reset_iso=_iso_utc(reset))["data"]
    return jsonify({
        "token": auth.mint_jwt(),
        "status": 200,
        "base_url": base_url,
        "user": user_data,
    })


@compat_bp.route("/logout", methods=["DELETE"])
@compat_auth(require_jwt=True)
def logout():
    """Validate the bearer JWT and revoke its jti. Unlike OS.com, which
    doesn't track token state, we keep a server-side jti denylist so a
    logged-out token stops working even before its own exp."""
    bearer = (request.headers.get("Authorization") or "")
    # compat_auth already validated; decode again to grab jti/exp cleanly.
    ok, claims = auth.validate_jwt(bearer[7:] if bearer.startswith("Bearer ") else "")
    if ok:
        auth.revoke_jwt(claims.get("jti", ""), int(claims.get("exp", 0)))
    return "", 204


@compat_bp.route("/subtitles", methods=["GET"])
@compat_auth(require_jwt=False)
def subtitles():
    args = request.args
    langs_s = args.get("languages") or ""
    imdb = args.get("imdb_id") or args.get("tmdb_id") or ""
    query_filename = args.get("query") or None
    if not imdb and not query_filename:
        return compat_error("imdb_id, tmdb_id, or query required", 400, "bad-request")
    moviehash = args.get("moviehash") or None
    moviehash_match = args.get("moviehash_match") or None
    if moviehash_match and moviehash_match not in ("include", "only"):
        return compat_error("moviehash_match must be include|only", 400, "bad-request")
    from subzero.language import Language
    requested_codes = [c.strip() for c in langs_s.split(",") if c.strip()] if langs_s else []
    try:
        if requested_codes:
            langs = [Language.fromietf(c) for c in requested_codes]
        else:
            langs = [Language.fromietf("en")]
            requested_codes = ["en"]
    except Exception:
        return compat_error("invalid language code", 400, "bad-request")
    season = args.get("season_number", type=int)
    episode = args.get("episode_number", type=int)
    raw_type = (args.get("type") or "").strip().lower()
    if raw_type in ("episode", "movie"):
        media_type = raw_type
    elif season is not None or episode is not None:
        media_type = "episode"
    else:
        media_type = "movie"
    try:
        result = service.search(imdb or "", season, episode, langs, media_type,
                                query=query_filename, moviehash=moviehash,
                                moviehash_match=moviehash_match,
                                requested_languages=requested_codes)
    except Exception:
        return compat_error("upstream providers unavailable", 503, "upstream")
    page = max(1, args.get("page", default=1, type=int) or 1)
    per_page = args.get("per_page", default=50, type=int) or 50
    per_page = min(max(per_page, 1), 100)
    all_entries = result.get("data", [])
    start = (page - 1) * per_page
    end = start + per_page
    sliced = all_entries[start:end]
    total = len(all_entries)
    total_pages = max(1, (total + per_page - 1) // per_page) if per_page > 0 else 1
    return jsonify({
        "total_pages": total_pages,
        "total_count": total,
        "per_page": per_page,
        "page": page,
        "data": sliced,
    })


@compat_bp.route("/download", methods=["POST"])
@compat_auth(require_jwt=True)
def download():
    body = request.get_json(silent=True) or {}
    fid = body.get("file_id")
    if fid is None or fid == "":
        return compat_error("file_id required", 400, "bad-request")
    try:
        fid_int = int(fid)
    except (TypeError, ValueError):
        return compat_error("file_id must be an integer", 400, "bad-request")
    sub_format = str(body.get("sub_format") or "srt").lower()
    if sub_format not in _SUPPORTED_SUB_FORMATS:
        return compat_error(f"unsupported sub_format: {sub_format}",
                            400, "bad-request")

    limit, window = _quota_config()
    jti = _jti_from_request() or ""
    allowed, remaining, reset = rate_limiter.try_consume(jti, limit, window)
    if not allowed:
        resp = jsonify({"message": "download quota exhausted",
                        "reset_time_utc": _iso_utc(reset)})
        resp.status_code = 406
        resp.headers["x-reason"] = "throttled"
        return resp

    try:
        scheme, host = _infer_client_base(request)
        if host:
            base_host = f"{scheme}://{host}"
        else:
            base_host = request.host_url.rstrip("/")
        resp = service.download(fid_int, base_host=base_host,
                                remaining=remaining,
                                reset_iso=_iso_utc(reset))
    except FileNotFoundError:
        return compat_error("subtitle not found", 404, "not_found")
    return jsonify(resp)


@compat_bp.route("/download/stream/<stream_token>", methods=["GET"])
def download_stream(stream_token):
    """Pre-signed download URL.

    Contract: plugin sends NO auth headers here. The HMAC-signed stream
    token IS the auth - only a token we minted (via mint_file_stream_token,
    signed with compat_endpoint.file_id_secret) can resolve to a payload,
    and the token carries an exp claim that service.serve_subtitle_content
    enforces. Applying @compat_auth here would require an Api-Key header
    on this route, which would break plugin download the moment it fired.
    """
    import logging
    _log = logging.getLogger("bazarr.compat.routes")
    try:
        blob, ctype = service.serve_subtitle_content(stream_token)
    except ValueError:
        return compat_error("stream token expired", 410, "not_found")
    except FileNotFoundError:
        return compat_error("subtitle not found", 404, "not_found")
    except Exception as e:
        _log.exception("compat stream: unexpected: %s", e)
        return compat_error("provider fetch failed", 503, "upstream")
    return Response(blob, mimetype=ctype)


@compat_bp.route("/infos/user", methods=["GET"])
@compat_auth(require_jwt=False)
def infos_user():
    """Api-Key alone is sufficient. OS-compat clients (Jellyfin) poll /infos/user
    for remaining-downloads updates without re-minting the JWT each time."""
    limit, window = _quota_config()
    jti = _jti_from_request() or ""
    remaining, reset = rate_limiter.inspect(jti, limit, window)
    return jsonify(M.user_info_response(remaining=remaining, allowed=limit,
                                          reset_iso=_iso_utc(reset)))


@compat_bp.route("/infos/languages", methods=["GET"])
def infos_languages():
    return jsonify(M.languages_response())


@compat_bp.route("/utilities/guessit", methods=["POST"])
@compat_auth(require_jwt=False)
def utilities_guessit():
    body = request.get_json(silent=True) or {}
    filename = body.get("filename") or request.args.get("filename") or ""
    try:
        return jsonify(service.guessit_filename(filename))
    except ValueError:
        return compat_error("bad filename", 400, "bad-request")
