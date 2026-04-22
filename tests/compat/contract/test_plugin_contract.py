"""Contract compliance tests for the Jellyfin Bazarr+ plugin.

Source of truth: the plugin contract document (reproduced in docstrings
below). Any change that breaks these tests is almost certainly breaking
the Jellyfin plugin in production - update the tests only after
confirming the contract itself has changed.

These tests use Flask test_client, not the live server, so they run in
CI with no external dependencies.
"""
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


API_KEY = "t" * 32
JWT_SECRET = "j" * 32
FID_SECRET = "f" * 32


@pytest.fixture(autouse=True)
def _compat_secrets():
    from bazarr.app.config import settings
    from bazarr.compat.file_id_store import reset_store
    originals = {
        n: getattr(settings.compat_endpoint, n, "")
        for n in ("token", "jwt_secret", "file_id_secret",
                  "jwt_ttl_seconds", "file_id_ttl_seconds",
                  "stream_token_ttl_seconds")
    }
    settings["compat_endpoint"]["token"] = API_KEY
    settings["compat_endpoint"]["jwt_secret"] = JWT_SECRET
    settings["compat_endpoint"]["file_id_secret"] = FID_SECRET
    settings["compat_endpoint"]["jwt_ttl_seconds"] = 3600
    settings["compat_endpoint"]["file_id_ttl_seconds"] = 3600
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 300
    reset_store()
    yield
    for n, v in originals.items():
        settings["compat_endpoint"][n] = v
    reset_store()


def _app():
    from bazarr.compat.routes import compat_bp
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# 1. POST /login
# ---------------------------------------------------------------------------

def test_login_ignores_placeholder_body_if_api_key_valid():
    """Plugin sends username=bazarr / password=bazarr-placeholder; Api-Key
    is the real auth."""
    r = _app().test_client().post(
        "/api/v1/login",
        headers={"Api-Key": API_KEY},
        json={"username": "bazarr", "password": "bazarr-placeholder"},
    )
    assert r.status_code == 200


def test_login_returns_jwt_with_exp_claim():
    """Plugin decodes JWT locally and re-logs in when exp is in the past.
    Without exp, plugin treats token as already expired on every call."""
    import jwt as pyjwt
    r = _app().test_client().post(
        "/api/v1/login",
        headers={"Api-Key": API_KEY},
        json={},
    )
    assert r.status_code == 200
    token = r.get_json()["token"]
    # 3-segment base64url
    assert token.count(".") == 2
    claims = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert isinstance(claims.get("exp"), int)
    assert claims["exp"] > 0


def test_login_user_block_has_iso_reset_time_utc():
    """Contract: if `user` is included, reset_time_utc is STRICT (non-nullable
    in the plugin's System.DateTime model; empty string 500s)."""
    r = _app().test_client().post(
        "/api/v1/login",
        headers={"Api-Key": API_KEY},
        json={},
    )
    body = r.get_json()
    if "user" in body:
        rt = body["user"].get("reset_time_utc")
        assert isinstance(rt, str) and rt.endswith("Z")
        # parseable as ISO
        from datetime import datetime
        datetime.fromisoformat(rt.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# 2. DELETE /logout
# ---------------------------------------------------------------------------

def test_logout_accepts_api_key_alone():
    """Contract: 'any 2xx'. Plugin may hit /logout after it has already
    cleared its JWT; requiring Bearer traps it in a clear-and-retry loop."""
    r = _app().test_client().delete(
        "/api/v1/logout",
        headers={"Api-Key": API_KEY},
    )
    assert 200 <= r.status_code < 300


# ---------------------------------------------------------------------------
# 3. GET /infos/user
# ---------------------------------------------------------------------------

def test_infos_user_envelope_has_strict_reset_time_utc():
    r = _app().test_client().get(
        "/api/v1/infos/user",
        headers={"Api-Key": API_KEY},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "data" in body
    rt = body["data"].get("reset_time_utc")
    assert isinstance(rt, str) and rt.endswith("Z") and rt != ""


# ---------------------------------------------------------------------------
# 4. GET /infos/languages
# ---------------------------------------------------------------------------

def test_languages_envelope_uses_language_code_key():
    """Historical bug: was {code, name}. Must be {language_code, language_name}."""
    r = _app().test_client().get("/api/v1/infos/languages")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert isinstance(data, list) and data
    for entry in data:
        assert "language_code" in entry
        assert "code" not in entry


def test_languages_includes_zh_CN_and_pt_PT():
    """Plugin normalizes 'zh' -> 'zh-CN' and 'pt' -> 'pt-PT' before matching.
    The contract says those two tokens MUST appear - guard with strict
    casing (not 'zh-cn' / 'pt-pt') for defensive compatibility with
    clients that do case-sensitive compare."""
    r = _app().test_client().get("/api/v1/infos/languages")
    codes = [e["language_code"] for e in r.get_json()["data"]]
    assert "zh-CN" in codes
    assert "pt-PT" in codes


# ---------------------------------------------------------------------------
# 5. GET /subtitles - envelope + attribute shapes
# ---------------------------------------------------------------------------

def _fake_search_result(attrs_override=None):
    """A minimal envelope matching service.search() return shape."""
    base_attrs = {
        "language": "en", "subtitle_id": "s1", "release": "rel",
        "download_count": 0, "ratings": 0.0, "votes": 0,
        "from_trusted": False, "hd": False, "hearing_impaired": False,
        "moviehash_match": False, "ai_translated": False,
        "machine_translated": False, "foreign_parts_only": False,
        "fps": 0.0,
        # The one that was broken: must be a non-empty ISO-8601 string.
        "upload_date": "1970-01-01T00:00:00Z",
        "uploader": {"name": "x"},
        "feature_details": {
            "feature_type": "Movie",
            "imdb_id": 111161,
            "season_number": 0, "episode_number": 0,
            "title": "X", "movie_name": "1994 - X", "year": 1994,
        },
        "files": [{"file_id": 7, "file_name": "111161.en.srt"}],
    }
    if attrs_override:
        base_attrs.update(attrs_override)
    return {
        "total_pages": 1, "total_count": 1, "per_page": 50, "page": 1,
        "data": [{"id": "7", "type": "subtitle", "attributes": base_attrs}],
    }


def test_subtitles_envelope_has_all_strict_fields():
    """total_pages, total_count, per_page, page are all strict."""
    with patch("bazarr.compat.service.search",
                return_value=_fake_search_result()):
        r = _app().test_client().get(
            "/api/v1/subtitles?imdb_id=111161&languages=en&type=movie",
            headers={"Api-Key": API_KEY},
        )
    assert r.status_code == 200
    body = r.get_json()
    for field in ("page", "per_page", "total_pages", "total_count", "data"):
        assert field in body, f"missing {field}"
    assert isinstance(body["page"], int)
    assert isinstance(body["total_pages"], int)
    assert body["total_pages"] >= 1


def test_subtitles_upload_date_is_never_empty_string():
    """CRASH-class bug per contract: upload_date='' raises
    System.Text.Json.JsonException in the plugin. Must be a valid ISO
    datetime, with 1970 epoch as the fallback."""
    # Mock the response_mapper directly so we test the mapper path.
    from bazarr.compat import response_mapper as M
    sub = MagicMock(upload_date=None, id="1", language=MagicMock(alpha2="en"),
                    download_count=0, ratings=0.0, release_info="",
                    uploader=None, provider_name="os")
    entry = M.subtitle_to_os_entry(sub, file_id=1, media_type="movie",
                                    imdb_id="0111161")
    assert entry["attributes"]["upload_date"] != ""
    assert entry["attributes"]["upload_date"].endswith("Z")
    # Parseable as ISO 8601
    from datetime import datetime
    datetime.fromisoformat(
        entry["attributes"]["upload_date"].replace("Z", "+00:00")
    )


def test_subtitles_upload_date_from_provider_is_preserved():
    """When a provider DOES expose upload_date, we must pass it through,
    not replace it with the epoch fallback."""
    from datetime import datetime
    from bazarr.compat import response_mapper as M
    provider_date = datetime(2023, 1, 15, 12, 34, 56)
    sub = MagicMock(upload_date=provider_date, id="1",
                    language=MagicMock(alpha2="en"),
                    download_count=0, ratings=0.0, release_info="",
                    uploader=None, provider_name="os")
    entry = M.subtitle_to_os_entry(sub, file_id=1, media_type="movie",
                                    imdb_id="0111161")
    assert entry["attributes"]["upload_date"].startswith("2023-01-15")


def test_subtitles_feature_details_imdb_id_is_int_not_string():
    """Filter-inducing: plugin drops results where imdb_id is a string."""
    from bazarr.compat import response_mapper as M
    sub = MagicMock(upload_date=None, id="1", language=MagicMock(alpha2="en"),
                    download_count=0, ratings=0.0, release_info="",
                    uploader=None, provider_name="os")
    entry = M.subtitle_to_os_entry(sub, file_id=1, media_type="movie",
                                    imdb_id="tt0111161")
    assert isinstance(entry["attributes"]["feature_details"]["imdb_id"], int)
    assert entry["attributes"]["feature_details"]["imdb_id"] == 111161


def test_subtitles_feature_details_feature_type_case_exact():
    """'Movie' vs 'movie' matters - plugin does case-SENSITIVE compare."""
    from bazarr.compat import response_mapper as M
    sub = MagicMock(upload_date=None, id="1", language=MagicMock(alpha2="en"),
                    download_count=0, ratings=0.0, release_info="",
                    uploader=None, provider_name="os")
    movie_entry = M.subtitle_to_os_entry(sub, file_id=1, media_type="movie",
                                          imdb_id="0111161")
    assert movie_entry["attributes"]["feature_details"]["feature_type"] == "Movie"

    ep_entry = M.subtitle_to_os_entry(sub, file_id=1, media_type="episode",
                                       imdb_id="0903747", season=1, episode=2)
    assert ep_entry["attributes"]["feature_details"]["feature_type"] == "Episode"


def test_subtitles_files_has_positive_int_file_id():
    """String or null file_id → result silently dropped by plugin."""
    from bazarr.compat import response_mapper as M
    sub = MagicMock(upload_date=None, id="1", language=MagicMock(alpha2="en"),
                    download_count=0, ratings=0.0, release_info="",
                    uploader=None, provider_name="os")
    entry = M.subtitle_to_os_entry(sub, file_id=42, media_type="movie",
                                    imdb_id="0111161")
    assert isinstance(entry["attributes"]["files"], list)
    assert len(entry["attributes"]["files"]) >= 1
    fid = entry["attributes"]["files"][0]["file_id"]
    assert isinstance(fid, int) and fid > 0


def test_subtitles_episode_season_episode_numbers_are_ints():
    """Filter-inducing: wrong type or 0/0 on an episode search gets dropped."""
    from bazarr.compat import response_mapper as M
    sub = MagicMock(upload_date=None, id="1", language=MagicMock(alpha2="en"),
                    download_count=0, ratings=0.0, release_info="",
                    uploader=None, provider_name="os")
    entry = M.subtitle_to_os_entry(sub, file_id=1, media_type="episode",
                                    imdb_id="0903747", season=1, episode=2)
    fd = entry["attributes"]["feature_details"]
    assert fd["season_number"] == 1 and isinstance(fd["season_number"], int)
    assert fd["episode_number"] == 2 and isinstance(fd["episode_number"], int)


def test_subtitles_route_accepts_query_only_search():
    """Plugin sends either imdb_id OR query+season+episode. Query-only
    must not 400, and must not smuggle the filename into imdb_id."""
    result = _fake_search_result()
    with patch("bazarr.compat.service.search", return_value=result) as s:
        r = _app().test_client().get(
            "/api/v1/subtitles?query=For.All.Mankind.S01E01.mkv"
            "&languages=en&type=episode&season_number=1&episode_number=1",
            headers={"Api-Key": API_KEY},
        )
    assert r.status_code == 200
    # service.search was called with imdb_id='' (empty) and query=filename,
    # NOT with the filename-as-imdb confusion.
    call_args = s.call_args
    imdb_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("imdb_id")
    query_arg = call_args.kwargs.get("query")
    assert imdb_arg == "" or imdb_arg is None
    assert query_arg == "For.All.Mankind.S01E01.mkv"


# ---------------------------------------------------------------------------
# 6. POST /download
# ---------------------------------------------------------------------------

def test_download_link_is_absolute_url():
    """Plugin runs HttpClient.GetAsync(link) - relative URL throws when no
    BaseAddress. Must be scheme://host/path."""
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    r = _app().test_client().post(
        "/api/v1/download",
        headers={"Api-Key": API_KEY,
                 "Authorization": f"Bearer {jwt_tok}"},
        json={"file_id": fid, "sub_format": "srt"},
    )
    assert r.status_code == 200
    link = r.get_json()["link"]
    assert link.startswith(("http://", "https://")), f"not absolute: {link!r}"
    assert "/api/v1/download/stream/" in link


def test_download_link_honors_forwarded_headers():
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    r = _app().test_client().post(
        "/api/v1/download",
        headers={"Api-Key": API_KEY,
                 "Authorization": f"Bearer {jwt_tok}",
                 "X-Forwarded-Host": "bazarr.example.com",
                 "X-Forwarded-Proto": "https"},
        json={"file_id": fid},
    )
    assert r.status_code == 200
    link = r.get_json()["link"]
    assert link.startswith("https://bazarr.example.com/"), link


# ---------------------------------------------------------------------------
# 7. GET <stream link> - MUST NOT require auth headers
# ---------------------------------------------------------------------------

def test_download_stream_url_accepts_no_auth_headers():
    """Plugin contract: 'Plugin sends no auth header (OS.com uses signed URLs).'
    The HMAC-signed stream token IS the auth - the route MUST NOT gate on
    Api-Key or Bearer. If it did, the plugin's follow-up GET of the link
    would 403 immediately after every successful download-link mint."""
    from bazarr.compat import auth
    # Patch serve_subtitle_content so we don't need a real provider backend.
    with patch("bazarr.compat.service.serve_subtitle_content",
                return_value=(b"1\n00:00:01,000 --> 00:00:02,000\nhi\n",
                              "application/x-subrip")):
        token = auth.mint_file_stream_token(1)
        r = _app().test_client().get(f"/api/v1/download/stream/{token}")
    assert r.status_code == 200
    assert r.data.startswith(b"1\n") or r.data


def test_download_stream_url_rejects_tampered_token():
    """Unsigned / tampered tokens must not resolve - the HMAC is load-bearing."""
    # Pick any well-formed-looking base64 that won't HMAC-verify.
    bogus = "eyJmaWQiOjEsImV4cCI6OTk5OTk5OTk5OX0.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    r = _app().test_client().get(f"/api/v1/download/stream/{bogus}")
    assert r.status_code in (404, 410)


# ---------------------------------------------------------------------------
# Status-code contract
# ---------------------------------------------------------------------------

def test_missing_api_key_returns_403_not_401():
    """401 is reserved for JWT expiry. 401 on an Api-Key failure traps the
    plugin in a clear-token-and-retry loop that can't recover."""
    r = _app().test_client().get("/api/v1/subtitles?imdb_id=1&languages=en")
    assert r.status_code == 403


def test_missing_jwt_on_download_returns_401():
    """Download requires JWT. Missing Bearer on a require_jwt route is
    the exact signal the plugin uses to re-login."""
    r = _app().test_client().post(
        "/api/v1/download",
        headers={"Api-Key": API_KEY},
        json={"file_id": 1},
    )
    assert r.status_code == 401
