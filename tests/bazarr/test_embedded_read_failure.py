# coding=utf-8
"""A failed embedded-track read keeps what the indexer stored before.

The read can fail for reasons that say nothing about the file's tracks: the
file is briefly unreadable, a network share drops, ffprobe fails. Every
missing-subtitle recompute reads the stored listing, so storing it without the
embedded tracks made each embedded language wanted, and the next search could
fetch a subtitle the file already carries. The database cases run on SQLite
and PostgreSQL, both on an AUTOCOMMIT engine.
"""
import ast
import json
import os

import pytest
import sqlalchemy as sa

from app.database import TableEpisodes, TableLanguagesProfiles, TableMovies, TableShows
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_embedded_track_hidden_indexer import PROFILE, _stored, _stub_indexer
from test_sportarr_indexer import indexed_library, row, sports  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401

STORED = str([["en", None, None, "subrip"], ["en:hi", None, None, "subrip"], ["fr", None, None, "pgs"],
              ["de", None, None], ["en", "/movies/m.en.srt", 10]])


def _unreadable(*args, **kwargs):
    raise OSError("the share dropped")


# What knowit reports for a readable file that carries no subtitle stream.
NO_SUBTITLE_STREAMS = {"video": [{"codec": "h264"}], "audio": [{"codec": "aac"}]}


def _read_through_knowit(module, session, monkeypatch, result):
    """Read the tracks through the real reader and parse_video_metadata, with
    knowit answering ``result``."""
    from utilities import binaries, video_analyzer

    monkeypatch.setattr(module, "embedded_subs_reader", video_analyzer.embedded_subs_reader)
    monkeypatch.setattr(video_analyzer, "database", session)
    monkeypatch.setattr(video_analyzer.settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(video_analyzer.settings.general, "enable_strm_support", False, raising=False)
    monkeypatch.setattr(binaries, "get_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(video_analyzer, "alpha3_from_alpha2", lambda code: None)
    monkeypatch.setattr(video_analyzer, "know", lambda *a, **kw: result)


def test_movie_keeps_its_embedded_tracks_when_the_read_fails(hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    logged = []
    _stub_indexer(mv, hidden_db, monkeypatch, logged)
    monkeypatch.setattr(mv, "embedded_subs_reader", _unreadable)
    monkeypatch.setattr(mv.settings.general, "ignore_pgs_subs", True, raising=False)
    hidden_db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                              movie_file_id=700, file_size=111, profileId=1, subtitles=STORED))
    hidden_db.commit()
    hide_track(hidden_db, "movie", 1, 1, "en:hi")

    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)

    # The embedded entries stay, less the hidden key and the codec the ignore
    # switch now skips. External files are discovered afresh, and this one is
    # gone. The kept tracks were recorded when they were read, not again now.
    assert _stored(hidden_db, TableMovies, 1) == (["en", "de"], ["fr"])
    stored = hidden_db.execute(sa.select(TableMovies.subtitles)).scalar_one()
    assert "['en', None, None, 'subrip']" in stored
    assert logged == []

    # knowit answers {} when its provider cannot load: a failed read too.
    _read_through_knowit(mv, hidden_db, monkeypatch, {})
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["en", "de"], ["fr"])

    # A good read of a file without subtitle streams does drop them.
    _read_through_knowit(mv, hidden_db, monkeypatch, NO_SUBTITLE_STREAMS)
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == ([], ["en", "fr"])
    assert logged == []


def test_episode_keeps_its_embedded_tracks_on_every_row_of_the_file_when_the_read_fails(
        hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.series as se
    from subtitles.embedded_hidden import hide_track

    logged = []
    _stub_indexer(se, hidden_db, monkeypatch, logged)
    monkeypatch.setattr(se, "embedded_subs_reader", _unreadable)
    hidden_db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    hidden_db.add(TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title="S", path="/tv/s", profileId=1))
    hidden_db.flush()
    stored = str([["en", None, None, "subrip"], ["en:hi", None, None, "subrip"], ["fr", None, None, "pgs"]])
    for episode_id in (1, 2):
        hidden_db.add(TableEpisodes(
            id=episode_id, arr_instance_id=1, series_id=1, sonarrSeriesId=1, sonarrEpisodeId=10 + episode_id,
            title="E", path="/tv/s/e01-e02.mkv", season=1, episode=episode_id, episode_file_id=500,
            file_size=111, subtitles=stored))
    hidden_db.commit()
    hide_track(hidden_db, "episode", 2, 1, "fr")

    se.store_subtitles("/tv/s/e01-e02.mkv", "/local/e01-e02.mkv", arr_instance_id=1)

    for episode_id in (1, 2):
        assert _stored(hidden_db, TableEpisodes, episode_id) == (["en", "en:hi"], ["fr"])
    assert logged == []

    _read_through_knowit(se, hidden_db, monkeypatch, {})
    se.store_subtitles("/tv/s/e01-e02.mkv", "/local/e01-e02.mkv", arr_instance_id=1)
    for episode_id in (1, 2):
        assert _stored(hidden_db, TableEpisodes, episode_id) == (["en", "en:hi"], ["fr"])

    _read_through_knowit(se, hidden_db, monkeypatch, NO_SUBTITLE_STREAMS)
    se.store_subtitles("/tv/s/e01-e02.mkv", "/local/e01-e02.mkv", arr_instance_id=1)
    for episode_id in (1, 2):
        assert _stored(hidden_db, TableEpisodes, episode_id) == ([], ["en", "fr"])
    assert logged == []


def test_sports_keeps_its_listing_when_the_probe_fails(indexed_library, monkeypatch):  # noqa: F811
    """The sports indexer commits before it probes. On a file it has no good
    read of, that commit emptied the listing, so a failed probe left the row
    with no subtitles at all, external files included."""
    from app import database as db
    from sportarr import library

    session, path = indexed_library
    module = sports(monkeypatch, session)
    items = [dict(id=i, language=language, hi="False", forced="False",
                  audio_exclude="False", audio_only_include="False")
             for i, language in enumerate(("fr", "en", "de"), 1)]
    session.execute(sa.insert(db.TableLanguagesProfiles).values(profileId=5, name="Sports", items=json.dumps(items)))
    db.update_profile_id_list.invalidate()
    assert library.assign_profile(session, 51, 1, 5)
    assert {item[0] for item in module.store_subtitles_sports(61, 1)} == {"fr", "en:hi"}
    listing = row(session, 61).subtitles
    assert ast.literal_eval(row(session, 61).missing_subtitles) == ["de"]

    # The file changes, so the cached read no longer applies, and the probe fails.
    video = path / "1" / "event.mkv"
    stat = video.stat()
    os.utime(video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10 ** 9))
    monkeypatch.setattr(module, "parse_video_metadata", lambda *a, **kw: None)
    with pytest.raises(OSError):
        module.store_subtitles_sports(61, 1)

    after = row(session, 61)
    assert after.subtitles == listing
    assert ast.literal_eval(after.missing_subtitles) == ["de"]
    # Still unindexed, so the next sync or scan retries it.
    assert after.ffprobe_cache is None
