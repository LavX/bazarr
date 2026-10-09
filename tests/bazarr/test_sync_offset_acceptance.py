# coding=utf-8
"""The maximum sync offset is an acceptance threshold, not a search window.

The setting used to be handed to ffsubsync as ``--max-offset-seconds``, which makes
the engine mask every candidate alignment outside the window *after* computing the
full correlation, then return the best of whatever is left. A subtitle whose real
offset sits outside the window therefore came back with a plausible looking but wrong
in-window offset, reported as a success, and in overwrite mode it replaced the original
and short-circuited the rest of the engine chain.

The engine now searches unconstrained and the host validates the result: an engine that
reports failure is rejected even when it wrote an output file, and an alignment whose
absolute offset exceeds the configured maximum is rejected too. Both rejections raise,
so the runner deletes the engine output, leaves the original subtitle alone and moves on
to the next engine.
"""

import pytest

import app.database  # noqa: F401


def _write(path, content):
    path.write_text(content, encoding='utf-8')


@pytest.fixture
def in_memory_runner(monkeypatch):
    """Run the real SubsyncEngineRunner, but keep its failure bookkeeping in memory."""
    from subtitles.tools import subsyncer as subsyncer_module
    from subtitles.tools.subsync_engines import InMemorySubsyncFailureStore, SubsyncEngineRunner

    monkeypatch.setattr(
        subsyncer_module,
        'SubsyncEngineRunner',
        lambda: SubsyncEngineRunner(InMemorySubsyncFailureStore()),
    )


def _sync(subtitle, tmp_path, enabled_engines, max_offset_seconds='60', output_mode='overwrite'):
    from subtitles.tools.subsyncer import SubSyncer

    return SubSyncer().sync(
        video_path=str(tmp_path / 'Movie.mkv'),
        srt_path=str(subtitle),
        srt_lang='en',
        hi=False,
        forced=False,
        max_offset_seconds=max_offset_seconds,
        no_fix_framerate=True,
        gss=False,
        force_sync=True,
        output_mode=output_mode,
        enabled_engines=enabled_engines,
        write_history=False,
    )


def test_ffsubsync_searches_without_an_offset_window(monkeypatch):
    """The configured maximum must not narrow the engine's search.

    ffsubsync 0.5.0 masks out-of-window candidates in MaxScoreAligner.transform, i.e.
    after the correlation is computed, so an unconstrained search costs nothing and is
    the only way to learn the offset the engine would really have chosen.
    """
    from subtitles.tools.subsync_engines import UNCONSTRAINED_MAX_OFFSET_SECONDS
    from subtitles.tools.subsyncer import SubSyncer

    syncer = SubSyncer()
    monkeypatch.setattr(SubSyncer, '_ensure_ffmpeg_path', lambda self: '/usr/bin')

    args = syncer._build_ffsubsync_args(
        output_path='/tmp/out.srt',
        no_fix_framerate=True,
        gss=False,
    )

    assert args.max_offset_seconds == UNCONSTRAINED_MAX_OFFSET_SECONDS


@pytest.mark.parametrize('debug', [False, True], ids=['normal', 'subsync debug'])
def test_ffsubsync_keeps_a_log_file_only_for_a_subsync_debug_test_case(monkeypatch, debug):
    """ffsubsync appends to <folder>/ffsubsync.log on every run it is given a folder,
    and nothing rotates that file. Only the debug test case archive needs it."""
    from app.config import settings
    from subtitles.tools.subsyncer import SubSyncer

    monkeypatch.setattr(settings.subsync, 'debug', debug)
    monkeypatch.setattr(SubSyncer, '_ensure_ffmpeg_path', lambda self: '/usr/bin')
    syncer = SubSyncer()
    syncer.reference, syncer.srtin = '/media/Movie.mkv', '/media/Movie.en.srt'

    args = syncer._build_ffsubsync_args(output_path='/media/Movie.en.synced.srt', no_fix_framerate=True, gss=False,
                                        log_dir_path='/tmp/one-run' if debug else None)

    assert args.log_dir_path == ('/tmp/one-run' if debug else None)
    assert args.make_test_case is debug


@pytest.mark.parametrize('outcome', ['returns', 'raises'])
def test_a_subsync_debug_run_logs_into_a_folder_of_its_own_that_goes_away(monkeypatch, tmp_path, outcome):
    """The test case archive then holds this run's log alone, and nothing is left
    behind to grow in the log folder."""
    import os

    import ffsubsync.ffsubsync
    from app.config import settings
    from app.get_args import args as bazarr_args
    from subtitles.tools.subsyncer import SubSyncer

    monkeypatch.setattr(settings.subsync, 'debug', True)
    monkeypatch.setattr(SubSyncer, '_ensure_ffmpeg_path', lambda self: '/usr/bin')
    seen = {}

    def fake_run(parsed):
        seen['folder'] = parsed.log_dir_path
        seen['existed'] = os.path.isdir(parsed.log_dir_path)
        with open(os.path.join(parsed.log_dir_path, 'ffsubsync.log'), 'w') as log:
            log.write('this run only\n')
        if outcome == 'raises':
            raise RuntimeError('ffmpeg went away')
        return {'retval': 0, 'sync_was_successful': True, 'offset_seconds': 0.0, 'framerate_scale_factor': 1.0}

    monkeypatch.setattr(ffsubsync.ffsubsync, 'run', fake_run)
    syncer = SubSyncer()
    syncer.reference, syncer.srtin = '/media/Movie.mkv', '/media/Movie.en.srt'

    if outcome == 'raises':
        with pytest.raises(RuntimeError):
            syncer._run_ffsubsync_engine(output_path=str(tmp_path / 'out.srt'), no_fix_framerate=True, gss=False)
    else:
        syncer._run_ffsubsync_engine(output_path=str(tmp_path / 'out.srt'), no_fix_framerate=True, gss=False)

    assert seen['existed'] is True
    assert os.path.realpath(seen['folder']) != os.path.realpath(os.path.join(bazarr_args.config_dir, 'log'))
    assert not os.path.exists(seen['folder'])


def test_a_normal_run_gives_ffsubsync_no_log_folder(monkeypatch, tmp_path):
    import ffsubsync.ffsubsync
    from app.config import settings
    from subtitles.tools.subsyncer import SubSyncer

    monkeypatch.setattr(settings.subsync, 'debug', False)
    monkeypatch.setattr(SubSyncer, '_ensure_ffmpeg_path', lambda self: '/usr/bin')
    seen = []
    monkeypatch.setattr(ffsubsync.ffsubsync, 'run', lambda parsed: seen.append(parsed) or {'retval': 0})
    syncer = SubSyncer()
    syncer.reference, syncer.srtin = '/media/Movie.mkv', '/media/Movie.en.srt'

    syncer._run_ffsubsync_engine(output_path=str(tmp_path / 'out.srt'), no_fix_framerate=True, gss=False)

    assert seen[0].log_dir_path is None
    assert seen[0].make_test_case is False


@pytest.mark.parametrize('first_read', [False, True], ids=['turned on mid-run', 'turned off mid-run'])
def test_a_test_case_and_its_log_folder_come_from_one_reading_of_the_setting(monkeypatch, tmp_path, first_read):
    """A test case run without a folder of its own writes ffsubsync.log into the
    process's working directory, where parallel syncs share one file. The setting
    can change while a sync starts, so one run must not read it twice."""
    import itertools
    from types import SimpleNamespace

    import ffsubsync.ffsubsync
    from subtitles.tools import subsyncer
    from subtitles.tools.subsyncer import SubSyncer

    monkeypatch.setattr(SubSyncer, '_ensure_ffmpeg_path', lambda self: '/usr/bin')
    syncer = SubSyncer()
    syncer.reference, syncer.srtin = '/media/Movie.mkv', '/media/Movie.en.srt'
    readings = itertools.cycle([first_read, not first_read])

    class ChangingSubsync(SimpleNamespace):
        @property
        def debug(self):
            return next(readings)

    monkeypatch.setattr(subsyncer, 'settings', SimpleNamespace(subsync=ChangingSubsync(
        force_audio=False, use_original_language=False, auto_use_original_language=False)))
    seen = []
    monkeypatch.setattr(ffsubsync.ffsubsync, 'run', lambda parsed: seen.append(parsed) or {'retval': 0})

    syncer._run_ffsubsync_engine(output_path=str(tmp_path / 'out.srt'), no_fix_framerate=True, gss=False)

    assert seen[0].make_test_case is first_read
    assert (seen[0].log_dir_path is not None) is first_read


def test_offset_beyond_the_maximum_is_rejected_and_the_next_engine_runs(monkeypatch, in_memory_runner, tmp_path):
    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')
    calls = []

    def fake_ffsubsync(self, output_path, **kwargs):
        calls.append('ffsubsync')
        _write(output_path, 'badly synced')
        return {'retval': 0, 'sync_was_successful': True, 'offset_seconds': -200.0,
                'framerate_scale_factor': 1.0}

    def fake_external(self, engine, output_path, video_path):
        calls.append(engine)
        _write(output_path, f'{engine} result')
        return {'returncode': 0}

    from subtitles.tools.subsyncer import SubSyncer
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', fake_ffsubsync)
    monkeypatch.setattr(SubSyncer, '_run_external_engine', fake_external)

    result = _sync(subtitle, tmp_path, ['ffsubsync', 'autosubsync'])

    assert calls == ['ffsubsync', 'autosubsync']
    assert [item.engine for item in result.failed_results] == ['ffsubsync']
    assert '200.0' in result.failed_results[0].message
    assert [item.engine for item in result.successful_results] == ['autosubsync']
    assert subtitle.read_text(encoding='utf-8') == 'autosubsync result'


def test_engine_reported_failure_is_rejected_even_with_an_output_file(monkeypatch, in_memory_runner, tmp_path):
    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def fake_ffsubsync(self, output_path, **kwargs):
        _write(output_path, 'unsynced passthrough')
        return {'retval': 0, 'sync_was_successful': False, 'offset_seconds': 1.5,
                'framerate_scale_factor': 1.0}

    from subtitles.tools.subsyncer import SubSyncer
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', fake_ffsubsync)

    result = _sync(subtitle, tmp_path, ['ffsubsync'])

    assert not result.success
    assert [item.engine for item in result.failed_results] == ['ffsubsync']
    assert subtitle.read_text(encoding='utf-8') == 'original'


def test_negative_offset_within_the_maximum_is_accepted(monkeypatch, in_memory_runner, tmp_path):
    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def fake_ffsubsync(self, output_path, **kwargs):
        _write(output_path, 'well synced')
        return {'retval': 0, 'sync_was_successful': True, 'offset_seconds': -45.5,
                'framerate_scale_factor': 1.0}

    from subtitles.tools.subsyncer import SubSyncer
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', fake_ffsubsync)

    result = _sync(subtitle, tmp_path, ['ffsubsync'])

    assert result.success
    assert [item.engine for item in result.successful_results] == ['ffsubsync']
    assert subtitle.read_text(encoding='utf-8') == 'well synced'


def test_rejection_leaves_the_original_untouched_when_it_is_the_only_engine(monkeypatch, in_memory_runner, tmp_path):
    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def fake_ffsubsync(self, output_path, **kwargs):
        _write(output_path, 'badly synced')
        return {'retval': 0, 'sync_was_successful': True, 'offset_seconds': 61.0,
                'framerate_scale_factor': 1.0}

    from subtitles.tools.subsyncer import SubSyncer
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', fake_ffsubsync)

    result = _sync(subtitle, tmp_path, ['ffsubsync'])

    assert not result.success
    assert subtitle.read_text(encoding='utf-8') == 'original'
    assert [path.name for path in tmp_path.iterdir()] == ['Movie.en.srt']


def test_keep_all_mode_discards_the_rejected_engine_output(monkeypatch, in_memory_runner, tmp_path):
    from subtitles.tools.subsync_engines import engine_output_path

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def fake_ffsubsync(self, output_path, **kwargs):
        _write(output_path, 'badly synced')
        return {'retval': 0, 'sync_was_successful': True, 'offset_seconds': 300.0,
                'framerate_scale_factor': 1.0}

    from subtitles.tools.subsyncer import SubSyncer
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', fake_ffsubsync)

    result = _sync(subtitle, tmp_path, ['ffsubsync'], output_mode='keep_all')

    assert not result.success
    assert not engine_output_path(str(subtitle), 'ffsubsync').exists()
    assert subtitle.read_text(encoding='utf-8') == 'original'


@pytest.mark.parametrize(
    'raw_result,max_offset_seconds',
    [
        ({'sync_was_successful': True, 'offset_seconds': 60.0}, '60'),
        ({'sync_was_successful': True, 'offset_seconds': -60.0}, 60),
        ({'sync_was_successful': True, 'offset_seconds': 1000.0}, None),
        ({'success': True}, '60'),
        ({'returncode': 0, 'stdout': '', 'stderr': ''}, '60'),
        (None, '60'),
    ],
)
def test_acceptable_results_are_not_rejected(raw_result, max_offset_seconds):
    from subtitles.tools.subsync_engines import validate_engine_result

    validate_engine_result('ffsubsync', raw_result, max_offset_seconds)


@pytest.mark.parametrize(
    'raw_result,max_offset_seconds',
    [
        ({'sync_was_successful': True, 'offset_seconds': 60.1}, '60'),
        ({'sync_was_successful': True, 'offset_seconds': -60.1}, 60),
    ],
)
def test_an_offset_past_the_maximum_is_rejected(raw_result, max_offset_seconds):
    """Bazarr overruling an engine that claimed success. That is a rejection."""
    from subtitles.tools.subsync_engines import SyncResultRejectedError, validate_engine_result

    with pytest.raises(SyncResultRejectedError):
        validate_engine_result('ffsubsync', raw_result, max_offset_seconds)


@pytest.mark.parametrize(
    'raw_result,max_offset_seconds',
    [
        ({'sync_was_successful': False, 'offset_seconds': 0.0}, '60'),
        ({'success': False}, '60'),
    ],
)
def test_an_engine_reporting_its_own_failure_is_a_decline(raw_result, max_offset_seconds):
    """The engine's own verdict on its own output, not Bazarr overruling it.

    ffsubsync setting sync_was_successful False is the same act as autosubsync's
    quality check refusing its own result: routine rather than a fault, so the
    two carry the same reason code and the same non-alarming wording.
    """
    from subtitles.tools.subsync_engines import SyncEngineDeclinedError, validate_engine_result

    with pytest.raises(SyncEngineDeclinedError):
        validate_engine_result('ffsubsync', raw_result, max_offset_seconds)


def test_a_decline_does_not_count_towards_the_engine_quarantine(monkeypatch, in_memory_runner, tmp_path):
    """An engine declining its own result is routine, not a fault.

    autosubsync's quality check refuses its own output whenever the audio is
    hard: sparse dialogue, heavy score, a language its model handles poorly.
    Counting that towards the consecutive-failure threshold quarantines the
    engine for that subtitle after three attempts, so it is skipped even after
    the user changes the reference or the sync settings.
    """
    from subtitles.tools.subsync_engines import InMemorySubsyncFailureStore

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def declining(self, output_path, **kwargs):
        _write(output_path, 'unsynced passthrough')
        return {'retval': 0, 'sync_was_successful': False, 'offset_seconds': 0.0,
                'framerate_scale_factor': 1.0}

    from subtitles.tools import subsyncer as subsyncer_module
    from subtitles.tools.subsync_engines import SubsyncEngineRunner
    from subtitles.tools.subsyncer import SubSyncer

    store = InMemorySubsyncFailureStore()
    monkeypatch.setattr(subsyncer_module, 'SubsyncEngineRunner', lambda: SubsyncEngineRunner(store))
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', declining)

    for _ in range(3):
        result = _sync(subtitle, tmp_path, ['ffsubsync'])
        assert not result.success

    assert store.failure_count(str(subtitle), 'ffsubsync') == 0
    assert not store.should_skip(str(subtitle), 'ffsubsync'), (
        'three routine declines quarantined the engine for this subtitle'
    )


def test_a_rejection_still_counts_towards_the_quarantine(monkeypatch, in_memory_runner, tmp_path):
    """Bazarr overruling an engine that claimed success is a different thing:
    the engine is confidently producing an answer this file cannot use."""
    from subtitles.tools.subsync_engines import InMemorySubsyncFailureStore, SubsyncEngineRunner
    from subtitles.tools import subsyncer as subsyncer_module
    from subtitles.tools.subsyncer import SubSyncer

    subtitle = tmp_path / 'Movie.en.srt'
    _write(subtitle, 'original')

    def far_off(self, output_path, **kwargs):
        _write(output_path, 'badly synced')
        return {'retval': 0, 'sync_was_successful': True, 'offset_seconds': 900.0,
                'framerate_scale_factor': 1.0}

    store = InMemorySubsyncFailureStore()
    monkeypatch.setattr(subsyncer_module, 'SubsyncEngineRunner', lambda: SubsyncEngineRunner(store))
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', far_off)

    _sync(subtitle, tmp_path, ['ffsubsync'])

    assert store.failure_count(str(subtitle), 'ffsubsync') == 1


@pytest.mark.parametrize('max_offset_seconds', ['-60', -60, 0, '0'])
def test_a_non_positive_maximum_means_no_limit(max_offset_seconds):
    """The sync API takes an arbitrary integer, not just the settings page's
    choices, and every non-positive value means the limit is off."""
    from subtitles.tools.subsync_engines import validate_engine_result

    validate_engine_result('ffsubsync', {'sync_was_successful': True, 'offset_seconds': 5000.0},
                           max_offset_seconds)
