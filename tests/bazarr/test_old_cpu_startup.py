# coding=utf-8
"""Startup on x86-64 CPUs without SSE4.2 or POPCNT.

numpy 2.4 and newer abort the process with an illegal instruction on such CPUs,
and textdistance, ffsubsync and autosubsync all import numpy. These tests pin
the three parts of the fix: nothing imported at startup needs numpy, the CPU is
checked without importing it, and the engines that need it are left out of a
sync instead of killing the process.
"""

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

REAL_TITLES = [
    'The Matrix', 'The Matrix Reloaded', 'The Matrix Revolutions', 'Matrix',
    'Breaking Bad', 'Better Call Saul', 'Game of Thrones', 'House of the Dragon',
    'Star Trek: The Next Generation', 'Star Trek', 'Star Wars: Episode IV - A New Hope',
    'Spirited Away', 'Sen to Chihiro no Kamikakushi', 'Amélie', 'Amelie',
    'Léon: The Professional', 'Leon', 'Pokémon', 'Pokemon', 'Dr. Strangelove',
    'Se7en', 'Seven', 'WALL-E', 'Wall E', 'Mr. Robot', 'Mr Robot', 'Fargo',
    'A Gyertyák csonkig égnek', 'Családi pótlék', 'Tóth Ica', 'Kontroll',
    '9-1-1', '911', '1917', '2001: A Space Odyssey', '', 'A', 'a',
]
QUERIES = ['matrix', 'Matrix', 'the matrix', 'star', 'Star Trek', 'amelie', 'robot', 'house', 'fargo', '1917',
           'tóth', '', 'x']


# ---------------------------------------------------------------- hamming

def test_hamming_counts_the_length_difference():
    from api.system.searches import _hamming_distance

    assert _hamming_distance('', '') == 0
    assert _hamming_distance('abc', 'abc') == 0
    assert _hamming_distance('abc', 'abd') == 1
    assert _hamming_distance('abc', '') == 3
    assert _hamming_distance('', 'abcd') == 4
    assert _hamming_distance('abc', 'abcde') == 2
    assert _hamming_distance('abcde', 'bcd') == 5


def test_hamming_matches_textdistance_on_real_titles():
    textdistance = pytest.importorskip('textdistance')
    from api.system.searches import _hamming_distance

    for query in QUERIES:
        for title in REAL_TITLES:
            assert _hamming_distance(query, title) == textdistance.hamming.distance(query, title), (query, title)
        local_order = sorted(REAL_TITLES, key=lambda title: _hamming_distance(query, title))
        reference_order = sorted(REAL_TITLES, key=lambda title: textdistance.hamming.distance(query, title))
        assert local_order == reference_order, query


# ------------------------------------------------------- numpy-free startup

_NUMPY_FREE_IMPORT = textwrap.dedent("""
    import sys

    class NumpyImported(Exception):
        pass

    class BlockNumpy:
        def find_spec(self, name, path=None, target=None):
            if name == 'numpy' or name.startswith('numpy.'):
                raise NumpyImported(name)

    sys.meta_path.insert(0, BlockNumpy())

    # Negative control: the hook has to stop a real numpy import, or the
    # import below proves nothing.
    try:
        import numpy
    except NumpyImported:
        pass
    else:
        raise SystemExit('the numpy block did not engage')

    sys.path[:0] = [sys.argv[1], sys.argv[2]]
    import api.system.searches

    leaked = sorted(name for name in sys.modules if name.split('.')[0] in ('numpy', 'textdistance'))
    if leaked:
        raise SystemExit('imported at startup: ' + ', '.join(leaked))
    print('numpy-free')
""")


def test_searches_import_needs_no_numpy(tmp_path):
    config_dir = tmp_path / 'config-root'
    for sub in ('', 'backup', 'cache', 'config', 'db', 'log', 'restore'):
        (config_dir / sub).mkdir(parents=True, exist_ok=True)
    for document in ('releases.txt', 'announcements.txt'):
        (config_dir / 'config' / document).write_text('[]', encoding='utf-8')

    environment = dict(os.environ)
    environment.update({
        'NO_CLI': 'true',
        'SZ_USER_AGENT': 'test',
        'BAZARR_VERSION': 'test',
        'BAZARR_CONFIG_DIR': str(config_dir),
    })
    environment.pop('PYTHONPATH', None)

    completed = subprocess.run(
        [sys.executable, '-I', '-c', _NUMPY_FREE_IMPORT, str(REPO_ROOT / 'bazarr'), str(REPO_ROOT / 'custom_libs')],
        env=environment, capture_output=True, text=True, timeout=300, cwd=str(tmp_path),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'numpy-free' in completed.stdout


def test_textdistance_is_no_longer_a_requirement():
    from app import requirements

    declared = (REPO_ROOT / 'requirements.txt').read_text(encoding='utf-8').lower()
    assert 'textdistance' not in declared
    assert 'textdistance' not in Path(requirements.__file__).read_text(encoding='utf-8').lower()


# -------------------------------------------------------------- CPU check

X86_FLAGS_OK = 'fpu vme sse sse2 ssse3 sse4_1 sse4_2 popcnt avx'
X86_FLAGS_OLD = 'fpu vme sse sse2 ssse3 sse4_1 avx'
X86_FLAGS_NO_POPCNT = 'fpu vme sse sse2 sse4_2'


def _cpuinfo(tmp_path, flags, cpus=2):
    blocks = [f'processor\t: {index}\nmodel name\t: Test CPU\nflags\t\t: {flags}\n' for index in range(cpus)]
    path = tmp_path / 'cpuinfo'
    path.write_text('\n'.join(blocks), encoding='utf-8')
    return str(path)


def test_cpu_check_passes_when_flags_are_present(tmp_path):
    from utilities import cpu_features

    path = _cpuinfo(tmp_path, X86_FLAGS_OK)
    assert cpu_features.missing_numpy_cpu_flags(path, system='Linux', machine='x86_64') == []


def test_cpu_check_names_each_missing_flag(tmp_path):
    from utilities import cpu_features

    assert cpu_features.missing_numpy_cpu_flags(
        _cpuinfo(tmp_path, X86_FLAGS_OLD), system='Linux', machine='x86_64') == ['sse4_2', 'popcnt']
    assert cpu_features.missing_numpy_cpu_flags(
        _cpuinfo(tmp_path, X86_FLAGS_NO_POPCNT), system='Linux', machine='AMD64') == ['popcnt']


def test_cpu_check_ignores_other_architectures_and_systems(tmp_path):
    from utilities import cpu_features

    path = _cpuinfo(tmp_path, X86_FLAGS_OLD)
    assert cpu_features.missing_numpy_cpu_flags(path, system='Linux', machine='aarch64') == []
    assert cpu_features.missing_numpy_cpu_flags(path, system='Darwin', machine='x86_64') == []
    assert cpu_features.missing_numpy_cpu_flags(path, system='Windows', machine='AMD64') == []


def test_cpu_check_treats_unreadable_cpuinfo_as_fine(tmp_path):
    from utilities import cpu_features

    assert cpu_features.missing_numpy_cpu_flags(
        str(tmp_path / 'missing'), system='Linux', machine='x86_64') == []
    assert cpu_features.missing_numpy_cpu_flags(str(tmp_path), system='Linux', machine='x86_64') == []
    empty = tmp_path / 'empty'
    empty.write_text('', encoding='utf-8')
    assert cpu_features.missing_numpy_cpu_flags(str(empty), system='Linux', machine='x86_64') == []
    no_flags = tmp_path / 'noflags'
    no_flags.write_text('processor\t: 0\nmodel name\t: Test\n', encoding='utf-8')
    assert cpu_features.missing_numpy_cpu_flags(str(no_flags), system='Linux', machine='x86_64') == []


def test_cpu_check_never_imports_numpy(tmp_path):
    from utilities import cpu_features

    before = 'numpy' in sys.modules
    cpu_features.missing_numpy_cpu_flags(_cpuinfo(tmp_path, X86_FLAGS_OLD), system='Linux', machine='x86_64')
    assert ('numpy' in sys.modules) == before


@pytest.fixture
def cpu_without_sse42(monkeypatch):
    from utilities import cpu_features

    monkeypatch.setattr(cpu_features, '_cached_missing_flags', ['sse4_2', 'popcnt'])
    monkeypatch.setattr(cpu_features, '_engines_logged', False)
    return cpu_features


@pytest.fixture
def modern_cpu(monkeypatch):
    from utilities import cpu_features

    monkeypatch.setattr(cpu_features, '_cached_missing_flags', [])
    monkeypatch.setattr(cpu_features, '_engines_logged', False)
    return cpu_features


def test_startup_warning_is_one_clear_line(cpu_without_sse42, caplog):
    with caplog.at_level(logging.WARNING):
        cpu_without_sse42.log_cpu_check()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert 'SSE4.2 and POPCNT' in text
    assert 'cannot run on this CPU' in text and 'ffsubsync and autosubsync' in text
    assert 'ALASS' in text and 'still works' in text


def test_startup_warning_is_silent_on_a_modern_cpu(modern_cpu, caplog):
    with caplog.at_level(logging.WARNING):
        modern_cpu.log_cpu_check()
    assert not caplog.records


def test_startup_check_never_raises(monkeypatch, caplog):
    from utilities import cpu_features

    monkeypatch.setattr(cpu_features, '_cached_missing_flags', None)
    monkeypatch.setattr(cpu_features, 'read_cpu_flags', lambda *_: (_ for _ in ()).throw(RuntimeError('boom')))
    cpu_features.log_cpu_check()
    assert cpu_features.numpy_engines_usable() is True


# ----------------------------------------------------- engine filtering

def test_numpy_engines_are_dropped_and_alass_kept(cpu_without_sse42, caplog):
    with caplog.at_level(logging.WARNING):
        assert cpu_without_sse42.usable_sync_engines(['ffsubsync', 'autosubsync', 'alass']) == ['alass']
        assert cpu_without_sse42.usable_sync_engines(['ffsubsync', 'alass']) == ['alass']
        assert cpu_without_sse42.usable_sync_engines(['alass']) == ['alass']

    # Logged once for the process, not once per sync.
    assert len([record for record in caplog.records if 'leaving' in record.getMessage()]) == 1


def test_all_engines_are_kept_on_a_modern_cpu(modern_cpu):
    assert modern_cpu.usable_sync_engines(['ffsubsync', 'autosubsync', 'alass']) == [
        'ffsubsync', 'autosubsync', 'alass']
    assert modern_cpu.usable_sync_engines([]) == []


def _write(path, text):
    Path(path).write_text(text, encoding='utf-8')


def _sync(tmp_path, enabled_engines, monkeypatch, schema_session):
    from app import database as db_module
    from subtitles.tools.subsyncer import SubSyncer

    monkeypatch.setattr(db_module, 'database', schema_session)
    subtitle = tmp_path / 'Movie.hu.srt'
    _write(subtitle, 'original')
    (tmp_path / 'Movie.mkv').touch()
    progress = []
    result = SubSyncer().sync(
        video_path=str(tmp_path / 'Movie.mkv'), srt_path=str(subtitle), srt_lang='hu', hi=False, forced=False,
        max_offset_seconds='60', no_fix_framerate=True, gss=True, force_sync=True, output_mode='keep_all',
        enabled_engines=enabled_engines, write_history=False,
        progress_callback=lambda message, value, total: progress.append((message, value, total)),
    )
    return result, subtitle, progress


def test_sync_runs_only_the_usable_engines(cpu_without_sse42, monkeypatch, tmp_path, schema_session):
    from subtitles.tools.subsyncer import SubSyncer

    ran = []

    def fake_external(self, engine, output_path, video_path):
        ran.append(engine)
        _write(output_path, 'synced')
        return {}

    def numpy_engine(self, *args, **kwargs):
        raise AssertionError('a numpy engine must not be started on this CPU')

    monkeypatch.setattr(SubSyncer, '_run_external_engine', fake_external)
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', numpy_engine)

    result, _subtitle, progress = _sync(tmp_path, ['ffsubsync', 'autosubsync', 'alass'], monkeypatch, schema_session)

    assert ran == ['alass']
    assert result.success
    assert [item.engine for item in result.results] == ['alass']
    assert progress[1] == ('Running ALASS (1/1)', 0, 1)


def test_sync_with_only_numpy_engines_reports_no_usable_engine(cpu_without_sse42, monkeypatch, tmp_path,
                                                              schema_session, caplog):
    from subtitles.tools.subsyncer import SubSyncer
    from subtitles.tools.subsync_engines import REASON_NO_USABLE_ENGINE, RESULT_FAILED

    def numpy_engine(self, *args, **kwargs):
        raise AssertionError('a numpy engine must not be started on this CPU')

    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', numpy_engine)
    monkeypatch.setattr(SubSyncer, '_run_external_engine', numpy_engine)

    with caplog.at_level(logging.ERROR):
        result, subtitle, _progress = _sync(tmp_path, ['ffsubsync', 'autosubsync'], monkeypatch, schema_session)

    assert not result.success
    assert [(item.engine, item.status, item.reason) for item in result.results] == [
        ('all', RESULT_FAILED, REASON_NO_USABLE_ENGINE)]
    assert 'SSE4.2 and POPCNT' in result.results[0].message
    assert 'no usable sync engine on this CPU' in caplog.text
    assert subtitle.read_text(encoding='utf-8') == 'original'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['Movie.hu.srt', 'Movie.mkv']


def test_no_usable_engine_result_reads_clearly_in_the_job_card(cpu_without_sse42, monkeypatch, tmp_path,
                                                              schema_session):
    from subtitles import sync as sync_module

    result, _subtitle, _progress = _sync(tmp_path, ['ffsubsync'], monkeypatch, schema_session)

    message = sync_module._sync_outcome_message(result)
    assert message.startswith('Sync failed')
    assert 'No usable sync engine on this CPU.' in message
    assert 'SSE4.2 and POPCNT' in message
    assert sync_module._sync_complete_job_name('/subtitle.hu.srt', result) == 'Failed to sync /subtitle.hu.srt'


def test_saved_settings_are_not_rewritten(cpu_without_sse42, monkeypatch, tmp_path, schema_session):
    from app.config import settings

    saved = list(settings.subsync.enabled_engines)
    _sync(tmp_path, ['ffsubsync'], monkeypatch, schema_session)
    assert list(settings.subsync.enabled_engines) == saved


def test_empty_engine_list_keeps_its_old_behaviour(cpu_without_sse42, monkeypatch, tmp_path, schema_session):
    result, _subtitle, _progress = _sync(tmp_path, [], monkeypatch, schema_session)

    assert not result.success
    assert result.results == []
