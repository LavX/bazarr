# coding=utf-8
"""Follow-up multi-instance threading regressions (#156).

These close four LOW-severity gaps surfaced by adversarial verification of the
earlier multi-instance fix series. The central invariant: every read/write is
scoped to the OWNING ``arr_instance_id``; upstream ids
(sonarrSeriesId/sonarrEpisodeId/radarrId) are unique only per instance, so the
canonical reference is the LOCAL id.

  * L13: episode ``event_stream`` payloads must carry the LOCAL episode id (the
    frontend caches episode detail by local id), never the upstream id.
  * M7: the editor + batch subsync callers must thread the owning
    ``arr_instance_id`` into ``sync_subtitles`` so the original-language lookup
    reads the exact owner, not the default-preferred instance.
  * L14: path_replace* on a media row that carries an ``arr_instance_id`` must
    honour that instance's per-instance path_mappings (indexer path).
"""

import json

from types import SimpleNamespace

from app.database import (
    TableArrInstances,
    TableEpisodes,
    TableShows,
)


# ---------------------------------------------------------------------------
# L13: episode event_stream payloads must be the LOCAL episode id.
# ---------------------------------------------------------------------------


def test_sync_one_episode_update_emits_local_episode_id(schema_session, monkeypatch):
    """During a single-episode sync the update branch must emit the LOCAL
    episode id, not the upstream sonarrEpisodeId, so the frontend invalidates
    the right cached episode for a non-default instance."""
    from sonarr.sync import episodes as ep_mod

    monkeypatch.setattr(ep_mod, "database", schema_session)
    monkeypatch.setattr(ep_mod, "store_subtitles", lambda *a, **k: None)
    monkeypatch.setattr(ep_mod.path_mappings, "path_replace", lambda p: p)

    events = []
    monkeypatch.setattr(ep_mod, "event_stream",
                        lambda **kwargs: events.append(kwargs))

    # Non-default instance: local episode id 901 != upstream sonarrEpisodeId 42.
    schema_session.add(TableShows(
        id=801, sonarrSeriesId=7, arr_instance_id=3, path="/s3", title="S",
        tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=901, series_id=801, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=3, path="/s3/e.mkv", title="Ep", season=1, episode=1,
        monitored="True", subtitles="[]"))
    schema_session.flush()

    # Drive the update branch: existing row present, parser returns a row,
    # defer_search True so we stop before the heavy download path.
    monkeypatch.setattr(
        ep_mod, "get_episodes_from_sonarr_api",
        lambda **k: {"hasFile": False, "sonarrEpisodeId": 42})
    monkeypatch.setattr(
        ep_mod, "episodeParser",
        lambda data, arr_instance_id=None: {
            "sonarrEpisodeId": 42, "sonarrSeriesId": 7, "path": "/s3/e.mkv",
            "season": 1, "episode": 1, "title": "Ep", "monitored": "True"})

    ep_mod.sync_one_episode(42, defer_search=True, arr_instance_id=3)

    episode_events = [e for e in events if e.get("type") == "episode"]
    assert episode_events, "an episode event must be emitted on update"
    assert episode_events[0]["payload"] == 901, (
        "episode update event must carry the LOCAL episode id (901), not the "
        f"upstream sonarrEpisodeId (42); got {episode_events[0]['payload']}")


def test_sync_one_episode_delete_emits_local_episode_id(schema_session, monkeypatch):
    """The delete branch must emit the LOCAL episode id resolved BEFORE the row
    is removed."""
    from sonarr.sync import episodes as ep_mod

    monkeypatch.setattr(ep_mod, "database", schema_session)
    monkeypatch.setattr(ep_mod.path_mappings, "path_replace", lambda p: p)

    events = []
    monkeypatch.setattr(ep_mod, "event_stream",
                        lambda **kwargs: events.append(kwargs))

    schema_session.add(TableShows(
        id=801, sonarrSeriesId=7, arr_instance_id=3, path="/s3", title="S",
        tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=901, series_id=801, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=3, path="/s3/e.mkv", title="Ep", season=1, episode=1,
        monitored="True", subtitles="[]"))
    schema_session.flush()

    # Sonarr returns data but the parser yields no usable episode (e.g. an
    # episode without a file that should be removed) -> delete branch.
    monkeypatch.setattr(
        ep_mod, "get_episodes_from_sonarr_api",
        lambda **k: {"hasFile": False, "sonarrEpisodeId": 42})
    monkeypatch.setattr(ep_mod, "episodeParser", lambda data, arr_instance_id=None: None)

    ep_mod.sync_one_episode(42, defer_search=True, arr_instance_id=3)

    episode_events = [e for e in events
                      if e.get("type") == "episode" and e.get("action") == "delete"]
    assert episode_events, "an episode delete event must be emitted"
    assert episode_events[0]["payload"] == 901, (
        "episode delete event must carry the LOCAL episode id (901), not the "
        f"upstream sonarrEpisodeId (42); got {episode_events[0]['payload']}")


def test_local_episode_id_resolver_scopes_by_instance(schema_session, monkeypatch):
    """The shared resolver returns the LOCAL id for the OWNING instance and
    falls back to the upstream id only when no local row matches."""
    from utilities.path_mappings import path_mappings  # noqa: F401
    import utilities.media_ids as media_ids

    monkeypatch.setattr("app.database.database", schema_session)

    # Two episodes share sonarrEpisodeId=42 (owners 1 and 3) with diff local ids.
    for local_show, local_ep, owner in [(800, 900, 1), (801, 901, 3)]:
        schema_session.add(TableShows(
            id=local_show, sonarrSeriesId=7, arr_instance_id=owner,
            path=f"/s{owner}", title="S", tags="[]"))
    schema_session.flush()
    for local_ep, local_show, owner in [(900, 800, 1), (901, 801, 3)]:
        schema_session.add(TableEpisodes(
            id=local_ep, series_id=local_show, sonarrEpisodeId=42, sonarrSeriesId=7,
            arr_instance_id=owner, path=f"/s{owner}/e.mkv", title="Ep", season=1,
            episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    assert media_ids.local_episode_id(42, 3) == 901, "must resolve owner 3's local id"
    assert media_ids.local_episode_id(42, 1) == 900, "must resolve owner 1's local id"
    # No local row for this owner => fall back to the upstream id.
    assert media_ids.local_episode_id(42, 99) == 42


# ---------------------------------------------------------------------------
# L13: subtitle-action post-emit sites resolve the local id from the upstream.
# ---------------------------------------------------------------------------


def test_mods_emits_local_episode_id(schema_session, monkeypatch):
    """subtitles/tools/mods.py apply_subtitle_mods re-index emit must carry the
    LOCAL episode id, resolved from (upstream id, owning instance)."""
    from subtitles.tools import mods as mods_mod

    monkeypatch.setattr("app.database.database", schema_session)
    monkeypatch.setattr(mods_mod, "subtitles_apply_mods", lambda **k: None)

    events = []
    import app.event_handler as eh
    monkeypatch.setattr(eh, "event_stream", lambda **kwargs: events.append(kwargs))

    import subtitles.indexer.series as si
    import subtitles.indexer.movies as mi
    monkeypatch.setattr(si, "store_subtitles", lambda *a, **k: None)
    monkeypatch.setattr(mi, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr("utilities.path_mappings.path_mappings.path_replace_reverse",
                        lambda p: p)

    schema_session.add(TableShows(
        id=801, sonarrSeriesId=7, arr_instance_id=3, path="/s3", title="S", tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=901, series_id=801, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=3, path="/s3/e.mkv", title="Ep", season=1, episode=1,
        monitored="True", subtitles="[]"))
    schema_session.flush()

    class _JobsQueue:
        def update_job_name(self, *a, **k):
            pass

    monkeypatch.setattr(mods_mod, "jobs_queue", _JobsQueue())
    monkeypatch.setattr(mods_mod.settings, "general",
                        SimpleNamespace(chmod_enabled=False, chmod="644"),
                        raising=False)
    monkeypatch.setattr(mods_mod.sys, "platform", "linux")
    monkeypatch.setattr(mods_mod.os.path, "exists", lambda p: False)

    mods_mod.apply_subtitle_mods(
        language="en", subtitle_path="/s3/e.en.srt", video_path="/s3/e.mkv",
        mods=["remove_HI"], media_type="episode", media_id=42, job_id="j",
        arr_instance_id=3)

    episode_events = [e for e in events if e.get("type") == "episode"]
    assert episode_events, "an episode event must be emitted"
    assert episode_events[0]["payload"] == 901, (
        f"mods emit must carry the LOCAL episode id (901); got {episode_events[0]['payload']}")


# ---------------------------------------------------------------------------
# M7: the editor + batch subsync callers must thread the owning instance into
# sync_subtitles.
# ---------------------------------------------------------------------------


def test_mass_operations_sync_threads_owner(monkeypatch):
    """_process_subtitle_item must pass the per-item arr_instance_id into
    sync_subtitles so the subsync original-language lookup hits the exact owner."""
    from subtitles import mass_operations as mo

    captured = {}
    monkeypatch.setattr(mo, "sync_subtitles",
                        lambda **kwargs: captured.update(kwargs) or True)

    item = {
        "video_path": "/s3/e.mkv",
        "srt_path": "/s3/e.en.srt",
        "srt_lang": "en",
        "forced": False,
        "hi": False,
        "sonarr_series_id": 7,
        "sonarr_episode_id": 42,
        "radarr_id": None,
        "arr_instance_id": 3,
        "max_offset_seconds": "60",
        "no_fix_framerate": False,
        "gss": False,
        "output_mode": None,
        "enabled_engines": None,
    }
    mo._process_subtitle_item(item, "sync", {}, job_id="job-1")

    assert captured.get("arr_instance_id") == 3, (
        "batch sync must thread the per-item owning instance into sync_subtitles; "
        f"got {captured.get('arr_instance_id')}")


# ---------------------------------------------------------------------------
# L14: per-instance path_mappings honoured in the indexer path.
# ---------------------------------------------------------------------------


def test_store_subtitles_uses_instance_path_mapping(schema_session, monkeypatch):
    """An episode owned by an instance with its own path_mappings gets its
    indexed subtitle path reversed with the per-instance mapping, not the
    global one."""
    import subtitles.indexer.series as si
    import utilities.path_mappings as pm_mod

    monkeypatch.setattr(si, "database", schema_session)
    monkeypatch.setattr("app.database.database", schema_session)

    # Instance 3 maps /local3 <-> /remote3 (series). Global maps /g/local <-> /g/remote.
    schema_session.add(TableArrInstances(
        id=3, kind="sonarr", stable_key="k3", name="i3", enabled=1, is_default=0,
        ip="127.0.0.1", port=8989, base_url="/", ssl=0, verify_ssl=0,
        http_timeout=60, api_key="",
        path_mappings=json.dumps({"series": [["/remote3", "/local3"]]})))
    schema_session.flush()
    schema_session.add(TableShows(
        id=801, sonarrSeriesId=7, arr_instance_id=3, path="/local3/show", title="S",
        tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=901, series_id=801, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=3, path="/local3/show/e.mkv", title="Ep", season=1,
        episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    # global mapping is DIFFERENT; if the indexer used it the reverse output
    # would be wrong.
    pm_mod.path_mappings.path_mapping_series = [["/g/remote", "/g/local"]]

    captured = {}

    def _fake_reverse_instance(path, arr_instance_id, media_type):
        captured["args"] = (path, arr_instance_id, media_type)
        return pm_mod.PathMappings.path_replace_reverse_instance(
            pm_mod.path_mappings, path, arr_instance_id, media_type)

    monkeypatch.setattr(pm_mod.path_mappings, "path_replace_reverse_instance",
                        _fake_reverse_instance)

    # Make the external-subtitle search return one en subtitle under /local3.
    sub_local = "/local3/show/e.en.srt"

    monkeypatch.setattr(si.os.path, "exists", lambda p: True)
    monkeypatch.setattr(si, "embedded_subs_reader", lambda *a, **k: [])
    monkeypatch.setattr(si.settings, "general",
                        SimpleNamespace(use_embedded_subs=False, single_language=False,
                                        subfolder="current", subfolder_custom=""),
                        raising=False)
    monkeypatch.setattr(si, "get_subtitle_destination_folder", lambda: None)

    class _Lang:
        alpha3 = "eng"
        basename = "en"
        forced = False
        hi = False

        def __str__(self):
            return "en"

    monkeypatch.setattr(si, "search_external_subtitles", lambda *a, **k: {sub_local: _Lang()})
    monkeypatch.setattr(si, "add_sync_engine_outputs", lambda f, s: s)
    monkeypatch.setattr(si, "add_combined_outputs", lambda f, s, **k: s)
    monkeypatch.setattr(si, "guess_external_subtitles", lambda f, s, t, e: s)
    monkeypatch.setattr(si, "get_external_subtitles_path", lambda r, s: s)
    monkeypatch.setattr(si, "alpha2_from_alpha3", lambda a: "en")
    monkeypatch.setattr(si, "subtitle_language_with_sync_modifier", lambda ls, s: ls)
    monkeypatch.setattr(si, "subtitle_language_with_combined_modifier", lambda ls, s: ls)
    monkeypatch.setattr(si.CustomLanguage, "found_external", lambda s, sp: None)
    monkeypatch.setattr(si.os, "stat", lambda p: SimpleNamespace(st_size=10))
    monkeypatch.setattr(si, "list_missing_subtitles", lambda **k: None)
    monkeypatch.setattr(si, "_log_embedded_history", lambda *a, **k: None)

    actual = si.store_subtitles("/local3/show/e.mkv", "/local3/show/e.mkv")

    # The stored subtitle DB-path must come from the per-instance reverse mapping.
    assert captured.get("args") is not None, (
        "store_subtitles must call path_replace_reverse_instance for an owned row")
    assert captured["args"][1] == 3, "the owning instance id must be threaded"
    stored_paths = [row[1] for row in actual]
    assert "/remote3/show/e.en.srt" in stored_paths, (
        "indexed subtitle path must be reversed with the per-instance mapping "
        f"(/remote3...), not the global one; got {stored_paths}")
