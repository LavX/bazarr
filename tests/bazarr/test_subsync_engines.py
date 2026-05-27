# coding=utf-8

import logging
import inspect
import os
import pytest
import subprocess

import app.database  # noqa: F401


def _write(path, content):
    path.write_text(content, encoding='utf-8')


def test_overwrite_mode_stops_on_first_success(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_OVERWRITE,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        engine_output_path,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    calls = []

    def execute(engine, output_path):
        calls.append(engine)
        if engine != 'ffsubsync':
            raise AssertionError(f'{engine} should not run after first success')
        _write(output_path, 'ffsubsync result')
        return {'offset_seconds': 0.5, 'framerate_scale_factor': 1.0}

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_OVERWRITE,
        enabled_engines=['ffsubsync', 'autosubsync', 'alass'],
        execute_engine=execute,
    )

    assert calls == ['ffsubsync']
    assert subtitle.read_text(encoding='utf-8') == 'ffsubsync result'
    assert not engine_output_path(str(subtitle), 'ffsubsync').exists()
    assert result.success
    assert [item.engine for item in result.successful_results] == ['ffsubsync']


def test_overwrite_mode_preserves_existing_keep_all_outputs(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_OVERWRITE,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        engine_output_path,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    ffsubsync_output = engine_output_path(str(subtitle), 'ffsubsync')
    autosubsync_output = engine_output_path(str(subtitle), 'autosubsync')
    alass_output = engine_output_path(str(subtitle), 'alass')
    _write(ffsubsync_output, 'existing ffsubsync')
    _write(autosubsync_output, 'existing autosubsync')
    _write(alass_output, 'existing alass')
    used_output_paths = []

    def execute(engine, output_path):
        used_output_paths.append(output_path)
        _write(output_path, 'new overwrite result')
        return {}

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_OVERWRITE,
        enabled_engines=['ffsubsync', 'autosubsync', 'alass'],
        execute_engine=execute,
    )

    assert result.success
    assert subtitle.read_text(encoding='utf-8') == 'new overwrite result'
    assert ffsubsync_output.read_text(encoding='utf-8') == 'existing ffsubsync'
    assert autosubsync_output.read_text(encoding='utf-8') == 'existing autosubsync'
    assert alass_output.read_text(encoding='utf-8') == 'existing alass'
    assert used_output_paths[0] != ffsubsync_output


def test_keep_all_mode_runs_all_enabled_engines(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        engine_output_path,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    calls = []

    def execute(engine, output_path):
        calls.append(engine)
        _write(output_path, f'{engine} result')
        return {'offset_seconds': 0, 'framerate_scale_factor': 1.0}

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync', 'autosubsync', 'alass'],
        execute_engine=execute,
    )

    assert calls == ['ffsubsync', 'autosubsync', 'alass']
    assert subtitle.read_text(encoding='utf-8') == 'original'
    assert engine_output_path(str(subtitle), 'ffsubsync').read_text(encoding='utf-8') == 'ffsubsync result'
    assert engine_output_path(str(subtitle), 'autosubsync').read_text(encoding='utf-8') == 'autosubsync result'
    assert engine_output_path(str(subtitle), 'alass').read_text(encoding='utf-8') == 'alass result'
    assert result.success
    assert [item.engine for item in result.successful_results] == ['ffsubsync', 'autosubsync', 'alass']


def test_existing_generated_output_is_skipped_until_force_resync(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        engine_output_path,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    generated = engine_output_path(str(subtitle), 'ffsubsync')
    _write(generated, 'existing')
    os.utime(subtitle, (100, 100))
    os.utime(generated, (200, 200))
    calls = []

    def execute(engine, output_path):
        calls.append(engine)
        _write(output_path, 'new result')
        return {}

    runner = SubsyncEngineRunner(InMemorySubsyncFailureStore())
    result = runner.run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync'],
        execute_engine=execute,
    )

    assert calls == []
    assert generated.read_text(encoding='utf-8') == 'existing'
    assert result.skipped_results[0].reason == 'output_exists'

    forced = runner.run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync'],
        execute_engine=execute,
        force_sync=True,
    )

    assert calls == ['ffsubsync']
    assert generated.read_text(encoding='utf-8') == 'new result'
    assert forced.success


def test_stale_keep_all_output_is_regenerated(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        engine_output_path,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'new source')
    generated = engine_output_path(str(subtitle), 'ffsubsync')
    _write(generated, 'stale output')
    os.utime(generated, (100, 100))
    os.utime(subtitle, (200, 200))
    calls = []

    def execute(engine, output_path):
        calls.append(engine)
        _write(output_path, 'fresh result')
        return {}

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync'],
        execute_engine=execute,
    )

    assert calls == ['ffsubsync']
    assert generated.read_text(encoding='utf-8') == 'fresh result'
    assert result.success


def test_keep_all_output_with_recorded_failure_is_regenerated(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        engine_output_path,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'source')
    generated = engine_output_path(str(subtitle), 'ffsubsync')
    _write(generated, 'failed output')
    os.utime(subtitle, (100, 100))
    os.utime(generated, (200, 200))

    store = InMemorySubsyncFailureStore()
    store.record_failure(str(subtitle), 'ffsubsync', 'previous failure')
    calls = []

    def execute(engine, output_path):
        calls.append(engine)
        _write(output_path, 'recovered result')
        return {}

    result = SubsyncEngineRunner(store).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync'],
        execute_engine=execute,
    )

    assert calls == ['ffsubsync']
    assert generated.read_text(encoding='utf-8') == 'recovered result'
    assert result.success
    assert store.failure_count(str(subtitle), 'ffsubsync') == 0


def test_generated_engine_file_is_not_used_as_source(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
        is_sync_engine_output,
    )

    subtitle = tmp_path / 'Movie.en.ffsubsync.srt'
    _write(subtitle, 'generated')

    def execute(engine, output_path):
        raise AssertionError('generated engine outputs must not be recursively synced')

    result = SubsyncEngineRunner(InMemorySubsyncFailureStore()).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['ffsubsync', 'autosubsync', 'alass'],
        execute_engine=execute,
    )

    assert is_sync_engine_output(str(subtitle))
    assert not result.success
    assert result.skipped_results[0].reason == 'generated_source'


def test_failure_count_skips_after_three_and_resets_after_success(tmp_path):
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_OVERWRITE,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    store = InMemorySubsyncFailureStore()
    runner = SubsyncEngineRunner(store)
    calls = []

    def fail(engine, output_path):
        calls.append(engine)
        raise RuntimeError('boom')

    for _ in range(3):
        result = runner.run(
            srt_path=str(subtitle),
            output_mode=OUTPUT_MODE_OVERWRITE,
            enabled_engines=['ffsubsync'],
            execute_engine=fail,
        )
        assert not result.success

    assert calls == ['ffsubsync', 'ffsubsync', 'ffsubsync']
    assert store.failure_count(str(subtitle), 'ffsubsync') == 3

    skipped = runner.run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_OVERWRITE,
        enabled_engines=['ffsubsync'],
        execute_engine=fail,
    )

    assert calls == ['ffsubsync', 'ffsubsync', 'ffsubsync']
    assert skipped.skipped_results[0].reason == 'failure_threshold'

    def succeed(engine, output_path):
        calls.append(engine)
        _write(output_path, 'recovered')
        return {}

    recovered = runner.run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_OVERWRITE,
        enabled_engines=['ffsubsync'],
        execute_engine=succeed,
        force_sync=True,
    )

    assert recovered.success
    assert subtitle.read_text(encoding='utf-8') == 'recovered'
    assert store.failure_count(str(subtitle), 'ffsubsync') == 0


def test_missing_optional_engine_is_skipped_without_failure_count(tmp_path):
    from subtitles.tools.subsync_engines import (
        MissingSyncEngineError,
        OUTPUT_MODE_KEEP_ALL,
        InMemorySubsyncFailureStore,
        SubsyncEngineRunner,
    )

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    store = InMemorySubsyncFailureStore()

    def missing(engine, output_path):
        raise MissingSyncEngineError(engine, 'not found on PATH')

    result = SubsyncEngineRunner(store).run(
        srt_path=str(subtitle),
        output_mode=OUTPUT_MODE_KEEP_ALL,
        enabled_engines=['alass'],
        execute_engine=missing,
    )

    assert not result.success
    assert result.skipped_results[0].reason == 'missing_engine'
    assert store.failure_count(str(subtitle), 'alass') == 0


def test_external_engine_failure_includes_stderr(monkeypatch, tmp_path):
    from subtitles.tools.subsyncer import SubSyncer

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def fail_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr='missing pkg_resources',
        )

    monkeypatch.setattr('subtitles.tools.subsyncer.shutil.which', lambda executable: f'/usr/bin/{executable}')
    monkeypatch.setattr('subtitles.tools.subsyncer.subprocess.run', fail_run)

    syncer = SubSyncer()
    syncer.srtin = str(subtitle)
    syncer.reference = '/tmp/video.mkv'

    with pytest.raises(RuntimeError, match='missing pkg_resources'):
        syncer._run_external_engine('alass', tmp_path / 'Movie.en.alass.srt', '/tmp/video.mkv')


def test_autosubsync_engine_uses_python_api_without_multiprocessing(monkeypatch, tmp_path):
    from subtitles.tools.subsyncer import SubSyncer

    subtitle = tmp_path / 'Movie.en.srt'
    output = tmp_path / 'Movie.en.autosubsync.srt'
    _write(subtitle, 'original')
    calls = {}

    def synchronize(reference, subtitle_file, output_file, model_file, parallelism=None):
        calls['reference'] = reference
        calls['subtitle_file'] = subtitle_file
        calls['output_file'] = output_file
        calls['model_file'] = model_file
        calls['parallelism'] = parallelism
        _write(output, 'autosubsync result')
        return True

    monkeypatch.setattr('subtitles.tools.subsyncer._autosubsync_model_file', lambda: '/models/trained-model.bin')
    monkeypatch.setattr('subtitles.tools.subsyncer._run_autosubsync_api', synchronize)

    syncer = SubSyncer()
    syncer.srtin = str(subtitle)
    syncer.reference = '/tmp/video.mkv'

    result = syncer._run_external_engine('autosubsync', output, '/tmp/video.mkv')

    assert calls == {
        'reference': '/tmp/video.mkv',
        'subtitle_file': str(subtitle),
        'output_file': str(output),
        'model_file': '/models/trained-model.bin',
        'parallelism': 1,
    }
    assert result['success'] is True
    assert output.read_text(encoding='utf-8') == 'autosubsync result'


def test_successful_sync_without_history_does_not_log_error(monkeypatch, tmp_path, caplog):
    from subtitles.tools import subsyncer as subsyncer_module
    from subtitles.tools.subsync_engines import RESULT_SUCCESS, SyncEngineResult, SyncRunResult

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    class FakeRunner:
        def run(self, srt_path, output_mode, enabled_engines, execute_engine, force_sync=False):
            result = SyncRunResult(source_path=srt_path, output_mode=output_mode)
            result.results = [
                SyncEngineResult(
                    engine='autosubsync',
                    status=RESULT_SUCCESS,
                    output_path=str(tmp_path / 'Movie.en.autosubsync.srt'),
                )
            ]
            return result

    monkeypatch.setattr(subsyncer_module, 'SubsyncEngineRunner', FakeRunner)

    syncer = subsyncer_module.SubSyncer()
    with caplog.at_level(logging.ERROR):
        result = syncer.sync(
            video_path=str(tmp_path / 'Movie.mkv'),
            srt_path=str(subtitle),
            srt_lang='en',
            hi=False,
            forced=False,
            max_offset_seconds='60',
            no_fix_framerate=True,
            gss=True,
            write_history=False,
        )

    assert result.success
    assert 'BAZARR unable to sync subtitles' not in caplog.text


def test_sync_subtitles_queues_manual_sync_as_progress_job(mocker):
    from subtitles import sync as sync_module

    mock_jobs_queue = mocker.patch.object(sync_module, 'jobs_queue')
    mock_jobs_queue.add_job_from_function.return_value = 42

    result = sync_module.sync_subtitles(
        video_path='/video.mkv',
        srt_path='/subtitle.hu.srt',
        srt_lang='hu',
        forced=False,
        hi=False,
        percent_score=0,
        force_sync=True,
        output_mode='keep_all',
        enabled_engines=['ffsubsync', 'autosubsync'],
    )

    assert result is False
    mock_jobs_queue.add_job_from_function.assert_called_once_with(
        'Syncing /subtitle.hu.srt',
        is_progress=True,
        progress_max=2,
    )


def test_sync_subtitles_queue_path_has_only_signature_locals(mocker):
    from subtitles import sync as sync_module

    class IntrospectingJobsQueue:
        def add_job_from_function(self, job_name, is_progress, progress_max=0, wait_for_completion=False):
            caller_frame = inspect.currentframe().f_back
            caller_signature = inspect.signature(
                inspect.getmodule(caller_frame.f_code).__dict__[caller_frame.f_code.co_name]
            )
            caller_signature.bind(**caller_frame.f_locals)
            return 42

    mocker.patch.object(sync_module, 'jobs_queue', IntrospectingJobsQueue())

    result = sync_module.sync_subtitles(
        video_path='/video.mkv',
        srt_path='/subtitle.hu.srt',
        srt_lang='hu',
        forced=False,
        hi=False,
        percent_score=0,
        force_sync=True,
        output_mode='keep_all',
        enabled_engines=['ffsubsync', 'autosubsync'],
    )

    assert result is False


def test_sync_subtitles_does_not_queue_generated_sync_output(mocker):
    from subtitles import sync as sync_module

    mock_jobs_queue = mocker.patch.object(sync_module, 'jobs_queue')

    result = sync_module.sync_subtitles(
        video_path='/video.mkv',
        srt_path='/subtitle.hu.ffsubsync.srt',
        srt_lang='hu',
        forced=False,
        hi=False,
        percent_score=0,
        force_sync=True,
        output_mode='keep_all',
        enabled_engines=['ffsubsync', 'autosubsync'],
    )

    assert result is False
    mock_jobs_queue.add_job_from_function.assert_not_called()


def test_sync_subtitles_reports_engine_progress(mocker):
    from subtitles import sync as sync_module
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        RESULT_SUCCESS,
        SyncEngineResult,
        SyncRunResult,
    )

    mock_jobs_queue = mocker.patch.object(sync_module, 'jobs_queue')
    sync_result = SyncRunResult(source_path='/subtitle.hu.srt', output_mode=OUTPUT_MODE_KEEP_ALL)
    sync_result.results = [
        SyncEngineResult(engine='autosubsync', status=RESULT_SUCCESS, output_path='/subtitle.hu.autosubsync.srt'),
    ]

    class FakeSubSyncer:
        def sync(self, **kwargs):
            kwargs['progress_callback']('Running Autosubsync (1/1)', 0, 1)
            kwargs['progress_callback']('Finished Autosubsync (1/1)', 1, 1)
            return sync_result

    mocker.patch.object(sync_module, 'SubSyncer', return_value=FakeSubSyncer())

    result = sync_module.sync_subtitles(
        video_path='/video.mkv',
        srt_path='/subtitle.hu.srt',
        srt_lang='hu',
        forced=False,
        hi=False,
        percent_score=0,
        job_id=99,
        force_sync=True,
        output_mode='keep_all',
        enabled_engines=['autosubsync'],
    )

    assert result is True
    progress_calls = [call.kwargs for call in mock_jobs_queue.update_job_progress.call_args_list]
    assert {
        'job_id': 99,
        'progress_value': 0,
        'progress_max': 1,
        'progress_message': 'Preparing synchronization',
    } in progress_calls
    assert {
        'job_id': 99,
        'progress_value': 0,
        'progress_max': 1,
        'progress_message': 'Running Autosubsync (1/1)',
    } in progress_calls
    assert {
        'job_id': 99,
        'progress_value': 1,
        'progress_max': 1,
        'progress_message': 'Finished Autosubsync (1/1)',
    } in progress_calls
    assert {
        'job_id': 99,
        'progress_value': 'max',
        'progress_message': 'Sync complete',
    } in progress_calls


def test_sync_subtitles_marks_skipped_progress_complete(mocker):
    from subtitles import sync as sync_module

    mock_jobs_queue = mocker.patch.object(sync_module, 'jobs_queue')

    result = sync_module.sync_subtitles(
        video_path='/video.mkv',
        srt_path='/subtitle.hu.srt',
        srt_lang='hu',
        forced=True,
        hi=False,
        percent_score=0,
        job_id=99,
        force_sync=True,
        enabled_engines=['ffsubsync', 'autosubsync'],
    )

    assert result is False
    progress_calls = [call.kwargs for call in mock_jobs_queue.update_job_progress.call_args_list]
    assert {
        'job_id': 99,
        'progress_value': 'max',
        'progress_message': 'Sync skipped',
    } in progress_calls
    mock_jobs_queue.update_job_name.assert_any_call(
        job_id=99,
        new_job_name='Skipped sync for /subtitle.hu.srt',
    )


def test_subsyncer_reports_engine_progress(monkeypatch, tmp_path):
    from subtitles.tools.subsyncer import SubSyncer

    subtitle = tmp_path / 'Movie.hu.srt'
    _write(subtitle, 'original')
    progress = []

    def fake_engine(self, output_path, **kwargs):
        _write(output_path, 'synced')
        return {}

    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', fake_engine)

    result = SubSyncer().sync(
        video_path=str(tmp_path / 'Movie.mkv'),
        srt_path=str(subtitle),
        srt_lang='hu',
        hi=False,
        forced=False,
        max_offset_seconds='60',
        no_fix_framerate=True,
        gss=True,
        force_sync=True,
        output_mode='keep_all',
        enabled_engines=['ffsubsync'],
        write_history=False,
        progress_callback=lambda message, value, total: progress.append((message, value, total)),
    )

    assert result.success
    assert progress == [
        ('Preparing synchronization', 0, 1),
        ('Running FFsubsync (1/1)', 0, 1),
        ('Finished FFsubsync (1/1)', 1, 1),
    ]


@pytest.mark.parametrize(
    'filename',
    ['Movie.en.ffsubsync.srt', 'Movie.en.autosubsync.srt', 'Movie.en.alass.srt'],
)
def test_sync_engine_output_detection(filename):
    from subtitles.tools.subsync_engines import is_sync_engine_output

    assert is_sync_engine_output(filename)


@pytest.mark.parametrize(
    'filename',
    ['Movie.ffsubsync.Release.en.srt', 'Movie.alass.Source.hu.srt', 'Movie.autosubsync.cut.en.hi.srt'],
)
def test_sync_engine_output_detection_ignores_engine_tokens_in_titles(filename):
    from subtitles.tools.subsync_engines import is_sync_engine_output

    assert not is_sync_engine_output(filename)


def test_sync_engine_outputs_keep_language_modifier():
    from subtitles.indexer.utils import subtitle_language_with_sync_modifier

    assert subtitle_language_with_sync_modifier('en', 'Movie.en.ffsubsync.srt') == 'en:sync-ffsubsync'
    assert subtitle_language_with_sync_modifier('en:hi', 'Movie.en.hi.autosubsync.srt') == 'en:hi:sync-autosubsync'
    assert subtitle_language_with_sync_modifier('en:forced', 'Movie.en.forced.alass.srt') == 'en:forced:sync-alass'
    assert subtitle_language_with_sync_modifier('en', 'Movie.en.srt') == 'en'


def test_add_sync_engine_outputs_indexes_generated_files(tmp_path):
    from subzero.language import Language

    from subtitles.indexer.utils import add_sync_engine_outputs, subtitle_language_with_sync_modifier

    _write(tmp_path / 'Movie.hu.srt', 'original')
    _write(tmp_path / 'Movie.hu.ffsubsync.srt', 'ffsubsync')
    _write(tmp_path / 'Movie.hu.alass.srt', 'alass')

    subtitles = {'Movie.hu.srt': Language.fromietf('hu')}
    result = add_sync_engine_outputs(str(tmp_path), subtitles)

    assert set(result) == {'Movie.hu.srt', 'Movie.hu.ffsubsync.srt', 'Movie.hu.alass.srt'}
    assert subtitle_language_with_sync_modifier(
        str(result['Movie.hu.ffsubsync.srt']),
        'Movie.hu.ffsubsync.srt',
    ) == 'hu:sync-ffsubsync'
    assert subtitle_language_with_sync_modifier(
        str(result['Movie.hu.alass.srt']),
        'Movie.hu.alass.srt',
    ) == 'hu:sync-alass'


def test_keep_all_job_name_does_not_claim_original_was_overwritten():
    from subtitles.sync import _sync_complete_job_name
    from subtitles.tools.subsync_engines import (
        OUTPUT_MODE_KEEP_ALL,
        RESULT_SUCCESS,
        SyncEngineResult,
        SyncRunResult,
    )

    result = SyncRunResult(source_path='/subs/movie.hu.srt', output_mode=OUTPUT_MODE_KEEP_ALL)
    result.results = [
        SyncEngineResult(engine='ffsubsync', status=RESULT_SUCCESS, output_path='/subs/movie.hu.ffsubsync.srt'),
        SyncEngineResult(engine='alass', status=RESULT_SUCCESS, output_path='/subs/movie.hu.alass.srt'),
    ]

    assert _sync_complete_job_name('/subs/movie.hu.srt', result) == (
        'Generated 2 sync outputs for /subs/movie.hu.srt'
    )


def test_normalize_subtitle_language_variant_preserves_sync_modifier():
    from subtitles.indexer.utils import normalize_subtitle_language_variant

    assert normalize_subtitle_language_variant('en:sync-alass') == 'en:sync-alass'
