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


def archive_provider(providers, monkeypatch, payload, selector=None):
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.extensions import provider_registry
    from test_provider_hub_archive_candidates import ArchiveSearchWorker

    worker = ArchiveSearchWorker(payload, selector=selector)
    cls = provider_registry[providers.add("discover_source")]
    monkeypatch.setattr(cls, "list_subtitles", HubProxyProvider.list_subtitles)
    monkeypatch.setattr(cls, "worker_client", worker, raising=False)
    monkeypatch.setattr("app.jobs_queue.event_stream", lambda **kwargs: None)
    return worker


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


@pytest.fixture(scope="module")
def source_language_modules():
    """Import cached database references before function-scoped databases replace them."""
    from languages import custom_lang, get_languages

    return custom_lang, get_languages


@pytest.fixture
def source_languages(source_language_modules, source_db):
    """Run language startup against this source database and own its lookup cache."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    custom_lang, get_languages = source_language_modules
    insert = pg_insert if source_db.get_bind().dialect.name == "postgresql" else sqlite_insert
    missing = object()
    previous = getattr(get_languages, "languages_dict", missing)
    initialized = missing
    try:
        # Both modules cache the startup session and its dialect-specific insert.
        # Restore those references before the test can import another consumer.
        with pytest.MonkeyPatch.context() as patch:
            for module in (get_languages, custom_lang):
                patch.setattr(module, "database", source_db)
                patch.setattr(module, "insert", insert)
            get_languages.load_language_in_db()
            initialized = get_languages.languages_dict
        yield
    finally:
        if initialized is not missing and getattr(get_languages, "languages_dict", missing) is initialized:
            if previous is missing:
                del get_languages.languages_dict
            else:
                get_languages.languages_dict = previous


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


@pytest.mark.parametrize("identity_matches", [True, False], ids=["unchanged", "mismatch"])
def test_selected_source_copy_requires_matching_identity_at_worker_admission(
    authenticated_client, source_db, providers, monkeypatch, tmp_path, identity_matches,
):
    from pathlib import Path
    from types import SimpleNamespace

    from app import database as db
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import capabilities, registry
    from provider_hub.manifest import validate_manifest
    from provider_hub.worker import ProviderWorkerClient, WorkerResult
    from subliminal_patch.extensions import provider_registry

    monkeypatch.setattr(db, "engine", source_db.get_bind())
    owner = add_source(source_db, "movie")
    path = tmp_path / "Night.Film.mkv"
    path.write_bytes(b"movie file")
    source_db.add(db.TableMediaSourceLibraryRoots(library_id=owner[1], local_path=str(tmp_path)))
    add_title(source_db, owner, db.TableSourceItems, kind="movie", title="Night Film", path=str(path), tmdb_id="42")
    source_db.commit()

    if not identity_matches:
        original_lstat = Path.lstat
        real_path = path.resolve()

        def late_stat(candidate):
            info = original_lstat(candidate)
            if candidate == real_path:
                # Only the late file check sees the fixture's alternate identity.
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                       st_dev=info.st_dev, st_ino=info.st_ino + 1)
            return info

        monkeypatch.setattr(Path, "lstat", late_stat)

    source_grants = []
    worker = ProviderWorkerClient(command=[])

    def request_locked(operation, payload, timeout, *, source_media):
        assert operation == "search"
        source_grants.append(source_media)
        return WorkerResult(ok=True, payload={"candidates": []}, events=[])

    monkeypatch.setattr(worker, "_request_locked", request_locked)
    name = providers.add("discover_source")
    cls = provider_registry[name]
    raw = provider_manifest_v2(name, permissions=["media_read"])
    monkeypatch.setattr(cls, "manifest", validate_manifest(raw), raising=False)
    monkeypatch.setattr(cls, "list_subtitles", registry.HubProxyProvider.list_subtitles)
    monkeypatch.setattr(cls, "worker_client", worker, raising=False)
    monkeypatch.setattr(capabilities, "live_row", lambda provider: {"enabled": True, "manifest": raw})

    target = {"media_type": "movie", "tmdb_id": 42}
    [copy] = copies_fixtures.copies(authenticated_client, **target).json["items"]
    response = copies_fixtures.post(authenticated_client, {**target, "copy_id": copy["copy_id"], "language": "eng"})
    assert response.status_code == 200
    [outcome] = response.json["coverage"]["providers"]
    if identity_matches:
        assert outcome["status"] == "empty"
        [grant] = source_grants
        assert grant.path == path.resolve()
        assert grant.identity == (path.stat().st_dev, path.stat().st_ino)
    else:
        assert (outcome["status"], outcome["reason"]) == ("skipped", "provider_excluded")
        assert source_grants == []


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


@pytest.mark.parametrize("kind", ["movie", "show"])
@pytest.mark.parametrize("source_tmdb", [84, 99], ids=["agreeing", "contradictory"])
def test_catalog_ownership_keeps_contradictory_partial_source_groups_separate(
    authenticated_client, copy_database, monkeypatch, tmp_path, kind, source_tmdb,
):
    from app.database import TableSourceGroups, TableSourceItems
    from discover import metadata

    _, session = copy_database
    owner = add_source(session, "movie" if kind == "movie" else "series")
    ids = []
    episode_ids = []
    imdb = "tt0000084"
    for season, tmdb_id in enumerate((None, f"00{source_tmdb}"), 1):
        path = tmp_path / f"Moon.{season}.mkv"
        path.write_bytes(b"fixture media")
        title = {"kind": kind, "title": f"Moon {season}", "imdb_id": imdb, "tmdb_id": tmdb_id}
        if kind == "movie":
            ids.append(add_title(session, owner, TableSourceItems, **title, path=str(path)))
        else:
            group_id = add_title(session, owner, TableSourceGroups, **title, group_key=f"moon-{season}")
            ids.append(group_id)
            episode_ids.append(add_title(session, owner, TableSourceItems, kind="episode", title="Moon",
                                         group_id=group_id, season=season, episode=1, path=str(path)))
    session.commit()

    catalog_id = f"tmdb:{kind}:84"
    catalog = {"source": "tmdb", "source_id": catalog_id, "id": 84, "media_type": kind,
               "imdb_id": imdb, "title": "Moon Catalog"}
    plural = "movies" if kind == "movie" else "shows"
    monkeypatch.setattr(metadata, f"{kind}_details", lambda _: {"data": {"item": dict(catalog)}})
    monkeypatch.setattr(metadata, f"search_{plural}", lambda _: {
        "data": {"status": "available", "items": [dict(catalog)]},
    })

    response = authenticated_client.get(f"/api/discover/metadata/{plural}/84?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    item = response.json["data"]["item"]
    assert {copy["local_id"] for copy in item["copies"]} == (set(ids) if source_tmdb == 84 else set())
    response = authenticated_client.get(f"/api/discover/metadata/search?q=Moon&type={kind}&source=all", headers=HEADERS)
    assert response.status_code == 200
    items = response.json["data"]["items"]
    expected = {catalog_id: set(ids)} if source_tmdb == 84 else {
        catalog_id: set(), **{f"source:{kind}:{identity}": {identity} for identity in ids},
    }
    assert {item["source_id"]: {copy["local_id"] for copy in item["copies"]} for item in items} == expected
    if kind == "show":
        for title in [item, *items]:
            seasons = [season for season, identity in enumerate(ids, 1) if identity in expected[title["source_id"]]]
            assert title["ownership"]["seasons_owned"] == seasons
            assert title["ownership"]["episode_count"] == len(seasons)
        target = {"media_type": "episode", "imdb_id": imdb, "season": 2, "episode": 1}
        for tmdb_id in {84, source_tmdb}:
            choices = copies_fixtures.copies(authenticated_client, **target, tmdb_id=tmdb_id)
            assert choices.status_code == 200
            assert [copy["local_id"] for copy in choices.json["items"]] == (
                [episode_ids[1]] if tmdb_id == source_tmdb else []
            )


@pytest.mark.parametrize("archive_mode", ["source", "sibling", "ambiguous", "pinned_sibling", "worker_sibling"])
def test_remapped_episode_uses_only_the_selected_source_archive_member(
    authenticated_client, copy_database, providers, monkeypatch, tmp_path, archive_mode,
):
    from app.database import TableSourceGroups, TableSourceItems
    from discover import metadata
    from test_provider_hub_archive_candidates import archive_payload

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
    worker = archive_provider(providers, monkeypatch, archive_payload(members, **extra),
                              {"decision": "pin", "member": f"{wrong.stem}.srt"})
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
    if archive_mode == "worker_sibling":
        [selector_request] = worker.selections
        assert selector_request["members"] == [f"{correct.stem}.srt"]
        assert (selector_request["season"], selector_request["episode"], selector_request["absolute_episode"]) == (1, 3, None)


@pytest.mark.parametrize("mode", ["pin", "defer", "sibling", "corrected", "show_only"])
def test_scanned_episode_archive_uses_manual_numbers_and_respects_an_identity_correction(
    authenticated_client, source_db, providers, monkeypatch, tmp_path, mode, source_languages,
):
    from app import database
    from discover import metadata
    from media_sources import indexer, scanner
    from test_provider_hub_archive_candidates import archive_payload

    monkeypatch.setattr(database, "engine", source_db.get_bind())
    monkeypatch.setattr(indexer, "get_language_set", lambda: set())
    show_only = mode == "show_only"
    show_id = 100 if show_only else 84
    manual_numbers = (2, 5) if show_only else (1, 3)
    title, year = ("Northern Light", 2020) if show_only else ("Moon Show", 2008)
    show = {"id": show_id, "source": "tmdb", "media_type": "show", "title": title, "year": year,
            "imdb_id": None if show_only else "tt0000084"}
    monkeypatch.setattr(metadata, "search_shows", lambda *args, **kwargs: {
        "data": {"status": "available", "items": [show]},
    })
    monkeypatch.setattr(metadata, "show_details", lambda _: {"data": {"status": "available", "item": show}})
    monkeypatch.setattr(metadata, "_show_details_with_validity", lambda _: ({"data": {"item": show}}, float("inf")))
    catalog_numbers = (2, 1) if mode == "corrected" else manual_numbers
    canonical = {"source": "tmdb", "id": 903, "show_id": show_id, "season": catalog_numbers[0], "episode": catalog_numbers[1],
                 "target_season": 2, "target_episode": 1, "identity_status": "resolved", "show_imdb_id": show["imdb_id"],
                 "show_title": title, "show_year": year}
    monkeypatch.setattr(metadata, "_episode_details_with_validity", lambda *_: ({"data": {"episode": canonical}}, float("inf")))
    if show_only:
        monkeypatch.setattr(metadata, "_episode_details_with_validity", lambda *_: pytest.fail("Episode details are pending"))

    library = tmp_path / "library"
    slug = title.replace(" ", ".")
    release = f"{slug}.S{manual_numbers[0]:02d}E{manual_numbers[1]:02d}"
    path = copies_fixtures.media_file(library / f"{title} ({year})", f"{release}.mkv", b"selected episode")
    created = authenticated_client.post("/api/system/media-sources", headers=HEADERS,
                                       json={"name": "Local shows", "library": {"path": str(library), "content_kind": "series"}})
    assert created.status_code == 201
    source_id = created.json["id"]
    assert scanner.run_scan(source_id)[0].status == "ok"
    listed = authenticated_client.get("/api/sources/items", headers=HEADERS, query_string={"source_id": source_id})
    [item] = listed.json["data"]
    assert (item["season"], item["episode"], item["tmdb_id"]) == (*manual_numbers, None)

    target = {"media_type": "episode", "tmdb_id": show_id, "season": 2, "episode": 1}
    if not show_only:
        target["imdb_id"] = show["imdb_id"]
    verified = {**target, "source_season": catalog_numbers[0], "source_episode": catalog_numbers[1], "episode_tmdb_id": 903}
    assert copies_fixtures.copies(authenticated_client, **verified).json["items"] == []
    if mode == "corrected":
        corrected = authenticated_client.patch(f"/api/sources/items/{item['id']}", headers=HEADERS,
                                                json={"season": 2, "episode": 1})
        assert corrected.status_code == 200
        assert (corrected.json["season"], corrected.json["episode"], corrected.json["identity_source"]) == (2, 1, "user")
        [copy] = copies_fixtures.copies(authenticated_client, **verified).json["items"]
    else:
        target.update(season=manual_numbers[0], episode=manual_numbers[1])
        [copy] = copies_fixtures.copies(authenticated_client, **target).json["items"]

    combined = f"{release}E{manual_numbers[1] + 1:02d}.srt"
    wrong = f"{slug}.S02E01.srt"
    members = {wrong: copies_fixtures.SRT.replace(b"Chosen copy dialogue", b"Sibling episode dialogue")}
    if mode != "sibling":
        members[combined] = copies_fixtures.SRT
    selector = {"decision": "pin", "member": combined} if mode in ("pin", "show_only") else {"decision": "defer"}
    worker = archive_provider(providers, monkeypatch, archive_payload(members, select_member=mode in ("pin", "defer", "show_only")), selector)
    selection = {
        **target, "language": "eng", "title": title, "year": year,
        "manual_confirmed": mode != "corrected", "copy_id": copy["copy_id"],
    }
    if show_only:
        selection.update(show_id=show_id, refresh=False)
    else:
        selection["episode_identity"] = canonical
    response = copies_fixtures.post(authenticated_client, selection)
    assert response.status_code == 200
    assert response.json["context"]["copy"]["filename"] == path.name
    [video] = worker.searches
    assert (video["season"], video["episode"]) == (target["season"], target["episode"])
    [result] = response.json["results"]
    download = download_fixtures.get(authenticated_client, result)
    if mode in ("pin", "defer", "show_only"):
        assert download.status_code == 200
        assert download.data == copies_fixtures.SRT
        [request] = worker.selections
        assert request["members"] == [combined]
        assert (request["season"], request["episode"]) == manual_numbers
    else:
        assert download.status_code == 502
        assert download.json["reason"] == "archive_no_match"


def test_linked_show_with_ambiguous_attachment_resolves_and_downloads_the_selected_source(
    authenticated_client, copy_database, providers, catalog_provider, monkeypatch, tmp_path,
):
    from app.database import TableSourceGroups, TableSourceItems
    from discover import metadata
    from discover.handles import resolve_result

    _, session = copy_database
    owner = add_source(session, "series")
    group_id = add_title(session, owner, TableSourceGroups, kind="show", title="Moon Show", group_key="moon",
                         tmdb_id="84", year="2008")
    add_title(session, owner, TableSourceGroups, kind="show", title="Other Show", group_key="other",
              tmdb_id="84", imdb_id="tt9999999")
    path = tmp_path / "Moon.Show.S02E03.mkv"
    path.write_bytes(b"source episode")
    episode_id = add_title(session, owner, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id,
                           season=2, episode=3, path=str(path))
    session.commit()
    catalog = {"source": "tmdb", "source_id": "tmdb:show:84", "id": 84, "media_type": "show",
               "imdb_id": "tt0000084", "title": "Moon Show", "year": 2008}
    monkeypatch.setattr(metadata, "show_details", lambda _: {"data": {"item": dict(catalog)}})
    canonical = {"source": "tmdb", "id": 903, "show_id": 84, "season": 2, "episode": 3,
                 "target_season": 2, "target_episode": 3, "identity_status": "resolved",
                 "show_imdb_id": "tt0000084", "show_title": "Moon Show", "show_year": 2008}
    monkeypatch.setattr(metadata, "_episode_details_with_validity",
                        lambda *_: ({"data": {"episode": canonical}}, float("inf")))
    details = authenticated_client.get("/api/discover/metadata/shows/84?source=tmdb", headers=HEADERS)
    assert details.status_code == 200
    assert details.json["data"]["item"]["copies"] == []

    target = {"media_type": "episode", "imdb_id": "tt0000084", "tmdb_id": 84, "season": 2, "episode": 3}
    offered = copies_fixtures.copies(authenticated_client, **target)
    assert offered.status_code == 200
    [copy] = offered.json["items"]
    assert copy["local_id"] == episode_id
    selection = {**target, "show_id": 84, "copy_id": copy["copy_id"], "language": "eng",
                 "title": "Moon Show", "year": 2008, "episode_identity": canonical}
    without_tmdb = dict(selection)
    del without_tmdb["tmdb_id"]
    assert copies_fixtures.post(authenticated_client, without_tmdb).status_code == 409
    response = copies_fixtures.post(authenticated_client, selection)
    assert response.status_code == 200
    assert response.json["context"]["copy"]["local_id"] == episode_id
    assert providers.videos[0][1].name == str(path)
    [row, *_] = response.json["results"]
    record = resolve_result(row["id"], row["search_id"])
    assert record["context"]["tmdb_id"] == 84
    assert record["context"]["copy_id"] == copy["copy_id"]
    monkeypatch.setattr("app.jobs_queue.event_stream", lambda **kwargs: None)
    download = download_fixtures.get(authenticated_client, row)
    assert download.status_code == 200
    assert download.data == copies_fixtures.SRT


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
    authenticated_client, source_db, catalog_provider, providers, monkeypatch, tmp_path, source_languages,
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
