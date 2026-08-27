# coding=utf-8
"""The parsers must be handed the RESOLVED owner, not the requested scope (#156).

``movieParser`` and ``episodeParser`` read embedded audio, which now keys the
ffprobe metadata cache by ``arr_instance_id``. The sync orchestrators resolve
the real owning instance, but the legacy and SignalR entry points reach the
parser before that resolution happens and pass the REQUESTED scope, which is
``None`` on every default-instance path.

``scoped(..., None)`` applies no filter at all, so on a multi-instance install
with colliding upstream file ids the default instance still reads and
overwrites a sibling instance's cached stream list. Threading the cache through
the parser is only worth anything if the value threaded is the owner.
"""
import semver

from sqlalchemy import insert

from app.database import TableShows


def _noop(*args, **kwargs):
    return None


def _seed_default(session, kind):
    from arr_instances.repository import ArrInstanceRepository

    inst = ArrInstanceRepository(session).create(kind, kind.capitalize())
    session.flush()
    return inst.id


class _SonarrInfoStub:
    def semver(self):
        return semver.Version(4, 0, 10, 0)


class _DummyJobs:
    def update_job_progress(self, *a, **k):
        return None

    def update_job_name(self, *a, **k):
        return None


# ------------------------------------------------------------------ radarr

def test_update_one_movie_hands_the_parser_the_resolved_owner(schema_session, monkeypatch):
    """The legacy/SignalR movie path resolves ``instance_id`` and then passed the
    unresolved ``arr_instance_id``, leaving the cache query unscoped."""
    import radarr.sync.movies as mv_mod

    inst = _seed_default(schema_session, "radarr")
    seen = {}

    monkeypatch.setattr(mv_mod, "database", schema_session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", lambda *a, **k: {"id": 10})

    def _parser(*a, **k):
        seen["arr_instance_id"] = k.get("arr_instance_id")
        return {"radarrId": 10, "title": "M", "path": "/m", "tmdbId": "t10"}

    monkeypatch.setattr(mv_mod, "movieParser", _parser)

    mv_mod.update_one_movie(10, action="updated", defer_search=True)

    assert seen["arr_instance_id"] == inst, (
        "movieParser must receive the resolved default instance, not None; got "
        f"{seen['arr_instance_id']!r}")


# ------------------------------------------------------------------ sonarr

def _episodes_payload():
    return [{"id": 100, "hasFile": True, "monitored": True, "episodeFileId": 100,
             "episodeFile": {"size": 999999, "path": "/tv/s/e"}}]


def _parsed_episode():
    return {"sonarrSeriesId": 5, "sonarrEpisodeId": 100, "path": "/tv/s/e",
            "season": 1, "episode": 1, "title": "E", "monitored": "True"}


def _patch_episode_sync(monkeypatch, session, seen):
    import sonarr.sync.episodes as ep_mod

    monkeypatch.setattr(ep_mod, "database", session)
    monkeypatch.setattr(ep_mod, "store_subtitles", _noop)
    monkeypatch.setattr(ep_mod, "event_stream", _noop)
    monkeypatch.setattr(ep_mod, "get_sonarr_info", _SonarrInfoStub())

    def _parser(episode, **kwargs):
        seen.append(kwargs.get("arr_instance_id"))
        return _parsed_episode()

    monkeypatch.setattr(ep_mod, "episodeParser", _parser)
    return ep_mod


def test_sync_episodes_hands_the_parser_the_parent_owner(schema_session, monkeypatch):
    """The default bulk path passed None even though the parent series resolves
    to the default instance a line later."""
    inst = _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=inst, path="/tv/s", title="S",
        audio_language="[]"))
    seen = []
    ep_mod = _patch_episode_sync(monkeypatch, schema_session, seen)

    ep_mod.sync_episodes(series_id=5, episodes_data=_episodes_payload(), defer_search=True)

    assert seen == [inst], (
        f"episodeParser must be given the parent's owning instance; got {seen!r}")


def test_sync_episodes_hands_the_parser_a_nondefault_parent_owner(schema_session, monkeypatch):
    """A series owned by a secondary instance must not have its episodes parsed
    against the default instance's cache."""
    _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=77, arr_instance_id=2, path="/tv/s", title="S",
        audio_language="[]"))
    seen = []
    ep_mod = _patch_episode_sync(monkeypatch, schema_session, seen)

    ep_mod.sync_episodes(series_id=5, episodes_data=_episodes_payload(), defer_search=True)

    assert seen == [2], (
        f"episodeParser must inherit the parent series' instance; got {seen!r}")


def test_sync_one_episode_hands_the_parser_the_parent_owner(schema_session, monkeypatch):
    """The SignalR/webhook single-episode path resolves the owner only after
    parsing, so the parser saw None."""
    inst = _seed_default(schema_session, "sonarr")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=inst, path="/tv/s", title="S",
        audio_language="[]"))
    seen = []
    ep_mod = _patch_episode_sync(monkeypatch, schema_session, seen)
    monkeypatch.setattr(ep_mod, "jobs_queue", _DummyJobs(), raising=False)
    monkeypatch.setattr(
        ep_mod, "get_episodes_from_sonarr_api",
        lambda *a, **k: {"id": 100, "seriesId": 5, "hasFile": True,
                         "episodeFileId": 100,
                         "episodeFile": {"size": 999999, "path": "/tv/s/e"}})
    monkeypatch.setattr(ep_mod, "get_episodesFiles_from_sonarr_api",
                        lambda *a, **k: {"size": 999999, "path": "/tv/s/e"})

    ep_mod.sync_one_episode(episode_id=100, defer_search=True)

    assert seen == [inst], (
        f"episodeParser must be given the parent's owning instance; got {seen!r}")
