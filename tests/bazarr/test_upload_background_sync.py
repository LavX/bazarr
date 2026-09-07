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
            sonarrSeriesId=10 if media_type == "series" else None,
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
    assert len(flow.notifications) == 2
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

    def postprocess(command, video_path):
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
    assert len(flow.notifications) == 1
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

    def postprocess(command, video_path):
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
