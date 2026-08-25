# coding=utf-8

"""Per-engine sync outcome reporting.

A sync run used to collapse to a single line: any one engine producing output made
the whole run an unqualified success, and the engines that failed were visible only
as a traceback in System > Logs. These tests pin the vocabulary the runner emits for
each way an engine can fail to produce output, and the sentences the job queue shows
for them.
"""

import logging

import pytest

import app.database  # noqa: F401


def _write(path, content):
    path.write_text(content, encoding='utf-8')


def _keep_all_run(results):
    from subtitles.tools.subsync_engines import OUTPUT_MODE_KEEP_ALL, SyncRunResult

    run = SyncRunResult(source_path='/subs/movie.hu.srt', output_mode=OUTPUT_MODE_KEEP_ALL)
    run.results = results
    return run


def _result(engine, status, reason=None, message=None):
    from subtitles.tools.subsync_engines import SyncEngineResult

    return SyncEngineResult(engine=engine, status=status, reason=reason, message=message)


# --------------------------------------------------------------------------------------
# Runner vocabulary: the four ways an engine can come back without an output.
# --------------------------------------------------------------------------------------


def test_engine_that_declines_its_own_result_is_not_reported_as_a_crash(tmp_path):
    """autosubsync rejecting its own output is normal, and must not read like a bug."""
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        REASON_ENGINE_DECLINED,
        RESULT_FAILED,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        SyncEngineDeclinedError,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def execute(engine, output_path):
        _write(output_path, 'partial')
        raise SyncEngineDeclinedError(engine, 'autosubsync completed but did not meet the quality threshold.')

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['autosubsync'],
        execute_engine=execute,
    )

    declined = result.results[0]
    assert declined.status == RESULT_FAILED
    assert declined.reason == REASON_ENGINE_DECLINED
    assert 'quality threshold' in declined.message
    # The engine's own partial output must not be left behind.
    assert not (tmp_path / 'Movie.en.autosubsync.srt').exists()
    assert subtitle.read_text(encoding='utf-8') == 'original'


def test_declined_engine_is_logged_without_a_traceback(tmp_path, caplog):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        SyncEngineDeclinedError,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def execute(engine, output_path):
        raise SyncEngineDeclinedError(engine, 'did not meet the quality threshold.')

    with caplog.at_level(logging.DEBUG):
        SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
            srt_path=str(subtitle),
            output_mode=OUTPUT_MODE_KEEP_ALL,
            enabled_engines=['autosubsync'],
            execute_engine=execute,
        )

    assert not [record for record in caplog.records if record.exc_info]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_rejected_engine_result_has_its_own_reason(tmp_path):
    """A result the host refuses is neither a crash nor the engine's own verdict."""
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        REASON_RESULT_REJECTED,
        RESULT_FAILED,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        SyncResultRejectedError,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def execute(engine, output_path):
        _write(output_path, 'shifted too far')
        raise SyncResultRejectedError(
            engine, 'ffsubsync aligned the subtitles by 200.000 seconds, more than the 60 second maximum offset.')

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync'],
        execute_engine=execute,
    )

    rejected = result.results[0]
    assert rejected.status == RESULT_FAILED
    assert rejected.reason == REASON_RESULT_REJECTED
    assert 'maximum offset' in rejected.message
    assert not (tmp_path / 'Movie.en.ffsubsync.srt').exists()


def test_unexpected_engine_error_still_reports_engine_failed(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        REASON_ENGINE_FAILED,
        RESULT_FAILED,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def execute(engine, output_path):
        raise RuntimeError('alass failed with exit code 1: could not read the video stream')

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['alass'],
        execute_engine=execute,
    )

    failed = result.results[0]
    assert failed.status == RESULT_FAILED
    assert failed.reason == REASON_ENGINE_FAILED


@pytest.mark.parametrize('error_name', ['SyncEngineDeclinedError', 'SyncResultRejectedError'])
def test_declined_and_rejected_results_still_count_towards_the_failure_threshold(tmp_path, error_name):
    """Reporting-only change: a decline or a rejection must keep costing a strike."""
    import subtitles.tools.subsync_engines as engines_module

    error = getattr(engines_module, error_name)
    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    store = engines_module.InMemorySubsyncFailureStore()

    def execute(engine, output_path):
        raise error(engine, 'nope')

    engines_module.SubsyncEngineRunner(store).run(
        srt_path=str(subtitle),
        output_mode=engines_module.OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['alass'],
        execute_engine=execute,
    )

    assert store.failure_count(str(subtitle), 'alass') == 1


def test_autosubsync_quality_check_raises_a_decline_not_a_generic_error(monkeypatch, tmp_path):
    from subtitles.tools.subsync_engines import SyncEngineDeclinedError
    from subtitles.tools.subsyncer import SubSyncer

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    monkeypatch.setattr('subtitles.tools.subsyncer._autosubsync_model_file', lambda: 'model.bin')
    monkeypatch.setattr('subtitles.tools.subsyncer._run_autosubsync_api', lambda **kwargs: False)

    subsyncer = SubSyncer()
    subsyncer.srtin = str(subtitle)
    subsyncer.reference = None

    with pytest.raises(SyncEngineDeclinedError) as excinfo:
        subsyncer._run_autosubsync_engine(output_path=tmp_path / 'out.srt', video_path='/video.mkv')

    assert excinfo.value.engine == 'autosubsync'
    assert 'quality' in str(excinfo.value)


# --------------------------------------------------------------------------------------
# Job name: how many engines were asked for, how many produced output.
# --------------------------------------------------------------------------------------


def test_partial_keep_all_run_names_the_engine_counts():
    from subtitles.sync import _sync_complete_job_name
    from subtitles.tools.subsync_engines import (
        REASON_ENGINE_DECLINED,
        REASON_MISSING_ENGINE,
        RESULT_FAILED,
        RESULT_SKIPPED,
        RESULT_SUCCESS,
    )

    run = _keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('autosubsync', RESULT_FAILED, REASON_ENGINE_DECLINED, 'did not meet the quality threshold.'),
        _result('alass', RESULT_SKIPPED, REASON_MISSING_ENGINE, 'alass executable not found on PATH'),
    ])

    assert _sync_complete_job_name('/subs/movie.hu.srt', run) == (
        'Generated 1 of 3 sync outputs for /subs/movie.hu.srt'
    )


def test_fully_successful_keep_all_run_reads_exactly_as_it_does_today():
    from subtitles.sync import _sync_complete_job_name
    from subtitles.tools.subsync_engines import RESULT_SUCCESS

    run = _keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_SUCCESS),
    ])

    assert _sync_complete_job_name('/subs/movie.hu.srt', run) == (
        'Generated 2 sync outputs for /subs/movie.hu.srt'
    )


def test_overwrite_run_name_is_unaffected_by_an_earlier_engine_failure():
    from subtitles.sync import _sync_complete_job_name
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_OVERWRITE,
        REASON_RESULT_REJECTED,
        RESULT_FAILED,
        RESULT_SUCCESS,
        SyncRunResult,
    )

    run = SyncRunResult(source_path='/subs/movie.hu.srt', output_mode=OUTPUT_MODE_OVERWRITE)
    run.results = [
        _result('ffsubsync', RESULT_FAILED, REASON_RESULT_REJECTED, 'more than the 60 second maximum offset.'),
        _result('autosubsync', RESULT_SUCCESS),
    ]

    assert _sync_complete_job_name('/subs/movie.hu.srt', run) == (
        'Synced /subs/movie.hu.srt using autosubsync'
    )


# --------------------------------------------------------------------------------------
# Outcome message: the engines that failed, and why, without opening the log.
# --------------------------------------------------------------------------------------


def test_partial_run_message_names_every_engine_that_produced_nothing():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import (
        REASON_ENGINE_DECLINED,
        REASON_MISSING_ENGINE,
        RESULT_FAILED,
        RESULT_SKIPPED,
        RESULT_SUCCESS,
    )

    run = _keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('autosubsync', RESULT_FAILED, REASON_ENGINE_DECLINED, 'did not meet the quality threshold.'),
        _result('alass', RESULT_SKIPPED, REASON_MISSING_ENGINE, 'alass executable not found on PATH'),
    ])

    message = _sync_outcome_message(run)

    assert message.startswith('Sync partially complete: 1 of 3 engines produced output.')
    assert 'Autosubsync' in message
    assert 'ALASS is not installed.' in message


def test_autosubsync_self_rejection_reads_as_normal_behaviour():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import REASON_ENGINE_DECLINED, RESULT_FAILED, RESULT_SUCCESS

    run = _keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('autosubsync', RESULT_FAILED, REASON_ENGINE_DECLINED, 'did not meet the quality threshold.'),
    ])

    message = _sync_outcome_message(run)

    assert 'Autosubsync rejected its own result' in message
    assert 'normal' in message
    # It must not read like a missing dependency or a crash.
    assert 'not installed' not in message
    assert 'failed:' not in message


def test_missing_engine_reads_differently_from_an_engine_that_ran_and_failed():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import (
        REASON_ENGINE_FAILED,
        REASON_MISSING_ENGINE,
        RESULT_FAILED,
        RESULT_SKIPPED,
        RESULT_SUCCESS,
    )

    missing = _sync_outcome_message(_keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_SKIPPED, REASON_MISSING_ENGINE, 'alass executable not found on PATH'),
    ]))
    ran_and_failed = _sync_outcome_message(_keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_FAILED, REASON_ENGINE_FAILED, 'alass exited with code 1: bad stream'),
    ]))

    assert 'ALASS is not installed.' in missing
    assert 'ALASS failed: exited with code 1: bad stream' in ran_and_failed
    assert missing != ran_and_failed


def test_engine_naming_itself_in_its_message_is_not_repeated_after_the_label():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import REASON_ENGINE_FAILED, RESULT_FAILED, RESULT_SUCCESS

    message = _sync_outcome_message(_keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_FAILED, REASON_ENGINE_FAILED, 'alass exited with code 1: bad stream'),
    ]))

    assert 'ALASS failed: exited with code 1: bad stream.' in message
    assert 'alass' not in message


def test_threshold_skip_reads_as_skipped_rather_than_failed():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import REASON_FAILURE_THRESHOLD, RESULT_SKIPPED, RESULT_SUCCESS

    message = _sync_outcome_message(_keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_SKIPPED, REASON_FAILURE_THRESHOLD,
                'alass skipped after 3 consecutive failures.'),
    ]))

    assert 'ALASS was skipped after repeated failures.' in message
    assert 'ALASS failed' not in message


def test_rejected_result_message_explains_the_offset_limit():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import REASON_RESULT_REJECTED, RESULT_FAILED, RESULT_SUCCESS

    message = _sync_outcome_message(_keep_all_run([
        _result('alass', RESULT_SUCCESS),
        _result('ffsubsync', RESULT_FAILED, REASON_RESULT_REJECTED,
                'ffsubsync aligned the subtitles by 200.000 seconds, more than the 60 second maximum offset.'),
    ]))

    assert 'FFsubsync result rejected:' in message
    assert 'more than the 60 second maximum offset.' in message


def test_fully_successful_run_message_is_unchanged():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import RESULT_SUCCESS

    run = _keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_SUCCESS),
    ])

    assert _sync_outcome_message(run) == 'Sync complete'


def test_overwrite_run_message_is_unaffected_by_an_earlier_engine_failure():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_OVERWRITE,
        REASON_ENGINE_FAILED,
        RESULT_FAILED,
        RESULT_SUCCESS,
        SyncRunResult,
    )

    run = SyncRunResult(source_path='/subs/movie.hu.srt', output_mode=OUTPUT_MODE_OVERWRITE)
    run.results = [
        _result('ffsubsync', RESULT_FAILED, REASON_ENGINE_FAILED, 'boom'),
        _result('autosubsync', RESULT_SUCCESS),
    ]

    assert _sync_outcome_message(run) == 'Sync complete'


def test_run_where_every_engine_failed_still_reports_failure_with_counts():
    from subtitles.sync import _sync_complete_job_name, _sync_outcome_message
    from subtitles.tools.subsync_engines import (
        REASON_ENGINE_DECLINED,
        REASON_ENGINE_FAILED,
        REASON_RESULT_REJECTED,
        RESULT_FAILED,
    )

    run = _keep_all_run([
        _result('ffsubsync', RESULT_FAILED, REASON_RESULT_REJECTED, 'more than the 60 second maximum offset.'),
        _result('autosubsync', RESULT_FAILED, REASON_ENGINE_DECLINED, 'did not meet the quality threshold.'),
        _result('alass', RESULT_FAILED, REASON_ENGINE_FAILED, 'alass exited with code 1: bad stream'),
    ])

    assert _sync_complete_job_name('/subs/movie.hu.srt', run) == 'Failed to sync /subs/movie.hu.srt'
    message = _sync_outcome_message(run)
    assert message.startswith('Sync failed: no output from 3 engines.')
    assert 'ALASS failed:' in message


def test_run_where_every_engine_was_skipped_reports_skipped_with_reasons():
    from subtitles.sync import _sync_complete_job_name, _sync_outcome_message
    from subtitles.tools.subsync_engines import REASON_MISSING_ENGINE, RESULT_SKIPPED

    run = _keep_all_run([
        _result('autosubsync', RESULT_SKIPPED, REASON_MISSING_ENGINE, 'autosubsync Python package not installed'),
        _result('alass', RESULT_SKIPPED, REASON_MISSING_ENGINE, 'alass executable not found on PATH'),
    ])

    assert _sync_complete_job_name('/subs/movie.hu.srt', run) == 'Skipped sync for /subs/movie.hu.srt'
    message = _sync_outcome_message(run)
    assert message.startswith('Sync skipped: no engine produced output.')
    assert 'Autosubsync is not installed.' in message
    assert 'ALASS is not installed.' in message


def test_engine_message_is_truncated_so_a_stderr_dump_cannot_flood_the_job_card():
    from subtitles.sync import _sync_outcome_message
    from subtitles.tools.subsync_engines import REASON_ENGINE_FAILED, RESULT_FAILED, RESULT_SUCCESS

    message = _sync_outcome_message(_keep_all_run([
        _result('ffsubsync', RESULT_SUCCESS),
        _result('alass', RESULT_FAILED, REASON_ENGINE_FAILED, 'x' * 5000),
    ]))

    assert len(message) < 500
    assert message.endswith('...')


# --------------------------------------------------------------------------------------
# End to end through sync_subtitles: what the jobs drawer shows.
# --------------------------------------------------------------------------------------


def test_sync_subtitles_reports_a_partial_run_to_the_jobs_queue(mocker):
    from subtitles import sync as sync_module
    from subtitles.tools.subsync_engines import (
        REASON_ENGINE_DECLINED,
        REASON_MISSING_ENGINE,
        RESULT_FAILED,
        RESULT_SKIPPED,
        RESULT_SUCCESS,
        SyncEngineResult,
        SyncRunResult,
    )

    mock_jobs_queue = mocker.patch.object(sync_module, 'jobs_queue')
    sync_result = SyncRunResult(source_path='/subtitle.hu.srt', output_mode='keep_all')
    sync_result.results = [
        SyncEngineResult(engine='ffsubsync', status=RESULT_SUCCESS,
                         output_path='/subtitle.hu.ffsubsync.srt'),
        SyncEngineResult(engine='autosubsync', status=RESULT_FAILED, reason=REASON_ENGINE_DECLINED,
                         message='autosubsync completed but did not meet the quality threshold.'),
        SyncEngineResult(engine='alass', status=RESULT_SKIPPED, reason=REASON_MISSING_ENGINE,
                         message='alass executable not found on PATH'),
    ]

    class FakeSubSyncer:
        def sync(self, **kwargs):
            return sync_result

    mocker.patch.object(sync_module, 'SubSyncer', return_value=FakeSubSyncer())
    mocker.patch.object(sync_module, '_index_keep_all_outputs')

    assert sync_module.sync_subtitles(
        video_path='/video.mkv',
        srt_path='/subtitle.hu.srt',
        srt_lang='hu',
        forced=False,
        hi=False,
        percent_score=0,
        job_id=99,
        force_sync=True,
        output_mode='keep_all',
        enabled_engines=['ffsubsync', 'autosubsync', 'alass'],
    ) is True

    mock_jobs_queue.update_job_name.assert_any_call(
        job_id=99,
        new_job_name='Generated 1 of 3 sync outputs for /subtitle.hu.srt',
    )
    final = [call.kwargs for call in mock_jobs_queue.update_job_progress.call_args_list
             if call.kwargs.get('progress_value') == 'max']
    assert final, 'the run never reported completion'
    outcome = final[-1]['progress_message']
    assert outcome.startswith('Sync partially complete: 1 of 3 engines produced output.')
    assert 'Autosubsync rejected its own result' in outcome
    assert 'ALASS is not installed.' in outcome
