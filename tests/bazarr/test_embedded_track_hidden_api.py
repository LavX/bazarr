# coding=utf-8
"""POST and DELETE /api/subtitles/embedded-hidden, and the listing GET.

Hide never touches the media file: it records the track, re-reads the file's
tracks through the normal indexer and pushes the events a Delete pushes. Every
case runs on SQLite and on PostgreSQL, both on an AUTOCOMMIT engine, because
the undo after a failed re-index is hand-written for that engine.
"""
import ast
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from flask import Flask

from app.database import TableEmbeddedTrackHidden, TableEpisodes, TableMovies, TableShows
from test_embedded_track_hidden import hidden_db  # noqa: F401

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
ROOT = "/api/subtitles/embedded-hidden"


@pytest.fixture
def hidden_api(hidden_db, monkeypatch, tmp_path):  # noqa: F811
    from api import api_bp
    from api.subtitles import embedded_hidden as api_mod
    from app.config import settings as app_settings
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se
    import subtitles.indexer.sports as sp

    session = hidden_db
    monkeypatch.setitem(app_settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(api_mod, "database", session)
    monkeypatch.setattr(api_mod, "language_from_alpha2", {"en": "English", "fr": "French"}.get)
    monkeypatch.setattr(api_mod.path_mappings, "path_replace_instance",
                        lambda path, owner, kind: str(tmp_path) + path)
    # "indexed" stands in for what the real indexer writes back. By default it
    # applies the recorded hides to the listings; a test that reads the POST's
    # answer, or needs the indexer to misbehave, sets its own.
    calls = {"reindex": [], "events": [], "indexed": lambda: _apply_hides(session)}
    monkeypatch.setattr(api_mod, "event_stream", lambda **kw: calls["events"].append(kw))

    def reindexed(*call):
        calls["reindex"].append(call)
        calls["indexed"]()

    monkeypatch.setattr(mv, "store_subtitles_movie",
                        lambda original, mapped, **kw: reindexed("movie", original, mapped, kw))
    monkeypatch.setattr(se, "store_subtitles",
                        lambda original, mapped, **kw: reindexed("episode", original, mapped, kw))
    monkeypatch.setattr(sp, "store_subtitles_sports",
                        lambda event_id, owner, **kw: reindexed("sports", event_id, owner))

    (tmp_path / "movies").mkdir()
    (tmp_path / "movies" / "m.mkv").write_bytes(b"video")
    (tmp_path / "tv").mkdir()
    (tmp_path / "tv" / "e01-e02.mkv").write_bytes(b"video")
    session.add_all([
        TableMovies(id=1, arr_instance_id=2, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                    # A four-element entry carries the track's codec; a
                    # three-element one is what older indexes stored.
                    subtitles=str([["en", None, None, "subrip"], ["fr", None, None],
                                   ["de", "/movies/m.de.srt", 10]])),
        TableMovies(id=3, arr_instance_id=2, radarrId=9, title="Gone", path="/movies/gone.mkv", tmdbId="3",
                    subtitles=str([["en", None, None]])),
        TableShows(id=1, arr_instance_id=1, sonarrSeriesId=5, title="S", path="/tv"),
    ])
    session.flush()
    for episode_id in (1, 2):
        session.add(TableEpisodes(
            id=episode_id, arr_instance_id=1, series_id=1, sonarrSeriesId=5, sonarrEpisodeId=50 + episode_id,
            title="E", path="/tv/e01-e02.mkv", season=1, episode=episode_id,
            subtitles=str([["fr", None, None]])))
    session.commit()

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client(), session, calls


def _apply_hides(session):
    """What the real indexers do with a hide: the embedded track leaves the
    listing of its row, and of every episode row sharing the file."""
    from app.database import TableSportsEvents
    from subtitles.embedded_hidden import applicable_owners, episode_file_siblings

    tables = {"movie": TableMovies, "episode": TableEpisodes, "sports": TableSportsEvents}
    records = session.execute(sa.select(TableEmbeddedTrackHidden.media_type, TableEmbeddedTrackHidden.media_id,
                                        TableEmbeddedTrackHidden.arr_instance_id,
                                        TableEmbeddedTrackHidden.language)).all()
    for media_type, media_id, owner, language in records:
        table = tables[media_type]
        ids = (episode_file_siblings(session, [media_id])[media_id] if media_type == "episode"
               else {media_id})
        for row in session.execute(sa.select(table.id, table.arr_instance_id, table.subtitles)
                                   .where(table.id.in_(ids))).all():
            if owner not in applicable_owners(row.arr_instance_id):
                continue
            kept = [entry for entry in ast.literal_eval(row.subtitles or "[]")
                    if not (entry[0] == language and not (len(entry) > 1 and entry[1]))]
            session.execute(sa.update(table).where(table.id == row.id).values(subtitles=str(kept)))


def _indexes_as(session, table, row_id, subtitles, missing):
    """What the indexer would write for the row after the re-index."""
    return lambda: session.execute(sa.update(table).where(table.id == row_id).values(
        subtitles=str(subtitles), missing_subtitles=str(missing)))


def _hidden(session):
    session.expire_all()
    return session.execute(sa.select(TableEmbeddedTrackHidden.media_type, TableEmbeddedTrackHidden.media_id,
                                     TableEmbeddedTrackHidden.arr_instance_id, TableEmbeddedTrackHidden.language,
                                     TableEmbeddedTrackHidden.codec)
                           .order_by(TableEmbeddedTrackHidden.id)).all()


def test_requires_authentication(hidden_api):
    client, _session, _calls = hidden_api
    assert client.get(ROOT + "?media_type=movie&media_id[]=1").status_code == 401
    assert client.post(ROOT, data={"media_type": "movie", "media_id": 1, "language": "en"}).status_code == 401


def test_hiding_a_movie_track_records_it_and_reindexes(hidden_api, tmp_path):
    client, session, calls = hidden_api
    calls["indexed"] = _indexes_as(session, TableMovies, 1, [["fr", None, None], ["de", "/movies/m.de.srt", 10]],
                                   ["en"])
    form = {"media_type": "movie", "media_id": 1, "arr_instance_id": 2, "language": "en",
            "hi": "False", "forced": "False"}
    hidden = client.post(ROOT, data=form, headers=HEADERS)
    assert hidden.status_code == 200
    assert hidden.json["data"] == {
        "wanted": True,
        "hidden": {"code2": "en", "name": "English", "hi": False, "forced": False},
        "covered_by": [],
    }
    assert _hidden(session) == [("movie", 1, 2, "en", "subrip")]
    assert calls["reindex"] == [("movie", "/movies/m.mkv", str(tmp_path) + "/movies/m.mkv",
                                 {"arr_instance_id": 2})]
    assert calls["events"] == [{"type": "movie-wanted", "action": "update", "payload": 7}]

    # The re-index took the track out of the listing, so a second request
    # finds nothing to hide and records nothing more.
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 404
    assert len(_hidden(session)) == 1

    listed = client.get(ROOT + "?media_type=movie&media_id[]=1", headers=HEADERS)
    assert listed.status_code == 200
    assert [(r["media_id"], r["language"], r["name"], r["hi"], r["forced"], r["codec"])
            for r in listed.json["data"]] == [(1, "en", "English", False, False, "subrip")]
    assert listed.json["data"][0]["hidden_at"]


def test_a_legacy_three_element_entry_hides_without_a_codec(hidden_api):
    client, session, _calls = hidden_api
    form = {"media_type": "movie", "media_id": 1, "language": "fr"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 200
    assert _hidden(session) == [("movie", 1, 2, "fr", None)]


def test_one_hide_covers_and_records_every_track_with_that_key(hidden_api):
    """An SRT and a PGS English on one remux share the key "en". The wanted
    computation cannot tell them apart, so one hide takes both, and the record
    names both codecs rather than whichever came first."""
    client, session, _calls = hidden_api
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(
        subtitles=str([["en", None, None, "subrip"], ["en", None, None, "pgs"], ["fr", None, None]])))
    form = {"media_type": "movie", "media_id": 1, "language": "en"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 200
    assert _hidden(session) == [("movie", 1, 2, "en", "pgs, subrip")]


def test_hide_says_when_another_subtitle_still_covers_the_language(hidden_api):
    """The wanted computation counts an HI track as good as a normal one, and a
    subtitle file counts too, so hiding the plain English track can leave
    English satisfied. The answer says so, and names what still covers it."""
    client, session, calls = hidden_api
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(
        subtitles=str([["en", None, None], ["en:hi", None, None], ["fr", None, None]])))
    calls["indexed"] = _indexes_as(session, TableMovies, 1, [["en:hi", None, None], ["fr", None, None],
                                                             ["en", "/movies/m.en.srt", 10]], [])
    hidden = client.post(ROOT, data={"media_type": "movie", "media_id": 1, "language": "en"}, headers=HEADERS)
    assert hidden.status_code == 200
    assert hidden.json["data"] == {
        "wanted": False,
        "hidden": {"code2": "en", "name": "English", "hi": False, "forced": False},
        "covered_by": [
            {"code2": "en", "name": "English", "hi": True, "forced": False, "embedded": True},
            {"code2": "en", "name": "English", "hi": False, "forced": False, "embedded": False},
        ],
    }
    # Still recorded: the track is hidden, whatever else covers the language.
    assert _hidden(session) == [("movie", 1, 2, "en", None)]


@pytest.mark.parametrize("form,status", [
    ({"media_type": "movie", "media_id": 1, "language": "de"}, 404),       # external file, not a track
    ({"media_type": "movie", "media_id": 1, "language": "en", "hi": "True"}, 404),
    ({"media_type": "movie", "media_id": 1, "arr_instance_id": 9, "language": "en"}, 404),
    ({"media_type": "movie", "media_id": 404, "language": "en"}, 404),
    ({"media_type": "series", "media_id": 1, "language": "en"}, 400),
    ({"media_type": "movie", "media_id": 0, "language": "en"}, 400),
    ({"media_type": "movie", "media_id": 1, "language": "../en"}, 400),
    ({"media_type": "movie", "media_id": 3, "language": "en"}, 409),       # video file not reachable
])
def test_refusals_record_nothing(hidden_api, form, status):
    client, session, calls = hidden_api
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == status
    assert _hidden(session) == []
    assert calls["reindex"] == []


def test_an_episode_hide_covers_every_row_of_the_same_file(hidden_api):
    client, session, calls = hidden_api
    form = {"media_type": "episode", "media_id": 2, "arr_instance_id": 1, "language": "fr"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 200
    assert _hidden(session) == [("episode", 2, 1, "fr", None)]
    assert calls["events"] == [{"type": "series", "action": "update", "payload": 5},
                               {"type": "episode-wanted", "action": "update", "payload": 52}]

    listed = client.get(ROOT + "?media_type=episode&media_id[]=1&media_id[]=2", headers=HEADERS).json["data"]
    assert [(r["media_id"], r["language"]) for r in listed] == [(1, "fr"), (2, "fr")]

    # Unhide from the other episode of the file removes the one record.
    form["media_id"] = 1
    assert client.delete(ROOT, data=form, headers=HEADERS).status_code == 204
    assert _hidden(session) == []
    assert calls["reindex"][-1][0] == "episode"


def test_the_listing_takes_a_whole_long_series(hidden_api):
    """The page asks in batches of 100, but the server takes any number: the
    ids and their file siblings are read in chunks under the IN-clause limit."""
    from subtitles.embedded_hidden import hide_track

    client, session, _calls = hidden_api
    hide_track(session, "episode", 2, 1, "fr")
    query = "&".join(f"media_id[]={media_id}" for media_id in range(1, 1201))
    listed = client.get(f"{ROOT}?media_type=episode&{query}", headers=HEADERS)
    assert listed.status_code == 200
    assert [(r["media_id"], r["language"]) for r in listed.json["data"]] == [(1, "fr"), (2, "fr")]


def test_a_hide_made_before_the_row_had_an_owner_survives_the_owner_stamp(hidden_api, tmp_path):
    """The hide of a row without an owner is stored under the sentinel. When the
    startup backfill later stamps the row, the hide is still listed and Unhide
    still removes it."""
    client, session, _calls = hidden_api
    (tmp_path / "movies" / "unowned.mkv").write_bytes(b"video")
    session.add(TableMovies(id=4, arr_instance_id=None, radarrId=11, title="Unowned", path="/movies/unowned.mkv",
                            tmdbId="4", subtitles=str([["en", None, None]])))
    session.commit()
    form = {"media_type": "movie", "media_id": 4, "language": "en"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 200
    assert _hidden(session) == [("movie", 4, 0, "en", None)]

    session.execute(sa.update(TableMovies).where(TableMovies.id == 4).values(arr_instance_id=2))
    listed = client.get(ROOT + "?media_type=movie&media_id[]=4", headers=HEADERS).json["data"]
    assert [(r["media_id"], r["language"], r["arr_instance_id"]) for r in listed] == [(4, "en", None)]
    assert client.delete(ROOT, data=form, headers=HEADERS).status_code == 204
    assert _hidden(session) == []


def test_the_listing_leaves_out_a_record_of_another_owner(hidden_api):
    """A record under another owner belongs to a deleted row whose local id was
    reused. The indexer ignores it, so the disclosure must not show it."""
    from subtitles.embedded_hidden import hide_track

    client, session, _calls = hidden_api
    hide_track(session, "movie", 1, 9, "fr")
    hide_track(session, "movie", 1, 2, "en")
    listed = client.get(ROOT + "?media_type=movie&media_id[]=1", headers=HEADERS).json["data"]
    assert [(r["language"], r["arr_instance_id"]) for r in listed] == [("en", 2)]


def test_a_reindex_that_fails_takes_the_hide_back(hidden_api, monkeypatch):
    """Whatever stops the re-index, the listing still shows the track, so the
    hide is taken back instead of surprising the user at the next index."""
    import subtitles.indexer.movies as mv

    client, session, _calls = hidden_api

    def _fails(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mv, "store_subtitles_movie", _fails)
    form = {"media_type": "movie", "media_id": 1, "language": "en"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 500
    assert _hidden(session) == []


def test_a_reindex_that_fails_after_writing_the_listing_keeps_the_hide(hidden_api, monkeypatch):
    """When the re-index stops after it wrote the listing, the hide took effect
    and the record stays, so the page, which refreshes after an error too,
    shows the hide and can undo it. The request still fails: what is wanted now
    was not recomputed, so there is no outcome to report."""
    import subtitles.indexer.movies as mv

    client, session, _calls = hidden_api

    def _writes_then_fails(*_args, **_kwargs):
        session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(
            subtitles=str([["fr", None, None], ["de", "/movies/m.de.srt", 10]])))
        raise RuntimeError("could not recompute the missing subtitles")

    monkeypatch.setattr(mv, "store_subtitles_movie", _writes_then_fails)
    form = {"media_type": "movie", "media_id": 1, "language": "en"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 500
    assert _hidden(session) == [("movie", 1, 2, "en", "subrip")]


def test_a_reindex_that_leaves_the_track_listed_takes_the_hide_back(hidden_api):
    """A re-index can return without applying the hide: the file went away
    after the check, or the hidden lookup failed and listed the track. The
    track is still listed, so the hide is taken back and the request says so."""
    client, session, calls = hidden_api
    calls["indexed"] = lambda: None
    form = {"media_type": "movie", "media_id": 1, "language": "en"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 409
    assert _hidden(session) == []


def test_an_unreadable_listing_counts_as_still_listed(hidden_api, monkeypatch):
    """When the listing cannot be read back, the hide must not stay behind."""
    from api.subtitles import embedded_hidden as api_mod

    class Broken:
        def execute(self, *_args, **_kwargs):
            raise sa.exc.OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(api_mod, "database", Broken())
    assert api_mod._still_listed("movie", 1, "en") is True


def test_unhide_succeeds_when_the_reindex_fails(hidden_api, monkeypatch):
    """Unhide has deleted the record by the time it re-indexes, and the next
    index lists the track again, so a failed re-index is logged, not returned."""
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    client, session, _calls = hidden_api
    hide_track(session, "movie", 1, 2, "en")

    def _fails(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mv, "store_subtitles_movie", _fails)
    form = {"media_type": "movie", "media_id": 1, "language": "en"}
    assert client.delete(ROOT, data=form, headers=HEADERS).status_code == 204
    assert _hidden(session) == []


def test_unhide_is_idempotent_and_scoped(hidden_api):
    client, session, _calls = hidden_api
    client.post(ROOT, data={"media_type": "movie", "media_id": 1, "language": "en"}, headers=HEADERS)
    assert client.delete(ROOT, data={"media_type": "movie", "media_id": 1, "language": "fr"},
                         headers=HEADERS).status_code == 204
    assert len(_hidden(session)) == 1
    assert client.delete(ROOT, data={"media_type": "movie", "media_id": 1, "arr_instance_id": 9,
                                     "language": "en"}, headers=HEADERS).status_code == 404
    assert client.delete(ROOT, data={"media_type": "movie", "media_id": 1, "language": "en"},
                         headers=HEADERS).status_code == 204
    assert _hidden(session) == []


def test_sports_resolves_the_owned_event_and_reindexes_it(hidden_api, monkeypatch, tmp_path):
    client, session, calls = hidden_api
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from sportarr import identity, workflows

    video = tmp_path / "event.mkv"
    video.write_bytes(b"video")
    session.execute(sa.insert(TableArrInstances).values(id=3, kind="sportarr", name="s", stable_key="s",
                                                        port=1867, path_mappings="[]"))
    session.execute(sa.insert(TableSportsLeagues).values(id=51, arr_instance_id=3, sportarrLeagueId=7,
                                                         title="L"))
    session.execute(sa.insert(TableSportsEvents).values(
        id=61, arr_instance_id=3, league_id=51, sportarrEventId=8, file_id=9, path="/sports/event.mkv",
        title="Card", subtitles=str([["fr", None, None]])))
    monkeypatch.setattr(workflows, "require_sports_enabled", lambda: None)
    monkeypatch.setattr(identity, "resolve_event_in_session", lambda _db, event_id, owner: SimpleNamespace(
        event_id=event_id, arr_instance_id=3, original_path="/sports/event.mkv", mapped_path=str(video)))

    form = {"media_type": "sports", "media_id": 61, "arr_instance_id": 3, "language": "fr"}
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 200
    assert _hidden(session) == [("sports", 61, 3, "fr", None)]
    assert calls["reindex"] == [("sports", 61, 3)]
    assert calls["events"] == [{"type": "sports", "action": "update", "payload": 61}]

    def _fails(event_id, owner, **kw):
        raise OSError("Could not analyze sports video")

    import subtitles.indexer.sports as sp
    monkeypatch.setattr(sp, "store_subtitles_sports", _fails)
    # Unhide still succeeds: the record goes, and the next index that can
    # read the file lists the track again.
    assert client.delete(ROOT, data=form, headers=HEADERS).status_code == 204
    assert _hidden(session) == []
    # That next index, standing in for the one that failed here.
    session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 61).values(
        subtitles=str([["fr", None, None]])))
    # Hide refuses instead, and takes its record back, since the track would
    # stay listed.
    assert client.post(ROOT, data=form, headers=HEADERS).status_code == 409
    assert _hidden(session) == []
