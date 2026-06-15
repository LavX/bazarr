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
    # path_replace_instance / _reverse_instance are now the call sites; stub
    # them as identity so the instance-filter tests focus on scoping, not
    # path translation.
    monkeypatch.setattr(mo.path_mappings, "path_replace", lambda p: p)
    monkeypatch.setattr(mo.path_mappings, "path_replace_movie", lambda p: p)
    monkeypatch.setattr(mo.path_mappings, "path_replace_instance", lambda p, *a, **kw: p)
    monkeypatch.setattr(mo.path_mappings, "path_replace_reverse_instance", lambda p, *a, **kw: p)
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


def test_signalr_single_instance_binds_to_lone_instance(monkeypatch):
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances",
                        lambda kind: [types.SimpleNamespace(id=1, name="Sonarr")])

    singleton = sc.SonarrSignalrClient()
    extras = []
    clients = sc._start_clients_for_kind("sonarr", singleton, extras, sc.SonarrSignalrClient)

    # One enabled instance -> the singleton binds to that instance's id so the
    # live feed config, dispatch routing, and on-connect 'sync on live' all use
    # the per-instance path that matches the scheduler's update_series_<id> job
    # (the scalar config is stale now that the Host form is gone, #156).
    assert singleton.arr_instance_id == 1
    assert clients == [singleton]
    assert extras == []


def test_signalr_zero_instances_falls_back_to_scalar_path(monkeypatch):
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances", lambda kind: [])

    singleton = sc.SonarrSignalrClient()
    extras = []
    clients = sc._start_clients_for_kind("sonarr", singleton, extras, sc.SonarrSignalrClient)

    # No enabled instance -> the singleton falls back to the scalar/default path.
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


# --------------------------------------------------------------- INC8 SignalR multi-instance bug fixes


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeArrClient:
    """Minimal stand-in for arr_instances.client.ArrClient.

    Records the paths it is asked to GET and returns a canned system/status
    payload so the per-instance version gate can be probed without a network.
    """

    def __init__(self, version="4.0.0.0", base="http://4k:8990", key="k"):
        self.version = version
        self.api_key = key
        self._base = base
        self.got_paths = []

    def base_url(self):
        return self._base

    def get(self, path):
        self.got_paths.append(path)
        return _FakeResponse({"version": self.version})


def test_sonarr_start_gates_on_own_instance_status(monkeypatch):
    """Finding 1: a secondary Sonarr client must probe ITS instance's
    /api/v3/system/status, not the scalar default get_sonarr_info."""
    import app.signalr_client as sc

    # The scalar default probe would say "unknown" forever -> if start() used it
    # the secondary client would spin in its retry loop.
    monkeypatch.setattr(sc, "_sonarr_signalr_core_support_state",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("secondary must not probe the default")))

    fake = _FakeArrClient(version="4.0.9.2421-develop")
    monkeypatch.setattr(sc, "client_for_instance", lambda db, iid: fake)

    client = sc.SonarrSignalrClient(arr_instance_id=4)
    supports, version = client._support_state()

    assert supports is True
    assert version == "4.0.9.2421-develop"
    assert fake.got_paths == ["/api/v3/system/status"]


def test_sonarr_start_default_still_uses_scalar_probe(monkeypatch):
    """The scalar/default client (arr_instance_id None) keeps probing the shared
    get_sonarr_info path -> byte-identical to the legacy behaviour."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc, "_sonarr_signalr_core_support_state",
                        lambda: (True, "4.0.0.0"))
    # Touching client_for_instance for the default path would be wrong.
    monkeypatch.setattr(sc, "client_for_instance",
                        lambda db, iid: (_ for _ in ()).throw(
                            AssertionError("default path must not resolve an instance")))

    client = sc.SonarrSignalrClient(arr_instance_id=None)
    supports, version = client._support_state()

    assert supports is True
    assert version == "4.0.0.0"


def test_sonarr_per_instance_status_rejects_v3(monkeypatch):
    """A secondary instance on Sonarr v3 reports supports=False from its own
    status, not the default's."""
    import app.signalr_client as sc

    fake = _FakeArrClient(version="3.0.10.1567")
    monkeypatch.setattr(sc, "client_for_instance", lambda db, iid: fake)

    client = sc.SonarrSignalrClient(arr_instance_id=7)
    supports, version = client._support_state()

    assert supports is False
    assert version == "3.0.10.1567"


def test_sonarr_per_instance_status_unknown_when_unreachable(monkeypatch):
    """If the per-instance status probe fails, _support_state returns
    (None, 'unknown') so start() retries that instance rather than crashing."""
    import app.signalr_client as sc

    def boom(db, iid):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sc, "client_for_instance", boom)

    client = sc.SonarrSignalrClient(arr_instance_id=9)
    supports, version = client._support_state()

    assert supports is None
    assert version == "unknown"


# ---- Finding 2: re-fan-out always stops tracked extras

class _RecordingClient:
    """Tracks whether stop() was called regardless of transport state."""

    def __init__(self, active):
        self.arr_instance_id = 99
        self.connection = object() if active else None
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_refanout_stops_extra_mid_reconnect(monkeypatch):
    """Finding 2: an extra client whose connection is NOT in an active transport
    state (mid-reconnect) must still have stop() called before the list clears,
    so signalrcore's auto-reconnect thread is torn down."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances",
                        lambda kind: [types.SimpleNamespace(id=1, name="Sonarr")])
    # Force the "not active" branch even though a connection object exists.
    monkeypatch.setattr(sc, "_signalr_connection_active", lambda conn: False)

    orphan = _RecordingClient(active=True)
    extras = [orphan]
    singleton = sc.SonarrSignalrClient()

    sc._start_clients_for_kind("sonarr", singleton, extras, sc.SonarrSignalrClient)

    assert orphan.stopped is True
    assert extras == []


def test_refanout_stop_failure_does_not_block_clear(monkeypatch):
    """A stop() that raises must not prevent the extras list from clearing."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc.threading, "Thread", _FakeThread)
    monkeypatch.setattr(sc, "_enabled_instances",
                        lambda kind: [types.SimpleNamespace(id=1, name="Sonarr")])

    class _ExplodingClient(_RecordingClient):
        def stop(self):
            self.stopped = True
            raise RuntimeError("already gone")

    orphan = _ExplodingClient(active=False)
    extras = [orphan]
    singleton = sc.SonarrSignalrClient()

    sc._start_clients_for_kind("sonarr", singleton, extras, sc.SonarrSignalrClient)

    assert orphan.stopped is True
    assert extras == []


def test_stop_tolerates_none_connection():
    """Finding 2: stop() must not raise when the connection was never built or
    has already been torn down."""
    import app.signalr_client as sc

    client = sc.SonarrSignalrClient(arr_instance_id=5)
    client.connection = None
    # Must not raise.
    client.stop()

    radarr = sc.RadarrSignalrClient(arr_instance_id=6)
    radarr.connection = None
    radarr.stop()


# ---- Finding 3: on_connect_handler guards JobLookupError

def test_on_connect_handler_survives_missing_job(monkeypatch):
    """Finding 3: a per-instance client whose update_series_<id> job is not yet
    registered must not let JobLookupError escape on_connect."""
    import app.signalr_client as sc
    from apscheduler.jobstores.base import JobLookupError

    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)
    monkeypatch.setattr(sc.settings.sonarr, "series_sync_on_live", True)

    def raise_lookup(taskid=None):
        raise JobLookupError(taskid)

    monkeypatch.setattr(sc.scheduler, "execute_job_now", raise_lookup)

    client = sc.SonarrSignalrClient(arr_instance_id=4)
    # Must not raise.
    client.on_connect_handler()
    assert client.connected is True


def test_radarr_on_connect_handler_survives_missing_job(monkeypatch):
    import app.signalr_client as sc
    from apscheduler.jobstores.base import JobLookupError

    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)
    monkeypatch.setattr(sc.settings.radarr, "movies_sync_on_live", True)

    def raise_lookup(taskid=None):
        raise JobLookupError(taskid)

    monkeypatch.setattr(sc.scheduler, "execute_job_now", raise_lookup)

    client = sc.RadarrSignalrClient(arr_instance_id=3)
    client.on_connect_handler()
    assert client.connected is True


# ---- Finding 4: configure() handles a deleted instance (None client)

def test_sonarr_configure_handles_deleted_instance(monkeypatch):
    """Finding 4: client_for_instance returns None for a deleted/disabled row;
    configure() must log, mark disconnected, and return early without raising."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc, "client_for_instance", lambda db, iid: None)

    client = sc.SonarrSignalrClient(arr_instance_id=4)
    client.connected = True
    # Must not raise AttributeError on None.base_url().
    client.configure()

    assert client.connected is False
    assert client.connection is None


def test_radarr_configure_handles_deleted_instance(monkeypatch):
    import app.signalr_client as sc

    monkeypatch.setattr(sc, "client_for_instance", lambda db, iid: None)

    client = sc.RadarrSignalrClient(arr_instance_id=3)
    client.connected = True
    client.configure()

    assert client.connected is False
    assert client.connection is None


# ---- Finding 5: dispatcher metadata lookups are instance-scoped

def _seed_colliding_dispatcher_rows(session):
    """Series + movie sharing upstream ids across instances, distinct titles."""
    session.execute(insert(TableShows).values(
        id=10, sonarrSeriesId=5, arr_instance_id=1, path="/tv/a", title="Default Show", year=2001))
    session.execute(insert(TableShows).values(
        id=100, sonarrSeriesId=5, arr_instance_id=2, path="/tv/b", title="4k Show", year=2002))
    session.execute(insert(TableMovies).values(
        id=20, radarrId=5, arr_instance_id=1, monitored=True, path="/mov/a.mkv",
        title="Default Movie", year=2001, tmdbId=111, audio_language="[]"))
    session.execute(insert(TableMovies).values(
        id=200, radarrId=5, arr_instance_id=2, monitored=True, path="/mov/b.mkv",
        title="4k Movie", year=2002, tmdbId=222, audio_language="[]"))


def test_dispatcher_episode_title_lookup_scoped(schema_session, monkeypatch):
    """Finding 5: the episode-event series-title fallback must resolve the title
    from the OWNING instance's row, not a colliding upstream id elsewhere. The
    title flows into the debug log line; we capture it to prove the scope."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc, "database", schema_session)
    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)
    _seed_colliding_dispatcher_rows(schema_session)

    monkeypatch.setattr(sc, "sync_one_episode_for_instance", lambda *a, **k: None)
    monkeypatch.setattr(sc, "client_for_instance", lambda db, iid: None)

    logged = []
    monkeypatch.setattr(sc.logging, "debug", lambda msg, *a, **k: logged.append(msg))

    # Episode event from instance 2, body carries no nested series block so the
    # DB title fallback runs. seriesId=5 collides with instance 1's "Default Show".
    data = {
        "name": "episode",
        "_arr_instance_id": 2,
        "body": {
            "action": "updated",
            "resource": {
                "id": 77, "seriesId": 5, "title": "Ep", "seasonNumber": 1, "episodeNumber": 1,
            },
        },
    }

    sc.dispatcher(data)

    # The scoped lookup resolved instance 2's title ("4k Show"), never the
    # colliding instance-1 "Default Show".
    episode_logs = [m for m in logged if "Event received from Sonarr for episode" in m]
    assert episode_logs, "expected the episode debug log line"
    assert "4k Show" in episode_logs[0]
    assert "Default Show" not in episode_logs[0]


def test_dispatcher_deleted_movie_existence_scoped(schema_session, monkeypatch):
    """Finding 5: a 'deleted' movie event must check the OWNING instance's row.
    If only the OTHER instance has a row with that radarrId, the event for this
    instance must be treated as not-present (early return, no sync)."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc, "database", schema_session)
    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)
    # Only instance 1 owns radarrId=5; the event is tagged instance 2.
    schema_session.execute(insert(TableMovies).values(
        id=20, radarrId=5, arr_instance_id=1, monitored=True, path="/mov/a.mkv",
        title="Default Movie", year=2001, tmdbId=111, audio_language="[]"))

    called = {"sync": False}

    def fake_sync(arr_instance_id, media_id, action, **kw):
        called["sync"] = True

    monkeypatch.setattr(sc, "update_one_movie_for_instance", fake_sync)

    data = {
        "name": "movie",
        "_arr_instance_id": 2,
        "body": {"action": "deleted", "resource": {"id": 5}},
    }

    sc.dispatcher(data)

    # Instance 2 has no row for radarrId=5 -> early return, no sync fired.
    assert called["sync"] is False


def test_dispatcher_deleted_movie_owning_instance_syncs(schema_session, monkeypatch):
    """The owning instance's deleted event DOES fire the scoped sync."""
    import app.signalr_client as sc

    monkeypatch.setattr(sc, "database", schema_session)
    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)
    schema_session.execute(insert(TableMovies).values(
        id=20, radarrId=5, arr_instance_id=1, monitored=True, path="/mov/a.mkv",
        title="Default Movie", year=2001, tmdbId=111, audio_language="[]"))

    called = {}

    def fake_sync(arr_instance_id, media_id, action, **kw):
        called["instance"] = arr_instance_id
        called["media_id"] = media_id

    monkeypatch.setattr(sc, "update_one_movie_for_instance", fake_sync)

    data = {
        "name": "movie",
        "_arr_instance_id": 1,
        "body": {"action": "deleted", "resource": {"id": 5}},
    }

    sc.dispatcher(data)

    assert called["instance"] == 1
    assert called["media_id"] == 5


# ---- Finding 6: badges aggregate per-instance SignalR connected state

def test_all_sonarr_signalr_connected_requires_every_client(monkeypatch):
    """Finding 6: LIVE means the singleton AND every per-instance extra is
    connected; one down extra means not-all-connected."""
    import app.signalr_client as sc

    sc.sonarr_signalr_client.connected = True
    up = types.SimpleNamespace(connected=True)
    down = types.SimpleNamespace(connected=False)

    monkeypatch.setattr(sc, "_sonarr_signalr_clients", [up])
    assert sc.all_sonarr_signalr_connected() is True

    monkeypatch.setattr(sc, "_sonarr_signalr_clients", [up, down])
    assert sc.all_sonarr_signalr_connected() is False


def test_all_radarr_signalr_connected_requires_every_client(monkeypatch):
    import app.signalr_client as sc

    sc.radarr_signalr_client.connected = True
    monkeypatch.setattr(sc, "_radarr_signalr_clients",
                        [types.SimpleNamespace(connected=True)])
    assert sc.all_radarr_signalr_connected() is True

    monkeypatch.setattr(sc, "_radarr_signalr_clients",
                        [types.SimpleNamespace(connected=False)])
    assert sc.all_radarr_signalr_connected() is False


def test_all_signalr_connected_false_when_singleton_down(monkeypatch):
    """If the singleton itself is down, the aggregate is down even with no extras."""
    import app.signalr_client as sc

    sc.sonarr_signalr_client.connected = False
    monkeypatch.setattr(sc, "_sonarr_signalr_clients", [])
    assert sc.all_sonarr_signalr_connected() is False


# ---- Finding 7: SignalR event-to-instance routing (not just client tagging)

def test_signalr_receive_message_routes_event_to_client_instance(monkeypatch):
    """Finding 7: a receiveMessage handler bound for instance N must route its
    event through the dispatcher tagged with N, so the downstream
    update_one_series_for_instance receives that exact instance id."""
    import app.signalr_client as sc

    # Drive the real feed_queue -> dispatcher path with the client's instance id,
    # bypassing the live queue threads.
    captured = {}

    def fake_update(arr_instance_id, media_id, action, **kw):
        captured["instance"] = arr_instance_id
        captured["media_id"] = media_id

    monkeypatch.setattr(sc, "update_one_series_for_instance", fake_update)
    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)

    # A series event with no episodesChanged routes to update_one_series_for_instance.
    event = {
        "name": "series",
        "body": {"action": "updated", "resource": {"id": 42, "title": "S", "year": 2020}},
    }

    # feed_queue tags the event with the owning instance, then we dispatch it
    # directly (the queue thread is not running in tests).
    sc.feed_queue(dict(event), arr_instance_id=4)
    assert len(sc.sonarr_queue) == 1
    sc.dispatcher(sc.sonarr_queue.popleft())

    assert captured["instance"] == 4
    assert captured["media_id"] == 42


def test_signalr_receive_message_routes_movie_event_to_client_instance(monkeypatch):
    import app.signalr_client as sc

    captured = {}

    def fake_update(arr_instance_id, media_id, action, **kw):
        captured["instance"] = arr_instance_id

    monkeypatch.setattr(sc, "update_one_movie_for_instance", fake_update)
    monkeypatch.setattr(sc, "event_stream", lambda **kw: None)

    event = {
        "name": "movie",
        "body": {"action": "updated", "resource": {"id": 88, "title": "M", "year": 2021}},
    }

    sc.feed_queue(dict(event), arr_instance_id=3)
    assert len(sc.radarr_queue) == 1
    sc.dispatcher(sc.radarr_queue.popleft())

    assert captured["instance"] == 3
