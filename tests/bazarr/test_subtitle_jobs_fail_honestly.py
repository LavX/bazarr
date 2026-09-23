# coding=utf-8
"""Queued subtitle jobs fail when the work fails.

The jobs queue marks a job failed only when the job raises. Manual download,
upload, sync and the series/league combine reported failure by returning a
string, a tuple or False, so the Jobs drawer listed them as completed. These
pin the new contract: the job raises SubtitleJobError with a sentence the user
can act on, the queue files it under failed, and a successful run still returns
what its caller needs.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import app.database  # noqa: F401


# ---------------------------------------------------------------------------
# The queue: a raised SubtitleJobError is a failed job, a return is not.
# ---------------------------------------------------------------------------

@pytest.fixture
def real_queue(monkeypatch):
    from app import jobs_queue as queue_module

    monkeypatch.setattr(queue_module.JobsQueue, '_flush_progress_loop', lambda self: None)
    monkeypatch.setattr(queue_module, 'event_stream', lambda **kwargs: None)
    queue = queue_module.JobsQueue()

    def run(module, func, kwargs):
        job_id = queue.feed_jobs_pending_queue(job_name='Test job', module=module, func=func, kwargs=kwargs,
                                               is_progress=True)
        job = next(job for job in queue.jobs_pending_queue if job.job_id == job_id)
        queue.jobs_pending_queue.remove(job)
        queue.jobs_running_queue.append(job)
        queue._run_job(job)
        return job

    return queue, run


def _episode_rows(count):
    return [SimpleNamespace(sonarrEpisodeId=100 + n, sonarrSeriesId=5, path=f'/tv/Show/e{n}.mkv',
                            arr_instance_id=7, season=1, episode=n, title='Show')
            for n in range(1, count + 1)]


@pytest.fixture
def series_combine(monkeypatch, real_queue):
    from subtitles.tools.combine import batch

    queue, run = real_queue
    monkeypatch.setattr(batch, 'jobs_queue', queue)
    db = Mock()
    monkeypatch.setattr(batch, 'database', db)
    monkeypatch.setattr(batch.path_mappings, 'path_replace_instance', lambda path, owner, kind: f'/mapped{path}')
    progress = []
    real_update = queue.update_job_progress

    def update(**kwargs):
        progress.append(kwargs)
        return real_update(**kwargs)

    monkeypatch.setattr(queue, 'update_job_progress', update)
    return SimpleNamespace(batch=batch, queue=queue, run=run, db=db, progress=progress)


def _combine_result(status, error='', reason='', path=''):
    return SimpleNamespace(status=status, error=error, reason=reason, path=path)


def test_a_series_combine_with_failed_episodes_fails_its_job_with_a_summary(series_combine, monkeypatch):
    flow = series_combine
    flow.db.execute.return_value.all.return_value = _episode_rows(5)
    outcomes = iter([_combine_result('built', path='/a'), _combine_result('failed', error='source unreadable'),
                     _combine_result('skipped', reason='missing hu'), _combine_result('failed', error='disk full'),
                     _combine_result('failed', error='no rule')])
    seen = []

    def combine(**kwargs):
        seen.append(kwargs)
        return next(outcomes)

    monkeypatch.setattr(flow.batch, 'try_combine_for_video', combine)

    job = flow.run('subtitles.tools.combine.batch', 'combine_series_subtitles',
                   {'series_id': 5, 'languages': ['en', 'hu'], 'format': 'srt', 'arr_instance_id': 7})

    assert job.status == 'failed'
    assert len(seen) == 5, 'one failed episode must not end the batch'
    assert seen[0]['video_path'] == '/mapped/tv/Show/e1.mkv'
    assert seen[0]['languages'] == ['en', 'hu'] and seen[0]['arr_instance_id'] == 7
    assert job in flow.queue.jobs_failed_queue
    # Per-episode progress through the queue's own progress channel.
    assert [p['progress_message'] for p in flow.progress][:2] == ['S01E01 (0/5)', 'S01E02 (1/5)']
    assert flow.progress[-1]['progress_value'] == 5


def test_the_series_combine_failure_names_the_first_few_episodes(series_combine, monkeypatch):
    from subtitles.job_errors import SubtitleJobError

    flow = series_combine
    flow.db.execute.return_value.all.return_value = _episode_rows(5)
    outcomes = iter([_combine_result('failed', error=f'reason {n}') for n in range(4)] + [_combine_result('built')])
    monkeypatch.setattr(flow.batch, 'try_combine_for_video', lambda **kwargs: next(outcomes))
    monkeypatch.setattr(flow.batch.jobs_queue, 'update_job_progress', lambda **kwargs: True)

    with pytest.raises(SubtitleJobError) as raised:
        flow.batch.combine_series_subtitles(5, job_id=1)

    message = str(raised.value)
    assert message.startswith('Combine for Show: 4 of 5 episodes failed (built 1, skipped 0, failed 4).')
    assert 'S01E01 (reason 0); S01E02 (reason 1); S01E03 (reason 2); and 1 more' in message


def test_a_clean_series_combine_completes_with_its_summary(series_combine, monkeypatch):
    flow = series_combine
    flow.db.execute.return_value.all.return_value = _episode_rows(2)
    outcomes = iter([_combine_result('built', path='/a'), _combine_result('skipped', reason='missing hu')])
    monkeypatch.setattr(flow.batch, 'try_combine_for_video', lambda **kwargs: next(outcomes))

    job = flow.run('subtitles.tools.combine.batch', 'combine_series_subtitles', {'series_id': 5})

    assert job.status == 'completed'
    assert job.job_returned_value['built'] == 1
    assert job.job_returned_value['skipped'] == 1
    assert job.job_returned_value['failed'] == 0
    assert job.job_name == 'Combined subtitles for Show: built 1, skipped 1, failed 0'


def test_a_series_without_episodes_fails_the_combine_job(series_combine):
    flow = series_combine
    flow.db.execute.return_value.all.return_value = []

    job = flow.run('subtitles.tools.combine.batch', 'combine_series_subtitles', {'series_id': 5})

    assert job.status == 'failed'


def test_a_league_combine_skips_events_without_a_profile_and_fails_on_errors(monkeypatch):
    from subtitles.job_errors import SubtitleJobError
    from subtitles.tools.combine import batch
    from sportarr import identity, profile_hooks, subtitles as sports_subtitles

    db = Mock()
    db.execute.return_value.first.return_value = SimpleNamespace(title='Formula 1')
    db.execute.return_value.all.return_value = [SimpleNamespace(id=1, title='Race 1'),
                                                SimpleNamespace(id=2, title='Race 2'),
                                                SimpleNamespace(id=3, title='Race 3')]
    monkeypatch.setattr(batch, 'database', db)
    monkeypatch.setattr(batch.jobs_queue, 'update_job_progress', lambda **kwargs: True)

    def resolve(session, event_id, owner):
        if event_id == 3:
            raise OSError('recording moved')
        return SimpleNamespace(event_id=event_id, mapped_path=f'/sports/{event_id}.mkv')

    monkeypatch.setattr(identity, 'resolve_event_in_session', resolve)
    monkeypatch.setattr(sports_subtitles, 'candidate_signature', lambda context: 'sig')
    monkeypatch.setattr(profile_hooks, 'capture_profile_operation',
                        lambda context, signature: SimpleNamespace(profile=context.event_id == 1))
    monkeypatch.setattr(batch, 'try_combine_for_video', lambda **kwargs: _combine_result('built', path='/c.srt'))

    with pytest.raises(SubtitleJobError) as raised:
        batch.combine_league_subtitles(9, 4, job_id=1)

    assert str(raised.value) == ('Combine for Formula 1: 1 of 3 events failed (built 1, skipped 1, failed 1). '
                                 'Race 3 (recording moved)')


# ---------------------------------------------------------------------------
# Manual download.
# ---------------------------------------------------------------------------

@pytest.fixture
def manual(monkeypatch):
    from subtitles import manual, pool
    from subtitles.tools import mods

    monkeypatch.setattr(pool, '_update_pool', lambda *args, **kwargs: False)
    monkeypatch.setattr(mods, 'get_subzero_mods', lambda owner: None)
    monkeypatch.setattr(manual, 'get_video', lambda *args, **kwargs: SimpleNamespace(original_path='/m/Film.mkv'))
    monkeypatch.setattr(manual, '_get_pool', lambda *args, **kwargs: None)
    return manual


def _cached_subtitle(content=None, valid=False):
    return SimpleNamespace(language=SimpleNamespace(hi=False, forced=False), provider_name='opensubtitles',
                           content=content, is_valid=lambda: valid, use_original_format=False, mods=None)


def _download(manual):
    return manual.manual_download_subtitle('/m/Film.mkv', 'English', 'False', 'False', 'cache-id', 'opensubtitles',
                                           'None', 'Film', 'movie', False, profile_id=1, job_id=3)


def test_an_expired_search_result_fails_the_download(manual, monkeypatch):
    from subtitles.job_errors import SubtitleJobError

    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: None)
    with pytest.raises(SubtitleJobError, match='search result has expired'):
        _download(manual)


def test_a_throttled_provider_fails_the_download_instead_of_completing(manual, monkeypatch):
    """The pool answers False for a throttled provider and leaves the subtitle
    empty. That used to come back as a string, so the job read completed."""
    from subtitles.job_errors import SubtitleJobError

    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: _cached_subtitle())
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    with pytest.raises(SubtitleJobError) as raised:
        _download(manual)
    assert str(raised.value).startswith('opensubtitles did not return the subtitle. It may be throttled')


def test_a_provider_error_is_the_reason_the_download_failed(manual, monkeypatch):
    from subtitles.job_errors import SubtitleJobError

    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: _cached_subtitle())

    def boom(subtitles, pool):
        raise RuntimeError('HTTP 429 Too Many Requests')

    monkeypatch.setattr(manual, 'download_subtitles', boom)
    with pytest.raises(SubtitleJobError, match='from opensubtitles failed: HTTP 429 Too Many Requests'):
        _download(manual)


def test_a_cancelled_download_is_not_turned_into_a_failure(manual, monkeypatch):
    from app.jobs_queue import JobCancelled

    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: _cached_subtitle())

    def cancelled(subtitles, pool):
        raise JobCancelled('stop')

    monkeypatch.setattr(manual, 'download_subtitles', cancelled)
    with pytest.raises(JobCancelled):
        _download(manual)


def test_a_successful_download_still_returns_the_processed_subtitle(manual, monkeypatch):
    processed = SimpleNamespace(path='/m/Film.mkv', message='English subtitles downloaded.')
    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: _cached_subtitle(content=b'1', valid=True))
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    monkeypatch.setattr(manual, 'subtitle_write_locks', lambda *args: nullcontext())
    monkeypatch.setattr(manual, 'publication_callback', lambda *args: None)
    monkeypatch.setattr(manual, '_save_downloaded_subtitles', lambda *args, **kwargs: ['saved'])
    monkeypatch.setattr(manual, 'clear_mismatch_after_manual_save', lambda *args: None)
    monkeypatch.setattr(manual, '_get_scores', lambda media_type: (0, 120, 0))
    monkeypatch.setattr(manual, 'process_subtitle', lambda **kwargs: processed)

    assert _download(manual) is processed


def test_nothing_written_fails_the_download(manual, monkeypatch):
    from subtitles.job_errors import SubtitleJobError

    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: _cached_subtitle(content=b'1', valid=True))
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    monkeypatch.setattr(manual, 'subtitle_write_locks', lambda *args: nullcontext())
    monkeypatch.setattr(manual, 'publication_callback', lambda *args: None)
    monkeypatch.setattr(manual, '_save_downloaded_subtitles', lambda *args, **kwargs: [])
    with pytest.raises(SubtitleJobError, match='No subtitle file was written'):
        _download(manual)


@pytest.fixture
def movie_wrapper(monkeypatch):
    from subtitles import manual

    jobs = MagicMock()
    monkeypatch.setattr(manual, 'jobs_queue', jobs)
    db = Mock()
    monkeypatch.setattr(manual, 'database', db)
    monkeypatch.setattr(manual, 'get_audio_profile_languages', lambda value: [])
    monkeypatch.setattr(manual.path_mappings, 'path_replace_instance', lambda path, owner, kind: path)
    monkeypatch.setattr(manual.activity, 'register', lambda *args, **kwargs: 'observed')
    published = []
    monkeypatch.setattr(manual.activity, 'note_publication', lambda *args, **kwargs: published.append(kwargs))
    history = []
    monkeypatch.setattr(manual, 'history_log_movie', lambda *args, **kwargs: history.append(args))
    monkeypatch.setattr(manual, 'send_notifications_movie', lambda *args, **kwargs: None)
    monkeypatch.setattr(manual, 'store_subtitles_movie', lambda *args, **kwargs: None)
    row = SimpleNamespace(title='Film', year=2020, path='/m/Film.mkv', sceneName=None, arr_instance_id=2,
                          profileId=1, audio_language='[]')
    db.execute.return_value.first.return_value = row
    return SimpleNamespace(manual=manual, jobs=jobs, db=db, published=published, history=history)


def test_manual_download_is_queued_under_its_standard_label(movie_wrapper):
    movie_wrapper.jobs.add_job_from_function.return_value = 12
    assert movie_wrapper.manual.movie_manually_download_specific_subtitle(9, 'False', 'False', 'False',
                                                                          'opensubtitles', 'cache-id') == 12
    movie_wrapper.jobs.add_job_from_function.assert_called_once_with('Manually downloading Subtitles',
                                                                    is_progress=False)


def test_a_failed_manual_download_raises_and_writes_no_history(movie_wrapper, monkeypatch):
    from subtitles.job_errors import SubtitleJobError

    flow = movie_wrapper

    def throttled(*args, **kwargs):
        raise SubtitleJobError('opensubtitles did not return the subtitle.')

    monkeypatch.setattr(flow.manual, 'manual_download_subtitle', throttled)
    with pytest.raises(SubtitleJobError, match='opensubtitles did not return the subtitle.'):
        flow.manual.movie_manually_download_specific_subtitle(9, 'False', 'False', 'False', 'opensubtitles',
                                                              'cache-id', job_id=4)
    assert flow.history == [] and flow.published == []
    names = [call.kwargs['new_job_name'] for call in flow.jobs.update_job_name.call_args_list]
    assert not any('downloaded' in name for name in names), 'a failed job must not be renamed "downloaded"'


def test_a_movie_gone_from_the_library_fails_the_download(movie_wrapper):
    from subtitles.job_errors import SubtitleJobError

    movie_wrapper.db.execute.return_value.first.return_value = None
    with pytest.raises(SubtitleJobError, match='no longer in the library'):
        movie_wrapper.manual.movie_manually_download_specific_subtitle(9, 'False', 'False', 'False',
                                                                       'opensubtitles', 'cache-id', job_id=4)


def test_a_successful_manual_download_keeps_its_return_and_side_effects(movie_wrapper, monkeypatch):
    flow = movie_wrapper
    result = SimpleNamespace(path='/m/Film.mkv', message='English subtitles downloaded.', language='en')
    monkeypatch.setattr(flow.manual, 'manual_download_subtitle', lambda *args, **kwargs: result)

    assert flow.manual.movie_manually_download_specific_subtitle(9, 'False', 'False', 'False', 'opensubtitles',
                                                                 'cache-id', job_id=4) == ('', 204)
    assert flow.history == [(2, 9, result)]
    assert flow.published[0]['outcome'] == 'success'
    assert flow.jobs.update_job_name.call_args.kwargs['new_job_name'] == \
        'Manually downloaded Subtitles for Film (2020)'


def test_the_sports_route_still_answers_a_failed_download_with_its_reason(monkeypatch):
    """Sports downloads run in the request and answer an OSError as a 409 with
    the sentence, so the job error is translated back at that boundary."""
    from subtitles import manual
    from subtitles.cache import subtitle_cache
    from subtitles.job_errors import SubtitleJobError
    from sportarr import subtitles as sports

    candidate = SimpleNamespace(subtitle=SimpleNamespace(provider_name='p', language=SimpleNamespace(
        hi=False, forced=False), use_original_format=False))
    monkeypatch.setattr(sports, 'resolve_event_in_session', lambda *args: SimpleNamespace(
        mapped_path='/s/e.mkv', profile_id=1, arr_instance_id=4))
    monkeypatch.setattr(subtitle_cache, 'get', lambda key: candidate)
    monkeypatch.setattr(sports, 'validate_candidate', lambda *args, **kwargs: None)
    monkeypatch.setattr(sports, 'database', Mock(get=lambda *args, **kwargs: SimpleNamespace(
        audio_language='[]', sceneName=None, title='Race')))
    monkeypatch.setattr(sports, 'get_audio_profile_languages', lambda value: [])

    def throttled(*args, **kwargs):
        raise SubtitleJobError('p did not return the subtitle.')

    monkeypatch.setattr(manual, 'manual_download_subtitle', throttled)
    with pytest.raises(OSError, match='p did not return the subtitle.'):
        sports.manual_download_sports(5, {'subtitle': 'id', 'provider': 'p'}, 4)


# ---------------------------------------------------------------------------
# Upload.
# ---------------------------------------------------------------------------

def test_upload_is_queued_under_its_standard_label(monkeypatch):
    from subtitles import upload

    jobs = MagicMock()
    monkeypatch.setattr(upload, 'jobs_queue', jobs)
    upload.manual_upload_subtitle('/m/Film.mkv', 'en', False, False, 'movie', None, 'Film.en.srt', '[]',
                                  radarrId=9)
    jobs.add_job_from_function.assert_called_once_with('Uploading Film.en.srt', is_progress=False)


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_an_upload_for_media_gone_from_the_library_fails(monkeypatch, media_type):
    from io import BytesIO

    from languages import get_languages
    from subtitles import processing, upload
    from subtitles.job_errors import SubtitleJobError

    monkeypatch.setattr(get_languages, 'languages_dict', [
        {'code2': 'en', 'code3': 'eng', 'code3b': 'eng', 'name': 'English'},
    ], raising=False)
    monkeypatch.setattr(processing, '_postprocessing_config', lambda kind, owner: (False, '', False, 0))
    db = Mock()
    db.execute.return_value.first.return_value = None
    monkeypatch.setattr(upload, 'database', db)

    with pytest.raises(SubtitleJobError, match='Could not upload Film.en.srt: the .* is no longer in the library'):
        upload.manual_upload_subtitle('/m/Film.mkv', 'en', False, False, media_type, BytesIO(b'1'),
                                      'Film.en.srt', '[]', job_id=6, sonarrEpisodeId=3, radarrId=9)


# ---------------------------------------------------------------------------
# Sync.
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_run(monkeypatch):
    from subtitles import sync
    from subtitles.tools.subsync_engines import OUTPUT_MODE_OVERWRITE, SyncRunResult

    monkeypatch.setattr(sync, 'jobs_queue', MagicMock())
    monkeypatch.setattr(sync, 'publication_callback', lambda *args: (lambda path: None))
    outcome = SimpleNamespace(results=[], raises=None)

    class FakeSubSyncer:
        sync_result = None

        def sync(self, **kwargs):
            if outcome.raises:
                raise outcome.raises
            run = SyncRunResult(source_path=kwargs['srt_path'], output_mode=OUTPUT_MODE_OVERWRITE)
            run.results = outcome.results
            return run

    monkeypatch.setattr(sync, 'SubSyncer', FakeSubSyncer)

    def call(**overrides):
        kwargs = dict(video_path='/m/Film.mkv', srt_path='/m/Film.en.srt', srt_lang='en', forced=False, hi=False,
                      percent_score=0, radarr_id=9, job_id=8, force_sync=True, enabled_engines=['ffsubsync'])
        kwargs.update(overrides)
        return sync.sync_subtitles(**kwargs)

    return SimpleNamespace(call=call, outcome=outcome)


def _engine(status, reason=None, message=None):
    from subtitles.tools.subsync_engines import SyncEngineResult

    return SyncEngineResult(engine='ffsubsync', status=status, reason=reason, message=message,
                            output_path='/m/Film.en.srt' if status == 'success' else None)


def test_a_queued_sync_whose_engines_fail_fails_its_job(sync_run):
    from subtitles.job_errors import SubtitleJobError
    from subtitles.tools.subsync_engines import REASON_ENGINE_FAILED

    sync_run.outcome.results = [_engine('failed', REASON_ENGINE_FAILED, 'ffsubsync crashed')]
    with pytest.raises(SubtitleJobError) as raised:
        sync_run.call()
    assert str(raised.value).startswith('Sync failed: no output from 1 engine.')


def test_a_queued_sync_that_raises_fails_with_the_reason(sync_run):
    from subtitles.job_errors import SubtitleJobError

    sync_run.outcome.raises = RuntimeError('alass: missing binary')
    with pytest.raises(SubtitleJobError, match='Sync failed: alass: missing binary'):
        sync_run.call()


def test_a_sync_inside_another_job_still_answers_false(sync_run):
    """A download or a batch runs sync inside its own job and decides what a
    failed sync means for it, so only the sync's own job raises."""
    from subtitles.tools.subsync_engines import REASON_ENGINE_FAILED

    sync_run.outcome.results = [_engine('failed', REASON_ENGINE_FAILED, 'crashed')]
    assert sync_run.call(owns_job_progress=False) is False
    assert sync_run.call(track_job_progress=False) is False


def test_a_skipped_sync_completes(sync_run):
    from subtitles.tools.subsync_engines import REASON_GENERATED_SOURCE

    sync_run.outcome.results = [_engine('skipped', REASON_GENERATED_SOURCE)]
    assert sync_run.call() is False


def test_a_successful_sync_still_returns_true(sync_run):
    sync_run.outcome.results = [_engine('success')]
    assert sync_run.call() is True
