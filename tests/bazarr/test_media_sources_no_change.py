"""No source, no change: the endpoints an arr install reads answer the same bytes with source rows in the database.

A source adds rows to its own tables and never to an arr table, and no existing list reads them (the lists gain
source rows only on an explicit request, in a later change). These tests seed an arr library with its history and
blacklist, record the raw bytes of every list the Movies, Series and Sports pages read, then add a source with
items, a download, a sync and a blacklisted release, and compare the bytes again. With ``sources=1`` on an install
that has no source of the kind the answer is today's as well.
"""
import hashlib
import json
import sys
from datetime import datetime

import pytest
import sqlalchemy as sa
from flask import Flask

from media_source_helpers import add_profile, add_source, video

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}

# Every list the Movies, Series and Sports pages and their Wanted, History and Blacklist pages read, plus the
# badge counts and the global search.
ENDPOINTS = [
    "/api/movies", "/api/series", "/api/sports/leagues", "/api/movies/wanted", "/api/episodes/wanted",
    "/api/sports/wanted", "/api/movies/history", "/api/episodes/history", "/api/sports/history",
    "/api/movies/blacklist", "/api/episodes/blacklist", "/api/sports/blacklist", "/api/badges",
    "/api/system/searches?query=a",
]


@pytest.fixture
def everything_uses_the_test_database(source_db, monkeypatch):
    """Every module that imported the application's session gets the test one, so the real list endpoints run."""
    from app import database as db

    original = {name: module for name, module in sys.modules.items()
                if module is not None and getattr(module, "database", None) is not None}
    # The fixture already replaced app.database.database; the others still hold the original object.
    originals = {name for name, module in original.items()
                 if getattr(module, "database", None).__class__.__name__ == "scoped_session"
                 and module is not db}
    for name in originals:
        monkeypatch.setattr(original[name], "database", source_db, raising=False)
    return source_db


@pytest.fixture
def arr_library(everything_uses_the_test_database, monkeypatch, languages_loaded):
    from app.config import settings
    from app.database import (TableArrInstances, TableBlacklist, TableBlacklistMovie, TableBlacklistSports,
                              TableEpisodes, TableHistory, TableHistoryMovie, TableHistorySports, TableMovies,
                              TableShows, TableSportsEvents, TableSportsLeagues)

    session = everything_uses_the_test_database
    profile = add_profile(session, [("en", "False", "False")])
    for key in ("use_sonarr", "use_radarr", "use_sportarr"):
        monkeypatch.setattr(settings.general, key, True)
    monkeypatch.setitem(settings.auth, "apikey", "synthetic-bazarr-key")
    now = datetime(2026, 1, 2, 3, 4, 5)
    for number, kind in enumerate(("sonarr", "radarr", "sportarr"), start=1):
        session.execute(sa.insert(TableArrInstances).values(
            id=number, kind=kind, name=kind.title(), stable_key=kind, port=8000 + number, enabled=1,
            is_default=1, path_mappings="[]"))
    session.execute(sa.insert(TableShows).values(
        id=1, arr_instance_id=1, sonarrSeriesId=11, title="Arr Show", path="/arr/tv/Arr Show", profileId=profile,
        monitored="True", seriesType="standard", tags="[]", alternativeTitles="[]", audio_language="[]", year="2020",
        sortTitle="arr show"))
    session.execute(sa.insert(TableEpisodes).values(
        id=1, arr_instance_id=1, series_id=1, sonarrSeriesId=11, sonarrEpisodeId=111, season=1, episode=1,
        title="Pilot", path="/arr/tv/Arr Show/S01E01.mkv", missing_subtitles="['en']", subtitles="[]",
        monitored="True", audio_language="[]"))
    session.execute(sa.insert(TableMovies).values(
        id=1, arr_instance_id=2, radarrId=21, title="Arr Movie", path="/arr/movies/Arr Movie.mkv", tmdbId="603",
        profileId=profile, missing_subtitles="['en']", subtitles="[]", monitored="True", audio_language="[]",
        tags="[]", alternativeTitles="[]", year="1999", sortTitle="arr movie"))
    session.execute(sa.insert(TableHistory).values(
        id=1, arr_instance_id=1, series_id=1, episode_id=1, sonarrSeriesId=11, sonarrEpisodeId=111, action=1,
        description="downloaded", language="en", provider="alpha", score=90, timestamp=now,
        video_path="/arr/tv/Arr Show/S01E01.mkv"))
    session.execute(sa.insert(TableHistoryMovie).values(
        id=1, arr_instance_id=2, movie_id=1, radarrId=21, action=1, description="downloaded", language="en",
        provider="alpha", score=90, timestamp=now, video_path="/arr/movies/Arr Movie.mkv"))
    session.execute(sa.insert(TableBlacklist).values(
        id=1, arr_instance_id=1, series_id=1, episode_id=1, sonarr_series_id=11, sonarr_episode_id=111,
        provider="alpha", subs_id="a1", language="en", timestamp=now))
    session.execute(sa.insert(TableBlacklistMovie).values(
        id=1, arr_instance_id=2, movie_id=1, radarr_id=21, provider="alpha", subs_id="m1", language="en",
        timestamp=now))
    session.execute(sa.insert(TableSportsLeagues).values(
        id=1, arr_instance_id=3, sportarrLeagueId=31, title="Arr League", sortTitle="arr league", profileId=profile,
        monitored="True", path="/arr/sports/Arr League"))
    session.execute(sa.insert(TableSportsEvents).values(
        id=1, arr_instance_id=3, league_id=1, sportarrEventId=41, file_id=51, season=2024, episode=1,
        path="/arr/sports/Arr League/Final.mkv", title="Final", missing_subtitles="['en']", subtitles="[]",
        monitored="True", audio_language="[]"))
    session.execute(sa.insert(TableHistorySports).values(
        id=1, arr_instance_id=3, league_id=1, event_id=1, action=1, description="downloaded", language="en",
        provider="alpha", score=90, timestamp=now, video_path="/arr/sports/Arr League/Final.mkv"))
    session.execute(sa.insert(TableBlacklistSports).values(
        id=1, arr_instance_id=3, league_id=1, event_id=1, provider="alpha", subs_id="sp1", language="en",
        timestamp=now))
    return session


@pytest.fixture
def client(arr_library):
    from api import api_bp

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


def _read(client, endpoints):
    out = {}
    for url in endpoints:
        response = client.get(url, headers=HEADERS)
        out[url] = (response.status_code, hashlib.sha256(response.data).hexdigest(), response.data)
    return out


def _add_a_source_with_everything(session, tmp_path, monkeypatch):
    """A folder source with a scanned movie, a download in its history, a sync, and a blacklisted release."""
    from media_sources import history, repository, scanner
    from media_sources.context import resolve_item
    from subtitles.processing import ProcessSubtitlesResult

    source_id, library_id, root = add_source(session, tmp_path, profile_id=1)
    video(root / "Source.Movie.2001.1080p.mkv")
    monkeypatch.setattr("media_sources.indexer.get_language_set", lambda: set())
    scanner.run_scan(source_id)
    (item,) = repository.list_items(library_id=library_id)
    context = resolve_item(item.id)
    result = ProcessSubtitlesResult("downloaded", context.path, "en", "alpha", 90, False, "s1",
                                    context.path.replace(".mkv", ".en.srt"), False)
    history.history_log_source(1, context, result)
    history.history_log_source(5, context, result)
    history.blacklist_log_source(context, "alpha", "release-x", "en")
    return source_id


def test_every_list_an_arr_install_reads_answers_the_same_bytes_with_a_source_in_the_database(
        client, arr_library, tmp_path, monkeypatch):
    before = _read(client, ENDPOINTS)
    # The seeded library is really being listed: a test that compared empty answers would prove nothing.
    for url in ("/api/movies", "/api/series", "/api/movies/wanted", "/api/episodes/wanted", "/api/movies/history",
                "/api/episodes/history", "/api/movies/blacklist", "/api/episodes/blacklist", "/api/sports/leagues",
                "/api/sports/wanted", "/api/sports/history", "/api/sports/blacklist"):
        status, _, body = before[url]
        assert status == 200, (url, body[:200])
        assert len(json.loads(body)["data"]) >= 1, url
    for url in ENDPOINTS:
        assert before[url][0] == 200, (url, before[url][2][:300])

    _add_a_source_with_everything(arr_library, tmp_path, monkeypatch)

    after = _read(client, ENDPOINTS)
    assert {url: entry[1] for url, entry in after.items()} == {url: entry[1] for url, entry in before.items()}
    assert {url: entry[2] for url, entry in after.items()} == {url: entry[2] for url, entry in before.items()}


def test_sources_equals_one_on_an_install_with_no_source_changes_nothing(client, arr_library):
    plain = _read(client, ENDPOINTS)
    for url in ENDPOINTS:
        joined = url + ("&" if "?" in url else "?") + "sources=1"
        assert _read(client, [joined])[joined][2] == plain[url][2], url


def test_the_arr_tables_are_not_touched_by_a_source(client, arr_library, tmp_path, monkeypatch):
    from media_source_helpers import ARR_AND_SPORTS_TABLES, table_counts

    before = table_counts(arr_library, ARR_AND_SPORTS_TABLES)
    _add_a_source_with_everything(arr_library, tmp_path, monkeypatch)
    assert table_counts(arr_library, ARR_AND_SPORTS_TABLES) == before


def test_the_repository_imports_while_the_database_module_is_a_stub(monkeypatch):
    """Importing the API package imports the repository, and some test files do that with ``app.database``
    replaced by a stub whose classes carry no ``__table__``."""
    import importlib
    import types

    class Stub(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return type(name, (), {})

    for name in ("media_sources.repository", "media_sources.db"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "app.database", Stub("app.database"))
    repository = importlib.import_module("media_sources.repository")
    assert repository.Items is None and repository.ITEM_KINDS
