# coding=utf-8
"""A failed embedded-track read keeps what the indexer stored before.

The read can fail for reasons that say nothing about the file's tracks: the
file is briefly unreadable, a network share drops, ffprobe fails. Every
missing-subtitle recompute reads the stored listing, so storing it without the
embedded tracks made each embedded language wanted, and the next search could
fetch a subtitle the file already carries. The database cases run on SQLite
and PostgreSQL, both on an AUTOCOMMIT engine.
"""
import json

import sqlalchemy as sa

from app.database import TableLanguagesProfiles, TableMovies
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_embedded_track_hidden_indexer import PROFILE, _stored, _stub_indexer

STORED = str([["en", None, None, "subrip"], ["en:hi", None, None, "subrip"], ["fr", None, None, "pgs"],
              ["de", None, None], ["en", "/movies/m.en.srt", 10]])


def _unreadable(*args, **kwargs):
    raise OSError("the share dropped")


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
