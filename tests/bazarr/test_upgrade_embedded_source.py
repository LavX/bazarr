# coding=utf-8
"""A newer Embedded Source row ends the upgrade candidacy of older downloads.

The indexer writes an Embedded Source row (action 7) the first time it finds a
track of a language in the video. From then on the language is there at source
quality, and the file at an older download's path need no longer be that
download: a tool that copies the track out next to the video and then strips it
from the container leaves the track as an ordinary file at exactly that path.
The upgrade job used to take the older download as its candidate all the same,
and deleted that file as the one it replaced.

Plain and HI count as one language here, as they do in the upgrade's fallback
to the current file of a language. Forced stays apart.
"""
# ruff: noqa: F811
from datetime import datetime, timedelta

import pytest
from sqlalchemy import insert, update

from app.database import TableEpisodes, TableHistory, TableHistoryMovie, TableMovies
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_extracted_history import upgrade_db, upgrade_run  # noqa: F401
from test_upgrade_embedded_history import _insert_episode, _insert_movie

MOVIE_VIDEO = "/movies/movie.mkv"
EPISODE_VIDEO = "/series/show/s01e01.mkv"


def _movie_row(history_id, action, minutes_ago, language="en", score=90, owner=2, video_path=MOVIE_VIDEO,
               subtitles_path="/movies/movie.en.srt", **extra):
    embedded = action == 7
    return dict(id=history_id, arr_instance_id=owner, movie_id=30, radarrId=30, action=action, description="x",
                language=language, provider="embedded" if embedded else "opensubtitlescom",
                score=100 if embedded else score, score_out_of=100,
                timestamp=datetime.now() - timedelta(minutes=minutes_ago), video_path=video_path,
                subtitles_path=None if embedded else subtitles_path, **extra)


def _episode_row(history_id, action, minutes_ago, language="en", score=90, owner=1, video_path=EPISODE_VIDEO,
                 subtitles_path="/series/show/s01e01.en.srt", **extra):
    embedded = action == 7
    return dict(id=history_id, arr_instance_id=owner, series_id=10, episode_id=20, sonarrEpisodeId=20,
                sonarrSeriesId=10, action=action, description="x", language=language,
                provider="embedded" if embedded else "opensubtitlescom",
                score=100 if embedded else score, score_out_of=100,
                timestamp=datetime.now() - timedelta(minutes=minutes_ago), video_path=video_path,
                subtitles_path=None if embedded else subtitles_path, **extra)


def _history(db, *, download, embedded):
    """The same download and Embedded Source rows for the movie and the episode."""
    _insert_movie(db)
    _insert_episode(db)
    db.execute(insert(TableHistoryMovie).values(**_movie_row(201, 1, **download)))
    db.execute(insert(TableHistoryMovie).values(**_movie_row(202, 7, **embedded)))
    db.execute(insert(TableHistory).values(**_episode_row(101, 1, **download)))
    db.execute(insert(TableHistory).values(**_episode_row(102, 7, **embedded)))


def _candidates():
    """What the upgrade jobs would take, and what the library markers show."""
    from api.subtitles.batch import get_upgradable_media_ids
    from subtitles.upgrade import get_upgradable_episode_subtitles, get_upgradable_movies_subtitles

    markers = get_upgradable_media_ids()
    return (get_upgradable_movies_subtitles(), get_upgradable_episode_subtitles(),
            markers["movies"], markers["series"])


ENDED = ({}, {}, [], [])
KEPT = ({201: None}, {101: None}, [30], [10])


@pytest.mark.parametrize("download_language,embedded_language,expected", [
    ("en", "en", ENDED),
    ("en", "en:hi", ENDED),
    ("en:hi", "en", ENDED),
    ("en:hi", "en:hi", ENDED),
    ("en:forced", "en:forced", ENDED),
    ("en", "en:forced", KEPT),
    ("en:forced", "en", KEPT),
    ("en:forced", "en:hi", KEPT),
    ("en", "fr", KEPT),
], ids=["same", "plain-hi", "hi-plain", "hi-hi", "forced-forced", "plain-forced", "forced-plain", "forced-hi",
        "other-language"])
def test_a_newer_embedded_track_ends_an_older_download_of_its_language(upgrade_db, download_language,
                                                                        embedded_language, expected):
    _history(upgrade_db, download=dict(minutes_ago=60, language=download_language),
             embedded=dict(minutes_ago=1, language=embedded_language))

    assert _candidates() == expected


def test_a_download_after_the_embedded_track_is_still_a_candidate(upgrade_db):
    """The track was found first and a subtitle was downloaded later, after the
    track was stripped and its file deleted, say. The file at the download's
    path is then that download."""
    _history(upgrade_db, download=dict(minutes_ago=1), embedded=dict(minutes_ago=60))

    assert _candidates() == KEPT


@pytest.mark.parametrize("other", [dict(owner=9), dict(video_path="/elsewhere/video.mkv")], ids=["owner", "video"])
def test_an_embedded_track_of_another_owner_or_video_ends_nothing(upgrade_db, other):
    _history(upgrade_db, download=dict(minutes_ago=60), embedded=dict(minutes_ago=1, **other))

    assert _candidates() == KEPT


@pytest.mark.parametrize("track_found", [False, True], ids=["no-track", "track-found"])
def test_the_upgrade_job_keeps_the_file_a_stripped_track_was_copied_to(upgrade_run, track_found):
    """The reported case: a download, its upgrade, then the release's own track
    found in the video (Embedded Source at 100%). Another tool then copied the
    track out over the download's file and stripped it from the container. The
    upgrade stayed the newest download of the language and its path was on
    disk, so the job replaced the track's file with any provider subtitle that
    scored higher."""
    upgrade, calls = upgrade_run
    db = upgrade.database
    movie_file, episode_file = "/movies/movie.pt-BR.srt", "/series/show/s01e01.pt-BR.srt"
    _insert_movie(db)
    _insert_episode(db)
    db.execute(update(TableMovies).values(subtitles=str([["pb", movie_file, 10]])))
    db.execute(update(TableEpisodes).values(subtitles=str([["pb", episode_file, 10]])))
    for rows, table, row, path in ((range(201, 204), TableHistoryMovie, _movie_row, movie_file),
                                   (range(101, 104), TableHistory, _episode_row, episode_file)):
        first, upgraded, track = rows
        db.execute(insert(table).values(**row(first, 1, 60 * 24 * 2, language="pb", score=87,
                                              subtitles_path=path)))
        db.execute(insert(table).values(**row(upgraded, 3, 60 * 24, language="pb", score=93,
                                              subtitles_path=path, upgradedFromId=first)))
        if track_found:
            db.execute(insert(table).values(**row(track, 7, 60 * 15, language="pb")))

    upgrade.upgrade_movies_subtitles(job_id=1)
    upgrade.upgrade_episodes_subtitles(job_id=1)

    assert calls == ([] if track_found else [movie_file, episode_file])
