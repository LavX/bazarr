# coding=utf-8
"""INC4: orchestrators stamp arr_instance_id on every media row write (#156).

After the local-id PK cutover, the backfill stamps existing rows but the sync
orchestrators did not stamp NEW rows, so shows/movies/episodes synced after an
upgrade accumulated arr_instance_id=NULL (unowned). INC4 closes that: every
insert/update resolves the owning instance (the enabled default for the
single-instance path) and stamps it, touching only written VALUES - never a
WHERE clause or a delete - so the default path stays byte-identical.

These drive the real orchestrator write paths against an in-memory DB with the
parser + I/O leaves stubbed (INC4 changes only the stamping that happens AFTER
the parser returns, so stubbing the parser isolates the seam under test).

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 4).
"""
import semver

from sqlalchemy import insert, select

from app.database import TableEpisodes, TableMovies, TableShows


def _noop(*args, **kwargs):
    return None


def _seed_default(session, kind):
    """Create the enabled default instance for a kind and return its id."""
    from arr_instances.repository import ArrInstanceRepository

    inst = ArrInstanceRepository(session).create(kind, kind.capitalize())
    session.flush()
    return inst.id


class _SonarrInfoStub:
    # >= 4.0.9.2421 so sync_episodes skips the legacy episodeFile backfill fetch
    def semver(self):
        return semver.Version(4, 0, 10, 0)


class _DummyJobs:
    def add_job_from_function(self, *a, **k):
        return None

    def update_job_progress(self, *a, **k):
        return None

    def update_job_name(self, *a, **k):
        return None

    def feed_jobs_pending_queue(self, *a, **k):
        return None


# ---------------------------------------------------------------- resolver units

def test_default_instance_id_returns_enabled_default(schema_session):
    from arr_instances.resolution import default_instance_id

    sonarr_id = _seed_default(schema_session, "sonarr")
    radarr_id = _seed_default(schema_session, "radarr")

    assert default_instance_id(schema_session, "sonarr") == sonarr_id
    assert default_instance_id(schema_session, "radarr") == radarr_id


def test_default_instance_id_none_when_no_instances(schema_session):
    from arr_instances.resolution import default_instance_id

    assert default_instance_id(schema_session, "sonarr") is None


def test_stamp_owner_guards_against_none():
    from arr_instances.resolution import stamp_owner

    assert stamp_owner({}, None) == {}  # never writes a literal None owner
    assert stamp_owner({}, 7) == {"arr_instance_id": 7}


def test_sonarr_series_owner_reads_parent(schema_session):
    from arr_instances.resolution import sonarr_series_owner

    inst = _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=inst, path="/tv/s", title="S"))

    assert sonarr_series_owner(schema_session, 5) == (inst, 5)


def test_sonarr_series_owner_missing_parent_falls_back_to_default(schema_session):
    from arr_instances.resolution import sonarr_series_owner

    inst = _seed_default(schema_session, "sonarr")
    # no parent row yet -> owner is the default, local id unknown
    assert sonarr_series_owner(schema_session, 999) == (inst, None)


def test_sonarr_series_owner_unstamped_parent_falls_back_to_default(schema_session):
    from arr_instances.resolution import sonarr_series_owner

    inst = _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=8, id=8, arr_instance_id=None, path="/tv/u", title="U"))

    # parent exists but is unstamped -> owner falls back to default, id known
    assert sonarr_series_owner(schema_session, 8) == (inst, 8)


# ------------------------------------------------------------- series stamping

def test_series_insert_stamps_default_instance(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    inst = _seed_default(schema_session, "sonarr")
    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "seriesParser",
                        lambda *a, **k: {"sonarrSeriesId": 5, "path": "/tv/s", "title": "S"})

    series_mod.update_one_series(
        5, action="updated", series_data={"id": 5}, existing_in_db=False,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True)

    row = schema_session.execute(
        select(TableShows.arr_instance_id).where(TableShows.sonarrSeriesId == 5)).first()
    assert row is not None and row.arr_instance_id == inst


def test_series_update_restamps_owner(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    inst = _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=inst, path="/tv/old", title="Old"))
    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "seriesParser",
                        lambda *a, **k: {"sonarrSeriesId": 5, "path": "/tv/new", "title": "New"})

    series_mod.update_one_series(
        5, action="updated", series_data={"id": 5}, existing_in_db=True,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True)

    row = schema_session.execute(
        select(TableShows.arr_instance_id, TableShows.path)
        .where(TableShows.sonarrSeriesId == 5)).first()
    assert row.arr_instance_id == inst and row.path == "/tv/new"


def test_series_insert_leaves_null_when_no_default(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "seriesParser",
                        lambda *a, **k: {"sonarrSeriesId": 5, "path": "/tv/s", "title": "S"})

    series_mod.update_one_series(
        5, action="updated", series_data={"id": 5}, existing_in_db=False,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True)

    row = schema_session.execute(
        select(TableShows.arr_instance_id).where(TableShows.sonarrSeriesId == 5)).first()
    assert row is not None and row.arr_instance_id is None  # legacy: no literal None owner forced


# ------------------------------------------------------------ episode stamping

def test_episode_insert_inherits_parent_owner_and_local_id(schema_session, monkeypatch):
    import sonarr.sync.episodes as ep_mod

    inst = _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=inst, path="/tv/s", title="S",
        audio_language="[]"))
    monkeypatch.setattr(ep_mod, "database", schema_session)
    monkeypatch.setattr(ep_mod, "store_subtitles", _noop)
    monkeypatch.setattr(ep_mod, "event_stream", _noop)
    monkeypatch.setattr(ep_mod, "get_sonarr_info", _SonarrInfoStub())
    monkeypatch.setattr(ep_mod, "episodeParser", lambda e: {
        "sonarrSeriesId": 5, "sonarrEpisodeId": 100, "path": "/tv/s/e", "season": 1,
        "episode": 1, "title": "E", "monitored": "True"})

    episodes_data = [{"id": 100, "hasFile": True, "monitored": True, "episodeFileId": 100,
                      "episodeFile": {"size": 999999, "path": "/tv/s/e"}}]
    ep_mod.sync_episodes(series_id=5, episodes_data=episodes_data, defer_search=True)

    row = schema_session.execute(
        select(TableEpisodes.arr_instance_id, TableEpisodes.series_id)
        .where(TableEpisodes.sonarrEpisodeId == 100)).first()
    assert row is not None
    assert row.arr_instance_id == inst
    assert row.series_id == 5  # parent.id, inherited


def test_episode_insert_inherits_nondefault_owner(schema_session, monkeypatch):
    import sonarr.sync.episodes as ep_mod

    _seed_default(schema_session, "sonarr")  # default exists but parent is owned elsewhere
    # parent series owned by a DIFFERENT (non-default) instance id=2, local id=77
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=77, arr_instance_id=2, path="/tv/s", title="S",
        audio_language="[]"))
    monkeypatch.setattr(ep_mod, "database", schema_session)
    monkeypatch.setattr(ep_mod, "store_subtitles", _noop)
    monkeypatch.setattr(ep_mod, "event_stream", _noop)
    monkeypatch.setattr(ep_mod, "get_sonarr_info", _SonarrInfoStub())
    monkeypatch.setattr(ep_mod, "episodeParser", lambda e: {
        "sonarrSeriesId": 5, "sonarrEpisodeId": 100, "path": "/tv/s/e", "season": 1,
        "episode": 1, "title": "E", "monitored": "True"})

    episodes_data = [{"id": 100, "hasFile": True, "monitored": True, "episodeFileId": 100,
                      "episodeFile": {"size": 999999, "path": "/tv/s/e"}}]
    ep_mod.sync_episodes(series_id=5, episodes_data=episodes_data, defer_search=True)

    row = schema_session.execute(
        select(TableEpisodes.arr_instance_id, TableEpisodes.series_id)
        .where(TableEpisodes.sonarrEpisodeId == 100)).first()
    assert row.arr_instance_id == 2  # NOT the default - inherited from the parent series
    assert row.series_id == 77


# -------------------------------------------------------------- movie stamping

def test_movie_bulk_insert_stamps_default_instance(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod

    inst = _seed_default(schema_session, "radarr")
    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "check_radarr_rootfolder", _noop)
    monkeypatch.setattr(mv_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: [
        {"id": 10, "hasFile": True, "monitored": True, "title": "M",
         "movieFile": {"size": 999999, "path": "/m"}}])
    monkeypatch.setattr(mv_mod, "movieParser",
                        lambda *a, **k: {"radarrId": 10, "title": "M", "path": "/m", "tmdbId": "t10"})

    mv_mod.update_movies(job_id="job-1")

    row = schema_session.execute(
        select(TableMovies.arr_instance_id).where(TableMovies.radarrId == 10)).first()
    assert row is not None and row.arr_instance_id == inst


def test_movie_single_insert_stamps_default_instance(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod

    inst = _seed_default(schema_session, "radarr")
    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: {"id": 10})
    monkeypatch.setattr(mv_mod, "movieParser",
                        lambda *a, **k: {"radarrId": 10, "title": "M", "path": "/m", "tmdbId": "t10"})

    mv_mod.update_one_movie(10, action="updated", defer_search=True)

    row = schema_session.execute(
        select(TableMovies.arr_instance_id).where(TableMovies.radarrId == 10)).first()
    assert row is not None and row.arr_instance_id == inst


def test_movie_bulk_insert_leaves_null_when_no_default(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod

    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "check_radarr_rootfolder", _noop)
    monkeypatch.setattr(mv_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: [
        {"id": 10, "hasFile": True, "monitored": True, "title": "M",
         "movieFile": {"size": 999999, "path": "/m"}}])
    monkeypatch.setattr(mv_mod, "movieParser",
                        lambda *a, **k: {"radarrId": 10, "title": "M", "path": "/m", "tmdbId": "t10"})

    mv_mod.update_movies(job_id="job-1")

    row = schema_session.execute(
        select(TableMovies.arr_instance_id).where(TableMovies.radarrId == 10)).first()
    assert row is not None and row.arr_instance_id is None


# ============================================================================
# INC5: thread arr_instance_id + arr_client through the orchestrators.
# Default (both None) must stay byte-identical; a passed id is stamped instead
# of the default, and a passed arr_client routes every leaf fetch through it.
# ============================================================================

def test_update_one_series_passed_instance_id_wins_over_default(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    _seed_default(schema_session, "sonarr")  # a default exists, id != 2
    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "seriesParser",
                        lambda *a, **k: {"sonarrSeriesId": 5, "path": "/tv/s", "title": "S"})

    series_mod.update_one_series(
        5, action="updated", series_data={"id": 5}, existing_in_db=False,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True, arr_instance_id=2, arr_client=object())

    row = schema_session.execute(
        select(TableShows.arr_instance_id).where(TableShows.sonarrSeriesId == 5)).first()
    assert row.arr_instance_id == 2  # explicit instance, not the default


def test_update_one_series_routes_arr_client_to_leaf_fetch(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    _seed_default(schema_session, "sonarr")
    captured = {}

    def fake_fetch(apikey_sonarr=None, sonarr_series_id=None, arr_client=None):
        captured["arr_client"] = arr_client
        return [{"id": 5}]

    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "get_series_from_sonarr_api", fake_fetch)
    monkeypatch.setattr(series_mod, "seriesParser",
                        lambda *a, **k: {"sonarrSeriesId": 5, "path": "/tv/s", "title": "S"})
    sentinel = object()

    # series_data omitted -> update_one_series must fetch, threading the client
    series_mod.update_one_series(
        5, action="updated", existing_in_db=False,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True, arr_client=sentinel)

    assert captured["arr_client"] is sentinel


def test_update_series_default_threads_none_everywhere(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    seen = {}

    def rec_update_one(*a, **k):
        seen["uos_arr_client"] = k.get("arr_client", "MISSING")
        seen["uos_instance"] = k.get("arr_instance_id", "MISSING")

    def rec_sync_eps(*a, **k):
        seen["se_arr_client"] = k.get("arr_client", "MISSING")

    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "check_sonarr_rootfolder", _noop)
    monkeypatch.setattr(series_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(series_mod, "get_series_from_sonarr_api",
                        lambda *a, **k: [{"id": 5, "monitored": True, "title": "S"}])
    monkeypatch.setattr(series_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_episodes_from_sonarr_api", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "update_one_series", rec_update_one)
    monkeypatch.setattr(series_mod, "sync_episodes", rec_sync_eps)

    series_mod.update_series(job_id="job-1")

    # default path forwards arr_client=None and arr_instance_id=None
    assert seen["uos_arr_client"] is None
    assert seen["uos_instance"] is None
    assert seen["se_arr_client"] is None


def test_sync_episodes_passed_instance_id_wins(schema_session, monkeypatch):
    import sonarr.sync.episodes as ep_mod

    _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=1, path="/tv/s", title="S", audio_language="[]"))
    monkeypatch.setattr(ep_mod, "database", schema_session)
    monkeypatch.setattr(ep_mod, "store_subtitles", _noop)
    monkeypatch.setattr(ep_mod, "event_stream", _noop)
    monkeypatch.setattr(ep_mod, "get_sonarr_info", _SonarrInfoStub())
    monkeypatch.setattr(ep_mod, "episodeParser", lambda e: {
        "sonarrSeriesId": 5, "sonarrEpisodeId": 100, "path": "/tv/s/e", "season": 1,
        "episode": 1, "title": "E", "monitored": "True"})

    episodes_data = [{"id": 100, "hasFile": True, "monitored": True, "episodeFileId": 100,
                      "episodeFile": {"size": 999999, "path": "/tv/s/e"}}]
    ep_mod.sync_episodes(series_id=5, episodes_data=episodes_data, defer_search=True,
                         arr_instance_id=2, arr_client=object())

    row = schema_session.execute(
        select(TableEpisodes.arr_instance_id).where(TableEpisodes.sonarrEpisodeId == 100)).first()
    assert row.arr_instance_id == 2  # the synced instance, overriding the parent/default


def test_update_movies_passed_instance_id_and_client_bypasses_apikey_gate(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod
    from app.config import settings

    _seed_default(schema_session, "radarr")
    monkeypatch.setattr(settings.radarr, "apikey", None)  # no scalar key
    captured = {}

    def fake_movies(apikey_radarr=None, radarr_id=None, arr_client=None):
        captured["arr_client"] = arr_client
        return [{"id": 10, "hasFile": True, "monitored": True, "title": "M",
                 "movieFile": {"size": 999999, "path": "/m"}}]

    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "check_radarr_rootfolder", _noop)
    monkeypatch.setattr(mv_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", fake_movies)
    monkeypatch.setattr(mv_mod, "movieParser",
                        lambda *a, **k: {"radarrId": 10, "title": "M", "path": "/m", "tmdbId": "t10"})
    sentinel = object()

    # scalar apikey is None, but a client is provided -> the sync must still run
    mv_mod.update_movies(job_id="job-1", arr_instance_id=2, arr_client=sentinel)

    assert captured["arr_client"] is sentinel
    row = schema_session.execute(
        select(TableMovies.arr_instance_id).where(TableMovies.radarrId == 10)).first()
    assert row is not None and row.arr_instance_id == 2


def test_update_one_movie_passed_instance_id_wins(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod

    _seed_default(schema_session, "radarr")
    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: {"id": 10})
    monkeypatch.setattr(mv_mod, "movieParser",
                        lambda *a, **k: {"radarrId": 10, "title": "M", "path": "/m", "tmdbId": "t10"})

    mv_mod.update_one_movie(10, action="updated", defer_search=True, arr_instance_id=2, arr_client=object())

    row = schema_session.execute(
        select(TableMovies.arr_instance_id).where(TableMovies.radarrId == 10)).first()
    assert row is not None and row.arr_instance_id == 2


# ============================================================================
# INC6: thin *_for_instance entry points build the instance client and forward.
# ============================================================================

def test_update_series_for_instance_forwards(monkeypatch):
    import sonarr.sync.series as series_mod

    sentinel = object()
    seen = {}
    monkeypatch.setattr(series_mod, "client_for_instance", lambda *a, **k: sentinel)
    monkeypatch.setattr(series_mod, "update_series", lambda **k: seen.update(k))

    series_mod.update_series_for_instance(2, job_id="job-x")
    assert seen["arr_instance_id"] == 2 and seen["arr_client"] is sentinel and seen["job_id"] == "job-x"


def test_update_series_for_instance_skips_unknown(monkeypatch):
    import sonarr.sync.series as series_mod

    called = []
    monkeypatch.setattr(series_mod, "client_for_instance", lambda *a, **k: None)
    monkeypatch.setattr(series_mod, "update_series", lambda **k: called.append(k))

    series_mod.update_series_for_instance(999, job_id="job-x")
    assert called == []  # missing/disabled instance -> no sync attempted


def test_update_movies_for_instance_forwards(monkeypatch):
    import radarr.sync.movies as mv_mod

    sentinel = object()
    seen = {}
    monkeypatch.setattr(mv_mod, "client_for_instance", lambda *a, **k: sentinel)
    monkeypatch.setattr(mv_mod, "update_movies", lambda **k: seen.update(k))

    mv_mod.update_movies_for_instance(3, job_id="job-y")
    assert seen["arr_instance_id"] == 3 and seen["arr_client"] is sentinel


def test_update_movies_for_instance_skips_unknown(monkeypatch):
    import radarr.sync.movies as mv_mod

    called = []
    monkeypatch.setattr(mv_mod, "client_for_instance", lambda *a, **k: None)
    monkeypatch.setattr(mv_mod, "update_movies", lambda **k: called.append(k))

    mv_mod.update_movies_for_instance(999, job_id="job-y")
    assert called == []


def test_for_instance_enqueues_without_job_id_no_client_in_queue(monkeypatch):
    # The scheduler schedules *_for_instance with no job_id -> it must enqueue
    # itself (only the int arr_instance_id in this frame's locals; the ArrClient
    # is built on the real run, never put on the queue) and not run the sync.
    import sonarr.sync.series as series_mod
    import radarr.sync.movies as mv_mod

    for mod, fn, base in ((series_mod, "update_series_for_instance", "update_series"),
                          (mv_mod, "update_movies_for_instance", "update_movies")):
        enq = []
        ran = []
        monkeypatch.setattr(mod.jobs_queue, "add_job_from_function",
                            lambda *a, **k: enq.append((a, k)) or 1)
        monkeypatch.setattr(mod, base, lambda **k: ran.append(k))
        monkeypatch.setattr(mod, "client_for_instance", lambda *a, **k: object())

        getattr(mod, fn)(2)  # no job_id -> enqueue branch
        assert len(enq) == 1, fn
        assert ran == [], fn  # did not run the sync directly


# ============================================================================
# INC7: instance-scoped reads/deletes/updates. The default path (arr_instance_id
# None) stays unscoped (byte-identical). The ORM still keys on the upstream id,
# so the test DB can't hold two rows with the same sonarrSeriesId/radarrId;
# isolation is proven via "a scoped query must NOT touch another instance's row".
# ============================================================================

def test_update_series_delete_is_scoped_to_instance(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    # instance 1 owns series 5; instance 2 owns series 7
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=1, path="/i1", title="I1"))
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=7, id=7, arr_instance_id=2, path="/i2", title="I2"))

    deleted = []

    def rec_update_one(series_id, action, **k):
        if action == "deleted":
            deleted.append(series_id)

    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "check_sonarr_rootfolder", _noop)
    monkeypatch.setattr(series_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "update_one_series", rec_update_one)
    monkeypatch.setattr(series_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_episodes_from_sonarr_api", lambda *a, **k: [])
    # instance 2's API returns only its own series 7
    monkeypatch.setattr(series_mod, "get_series_from_sonarr_api",
                        lambda *a, **k: [{"id": 7, "monitored": True, "title": "I2"}])

    series_mod.update_series(job_id="job-1", arr_instance_id=2, arr_client=object())

    # scoped: instance 1's series 5 is NOT seen as "removed" by instance 2's sync
    assert deleted == []


def test_update_series_default_path_unscoped_sees_all(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=1, path="/i1", title="I1"))
    deleted = []

    def rec_update_one(series_id, action, **k):
        if action == "deleted":
            deleted.append(series_id)

    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "check_sonarr_rootfolder", _noop)
    monkeypatch.setattr(series_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "update_one_series", rec_update_one)
    monkeypatch.setattr(series_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_episodes_from_sonarr_api", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_series_from_sonarr_api", lambda *a, **k: [])  # API empty

    series_mod.update_series(job_id="job-1")  # default path: unscoped

    # unscoped default path considers all rows -> series 5 is "removed"
    assert deleted == [5]


def test_update_one_series_scoped_update_misses_other_instance(schema_session, monkeypatch):
    import sonarr.sync.series as series_mod

    # row owned by instance 1; an instance-2 sync of the same upstream id must
    # not overwrite it (scoped UPDATE matches nothing for instance 2)
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=1, path="/i1", title="I1"))
    monkeypatch.setattr(series_mod, "database", schema_session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "seriesParser",
                        lambda *a, **k: {"sonarrSeriesId": 5, "path": "/hijacked", "title": "X"})

    series_mod.update_one_series(
        5, action="updated", series_data={"id": 5}, existing_in_db=True,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True, arr_instance_id=2, arr_client=object())

    row = schema_session.execute(
        select(TableShows.path).where(TableShows.sonarrSeriesId == 5)).first()
    assert row.path == "/i1"  # instance 1's row untouched by instance 2's scoped update


def test_update_movies_delete_is_scoped_to_instance(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod

    schema_session.execute(insert(TableMovies).values(
        radarrId=5, id=5, arr_instance_id=1, path="/m1", title="M1", tmdbId="t5"))
    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "check_radarr_rootfolder", _noop)
    monkeypatch.setattr(mv_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: [])  # instance 2 has no movies

    mv_mod.update_movies(job_id="job-1", arr_instance_id=2, arr_client=object())

    # scoped delete: instance 1's movie 5 survives instance 2's empty sync
    row = schema_session.execute(
        select(TableMovies.radarrId).where(TableMovies.radarrId == 5)).first()
    assert row is not None


def test_update_one_movie_scoped_update_misses_other_instance(schema_session, monkeypatch):
    import radarr.sync.movies as mv_mod

    schema_session.execute(insert(TableMovies).values(
        radarrId=5, id=5, arr_instance_id=1, path="/m1", title="M1", tmdbId="t5"))
    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: {"id": 5})
    monkeypatch.setattr(mv_mod, "movieParser",
                        lambda *a, **k: {"radarrId": 5, "title": "X", "path": "/hijacked", "tmdbId": "t5"})

    # existing_movie is looked up scoped to instance 2 -> not found -> insert path
    # would try, but the row already exists under instance 1; the scoped lookup
    # missing it must not let instance 2 overwrite instance 1's row.
    mv_mod.update_one_movie(5, action="updated", defer_search=True,
                            arr_instance_id=2, arr_client=object())

    rows = schema_session.execute(
        select(TableMovies.arr_instance_id, TableMovies.path)
        .where(TableMovies.radarrId == 5).order_by(TableMovies.id)).all()
    # instance 1's original row is intact
    assert any(r.arr_instance_id == 1 and r.path == "/m1" for r in rows)


def test_sync_episodes_delete_is_scoped_to_instance(schema_session, monkeypatch):
    import sonarr.sync.episodes as ep_mod

    # instance 1 owns episode 100 of series 5
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=1, path="/tv/s", title="S", audio_language="[]"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrSeriesId=5, sonarrEpisodeId=100, id=100, arr_instance_id=1,
        path="/tv/s/e1", title="E1", season=1, episode=1, monitored="True"))
    monkeypatch.setattr(ep_mod, "database", schema_session)
    monkeypatch.setattr(ep_mod, "store_subtitles", _noop)
    monkeypatch.setattr(ep_mod, "event_stream", _noop)
    monkeypatch.setattr(ep_mod, "get_sonarr_info", _SonarrInfoStub())
    monkeypatch.setattr(ep_mod, "episodeParser", lambda e: {
        "sonarrSeriesId": 5, "sonarrEpisodeId": 200, "path": "/tv/s/e2", "season": 1,
        "episode": 2, "title": "E2", "monitored": "True"})

    # instance 2 syncs series 5 with a different episode set (200)
    episodes_data = [{"id": 200, "hasFile": True, "monitored": True, "episodeFileId": 200,
                      "episodeFile": {"size": 999999, "path": "/tv/s/e2"}}]
    ep_mod.sync_episodes(series_id=5, episodes_data=episodes_data, defer_search=True,
                         arr_instance_id=2, arr_client=object())

    # scoped: instance 1's episode 100 must NOT be deleted by instance 2's sync
    survives = schema_session.execute(
        select(TableEpisodes.sonarrEpisodeId).where(TableEpisodes.sonarrEpisodeId == 100)).first()
    assert survives is not None
