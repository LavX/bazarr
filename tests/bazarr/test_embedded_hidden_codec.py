# coding=utf-8
"""Hiding an embedded track and storing its codec, together.

The indexers store each embedded track as ``[language, None, None, codec]``
and skip a track the user hid. These cases run the real indexers with both at
once: a hidden track leaves the listing, every track left keeps its codec, and
an unhide brings the track back with its codec. The hide API reads the codec
the indexer stored, so the hidden-tracks listing can name it. Every case runs
on SQLite and on PostgreSQL.
"""
import ast
import json

import sqlalchemy as sa
from flask import Flask

from app.database import TableEmbeddedTrackHidden, TableEpisodes, TableLanguagesProfiles, TableMovies, TableShows
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_embedded_track_hidden_indexer import PROFILE, _stub_indexer
from test_sportarr_indexer import indexed_library, row, sports  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
ROOT = "/api/subtitles/embedded-hidden"
ENGLISH = ["en", None, None, "subrip"]
ENGLISH_HI = ["en:hi", None, None, "subrip"]
FRENCH_PGS = ["fr", None, None, "pgs"]


def _listing(session, table, row_id):
    session.expire_all()
    found = session.execute(sa.select(table.subtitles, table.missing_subtitles)
                            .where(table.id == row_id)).one()
    return ast.literal_eval(found.subtitles), ast.literal_eval(found.missing_subtitles)


def _movie(session):
    session.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            movie_file_id=700, file_size=111, profileId=1, subtitles="[]"))
    session.commit()


def test_a_movie_hide_leaves_every_other_track_with_its_codec(hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track, unhide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    _movie(hidden_db)

    hide_track(hidden_db, "movie", 1, 1, "fr", "pgs")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _listing(hidden_db, TableMovies, 1) == ([ENGLISH, ENGLISH_HI], ["fr"])

    unhide_track(hidden_db, "movie", [1], 1, "fr")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _listing(hidden_db, TableMovies, 1) == ([ENGLISH, ENGLISH_HI, FRENCH_PGS], [])


def test_an_episode_hide_leaves_every_other_track_with_its_codec(hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.series as se
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(se, hidden_db, monkeypatch, [])
    hidden_db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    hidden_db.add(TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title="S", path="/tv/s", profileId=1))
    hidden_db.flush()
    for episode_id in (1, 2):
        hidden_db.add(TableEpisodes(
            id=episode_id, arr_instance_id=1, series_id=1, sonarrSeriesId=1, sonarrEpisodeId=10 + episode_id,
            title="E", path="/tv/s/e01-e02.mkv", season=1, episode=episode_id, episode_file_id=500,
            file_size=111, subtitles="[]"))
    hidden_db.commit()

    hide_track(hidden_db, "episode", 1, 1, "en:hi", "subrip")
    se.store_subtitles("/tv/s/e01-e02.mkv", "/local/e01-e02.mkv", arr_instance_id=1)
    for episode_id in (1, 2):
        assert _listing(hidden_db, TableEpisodes, episode_id) == ([ENGLISH, FRENCH_PGS], [])


def test_a_sports_hide_leaves_every_other_track_with_its_codec(indexed_library, monkeypatch):  # noqa: F811
    from app import database as db
    from sportarr import library
    from subtitles.embedded_hidden import hide_track, unhide_track

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    real = module.embedded_subtitles_from_metadata
    # ffmpeg cannot encode a bitmap track from text, so the PGS stream is added
    # to what the real probe of the fixture reports.
    monkeypatch.setattr(module, "embedded_subtitles_from_metadata",
                        lambda data: real(data) + [["deu", False, False, "PGS"]])
    items = [dict(id=i, language=language, hi="False", forced="False",
                  audio_exclude="False", audio_only_include="False")
             for i, language in enumerate(("fr", "de"), 1)]
    session.execute(sa.insert(db.TableLanguagesProfiles).values(profileId=5, name="Sports",
                                                                items=json.dumps(items)))
    db.update_profile_id_list.invalidate()
    assert library.assign_profile(session, 51, 1, 5)

    hide_track(session, "sports", 61, 1, "de", "pgs")
    stored = module.store_subtitles_sports(61, 1)
    assert [entry for entry in stored if not entry[1]] == [["fr", None, None, "subrip"]]
    assert [entry[0] for entry in stored if entry[1]] == ["en:hi"]
    assert ast.literal_eval(row(session, 61).subtitles) == stored
    assert ast.literal_eval(row(session, 61).missing_subtitles) == ["de"]

    unhide_track(session, "sports", [61], 1, "de")
    stored = module.store_subtitles_sports(61, 1)
    assert [entry for entry in stored if not entry[1]] == [["fr", None, None, "subrip"],
                                                           ["de", None, None, "pgs"]]
    assert ast.literal_eval(row(session, 61).missing_subtitles) == []


def test_the_api_records_the_codec_the_indexer_stored(hidden_db, monkeypatch):  # noqa: F811
    """No hand-written listing here: the real movie indexer writes the codec,
    the hide reads it back into its record, and the re-index it runs keeps the
    codec of every track it leaves."""
    from api import api_bp
    from api.subtitles import embedded_hidden as api_mod
    from app.config import settings as app_settings
    import subtitles.indexer.movies as mv

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    monkeypatch.setitem(app_settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(api_mod, "database", hidden_db)
    monkeypatch.setattr(api_mod, "language_from_alpha2", {"en": "English", "fr": "French"}.get)
    monkeypatch.setattr(api_mod.path_mappings, "path_replace_instance", lambda path, owner, kind: "/local/m.mkv")
    monkeypatch.setattr(api_mod, "event_stream", lambda **kw: None)
    _movie(hidden_db)
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _listing(hidden_db, TableMovies, 1) == ([ENGLISH, ENGLISH_HI, FRENCH_PGS], [])

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    client = app.test_client()
    form = {"media_type": "movie", "media_id": 1, "arr_instance_id": 1, "language": "fr"}

    hidden = client.post(ROOT, data=form, headers=HEADERS)
    assert hidden.status_code == 200
    assert hidden.json["data"]["wanted"] is True
    hidden_db.expire_all()
    assert hidden_db.execute(sa.select(TableEmbeddedTrackHidden.language, TableEmbeddedTrackHidden.codec)
                             ).all() == [("fr", "pgs")]
    assert _listing(hidden_db, TableMovies, 1) == ([ENGLISH, ENGLISH_HI], ["fr"])
    listed = client.get(ROOT + "?media_type=movie&media_id[]=1", headers=HEADERS)
    assert [(item["language"], item["codec"]) for item in listed.json["data"]] == [("fr", "pgs")]

    assert client.delete(ROOT, data=form, headers=HEADERS).status_code == 204
    assert _listing(hidden_db, TableMovies, 1) == ([ENGLISH, ENGLISH_HI, FRENCH_PGS], [])
