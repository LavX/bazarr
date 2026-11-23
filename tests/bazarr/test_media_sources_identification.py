"""Movie and show identification through scans, using offline TMDB responses."""
import json
import os
from dataclasses import replace

import pytest
from flask import Flask

from media_source_helpers import Lib, stamped_video


HEADERS = {"X-API-KEY": "synthetic-source-test-key"}


def response(*, items=None, item=None, status="available"):
    return {"data": {"status": status, "items": items or [], "item": item}}


def title(identity, name, year=None, **extra):
    return dict(id=identity, title=name, year=year, **extra)


@pytest.fixture
def metadata(monkeypatch, languages_loaded, source_db):
    from discover import metadata
    from media_sources import indexer

    movies, shows, details, queried = {}, {}, {}, []

    def search(titles, kind, query):
        queried.append((kind, query))
        return titles.get(query, response())

    monkeypatch.setattr(indexer, "get_language_set", lambda: set())
    monkeypatch.setattr(metadata, "search_movies", lambda query, **kwargs: search(movies, "movie", query))
    monkeypatch.setattr(metadata, "search_shows", lambda query, **kwargs: search(shows, "show", query))
    monkeypatch.setattr(metadata, "movie_details", lambda movie_id: details.get(movie_id, response()))
    monkeypatch.setattr(metadata, "show_details", lambda show_id: details.get(show_id, response()))
    return movies, shows, details, queried


@pytest.fixture
def client(monkeypatch, metadata):
    from api import api_bp
    from app.config import settings

    monkeypatch.setitem(settings.auth, "apikey", HEADERS["X-API-KEY"])
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


def test_movie_scan_keeps_only_exact_title_and_year_matches(source_db, tmp_path, metadata):
    movies, _, details, _ = metadata
    lib = Lib(source_db, tmp_path, content_kind="movie")
    confirmed = stamped_video(lib.root / "Le.Fabuleux.Destin.d'Amélie.Poulain.2001.1080p.mkv")
    duplicate = stamped_video(lib.root / "Home.2009.1080p.mkv")
    no_match = stamped_video(lib.root / "2001.A.Space.Odyssey.1968.mkv")
    no_year = stamped_video(lib.root / "holiday clip.mkv")
    numeric = stamped_video(lib.root / "1917.2019.1080p.WEB-DL.mkv")

    movies["Le Fabuleux Destin d'Amélie Poulain"] = response(items=[
        title(11, "Amélie", 2002, original_title="Le Fabuleux Destin d’Amélie Poulain"),
        title(12, "Le Fabuleux Destin d'Amélie Poulain Returns", 2001),
        title(13, "Amélie", 1991, original_title="Le Fabuleux Destin d'Amélie Poulain"),
        title(14, "Le Fabuleux Destin d'Amélie Poulain")])
    details["11"] = response(item=title(11, "Amélie", 2002, imdb_id="tt0211915",
                                        poster_url="https://image.tmdb.org/t/p/w185/amelie.jpg"))
    movies["Home"] = response(items=[title(21, "Home", 2008), title(22, "Home", 2009), title(23, "Home", 2015)])

    assert lib.scan().status == "ok"
    items = lib.items()
    movie = items[str(confirmed)]
    assert (movie.identity_source, movie.identity_state, movie.tmdb_id, movie.imdb_id) == (
        "tmdb", "identified", "11", "tt0211915")
    assert movie.poster.endswith("/amelie.jpg")
    assert (movie.title, movie.parsed_title, movie.year) == (
        "Amélie", "Le Fabuleux Destin d'Amélie Poulain", "2002")
    assert [candidate["id"] for candidate in json.loads(items[str(duplicate)].identity_candidates)] == [21, 22]
    assert items[str(duplicate)].identity_state == "ambiguous"
    assert items[str(no_match)].identity_state == "parsed"
    assert items[str(no_year)].identity_state == "unmatched"
    assert items[str(numeric)].identity_state == "parsed"


def test_retries_unchanged_file_and_show_after_tmdb_recovers(source_db, tmp_path, metadata):
    movies, shows, details, queried = metadata
    movie_lib = Lib(source_db, tmp_path, name="Movies", folder="Movies", content_kind="movie")
    lib = Lib(source_db, tmp_path)
    movie = stamped_video(movie_lib.root / "Alien.1979.mkv")
    episode = stamped_video(lib.root / "Dark (2017)" / "Season 2" / "E05.mkv")
    stamped_video(lib.root / "Dark (2017)" / "Dark.S02E06.mkv")
    movies["Alien"] = shows["Dark"] = response(status="unavailable")

    movie_lib.scan()
    lib.scan()
    assert (movie_lib.items()[str(movie)].identity_state, movie_lib.items()[str(movie)].identify_pending) == (
        "parsed", 1)
    assert lib.group("Dark (2017)").identify_pending == 1
    assert lib.items()[str(episode)].identify_pending == 1
    assert queried == [("movie", "Alien"), ("show", "Dark")]

    movies["Alien"] = response()
    shows["Dark"] = response(items=[title(31, "Sombre", 2017, original_name="Dark")])
    details["31"] = response(item=title(31, "Sombre", 2017, imdb_id="tt5753856", tvdb_id=334824))
    assert movie_lib.scan().unchanged == 1
    assert lib.scan().unchanged == 2
    group = lib.group("Dark (2017)")
    assert (group.tmdb_id, group.imdb_id, group.tvdb_id, group.identify_pending) == (
        "31", "tt5753856", 334824, 0)
    row = lib.items()[str(episode)]
    assert (row.season, row.episode, row.identity_state, row.identify_pending, row.imdb_id) == (2, 5, "parsed", 0, None)
    assert movie_lib.items()[str(movie)].identify_pending == 0
    assert queried == [("movie", "Alien"), ("show", "Dark"), ("movie", "Alien"), ("show", "Dark")]


def test_hints_win_and_episode_ids_stay_on_the_episode(source_db, tmp_path, metadata, monkeypatch):
    from media_sources import scanner
    from media_sources.kinds import IdentityHints
    from media_sources.kinds.folder import FolderKind

    movies = Lib(source_db, tmp_path, name="Movies", folder="Movies", content_kind="movie")
    tv = Lib(source_db, tmp_path)
    movie = stamped_video(movies.root / "Wrong.Title.2001.mkv")
    episode = stamped_video(tv.root / "Wrong Show" / "Wrong.Show.S01E01.mkv")
    real = FolderKind.enumerate

    class Kind(FolderKind):
        def enumerate(self, source, library, cancel=None):
            listing = real(self, source, library, cancel)
            files = listing.files

            def enriched():
                for found in files:
                    if library.content_kind == "movie":
                        hints = IdentityHints(title="Correct Movie", year=2005, tmdb_id="30")
                    else:
                        hints = IdentityHints(title="Episode Title", season=2, episode=9, tmdb_id="42",
                                              imdb_id="tt0000042", tvdb_id=120)
                    yield replace(found, hints=hints)

            listing.files = enriched()
            return listing

        def series_identity(self, source, group_key):
            return IdentityHints(title="Correct Show", year=2020, tmdb_id="99", tvdb_id=721)

    monkeypatch.setattr(scanner, "get_kind", lambda name: Kind())
    movies.scan()
    tv.scan()
    movie = movies.items()[str(movie)]
    row = tv.items()[str(episode)]
    group = tv.group("Wrong Show")
    assert (movie.title, movie.year, movie.tmdb_id, movie.identity_source) == (
        "Correct Movie", "2005", "30", "media_server")
    assert (row.title, row.season, row.episode, row.tmdb_id, row.imdb_id, row.tvdb_id) == (
        "Episode Title", 2, 9, "42", "tt0000042", 120)
    assert (group.title, group.tmdb_id, group.imdb_id, group.tvdb_id, group.identity_source) == (
        "Correct Show", "99", None, 721, "media_server")
    assert not metadata[3]


def test_candidate_and_episode_overrides_survive_group_and_file_rename(source_db, tmp_path, metadata, client):
    _, shows, details, _ = metadata
    lib = Lib(source_db, tmp_path)
    valid = stamped_video(lib.root / "Utopia" / "Utopia.S01E01.mkv")
    multi = stamped_video(lib.root / "Utopia" / "Utopia.S01E02-E04.mkv")
    shows["Utopia"] = response(items=[title(51, "Utopia", 2013), title(52, "Utopia", 2020)])
    details["52"] = response(item=title(52, "Utopia", 2020, imdb_id="tt8303474"))
    lib.scan()
    row = lib.group("Utopia")
    item = lib.items()[str(multi)]
    assert row.identity_state == "ambiguous"
    assert lib.items()[str(valid)].identity_state == "ambiguous"
    assert item.identity_state == "unmatched" and item.episode is None

    group_url = f"/api/sources/groups/{row.id}"
    item_url = f"/api/sources/items/{item.id}"
    assert client.patch(group_url, json={"candidate_id": 52}).status_code == 401
    assert client.patch(group_url, json={"candidate_id": 100}, headers=HEADERS).status_code == 400
    assert client.get(group_url, headers=HEADERS).json["identity_candidates"][1]["id"] == 52
    assert client.patch(item_url, json={"season": 3, "episode": True}, headers=HEADERS).status_code == 400
    edited = client.patch(item_url, json={"season": 3, "episode": 7}, headers=HEADERS)
    assert edited.status_code == 200 and edited.json["identity_state"] == "ambiguous"
    assert client.patch(group_url, json={"candidate_id": 52}, headers=HEADERS).status_code == 200
    assert lib.items()[str(multi)].identity_state == "identified"
    assert lib.items()[str(valid)].identity_state == "parsed"

    os.rename(lib.root / "Utopia", lib.root / "Some Other Name")
    renamed = lib.root / "Some Other Name" / "Other.S01E13.mkv"
    os.rename(renamed.parent / multi.name, renamed)
    lib.scan()
    row = lib.group("Some Other Name")
    item = lib.items()[str(renamed)]
    assert (row.identity_source, row.identity_state, row.tmdb_id, row.imdb_id) == (
        "user", "identified", "52", "tt8303474")
    assert (item.identity_source, item.identity_state, item.season, item.episode) == (
        "user", "identified", 3, 7)


def test_manual_search_still_builds_an_unmatched_episode_video(source_db, tmp_path, metadata, monkeypatch):
    from media_sources.context import resolve_item
    from subtitles import utils

    lib = Lib(source_db, tmp_path)
    path = stamped_video(lib.root / "Mystery Show" / "copy.mkv")
    lib.scan()
    row = lib.items()[str(path)]
    assert row.identity_state == "unmatched"
    monkeypatch.setitem(utils.registered_refiners, "anidb", lambda *args: None)
    monkeypatch.setitem(utils.registered_refiners, "anilist", lambda *args: None)
    context = resolve_item(row.id)
    video = utils.get_video(context.path, context.group_title, context.scene_name, source=context)
    assert video.series == "Mystery Show"
    assert video.season is None and video.episode is None

    from subliminal_patch import core
    from subliminal_patch.providers.legendasdivx import LegendasdivxProvider
    from subliminal_patch.providers.napiprojekt import NapiProjektProvider
    from subliminal_patch.providers.shooter import ShooterProvider
    from subzero.language import Language

    monkeypatch.setattr(LegendasdivxProvider, 'initialize', lambda self: None)
    monkeypatch.setattr(LegendasdivxProvider, 'terminate', lambda self: None)
    file_searches = []

    class FileProvider(LegendasdivxProvider):
        def list_subtitles(self, video, languages):
            file_searches.append(video.original_path)
            return []

    monkeypatch.setattr(ShooterProvider, 'initialize', lambda self: None)
    monkeypatch.setattr(ShooterProvider, 'terminate', lambda self: None)
    monkeypatch.setattr(ShooterProvider, 'query', lambda self, language, filename, hash:
                        [self.subtitle_class(language, hash, 'https://example.invalid/hash-result')])
    monkeypatch.setattr(NapiProjektProvider, 'initialize', lambda self: None)
    monkeypatch.setattr(NapiProjektProvider, 'terminate', lambda self: None)
    monkeypatch.setattr(NapiProjektProvider, 'query', lambda *args: None)
    monkeypatch.setattr(NapiProjektProvider, '_find_title', lambda *args: ('Mystery Show', set()))
    libraries = {'legendasdivx': LegendasdivxProvider, 'embeddedsubtitles': FileProvider, 'shooter': ShooterProvider,
                 'napiprojekt': NapiProjektProvider}
    monkeypatch.setattr(core, 'provider_registry', libraries)
    configuration = {provider: {'username': None, 'password': None}
                     for provider in ('legendasdivx', 'embeddedsubtitles')}
    failures = []
    with core.SZProviderPool(list(libraries), configuration,
                             throttle_callback=lambda *args, **kwargs: failures.append(args)) as pool:
        result = pool.list_subtitles_provider('legendasdivx', video, {Language('por')}, detailed=True)
        assert (result.status, result.reason) == ('skipped', 'unsupported_media')
        video.hashes['napiprojekt'] = 'fixture-napi-hash'
        mixed = pool.list_subtitles_provider('napiprojekt', video, {Language('pol')}, detailed=True)
        assert (mixed.status, mixed.reason) == ('skipped', 'unsupported_media')
        assert not pool.discarded_providers
        file_result = pool.list_subtitles_provider('embeddedsubtitles', video, {Language('por')}, detailed=True)
        assert file_result.status == 'empty'

        absent_hash = pool.list_subtitles_provider('shooter', video, {Language('eng')}, detailed=True)
        assert absent_hash.status == 'skipped'
        video.hashes['shooter'] = 'fixture-video-hash'
        hashed_result = pool.list_subtitles_provider('shooter', video, {Language('eng')}, detailed=True)
        assert hashed_result.status == 'success'
        (subtitle,) = hashed_result.subtitles
        matches = subtitle.get_matches(video)
        assert core.SZProviderPool._episode_candidate_is_valid(subtitle, matches, matches)
    assert not failures
    assert file_searches == [str(path)]


def test_confirmed_identities_reach_provider_video(source_db, tmp_path, metadata, monkeypatch):
    from media_sources.context import resolve_item
    from subtitles import utils

    movies, shows, details, _ = metadata
    tv = Lib(source_db, tmp_path)
    ml = Lib(source_db, tmp_path, name="Movies", folder="Movies", content_kind="movie")
    movie = stamped_video(ml.root / "Arrival.2015.mkv")
    episode = stamped_video(tv.root / "Dark (2017)" / "Dark.2016.S01E03.mkv")
    movies["Arrival"] = response(items=[title(81, "Arrival", 2016, imdb_id="tt2543164")])
    shows["Dark"] = response(items=[title(82, "Dark", 2017)])
    details["82"] = response(item=title(82, "Dark", 2017, tvdb_id=334824))
    ml.scan()
    tv.scan()

    monkeypatch.setitem(utils.registered_refiners, "anidb", lambda *args: None)
    monkeypatch.setitem(utils.registered_refiners, "anilist", lambda *args: None)
    movie_context = resolve_item(ml.items()[str(movie)].id)
    show_context = resolve_item(tv.items()[str(episode)].id)
    movie_video = utils.get_video(movie_context.path, movie_context.title, movie_context.scene_name,
                                  source=movie_context)
    episode_video = utils.get_video(show_context.path, show_context.group_title, show_context.scene_name,
                                    source=show_context)
    assert (movie_video.year, movie_video.tmdb_id, movie_video.imdb_id) == (2016, 81, "tt2543164")
    assert (episode_video.year, episode_video.series_tmdb_id, episode_video.series_tvdb_id) == (2017, 82, 334824)
    assert episode_video.tmdb_id is None and episode_video.tvdb_id is None
    from provider_hub.protocol import video_to_payload

    payload = video_to_payload(episode_video)
    assert (payload['series_tmdb_id'], payload['tmdb_id']) == (82, None)


def test_identification_search_uses_original_titles_and_reports_incomplete_results(monkeypatch):
    from discover import metadata

    calls = []

    def request(config, path, params):
        assert path == "/search/movie"
        calls.append(params["page"])
        return {"total_pages": 20, "results": [
            {"id": params["page"], "release_date": "1999-03-31", "title": "The Matrix", "original_title": "Matrix"}]}

    monkeypatch.setattr(metadata, "_request", request)
    default = metadata.search_movies("test discovery projection")["data"]
    assert calls == [1]
    assert default["items"][0]["original_title"] == "Matrix"
    calls.clear()
    exact = metadata.search_movies("test discovery projection", for_identification=True)["data"]
    assert len(calls) > 1 and exact["truncated"]

    show = metadata._show({"id": 1, "name": "Sombre", "original_name": "Dark"}, {})
    assert (show["title"], show["original_name"]) == ("Sombre", "Dark")


def test_an_override_saved_during_lookup_wins(source_db, tmp_path, metadata, client, monkeypatch):
    from discover import metadata as discover
    from media_sources import repository

    movies, _, _, queried = metadata
    lib = Lib(source_db, tmp_path, content_kind="movie")
    path = stamped_video(lib.root / "Edge.of.Tomorrow.2014.mkv")
    movies["Edge of Tomorrow"] = response(items=[title(91, "Edge of Tomorrow", 2014)])

    def details(movie_id):
        row = repository.get_item_by_path(str(path))
        saved = client.patch(f"/api/sources/items/{row.id}", json={"title": "Live Die Repeat", "year": 2014},
                             headers=HEADERS)
        assert saved.status_code == 200
        return response(item=title(91, "Edge of Tomorrow", 2014))

    monkeypatch.setattr(discover, "movie_details", details)
    lib.scan()
    row = lib.items()[str(path)]
    assert (row.title, row.identity_source, row.tmdb_id, row.identify_pending) == ("Live Die Repeat", "user", None, 0)
    assert queried == [("movie", "Edge of Tomorrow")]
    lib.scan()
    assert queried == [("movie", "Edge of Tomorrow")]


def test_movie_candidate_choice_does_not_depend_on_details_being_online(source_db, tmp_path, metadata, client):
    movies, _, details, _ = metadata
    lib = Lib(source_db, tmp_path, content_kind="movie")
    path = stamped_video(lib.root / "Crash.mkv")
    movies["Crash"] = response(items=[title(101, "Crash", 1996), title(102, "Crash", 2004)])
    details["101"] = response(status="unavailable")
    lib.scan()
    row = lib.items()[str(path)]
    assert row.identity_state == "ambiguous"
    saved = client.patch(f"/api/sources/items/{row.id}", json={"candidate_id": 101}, headers=HEADERS)
    assert saved.status_code == 200
    assert (saved.json["tmdb_id"], saved.json["year"], saved.json["identity_source"]) == ("101", "1996", "user")
    repeated = client.patch(f"/api/sources/items/{row.id}", json={"candidate_id": 101}, headers=HEADERS)
    assert repeated.status_code == 200 and repeated.json == saved.json


def test_show_folder_identity_takes_precedence_over_misleading_file_names(source_db, tmp_path, metadata):
    _, _, _, queried = metadata
    lib = Lib(source_db, tmp_path)
    episode = stamped_video(lib.root / "Severance (2022)" / "Season 2" / "Mr.Robot.S02E04.mkv")
    fallback = stamped_video(lib.root / "1080p" / "Dark.2017.S02E05.mkv")
    lib.scan()

    group = lib.group("Severance (2022)")
    assert (group.title, group.year) == ("Severance", "2022")
    assert lib.items()[str(episode)].identity_state == "parsed"
    assert lib.group("1080p").title == "Dark"
    assert lib.items()[str(fallback)].identity_state == "parsed"
    assert set(queried) == {("show", "Severance"), ("show", "Dark")}


def test_pending_movie_keeps_its_server_link_and_adopts_late_hints(source_db, tmp_path, metadata, monkeypatch):
    from media_sources import scanner
    from media_sources.kinds import IdentityHints
    from media_sources.kinds.folder import FolderKind

    movies, _, _, queried = metadata
    lib = Lib(source_db, tmp_path, content_kind="movie")
    path = stamped_video(lib.root / "Rename.2001.mkv")
    movies["Rename"] = response(status="unavailable")
    hints = IdentityHints(external_id="server-movie-8")
    walk = FolderKind.enumerate

    class Kind(FolderKind):
        def enumerate(self, source, library, cancel=None):
            listing = walk(self, source, library, cancel)
            files = listing.files
            listing.files = (replace(found, hints=hints) for found in files)
            return listing

    monkeypatch.setattr(scanner, "get_kind", lambda name: Kind())
    lib.scan()
    item = lib.items()[str(path)]
    assert (item.external_id, item.identify_pending) == ("server-movie-8", 1)

    hints = replace(hints, title="Corrected", tmdb_id="180")
    assert lib.scan().unchanged == 1
    corrected = lib.items()[str(path)]
    assert (corrected.title, corrected.tmdb_id, corrected.identity_source, corrected.identify_pending) == (
        "Corrected", "180", "media_server", 0)
    assert queried == [("movie", "Rename")]


def test_real_world_episode_names_have_safe_search_coordinates(source_db, tmp_path, metadata):
    lib = Lib(source_db, tmp_path)
    names = {
        "Rick%20and%20Morty%20S09E02%20%281080p%29.mkv": ("parsed", 9, 2),
        "Season 2/E05.mkv": ("parsed", 2, 5),
        "Show.S00E02.mkv": ("parsed", 0, 2),
        "Show.1x08.mkv": ("parsed", 1, 8),
        "Show.2024-02-12.mkv": ("unmatched", None, None),
        "Show.S01E01-E03.mkv": ("unmatched", None, None),
        "Show.S01E01.S02E03.mkv": ("unmatched", None, None),
    }
    paths = {name: stamped_video(lib.root / "A Show" / name) for name in names}
    lib.scan()
    items = lib.items()
    assert {name: (items[str(path)].identity_state, items[str(path)].season, items[str(path)].episode)
            for name, path in paths.items()} == names


@pytest.fixture
def development_upgrade(source_db):
    from types import SimpleNamespace

    import flask_migrate
    from alembic.migration import MigrationContext
    from app.database import metadata as models, migrations_directory
    from test_media_sources_schema import PREVIOUS, REMOVAL_KEYS, REVISION, _migration, _run

    engine = source_db.get_bind()
    # Recreate the sources schema at the actual released development revision
    # through the same Alembic environment that startup calls.
    _run(engine, _migration(REVISION).downgrade)
    migration_app = Flask(__name__)
    db = SimpleNamespace(get_engine=lambda: engine, metadata=models)
    flask_migrate.Migrate(migration_app, db, render_as_batch=True)
    with migration_app.app_context():
        flask_migrate.stamp(directory=migrations_directory, revision=PREVIOUS)
        flask_migrate.upgrade(directory=migrations_directory, revision=REVISION)
    with engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == REVISION

    def upgrade():
        with migration_app.app_context():
            flask_migrate.upgrade(directory=migrations_directory)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == REMOVAL_KEYS

    return upgrade


def _legacy_item(source_db, lib, path, **values):
    from media_source_helpers import add_item

    stat = path.stat()
    values.setdefault('scene_name', path.stem)
    return add_item(source_db, lib.source_id, lib.library_id, path,
                    file_dev=str(stat.st_dev), file_ino=str(stat.st_ino), file_size=stat.st_size,
                    file_mtime_ns=stat.st_mtime_ns, **values)


def test_upgrade_repairs_legacy_coordinates_without_promoting_multi_episode_files(source_db, tmp_path, metadata,
                                                                                 client, development_upgrade):
    from media_source_helpers import add_group

    lib = Lib(source_db, tmp_path)
    multi = stamped_video(lib.root / "Show" / "Show.S01E01-E03.mkv")
    single = stamped_video(lib.root / "Show" / "Show.S01E04.mkv")
    group_id = add_group(source_db, lib.source_id, lib.library_id, key=str(multi.parent))
    for path, number in ((multi, 1), (single, 4)):
        _legacy_item(source_db, lib, path, kind='episode', group_id=group_id, identity_state='unmatched',
                     season=1, episode=number)
    development_upgrade()

    before = lib.items()
    assert (before[str(multi)].season, before[str(multi)].episode) == (None, None)
    # A show correction can happen before the first post-upgrade scan.
    assert client.patch(f"/api/sources/groups/{group_id}", json={"title": "Correct Show"},
                        headers=HEADERS).status_code == 200
    assert lib.scan().unchanged == 2
    items = lib.items()
    assert (items[str(multi)].identity_state, items[str(multi)].season, items[str(multi)].episode) == (
        "unmatched", None, None)
    assert (items[str(single)].identity_state, items[str(single)].season, items[str(single)].episode) == (
        "parsed", 1, 4)
    assert not items[str(multi)].identify_pending and not items[str(single)].identify_pending


def test_cancelling_after_show_reidentification_keeps_its_episodes_consistent(source_db, tmp_path, metadata,
                                                                                 monkeypatch):
    from app.jobs_queue import JobCancelled
    from media_sources import repository, scanner

    _, shows, _, _ = metadata
    lib = Lib(source_db, tmp_path)
    episode = stamped_video(lib.root / "Crash" / "Crash.S01E01.mkv")
    stamped_video(lib.root / "Zulu" / "Zulu.S01E01.mkv")
    shows["Crash"] = response(status="unavailable")
    lib.scan()
    assert lib.items()[str(episode)].identity_state == "parsed"
    shows["Crash"] = response(items=[title(201, "Crash", 2008), title(202, "Crash", 2023)])

    became_ambiguous = False
    update = repository.update_group

    def observe(*args, **kwargs):
        nonlocal became_ambiguous
        result = update(*args, **kwargs)
        if kwargs.get("identity_state") == "ambiguous":
            became_ambiguous = True
        return result

    def cancel():
        if became_ambiguous:
            raise JobCancelled()

    monkeypatch.setattr(repository, "update_group", observe)
    with pytest.raises(JobCancelled):
        scanner.run_scan(lib.source_id, lib.library_id, cancel=cancel)
    assert lib.group("Crash").identity_state == "ambiguous"
    assert lib.items()[str(episode)].identity_state == "ambiguous"


def test_overlapping_candidate_submissions_return_the_same_saved_override(source_db, tmp_path, metadata,
                                                                         client, monkeypatch):
    from discover import metadata as discover

    movies, _, _, _ = metadata
    lib = Lib(source_db, tmp_path, content_kind="movie")
    path = stamped_video(lib.root / "Collision.mkv")
    movies["Collision"] = response(items=[title(301, "Collision", 2012), title(302, "Collision", 2006)])
    lib.scan()
    url = f"/api/sources/items/{lib.items()[str(path)].id}"
    body = {"candidate_id": 301}
    overlap = []

    def details(candidate_id):
        monkeypatch.setattr(discover, 'movie_details', lambda _: response())
        overlap.append(client.patch(url, json=body, headers=HEADERS))
        return response()

    monkeypatch.setattr(discover, 'movie_details', details)
    first = client.patch(url, json=body, headers=HEADERS)
    assert first.status_code == overlap[0].status_code == 200
    assert first.json == overlap[0].json


def test_reparse_uses_committed_group_override_even_if_scan_is_cancelled(source_db, tmp_path, metadata, client,
                                                                       monkeypatch):
    from app.jobs_queue import JobCancelled
    from media_sources import identify, repository, scanner

    lib = Lib(source_db, tmp_path)
    path = stamped_video(lib.root / "Show" / "Show.S01E05.mkv")
    lib.scan()
    group = lib.group("Show")
    item = lib.items()[str(path)]
    repository.update_group(group.id, source_db, identity_state='unmatched')
    repository.update_item(item.id, source_db, identity_state='unmatched', season=None, episode=None,
                           scene_name=None, identify_pending=1)
    parse = identify.identify_item
    update = repository.update_item
    restored = False

    def correct_during_parse(*args, **kwargs):
        changed = client.patch(f"/api/sources/groups/{group.id}", json={"title": "User Show"}, headers=HEADERS)
        assert changed.status_code == 200
        return parse(*args, **kwargs)

    def observe(*args, **kwargs):
        nonlocal restored
        result = update(*args, **kwargs)
        restored = restored or kwargs.get('episode') == 5
        return result

    def cancel():
        if restored:
            raise JobCancelled()

    monkeypatch.setattr(identify, 'identify_item', correct_during_parse)
    monkeypatch.setattr(repository, 'update_item', observe)
    with pytest.raises(JobCancelled):
        scanner.run_scan(lib.source_id, lib.library_id, cancel=cancel)
    row = lib.items()[str(path)]
    assert (lib.group("Show").identity_state, row.identity_state, row.season, row.episode) == (
        'identified', 'parsed', 1, 5)


def test_cancelling_after_folder_rename_keeps_show_and_episode_states_aligned(source_db, tmp_path, metadata,
                                                                             monkeypatch):
    from app.jobs_queue import JobCancelled
    from media_sources import repository, scanner

    _, shows, _, _ = metadata
    lib = Lib(source_db, tmp_path)
    path = stamped_video(lib.root / "Ambiguous" / "Ambiguous.S01E01.mkv")
    shows["Ambiguous"] = response(items=[title(501, "Ambiguous", 2013), title(502, "Ambiguous", 2020)])
    lib.scan()
    assert lib.items()[str(path)].identity_state == 'ambiguous'
    os.rename(path.parent, lib.root / "Renamed Show")
    update = repository.update_group
    group_written = False

    def observe(*args, **kwargs):
        nonlocal group_written
        result = update(*args, **kwargs)
        group_written = group_written or kwargs.get('identity_state') == 'parsed'
        return result

    def cancel():
        if group_written:
            raise JobCancelled()

    monkeypatch.setattr(repository, 'update_group', observe)
    with pytest.raises(JobCancelled):
        scanner.run_scan(lib.source_id, lib.library_id, cancel=cancel)
    group = lib.group("Renamed Show")
    (episode,) = lib.items().values()
    assert (group.identity_state, episode.identity_state) == ('parsed', 'parsed')


def test_a_pending_show_does_not_reparse_an_unchanged_multi_episode_file(source_db, tmp_path, metadata, monkeypatch):
    from media_sources import identify

    _, shows, _, _ = metadata
    lib = Lib(source_db, tmp_path)
    path = stamped_video(lib.root / "Show" / "Show.S01E01E02.mkv")
    shows["Show"] = response(status="unavailable")
    lib.scan()
    assert lib.items()[str(path)].identify_pending

    monkeypatch.setattr(identify, 'identify_item', lambda *args: pytest.fail('unchanged filename was reparsed'))
    assert lib.scan().unchanged == 1
    row = lib.items()[str(path)]
    assert (row.identity_state, row.identify_pending, row.episode) == ('unmatched', 1, None)


@pytest.mark.parametrize('correct_show', [True, False], ids=['concurrent-correction', 'pending-lookup'])
def test_cancel_after_renamed_episode_commits_its_current_group_state(source_db, tmp_path, metadata, client,
                                                                    monkeypatch, correct_show):
    from concurrent.futures import ThreadPoolExecutor

    from app.jobs_queue import JobCancelled
    from media_sources import identify, scanner

    _, shows, _, _ = metadata
    lib = Lib(source_db, tmp_path)
    group_dir = "1080p" if correct_show else "Original Show"
    next_dir = "720p" if correct_show else "Replacement Show"
    original = stamped_video(lib.root / group_dir / "Season 1" / "E05.mkv")
    shows["Original Show"] = response(status="unavailable")
    lib.scan()
    group_id = lib.group(group_dir).id
    renamed = lib.root / next_dir / "Season 1" / original.name
    original.parents[1].rename(lib.root / next_dir)
    episode_parse = identify.identify_item
    count_repathed = scanner._count_repathed
    item_committed = False

    def correct_show_in_other_request():
        try:
            response = client.application.test_client().patch(
                f"/api/sources/groups/{group_id}", json={"title": "User Show"}, headers=HEADERS)
            assert response.status_code == 200
        finally:
            source_db.remove()

    def parse(*args, **kwargs):
        if correct_show and args[1] == str(renamed):
            with ThreadPoolExecutor(max_workers=1) as workers:
                workers.submit(correct_show_in_other_request).result(timeout=10)
        return episode_parse(*args, **kwargs)

    def committed(*args):
        nonlocal item_committed
        item_committed = True
        return count_repathed(*args)

    def interrupt():
        if item_committed:
            raise JobCancelled()

    monkeypatch.setattr(identify, 'identify_item', parse)
    monkeypatch.setattr(scanner, '_count_repathed', committed)
    with pytest.raises(JobCancelled):
        scanner.run_scan(lib.source_id, lib.library_id, cancel=interrupt)
    group = lib.group(next_dir)
    episode = lib.items()[str(renamed)]
    assert group.identity_state == ('identified' if correct_show else 'parsed')
    assert (episode.identity_state, episode.season, episode.episode) == ('parsed', 1, 5)
    assert episode.identify_pending == group.identify_pending == (0 if correct_show else 1)


def test_a_pending_movie_retries_using_its_saved_filename_parse(source_db, tmp_path, metadata, monkeypatch):
    from media_sources import identify

    movies, _, details, queried = metadata
    lib = Lib(source_db, tmp_path, content_kind="movie")
    path = stamped_video(lib.root / "Alien.1979.mkv")
    movies["Alien"] = response(status="unavailable")
    lib.scan()
    assert lib.items()[str(path)].identify_pending
    monkeypatch.setattr(identify, "identify_item", lambda *args: pytest.fail("unchanged movie was reparsed"))

    assert lib.scan().unchanged == 1
    assert lib.items()[str(path)].identify_pending
    movies["Alien"] = response(items=[title(601, "Alien", 1979)])
    details["601"] = response(item=title(601, "Alien", 1979))
    assert lib.scan().unchanged == 1
    row = lib.items()[str(path)]
    assert (row.identity_state, row.identify_pending, row.tmdb_id) == ("identified", 0, "601")
    assert queried == [("movie", "Alien")] * 3


def test_manual_source_job_hashes_scores_and_saves_a_shooter_result(source_db, tmp_path, metadata, monkeypatch, caplog):
    from types import SimpleNamespace

    from app.config import settings
    from media_servers import dispatcher
    from media_sources import indexer, repository, service
    from media_source_helpers import add_profile
    from subliminal_patch import core
    from subliminal_patch.hashes import hash_shooter
    from subliminal_patch.providers.shooter import ShooterProvider
    from subliminal_patch.score import DEFAULT_SCORES
    from subtitles import download, pool, processing, sync
    from subzero.language import Language

    caplog.set_level('DEBUG', logger='subliminal_patch')
    english = Language('eng')
    monkeypatch.setattr(indexer, 'get_language_set', lambda: {english})
    monkeypatch.setitem(settings.general, 'skip_hashing', False)
    monkeypatch.setitem(settings.general, 'single_language', False)
    monkeypatch.setattr(pool, '_update_pool', lambda *args, **kwargs: False)
    monkeypatch.setattr(download.subliminal, 'region', SimpleNamespace(backend=SimpleNamespace(sync=lambda: None)))
    monkeypatch.setattr(service, 'get_providers', lambda: ['shooter'])
    monkeypatch.setattr(service.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    monkeypatch.setattr(dispatcher, 'notify_subtitle_mutation', lambda event: None)
    monkeypatch.setattr(processing, 'call_external_webhook', lambda *args, **kwargs: None)
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: False)
    monkeypatch.setattr(sync, '_resolve_subsync_overrides', lambda *args, **kwargs: (True, False, 0, None, '60'))

    lib = Lib(source_db, tmp_path)
    profile = add_profile(source_db, [('en', 'False', 'False')])
    repository.update_library(lib.library_id, source_db, profile_id=profile)
    path = stamped_video(lib.root / 'Mystery Show' / 'recording.mkv')
    with path.open('r+b') as media:
        media.truncate(11 * 1024 * 1024)
        media.seek(4096)
        media.write(b'fixture bytes at the first Shooter hash offset')
    lib.scan()
    item = lib.items()[str(path)]
    assert item.identity_state == 'unmatched' and item.missing_subtitles == "['en']"

    requests = []
    content = b'1\n00:00:01,000 --> 00:00:02,000\nA matching subtitle.\n'

    def query(provider, language, filename, hash):
        requests.append((language, filename, hash))
        return [provider.subtitle_class(language, hash, 'https://example.invalid/subtitle')]

    monkeypatch.setattr(ShooterProvider, 'initialize', lambda self: None)
    monkeypatch.setattr(ShooterProvider, 'terminate', lambda self: None)
    monkeypatch.setattr(ShooterProvider, 'query', query)
    monkeypatch.setattr(ShooterProvider, 'download_subtitle', lambda self, subtitle: setattr(subtitle, 'content', content))
    monkeypatch.setattr(core, 'provider_registry', {'shooter': ShooterProvider})
    with core.SZProviderPool(['shooter']) as search_pool:
        monkeypatch.setattr(download, '_get_pool', lambda *args, **kwargs: search_pool)
        service.search_source_item(item.id, job_id=3)

    assert requests == [(english, str(path), hash_shooter(path))]
    assert path.with_suffix('.en.srt').read_bytes() == content
    (saved,) = source_db.execute(repository.History.select()).all()
    scores = DEFAULT_SCORES['episode']
    assert saved.item_id == item.id and saved.provider == 'shooter'
    assert saved.score == scores['hash'] + scores['hearing_impaired']


def test_upgrade_seeds_existing_episode_pending_flags_during_a_tmdb_outage(source_db, tmp_path, metadata,
                                                                          development_upgrade):
    from media_source_helpers import add_group

    _, shows, _, _ = metadata
    lib = Lib(source_db, tmp_path)
    located = stamped_video(lib.root / 'Destination' / 'Destination.S01E01.mkv')
    manual = stamped_video(lib.root / 'Destination' / 'Destination.S01E02.mkv')
    group_id = add_group(source_db, lib.source_id, lib.library_id, title='Destination', key=str(located.parent),
                         parsed_title='Destination')
    _legacy_item(source_db, lib, located, kind='episode', group_id=group_id, season=1, episode=1)
    _legacy_item(source_db, lib, manual, kind='episode', group_id=group_id, season=1, episode=2,
                 identity_source='user', identity_state='identified')
    shows['Destination'] = response(status='unavailable')

    development_upgrade()
    assert lib.group('Destination').identify_pending == lib.items()[str(located)].identify_pending == 1
    for _ in range(2):
        assert lib.scan().unchanged == 2
        rows = lib.items()
        assert lib.group('Destination').identify_pending == rows[str(located)].identify_pending == 1
        assert rows[str(manual)].identify_pending == 0


def test_upgrade_reparses_legacy_encoded_movie_and_preserves_saved_movie_data(source_db, tmp_path, metadata,
                                                                              development_upgrade):
    _, _, _, queried = metadata
    lib = Lib(source_db, tmp_path, content_kind='movie')
    encoded = stamped_video(lib.root / 'Alien%20%281979%29%20%5B1080p%5D.mkv')
    saved = stamped_video(lib.root / 'Movie.Release.Label.2010.mkv')
    manual = stamped_video(lib.root / 'Also%20Encoded%20%282019%29.mkv')
    _legacy_item(source_db, lib, encoded, title=encoded.stem, parsed_title=encoded.stem, identity_state='unmatched')
    _legacy_item(source_db, lib, saved, title='Saved Movie', parsed_title='Saved Movie', year='2010', scene_name=None)
    _legacy_item(source_db, lib, manual, title='User Override', parsed_title=manual.stem, year='2018',
                 identity_source='user', identity_state='identified', imdb_id='tt0000100')

    development_upgrade()
    assert lib.scan().unchanged == 3
    items = lib.items()
    assert (items[str(encoded)].title, items[str(encoded)].year, items[str(encoded)].identity_state) == (
        'Alien', '1979', 'parsed')
    assert items[str(encoded)].identify_pending == 0
    assert (items[str(saved)].title, items[str(saved)].year, items[str(saved)].scene_name) == (
        'Saved Movie', '2010', None)
    row = items[str(manual)]
    assert (row.title, row.year, row.identity_source, row.imdb_id, row.scene_name) == (
        'User Override', '2018', 'user', 'tt0000100', manual.stem)
    assert set(queried) == {('movie', 'Alien'), ('movie', 'Saved Movie')}


def test_pg_handover_takes_the_show_lock_before_deleting_its_displaced_episode(source_db, backend, tmp_path,
                                                                             metadata, monkeypatch):
    if backend != 'postgresql':
        pytest.skip('PostgreSQL is needed to observe row lock ordering')

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    import sqlalchemy as sa

    from media_sources import repository, scanner
    from media_sources.db import source_transaction

    _, shows, _, _ = metadata
    lib = Lib(source_db, tmp_path)
    other = Lib(source_db, tmp_path, name='Other', folder='Other')
    destination, _ = lib.show('Destination', count=2)
    (incoming,) = other.show('Incoming', count=1)
    other.show('Keeps', count=1)
    shows['Destination'] = response(items=[title(701, 'Destination', 2014), title(702, 'Destination', 2021)])
    lib.scan()
    other.scan()
    group = lib.group('Destination')
    displaced_id = lib.items()[str(destination)].id
    incoming_id = other.items()[str(incoming)].id
    assert group.identity_state == 'ambiguous'
    incoming.replace(destination)

    ready, proceed, entered, deleted = Event(), Event(), Event(), Event()
    scan_pids = []
    write, move, remove = scanner._write_repathed, scanner._write_final_path, repository.delete_items

    def pause_after_group_planning(*args, **kwargs):
        ready.set()
        assert proceed.wait(10)
        return write(*args, **kwargs)

    def remember_handover_connection(library, entry, grouped, decisions, connection, releases):
        scan_pids.append(connection.exec_driver_sql('select pg_backend_pid()').scalar_one())
        entered.set()
        return move(library, entry, grouped, decisions, connection, releases)

    def remember_displaced_delete(item_ids, *args, **kwargs):
        result = remove(item_ids, *args, **kwargs)
        if displaced_id in item_ids:
            deleted.set()
        return result

    def scan():
        try:
            return lib.scan()
        finally:
            source_db.remove()

    monkeypatch.setattr(scanner, '_write_repathed', pause_after_group_planning)
    monkeypatch.setattr(scanner, '_write_final_path', remember_handover_connection)
    monkeypatch.setattr(repository, 'delete_items', remember_displaced_delete)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(scan)
        try:
            assert ready.wait(10)
            with source_transaction(source_db) as correction:
                assert repository.get_group(group.id, correction, for_update=True) is not None
                proceed.set()
                assert entered.wait(10)
                deadline = monotonic() + 5
                while not correction.execute(sa.text('select pg_blocking_pids(:pid)'),
                                             {'pid': scan_pids[0]}).scalar_one():
                    assert monotonic() < deadline, 'handover never waited for the destination show lock'
                    sleep(0.01)
                assert not deleted.is_set(), 'handover took the displaced episode lock first'
                repository.update_show_identity(group.id, correction, title='Corrected', sort_title='corrected',
                                                identity_source='user', identity_state='identified')
        finally:
            proceed.set()
        result = future.result(timeout=10)
    row = lib.items()[str(destination)]
    assert result.handed_over == 1 and row.id == incoming_id and row.group_id == group.id
    assert lib.group('Destination').identity_source == 'user'
    assert row.identity_state == 'parsed'
