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
    # The scheduled tasks are rebuilt after every change that alters what is enabled; here that is only counted.
    refreshes = []
    monkeypatch.setattr(service, "refresh_tasks", lambda: refreshes.append(True))
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    movies = tmp_path / "Movies"
    movies.mkdir()
    return type("Api", (), {"client": app.test_client(), "db": source_db, "movies": movies, "queued": queued,
                            "tmp_path": tmp_path, "refreshes": refreshes})


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
    ({"kind": "jellyfin"}, "A saved media server connection is required"),
    ({"kind": "plex"}, "No media source kind"),
    ({"library": {"path": "relative", "content_kind": "movie"}}, "must be absolute"),
    ({"library": {"path": "/definitely/not/there", "content_kind": "movie"}}, "does not exist"),
    ({"library": {"path": "{movies}", "content_kind": "music"}}, "content_kind must be one of"),
    ({"library": {"path": "{movies}", "content_kind": "music"}}, "content_kind must be one of"),
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


# Editing, enabling and removing sources and libraries, and checking a folder

def test_a_source_is_listed_read_edited_and_removed(api):
    from media_sources import repository

    created = _create(api).json
    source_url = f"{SOURCES}/{created['id']}"
    assert [row["name"] for row in api.client.get(SOURCES, headers=HEADERS).json] == ["Local movies"]
    assert api.client.get(source_url, headers=HEADERS).json["libraries"][0]["locator"] == str(api.movies)
    assert api.client.get(f"{SOURCES}/999", headers=HEADERS).status_code == 404

    edited = api.client.patch(source_url, json={"name": "Renamed", "enabled": False, "scan_frequency_hours": 0},
                              headers=HEADERS)
    assert edited.status_code == 200
    assert (edited.json["name"], edited.json["enabled"], edited.json["scan_frequency_hours"]) == ("Renamed", False, 0)
    assert len(api.refreshes) == 2  # the create, then the edit
    for body, status in (({"name": " "}, 400), ({"enabled": "yes"}, 400), ({"scan_frequency_hours": -1}, 400),
                         ({"kind": "emby"}, 400)):
        assert api.client.patch(source_url, json=body, headers=HEADERS).status_code == status
    other = _create(api, name="Other", library={"path": str(api.tmp_path / "Other"), "content_kind": "movie"})
    assert other.status_code == 400
    (api.tmp_path / "Other").mkdir()
    assert _create(api, name="Other", library={"path": str(api.tmp_path / "Other"), "content_kind": "movie"}
                   ).status_code == 201
    assert api.client.patch(source_url, json={"name": "Other"}, headers=HEADERS).status_code == 409

    assert api.client.delete(source_url, headers=HEADERS).status_code == 204
    assert api.client.delete(source_url, headers=HEADERS).status_code == 404
    assert [row.name for row in repository.list_sources()] == ["Other"]
    assert len(api.refreshes) >= 4


@pytest.mark.parametrize("content_kind,item_kind", [("movie", "movie"), ("series", "episode"),
                                                     ("sports", "sports_event")])
def test_navigation_availability_keeps_browsing_and_searching_separate(api, content_kind, item_kind):
    from app.config import get_settings
    from media_sources import repository

    assert get_settings()["media_sources"]["visible"] is False
    hidden = {"browsable": False, "searchable": False, "retained": False}

    def availability():
        response = api.client.get(SOURCES + "?availability=1", headers=HEADERS)
        assert response.status_code == 200
        all_kinds = response.json["availability"]
        assert all_kinds.keys() == {"movie", "series", "sports"}
        assert all(value == hidden for kind, value in all_kinds.items() if kind != content_kind)
        return all_kinds[content_kind]

    assert availability() == hidden
    source = _create(api).json
    source_id = source["id"]
    library_id = source["libraries"][0]["id"]
    api.db.execute(sa.update(repository.Libraries).where(repository.Libraries.c.id == library_id)
                   .values(content_kind=content_kind))
    assert availability() == {**hidden, "browsable": True, "searchable": True}

    source_url = f"{SOURCES}/{source_id}"
    library_url = f"{source_url}/libraries/{library_id}"
    assert api.client.patch(library_url, json={"enabled": False}, headers=HEADERS).status_code == 200
    assert availability() == {**hidden, "browsable": True}
    assert api.client.patch(library_url, json={"enabled": True}, headers=HEADERS).status_code == 200
    assert api.client.patch(source_url, json={"enabled": False}, headers=HEADERS).status_code == 200
    assert availability() == {**hidden, "browsable": True}

    api.db.execute(sa.insert(repository.History).values(
        source_id=source_id, item_kind=item_kind, action=1, description="Downloaded"))
    api.db.execute(sa.insert(repository.Blacklist).values(source_id=source_id, item_kind=item_kind))
    assert api.client.delete(source_url, headers=HEADERS).status_code == 204
    assert availability() == {**hidden, "retained": True}

    api.db.execute(sa.delete(repository.History))
    assert availability() == {**hidden, "retained": True}
    api.db.execute(sa.delete(repository.Blacklist))
    assert availability() == hidden


def test_list_omits_a_source_removed_after_it_was_enumerated(api, monkeypatch):
    from media_sources import repository

    created = _create(api).json
    source_id = created["id"]
    api.db.execute(sa.insert(repository.History).values(
        source_id=source_id, item_kind="movie", action=1, description="Downloaded"))
    list_sources = repository.list_sources

    def remove_after_list(target=None):
        sources = list_sources(target)
        repository.delete_source(source_id, target)
        return sources

    monkeypatch.setattr(repository, "list_sources", remove_after_list)
    response = api.client.get(SOURCES + "?availability=1", headers=HEADERS)
    assert response.status_code == 200
    assert response.json["data"] == []
    assert response.json["availability"]["movie"] == {
        "browsable": False, "searchable": False, "retained": True,
    }


def test_a_library_is_added_edited_and_removed_and_its_path_cannot_change(api):
    from media_sources import repository

    created = _create(api).json
    base = f"{SOURCES}/{created['id']}/libraries"
    series = api.tmp_path / "TV"
    series.mkdir()

    added = api.client.post(base, json={"path": str(series), "content_kind": "series"}, headers=HEADERS)
    assert added.status_code == 201 and added.json["content_kind"] == "series"
    library_url = f"{base}/{added.json['id']}"
    assert [root["local_path"] for root in added.json["roots"]] == [str(series)]

    # Overlapping folders are refused with a message that names the other folder.
    (series / "Show").mkdir()
    clash = api.client.post(base, json={"path": str(series / "Show"), "content_kind": "series"}, headers=HEADERS)
    assert clash.status_code == 409 and str(series) in clash.json["message"] and clash.json["error"] == "overlap"
    assert api.client.post(base, json={"path": str(api.movies), "content_kind": "movie"},
                           headers=HEADERS).status_code == 409

    profile = add_profile(api.db, [("en", "False", "False")])
    edited = api.client.patch(library_url, json={"enabled": False, "profile_id": profile, "name": "Series"},
                              headers=HEADERS)
    assert edited.status_code == 200 and edited.json["enabled"] is False and edited.json["profile_id"] == profile
    for path_field in ("path", "locator"):
        refused = api.client.patch(library_url, json={path_field: str(api.movies)}, headers=HEADERS)
        assert refused.status_code == 400 and refused.json["error"] == "immutable"
        assert "cannot be edited" in refused.json["message"]
    assert repository.get_library(added.json["id"]).locator == str(series)
    assert api.client.patch(library_url, json={"profile_id": 99}, headers=HEADERS).status_code == 400
    assert api.client.patch(f"{base}/999", json={"enabled": True}, headers=HEADERS).status_code == 404
    assert api.client.patch(f"{SOURCES}/999/libraries/{added.json['id']}", json={"enabled": True},
                            headers=HEADERS).status_code == 404

    assert api.client.delete(library_url, headers=HEADERS).status_code == 204
    assert api.client.delete(library_url, headers=HEADERS).status_code == 404
    assert [row.id for row in repository.list_libraries(created["id"])] == [created["libraries"][0]["id"]]


def test_removing_a_library_or_a_source_keeps_the_history_it_wrote(api):
    from media_sources import repository, scanner

    created = _create(api).json
    video(api.movies / "Film.2001.mkv")
    scanner.run_scan(created["id"])
    (item,) = repository.list_items(source_id=created["id"])
    repository.insert_history(api.db, item_id=item.id, source_id=created["id"], item_kind="movie",
                              item_title="Film", source_name="Local movies", action=1, description="downloaded",
                              video_path=item.path)

    library_id = created["libraries"][0]["id"]
    assert api.client.delete(f"{SOURCES}/{created['id']}/libraries/{library_id}", headers=HEADERS).status_code == 204
    assert repository.count_items(source_id=created["id"]) == 0
    (history,) = api.db.execute(sa.select(repository.History)).all()
    assert (history.item_id, history.item_title, history.source_name, history.video_path) == (
        None, "Film", "Local movies", item.path)


def test_a_library_scan_is_queued_for_that_library_only(api):
    created = _create(api).json
    library_id = created["libraries"][0]["id"]
    url = f"{SOURCES}/{created['id']}/libraries/{library_id}/scan"
    assert api.client.post(url, headers=HEADERS).status_code == 202
    assert api.client.post(f"{SOURCES}/{created['id']}/libraries/999/scan", headers=HEADERS).status_code == 404
    api.db.execute(sa.text("UPDATE media_source_libraries SET enabled = 0"))
    assert api.client.post(url, headers=HEADERS).status_code == 409


def test_validate_path_says_what_refuses_a_folder_and_what_only_warns(api, monkeypatch):
    from app.database import TableArrInstances, TableMoviesRootfolder

    url = f"{SOURCES}/validate-path"
    api.db.execute(sa.insert(TableArrInstances).values(
        id=1, kind="radarr", name="Radarr", stable_key="r", port=7878, enabled=1, is_default=0,
        path_mappings=f'[["/arr/movies", "{api.tmp_path / "Arr"}"]]'))
    api.db.execute(sa.insert(TableMoviesRootfolder).values(arr_instance_id=1, upstream_rootfolder_id=1,
                                                           path="/arr/movies", accessible=1))
    (api.tmp_path / "Arr" / "Inside").mkdir(parents=True)
    fresh = api.tmp_path / "Fresh"
    fresh.mkdir()

    clean = api.client.post(url, json={"path": str(fresh), "content_kind": "movie"}, headers=HEADERS)
    assert clean.status_code == 200 and clean.json["ok"] is True and clean.json["warnings"] == []

    warned = api.client.post(url, json={"path": str(api.tmp_path / "Arr" / "Inside")}, headers=HEADERS).json
    assert warned["ok"] is True and "root folder of Radarr" in warned["warnings"][0]
    containing = api.client.post(url, json={"path": str(api.tmp_path)}, headers=HEADERS).json
    assert any("root folder of Radarr" in warning for warning in containing["warnings"])

    refused = api.client.post(url, json={"path": "relative"}, headers=HEADERS).json
    assert refused["ok"] is False and refused["errors"][0]["code"] == "not_absolute"
    assert api.client.post(url, json={"path": str(fresh), "content_kind": "music"}, headers=HEADERS).status_code == 400

    _create(api)
    (api.movies / "Sub").mkdir()
    clash = api.client.post(url, json={"path": str(api.movies / "Sub")}, headers=HEADERS).json
    assert clash["ok"] is False and clash["errors"][0]["code"] == "overlap"
    assert str(api.movies) in clash["errors"][0]["message"]

    monkeypatch.setattr("media_sources.paths.os.access", lambda path, mode: not (mode & 2))
    unwritable = api.client.post(url, json={"path": str(fresh)}, headers=HEADERS).json
    assert unwritable["ok"] is True and "not writable" in unwritable["warnings"][0]


def test_editing_the_roots_of_a_server_library_deletes_only_what_is_out_of_scope_and_still_on_disk(api, monkeypatch):
    """The folder kind refuses the edit; the server kinds (their libraries come with them) use this rule."""
    from media_sources import repository, scanner, service

    created = _create(api).json
    library_id = created["libraries"][0]["id"]
    inside = video(api.movies / "In.2001.mkv")
    outside_still_there = video(api.movies / "Moved" / "Out.2002.mkv")
    outside_gone = video(api.movies / "Gone" / "Away.2003.mkv")
    scanner.run_scan(created["id"])
    for path in (inside, outside_still_there, outside_gone):
        assert repository.get_item_by_path(str(path)) is not None
    outside_gone.unlink()
    # A folder library's root is its path, which cannot be edited.
    with pytest.raises(service.SourceError) as refused:
        service.change_library_roots(created["id"], library_id, [{"local_path": str(api.movies)}])
    assert refused.value.code == "immutable"

    queued = []
    monkeypatch.setattr(service, "queue_scan", lambda source_id, library_id=None: queued.append((source_id, library_id)))
    monkeypatch.setattr(repository, "get_source", lambda source_id, target=None: type(
        "Source", (), {"kind": "jellyfin", "id": source_id})())
    # Narrow the library to one folder that holds none of the files.
    narrow = api.movies / "Only"
    narrow.mkdir()
    deleted = service.change_library_roots(created["id"], library_id, [
        {"server_path": "/server/only", "local_path": str(narrow)}], confirmed_removed=2)

    # Both files that are still on disk are out of scope and deleted; the one whose path is gone is left alone,
    # so that a mount that moved can have its rows re-pathed by the next scan.
    assert deleted == 2
    assert repository.get_item_by_path(str(inside)) is None
    assert repository.get_item_by_path(str(outside_still_there)) is None
    assert repository.get_item_by_path(str(outside_gone)) is not None
    assert [(root.server_path, root.local_path) for root in repository.get_roots(library_id)] == [
        ("/server/only", str(narrow))]
    assert queued == [(created["id"], library_id)]
    # An edit that changes only the server path deletes nothing.
    assert service.change_library_roots(created["id"], library_id, [
        {"server_path": "/elsewhere", "local_path": str(narrow)}]) == 0
