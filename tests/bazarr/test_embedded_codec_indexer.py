# coding=utf-8
"""The movie and episode indexers store the codec of every embedded track.

Runs on SQLite, and on PostgreSQL when BAZARR_PG_TEST_URL is set, because the
column's format changes for every media row in every library.
"""
# ruff: noqa: F811
import ast

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session, sessionmaker
from test_sportarr_kind_migration import migration_engine  # noqa: F401

LANGUAGES = [
    {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
    {"code2": "fr", "code3": "fra", "code3b": "fre", "name": "French"},
    {"code2": "de", "code3": "deu", "code3b": "ger", "name": "German"},
]

# (alpha3, forced, hi, knowit format) as embedded_subs_reader yields them.
TRACKS = [
    ["eng", False, False, "SubRip"],
    ["eng", False, True, "PGS"],
    ["fra", True, False, "VobSub"],
    ["deu", False, False, None],
]

EXPECTED = [
    ["en", None, None, "subrip"],
    ["en:hi", None, None, "pgs"],
    ["fr:forced", None, None, "vobsub"],
    ["de", None, None, None],
]


@pytest.fixture
def library(migration_engine, monkeypatch, tmp_path):
    from app import database as db
    from app.config import settings
    from languages import get_languages
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se

    db.Base.metadata.create_all(migration_engine)
    session = scoped_session(sessionmaker(bind=migration_engine))
    monkeypatch.setattr(get_languages, "languages_dict", LANGUAGES, raising=False)
    for name, value in (("use_embedded_subs", True), ("ignore_pgs_subs", False),
                        ("ignore_vobsub_subs", False), ("ignore_ass_subs", False),
                        ("subfolder", "current")):
        monkeypatch.setattr(settings.general, name, value)
    for module in (mv, se):
        monkeypatch.setattr(module, "database", session)
        monkeypatch.setattr(module, "embedded_subs_reader", lambda *a, **kw: [list(t) for t in TRACKS])
        monkeypatch.setattr(module, "search_external_subtitles", lambda *a, **kw: {})
        monkeypatch.setattr(module, "add_sync_engine_outputs", lambda folder, subs, **kw: subs)
        monkeypatch.setattr(module, "add_combined_outputs", lambda folder, subs, **kw: subs)
        monkeypatch.setattr(module, "guess_external_subtitles", lambda *a, **kw: {})
    monkeypatch.setattr(mv, "list_missing_subtitles_movies", lambda *a, **kw: None)
    monkeypatch.setattr(mv, "_log_embedded_history_movie", lambda *a, **kw: None)
    monkeypatch.setattr(se, "list_missing_subtitles", lambda *a, **kw: None)
    monkeypatch.setattr(se, "_log_embedded_history", lambda *a, **kw: None)

    video = tmp_path / "Video.mkv"
    video.write_bytes(b"not a real video")
    session.execute(sa.insert(db.TableShows).values(id=1, sonarrSeriesId=10, title="Show", path="/tv/show"))
    session.execute(sa.insert(db.TableEpisodes).values(
        id=1, series_id=1, sonarrSeriesId=10, sonarrEpisodeId=20, title="Pilot", season=1, episode=1,
        path="/tv/show/s01e01.mkv", episode_file_id=5, file_size=16,
        # A row indexed by 2.7.x: the embedded track has no codec yet.
        subtitles="[['en', None, None]]"))
    session.execute(sa.insert(db.TableMovies).values(
        id=1, radarrId=30, title="Movie", tmdbId="100", path="/movies/movie.mkv",
        movie_file_id=6, file_size=16, subtitles="[['en', None, None]]"))
    session.commit()
    yield mv, se, session, str(video)
    session.remove()


def _stored(session, table):
    session.expire_all()
    return ast.literal_eval(session.execute(sa.select(table.subtitles)).scalar_one())


def test_movie_index_stores_the_codec_of_every_embedded_track(library):
    from app.database import TableMovies

    mv, _se, session, video = library

    returned = mv.store_subtitles_movie("/movies/movie.mkv", video, arr_instance_id=None)

    assert returned == EXPECTED
    assert _stored(session, TableMovies) == EXPECTED


def test_episode_index_stores_the_codec_of_every_embedded_track(library):
    from app.database import TableEpisodes

    _mv, se, session, video = library

    returned = se.store_subtitles("/tv/show/s01e01.mkv", video, arr_instance_id=None)

    assert returned == EXPECTED
    assert _stored(session, TableEpisodes) == EXPECTED


def test_ignored_codecs_are_still_skipped(library, monkeypatch):
    from app.config import settings
    from app.database import TableMovies

    mv, _se, session, video = library
    monkeypatch.setattr(settings.general, "ignore_pgs_subs", True)
    monkeypatch.setattr(settings.general, "ignore_vobsub_subs", True)
    monkeypatch.setattr(mv, "embedded_subs_reader",
                        lambda *a, **kw: [list(t) for t in TRACKS if t[3] is not None])

    mv.store_subtitles_movie("/movies/movie.mkv", video, arr_instance_id=None)

    assert _stored(session, TableMovies) == [["en", None, None, "subrip"]]


def test_reindexed_row_reaches_the_api_with_its_codec(library, monkeypatch):
    from api import utils
    from app.database import TableMovies

    mv, _se, session, video = library
    monkeypatch.setattr(utils.settings.general, "embedded_subs_show_desired", False)
    monkeypatch.setattr(utils.path_mappings, "path_replace_movie", lambda path: path)
    session.expire_all()
    legacy = session.execute(sa.select(TableMovies.subtitles)).scalar_one()
    assert [s["codec"] for s in utils.postprocess({"radarrId": 30, "subtitles": legacy})["subtitles"]] == [None]

    mv.store_subtitles_movie("/movies/movie.mkv", video, arr_instance_id=None)
    session.expire_all()
    current = session.execute(sa.select(TableMovies.subtitles)).scalar_one()
    by_language = {s["language"]: s["codec"]
                   for s in utils.postprocess({"radarrId": 30, "subtitles": current})["subtitles"]}

    assert by_language == {"en": "subrip", "en:hi": "pgs", "fr:forced": "vobsub", "de": None}
