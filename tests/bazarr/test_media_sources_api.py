"""The first routes: create a folder source with a library, scan it, list its items and search one."""
import pytest
from flask import Flask

from media_source_helpers import add_profile, video

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
SOURCES = "/api/system/media-sources"
ITEMS = "/api/sources/items"


@pytest.fixture
def api(source_db, monkeypatch, tmp_path, languages_loaded):
    from api import api_bp
    from app.config import settings
    from media_sources import indexer, scanner, service

    monkeypatch.setitem(settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(indexer, "get_language_set", lambda: set())
    queued = []
    for module in (scanner, service):
        monkeypatch.setattr(module.jobs_queue, "add_job_from_function",
                            lambda name, is_progress, queued=queued: queued.append(name) or 33)
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    movies = tmp_path / "Movies"
    movies.mkdir()
    return type("Api", (), {"client": app.test_client(), "db": source_db, "movies": movies, "queued": queued,
                            "tmp_path": tmp_path})


def _create(api, **extra):
    body = {"name": "Local movies", "library": {"path": str(api.movies), "content_kind": "movie"}}
    body.update(extra)
    return api.client.post(SOURCES, json=body, headers=HEADERS)


def test_every_route_needs_the_api_key(api):
    assert api.client.post(SOURCES, json={}).status_code == 401
    assert api.client.post(SOURCES + "/1/scan").status_code == 401
    assert api.client.get(ITEMS).status_code == 401
    assert api.client.post(ITEMS + "/1/search").status_code == 401


def test_a_source_is_created_with_its_library_and_root(api):
    profile = add_profile(api.db, [("en", "False", "False")])
    response = _create(api, library={"path": str(api.movies), "content_kind": "movie", "profile_id": profile},
                       scan_frequency_hours=0)

    assert response.status_code == 201
    body = response.json
    assert (body["kind"], body["name"], body["enabled"], body["scan_frequency_hours"]) == ("folder", "Local movies",
                                                                                           True, 0)
    (library,) = body["libraries"]
    assert (library["content_kind"], library["locator"], library["profile_id"]) == ("movie", str(api.movies),
                                                                                     profile)
    assert [root["local_path"] for root in library["roots"]] == [str(api.movies)]
    assert library["roots"][0]["server_path"] is None


@pytest.mark.parametrize("change, message", [
    ({"name": " "}, "A name is required"),
    ({"name": None}, "A name is required"),
    ({"library": None}, "A library is required"),
    ({"kind": "jellyfin"}, "No media source kind"),
    ({"kind": "plex"}, "No media source kind"),
    ({"library": {"path": "relative", "content_kind": "movie"}}, "must be absolute"),
    ({"library": {"path": "/definitely/not/there", "content_kind": "movie"}}, "does not exist"),
    ({"library": {"path": "{movies}", "content_kind": "music"}}, "content_kind must be one of"),
    ({"library": {"path": "{movies}", "content_kind": "series"}}, "Only movie libraries"),
    ({"library": {"path": "{movies}", "content_kind": "movie", "profile_id": 99}}, "profile does not exist"),
    ({"scan_frequency_hours": -1}, "scan_frequency_hours"),
    ({"scan_frequency_hours": "6"}, "scan_frequency_hours"),
])
def test_a_request_that_cannot_work_is_refused_and_creates_nothing(api, change, message):
    from media_sources import repository

    change = {key: ({**value, "path": value["path"].format(movies=api.movies)} if isinstance(value, dict) else value)
              for key, value in change.items()}
    response = _create(api, **change)
    assert response.status_code == 400 and response.json["error"] == "invalid"
    assert message in response.json["message"]
    assert repository.list_sources() == []


def test_a_body_that_is_not_an_object_is_refused(api):
    response = api.client.post(SOURCES, data="[1]", content_type="application/json", headers=HEADERS)
    assert response.status_code == 400


def test_a_name_in_use_is_a_conflict(api):
    assert _create(api).status_code == 201
    assert _create(api).status_code == 409


def test_a_library_that_fails_to_save_leaves_no_source_behind(api, monkeypatch):
    from media_sources import repository, service

    def fail(**kwargs):
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(service.repository, "create_library", fail)
    with pytest.raises(RuntimeError):
        service.create_source({"name": "X", "library": {"path": str(api.movies), "content_kind": "movie"}})
    assert repository.list_sources() == []


def test_a_scan_is_queued_as_a_job_and_the_items_are_listed(api):
    from media_sources import scanner

    created = _create(api).json
    response = api.client.post(f"{SOURCES}/{created['id']}/scan", headers=HEADERS)
    assert response.status_code == 202 and response.json["job_id"] == 33 and "session" in response.json
    assert api.queued == ["Scanning media source Local movies"]
    assert api.client.post(f"{SOURCES}/999/scan", headers=HEADERS).status_code == 404

    video(api.movies / "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv")
    video(api.movies / "Alien.1979.mkv")
    scanner.run_scan(created["id"])
    listed = api.client.get(ITEMS, headers=HEADERS)
    assert listed.status_code == 200 and listed.json["total"] == 2
    first, second = listed.json["data"]
    assert (first["title"], second["title"]) == ("Alien", "The Matrix")
    assert (second["year"], second["identity_state"], second["kind"], second["subtitles"],
            second["missing_subtitles"]) == ("1999", "parsed", "movie", [], [])
    assert second["path"] == str(api.movies / "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv")


def test_the_item_list_pages_and_filters(api):
    from media_sources import scanner

    created = _create(api).json
    for number in range(5):
        video(api.movies / f"Film.{2000 + number}.mkv")
    scanner.run_scan(created["id"])
    page = api.client.get(ITEMS + "?start=1&length=2", headers=HEADERS).json
    assert (page["total"], [row["year"] for row in page["data"]]) == (5, ["2001", "2002"])
    assert api.client.get(ITEMS + "?length=-1", headers=HEADERS).json["total"] == 5
    assert api.client.get(ITEMS + f"?source_id={created['id'] + 1}", headers=HEADERS).json == {"data": [],
                                                                                                 "total": 0}
    assert api.client.get(ITEMS + "?kind=episode", headers=HEADERS).json["total"] == 0
    assert api.client.get(ITEMS + "?kind=song", headers=HEADERS).status_code == 400


def test_a_search_is_queued_through_the_jobs_queue(api):
    from media_sources import scanner

    created = _create(api).json
    video(api.movies / "Film.2001.mkv")
    scanner.run_scan(created["id"])
    (item,) = api.client.get(ITEMS, headers=HEADERS).json["data"]
    response = api.client.post(f"{ITEMS}/{item['id']}/search", headers=HEADERS)
    assert response.status_code == 202 and response.json["job_id"] == 33
    assert api.queued[-1] == "Searching subtitles for Film"
    assert api.client.post(f"{ITEMS}/9999/search", headers=HEADERS).status_code == 404


def test_the_swagger_document_lists_the_routes_and_the_job_response(api):
    spec = api.client.get("/api/swagger.json").json
    assert {"/system/media-sources", "/system/media-sources/{source_id}/scan", "/sources/items",
            "/sources/items/{item_id}/search"} <= set(spec["paths"])
    assert {"202", "404"} <= set(spec["paths"]["/system/media-sources/{source_id}/scan"]["post"]["responses"])
