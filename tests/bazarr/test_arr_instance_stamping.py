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
