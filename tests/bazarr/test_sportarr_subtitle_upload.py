# coding=utf-8
"""Uploading a subtitle for a sports event.

Episodes and movies have had an upload route since forever; sports had none, so
a subtitle you already owned could not be handed to Bazarr for a sports event.
"""


import ast
from io import BytesIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library  # noqa: F401
from test_sportarr_manual import manual_library  # noqa: F401


def test_the_upload_route_does_not_shadow_the_index_route():
    """POST /sports/events/<id>/subtitles was already taken by the indexer.

    Registering a second Resource on the same path silently shadowed it and
    broke indexing, which is why upload lives on its own path.
    """
    import inspect

    from api.sports import events, subtitles

    upload = inspect.getsource(subtitles)
    index = inspect.getsource(events)
    assert '"/sports/events/<int:event_id>/subtitles/upload"' in upload
    assert '"/sports/events/<int:event_id>/subtitles")' not in upload
    assert "'/sports/events/<int:event_id>/subtitles'" in index


def test_delete_lives_on_the_resource_that_owns_the_path():
    """Rather than a second Resource registered over the index POST."""
    import inspect

    from api.sports import events

    source = inspect.getsource(events.SportsEventSubtitles)
    assert "def post(self, event_id):" in source
    assert "def delete(self, event_id):" in source


def test_manual_upload_takes_a_sports_event():
    import inspect

    from subtitles import upload

    assert "sportsEventId" in inspect.signature(upload.manual_upload_subtitle).parameters


def test_the_language_profile_is_joined_from_the_league():
    """An event carries no profileId column; the profile lives on the league.
    Selecting it off the event raised AttributeError inside the queued job,
    so the upload returned 204 and then silently did nothing."""
    import inspect

    from sportarr.upload import upload_sports_subtitle

    source = inspect.getsource(upload_sports_subtitle)
    assert "_profile_original_format(context.profile_id)" in source


def test_the_upload_is_recorded_and_the_event_refreshed():
    import inspect

    from sportarr.upload import upload_sports_subtitle

    source = inspect.getsource(upload_sports_subtitle)
    assert "sports_history(session, context, result, action=4)" in source
    assert "_index_sports_outputs(context)" in source


def test_the_upload_notifies_like_series_and_movies_do():
    import inspect

    from sportarr.upload import upload_sports_subtitle

    source = inspect.getsource(upload_sports_subtitle)
    assert "send_notifications_sports" in source
    assert "dont_notify_manual_actions" in source


def test_no_consumer_refresh_is_attempted_for_sports():
    """Sportarr offers only an untargeted library scan and the media servers
    refresh by an imdbId a sports event has not got."""
    import inspect

    from subtitles import upload

    source = inspect.getsource(upload._refresh_upload_consumers)
    sports = source[source.index("if media_type == 'sports':"):source.index("if media_type == 'series':")]
    assert "return" in sports
    assert "notify_sonarr" not in sports
    assert "plex_refresh_item" not in sports


def test_an_invalid_extension_is_rejected_before_any_work():
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles.SportsEventSubtitleUpload.post)
    ext_at = source.index("SUBTITLE_EXTENSIONS")
    upload_at = source.index("manual_upload_subtitle(")
    assert ext_at < upload_at


def test_upload_consumers_for_sports_request_one_rescan(monkeypatch):
    """An upload is a live write: Sportarr gets exactly one whole-library
    rescan request for the owner, and unconfigured media servers are left
    alone."""
    from app.config import settings
    from sportarr import notify as sportarr_notify
    from subtitles import upload

    requested = []
    refreshes = []
    monkeypatch.setattr(sportarr_notify, "notify_rescan", lambda owner: requested.append(owner))
    monkeypatch.setattr(upload, "plex_update_sports_library",
                        lambda: refreshes.append("plex"))
    monkeypatch.setattr(upload, "jellyfin_update_sports_library",
                        lambda: refreshes.append("jellyfin"))
    monkeypatch.setattr(settings.general, "use_plex", True)
    monkeypatch.setattr(settings.general, "use_jellyfin", True)
    monkeypatch.setattr(settings.plex, "sports_library", [])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", [])

    upload._refresh_upload_consumers("sports", None, 7)
    assert requested == [7]
    assert refreshes == []

    monkeypatch.setattr(settings.plex, "sports_library", ["Sports"])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", ["10"])
    upload._refresh_upload_consumers("sports", None, 7)
    assert requested == [7, 7]
    assert refreshes == ["plex", "jellyfin"]


@pytest.fixture
def upload_library(manual_library, monkeypatch):  # noqa: F811
    from app.config import settings
    from app import event_handler
    from sportarr import history, workflows, upload as sports_upload
    from subtitles import upload

    _, session, folder = manual_library
    monkeypatch.setattr(upload, 'database', session)
    monkeypatch.setattr(upload, 'event_stream', lambda **kwargs: None)
    monkeypatch.setattr(event_handler, 'event_stream', lambda **kwargs: None)
    monkeypatch.setattr(history, 'database', session)
    monkeypatch.setattr(workflows, 'database', session)
    monkeypatch.setattr(workflows.jobs_queue, 'update_job_progress', lambda *a, **k: None)
    monkeypatch.setattr(settings.general, 'dont_notify_manual_actions', False)
    queued, published, notifications = [], [], []
    monkeypatch.setattr(upload.jobs_queue, 'feed_jobs_pending_queue', lambda **kwargs: queued.append(kwargs) or 99)
    monkeypatch.setattr(upload, 'publication_callback',
                        lambda kind, path, operation, owner: lambda output:
                        published.append((kind, path, operation, owner, str(output))))
    monkeypatch.setattr(sports_upload, 'send_notifications_sports',
                        lambda event, message, **kwargs: notifications.append(('user', event, kwargs)))
    monkeypatch.setattr(upload, '_refresh_upload_consumers',
                        lambda kind, metadata, owner: notifications.append(('consumer', kind, owner)))
    def submit():
        upload.manual_upload_subtitle(
            path=str(folder / '1/event.mkv'), language='en', forced=False, hi=False,
            media_type='sports', subtitle=BytesIO(b'1\n00:00:00,000 --> 00:00:01,000\nUploaded sports subtitle.\n'),
            filename='manual.srt', audio_language=[], arr_instance_id=1, sportsEventId=61)
        return dict(queued[-1]['kwargs'], job_id=99)
    return session, folder, submit, published, notifications


def test_queued_upload_resolves_the_current_owned_event_when_it_starts(upload_library):
    from app.database import TableSportsEvents, TableHistorySports
    from subtitles.upload import manual_upload_subtitle

    session, folder, submit, published, notifications = upload_library
    kwargs = submit()
    moved = folder / '1/moved.mkv'
    moved.write_bytes((folder / '1/event.mkv').read_bytes())
    session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 61)
                    .values(path='/sports/moved.mkv', file_id=999))
    session.commit()
    manual_upload_subtitle(**kwargs)
    assert not (folder / '1/event.en.srt').exists()
    assert 'Uploaded sports subtitle.' in moved.with_suffix('.en.srt').read_text()
    assert published == [('sports', str(moved), 'upload', 1, str(moved.with_suffix('.en.srt')))]
    history = session.execute(sa.select(TableHistorySports).where(TableHistorySports.action == 4)).scalar_one()
    assert (history.event_id, history.arr_instance_id, history.video_path, history.subtitles_path) == (
        61, 1, '/sports/moved.mkv', '/sports/moved.en.srt')
    assert any(item[1] == '/sports/moved.en.srt' for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles))
    assert session.get(TableSportsEvents, 62).subtitles == '[]'
    assert any(item[0] == 'user' for item in notifications)
    assert ('consumer', 'sports', 1) in notifications


@pytest.mark.parametrize('change', ['event', 'mapping', 'video', 'disabled'])
def test_upload_rejects_owned_changes_during_preparation(upload_library, monkeypatch, change):
    from app.database import TableSportsEvents, TableArrInstances, TableHistorySports
    from subtitles import upload

    session, folder, submit, published, notifications = upload_library
    kwargs = submit()
    real_save = upload.save_subtitles
    def change_then_save(*args, **kwargs):
        if change == 'event':
            session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 61).values(file_id=999))
        elif change == 'mapping':
            session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(path_mappings='[]'))
        elif change == 'disabled':
            session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0))
        else:
            (folder / '1/event.mkv').write_bytes(b'replaced video')
        session.commit()
        return real_save(*args, **kwargs)
    monkeypatch.setattr(upload, 'save_subtitles', change_then_save)
    try:
        upload.manual_upload_subtitle(**kwargs)
    except (ValueError, OSError):
        pass
    assert not (folder / '1/event.en.srt').exists()
    assert not published and not notifications
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []
    assert not list(folder.rglob('.bazarr-write-*'))


def test_upload_history_and_consumers_survive_index_failure(upload_library, monkeypatch):
    from app.database import TableHistorySports
    from subtitles import sync, upload

    session, folder, submit, _, notifications = upload_library
    def fail(context):
        raise RuntimeError('index unavailable')
    monkeypatch.setattr(sync, '_index_sports_outputs', fail)
    result = upload.manual_upload_subtitle(**submit())
    assert result['published'] is True and 'index' in result['message']
    assert session.execute(sa.select(TableHistorySports)).scalar_one().action == 4
    assert (folder / '1/event.en.srt').is_file()
    assert ('consumer', 'sports', 1) in notifications
    assert any(item[0] == 'user' for item in notifications)


def test_upload_cancellation_during_preparation_preserves_destination(upload_library, monkeypatch):
    from app.database import TableHistorySports
    from app.jobs_queue import JobCancelled
    from sportarr.workflows import SportsJobSignal
    from subtitles import upload

    session, folder, submit, published, notifications = upload_library
    destination = folder / '1/event.en.srt'
    destination.write_text('Existing subtitle')
    kwargs = submit()
    real_save = upload.save_subtitles
    def cancel_then_save(*args, **kwargs):
        def cancelled(self):
            raise JobCancelled('cancelled')
        monkeypatch.setattr(SportsJobSignal, 'is_set', cancelled)
        return real_save(*args, **kwargs)
    monkeypatch.setattr(upload, 'save_subtitles', cancel_then_save)
    with pytest.raises(JobCancelled):
        upload.manual_upload_subtitle(**kwargs)
    assert destination.read_text() == 'Existing subtitle'
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []
    assert not published and not notifications
    assert not list(folder.rglob('.bazarr-write-*'))


def test_uploaded_auto_sync_keeps_its_owned_context_after_indexing(upload_library, monkeypatch):
    from app.config import settings
    from app.database import TableHistorySports
    from subtitles import upload, sync
    from subtitles.tools import subsyncer, subsync_engines

    session, folder, submit, _, _ = upload_library
    monkeypatch.setattr(settings.subsync, 'use_subsync', True)
    monkeypatch.setattr(settings.subsync, 'use_subsync_movie_threshold', False)
    monkeypatch.setattr(settings.subsync, 'enabled_engines', ['ffsubsync'])
    monkeypatch.setattr(settings.subsync, 'debug', False)
    kwargs = submit()
    queued = []
    monkeypatch.setattr(sync.jobs_queue, 'feed_jobs_pending_queue', lambda **kwargs: queued.append(kwargs) or 101)
    upload.manual_upload_subtitle(**kwargs)
    arguments = queued[-1]['kwargs']
    assert arguments.get('context').event_id == 61
    def engine(self, output_path, **kwargs):
        Path(output_path).write_text('1\n00:00:00,200 --> 00:00:01,200\nUploaded then synced.\n')
        return {'offset_seconds': 0.2, 'framerate_scale_factor': 1}
    monkeypatch.setattr(subsyncer.SubSyncer, '_run_ffsubsync_engine', engine)
    monkeypatch.setattr(subsyncer, 'SubsyncEngineRunner',
                        lambda: subsync_engines.SubsyncEngineRunner(subsync_engines.InMemorySubsyncFailureStore()))
    assert sync.sync_subtitles(**dict(arguments, job_id=101))
    assert 'Uploaded then synced.' in (folder / '1/event.en.srt').read_text()
    assert sorted(session.execute(sa.select(TableHistorySports.action)).scalars()) == [4, 5]


def test_upload_postprocessing_runs_on_a_private_copy_and_records_final_bytes(upload_library, monkeypatch):
    import shlex
    from app.database import TableHistorySports
    from sportarr import upload as sports_upload
    from subtitles import post_processing, upload

    session, folder, submit, _, _ = upload_library
    kwargs = submit()
    monkeypatch.setattr(sports_upload, '_postprocessing_config', lambda *args: (True, '{{subtitles}}', False, 0))
    destinations = []
    def command(command, video):
        destination = Path(shlex.split(command)[0])
        destinations.append(destination)
        assert destination != folder / '1/event.en.srt'
        assert 'Uploaded sports subtitle.' in (folder / '1/event.en.srt').read_text()
        destination.write_text('1\n00:00:00,000 --> 00:00:01,000\nPostprocessed upload.\n')
    monkeypatch.setattr(post_processing, '_postprocessing_locked', command)
    upload.manual_upload_subtitle(**kwargs)
    assert 'Postprocessed upload.' in (folder / '1/event.en.srt').read_text()
    assert destinations and not any(path.exists() for path in destinations)
    assert session.execute(sa.select(TableHistorySports)).scalar_one().subtitles_path == '/sports/event.en.srt'


def test_upload_publication_notification_failure_preserves_record_and_refresh(upload_library, monkeypatch):
    from app.database import TableHistorySports, TableSportsEvents
    from subtitles import upload

    session, folder, submit, _, notifications = upload_library
    def fail(path):
        raise RuntimeError('publication receiver unavailable')
    monkeypatch.setattr(upload, 'publication_callback', lambda *args: fail)
    upload.manual_upload_subtitle(**submit())
    assert session.execute(sa.select(TableHistorySports)).scalar_one().action == 4
    assert any(item[1] == '/sports/event.en.srt' for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles))
    assert ('consumer', 'sports', 1) in notifications


_NEWER_UPLOAD = b'1\n00:00:00,000 --> 00:00:01,000\nNewer user subtitle.\n'


def _replace_from_another_writer(video, output):
    from threading import Thread
    from subtitles.tools.subsync_engines import write_subtitle_file
    errors = []
    def write():
        try:
            write_subtitle_file(video, output, _NEWER_UPLOAD)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=write)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), 'Replacement could not acquire the subtitle coordinator'
    assert not errors


def test_later_upload_is_not_adopted_as_the_earlier_upload_source(upload_library, monkeypatch):
    from app.database import TableHistorySports
    from subtitles import upload
    session, folder, submit, _, _ = upload_library
    output, video = str(folder / '1/event.en.srt'), str(folder / '1/event.mkv')
    real_save = upload.save_subtitles
    captured = []
    def save_then_other_writer(*args, **kwargs):
        result = real_save(*args, **kwargs)
        _replace_from_another_writer(video, output)
        return result
    def sync(**kwargs):
        captured.append(kwargs['source_version'].source_unchanged())
        kwargs['source_version'].release()
        return False
    monkeypatch.setattr(upload, 'save_subtitles', save_then_other_writer)
    monkeypatch.setattr(upload, 'sync_subtitles', sync)
    try:
        upload.manual_upload_subtitle(**submit())
    except ValueError:
        pass
    assert True not in captured, 'The earlier upload authorized syncing the later writer output'
    assert Path(output).read_bytes() == _NEWER_UPLOAD
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []


@pytest.mark.parametrize('phase', ['input', 'replacement', 'capture'])
def test_upload_postprocessing_preserves_a_later_writer(upload_library, monkeypatch, phase):
    import shlex
    from app.database import TableHistorySports
    from sportarr import upload as sports_upload
    from subtitles import upload, post_processing
    session, folder, submit, published, _ = upload_library
    output, video = str(folder / '1/event.en.srt'), str(folder / '1/event.mkv')
    processed = []
    def configure(*args):
        if phase == 'input':
            _replace_from_another_writer(video, output)
        return True, '{{subtitles}}', False, 0
    def command(command, video_path):
        temporary = Path(shlex.split(command)[0])
        processed.append(temporary.read_bytes())
        if phase == 'replacement':
            _replace_from_another_writer(video, output)
        temporary.write_bytes(b'1\n00:00:00,000 --> 00:00:01,000\nPostprocessed subtitle.\n')
    real_postprocess = upload.postprocessing
    def postprocess(*args, **kwargs):
        result = real_postprocess(*args, **kwargs)
        if phase == 'capture':
            _replace_from_another_writer(video, output)
        return result
    monkeypatch.setattr(sports_upload, '_postprocessing_config', configure)
    monkeypatch.setattr(post_processing, '_postprocessing_locked', command)
    monkeypatch.setattr(upload, 'postprocessing', postprocess)
    try:
        upload.manual_upload_subtitle(**submit())
    except ValueError:
        pass
    assert Path(output).read_bytes() == _NEWER_UPLOAD
    assert _NEWER_UPLOAD not in processed, 'Postprocessing adopted another writer input'
    assert len(published) == (2 if phase == 'capture' else 1)
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []
    assert not list(folder.rglob('.bazarr-write-*'))


def test_stale_queued_upload_sync_releases_its_publication(upload_library, monkeypatch):
    from app.config import settings
    from app.database import TableSportsEvents
    from subtitles import upload, sync
    session, _, submit, _, _ = upload_library
    monkeypatch.setattr(settings.subsync, 'use_subsync', True)
    kwargs = submit()
    queued = []
    monkeypatch.setattr(sync.jobs_queue, 'feed_jobs_pending_queue', lambda **kwargs: queued.append(kwargs) or 101)
    upload.manual_upload_subtitle(**kwargs)
    arguments = queued[-1]['kwargs']
    publication = arguments['source_version']
    session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 61).values(file_id=999))
    session.commit()
    with pytest.raises(ValueError, match='Sports event changed'):
        sync.sync_subtitles(**dict(arguments, job_id=101))
    assert publication.state is None, 'Failed queued job retained the media coordinator'


@pytest.mark.parametrize('observer_fails', [False, True])
def test_final_postprocessed_upload_bytes_have_a_publication_notification(upload_library, monkeypatch, observer_fails):
    import shlex
    from threading import Thread
    from app.database import TableHistorySports, TableSportsEvents
    from sportarr import upload as sports_upload
    from subtitles import upload, post_processing
    from subtitles.tools.subsync_engines import subtitle_write_locks
    session, folder, submit, _, _ = upload_library
    observed, callbacks, blocked, readers = [], [], [], []
    def callback(kind, video, operation, owner):
        callbacks.append((kind, operation, owner))
        def observe(output):
            def read():
                with subtitle_write_locks(video, output):
                    observed.append(Path(output).read_bytes())
            reader = Thread(target=read)
            readers.append(reader)
            reader.start()
            reader.join(1)
            blocked.append(reader.is_alive())
            if observer_fails and len(readers) == 2:
                raise RuntimeError('Final publication observer unavailable')
        return observe
    monkeypatch.setattr(upload, 'publication_callback', callback)
    monkeypatch.setattr(sports_upload, '_postprocessing_config', lambda *args: (True, '{{subtitles}}', False, 0))
    def command(command, video):
        Path(shlex.split(command)[0]).write_bytes(b'1\n00:00:00,000 --> 00:00:01,000\nPostprocessed final bytes.\n')
    monkeypatch.setattr(post_processing, '_postprocessing_locked', command)
    upload.manual_upload_subtitle(**submit())
    for reader in readers:
        reader.join(5)
    assert blocked == [False, False], 'Publication observers ran under the writer coordinator'
    assert callbacks == [('sports', 'upload', 1), ('sports', 'upload', 1)]
    assert len(observed) == 2
    assert observed[-1] == (folder / '1/event.en.srt').read_bytes()
    assert session.execute(sa.select(TableHistorySports)).scalar_one().action == 4
    assert any(item[1] == '/sports/event.en.srt' for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles))


@pytest.mark.parametrize('postprocess', [False, True])
def test_upload_chmod_preserves_the_exact_sync_publication(upload_library, monkeypatch, postprocess):
    import shlex
    import stat
    from app.config import settings
    from sportarr import upload as sports_upload
    from subtitles import upload, post_processing
    _, folder, submit, _, _ = upload_library
    monkeypatch.setattr(settings.general, 'chmod_enabled', True)
    monkeypatch.setattr(settings.general, 'chmod', '0640')
    monkeypatch.setattr(sports_upload, '_postprocessing_config',
                        lambda *args: (postprocess, '{{subtitles}}', False, 0))
    monkeypatch.setattr(post_processing, '_postprocessing_locked',
                        lambda command, video: Path(shlex.split(command)[0]).write_bytes(_NEWER_UPLOAD))
    captured = []
    def sync(**kwargs):
        publication = kwargs['source_version']
        captured.append(publication.source_unchanged())
        publication.release()
        return False
    monkeypatch.setattr(upload, 'sync_subtitles', sync)
    upload.manual_upload_subtitle(**submit())
    assert captured == [True]
    assert stat.S_IMODE((folder / '1/event.en.srt').stat().st_mode) == 0o640


def test_upload_releases_captured_publications_when_the_saver_raises(upload_library, monkeypatch):
    from sportarr import upload as sports_upload
    from subtitles import upload
    _, _, submit, _, _ = upload_library
    publications = []
    real_publication = sports_upload.SubtitlePublication
    real_save = upload.save_subtitles
    def capture(*args):
        publication = real_publication(*args)
        publications.append(publication)
        return publication
    def save_then_fail(*args, **kwargs):
        real_save(*args, **kwargs)
        raise OSError('Saver failed after publication')
    monkeypatch.setattr(sports_upload, 'SubtitlePublication', capture)
    monkeypatch.setattr(upload, 'save_subtitles', save_then_fail)
    with pytest.raises(OSError, match='Saver failed'):
        upload.manual_upload_subtitle(**submit())
    assert len(publications) == 1
    assert publications[0].state is None
