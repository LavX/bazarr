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
