"""Manual uploads remain visible while their separate sync job is running."""

from io import BytesIO
import ast
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def upload_flow(monkeypatch, tmp_path):
    from app import jobs_queue as queue_module
    from app.config import settings
    from arr_instances import resolution
    from languages import get_languages
    from subtitles import manual, processing, sync, upload
    from subtitles.indexer import movies, series
    from subtitles.tools import delete, subsyncer
    from subtitles.tools.subsync_engines import InMemorySubsyncFailureStore, SubsyncEngineRunner

    monkeypatch.setattr(queue_module.JobsQueue, "_flush_progress_loop", lambda self: None)
    queue = queue_module.JobsQueue()
    events, indexes, history, notifications, sync_history, engine_calls = [], [], [], [], [], []
    real_indexers = {"series": series.store_subtitles, "movie": movies.store_subtitles_movie}
    controls = SimpleNamespace(fail=False)
    engine_started, release_engine = Event(), Event()
    video = tmp_path / "Video.mkv"
    video.touch()
    metadata = SimpleNamespace(sonarrSeriesId=10, sonarrEpisodeId=20, radarrId=30,
                               profileId=1, imdbId="tt1", tvdbId=1, tmdbId=1,
                               season=1, episode=1)
    db = Mock()
    db.execute.return_value.first.return_value = metadata
    monkeypatch.setattr(get_languages, "languages_dict", [
        {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
    ], raising=False)
    monkeypatch.setattr(upload, "database", db)
    monkeypatch.setattr(delete, "database", db)
    monkeypatch.setattr(upload, "get_profiles_list", lambda _: {"originalFormat": False})
    monkeypatch.setattr(upload, "get_audio_profile_languages", lambda _: [])
    monkeypatch.setattr(upload, "client_for_instance", lambda *a, **k: None)
    monkeypatch.setattr(delete, "client_for_instance", lambda *a, **k: None)
    monkeypatch.setattr(delete, "call_external_webhook", lambda **k: None)
    monkeypatch.setattr(upload, "get_target_folder", lambda _: None)
    monkeypatch.setattr(manual, "clear_mismatch_after_manual_save", lambda *a: None)
    monkeypatch.setattr(processing, "_postprocessing_config", lambda *a: (False, "", False, 0))
    monkeypatch.setattr(resolution, "_subtitle_settings_cache", {7: {}, 8: {}})
    monkeypatch.setattr(upload.path_mappings, "path_replace_reverse_instance", lambda path, *a: path)
    monkeypatch.setattr(upload.path_mappings, "path_replace_instance", lambda path, *a: path)
    monkeypatch.setattr("utilities.media_ids.local_episode_id", lambda episode_id, owner: 2000 + owner)
    for name, value in {"single_language": False, "chmod_enabled": False, "utf8_encode": True,
                        "dont_notify_manual_actions": False, "use_plex": False, "use_jellyfin": False}.items():
        monkeypatch.setattr(settings.general, name, value)
    for name, value in {"use_subsync": True, "use_subsync_threshold": False,
                        "use_subsync_movie_threshold": False, "enabled_engines": ["ffsubsync"],
                        "output_mode": "overwrite", "debug": False}.items():
        monkeypatch.setattr(settings.subsync, name, value)

    def event(**kwargs):
        if kwargs.get("type") in ("movie", "series", "episode"):
            assert indexes, "media events must follow the first subtitle index"
        events.append(kwargs)

    def index(mapped_path, path, arr_instance_id=None):
        indexes.append((arr_instance_id, {p.name: p.read_text() for p in Path(path).parent.glob("*.srt")}))

    for module in (queue_module, upload, sync):
        monkeypatch.setattr(module, "jobs_queue", queue)
    for module in (queue_module, upload, delete):
        monkeypatch.setattr(module, "event_stream", event)
    monkeypatch.setattr("app.event_handler.event_stream", event)
    monkeypatch.setattr(series, "store_subtitles", index)
    monkeypatch.setattr(movies, "store_subtitles_movie", index)
    monkeypatch.setattr(delete, "store_subtitles", index)
    monkeypatch.setattr(delete, "store_subtitles_movie", index)
    for name in ("history_log", "history_log_movie"):
        monkeypatch.setattr(upload, name, lambda *a, **k: history.append((a, k)))
        monkeypatch.setattr(delete, name, lambda *a, **k: None)
    for name in ("notify_sonarr", "notify_radarr", "send_notifications", "send_notifications_movie"):
        monkeypatch.setattr(upload, name, lambda *a, **k: notifications.append((a, k)))
    for name in ("notify_sonarr", "notify_radarr"):
        monkeypatch.setattr(delete, name, lambda *a, **k: None)
    monkeypatch.setattr(subsyncer.SubSyncer, "_log_sync_history", lambda *a, **k: sync_history.append(k))
    monkeypatch.setattr(subsyncer, "SubsyncEngineRunner", lambda: SubsyncEngineRunner(InMemorySubsyncFailureStore()))

    def engine(self, output_path, **kwargs):
        engine_calls.append(kwargs)
        source = Path(self.srtin).read_text()
        engine_started.set()
        assert release_engine.wait(5), "test did not release sync engine"
        output_path.write_text("1\n00:00:03,000 --> 00:00:04,000\nSynced " + source)
        if controls.fail:
            raise RuntimeError("controlled sync failure")
        return {"offset_seconds": 0, "framerate_scale_factor": 1}

    monkeypatch.setattr(subsyncer.SubSyncer, "_run_ffsubsync_engine", engine)

    def submit(media_type, content="Uploaded", owner=7, **kwargs):
        return upload.manual_upload_subtitle(
            path=str(video), language="en", forced=kwargs.pop("forced", False),
            hi=kwargs.pop("hi", False), media_type=media_type,
            subtitle=BytesIO(f"1\n00:00:01,000 --> 00:00:02,000\n{content}\n".encode()),
            filename="upload.srt", audio_language=[],
            sonarrSeriesId=kwargs.pop("sonarrSeriesId", 10 if media_type == "series" else None),
            sonarrEpisodeId=20 if media_type == "series" else None,
            radarrId=30 if media_type == "movie" else None,
            arr_instance_id=owner, **kwargs)

    threads = []

    def start(job_id=None):
        job = next(job for job in queue.jobs_pending_queue if job_id is None or job.job_id == job_id)
        queue.jobs_pending_queue.remove(job)
        queue.jobs_running_queue.append(job)
        thread = Thread(target=queue._run_job, args=(job,))
        threads.append(thread)
        thread.start()
        return job, thread

    def remove(media_type):
        return delete.delete_subtitles(media_type=media_type, language="en", forced=False, hi=False,
                                       media_path=str(video), subtitles_path=str(video.with_suffix(".en.srt")),
                                       sonarr_series_id=10, sonarr_episode_id=20, radarr_id=30, arr_instance_id=7)

    yield SimpleNamespace(submit=submit, start=start, queue=queue, events=events, indexes=indexes,
                          history=history, notifications=notifications, engine_started=engine_started,
                          release_engine=release_engine, video=video, remove=remove, controls=controls,
                          sync_history=sync_history, engine_calls=engine_calls, real_indexers=real_indexers)
    release_engine.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("media_type", ["movie", "series"])
@pytest.mark.parametrize("failure", ["settings", "enqueue"])
def test_saved_upload_refreshes_consumers_when_sync_setup_fails(upload_flow, monkeypatch, media_type, failure):
    from app.config import settings
    from subtitles import sync, upload

    flow = upload_flow
    refreshes = []
    monkeypatch.setattr(settings.general, 'use_plex', True)
    monkeypatch.setattr(settings.general, 'use_jellyfin', True)
    for service in (settings.plex, settings.jellyfin):
        monkeypatch.setattr(service, 'update_movie_library', True)
        monkeypatch.setattr(service, 'update_series_library', True)
    monkeypatch.setattr(settings.plex, 'set_episode_added', False)
    monkeypatch.setattr(settings.plex, 'set_movie_added', False)

    def consumer(name):
        def refresh(*args, **kwargs):
            assert flow.indexes, 'consumers must see an indexed source publication'
            refreshes.append((name, args, kwargs))
        return refresh

    for name in ('notify_sonarr', 'notify_radarr', 'plex_refresh_item', 'jellyfin_refresh_item'):
        monkeypatch.setattr(upload, name, consumer(name))

    def fail(*args, **kwargs):
        raise RuntimeError('controlled sync setup failure')

    if failure == 'settings':
        monkeypatch.setattr(sync, '_resolve_subsync_overrides', fail)
    else:
        monkeypatch.setattr(flow.queue, 'add_job_from_function', fail)

    with pytest.raises(RuntimeError, match='controlled sync setup failure'):
        flow.submit(media_type, job_id='existing-upload-job')

    assert 'Uploaded' in flow.video.with_suffix('.en.srt').read_text()
    assert flow.indexes
    assert len(flow.history) == 1
    assert not flow.queue.jobs_pending_queue
    assert [name for name, args, kwargs in refreshes] == [
        'notify_sonarr' if media_type == 'series' else 'notify_radarr',
        'plex_refresh_item', 'jellyfin_refresh_item']
    assert refreshes[0][1] == (10 if media_type == 'series' else 30,)
    assert refreshes[1][2]['is_movie'] == (media_type == 'movie')
    assert refreshes[2][2]['is_movie'] == (media_type == 'movie')


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_saved_upload_is_visible_and_upload_completes_before_sync(upload_flow, media_type):
    flow = upload_flow
    upload_id = flow.submit(media_type)
    upload_job, upload_thread = flow.start()
    upload_thread.join(1)

    assert (flow.video.parent / "Video.en.srt").is_file()
    assert upload_job.status != "failed"
    assert flow.indexes, "the saved subtitle is still hidden while sync is pending/running"
    assert upload_job.status == "completed"
    assert upload_job.job_returned_value == ("", 204)
    assert len(flow.history) == 1
    assert len(flow.queue.jobs_pending_queue) == 1
    sync_job, sync_thread = flow.start()
    assert flow.engine_started.wait(2)
    assert sync_job.job_id != upload_id
    assert sync_job.is_progress
    assert sync_job.status == "running"
    assert "Uploaded" in flow.indexes[-1][1]["Video.en.srt"]
    flow.release_engine.set()
    sync_thread.join(3)
    assert sync_job.status == "completed"
    assert "Synced" in flow.indexes[-1][1]["Video.en.srt"]
    assert len(flow.history) == 1
    assert sum("arr_client" in kwargs for _, kwargs in flow.notifications) == 2
    assert sum("arr_instance_id" in kwargs for _, kwargs in flow.notifications) == 1
    assert len(flow.sync_history) == 1


@pytest.mark.parametrize("media_type", ["movie", "series"])
@pytest.mark.parametrize("output_mode", ["overwrite", "keep_all"])
@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
@pytest.mark.parametrize("change", ["upload", "delete"])
def test_stale_sync_does_not_replace_a_new_upload_or_restore_a_deleted_file(
        upload_flow, monkeypatch, media_type, output_mode, running, change):
    from app.config import settings

    monkeypatch.setattr(settings.subsync, "output_mode", output_mode)
    flow = upload_flow
    flow.submit(media_type)
    job, thread = flow.start()
    thread.join(2)
    assert job.status == "completed"
    sync_id = flow.queue.jobs_pending_queue[0].job_id
    if running:
        sync_job, sync_thread = flow.start(sync_id)
        assert flow.engine_started.wait(2)

    source = flow.video.parent / "Video.en.srt"
    if change == "upload":
        second_upload_id = flow.submit(media_type, content="New upload")
        second_job, second_thread = flow.start(second_upload_id)
        second_thread.join(2)
        assert second_job.status == "completed"
        assert "New upload" in source.read_text()
    else:
        source.unlink()

    flow.release_engine.set()
    if not running:
        sync_job, sync_thread = flow.start(sync_id)
    sync_thread.join(2)
    assert sync_job.status == "completed"
    if not running:
        assert not flow.engine_started.is_set(), "outdated queued sync should skip its engines"
    assert not (flow.video.parent / "Video.en.ffsubsync.srt").exists()
    if change == "upload":
        assert "New upload" in source.read_text()
        assert "Synced" not in source.read_text()
        latest_sync, latest_thread = flow.start()
        latest_thread.join(2)
        assert latest_sync.status == "completed"
        output = source if output_mode == "overwrite" else source.with_name("Video.en.ffsubsync.srt")
        assert "Synced" in output.read_text()
        assert "New upload" in output.read_text()
    else:
        assert not source.exists()
    assert not list(flow.video.parent.glob(".bazarr-sync-*"))


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_delete_during_sync_publication_cannot_be_undone(upload_flow, monkeypatch, media_type):
    from subtitles.tools import subsync_engines

    flow = upload_flow
    flow.submit(media_type)
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    publishing, publish = Event(), Event()
    real_replace = subsync_engines.os.replace

    def paused_replace(source, destination):
        publishing.set()
        assert publish.wait(5)
        return real_replace(source, destination)

    monkeypatch.setattr(subsync_engines.os, "replace", paused_replace)
    flow.release_engine.set()
    sync_job, sync_thread = flow.start()
    assert publishing.wait(2)
    deletion = Thread(target=flow.remove, args=(media_type,))
    deletion.start()
    try:
        deletion.join(0.1)
        assert deletion.is_alive(), "deletion must serialize with the checked output publication"
    finally:
        publish.set()
        sync_thread.join(2)
        deletion.join(2)
    assert not deletion.is_alive()
    assert not flow.video.with_suffix(".en.srt").exists()


@pytest.mark.parametrize("media_type", ["movie", "series"])
@pytest.mark.parametrize("outcome", ["failure", "cancel", "disabled", "forced", "threshold"])
def test_optional_sync_failure_or_skip_keeps_the_successful_upload(upload_flow, monkeypatch, media_type, outcome):
    from app.config import settings

    flow = upload_flow
    if outcome == "disabled":
        monkeypatch.setattr(settings.subsync, "use_subsync", False)
    if outcome == "threshold":
        threshold = "subsync_threshold" if media_type == "series" else "subsync_movie_threshold"
        monkeypatch.setattr(settings.subsync, "use_" + threshold, True)
        monkeypatch.setattr(settings.subsync, threshold, 99)
    flow.controls.fail = outcome == "failure"
    flow.submit(media_type, forced=outcome == "forced")
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    before = flow.indexes[-1]
    if outcome == "disabled":
        assert not flow.queue.jobs_pending_queue
    else:
        sync_job, sync_thread = flow.start()
        if outcome in ("cancel", "failure"):
            assert flow.engine_started.wait(2)
            if outcome == "cancel":
                assert flow.queue.cancel_running_job(sync_job.job_id)
        flow.release_engine.set()
        sync_thread.join(2)
        assert sync_job.status == "completed"
        if outcome == "cancel":
            assert sync_job.progress_message == "Cancelled by user"
        assert sync_job.job_returned_value is not True
    assert flow.indexes[-1] == before
    assert len(flow.history) == 1
    assert len(flow.notifications) == 2
    assert not flow.sync_history
    assert not list(flow.video.parent.glob(".bazarr-sync-*"))


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_upload_preserves_postprocessing_hi_and_notification_policy(upload_flow, monkeypatch, media_type):
    from app.config import settings
    from subtitles import processing, upload

    flow = upload_flow
    monkeypatch.setattr(settings.general, "dont_notify_manual_actions", True)
    monkeypatch.setattr(processing, "_postprocessing_config", lambda kind, owner: (True, "process", False, 0))
    monkeypatch.setattr(upload, "pp_replace", lambda *args: "process")
    monkeypatch.setattr(upload, "set_chmod", lambda **kwargs: None)

    def postprocess(command, video_path, subtitle_path=None):
        assert command == "process"
        flow.video.with_suffix(".en.hi.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nProcessed\n")

    monkeypatch.setattr(upload, "postprocessing", postprocess)
    flow.submit(media_type, hi=True)
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    assert "Processed" in flow.indexes[-1][1]["Video.en.hi.srt"]
    flow.release_engine.set()
    sync_job, sync_thread = flow.start()
    sync_thread.join(2)
    assert sync_job.status == "completed"
    assert "Synced" in flow.indexes[-1][1]["Video.en.hi.srt"]
    assert "Processed" in flow.indexes[-1][1]["Video.en.hi.srt"]
    assert sum("arr_client" in kwargs for _, kwargs in flow.notifications) == 2
    assert not any("arr_instance_id" in kwargs for _, kwargs in flow.notifications)
    assert flow.sync_history[0]["hi"] is True


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_real_index_and_settings_stay_with_the_upload_owner(upload_flow, schema_session, monkeypatch, media_type):
    from app.config import settings
    from app.database import TableEpisodes, TableMovies, TableShows, select
    from arr_instances import resolution
    from subzero.language import Language
    from subtitles import upload
    from subtitles.indexer import movies, series
    from subtitles.tools import subsyncer

    flow = upload_flow
    module = series if media_type == "series" else movies
    table = TableEpisodes if media_type == "series" else TableMovies
    for owner in (7, 8):
        if media_type == "series":
            schema_session.add(TableShows(id=owner, arr_instance_id=owner, sonarrSeriesId=10,
                                          title="Show", path=str(flow.video.parent), profileId=None))
            schema_session.flush()
            row = TableEpisodes(id=owner, arr_instance_id=owner, series_id=owner,
                                sonarrSeriesId=10, sonarrEpisodeId=20, title="Episode", path=str(flow.video),
                                season=1, episode=1, subtitles="[]")
        else:
            row = TableMovies(id=owner, arr_instance_id=owner, radarrId=30, title="Movie",
                              path=str(flow.video), tmdbId=str(owner), profileId=None, subtitles="[]")
        schema_session.add(row)
    schema_session.commit()
    monkeypatch.setattr(upload, "database", schema_session)
    monkeypatch.setattr(module, "database", schema_session)
    monkeypatch.setattr(settings.general, "use_embedded_subs", False)
    monkeypatch.setattr(settings.general, "subfolder", "current")
    monkeypatch.setattr(module, "get_language_set", lambda: {Language.fromietf("en")})
    monkeypatch.setattr(module.core, "CUSTOM_PATHS", [])
    missing = "list_missing_subtitles" if media_type == "series" else "list_missing_subtitles_movies"
    monkeypatch.setattr(module, missing, lambda *a, **k: None)
    monkeypatch.setattr(module, "event_stream", lambda **kwargs: None)
    real_index = flow.real_indexers[media_type]

    def index(*args, **kwargs):
        result = real_index(*args, **kwargs)
        flow.indexes.append((kwargs["arr_instance_id"], result))
        return result

    monkeypatch.setattr(module, "store_subtitles" if media_type == "series" else "store_subtitles_movie", index)
    resolution._subtitle_settings_cache[7] = {"subsync": {"use_subsync": False}}
    resolution._subtitle_settings_cache[8] = {"subsync": {
        "use_subsync": True, "enabled_engines": ["alass"], "max_offset_seconds": 17,
        "use_subsync_threshold": True, "subsync_threshold": 100,
        "use_subsync_movie_threshold": True, "subsync_movie_threshold": 100,
    }}
    monkeypatch.setattr(settings.subsync, "use_subsync", False)
    monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
    external_calls = []

    def external(self, engine, output_path, video_path):
        external_calls.append(engine)
        output_path.write_text("1\n00:00:03,000 --> 00:00:04,000\nSynced\n")
        return {"offset_seconds": 16, "framerate_scale_factor": 1}

    monkeypatch.setattr(subsyncer.SubSyncer, "_run_external_engine", external)
    flow.submit(media_type, owner=8)

    def run_next():
        job = flow.queue.jobs_pending_queue.popleft()
        flow.queue.jobs_running_queue.append(job)
        assert flow.queue._run_job(job)
        return job

    upload_job = run_next()
    assert upload_job.job_returned_value == ("", 204)
    before = ast.literal_eval(schema_session.execute(select(table.subtitles).where(table.id == 8)).scalar())
    assert len(before) == 1 and before[0][0] == "en"
    assert schema_session.execute(select(table.subtitles).where(table.id == 7)).scalar() == "[]"
    sync_job = run_next()
    assert sync_job.job_returned_value is True
    after = ast.literal_eval(schema_session.execute(select(table.subtitles).where(table.id == 8)).scalar())
    assert {row[0] for row in after} == {"en", "en:sync-alass"}
    assert schema_session.execute(select(table.subtitles).where(table.id == 7)).scalar() == "[]"
    assert external_calls == ["alass"]
    assert all(row[0] == 8 for row in flow.indexes)
    assert flow.history[0][1]["arr_instance_id"] == 8
    assert flow.sync_history[0]["arr_instance_id"] == 8


def test_unrelated_upload_finishes_while_other_media_postprocessing_is_blocked(upload_flow, monkeypatch):
    from subtitles import processing, upload

    flow = upload_flow
    blocked, unblock, second_entered = Event(), Event(), Event()

    def config(kind, owner):
        if owner == 8:
            second_entered.set()
        return (owner == 7, 'controlled', False, 0)

    def postprocess(command, video_path, subtitle_path=None):
        blocked.set()
        assert unblock.wait(5), 'probe failed to release postprocessing'

    monkeypatch.setattr(processing, '_postprocessing_config', config)
    monkeypatch.setattr(upload, 'pp_replace', lambda *args: 'controlled')
    monkeypatch.setattr(upload, 'postprocessing', postprocess)
    monkeypatch.setattr(upload, 'set_chmod', lambda **kwargs: None)
    flow.submit('movie', owner=7)
    first_job, first_thread = flow.start()
    assert blocked.wait(2)
    other_video = flow.video.with_name('Unrelated.mkv')
    other_video.touch()
    second_id = upload.manual_upload_subtitle(
        path=str(other_video), language='en', forced=False, hi=False, media_type='movie',
        subtitle=BytesIO(b'1\n00:00:01,000 --> 00:00:02,000\nOther media\n'),
        filename='other.srt', audio_language=[], radarrId=31, arr_instance_id=8)
    second_job, second_thread = flow.start(second_id)
    try:
        assert second_entered.wait(2), 'second upload did not start'
        second_thread.join(0.25)
        completed_before_release = second_job.status == 'completed'
        saved_before_release = other_video.with_suffix('.en.srt').exists()
    finally:
        unblock.set()
        first_thread.join(2)
        second_thread.join(2)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert first_job.status == second_job.status == 'completed'
    assert completed_before_release and saved_before_release, 'unrelated upload was blocked by another media postprocessing lock'


@pytest.mark.parametrize("output_mode", ["overwrite", "keep_all"])
def test_cancel_while_finished_engine_waits_for_publication_preserves_upload(upload_flow, monkeypatch, output_mode):
    from app.config import settings
    monkeypatch.setattr(settings.subsync, "output_mode", output_mode)
    from subtitles.tools import subsync_engines, subsyncer

    flow = upload_flow
    flow.submit('movie')
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == 'completed'
    original = flow.video.with_suffix('.en.srt').read_bytes()
    finished_checkpoint = Event()
    real_report = subsyncer.SubSyncer._report_progress

    def report(self, message, value, total):
        result = real_report(self, message, value, total)
        if message.startswith('Finished '):
            finished_checkpoint.set()
        return result

    monkeypatch.setattr(subsyncer.SubSyncer, '_report_progress', report)
    with subsync_engines.subtitle_write_lock(str(flow.video), str(flow.video.parent)):
        flow.release_engine.set()
        sync_job, sync_thread = flow.start()
        assert finished_checkpoint.wait(2), 'engine did not reach publication boundary'
        assert flow.video.with_suffix('.en.srt').read_bytes() == original
        assert flow.queue.cancel_running_job(sync_job.job_id)
    sync_thread.join(2)
    assert not sync_thread.is_alive()
    assert sync_job.progress_message == 'Cancelled by user'
    assert flow.video.with_suffix('.en.srt').read_bytes() == original
    assert not flow.video.with_suffix('.en.ffsubsync.srt').exists()
    assert not list(flow.video.parent.glob('.bazarr-sync-*'))
    assert not flow.sync_history


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_sync_reindex_cannot_restore_a_listing_after_successful_delete(upload_flow, monkeypatch, media_type):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.sql.dml import Update
    from app.config import settings
    from app.database import Base, TableEpisodes, TableMovies, TableShows, select
    from subzero.language import Language
    from subtitles import upload
    from subtitles.indexer import movies, series
    from subtitles.tools import delete

    flow = upload_flow
    module = series if media_type == "series" else movies
    table = TableEpisodes if media_type == "series" else TableMovies
    engine = create_engine(f"sqlite:///{flow.video.parent / 'index.sqlite'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        if media_type == "series":
            session.add(TableShows(id=7, arr_instance_id=7, sonarrSeriesId=10,
                                   title="Show", path=str(flow.video.parent), profileId=None))
            session.flush()
            session.add(TableEpisodes(id=7, arr_instance_id=7, series_id=7, sonarrSeriesId=10,
                                      sonarrEpisodeId=20, title="Episode", path=str(flow.video),
                                      season=1, episode=1, subtitles="[]"))
        else:
            session.add(TableMovies(id=7, arr_instance_id=7, radarrId=30, title="Movie",
                                    path=str(flow.video), tmdbId="1", profileId=None, subtitles="[]"))
    before_commit, commit = Event(), Event()
    controls = {"armed": False}

    class Database:
        def execute(self, statement, *args, **kwargs):
            if controls["armed"] and isinstance(statement, Update):
                controls["armed"] = False
                before_commit.set()
                assert commit.wait(5)
            with Session.begin() as session:
                result = session.execute(statement, *args, **kwargs)
                return result.freeze()() if statement.is_select else result

    db = Database()
    for item in (upload, delete, module):
        monkeypatch.setattr(item, "database", db)
    monkeypatch.setattr(settings.general, "use_embedded_subs", False)
    monkeypatch.setattr(settings.general, "subfolder", "current")
    monkeypatch.setattr(module, "get_language_set", lambda: {Language.fromietf("en")})
    missing = "list_missing_subtitles" if media_type == "series" else "list_missing_subtitles_movies"
    monkeypatch.setattr(module, missing, lambda *a, **k: None)
    monkeypatch.setattr(module, "event_stream", lambda **k: None)

    def index(*args, **kwargs):
        result = flow.real_indexers[media_type](*args, **kwargs)
        flow.indexes.append((kwargs["arr_instance_id"], result))
        return result

    index_name = "store_subtitles" if media_type == "series" else "store_subtitles_movie"
    monkeypatch.setattr(module, index_name, index)
    monkeypatch.setattr(delete, index_name, index)
    deletion = None
    try:
        flow.submit(media_type)
        upload_job, upload_thread = flow.start()
        upload_thread.join(2)
        assert upload_job.status == "completed"
        assert len(ast.literal_eval(db.execute(select(table.subtitles)).scalar())) == 1
        controls["armed"] = True
        flow.release_engine.set()
        sync_job, sync_thread = flow.start()
        assert before_commit.wait(2)
        deleted = []
        deletion = Thread(target=lambda: deleted.append(flow.remove(media_type)))
        deletion.start()
        deletion.join(0.2)
        commit.set()
        sync_thread.join(2)
        deletion.join(2)
        assert not deletion.is_alive() and not sync_thread.is_alive()
        assert sync_job.status == "completed"
        assert deleted == [True]
        assert not flow.video.with_suffix(".en.srt").exists()
        assert ast.literal_eval(db.execute(select(table.subtitles)).scalar()) == []
    finally:
        commit.set()
        if deletion:
            deletion.join(2)
        engine.dispose()


@pytest.mark.parametrize("media_type", ["movie", "series"])
@pytest.mark.parametrize("output_mode", ["overwrite", "keep_all"])
def test_review_sync_publication_refreshes_external_consumers(upload_flow, monkeypatch, media_type, output_mode):
    from app.config import settings
    from subtitles import upload

    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "output_mode", output_mode)
    monkeypatch.setattr(settings.general, "use_plex", True)
    monkeypatch.setattr(settings.general, "use_jellyfin", True)
    for name in ("update_series_library", "update_movie_library"):
        monkeypatch.setattr(settings.plex, name, True)
        monkeypatch.setattr(settings.jellyfin, name, True)
    monkeypatch.setattr(settings.plex, "set_movie_added", False)
    monkeypatch.setattr(settings.plex, "set_episode_added", False)
    refreshes = []

    def refresh(consumer, *args, **kwargs):
        refreshes.append((consumer, {path.name: path.read_text() for path in flow.video.parent.glob("*.srt")}))

    for name in ("notify_sonarr", "notify_radarr", "plex_refresh_item", "jellyfin_refresh_item"):
        monkeypatch.setattr(upload, name, lambda *args, _consumer=name, **kwargs: refresh(_consumer, *args, **kwargs))
    flow.submit(media_type)
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    assert len(refreshes) == 3
    flow.release_engine.set()
    sync_job, sync_thread = flow.start()
    sync_thread.join(3)
    assert sync_job.status == "completed"
    assert len(refreshes) == 6, "external consumers were not refreshed after sync publication"
    expected = "Video.en.ffsubsync.srt" if output_mode == "keep_all" else "Video.en.srt"
    assert all("Synced" in files[expected] for _, files in refreshes[3:])
    assert len(flow.history) == 1
    assert len(flow.notifications) == 1, "sync publication repeated the upload notification"


def test_review_upload_sync_uses_resolved_series_id(upload_flow):
    flow = upload_flow
    flow.submit("series", sonarrSeriesId=999)
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    sync_job = flow.queue.jobs_pending_queue[0]
    assert sync_job.kwargs["sonarr_series_id"] == 10, "unvalidated request ID reached synchronization"
    assert sync_job.kwargs["callback"].keywords["sonarr_series_id"] == 10
    assert sync_job.kwargs["sonarr_episode_id"] == 20
    assert sync_job.kwargs["arr_instance_id"] == 7


@pytest.mark.parametrize("media_type", ["movie", "series"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "disabled"])
def test_review_replacement_upload_does_not_list_stale_generated_outputs(upload_flow, monkeypatch, media_type, outcome):
    from app.config import settings

    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
    old_output = flow.video.with_suffix(".en.ffsubsync.srt")
    old_bytes = b"1\n00:00:01,000 --> 00:00:02,000\nOld generated output\n"
    old_output.write_bytes(old_bytes)
    if outcome == "disabled":
        monkeypatch.setattr(settings.subsync, "use_subsync", False)
    flow.submit(media_type, content="Replacement upload")
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    assert "Replacement upload" in flow.indexes[-1][1]["Video.en.srt"]
    assert old_output.name not in flow.indexes[-1][1], "the initial upload index exposed old generated text"
    assert not old_output.exists()
    assert any(path.read_bytes() == old_bytes for path in flow.video.parent.rglob("*") if path.is_file()), \
        "old generated bytes were destroyed instead of quarantined"
    if outcome == "disabled":
        assert not flow.queue.jobs_pending_queue
        return
    flow.controls.fail = outcome == "failure"
    sync_job, sync_thread = flow.start()
    assert flow.engine_started.wait(2)
    if outcome == "cancel":
        flow.queue.cancel_running_job(sync_job.job_id)
    flow.release_engine.set()
    sync_thread.join(3)
    assert not sync_thread.is_alive()
    if outcome == "success":
        assert "Replacement upload" in old_output.read_text()
        assert "Old generated output" not in old_output.read_text()
    else:
        assert not old_output.exists()
        assert old_output.name not in flow.indexes[-1][1]


@pytest.mark.parametrize("media_type", ["movie", "series"])
@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_review_deleted_generated_destination_stays_deleted(upload_flow, monkeypatch, media_type, running):
    from app.config import settings
    from subtitles.tools import delete

    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
    destination = flow.video.with_suffix(".en.ffsubsync.srt")
    destination.write_text("Old generated output")
    flow.submit(media_type)
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    if running:
        sync_job, sync_thread = flow.start()
        assert flow.engine_started.wait(2)
    delete.delete_subtitles(media_type=media_type, language="en", forced=False, hi=False,
                           media_path=str(flow.video), subtitles_path=str(destination),
                           sonarr_series_id=10, sonarr_episode_id=20, radarr_id=30, arr_instance_id=7)
    assert not destination.exists()
    flow.release_engine.set()
    if not running:
        sync_job, sync_thread = flow.start()
    sync_thread.join(3)
    assert not sync_thread.is_alive()
    assert not destination.exists(), "background sync recreated an explicitly deleted generated destination"
    assert destination.name not in flow.indexes[-1][1]


@pytest.mark.parametrize("output_mode", ["overwrite", "keep_all"])
@pytest.mark.parametrize("operation", ["edit", "edit-etag", "promote", "mods", "manual-sync", "google", "lingarr", "openrouter", "gemini", "manual-download", "auto-download", "postprocessing"])
def test_review_editor_write_cannot_be_lost_at_sync_publication(upload_flow, monkeypatch, output_mode, operation):
    import importlib
    import os
    import sys
    from threading import current_thread
    from flask import Flask
    from app.config import settings
    from subtitles.tools import subsync_engines

    existing = sys.modules.get("api.subtitles.content")
    if existing is not None and not getattr(existing, "__file__", None):
        monkeypatch.delitem(sys.modules, "api.subtitles.content")
    with monkeypatch.context() as importing:
        importing.setattr("app.check_update.check_releases", lambda *args, **kwargs: None)
        importing.setattr("app.announcements.get_announcements_to_file", lambda *args, **kwargs: None)
        content = importlib.import_module("api.subtitles.content")
    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "output_mode", output_mode)
    source = flow.video.with_suffix(".en.srt")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    promotion = flow.video.with_suffix(".en.alass.srt")
    user_text = "1\n00:00:01,000 --> 00:00:02,000\nUser selected text\n"
    metadata = {"mediaPath": str(flow.video), "mediaId": 30, "arrInstanceId": 7}
    monkeypatch.setattr(content, "resolve_subtitle_path", lambda media_type, media_id, language, **kwargs:
                        (str(promotion if language == "en:sync-alass" else source), metadata))
    monkeypatch.setattr(content, "store_subtitles_movie", lambda *args, **kwargs: None)
    monkeypatch.setattr(content, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(content, "history_log_movie", lambda **kwargs: None)
    monkeypatch.setattr(content, "language_from_alpha2", lambda value: "English")
    reached_publication, release_publication, writer_started, writer_done = Event(), Event(), Event(), Event()
    real_replace = os.replace
    published_destination = output if output_mode == "keep_all" else source

    def replace(temporary, destination):
        if (Path(destination) == published_destination and Path(temporary).name.startswith(".bazarr-sync-")
                and current_thread().name != "competing-subtitle-writer"):
            reached_publication.set()
            assert release_publication.wait(5), "test did not release sync publication"
        return real_replace(temporary, destination)

    monkeypatch.setattr(subsync_engines.os, "replace", replace)
    flow.submit("movie")
    upload_job, upload_thread = flow.start()
    upload_thread.join(2)
    assert upload_job.status == "completed"
    promotion.write_text(user_text)
    initial_etag = content.generate_etag(str(source))
    sync_job, sync_thread = flow.start()
    assert flow.engine_started.wait(2)
    flow.release_engine.set()
    writer_results = []
    writer_errors = []

    def write():
        try:
            writer_started.set()
            if operation in ("edit", "edit-etag"):
                app = Flask(__name__)
                with app.test_request_context(json={"content": user_text},
                                              headers={"If-Match": initial_etag} if operation == "edit-etag" else {}):
                    response = content._save_subtitle_content("movie", 30, "en", arr_instance_id=7)
                    writer_results.append(response.status_code if hasattr(response, "status_code") else response[1])
            elif operation == "promote":
                _, status = content.promote_sync_subtitle("movie", 30, "en", "en:sync-alass", arr_instance_id=7)
                writer_results.append(status)
            elif operation == "mods":
                from subtitles.tools import mods
                monkeypatch.setattr(mods.Subtitle, "get_modified_content", lambda self, **kwargs: user_text.encode())
                mods.subtitles_apply_mods("en", str(source), [], str(flow.video), arr_instance_id=7)
                writer_results.append(200)
            elif operation == "manual-sync":
                from subtitles.tools import subsyncer

                def external(self, engine, output_path, video_path):
                    output_path.write_text(user_text)
                    return {"offset_seconds": 0}

                monkeypatch.setattr(subsyncer.SubSyncer, "_run_external_engine", external)
                result = subsyncer.SubSyncer().sync(
                    str(flow.video), str(source), "en", False, False, 60, False, False,
                    output_mode="overwrite", enabled_engines=["alass"], write_history=False)
                writer_results.append(200 if result.success else 500)
            elif operation in ("manual-download", "auto-download"):
                from subtitles import manual, download, pool
                from subliminal_patch.subtitle import Subtitle
                from subzero.language import Language

                subtitle = Subtitle(Language("eng"), original_format=True)
                subtitle.content = user_text.encode()
                subtitle.provider_name = "fixture"
                subtitle.matches = set()
                video = Mock(original_path=str(flow.video))
                provider_pool = Mock(providers=["fixture"], discarded_providers=[])
                monkeypatch.setattr(pool, "_update_pool", lambda *args, **kwargs: False)
                module = manual if operation == "manual-download" else download
                monkeypatch.setattr(module, "get_video", lambda *args, **kwargs: video)
                monkeypatch.setattr(module, "_get_pool", lambda *args, **kwargs: provider_pool)
                monkeypatch.setattr(module, "get_target_folder", lambda *args, **kwargs: None)
                monkeypatch.setattr(module, "process_subtitle", lambda **kwargs: "saved")
                monkeypatch.setattr(module.subliminal, "region", Mock())
                if operation == "manual-download":
                    monkeypatch.setattr(manual.subtitle_cache, "get", lambda *args: subtitle)
                    monkeypatch.setattr(manual, "download_subtitles", lambda *args: None)
                    result = manual.manual_download_subtitle(
                        str(flow.video), [], False, False, "cached", "fixture", None, "Video", "movie", True, 1,
                        arr_instance_id=7)
                    assert result == "saved"
                else:
                    monkeypatch.setattr(download, "_get_language_obj", lambda **kwargs: {Language("eng")})
                    monkeypatch.setattr(download, "get_profiles_list", lambda **kwargs: {"originalFormat": True})
                    monkeypatch.setattr(download, "download_best_subtitles", lambda **kwargs: {video: [subtitle]})
                    monkeypatch.setattr(download, "clear_mismatch_for_video", lambda *args, **kwargs: None)
                    result = list(download.generate_subtitles(
                        str(flow.video), [("en", False, False)], [], None, "Video", "movie", 1, arr_instance_id=7))
                    assert result == ["saved"]
                writer_results.append(200)
            elif operation == "postprocessing":
                from subtitles import post_processing

                def popen(*args, **kwargs):
                    source.write_text(user_text)
                    return Mock(communicate=lambda: ("", ""))

                monkeypatch.setattr(post_processing.subprocess, "Popen", popen)
                post_processing.postprocessing("fixture command", str(flow.video))
                writer_results.append(200)
            else:
                module = importlib.import_module(f"subtitles.tools.translate.services.{operation}_translator")
                classes = {"google": "GoogleTranslatorService", "lingarr": "LingarrTranslatorService",
                           "openrouter": "OpenRouterTranslatorService", "gemini": "GeminiTranslatorService"}
                service = getattr(module, classes[operation])(
                    source_srt_file=str(promotion), dest_srt_file=str(source), lang_obj=None,
                    to_lang="eng", from_lang="en", media_type="movie", video_path=str(flow.video),
                    orig_to_lang="en", forced=False, hi=False, sonarr_series_id=None,
                    sonarr_episode_id=None, radarr_id=30)
                monkeypatch.setattr(module, "jobs_queue", Mock())
                monkeypatch.setattr(module, "history_log_movie", Mock())
                monkeypatch.setattr(module, "create_process_result", Mock())
                monkeypatch.setattr(module, "add_translator_info", lambda *args: None)
                if operation == "google":
                    monkeypatch.setattr(service, "_translate_text", lambda *args: "User selected text")
                elif operation == "lingarr":
                    monkeypatch.setattr(service, "_translate_content", lambda *args, **kwargs:
                                        [{"position": 0, "line": "User selected text"}])
                elif operation == "openrouter":
                    monkeypatch.setattr(service, "_submit_and_poll", lambda *args, **kwargs:
                                        [{"position": 0, "line": "User selected text"}])
                else:
                    monkeypatch.setattr(service, "_get_configured_api_keys", lambda: [])
                    monkeypatch.setattr(service, "_select_next_api_key", lambda: "fixture")
                    monkeypatch.setattr(service, "_check_saved_progress", lambda: None)
                    monkeypatch.setattr(module, "get_description", lambda *args, **kwargs: "")
                    monkeypatch.setattr(service, "_translate_with_gemini", lambda:
                                        Path(service.output_file).write_text(user_text))
                service.translate(job_id=123)
                writer_results.append(200)

        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    writer = Thread(target=write, name="competing-subtitle-writer")
    try:
        assert reached_publication.wait(2)
        writer.start()
        assert writer_started.wait(2)
        writer_done.wait(0.3)
        release_publication.set()
        sync_thread.join(3)
        writer.join(3)
        assert not writer.is_alive() and not sync_thread.is_alive()
        assert not writer_errors
        if operation == "edit-etag" and output_mode == "overwrite":
            assert writer_results == [412]
            assert "Synced" in source.read_text()
        else:
            assert writer_results == [204 if operation in ("edit", "edit-etag") else 200]
            assert "User selected text" in source.read_text(), "sync publication overwrote the completed user write"
        if output_mode == "keep_all":
            assert not output.exists(), "an output for the superseded source remained available"
    finally:
        release_publication.set()
        if writer.ident:
            writer.join(3)


@pytest.mark.parametrize("failure", ["cancel-before-engine", "setup-error", "settings-error"])
def test_review_terminal_job_releases_publication_lease(upload_flow, monkeypatch, failure):
    from subtitles import sync

    flow = upload_flow
    flow.submit("movie")
    _, upload_thread = flow.start()
    upload_thread.join(2)
    job = flow.queue.jobs_pending_queue[0]
    publication = job.kwargs["source_version"]
    if failure == "settings-error":
        monkeypatch.setattr(sync, "_resolve_subsync_overrides", Mock(side_effect=RuntimeError("controlled settings failure")))
    elif failure == "setup-error":
        monkeypatch.setattr(sync, "SubSyncer", Mock(side_effect=RuntimeError("controlled setup failure")))
    else:
        original = sync._report_progress

        def report(*args, **kwargs):
            flow.queue.cancel_running_job(job.job_id)
            return original(*args, **kwargs)

        monkeypatch.setattr(sync, "_report_progress", report)
    _, thread = flow.start()
    thread.join(3)
    assert not thread.is_alive()
    assert job.status in ("completed", "failed")
    assert publication.state is None, "terminal Job kwargs retained the media coordinator"


def test_review_partial_publication_refreshes_consumers_after_cancellation(upload_flow, monkeypatch):
    from app.config import settings
    from app.jobs_queue import JobCancelled
    from subtitles.tools import subsyncer

    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
    monkeypatch.setattr(settings.subsync, "enabled_engines", ["ffsubsync", "alass"])
    def cancel(self, **kwargs):
        flow.queue.cancel_running_job(self.job_id)
        raise JobCancelled()

    monkeypatch.setattr(subsyncer.SubSyncer, "_run_external_engine", cancel)
    flow.submit("movie")
    _, thread = flow.start()
    thread.join(2)
    flow.release_engine.set()
    job, thread = flow.start()
    thread.join(3)
    assert job.status == "completed" and job.cancelled
    assert flow.video.with_suffix(".en.ffsubsync.srt").is_file()
    assert sum("arr_client" in kwargs for _, kwargs in flow.notifications) == 2
    assert sum("arr_instance_id" in kwargs for _, kwargs in flow.notifications) == 1
    assert len(flow.history) == 1


def test_review_failed_delete_preserves_existing_generated_output(upload_flow, monkeypatch):
    import os
    from subtitles.tools import delete

    flow = upload_flow
    source = flow.video.with_suffix(".en.srt")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    source.write_text("Base")
    output.write_text("Valid generated output")
    remove = os.remove

    def reject(path):
        if Path(path) == source:
            raise PermissionError("controlled rejected delete")
        return remove(path)

    monkeypatch.setattr(delete.os, "remove", reject)
    assert flow.remove("movie") is False
    assert source.read_text() == "Base"
    assert output.read_text() == "Valid generated output"


@pytest.mark.parametrize("collision", [False, True], ids=["same-mapped-owner", "distinct-owners"])
def test_review_shared_folder_quarantine_requires_unique_mapped_owner(upload_flow, schema_session, monkeypatch, collision):
    from app import database as db_module
    from app.database import TableMovies
    from app.config import settings
    from subtitles.tools.subsync_engines import quarantine_sync_outputs, SubtitlePublication, subtitle_source_version

    flow = upload_flow
    folder = flow.video.parent / "shared"
    folder.mkdir()
    other = flow.video.parent / "other" / flow.video.name
    other.parent.mkdir()
    other.touch()
    source = folder / "Video.en.srt"
    output = folder / "Video.en.ffsubsync.srt"
    source.write_text("Base")
    output.write_text("Existing generated bytes")
    for owner, path in [(7, flow.video), (8, other if collision else flow.video)]:
        schema_session.add(TableMovies(id=owner, arr_instance_id=owner, radarrId=30,
                                      title="Movie", path=str(path), tmdbId=str(owner), subtitles="[]"))
    schema_session.commit()
    monkeypatch.setattr(db_module, "database", schema_session)
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(folder))
    quarantine_sync_outputs(str(flow.video), str(source))
    publication = SubtitlePublication(str(flow.video), str(source), subtitle_source_version(source))
    try:
        if collision:
            assert output.read_text() == "Existing generated bytes"
            assert not publication.destination_unchanged(output), "ambiguous output remained eligible for overwrite"
            from subtitles.indexer.utils import add_sync_engine_outputs
            assert output.name not in add_sync_engine_outputs(str(folder), {}, video_path=str(flow.video))
        else:
            assert not output.exists()
            assert publication.destination_unchanged(output)
            assert any(p.read_text() == "Existing generated bytes" for p in folder.glob("*.bak"))
    finally:
        publication.release()


def test_review_create_cannot_overwrite_a_later_upload(upload_flow, monkeypatch):
    import importlib
    from threading import current_thread
    from flask import Flask
    from app.config import settings

    with monkeypatch.context() as importing:
        importing.setattr("app.check_update.check_releases", lambda *args, **kwargs: None)
        importing.setattr("app.announcements.get_announcements_to_file", lambda *args, **kwargs: None)
        content = importlib.import_module("api.subtitles.content")
    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "use_subsync", False)
    monkeypatch.setattr(content, "database", Mock(execute=Mock(return_value=Mock(
        first=Mock(return_value=SimpleNamespace(path=str(flow.video), id=30, arr_instance_id=7))))))
    monkeypatch.setattr(content, "get_target_folder", lambda *args: None)
    monkeypatch.setattr(content, "store_subtitles_movie", lambda *args, **kwargs: None)
    monkeypatch.setattr(content, "event_stream", lambda **kwargs: None)
    reached, release = Event(), Event()
    mkstemp = content.tempfile.mkstemp

    def pause(*args, **kwargs):
        if current_thread().name == "create-subtitle":
            reached.set()
            assert release.wait(5)
        return mkstemp(*args, **kwargs)

    monkeypatch.setattr(content.tempfile, "mkstemp", pause)
    results = []

    def create():
        with Flask(__name__).test_request_context(json={"language": "en", "format": "srt", "content": "Created first"}):
            results.append(content._create_subtitle("movie", 30, arr_instance_id=7))

    creator = Thread(target=create, name="create-subtitle")
    creator.start()
    try:
        assert reached.wait(2)
        flow.submit("movie", content="Uploaded later")
        job, uploader = flow.start()
        uploader.join(0.3)
        release.set()
        creator.join(3)
        uploader.join(3)
        assert results[0][1] == 201
        assert job.status == "completed"
        assert "Uploaded later" in flow.video.with_suffix(".en.srt").read_text()
    finally:
        release.set()
        creator.join(3)


def test_review_combine_waits_for_an_existing_publication(upload_flow, monkeypatch):
    from subtitles.tools.combine import main
    from subtitles.tools.combine.rules import SourcePaths
    from subtitles.tools.subsync_engines import subtitle_write_lock

    flow = upload_flow
    first = flow.video.with_suffix(".en.srt")
    second = flow.video.with_suffix(".hu.srt")
    first.write_text("1\n00:00:01,000 --> 00:00:02,000\nFirst\n")
    second.write_text("1\n00:00:01,000 --> 00:00:02,000\nSecond\n")
    monkeypatch.setattr(main, "resolve_source_paths", lambda **kwargs: SourcePaths(str(first), [str(second)]))
    monkeypatch.setattr(main, "_post_write", lambda *args, **kwargs: None)
    output = flow.video.with_suffix(".en.combined-hu.srt")
    results = []
    finished = Event()

    def combine():
        results.append(main.try_combine_for_video(str(flow.video), "movie", languages=["en", "hu"], format="srt"))
        finished.set()

    with subtitle_write_lock(str(flow.video), str(flow.video.parent)):
        worker = Thread(target=combine)
        worker.start()
        blocked = not finished.wait(0.3)
    worker.join(3)
    assert results[0].status == "built"
    assert blocked, "combine published while another writer owned the publication lock"
    assert output.is_file()


@pytest.mark.parametrize("media_type", ["series", "movie"])
def test_review_external_index_waits_for_publication(upload_flow, monkeypatch, media_type):
    from subtitles.indexer import movies, series
    from subtitles.tools.subsync_engines import subtitle_write_lock

    flow = upload_flow
    module = movies if media_type == "movie" else series
    db = Mock()
    db.execute.return_value.first.return_value = SimpleNamespace(arr_instance_id=7, subtitles="[]")
    db.execute.return_value.all.return_value = []
    monkeypatch.setattr(module, "database", db)
    monkeypatch.setattr("app.config.settings.general.use_embedded_subs", False)
    monkeypatch.setattr(module, "get_language_set", lambda: set())
    monkeypatch.setattr(module, "event_stream", lambda **kwargs: None)
    entered = Event()
    monkeypatch.setattr(module, "search_external_subtitles", lambda *args, **kwargs: (entered.set() or {}))
    errors = []

    def index():
        try:
            flow.real_indexers[media_type](str(flow.video), str(flow.video), arr_instance_id=7)
        except Exception as exc:
            errors.append(exc)

    with subtitle_write_lock(str(flow.video), str(flow.video.parent)):
        worker = Thread(target=index)
        worker.start()
        entered_while_locked = entered.wait(0.3)
    worker.join(3)
    assert not worker.is_alive() and not errors
    assert entered.is_set()
    assert not entered_while_locked, "external scan ran inside another writer's publication transaction"


def test_review_duplicate_enqueue_keeps_the_existing_job_lease(upload_flow):
    from subtitles import sync

    flow = upload_flow
    flow.submit("movie")
    _, thread = flow.start()
    thread.join(2)
    job = flow.queue.jobs_pending_queue[0]
    publication = job.kwargs["source_version"]
    assert sync.sync_subtitles(**job.kwargs) is False
    assert list(flow.queue.jobs_pending_queue) == [job]
    assert publication.state is not None, "duplicate rejection invalidated the original queued publication"
    flow.release_engine.set()
    _, thread = flow.start()
    thread.join(3)
    assert "Synced" in flow.video.with_suffix(".en.srt").read_text()
    assert publication.state is None


def test_review_removed_pending_job_does_not_retain_the_coordinator(upload_flow):
    import gc
    import weakref

    flow = upload_flow
    flow.submit("movie")
    _, thread = flow.start()
    thread.join(2)
    job = flow.queue.jobs_pending_queue[0]
    state = weakref.ref(job.kwargs["source_version"].state)
    assert flow.queue.remove_job_from_pending_queue(job.job_id)
    del job
    gc.collect()
    assert state() is None


@pytest.mark.parametrize("media_type", ["series", "movie"])
def test_review_notification_failure_does_not_strand_upload_sync(upload_flow, monkeypatch, media_type):
    from subtitles import upload

    flow = upload_flow
    name = "send_notifications" if media_type == "series" else "send_notifications_movie"
    monkeypatch.setattr(upload, name, Mock(side_effect=RuntimeError("controlled notification failure")))
    flow.submit(media_type)
    job, thread = flow.start()
    thread.join(2)
    assert job.status == "completed"
    assert len(flow.queue.jobs_pending_queue) == 1
    flow.release_engine.set()
    _, thread = flow.start()
    thread.join(3)
    assert "Synced" in flow.video.with_suffix(".en.srt").read_text()


@pytest.fixture
def review_translator(upload_flow, monkeypatch, request):
    import importlib

    flow = upload_flow
    name, existing = request.param if isinstance(request.param, tuple) else (request.param, True)
    module = importlib.import_module(f"subtitles.tools.translate.services.{name}_translator")
    class_name = {"google": "GoogleTranslatorService", "lingarr": "LingarrTranslatorService",
                  "openrouter": "OpenRouterTranslatorService", "gemini": "GeminiTranslatorService"}[name]
    source = flow.video.with_suffix(".hu.srt")
    destination = flow.video.with_suffix(".en.srt")
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nSource\n")
    if existing:
        destination.write_text("1\n00:00:01,000 --> 00:00:02,000\nOriginal destination\n")
    service = getattr(module, class_name)(
        source_srt_file=str(source), dest_srt_file=str(destination), lang_obj=None,
        to_lang="eng", from_lang="en", media_type="movie", video_path=str(flow.video),
        orig_to_lang="en", forced=False, hi=False, sonarr_series_id=None, sonarr_episode_id=None, radarr_id=30,
        arr_instance_id=7)
    monkeypatch.setattr(module, "jobs_queue", flow.queue)
    monkeypatch.setattr(module, "history_log_movie", Mock())
    monkeypatch.setattr(module, "create_process_result", Mock())
    monkeypatch.setattr(module, "add_translator_info", lambda *args: None)
    started, release, computed = Event(), Event(), Event()

    def compute(*args, **kwargs):
        started.set()
        assert release.wait(5)
        computed.set()
        if name == "google":
            return "Translated text"
        if name == "gemini":
            Path(service.output_file).write_text("1\n00:00:01,000 --> 00:00:02,000\nTranslated text\n")
            return
        return [{"position": 0, "line": "Translated text"}]

    transport = {"google": "_translate_text", "lingarr": "_translate_content",
                 "openrouter": "_submit_and_poll", "gemini": "_translate_with_gemini"}[name]
    monkeypatch.setattr(service, transport, compute)
    if name == "gemini":
        monkeypatch.setattr(service, "_get_configured_api_keys", lambda: [])
        monkeypatch.setattr(service, "_select_next_api_key", lambda: "fixture")
        monkeypatch.setattr(service, "_check_saved_progress", lambda: None)
        monkeypatch.setattr(module, "get_description", lambda *args, **kwargs: "")
    job_id = flow.queue.feed_jobs_pending_queue("Translation", "fixture", "translate", is_progress=True)
    job = flow.queue.jobs_pending_queue.popleft()
    job.status = "running"
    flow.queue.jobs_running_queue.append(job)
    errors = []

    def run():
        try:
            service.translate(job_id=job_id)
        except Exception as exc:
            errors.append(exc)

    worker = Thread(target=run)
    worker.start()
    assert started.wait(2)
    yield SimpleNamespace(flow=flow, service=service, destination=destination, release=release,
                          computed=computed, worker=worker, errors=errors, job_id=job_id)
    release.set()
    worker.join(5)
    assert not worker.is_alive()


@pytest.mark.parametrize("review_translator", ["google", "lingarr", "openrouter", "gemini"], indirect=True)
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_review_translation_preserves_changes_made_during_remote_work(review_translator, monkeypatch, change):
    from subtitles.tools import mods
    from subtitles.tools.subsync_engines import SubtitleDestinationChanged

    run = review_translator
    if change == "edit":
        edited = b"1\n00:00:01,000 --> 00:00:02,000\nLater edit\n"
        monkeypatch.setattr(mods.Subtitle, "get_modified_content", lambda *args, **kwargs: edited)
        mods.subtitles_apply_mods("en", str(run.destination), [], str(run.flow.video), arr_instance_id=7)
    else:
        assert run.flow.remove("movie")
    run.release.set()
    run.worker.join(3)
    assert not run.worker.is_alive()
    assert any(isinstance(exc, SubtitleDestinationChanged) for exc in run.errors)
    if change == "edit":
        assert run.destination.read_bytes() == edited
    else:
        assert not run.destination.exists()


@pytest.mark.parametrize("review_translator", ["google", "lingarr", "openrouter", "gemini"], indirect=True)
def test_review_translation_cancellation_while_waiting_to_publish_keeps_old_file(review_translator):
    from app.jobs_queue import JobCancelled
    from subtitles.tools.subsync_engines import subtitle_write_lock

    run = review_translator
    original = run.destination.read_bytes()
    with subtitle_write_lock(str(run.flow.video), str(run.destination.parent)):
        run.release.set()
        assert run.computed.wait(2)
        assert run.flow.queue.cancel_running_job(run.job_id)
    run.worker.join(3)
    assert not run.worker.is_alive()
    assert any(isinstance(exc, JobCancelled) for exc in run.errors)
    assert run.destination.read_bytes() == original


@pytest.mark.parametrize("review_translator", [(name, False) for name in ["google", "lingarr", "openrouter", "gemini"]], indirect=True)
def test_review_new_translation_preserves_process_umask(review_translator):
    run = review_translator
    control = run.destination.with_name("permission-control.txt")
    control.write_text("control")
    run.release.set()
    run.worker.join(3)
    assert not run.errors
    assert run.destination.stat().st_mode & 0o777 == control.stat().st_mode & 0o777


@pytest.mark.parametrize("failure", ["write-error", "empty-content"])
def test_review_failed_upload_save_preserves_prior_subtitle_and_outputs(upload_flow, monkeypatch, failure):
    import builtins
    from subtitles import upload

    flow = upload_flow
    source = flow.video.with_suffix(".en.srt")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    source.write_text("Prior base bytes")
    output.write_text("Prior generated bytes")
    if failure == "empty-content":
        monkeypatch.setattr(upload.Subtitle, "get_modified_content", lambda *args, **kwargs: None)
    else:
        original = builtins.open

        def fail(file, mode="r", *args, **kwargs):
            if "w" in mode and "b" in mode:
                raise OSError("controlled subtitle write failure")
            return original(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail)
    flow.submit("movie")
    _, thread = flow.start()
    thread.join(3)
    assert source.read_text() == "Prior base bytes"
    assert output.read_text() == "Prior generated bytes"
    assert not flow.queue.jobs_pending_queue


def test_review_failed_upgrade_keeps_previous_subtitle(upload_flow, monkeypatch):
    from subtitles import download, pool
    from subliminal_patch.subtitle import Subtitle
    from subzero.language import Language

    flow = upload_flow
    previous = flow.video.with_suffix(".en.ass")
    output = flow.video.with_suffix(".en.ffsubsync.ass")
    previous.write_text("Prior base bytes")
    output.write_text("Prior generated bytes")
    subtitle = Subtitle(Language("eng"), original_format=True)
    subtitle.content = b"1\n00:00:01,000 --> 00:00:02,000\nReplacement\n"
    subtitle.matches = set()
    video = Mock(original_path=str(flow.video))
    monkeypatch.setattr(pool, "_update_pool", lambda *args, **kwargs: False)
    monkeypatch.setattr(download, "get_video", lambda *args, **kwargs: video)
    monkeypatch.setattr(download, "_get_pool", lambda *args, **kwargs: Mock(providers=["fixture"]))
    monkeypatch.setattr(download, "_get_language_obj", lambda **kwargs: {Language("eng")})
    monkeypatch.setattr(download, "get_profiles_list", lambda **kwargs: {"originalFormat": True})
    monkeypatch.setattr(download, "download_best_subtitles", lambda **kwargs: {video: [subtitle]})
    monkeypatch.setattr(download, "get_target_folder", lambda *args: None)
    monkeypatch.setattr(download, "save_subtitles", Mock(side_effect=OSError("controlled failed replacement")))
    list(download.generate_subtitles(str(flow.video), [("en", False, False)], [], None, "Video", "movie", 1,
                                     is_upgrade=True, previous_subtitles_to_delete=str(previous), arr_instance_id=7))
    assert previous.read_text() == "Prior base bytes"
    assert output.read_text() == "Prior generated bytes"


@pytest.fixture
def review_combine(upload_flow, monkeypatch):
    from subtitles.tools.combine import main
    from subtitles.tools.combine.rules import SourcePaths

    flow = upload_flow
    primary = flow.video.with_suffix(".en.srt")
    secondary = flow.video.with_suffix(".hu.srt")
    primary.write_text("1\n00:00:01,000 --> 00:00:02,000\nFirst\n")
    secondary.write_text("1\n00:00:01,000 --> 00:00:02,000\nSecond\n")
    monkeypatch.setattr(main, "resolve_source_paths", lambda **kwargs: SourcePaths(str(primary), [str(secondary)]))
    monkeypatch.setattr(main, "_post_write", lambda *args, **kwargs: None)
    return SimpleNamespace(flow=flow, module=main, output=flow.video.with_suffix(".en.combined-hu.srt"),
                           run=lambda format="srt": main.try_combine_for_video(
                               str(flow.video), "movie", languages=["en", "hu"], format=format))


def test_review_combine_cleanup_does_not_follow_sibling_symlinks(review_combine):
    run = review_combine
    foreign = run.flow.video.with_suffix(".fr.ass")
    foreign.write_text("Foreign subtitle bytes")
    run.output.with_suffix(".ass").symlink_to(foreign)
    assert run.run().status == "built"
    assert foreign.read_text() == "Foreign subtitle bytes"
    assert run.output.with_suffix(".ass").is_symlink()


def test_review_combine_format_rebuilds_cannot_delete_each_other(review_combine, monkeypatch):
    run = review_combine
    first_cleanup, second_cleanup = Event(), Event()
    original = run.module._remove_stale_combined_siblings
    results = []

    def cleanup(path, video, on_publish=None):
        mine, other = (first_cleanup, second_cleanup) if path.endswith(".srt") else (second_cleanup, first_cleanup)
        mine.set()
        other.wait(0.3)
        original(path, video, on_publish)

    monkeypatch.setattr(run.module, "_remove_stale_combined_siblings", cleanup)
    workers = [Thread(target=lambda fmt=fmt: results.append(run.run(fmt))) for fmt in ("srt", "ass")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert all(result.status == "built" for result in results)
    assert sum(run.output.with_suffix(ext).is_file() for ext in (".srt", ".ass")) == 1


def test_review_combine_preserves_edit_during_composition(review_combine, monkeypatch):
    from subtitles.tools import mods

    run = review_combine
    run.output.write_text("1\n00:00:01,000 --> 00:00:02,000\nOriginal combined\n")
    entered, release = Event(), Event()
    original = run.module.compose

    def compose(**kwargs):
        result = original(**kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(run.module, "compose", compose)
    results = []
    worker = Thread(target=lambda: results.append(run.run()))
    worker.start()
    try:
        assert entered.wait(2)
        edited = b"1\n00:00:01,000 --> 00:00:02,000\nEdited combined\n"
        monkeypatch.setattr(mods.Subtitle, "get_modified_content", lambda *args, **kwargs: edited)
        mods.subtitles_apply_mods("en", str(run.output), [], str(run.flow.video), arr_instance_id=7)
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert run.output.read_bytes() == edited
        assert results[0].status != "built"
    finally:
        release.set()
        worker.join(3)


def test_review_postprocessing_without_a_queued_job_invalidates_exact_outputs(upload_flow, monkeypatch):
    from subtitles import post_processing

    flow = upload_flow
    source = flow.video.with_suffix(".en.srt")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    source.write_text("Before processing")
    output.write_text("Generated before processing")

    def popen(*args, **kwargs):
        source.write_text("After processing")
        return Mock(communicate=lambda: ("", ""))

    monkeypatch.setattr(post_processing.subprocess, "Popen", popen)
    post_processing.postprocessing("fixture", str(flow.video), subtitle_path=str(source))
    assert source.read_text() == "After processing"
    assert not output.exists()
    assert any(path.read_text() == "Generated before processing" for path in flow.video.parent.glob("*.bak"))


def test_review_partial_multiformat_save_records_each_actual_write(upload_flow):
    from functools import partial
    from subliminal_patch.core import save_subtitles
    from subliminal_patch.subtitle import Subtitle
    from subzero.language import Language
    from subtitles.tools.subsync_engines import write_subtitle_file, SubtitlePublication, subtitle_source_version

    flow = upload_flow
    source = flow.video.with_suffix(".en.srt")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    source.write_text("Prior source")
    output.write_text("Prior generated output")
    publication = SubtitlePublication(str(flow.video), str(source), subtitle_source_version(source))
    subtitle = Subtitle(Language("eng"), original_format=True)
    subtitle.content = b"1\n00:00:01,000 --> 00:00:02,000\nReplacement\n"

    def content(format, **kwargs):
        if format == "ass":
            raise ValueError("controlled second format failure")
        return b"First format succeeded"

    subtitle.get_modified_content = content
    written_paths = []
    try:
        with pytest.raises(ValueError, match="second format"):
            save_subtitles(str(flow.video), [subtitle], formats=("srt", "ass"),
                           write_subtitle=partial(write_subtitle_file, str(flow.video), written_paths=written_paths))
        assert written_paths == [str(source)]
        assert source.read_text() == "First format succeeded"
        assert not publication.source_unchanged()
        assert not output.exists()
    finally:
        publication.release()


def test_review_index_failure_does_not_suppress_refresh_of_published_output(upload_flow):
    flow = upload_flow
    flow.submit("movie")
    _, thread = flow.start()
    thread.join(2)
    job = flow.queue.jobs_pending_queue[0]
    job.kwargs["callback"] = Mock(side_effect=RuntimeError("controlled final index failure"))
    flow.release_engine.set()
    _, thread = flow.start()
    thread.join(3)
    assert "Synced" in flow.video.with_suffix(".en.srt").read_text()
    assert sum("arr_client" in kwargs for _, kwargs in flow.notifications) == 2
    assert job.kwargs["source_version"].state is None


def test_review_deleting_one_engine_destination_preserves_other_engine_publication(upload_flow, monkeypatch):
    from app.config import settings
    from subtitles.tools import subsyncer, delete

    flow = upload_flow
    monkeypatch.setattr(settings.subsync, "enabled_engines", ["ffsubsync", "alass"])
    monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
    monkeypatch.setattr(subsyncer.SubSyncer, "_run_external_engine", lambda self, engine, output_path, video_path:
                        (output_path.write_text("Other engine output") and {"offset_seconds": 0}))
    flow.submit("movie")
    _, thread = flow.start()
    thread.join(2)
    deleted = flow.video.with_suffix(".en.ffsubsync.srt")
    assert delete.delete_subtitles("movie", "en", False, False, str(flow.video), str(deleted),
                                   radarr_id=30, arr_instance_id=7) is False
    _, thread = flow.start()
    thread.join(3)
    assert not flow.engine_calls
    assert not deleted.exists()
    assert flow.video.with_suffix(".en.alass.srt").read_text() == "Other engine output"


def test_review_quarantine_moves_only_exact_regular_variants(upload_flow):
    from subtitles.tools.subsync_engines import quarantine_sync_outputs, SubtitlePublication, subtitle_source_version

    flow = upload_flow
    source = flow.video.with_suffix(".en.hi.ass")
    source.write_text("Source")
    exact = flow.video.with_suffix(".en.hi.ffsubsync.ass")
    exact.write_text("Owned generated bytes")
    foreign = flow.video.with_suffix(".hu.srt")
    foreign.write_text("Foreign bytes")
    symlink = flow.video.with_suffix(".en.hi.alass.ass")
    symlink.symlink_to(foreign)
    untouched = [flow.video.with_suffix(suffix) for suffix in
                 (".en.ffsubsync.ass", ".en.hi.ffsubsync.srt", ".en.forced.ffsubsync.ass")]
    for path in untouched:
        path.write_text("Other variant bytes")
    quarantine_sync_outputs(str(flow.video), str(source))
    assert not exact.exists()
    assert symlink.is_symlink() and foreign.read_text() == "Foreign bytes"
    assert all(path.read_text() == "Other variant bytes" for path in untouched)
    assert any(path.read_text() == "Owned generated bytes" for path in flow.video.parent.glob("*.bak"))
    publication = SubtitlePublication(str(flow.video), str(source), subtitle_source_version(source))
    try:
        assert not publication.destination_unchanged(symlink)
    finally:
        publication.release()


def test_review_failed_quarantine_retains_bytes_and_destination_revision(upload_flow, monkeypatch):
    from subtitles.tools import subsync_engines

    flow = upload_flow
    source = flow.video.with_suffix(".en.srt")
    source.write_text("Source")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    output.write_text("Prior generated bytes")
    publication = subsync_engines.SubtitlePublication(
        str(flow.video), str(source), subsync_engines.subtitle_source_version(source))
    monkeypatch.setattr(subsync_engines.os, "replace", Mock(side_effect=PermissionError("controlled move rejection")))
    try:
        with pytest.raises(PermissionError, match="move rejection"):
            subsync_engines.quarantine_sync_outputs(str(flow.video), str(source))
        assert output.read_text() == "Prior generated bytes"
        assert publication.destination_unchanged(output)
        assert not list(flow.video.parent.glob(".bazarr-sync-obsolete-*.bak"))
    finally:
        publication.release()


@pytest.fixture
def rejected_quarantine(upload_flow, monkeypatch):
    from subtitles.tools import subsync_engines

    flow = upload_flow
    source = flow.video.with_suffix(".en.srt")
    source.write_text("Previous subtitle")
    output = flow.video.with_suffix(".en.ffsubsync.srt")
    output.write_text("Previous generated output")
    other = flow.video.with_suffix(".hu.ffsubsync.srt")
    other.write_text("Other language output")
    publication = subsync_engines.SubtitlePublication(
        str(flow.video), str(source), subsync_engines.subtitle_source_version(source))
    other_revision = publication.state.revision(other)
    real_replace = subsync_engines.os.replace
    rejected = []

    def replace(src, dst, *args, **kwargs):
        if Path(src) == output and Path(dst).name.startswith(".bazarr-sync-obsolete-"):
            rejected.append(str(src))
            raise PermissionError("controlled quarantine failure after publication")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(subsync_engines.os, "replace", replace)
    yield SimpleNamespace(flow=flow, source=source, output=output, other=other, publication=publication,
                          other_revision=other_revision, rejected=rejected)
    publication.release()


def _assert_reported_quarantine_failure(probe, caplog):
    assert probe.rejected == [str(probe.output)]
    assert not probe.publication.source_unchanged()
    assert probe.publication.state.revision(probe.source) > probe.publication.source_revision
    assert probe.output.read_text() == "Previous generated output"
    assert probe.publication.destination_unchanged(probe.output)
    assert probe.other.read_text() == "Other language output"
    assert probe.publication.state.revision(probe.other) == probe.other_revision
    assert not list(probe.source.parent.glob(".bazarr-sync-obsolete-*.bak"))
    assert any("cleanup failed" in record.message and "may remain available" in record.message
               and str(probe.source) in record.message for record in caplog.records)


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_review_published_upload_is_accounted_when_quarantine_fails(rejected_quarantine, caplog, media_type):
    probe = rejected_quarantine
    flow = probe.flow
    flow.submit(media_type, content="Replacement uploaded text")
    job, thread = flow.start()
    thread.join(3)
    assert not thread.is_alive()
    assert "Replacement uploaded text" in probe.source.read_text()
    assert flow.indexes, "durable upload publication was not indexed"
    assert flow.indexes[0][0] == 7
    assert flow.indexes[0][1][probe.output.name] == "Previous generated output"
    assert len(flow.history) == 1
    assert len(flow.notifications) == 2
    assert job.status == "completed"
    assert len(flow.queue.jobs_pending_queue) == 1
    _assert_reported_quarantine_failure(probe, caplog)


@pytest.mark.parametrize("writer", ["manual", "automatic"])
def test_review_published_provider_subtitle_is_accounted_when_quarantine_fails(
        rejected_quarantine, monkeypatch, caplog, writer):
    from subtitles import manual, download, pool
    from subliminal_patch.subtitle import Subtitle
    from subzero.language import Language

    probe = rejected_quarantine
    flow = probe.flow
    subtitle = Subtitle(Language("eng"), original_format=True)
    subtitle.content = b"1\n00:00:01,000 --> 00:00:02,000\nReplacement provider text\n"
    subtitle.provider_name = "fixture"
    subtitle.matches = set()
    video = Mock(original_path=str(flow.video))
    provider_pool = Mock(providers=["fixture"], discarded_providers=[])
    module = manual if writer == "manual" else download
    monkeypatch.setattr(pool, "_update_pool", lambda *args, **kwargs: False)
    monkeypatch.setattr(module, "get_video", lambda *args, **kwargs: video)
    monkeypatch.setattr(module, "_get_pool", lambda *args, **kwargs: provider_pool)
    monkeypatch.setattr(module, "get_target_folder", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.subliminal, "region", Mock())
    processed = []
    monkeypatch.setattr(module, "process_subtitle", lambda **kwargs: processed.append(kwargs) or "saved")
    if writer == "manual":
        monkeypatch.setattr(manual.subtitle_cache, "get", lambda *args: subtitle)
        monkeypatch.setattr(manual, "download_subtitles", lambda *args: None)
        manual.manual_download_subtitle(str(flow.video), [], False, False, "cached", "fixture", None,
                                        "Video", "movie", True, 1, arr_instance_id=7)
    else:
        monkeypatch.setattr(download, "_get_language_obj", lambda **kwargs: {Language("eng")})
        monkeypatch.setattr(download, "get_profiles_list", lambda **kwargs: {"originalFormat": True})
        monkeypatch.setattr(download, "download_best_subtitles", lambda **kwargs: {video: [subtitle]})
        monkeypatch.setattr(download, "clear_mismatch_for_video", lambda *args, **kwargs: None)
        list(download.generate_subtitles(str(flow.video), [("en", False, False)], [], None,
                                         "Video", "movie", 1, arr_instance_id=7))
    assert "Replacement provider text" in probe.source.read_text()
    assert len(processed) == 1, "durable provider publication was not passed to processing"
    assert processed[0]["subtitle"].storage_path == str(probe.source)
    assert processed[0]["path"] == str(flow.video)
    assert processed[0]["media_type"] == "movie"
    _assert_reported_quarantine_failure(probe, caplog)


@pytest.mark.parametrize("operation", ["delete", "manual-sync"])
def test_review_completed_mutation_keeps_success_when_quarantine_fails(rejected_quarantine, caplog, operation):
    from subtitles import sync

    probe = rejected_quarantine
    flow = probe.flow
    if operation == "delete":
        assert flow.remove("movie") is True
        assert not probe.source.exists()
        assert flow.indexes
    else:
        flow.release_engine.set()
        assert sync.sync_subtitles(str(flow.video), str(probe.source), "en", False, False, 100,
                                   radarr_id=30, force_sync=True, track_job_progress=False,
                                   arr_instance_id=7) is True
        assert "Synced" in probe.source.read_text()
        assert flow.sync_history
    _assert_reported_quarantine_failure(probe, caplog)
