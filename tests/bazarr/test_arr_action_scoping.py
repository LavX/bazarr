# coding=utf-8
"""Per-instance routing for the batch (Mass Translate/sync/mods) collect path
and the SignalR client fan-out (#156).

Both shipped deploy-verified; these add unit coverage for the genuinely-new
logic:

* ``mass_operations._collect_subtitle_items`` must drop a collected row whose
  owner differs from the instance the caller requested, so a colliding upstream
  id under another instance is never operated on. A requested instance of None
  (legacy/single-instance) imposes no filter -> byte-identical.
* The SignalR manager (``_start_clients_for_kind``) keeps the scalar/default
  client (``arr_instance_id`` None) for a single enabled instance, and fans out
  one tagged client per instance when there is more than one - mirroring the
  scheduler fan-out.
"""
import os
import types

from sqlalchemy import insert

from app.database import TableShows, TableEpisodes, TableMovies


# --------------------------------------------------------------- #15 batch collect

def _seed_colliding_episodes(session):
    """Two shows+episodes that share sonarrEpisodeId=1 across instances 1 and 2,
    with distinct local ids (the post-cutover reality)."""
    session.execute(insert(TableShows).values(
        id=10, sonarrSeriesId=1, arr_instance_id=1, path="/tv/a", title="A"))
    session.execute(insert(TableShows).values(
        id=100, sonarrSeriesId=1, arr_instance_id=2, path="/tv/b", title="B"))
    session.execute(insert(TableEpisodes).values(
        id=50, series_id=10, sonarrSeriesId=1, sonarrEpisodeId=1, arr_instance_id=1,
        monitored=True, path="/tv/a/ep.mkv", audio_language="[]", title="A E1",
        season=1, episode=1, subtitles="[['en', '/tv/a/ep.en.srt']]"))
    session.execute(insert(TableEpisodes).values(
        id=500, series_id=100, sonarrSeriesId=1, sonarrEpisodeId=1, arr_instance_id=2,
        monitored=True, path="/tv/b/ep.mkv", audio_language="[]", title="B E1",
        season=1, episode=1, subtitles="[['en', '/tv/b/ep.en.srt']]"))


def _patch_collect_io(monkeypatch, mo, schema_session):
    """Bind the global DB to the test session and stub the file-system checks so
    the instance filter (which runs first) is what decides inclusion."""
    monkeypatch.setattr(mo, "database", schema_session)
    monkeypatch.setattr(mo, "is_sync_engine_output", lambda p: False)
    monkeypatch.setattr(mo.path_mappings, "path_replace", lambda p: p)
    monkeypatch.setattr(mo.path_mappings, "path_replace_movie", lambda p: p)
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    # The real parser needs the app's languages_dict (loaded at startup, absent
    # in tests); the collect loop only consumes forced/hi from it.
    monkeypatch.setattr(mo, "languages_from_colon_seperated_string",
                        lambda s: {"forced": ":forced" in s, "hi": ":hi" in s})


def test_collect_episodes_scoped_to_requested_instance(schema_session, monkeypatch):
    import subtitles.mass_operations as mo
    _patch_collect_io(monkeypatch, mo, schema_session)
    _seed_colliding_episodes(schema_session)

    # Caller asks for the episode owned by instance 2.
    items = [{"type": "episode", "sonarrEpisodeId": 1, "arr_instance_id": 2}]
    collected, _ = mo._collect_subtitle_items(items, "sync", {"force_resync": True})

    # Instance 1's colliding row (same sonarrEpisodeId) is dropped.
    assert len(collected) == 1
    assert collected[0]["arr_instance_id"] == 2
    assert collected[0]["video_path"] == "/tv/b/ep.mkv"
    assert collected[0]["srt_path"] == "/tv/b/ep.en.srt"


def test_collect_episodes_unscoped_default_sees_all(schema_session, monkeypatch):
    import subtitles.mass_operations as mo
    _patch_collect_io(monkeypatch, mo, schema_session)
    _seed_colliding_episodes(schema_session)

    # arr_instance_id None == legacy single-instance path: no filter, byte-identical.
    items = [{"type": "episode", "sonarrEpisodeId": 1, "arr_instance_id": None}]
    collected, _ = mo._collect_subtitle_items(items, "sync", {"force_resync": True})

    assert len(collected) == 2
    assert {c["arr_instance_id"] for c in collected} == {1, 2}


def test_collect_movies_scoped_to_requested_instance(schema_session, monkeypatch):
    import subtitles.mass_operations as mo
    _patch_collect_io(monkeypatch, mo, schema_session)

    # Two movies sharing radarrId=1 across instances 2 and 3, distinct local ids.
    schema_session.execute(insert(TableMovies).values(
        id=20, radarrId=1, arr_instance_id=2, monitored=True, path="/mov/a.mkv",
        title="Movie A", tmdbId=111, audio_language="[]",
        subtitles="[['en', '/mov/a.en.srt']]"))
    schema_session.execute(insert(TableMovies).values(
        id=200, radarrId=1, arr_instance_id=3, monitored=True, path="/mov/b.mkv",
        title="Movie B", tmdbId=222, audio_language="[]",
        subtitles="[['en', '/mov/b.en.srt']]"))

    items = [{"type": "movie", "radarrId": 1, "arr_instance_id": 3}]
    collected, _ = mo._collect_subtitle_items(items, "sync", {"force_resync": True})

    assert len(collected) == 1
    assert collected[0]["arr_instance_id"] == 3
    assert collected[0]["video_path"] == "/mov/b.mkv"
    assert collected[0]["radarr_id"] == 1


# --------------------------------------------------------------- INC8 SignalR fan-out

class _FakeThread:
    """Records the target but never runs it (no real connection in tests)."""

    def __init__(self, target=None, **kwargs):
        self.target = target
        self.daemon = False

    def start(self):
        return None


def test_signalr_single_instance_keeps_scalar_path(monkeypatch):
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances",
                        lambda kind: [types.SimpleNamespace(id=1, name="Sonarr")])

    singleton = sc.SonarrSignalrClient()
    extras = []
    clients = sc._start_clients_for_kind("sonarr", singleton, extras, sc.SonarrSignalrClient)

    # One enabled instance -> the singleton stays on the scalar/default path.
    assert singleton.arr_instance_id is None
    assert clients == [singleton]
    assert extras == []


def test_signalr_multi_instance_fans_out_per_instance(monkeypatch):
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances",
                        lambda kind: [types.SimpleNamespace(id=1, name="Sonarr"),
                                      types.SimpleNamespace(id=4, name="4k Sonarr")])

    singleton = sc.SonarrSignalrClient()
    extras = []
    clients = sc._start_clients_for_kind("sonarr", singleton, extras, sc.SonarrSignalrClient)

    # >1 instance -> singleton handles the first, one extra tagged client per rest.
    assert singleton.arr_instance_id == 1
    assert len(clients) == 2
    assert len(extras) == 1
    assert extras[0].arr_instance_id == 4
    assert all(c.arr_instance_id is not None for c in clients)


def test_signalr_radarr_multi_instance_fans_out(monkeypatch):
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances",
                        lambda kind: [types.SimpleNamespace(id=2, name="Radarr"),
                                      types.SimpleNamespace(id=3, name="4k Radarr")])

    singleton = sc.RadarrSignalrClient()
    extras = []
    clients = sc._start_clients_for_kind("radarr", singleton, extras, sc.RadarrSignalrClient)

    assert singleton.arr_instance_id == 2
    assert [c.arr_instance_id for c in clients] == [2, 3]
