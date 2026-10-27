# coding=utf-8
"""An Embedded Source row stands for a track the indexer counts.

The row ends the upgrades of its language, so it has to go when the track stops
counting: hidden by the user, or skipped by an ignore switch. A track that is
only gone from the container keeps its row, because another tool may have
copied it out next to the video and the row is the only trace of that. The
database cases run on SQLite and PostgreSQL.
"""
# ruff: noqa: F811
import json
from datetime import datetime

import pytest
import sqlalchemy as sa

from app.database import (TableEpisodes, TableHistory, TableHistoryMovie, TableLanguagesProfiles, TableMovies,
                          TableShows)
# Pytest fixture, used by name below: the import is what registers it here.
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_embedded_track_hidden_indexer import PROFILE, _stored, _stub_indexer

MOVIE_PATH = "/movies/m.mkv"
EPISODE_PATH = "/tv/s/e01-e02.mkv"


@pytest.mark.parametrize("key,recorded", [
    ("en", "en"), ("en:hi", "en:hi"), ("en:forced", "en:forced"),
    ("en:hi:forced", "en:forced"), ("en:forced:hi", "en:forced"),
])
def test_a_forced_track_is_recorded_as_forced_even_when_it_is_also_hi(key, recorded):
    from subtitles.indexer.utils import embedded_history_language

    assert embedded_history_language(key) == recorded


def _movie_history(db, language, path=MOVIE_PATH, owner=1, movie_id=1, action=7):
    db.execute(sa.insert(TableHistoryMovie).values(
        arr_instance_id=owner, movie_id=movie_id, radarrId=7, action=action, description="x", language=language,
        provider="embedded", score=100, score_out_of=100, timestamp=datetime.now(), video_path=path))


def _movie_rows(db):
    return sorted((row.arr_instance_id, row.video_path, row.language) for row in db.execute(
        sa.select(TableHistoryMovie.arr_instance_id, TableHistoryMovie.video_path, TableHistoryMovie.language)
        .where(TableHistoryMovie.action == 7)).all())


def test_retraction_deletes_only_the_uncounted_rows_of_this_video_and_owner(hidden_db):
    from subtitles.indexer.utils import retract_embedded_history

    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path=MOVIE_PATH, tmdbId="1"))
    hidden_db.add(TableMovies(id=2, arr_instance_id=2, radarrId=7, title="M", path=MOVIE_PATH, tmdbId="1"))
    hidden_db.commit()
    for language in ("en", "en:hi", "fr"):
        _movie_history(hidden_db, language)
    _movie_history(hidden_db, "en:hi", owner=2, movie_id=2)
    _movie_history(hidden_db, "en:hi", path="/movies/other.mkv")
    _movie_history(hidden_db, "en:hi", action=1)

    deleted = retract_embedded_history(hidden_db, TableHistoryMovie, MOVIE_PATH, {"en:hi", "fr"}, ["fr:forced"], 1)

    assert deleted == 2
    assert _movie_rows(hidden_db) == [(1, MOVIE_PATH, "en"), (1, "/movies/other.mkv", "en:hi"),
                                      (2, MOVIE_PATH, "en:hi")]
    assert hidden_db.execute(sa.select(sa.func.count()).select_from(TableHistoryMovie)
                             .where(TableHistoryMovie.action == 1)).scalar() == 1


def test_a_language_another_counted_track_covers_keeps_its_row(hidden_db):
    from subtitles.indexer.utils import retract_embedded_history

    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path=MOVIE_PATH, tmdbId="1"))
    hidden_db.commit()
    _movie_history(hidden_db, "en:forced")

    # A forced and HI track hidden, a forced-only track still listed: both are recorded as en:forced.
    assert retract_embedded_history(hidden_db, TableHistoryMovie, MOVIE_PATH, {"en:forced"}, ["en:forced"], 1) == 0
    assert _movie_rows(hidden_db) == [(1, MOVIE_PATH, "en:forced")]


def _stub_movie(db, monkeypatch):
    import subtitles.indexer.movies as mv

    _stub_indexer(mv, db, monkeypatch, [])
    db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path=MOVIE_PATH, tmdbId="1",
                       movie_file_id=700, file_size=111, profileId=1, subtitles="[]"))
    db.add(TableMovies(id=2, arr_instance_id=2, radarrId=7, title="M", path=MOVIE_PATH, tmdbId="1",
                       movie_file_id=800, file_size=222, profileId=1, subtitles="[]"))
    db.commit()
    for language in ("en", "en:hi", "fr"):
        _movie_history(db, language)
    _movie_history(db, "en:hi", owner=2, movie_id=2)
    return mv


def test_hiding_a_track_takes_its_row_and_leaves_the_others_and_the_other_owner(hidden_db, monkeypatch):
    from subtitles.embedded_hidden import hide_track

    mv = _stub_movie(hidden_db, monkeypatch)
    hide_track(hidden_db, "movie", 1, 1, "en:hi")

    mv.store_subtitles_movie(MOVIE_PATH, "/local/m.mkv", arr_instance_id=1)

    assert _movie_rows(hidden_db) == [(1, MOVIE_PATH, "en"), (1, MOVIE_PATH, "fr"), (2, MOVIE_PATH, "en:hi")]


def test_an_ignored_codec_takes_its_row(hidden_db, monkeypatch):
    mv = _stub_movie(hidden_db, monkeypatch)
    monkeypatch.setattr(mv.settings.general, "ignore_pgs_subs", True, raising=False)

    mv.store_subtitles_movie(MOVIE_PATH, "/local/m.mkv", arr_instance_id=1)

    assert _movie_rows(hidden_db) == [(1, MOVIE_PATH, "en"), (1, MOVIE_PATH, "en:hi"), (2, MOVIE_PATH, "en:hi")]


def test_a_language_a_listed_track_still_covers_keeps_its_row(hidden_db, monkeypatch):
    """An SRT and a PGS English: ignoring PGS leaves the SRT, which covers it."""
    mv = _stub_movie(hidden_db, monkeypatch)
    monkeypatch.setattr(mv.settings.general, "ignore_pgs_subs", True, raising=False)
    monkeypatch.setattr(mv, "embedded_subs_reader", lambda *a, **kw: [
        ["eng", False, False, "subrip"], ["eng", False, False, "pgs"]])

    mv.store_subtitles_movie(MOVIE_PATH, "/local/m.mkv", arr_instance_id=1)

    assert (1, MOVIE_PATH, "en") in _movie_rows(hidden_db)
    assert _stored(hidden_db, TableMovies, 1)[0] == ["en"]


def test_a_track_gone_from_the_container_keeps_its_row(hidden_db, monkeypatch):
    """The reported flow: another tool copies the track out next to the video
    and strips it. The next index finds no track, and the row must stay."""
    mv = _stub_movie(hidden_db, monkeypatch)
    monkeypatch.setattr(mv, "embedded_subs_reader", lambda *a, **kw: [])

    mv.store_subtitles_movie(MOVIE_PATH, "/local/m.mkv", arr_instance_id=1)

    assert len(_movie_rows(hidden_db)) == 4


def test_an_unreadable_file_keeps_every_row(hidden_db, monkeypatch):
    from subtitles.embedded_hidden import hide_track

    mv = _stub_movie(hidden_db, monkeypatch)
    hide_track(hidden_db, "movie", 1, 1, "en:hi")

    def unreadable(*args, **kwargs):
        raise OSError("cannot read")

    monkeypatch.setattr(mv, "embedded_subs_reader", unreadable)
    mv.store_subtitles_movie(MOVIE_PATH, "/local/m.mkv", arr_instance_id=1)

    assert len(_movie_rows(hidden_db)) == 4


def test_hiding_an_episode_track_takes_the_row_of_every_episode_in_the_file(hidden_db, monkeypatch):
    import subtitles.indexer.series as se
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(se, hidden_db, monkeypatch, [])
    hidden_db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    hidden_db.add(TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title="S", path="/tv/s", profileId=1))
    hidden_db.flush()
    for episode_id in (1, 2):
        hidden_db.add(TableEpisodes(
            id=episode_id, arr_instance_id=1, series_id=1, sonarrSeriesId=1, sonarrEpisodeId=10 + episode_id,
            title="E", path=EPISODE_PATH, season=1, episode=episode_id, episode_file_id=500, file_size=111,
            subtitles="[]"))
    hidden_db.commit()
    for episode_id in (1, 2):
        for language in ("en", "fr"):
            hidden_db.execute(sa.insert(TableHistory).values(
                arr_instance_id=1, series_id=1, episode_id=episode_id, sonarrSeriesId=1,
                sonarrEpisodeId=10 + episode_id, action=7, description="x", language=language,
                provider="embedded", score=100, score_out_of=100, timestamp=datetime.now(),
                video_path=EPISODE_PATH))
    hide_track(hidden_db, "episode", 2, 1, "fr")

    se.store_subtitles(EPISODE_PATH, "/local/e01-e02.mkv", arr_instance_id=1)

    kept = hidden_db.execute(sa.select(TableHistory.episode_id, TableHistory.language)
                             .where(TableHistory.action == 7)).all()
    assert sorted(tuple(row) for row in kept) == [(1, "en"), (2, "en")]
