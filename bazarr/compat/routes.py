from __future__ import annotations
from flask import Blueprint, request, jsonify, Response, g
# Intra-package and intra-app imports MUST drop the `bazarr.` prefix - the
# rest of bazarr resolves modules from `bazarr/` as sys.path root, and a
# `bazarr.foo` import resolves to a SECOND module instance with its own
# state. The compat regenerate path previously showed duplicate
# settings/database instances when these imports used the package prefix.
from .auth import compat_error
from utilities.url_guard import UnsafeURLError

compat_stub_bp = Blueprint("compat_stub", __name__)


@compat_stub_bp.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@compat_stub_bp.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def _all_disabled(path):
    return compat_error("disabled", 404, "compat-disabled")


import datetime as _dt  # noqa: E402
import time as _time  # noqa: E402
from . import auth, service, response_mapper as M  # noqa: E402
from . import meter, limits, keyring  # noqa: E402
from .auth import compat_auth  # noqa: E402


_SUPPORTED_SUB_FORMATS = frozenset({"srt"})


def _current_key() -> dict:
    """The key record stashed by compat_auth (always set on authed routes)."""
    return getattr(g, "compat_key", None) or auth._legacy_key_record()


def _meter_ok(key_rec: dict, kind: str) -> None:
    """Record a successful metered hit + touch last-used (best-effort)."""
    kid = int(key_rec.get("id") or 0)
    if kid <= 0:
        return  # unkeyed legacy fallback: nothing persistent to meter
    meter.record(kid, kind)
    try:
        keyring.touch_last_used(kid)
    except Exception:
        pass


def _meter_blocked(key_rec: dict, kind: str) -> None:
    kid = int(key_rec.get("id") or 0)
    if kid > 0:
        meter.record(kid, kind, blocked=True)


def _display_download_quota(key_rec: dict) -> tuple[int, int, int]:
    """(allowed, remaining, reset_epoch) for the download quota shown to
    OS-compat clients. Unlimited keys report the legacy downloads_per_window
    as a cosmetic ceiling so the plugin UI still shows a sensible number."""
    from app.config import settings
    d = limits.check(key_rec, "download")
    if d.window == "none":
        ceiling = int(settings.compat_endpoint.downloads_per_window)
        reset = int(_time.time()) + int(settings.compat_endpoint.downloads_window_seconds)
        return ceiling, ceiling, reset
    return d.limit, max(0, d.remaining), d.reset_epoch


def _throttle_response(kind: str, decision):
    """429 for the search surface (the OS contract has no search-throttle
    code, so use the standard Too Many Requests). Carries Retry-After and
    X-RateLimit-* headers plus the OS-style x-reason + reset_time_utc."""
    reset = int(decision.reset_epoch)
    retry_after = max(1, reset - int(_time.time()))
    resp = jsonify({
        "message": f"{kind} rate limit exceeded ({decision.window})",
        "reset_time_utc": _iso_utc(reset),
    })
    resp.status_code = 429
    resp.headers["x-reason"] = "throttled"
    resp.headers["Retry-After"] = str(retry_after)
    resp.headers["X-RateLimit-Limit"] = str(int(decision.limit))
    resp.headers["X-RateLimit-Remaining"] = "0"
    resp.headers["X-RateLimit-Reset"] = str(reset)
    return resp


def _normalize_lang(lang):
    """Strip country subtag only for zh-CN. Providers register bare zho
    (generic Chinese) and zho;TW (Traditional), but NOT zho;CN. zh-TW
    must be preserved."""
    country = getattr(lang, "country", None)
    if not country:
        return lang
    country_code = getattr(country, "alpha2", None) or ""
    if lang.alpha3 == "zho" and country_code.upper() == "CN":
        from subzero.language import Language
        return Language(lang.alpha3)
    return lang


def _resolve_tmdb_to_imdb(tmdb_id: str) -> str:
    """Best-effort TMDB -> IMDB resolution via the local library database.

    Falls back to empty string (search proceeds as query-only) when
    resolution fails or the movie is not in the library.
    """
    try:
        from app.database import database, select, TableMovies
        row = database.execute(
            select(TableMovies.imdbId)
            .where(TableMovies.tmdbId == str(tmdb_id))
        ).first()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return ""


def _iso_utc(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)\
                       .strftime("%Y-%m-%dT%H:%M:%SZ")


compat_bp = Blueprint("compat", __name__)


@compat_bp.before_request
def _enforce_runtime_disable():
    """Refuse traffic when the operator toggles compat_endpoint.enabled
    off at runtime. The blueprint is mounted at startup based on the
    boot-time value, so without this guard a previously-enabled endpoint
    keeps serving with the old token until restart. Re-check the live
    setting on every request and 503 if it has been disabled.
    """
    from app.config import settings
    if not bool(settings.compat_endpoint.enabled):
        return jsonify({"error": "compat endpoint disabled"}), 503


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
    key_rec = _current_key()
    allowed, remaining, reset = _display_download_quota(key_rec)
    user_data = M.user_info_response(remaining=remaining, allowed=allowed,
                                      reset_iso=_iso_utc(reset))["data"]
    # Bind the JWT to the resolving key so /infos/user can attribute usage.
    return jsonify({
        "token": auth.mint_jwt({"kid": int(key_rec.get("id") or 0)}),
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
    imdb = args.get("imdb_id") or ""
    tmdb = args.get("tmdb_id") or ""
    query_filename = args.get("query") or None
    if not imdb and not query_filename and not tmdb:
        return compat_error("imdb_id, tmdb_id, or query required", 400, "bad-request")
    if not imdb and tmdb:
        imdb = _resolve_tmdb_to_imdb(tmdb)
    moviehash = args.get("moviehash") or None
    moviebytesize = args.get("moviebytesize", type=int)
    if moviebytesize is not None and moviebytesize <= 0:
        return compat_error("moviebytesize must be a positive integer", 400, "bad-request")
    series_anidb_id = args.get("series_anidb_id", type=int)
    if series_anidb_id is None:
        series_anidb_id = args.get("anidb_id", type=int)
    series_anidb_episode_id = args.get("series_anidb_episode_id", type=int)
    if series_anidb_episode_id is None:
        series_anidb_episode_id = args.get("anidb_episode_id", type=int)
    if series_anidb_id is not None and series_anidb_id <= 0:
        return compat_error("series_anidb_id must be a positive integer", 400, "bad-request")
    if series_anidb_episode_id is not None and series_anidb_episode_id <= 0:
        return compat_error("series_anidb_episode_id must be a positive integer", 400, "bad-request")
    moviehash_match = args.get("moviehash_match") or None
    if moviehash_match and moviehash_match not in ("include", "only"):
        return compat_error("moviehash_match must be include|only", 400, "bad-request")
    from subzero.language import Language
    requested_codes = [c.strip() for c in langs_s.split(",") if c.strip()] if langs_s else []
    try:
        if requested_codes:
            langs = [_normalize_lang(Language.fromietf(c)) for c in requested_codes]
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

    # Distribution Hub: meter + rate-limit search per API key (req #3, #5, #6).
    from app.config import settings
    key_rec = _current_key()
    if bool(settings.compat_endpoint.search_rate_limit_enabled):
        decision = limits.check(key_rec, "search")
        if not decision.allowed:
            _meter_blocked(key_rec, "search")
            return _throttle_response("search", decision)

    # Per-request provider exclusion + timeout, falling back to the key's
    # configured defaults (req #1, #2).
    exclude_param = args.get("exclude_providers") or ""
    req_exclude = [p.strip() for p in exclude_param.split(",") if p.strip()]
    eff_exclude = req_exclude or (key_rec.get("excluded_providers") or [])
    req_timeout = args.get("timeout_seconds", type=int)
    eff_timeout = req_timeout or key_rec.get("timeout_seconds")

    try:
        result = service.search(imdb or "", season, episode, langs, media_type,
                                query=query_filename, moviehash=moviehash,
                                moviebytesize=moviebytesize,
                                series_anidb_id=series_anidb_id,
                                series_anidb_episode_id=series_anidb_episode_id,
                                moviehash_match=moviehash_match,
                                requested_languages=requested_codes,
                                exclude_providers=eff_exclude or None,
                                timeout_seconds=eff_timeout)
    except Exception:
        return compat_error("upstream providers unavailable", 503, "upstream")
    _meter_ok(key_rec, "search")
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

    # Distribution Hub: per-key download metering + limit (req #5, #6). The
    # download throttle keeps the OS-contract 406 (the Jellyfin plugin treats
    # this code as a quota signal). The Api-Key resolves the key on every
    # request, so g.compat_key is authoritative for whose quota is spent.
    key_rec = _current_key()
    decision = limits.check(key_rec, "download")
    if not decision.allowed:
        _meter_blocked(key_rec, "download")
        resp = jsonify({"message": "download quota exhausted",
                        "reset_time_utc": _iso_utc(int(decision.reset_epoch))})
        resp.status_code = 406
        resp.headers["x-reason"] = "throttled"
        return resp

    try:
        scheme, host = _infer_client_base(request)
        if host:
            base_host = f"{scheme}://{host}"
        else:
            base_host = request.host_url.rstrip("/")
        # Compute the post-consume remaining for the displayed quota.
        _meter_ok(key_rec, "download")
        _allowed, remaining, reset = _display_download_quota(key_rec)
        resp = service.download(fid_int, base_host=base_host,
                                remaining=remaining,
                                reset_iso=_iso_utc(reset))
    except FileNotFoundError:
        return compat_error("subtitle not found", 404, "not_found")
    return jsonify(resp)


@compat_bp.route("/download/stream/<stream_token>", methods=["GET"])
def download_stream(stream_token):
    """Pre-signed download URL.

    Auth model: the HMAC-signed stream_token is the authorization, with
    exp enforced by parse_file_stream_token (see auth.py). Api-Key is
    not required here, but the Jellyfin plugin sends it on every request
    including this follow-up; the route accepts it silently so the plugin
    does not have to special-case this endpoint.

    Same-origin contract: the link returned by /download always points at
    this host (service.download constructs it from the request's own
    scheme+host or the forwarded headers). Never return a link to a
    third-party host, the plugin's request helper forwards the Bazarr
    Api-Key header on the follow-up, which would leak the token to
    whoever owns that host.

    Empty-body contract (P0 from plugin docs): when provider content is
    missing, return 200 + empty body, not 404. The plugin uses this
    signal to blocklist the file_id and skip it on future scans. A 404
    is treated as transient and retried forever.
    """
    import logging
    _log = logging.getLogger("bazarr.compat.routes")
    try:
        blob, ctype = service.serve_subtitle_content(stream_token)
    except UnsafeURLError:
        return compat_error("provider URL blocked by SSRF guard", 403, "auth")
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
    for remaining-downloads updates without re-minting the JWT each time.

    Reports the resolved key's download quota (inspect-only, no consume)."""
    key_rec = _current_key()
    allowed, remaining, reset = _display_download_quota(key_rec)
    return jsonify(M.user_info_response(remaining=remaining, allowed=allowed,
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
