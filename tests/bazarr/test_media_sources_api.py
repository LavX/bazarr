"""The first routes: create a folder source with a library, scan it, list its items and search one."""
import pytest
import sqlalchemy as sa
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
    ({"library": {"path": "{movies}", "content_kind": "sports"}}, "Only movie and series libraries"),
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


# Enabled flags and overlapping folders

def _scanned(api):
    from media_sources import scanner

    created = _create(api).json
    video(api.movies / "Film.2001.mkv")
    scanner.run_scan(created["id"])
    (item,) = api.client.get(ITEMS, headers=HEADERS).json["data"]
    return created, item


def _disable(api, table):
    from media_sources import repository
    api.db.execute(sa.update(table).values(enabled=0))
    return repository


@pytest.mark.parametrize("which", ["source", "library"])
def test_a_disabled_source_or_library_is_not_scanned_or_searched_through_the_api(api, which):
    from media_sources import repository

    created, item = _scanned(api)
    _disable(api, repository.Sources if which == "source" else repository.Libraries)
    api.queued.clear()

    scan = api.client.post(f"{SOURCES}/{created['id']}/scan", headers=HEADERS)
    search = api.client.post(f"{ITEMS}/{item['id']}/search", headers=HEADERS)

    assert scan.status_code == 409
    assert scan.json["error"] == "disabled"
    assert ("disabled" if which == "source" else "no enabled library") in scan.json["message"]
    assert search.status_code == 409 and "disabled" in search.json["message"]
    assert api.queued == []
    # Its items stay listed.
    assert api.client.get(ITEMS, headers=HEADERS).json["total"] == 1


@pytest.mark.parametrize("which", ["source", "library"])
def test_a_search_job_queued_before_the_flag_changed_does_nothing(api, monkeypatch, which):
    from media_sources import repository, service

    created, item = _scanned(api)
    _disable(api, repository.Sources if which == "source" else repository.Libraries)
    progress = []
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: progress.append(kwargs))
    monkeypatch.setattr(service, "get_providers", lambda: pytest.fail("a provider was asked"))
    assert service.search_source_item(item["id"], job_id=4) is None
    assert "disabled" in progress[-1]["progress_message"]


def test_a_library_over_a_folder_another_library_has_or_nests_with_is_refused(api, tmp_path):
    assert _create(api).status_code == 201
    inner = api.movies / "Inner"
    inner.mkdir()
    outer = tmp_path
    for number, path in enumerate((api.movies, inner, outer, str(api.movies) + "/", str(inner) + "/../Inner")):
        response = _create(api, name=f"Other {number}", library={"path": str(path), "content_kind": "movie"})
        assert response.status_code == 409, path
        assert response.json["error"] == "overlap"
        assert "Local movies" in response.json["message"] and str(api.movies) in response.json["message"]
    from media_sources import repository
    assert len(repository.list_sources()) == 1
    # A sibling is fine.
    sibling = tmp_path / "Movies2"
    sibling.mkdir()
    assert _create(api, name="Sibling", library={"path": str(sibling), "content_kind": "movie"}).status_code == 201


def test_the_path_is_stored_normalised(api):
    created = _create(api, library={"path": str(api.movies) + "/", "content_kind": "movie"}).json
    assert created["libraries"][0]["locator"] == str(api.movies)


def test_a_name_taken_between_the_check_and_the_insert_is_a_conflict_not_a_server_error(api, monkeypatch):
    from media_sources import service

    assert _create(api).status_code == 201
    other = api.tmp_path / "Other"
    other.mkdir()
    real, calls = service.repository.name_taken, []

    def stale_then_real(name, target=None):
        # The first look happens before the other request lands; the look after the failed insert sees it.
        calls.append(name)
        return False if len(calls) == 1 else real(name, target)

    monkeypatch.setattr(service.repository, "name_taken", stale_then_real)
    response = _create(api, library={"path": str(other), "content_kind": "movie"})
    assert response.status_code == 409 and "already exists" in response.json["message"]


@pytest.mark.parametrize("change", [{"enabled": "false"}, {"enabled": 0},
                                    {"library": {"path": "{movies}", "content_kind": "movie", "profile_id": 0}},
                                    {"library": {"path": "{movies}", "content_kind": "movie", "profile_id": "1"}},
                                    {"library": {"path": "{movies}", "content_kind": "movie", "profile_id": False}},
                                    {"library": {"path": "/tmp/bad\u0000path", "content_kind": "movie"}}])
def test_malformed_values_are_refused_with_a_400(api, change):
    change = {key: ({**value, "path": value["path"].format(movies=api.movies)} if isinstance(value, dict) else value)
              for key, value in change.items()}
    response = _create(api, **change)
    assert response.status_code == 400


# Overlap through links and under concurrency

def test_a_symlinked_second_root_to_the_same_folder_is_refused(api, tmp_path):
    assert _create(api).status_code == 201
    link = tmp_path / "alias"
    link.symlink_to(api.movies, target_is_directory=True)
    response = _create(api, name="Alias", library={"path": str(link), "content_kind": "movie"})
    assert response.status_code == 409 and response.json["error"] == "overlap"
    # Reaching into the existing root through a link is the same folder too.
    nested = _create(api, name="Nested", library={"path": str(link / "Sub"), "content_kind": "movie"})
    assert nested.status_code in (400, 409)
    (api.movies / "Sub").mkdir()
    nested = _create(api, name="Nested", library={"path": str(link / "Sub"), "content_kind": "movie"})
    assert nested.status_code == 409 and nested.json["error"] == "overlap"


def test_two_roots_with_the_same_device_and_inode_are_refused_whatever_their_spelling(api, tmp_path, monkeypatch):
    """A bind mount shows one folder at two paths that no string or realpath comparison relates."""
    from media_sources import repository

    assert _create(api).status_code == 201
    second = tmp_path / "Bound"
    second.mkdir()
    real = repository.folder_identity

    def same_as_movies(path):
        return real(str(api.movies)) if path == str(second) else real(path)

    monkeypatch.setattr(repository, "folder_identity", same_as_movies)
    response = _create(api, name="Bound", library={"path": str(second), "content_kind": "movie"})
    assert response.status_code == 409 and response.json["error"] == "overlap"


def test_a_double_leading_slash_is_the_same_folder(api):
    assert _create(api).status_code == 201
    response = _create(api, name="Slashes", library={"path": "/" + str(api.movies), "content_kind": "movie"})
    assert response.status_code == 409 and response.json["error"] == "overlap"


def test_two_overlapping_creations_at_once_cannot_both_succeed(api, monkeypatch):
    import threading

    from media_sources import repository, service

    inner = api.movies / "Inner"
    inner.mkdir()
    real = repository.overlapping_root
    barrier = threading.Barrier(2)

    def slow_check(path, target=None):
        found = real(path, target)
        try:
            barrier.wait(timeout=0.7)
        except threading.BrokenBarrierError:
            pass
        return found

    monkeypatch.setattr(service.repository, "overlapping_root", slow_check)
    outcomes = []

    def create(name, path):
        try:
            outcomes.append(service.create_source({"name": name, "library": {"path": str(path),
                                                                           "content_kind": "movie"}}))
        except service.SourceError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=create, args=("One", api.movies)),
               threading.Thread(target=create, args=("Two", inner))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(repository.list_sources()) == 1 and outcomes.count("overlap") == 1
