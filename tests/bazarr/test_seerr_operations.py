# coding=utf-8
"""What Bazarr shows for each Seerr answer, across the three flavours."""

import logging

import pytest

from test_media_server_http import http_fixture as http_fixture

OVERSEERR_PUBLIC = {"initialized": True, "applicationUrl": "https://requests.example", "movie4kEnabled": True,
                    "series4kEnabled": False, "partialRequestsEnabled": True, "region": "US"}
SEERR_PUBLIC = {"initialized": True, "applicationUrl": "", "movie4kEnabled": False, "series4kEnabled": False,
                "partialRequestsEnabled": False, "enableSpecialEpisodes": True, "hideBlocklisted": False,
                "mediaServerType": 2}


def test_capability_from_public_settings_field_set():
    from seerr.operations import capability
    assert capability(OVERSEERR_PUBLIC)["blocklist"] is False
    assert capability(SEERR_PUBLIC)["blocklist"] is True
    assert capability({})["partial_requests"] is True
    assert capability({})["special_episodes"] is False
    assert capability({"movie4kEnabled": "yes"})["movie_4k"] is False


def test_link_base_prefers_external_then_application_then_api_url(monkeypatch):
    from app.config import settings
    from seerr.operations import capability, link_base
    monkeypatch.setattr(settings.seerr, "url", "http://seerr:5055/")
    monkeypatch.setattr(settings.seerr, "external_url", "")
    assert link_base(capability(OVERSEERR_PUBLIC)) == "https://requests.example"
    assert link_base(capability(SEERR_PUBLIC)) == "http://seerr:5055"
    monkeypatch.setattr(settings.seerr, "external_url", "https://me.example/seerr/")
    assert link_base(capability(SEERR_PUBLIC)) == "https://me.example/seerr"


def test_link_base_rejects_a_candidate_without_an_http_scheme(monkeypatch):
    from app.config import settings
    from seerr.operations import capability, link_base
    monkeypatch.setattr(settings.seerr, "external_url", "javascript:alert(1)")
    monkeypatch.setattr(settings.seerr, "url", "http://seerr:5055")
    # The malformed candidate is skipped in favour of the next valid one.
    assert link_base(capability({})) == "http://seerr:5055"
    monkeypatch.setattr(settings.seerr, "url", "seerr.example/no-scheme")
    assert link_base(capability({})) == ""


@pytest.mark.parametrize("blocklist, code, expected", [
    (False, 6, "deleted"), (True, 6, "blocklisted"), (True, 7, "deleted"), (False, 7, "unknown"),
    (True, 1, "unknown"), (True, 2, "pending"), (True, 3, "processing"), (True, 4, "partially_available"), (True, 5, "available"),
])
def test_media_status_decoded_per_capability(blocklist, code, expected):
    from seerr.operations import normalize_media
    cap = {"blocklist": blocklist, "movie_4k": False, "series_4k": False, "partial_requests": True,
           "special_episodes": False, "application_url": "", "application_title": ""}
    body = {"id": 550, "mediaInfo": {"status": code, "status4k": 1, "requests": [], "seasons": []}}
    assert normalize_media("movie", 550, 200, body, cap, "http://s")["status"] == expected


def _cap(**overrides):
    cap = {"blocklist": True, "movie_4k": False, "series_4k": False, "partial_requests": True,
           "special_episodes": False, "application_url": "", "application_title": ""}
    cap.update(overrides)
    return cap


def test_unseen_title_is_requestable_and_links():
    from seerr.operations import normalize_media
    result = normalize_media("movie", 550, 200, {"id": 550}, _cap(), "http://s")
    assert result["known"] is False and result["status"] == "unknown"
    assert result["requestable"] is True and result["request"] is None
    assert result["link"] == "http://s/movie/550"


def test_available_movie_with_stale_approved_request_is_not_requestable():
    from seerr.operations import normalize_media
    body = {"id": 550, "mediaInfo": {"status": 5, "status4k": 1, "seasons": [],
                                     "requests": [{"id": 9, "status": 2, "is4k": False, "seasons": []}]}}
    result = normalize_media("movie", 550, 200, body, _cap(), "http://s")
    assert result["status"] == "available" and result["requestable"] is False
    assert result["request"] == {"id": 9, "status": "approved", "is4k": False, "seasons": []}


def test_declined_movie_request_re_enables_the_action():
    from seerr.operations import normalize_media
    body = {"id": 550, "mediaInfo": {"status": 1, "status4k": 1, "seasons": [],
                                     "requests": [{"id": 9, "status": 3, "is4k": False, "seasons": []}]}}
    result = normalize_media("movie", 550, 200, body, _cap(), "http://s")
    assert result["request"]["status"] == "declined" and result["requestable"] is True


def test_show_partially_available_keeps_remaining_seasons_requestable():
    from seerr.operations import normalize_media
    body = {"id": 1399, "mediaInfo": {"status": 4, "status4k": 1,
                                      "seasons": [{"seasonNumber": 1, "status": 5}, {"seasonNumber": 2, "status": 1}],
                                      "requests": [{"id": 3, "status": 2, "is4k": False,
                                                    "seasons": [{"seasonNumber": 3, "status": 2}]}]}}
    result = normalize_media("tv", 1399, 200, body, _cap(), "http://s")
    assert result["status"] == "partially_available" and result["requestable"] is True
    assert {s["number"]: s["state"] for s in result["seasons"]} == {1: "available", 2: "requestable", 3: "requested"}
    assert result["link"] == "http://s/tv/1399"


def test_show_season_in_flight_is_requested_not_available():
    from seerr.operations import normalize_media
    body = {"id": 1399, "mediaInfo": {"status": 4, "status4k": 1,
                                      "seasons": [{"seasonNumber": 1, "status": 3}, {"seasonNumber": 2, "status": 5},
                                                  {"seasonNumber": 3, "status": 1}],
                                      "requests": []}}
    result = normalize_media("tv", 1399, 200, body, _cap(), "http://s")
    assert {s["number"]: s["state"] for s in result["seasons"]} == {1: "requested", 2: "available", 3: "requestable"}


def test_show_available_season_is_never_demoted_by_an_open_request():
    from seerr.operations import normalize_media
    body = {"id": 1399, "mediaInfo": {"status": 4, "status4k": 1,
                                      "seasons": [{"seasonNumber": 1, "status": 3}, {"seasonNumber": 2, "status": 5},
                                                  {"seasonNumber": 3, "status": 1}],
                                      "requests": [{"id": 3, "status": 1, "is4k": False,
                                                    "seasons": [{"seasonNumber": 2, "status": 1}]}]}}
    result = normalize_media("tv", 1399, 200, body, _cap(), "http://s")
    assert {s["number"]: s["state"] for s in result["seasons"]}[2] == "available"


def test_blocklisted_show_is_never_requestable():
    from seerr.operations import normalize_media
    body = {"id": 1399, "mediaInfo": {"status": 6, "status4k": 1, "seasons": [], "requests": []}}
    assert normalize_media("tv", 1399, 200, body, _cap(), "http://s")["requestable"] is False


def test_4k_axis_is_independent():
    from seerr.operations import normalize_media
    body = {"id": 550, "mediaInfo": {"status": 5, "status4k": 1, "seasons": [], "requests": []}}
    assert normalize_media("movie", 550, 200, body, _cap(movie_4k=True), "http://s")["requestable_4k"] is True
    assert normalize_media("movie", 550, 200, body, _cap(movie_4k=False), "http://s")["requestable_4k"] is False


@pytest.mark.parametrize("status, body, expected", [
    (201, {"id": 12, "status": 2, "is4k": False, "seasons": []}, {"outcome": "requested"}),
    (202, {"message": "No seasons available to request"}, {"outcome": "nothing_to_request"}),
    (409, {"message": "Request for this media already exists."}, {"outcome": "already_requested"}),
    (403, {"message": "You do not have permission to make movie requests."}, {"error_code": "permission"}),
    (403, {"message": "Movie Quota exceeded."}, {"error_code": "quota"}),
    (403, {"message": "This media is blocklisted."}, {"error_code": "blocklisted"}),
    (403, {"message": "This media is blacklisted."}, {"error_code": "blocklisted"}),
    (403, {"status": 403, "error": "You do not have permission to access this endpoint"}, {"error_code": "rejected_key"}),
    (403, None, {"error_code": "csrf_blocked"}),
    (400, {"message": "request/body/mediaId must be number", "errors": []}, {"error_code": "validation"}),
    (500, {"message": "Unable to retrieve movie."}, {"error_code": "upstream_error"}),
])
def test_request_outcomes(status, body, expected):
    from seerr.operations import request_outcome
    result = request_outcome(status, body)
    for key, value in expected.items():
        assert result[key] == value
    if status == 201:
        assert result["request"] == {"id": 12, "status": "approved", "is4k": False, "seasons": []}


def test_a_201_with_an_unusable_body_is_still_reported_as_requested():
    """A request Seerr really created must never read back as a failure: that
    sends the user into a retry, which then reads back as already_requested."""
    from seerr.operations import request_outcome
    assert request_outcome(201, None) == {"outcome": "requested", "request": None}
    assert request_outcome(201, [1, 2, 3]) == {"outcome": "requested", "request": None}
    assert request_outcome(201, "not json") == {"outcome": "requested", "request": None}


def test_request_outcome_picks_the_newest_non_4k_request_by_id():
    """Order in the requests array is not a guarantee; only the id is."""
    from seerr.operations import normalize_media
    body = {"id": 550, "mediaInfo": {"status": 1, "status4k": 1, "seasons": [],
                                     "requests": [{"id": 9, "status": 1, "is4k": False, "seasons": []},
                                                  {"id": 3, "status": 1, "is4k": False, "seasons": []}]}}
    result = normalize_media("movie", 550, 200, body, _cap(), "http://s")
    assert result["request"]["id"] == 9


def test_test_connection_reports_capability_and_acting_user(http_fixture):
    from seerr.operations import test_connection
    base, records = http_fixture([
        (200, {"version": "3.3.0"}, {}),
        (200, SEERR_PUBLIC, {}),
        (200, {"id": 1, "displayName": "Owner", "permissions": 2}, {}),
    ])
    result = test_connection(base, "synthetic-key", True)
    assert result["success"] is True and result["version"] == "3.3.0" and result["blocklist_capable"] is True
    assert result["acting_user"] == {"id": 1, "display_name": "Owner", "can_request_movie": True,
                                     "can_request_tv": True, "can_request_4k_movie": True, "can_request_4k_tv": True}
    assert [r["path"] for r in records] == ["/api/v1/status", "/api/v1/settings/public", "/api/v1/auth/me"]


def test_test_connection_distinguishes_rejected_key(http_fixture):
    from seerr.operations import test_connection
    base, _records = http_fixture([(200, {"version": "3.3.0"}, {}), (200, {}, {}),
                                   (403, {"status": 403, "error": "denied"}, {})])
    assert test_connection(base, "synthetic-key", True) == {"success": False, "error_code": "rejected_key"}


def test_test_connection_never_echoes_the_key_or_exception_text(http_fixture, caplog):
    from seerr.operations import test_connection
    # The code logs the failure at DEBUG. Without raising the capture level
    # to match, caplog.text stays empty and the assertion below cannot fail
    # no matter what leaks into the log message.
    caplog.set_level(logging.DEBUG)
    base, _records = http_fixture([(500, b"synthetic-key leaked", {})])
    result = test_connection(base, "synthetic-key", True)
    assert result == {"success": False, "error_code": "connection_failed"}
    assert caplog.text
    assert "synthetic-key" not in caplog.text


def test_permission_bits():
    from seerr.operations import user_capabilities
    # REQUEST=32, REQUEST_MOVIE=262144, REQUEST_TV=524288, REQUEST_4K=1024
    assert user_capabilities(32)["can_request_movie"] is True
    assert user_capabilities(262144)["can_request_tv"] is False
    assert user_capabilities(524288)["can_request_tv"] is True
    assert user_capabilities(1024)["can_request_4k_movie"] is True
    assert user_capabilities(0)["can_request_movie"] is False
    assert user_capabilities(2)["can_request_4k_tv"] is True  # ADMIN


def test_tmdb_id_for_tvdb_uses_tmdb_find(monkeypatch):
    from discover import metadata
    from seerr import operations
    calls = []

    def fake_request(config, path, params=None):
        calls.append((path, params))
        return {"tv_results": [{"id": 1399}], "movie_results": []}

    monkeypatch.setattr(metadata, "_request", fake_request)
    monkeypatch.setattr(operations, "_find_cache", {})
    assert operations.tmdb_id_for_tvdb(121361) == 1399
    assert calls == [("/find/121361", {"external_source": "tvdb_id"})]
    assert operations.tmdb_id_for_tvdb(121361) == 1399
    assert len(calls) == 1


def test_tmdb_id_for_tvdb_returns_none_when_unmatched(monkeypatch):
    from discover import metadata
    from seerr import operations
    monkeypatch.setattr(metadata, "_request", lambda config, path, params=None: {"tv_results": []})
    monkeypatch.setattr(operations, "_find_cache", {})
    assert operations.tmdb_id_for_tvdb(5) is None
