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
        ({'sync_was_successful': False, 'offset_seconds': 0.0}, '60'),
        ({'success': False}, '60'),
    ],
)
def test_unacceptable_results_are_rejected(raw_result, max_offset_seconds):
    from subtitles.tools.subsync_engines import SyncResultRejectedError, validate_engine_result

    with pytest.raises(SyncResultRejectedError):
        validate_engine_result('ffsubsync', raw_result, max_offset_seconds)
