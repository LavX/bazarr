"""Source-only library ownership through the Discover API."""
import pytest

import test_discover_copy_matching as copies_fixtures
import test_discover_download as download_fixtures
import test_discover_summary as summary_fixtures

authenticated_client = copies_fixtures.authenticated_client
copy_database = copies_fixtures.copy_database
providers = copies_fixtures.providers
catalog_provider = copies_fixtures.catalog_provider
summary_database = summary_fixtures.summary_database
quiet_queue = summary_fixtures.quiet_queue

HEADERS = {"X-API-KEY": "discover-test-key"}


def add_source(session, kind, *, enabled=1, name=None):
    from app.database import TableMediaSourceLibraries, TableMediaSources

    source = TableMediaSources(kind="folder", name=name or f"Local {kind}", enabled=enabled)
    session.add(source)
    session.flush()
    library = TableMediaSourceLibraries(source_id=source.id, content_kind=kind, locator=f"/{kind}")
    session.add(library)
    session.flush()
    return source.id, library.id


def add_title(session, owner, model, **values):
    source_id, library_id = owner
    row = model(source_id=source_id, library_id=library_id, sort_title=values["title"],
                identity_source="user", identity_state="identified", **values)
    session.add(row)
    session.flush()
    return row.id


@pytest.fixture
def catalog_details(monkeypatch):
    from discover import metadata

    def movie_details(identity):
        return {"data": {"item": {"id": int(identity), "media_type": "movie", "source": "tmdb",
                                  "source_id": f"tmdb:movie:{identity}", "title": "Night Film"}}}

    def show_details(identity):
        return {"data": {"item": {"id": int(identity), "media_type": "show", "source": "tmdb",
                                  "source_id": f"tmdb:show:{identity}", "title": "Moon Show"}}}

    monkeypatch.setattr(metadata, "movie_details", movie_details)
    monkeypatch.setattr(metadata, "show_details", show_details)


def test_tmdb_movie_has_a_source_copy_through_details_offer_and_selection(
    authenticated_client, copy_database, catalog_details, providers, monkeypatch, tmp_path,
):
    from app.database import TableSourceItems
    from discover import metadata
    from discover.library import resolve_copy

    _, session = copy_database
    owner = add_source(session, "movie")
    path = tmp_path / "Night.Film.2024.1080p.mkv"
    path.write_bytes(b"movie file")
    item_id = add_title(session, owner, TableSourceItems, kind="movie", title="Night Film", path=str(path),
                        tmdb_id="42", year="2024")
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/movies/42?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    assert [(copy["local_id"], copy["source_id"]) for copy in response.json["data"]["item"]["copies"]] == [
        (item_id, owner[0]),
    ]
    local = authenticated_client.get(f"/api/discover/metadata/movies/source:{item_id}?source=local", headers=HEADERS)
    assert local.status_code == 200
    assert local.json["data"]["item"]["tmdb_id"] == 42
    unavailable = {"data": {"status": "unconfigured", "items": []}}
    monkeypatch.setattr(metadata, "search_movies", lambda _: unavailable)
    monkeypatch.setattr(metadata, "omdb_search", lambda *_: unavailable)
    search = authenticated_client.get("/api/discover/metadata/search?q=Night+Film&source=all", headers=HEADERS)
    assert search.status_code == 200
    assert [item["id"] for item in search.json["data"]["items"]] == [f"source:{item_id}"]

    target = {"media_type": "movie", "tmdb_id": 42}
    response = copies_fixtures.copies(authenticated_client, **target)
    assert response.status_code == 200
    [copy] = response.json["items"]
    assert copy["selectable"] is True
    assert copy["filename"] == path.name
    assert copy["source_id"] == owner[0]
    assert response.json["owning_titles"] == 1
    assert str(tmp_path) not in str(response.json)
    assert resolve_copy(copy["copy_id"], target)["path"] == str(path)
    providers.add("discover_source", copies_fixtures.catalog("discover_source", [("web", path.stem)]))
    selection = {**target, "copy_id": copy["copy_id"], "language": "eng"}
    response = copies_fixtures.post(authenticated_client, selection)
    assert response.status_code == 200
    assert response.json["context"]["copy"]["source_name"] == "Local movie"
    assert str(tmp_path) not in str(response.json)
    video = providers.videos[0][1]
    assert (video.name, video.title, video.tmdb_id, video.imdb_id) == (str(path), "Night Film", 42, None)
    conflict = copies_fixtures.post(authenticated_client, {**selection, "tmdb_id": 43})
    assert conflict.status_code == 409
    assert conflict.json["reason"] == "copy_unavailable"


def test_show_copy_uses_the_groups_identity_and_exact_episode(
    authenticated_client, copy_database, catalog_details, tmp_path,
):
    from app.database import TableSourceGroups, TableSourceItems

    _, session = copy_database
    owner = add_source(session, "series")
    group_id = add_title(session, owner, TableSourceGroups, kind="show", title="Moon Show", group_key="moon",
                         tmdb_id="84", imdb_id="tt0000084")
    episode_id = add_title(session, owner, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id,
                           tmdb_id="99", imdb_id="tt0000099", season=2, episode=3, episode_title="Eclipse",
                           path=str(tmp_path / "Moon.Show.S02E03.mkv"))
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/shows/84?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    item = response.json["data"]["item"]
    assert [copy["local_id"] for copy in item["copies"]] == [group_id]
    assert item["ownership"]["seasons_owned"] == [2]

    params = {"media_type": "episode", "imdb_id": "tt0000084", "season": 2, "episode": 3}
    response = copies_fixtures.copies(authenticated_client, **params)
    assert response.status_code == 200
    assert [(copy["local_id"], copy["series_local_id"], copy["episode_title"]) for copy in response.json["items"]] == [
        (episode_id, group_id, "Eclipse"),
    ]
    response = copies_fixtures.copies(authenticated_client, **{**params, "episode": 4})
    assert response.json["items"] == []
    assert response.json["owning_titles"] == 1
    response = copies_fixtures.copies(authenticated_client, **{**params, "imdb_id": "tt0000099"})
    assert response.json["items"] == []
    assert response.json["owning_titles"] == 0


@pytest.mark.parametrize("archive_mode", ["source", "sibling", "ambiguous", "pinned_sibling", "worker_sibling"])
def test_remapped_episode_uses_only_the_selected_source_archive_member(
    authenticated_client, copy_database, providers, monkeypatch, tmp_path, archive_mode,
):
    from app.database import TableSourceGroups, TableSourceItems
    from discover import metadata
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.extensions import provider_registry
    from test_provider_hub_archive_candidates import ArchiveSearchWorker, archive_payload

    _, session = copy_database
    owner = add_source(session, "series")
    group_id = add_title(session, owner, TableSourceGroups, kind="show", title="Breaking Bad", group_key="show",
                         tmdb_id="84", imdb_id="tt0903747")
    wrong = tmp_path / "Breaking.Bad.S02E01.mkv"
    correct = tmp_path / "Breaking.Bad.S01E03.mkv"
    wrong.write_bytes(b"different episode")
    correct.write_bytes(b"wanted episode")
    wrong_id = add_title(session, owner, TableSourceItems, kind="episode", title="Different episode", group_id=group_id,
                         season=2, episode=1, path=str(wrong))
    right_id = add_title(session, owner, TableSourceItems, kind="episode", title="Wanted episode", group_id=group_id,
                         tmdb_id="903", season=1, episode=3, path=str(correct))
    copies_fixtures.add_instance(session, 1, "Sonarr", kind="sonarr")
    copies_fixtures.add_episode(session, 8, 1, 40, tmp_path / "arr.mkv", season=2, episode=1)
    session.commit()

    canonical = {"source": "tmdb", "id": 903, "show_id": 84, "season": 1, "episode": 3,
                 "target_season": 2, "target_episode": 1, "identity_status": "resolved", "show_imdb_id": "tt0903747",
                 "show_title": "Breaking Bad", "show_year": 2008, "tvdb_id": 1203, "imdb_id": "tt0000903"}
    monkeypatch.setattr(metadata, "_episode_details_with_validity", lambda *_: ({"data": {"episode": canonical}}, float("inf")))
    manual = {"media_type": "episode", "imdb_id": "tt0903747", "tmdb_id": 84, "season": 2, "episode": 1}
    response = copies_fixtures.copies(authenticated_client, **manual)
    manual_source = next(copy for copy in response.json["items"] if copy["local_id"] == wrong_id)
    verified = {**manual, "source_season": 1, "source_episode": 3, "episode_tmdb_id": 903,
                "episode_tvdb_id": 1203, "episode_imdb_id": "tt0000903"}
    response = copies_fixtures.copies(authenticated_client, **verified)
    assert response.status_code == 200
    assert [copy["local_id"] for copy in response.json["items"]] == [8, right_id]
    resolved_source = response.json["items"][1]

    sibling_srt = copies_fixtures.SRT.replace(b"Chosen copy dialogue", b"Sibling episode dialogue")
    members = {f"{wrong.stem}.srt": sibling_srt}
    if archive_mode != "sibling":
        members[f"{correct.stem}.srt"] = copies_fixtures.SRT
    if archive_mode == "ambiguous":
        members[f"{correct.stem}.Other.srt"] = sibling_srt
    extra = {"pinned_sibling": {"member": f"{wrong.stem}.srt"},
             "worker_sibling": {"select_member": True}}.get(archive_mode, {})
    worker = ArchiveSearchWorker(archive_payload(members, **extra),
                                 selector={"decision": "pin", "member": f"{wrong.stem}.srt"})
    name = providers.add("discover_source")
    cls = provider_registry[name]
    monkeypatch.setattr(cls, "list_subtitles", HubProxyProvider.list_subtitles)
    monkeypatch.setattr(cls, "_worker", lambda _: worker)
    monkeypatch.setattr(cls, "_gated_request", lambda _, operation, payload, timeout: worker.request(operation, payload, timeout))
    monkeypatch.setattr("app.jobs_queue.event_stream", lambda **kwargs: None)
    selection = {**manual, "language": "eng", "title": "Breaking Bad", "year": 2008, "episode_identity": canonical}
    refusal = copies_fixtures.post(authenticated_client, {**selection, "copy_id": manual_source["copy_id"]})
    assert refusal.status_code == 409
    assert worker.searches == []
    response = copies_fixtures.post(authenticated_client, {**selection, "copy_id": resolved_source["copy_id"]})
    assert response.status_code == 200
    [video] = worker.searches
    assert (video["season"], video["episode"]) == (2, 1)
    assert response.json["context"]["copy"]["filename"] == correct.name
    [result] = response.json["results"]
    download = download_fixtures.get(authenticated_client, result)
    if archive_mode == "source":
        assert download.status_code == 200
        assert download.data == copies_fixtures.SRT
    else:
        assert download.status_code == 502
        assert download.json["reason"] == "archive_no_match"


def test_tmdb_only_catalog_episode_resolves_a_manually_confirmed_source_copy(
    authenticated_client, copy_database, providers, monkeypatch, tmp_path,
):
    from app.database import TableSourceGroups, TableSourceItems
    from discover import metadata

    _, session = copy_database
    owner = add_source(session, "series")
    group_id = add_title(session, owner, TableSourceGroups, kind="show", title="Moon Show", group_key="moon",
                         tmdb_id="84", year="2008")
    path = tmp_path / "Moon.Show.S02E03.mkv"
    path.write_bytes(b"source episode")
    add_title(session, owner, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id,
              season=2, episode=3, path=str(path))
    session.commit()

    canonical = {"source": "tmdb", "id": 903, "show_id": 84, "season": 2, "episode": 3,
                 "target_season": None, "target_episode": None, "identity_status": "unverified",
                 "show_imdb_id": None, "show_title": "Moon Show", "show_year": 2008}
    monkeypatch.setattr(metadata, "_episode_details_with_validity", lambda *_: ({"data": {"episode": canonical}}, float("inf")))
    providers.add("discover_source", copies_fixtures.catalog("discover_source", [("web", path.stem)]))
    target = {"media_type": "episode", "tmdb_id": 84, "season": 2, "episode": 3}
    [copy] = copies_fixtures.copies(authenticated_client, **target).json["items"]
    selection = {**target, "copy_id": copy["copy_id"], "show_id": 84, "episode_identity": canonical,
                 "language": "eng", "title": "Moon Show", "year": 2008}
    assert copies_fixtures.post(authenticated_client, selection).status_code == 400
    response = copies_fixtures.post(authenticated_client, {**selection, "manual_confirmed": True})
    assert response.status_code == 200
    [(_, video, _)] = providers.videos
    assert (video.name, video.series_tmdb_id, video.series_imdb_id, video.season, video.episode) == (
        str(path), 84, None, 2, 3,
    )


def test_unidentified_disabled_and_shadowed_source_rows_do_not_create_copy_options(
    authenticated_client, copy_database, catalog_details, tmp_path,
):
    from app.database import TableSourceItems

    _, session = copy_database
    owner = add_source(session, "movie")
    copies_fixtures.add_instance(session, 1, "Radarr")
    copies_fixtures.add_movie(session, 8, 1, tmp_path / "shared.mkv")
    add_title(session, owner, TableSourceItems, kind="movie", title="Night Film", path=str(tmp_path / "unknown.mkv"))
    add_title(session, (owner[0], owner[1]), TableSourceItems, kind="movie", title="Night Film",
              path=str(tmp_path / "shared.mkv"), imdb_id="tt0133093", arr_shadow=1, shadow_instance_id=1,
              shadow_arr_table="movie", shadow_arr_row_id=8)
    disabled = add_source(session, "movie", enabled=0, name="Disabled movies")
    add_title(session, disabled, TableSourceItems, kind="movie", title="Night Film", path=str(tmp_path / "disabled.mkv"),
              tmdb_id="42", imdb_id="tt0133093")
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/movies/42?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    assert response.json["data"]["item"]["copies"] == []
    response = copies_fixtures.copies(authenticated_client, media_type="movie", imdb_id="tt0133093")
    assert [copy["local_id"] for copy in response.json["items"]] == [8]
    assert response.json["owning_titles"] == 1


@pytest.mark.parametrize("arr_tmdb", ["42", "0", "missing"])
def test_a_distinct_source_copy_and_arr_copy_can_share_a_row_id(
    authenticated_client, copy_database, catalog_details, tmp_path, arr_tmdb, monkeypatch,
):
    from app.database import TableMovies, TableSourceItems
    from discover import metadata
    from discover.library import resolve_copy

    catalog = metadata.movie_details("42")
    catalog["data"]["item"]["imdb_id"] = "tt0133093"
    monkeypatch.setattr(metadata, "movie_details", lambda _: catalog)

    _, session = copy_database
    owner = add_source(session, "movie")
    item_id = add_title(session, owner, TableSourceItems, kind="movie", title="Night Film",
                        path=str(tmp_path / "disc.mkv"), tmdb_id="42", imdb_id="tt0133093")
    copies_fixtures.add_instance(session, 1, "Radarr")
    arr_file = tmp_path / "web.mkv"
    arr_file.write_bytes(b"radarr copy")
    session.add(TableMovies(id=item_id, arr_instance_id=1, radarrId=100, imdbId="tt0133093", tmdbId=arr_tmdb,
                            title="Night Film", path=str(tmp_path / "web.mkv")))
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/movies/42?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    assert [(copy["local_id"], copy.get("source_id")) for copy in response.json["data"]["item"]["copies"]] == [
        (item_id, None), (item_id, owner[0]),
    ]
    target = {"media_type": "movie", "tmdb_id": 42, "imdb_id": "tt0133093"}
    response = copies_fixtures.copies(authenticated_client, **target)
    assert [copy["copy_id"] for copy in response.json["items"]] == [f"c1.movie.{item_id}.1", f"c1.movie.{item_id}.s{owner[0]}"]
    assert response.json["owning_titles"] == 2
    assert resolve_copy(f"c1.movie.{item_id}.1", target)["path"] == str(arr_file)
    if arr_tmdb == "42":
        conflict = copies_fixtures.copies(authenticated_client, **{**target, "tmdb_id": 43})
        assert conflict.json["items"] == []
        assert conflict.json["owning_titles"] == 0


def test_scanned_source_copies_download_the_chosen_result_and_never_substitute_a_sibling(
    authenticated_client, source_db, catalog_provider, providers, monkeypatch, tmp_path,
):
    from app import database
    from discover import metadata
    from media_sources import indexer, scanner

    def movie(identity, year):
        return {"id": identity, "source": "tmdb", "media_type": "movie", "title": "The Matrix", "year": year}

    monkeypatch.setattr(indexer, "get_language_set", lambda: set())
    monkeypatch.setattr(database, "engine", source_db.get_bind())
    monkeypatch.setattr("app.jobs_queue.event_stream", lambda **kwargs: None)
    monkeypatch.setattr(metadata, "search_movies", lambda *args, **kwargs: {
        "data": {"status": "available", "items": [movie(603, 1999), movie(604, 2003)]},
    })
    monkeypatch.setattr(metadata, "movie_details", lambda _: {
        "data": {"status": "available", "item": movie(603, 1999)},
    })
    library = tmp_path / "library"
    chosen_file = copies_fixtures.media_file(library, "The.Matrix.1080p.WEB.mkv", b"first copy")
    sibling_file = copies_fixtures.media_file(library, "The.Matrix.2160p.BluRay.mkv", b"second copy")

    created = authenticated_client.post("/api/system/media-sources", headers=HEADERS,
                                       json={"name": "Local films", "library": {"path": str(library), "content_kind": "movie"}})
    assert created.status_code == 201
    source_id = created.json["id"]
    assert scanner.run_scan(source_id)[0].status == "ok"
    listed = authenticated_client.get("/api/sources/items", headers=HEADERS, query_string={"source_id": source_id})
    assert listed.status_code == 200
    assert len(listed.json["data"]) == 2
    for row in listed.json["data"]:
        assert row["identity_state"] == "ambiguous"
        picked = authenticated_client.patch(f"/api/sources/items/{row['id']}", headers=HEADERS, json={"candidate_id": 603})
        assert picked.status_code == 200
        assert picked.json["tmdb_id"] == "603"

    target = {"media_type": "movie", "tmdb_id": 603}
    offer = copies_fixtures.copies(authenticated_client, **target)
    assert offer.status_code == 200
    offered = {copy["filename"]: copy["copy_id"] for copy in offer.json["items"]}
    assert set(offered) == {chosen_file.name, sibling_file.name}
    chosen = copies_fixtures.post(authenticated_client, {**target, "language": "eng", "copy_id": offered[chosen_file.name]})
    assert chosen.status_code == 200
    assert copies_fixtures.searched(providers) == [str(chosen_file)]
    result = next(row for row in chosen.json["results"] if "1080p" in row["release"])
    download = download_fixtures.get(authenticated_client, result)
    assert download.status_code == 200
    assert download.data == copies_fixtures.SRT
    assert catalog_provider[1] == ["web"]

    queued = download_fixtures.enqueue(authenticated_client, result)
    ticket = queued.json["job_id"]
    job = download_fixtures.run_queued_job(ticket)
    assert job["status"] == "completed"

    chosen_file.unlink()
    save = authenticated_client.get("/api/discover/download", query_string={"job": ticket}, headers=HEADERS)
    assert save.status_code == 404
    assert save.json["reason"] == "ticket_expired"
    expired = download_fixtures.get(authenticated_client, result)
    assert expired.status_code == 410
    assert expired.json["reason"] == "result_expired"
    assert catalog_provider[1] == ["web"]
    assert sibling_file.is_file()
    sibling = copies_fixtures.post(authenticated_client, {**target, "language": "eng", "copy_id": offered[sibling_file.name]})
    assert sibling.status_code == 200
    assert copies_fixtures.searched(providers) == [str(chosen_file), str(sibling_file)]


def test_queued_search_rechecks_the_chosen_source_file_before_provider_admission(
    authenticated_client, copy_database, providers, monkeypatch, tmp_path,
):
    import app.jobs_queue as queue_module
    from app.database import TableSourceItems

    _, session = copy_database
    owner = add_source(session, "movie")
    chosen = tmp_path / "chosen.mkv"
    sibling = tmp_path / "sibling.mkv"
    for path in (chosen, sibling):
        path.write_bytes(b"fixture")
        add_title(session, owner, TableSourceItems, kind="movie", title="Night Film", path=str(path), tmdb_id="42")
    session.commit()
    providers.add("discover_source", copies_fixtures.catalog("discover_source", [("web", chosen.stem)]))
    real_queue = authenticated_client.inline_jobs.real
    monkeypatch.setattr(queue_module, "jobs_queue", real_queue)
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(real_queue, "force_start_pending_job", lambda *_: None)
    target = {"media_type": "movie", "tmdb_id": 42}
    copies = copies_fixtures.copies(authenticated_client, **target).json["items"]
    token = next(item["copy_id"] for item in copies if item["filename"] == chosen.name)
    queued = authenticated_client.post("/api/discover/search", headers=HEADERS,
                                       json={**target, "language": "eng", "copy_id": token})
    assert queued.status_code == 202
    chosen.unlink()
    job = download_fixtures.run_queued_job(queued.json["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["reason"] == "copy_unavailable"
    assert providers.videos == []
    assert sibling.is_file()


def test_summary_counts_source_files_once(authenticated_client, summary_database, quiet_queue, tmp_path):
    from app.database import TableSourceGroups, TableSourceItems

    session = summary_database.session
    movies = add_source(session, "movie")
    shows = add_source(session, "series")
    group_id = add_title(session, shows, TableSourceGroups, kind="show", title="Moon Show", group_key="moon",
                         tmdb_id="84")
    add_title(session, movies, TableSourceItems, kind="movie", title="Night Film", path=str(tmp_path / "movie.mkv"),
              tmdb_id="42")
    add_title(session, shows, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id, season=1,
              episode=1, path=str(tmp_path / "episode.mkv"))
    add_title(session, shows, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id, season=1,
              episode=2, path=str(tmp_path / "managed.mkv"), arr_shadow=1)
    session.commit()

    response = authenticated_client.get("/api/discover/summary", headers=HEADERS)
    assert response.status_code == 200
    library = response.json["library"]
    assert (library["movies"], library["series"], library["episodes"]) == (1, 1, 1)
