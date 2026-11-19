# coding=utf-8
"""Startup on x86-64 CPUs without SSE4.2 or POPCNT.

numpy 2.4 and newer abort the process with an illegal instruction on such CPUs,
and textdistance, ffsubsync, autosubsync and json_tricks all import numpy. These tests pin
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

# Expected hamming distances, recorded from textdistance 4.6 (the library this
# helper replaced) so the search ordering stays pinned without needing it
# installed. Each row lists the distance from the query to TITLES in order.
TITLES = ['The Matrix', 'Matrix', 'The Matrix Reloaded', 'Star Trek', 'Star Trek: The Next Generation',
          'Amélie', 'Amelie', 'Tóth Ica', 'Fargo', 'Se7en', '']
EXPECTED_DISTANCES = {
    'matrix': [10, 1, 19, 8, 29, 5, 5, 7, 5, 6, 6],
    'Matrix': [10, 0, 19, 8, 29, 5, 5, 7, 5, 6, 6],
    'star trek': [10, 8, 19, 2, 23, 9, 9, 8, 9, 9, 9],
    'amelie': [9, 5, 18, 9, 30, 2, 1, 8, 6, 6, 6],
    'tóth': [10, 5, 19, 9, 30, 6, 6, 5, 5, 5, 4],
    '': [10, 6, 19, 9, 30, 6, 6, 8, 5, 5, 0],
    'x': [10, 6, 19, 9, 30, 6, 6, 8, 5, 5, 1],
}
EXPECTED_ORDER = {
    'matrix': ['Matrix', 'Amélie', 'Amelie', 'Fargo', 'Se7en', '', 'Tóth Ica', 'Star Trek', 'The Matrix',
               'The Matrix Reloaded', 'Star Trek: The Next Generation'],
    'amelie': ['Amelie', 'Amélie', 'Matrix', 'Fargo', 'Se7en', '', 'Tóth Ica', 'The Matrix', 'Star Trek',
               'The Matrix Reloaded', 'Star Trek: The Next Generation'],
    'star trek': ['Star Trek', 'Matrix', 'Tóth Ica', 'Amélie', 'Amelie', 'Fargo', 'Se7en', '', 'The Matrix',
                  'The Matrix Reloaded', 'Star Trek: The Next Generation'],
    'tóth': ['', 'Matrix', 'Tóth Ica', 'Fargo', 'Se7en', 'Amélie', 'Amelie', 'Star Trek', 'The Matrix',
             'The Matrix Reloaded', 'Star Trek: The Next Generation'],
}


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


def test_hamming_matches_the_recorded_textdistance_distances():
    from api.system.searches import _hamming_distance

    for query, expected in EXPECTED_DISTANCES.items():
        assert [_hamming_distance(query, title) for title in TITLES] == expected, query


def test_search_order_matches_the_recorded_textdistance_order():
    from api.system.searches import _hamming_distance

    for query, expected in EXPECTED_ORDER.items():
        assert sorted(TITLES, key=lambda title: _hamming_distance(query, title)) == expected, query


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


# The whole of main.py, run the way a native install runs it, on a CPU the check
# fails. Two things differ from a plain start and both are on purpose: no
# --no-update (so the requirements probe runs, the one that used to import
# numpy), and `updatemethod=External` is switched off (a git worktree has no
# .git directory to tell the tracked package_info apart from a real image).
# The network is cut so the release and announcement fetches fail fast.
_NUMPY_FREE_STARTUP = textwrap.dedent("""
    import os, runpy, socket, subprocess, sys

    class NumpyImported(Exception):
        pass

    class BlockNumpy:
        def find_spec(self, name, path=None, target=None):
            if name == 'numpy' or name.startswith('numpy.'):
                raise NumpyImported(name)

    sys.meta_path.insert(0, BlockNumpy())
    try:
        import numpy
    except NumpyImported:
        pass
    else:
        raise SystemExit('the numpy block did not engage')

    bazarr, custom_libs, config_dir = sys.argv[1:4]
    sys.path[:0] = [bazarr, custom_libs]

    real_getaddrinfo = socket.getaddrinfo
    def offline(host, *args, **kwargs):
        if host in (None, '', '0.0.0.0', '::', '127.0.0.1', '::1', 'localhost'):
            return real_getaddrinfo(host, *args, **kwargs)
        raise socket.gaierror('offline')
    socket.getaddrinfo = offline

    real_check_output = subprocess.check_output
    def no_pip(command, *args, **kwargs):
        if 'pip' in command:
            raise SystemExit('the requirements probe tried to install: %r' % (command,))
        return real_check_output(command, *args, **kwargs)
    subprocess.check_output = no_pip

    from utilities import cpu_features, package
    package.updates_are_externally_managed = lambda *args, **kwargs: False
    cpu_features._cached_missing_flags = ['sse4_2', 'popcnt']

    sys.argv = ['main.py', '-c', config_dir, '--no-signalr']
    runpy.run_path(os.path.join(bazarr, 'main.py'), run_name='bazarr_startup_probe')

    from app.get_args import args
    if args.no_update:
        raise SystemExit('the requirements probe was skipped by --no-update')
    leaked = sorted(name for name in sys.modules if name.split('.')[0] in ('numpy', 'textdistance'))
    if leaked:
        raise SystemExit('imported at startup: ' + ', '.join(leaked))
    print('startup finished without numpy')
    sys.stdout.flush()
    os._exit(0)
""")


def test_full_startup_without_no_update_needs_no_numpy(tmp_path):
    config_dir = tmp_path / 'config-root'
    config_dir.mkdir()

    environment = dict(os.environ)
    for name in ('NO_CLI', 'NO_UPDATE', 'PYTHONPATH', 'BAZARR_CONFIG_DIR'):
        environment.pop(name, None)
    environment.update({'SZ_USER_AGENT': 'test', 'BAZARR_VERSION': 'test'})

    completed = subprocess.run(
        [sys.executable, '-I', '-c', _NUMPY_FREE_STARTUP, str(REPO_ROOT / 'bazarr'),
         str(REPO_ROOT / 'custom_libs'), str(config_dir)],
        env=environment, capture_output=True, text=True, timeout=600, cwd=str(tmp_path),
    )

    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-4000:]
    assert 'startup finished without numpy' in completed.stdout


def test_requirements_probe_checks_numpy_packages_by_metadata(cpu_without_sse42, monkeypatch):
    from app import requirements

    imported = []
    monkeypatch.setattr(requirements.importlib, 'import_module', lambda name: imported.append(name))
    monkeypatch.setattr(requirements, '_has_forbidden_origin', lambda module: False)
    checked = []

    def satisfied(distribution, spec):
        checked.append(distribution)
        return True

    monkeypatch.setattr(requirements, '_requirement_is_satisfied', satisfied)

    assert requirements.missing_runtime_requirements() == []

    assert not set(imported) & requirements.NUMPY_RUNTIME_IMPORTS
    assert {'numpy', 'ffsubsync', 'autosubsync', 'json_tricks'} <= set(checked)
    assert 'flask' in imported


def test_requirements_probe_reports_a_numpy_package_whose_metadata_is_wrong(cpu_without_sse42, monkeypatch):
    from app import requirements

    monkeypatch.setattr(requirements.importlib, 'import_module', lambda name: None)
    monkeypatch.setattr(requirements, '_has_forbidden_origin', lambda module: False)
    monkeypatch.setattr(requirements, '_requirement_is_satisfied',
                        lambda distribution, spec: distribution != 'ffsubsync')

    assert requirements.missing_runtime_requirements() == ['ffsubsync']


def test_requirements_probe_still_imports_everything_on_a_modern_cpu(modern_cpu, monkeypatch):
    from app import requirements

    imported = []
    monkeypatch.setattr(requirements.importlib, 'import_module', lambda name: imported.append(name))
    monkeypatch.setattr(requirements, '_has_forbidden_origin', lambda module: False)
    monkeypatch.setattr(requirements, '_requirement_is_satisfied', lambda distribution, spec: True)

    assert requirements.missing_runtime_requirements() == []
    assert requirements.NUMPY_RUNTIME_IMPORTS <= set(imported)


def test_gemini_reply_parser_skips_json_tricks_on_a_cpu_without_sse42(cpu_without_sse42, monkeypatch):
    from subtitles.tools.translate.services import gemini_translator

    # None in sys.modules makes any import of the package raise.
    monkeypatch.setitem(sys.modules, 'json_tricks', None)

    assert gemini_translator._loads_reply('[{"index": 1, "text": "a"}]') == [{'index': 1, 'text': 'a'}]


def test_gemini_reply_parser_keeps_json_tricks_on_a_modern_cpu(modern_cpu):
    from subtitles.tools.translate.services import gemini_translator

    assert gemini_translator._loads_reply('[{"index": 1, "text": "a"}]') == [{'index': 1, 'text': 'a'}]


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

    from subtitles.tools import subsyncer

    monkeypatch.setattr(cpu_features, '_cached_missing_flags', ['sse4_2', 'popcnt'])
    monkeypatch.setattr(cpu_features, '_engines_logged', False)
    monkeypatch.setattr(subsyncer, '_no_usable_engine_logged', False)
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


def test_no_usable_engine_message_uses_labels_and_agrees_in_number(cpu_without_sse42, monkeypatch, tmp_path,
                                                                  schema_session):
    two, _subtitle, _progress = _sync(tmp_path, ['ffsubsync', 'autosubsync'], monkeypatch, schema_session)
    one, _subtitle, _progress = _sync(tmp_path, ['autosubsync'], monkeypatch, schema_session)

    assert two.results[0].message == ('FFsubsync and Autosubsync need SSE4.2 and POPCNT, which this CPU lacks. '
                                      'Enable ALASS to synchronize on this machine.')
    assert one.results[0].message == ('Autosubsync needs SSE4.2 and POPCNT, which this CPU lacks. '
                                      'Enable ALASS to synchronize on this machine.')


def test_no_usable_engine_card_does_not_count_the_placeholder_row(cpu_without_sse42, monkeypatch, tmp_path,
                                                                 schema_session):
    from subtitles import sync as sync_module

    result, _subtitle, _progress = _sync(tmp_path, ['ffsubsync'], monkeypatch, schema_session)

    assert sync_module._sync_outcome_message(result) == (
        'Sync failed: No usable sync engine on this CPU. FFsubsync needs SSE4.2 and POPCNT, which this CPU '
        'lacks. Enable ALASS to synchronize on this machine.')


def test_no_usable_engine_failure_is_an_error_once_then_quiet(cpu_without_sse42, monkeypatch, tmp_path,
                                                             schema_session, caplog):
    with caplog.at_level(logging.DEBUG):
        _sync(tmp_path, ['ffsubsync'], monkeypatch, schema_session)
        _sync(tmp_path, ['ffsubsync'], monkeypatch, schema_session)

    failures = [record for record in caplog.records if 'no usable sync engine on this CPU' in record.getMessage()]
    assert [record.levelno for record in failures] == [logging.ERROR, logging.DEBUG]


def test_progress_total_counts_only_the_usable_engines(cpu_without_sse42):
    from subtitles.sync import _sync_progress_total

    assert _sync_progress_total(['ffsubsync', 'autosubsync', 'alass']) == 1
    assert _sync_progress_total(['ffsubsync', 'autosubsync']) == 1
    assert _sync_progress_total(['alass']) == 1


def test_progress_total_is_unchanged_on_a_modern_cpu(modern_cpu):
    from subtitles.sync import _sync_progress_total

    assert _sync_progress_total(['ffsubsync', 'autosubsync', 'alass']) == 3
    assert _sync_progress_total(['ffsubsync', 'alass']) == 2


def test_a_generated_output_is_still_skipped_not_failed(cpu_without_sse42, monkeypatch, tmp_path, schema_session):
    from app import database as db_module
    from subtitles.tools.subsync_engines import REASON_GENERATED_SOURCE, RESULT_SKIPPED
    from subtitles.tools.subsyncer import SubSyncer

    monkeypatch.setattr(db_module, 'database', schema_session)
    generated = tmp_path / 'Movie.hu.ffsubsync.srt'
    _write(generated, 'already synced')
    (tmp_path / 'Movie.mkv').touch()

    result = SubSyncer().sync(
        video_path=str(tmp_path / 'Movie.mkv'), srt_path=str(generated), srt_lang='hu', hi=False, forced=False,
        max_offset_seconds='60', no_fix_framerate=True, gss=True, enabled_engines=['ffsubsync'],
        write_history=False,
    )

    assert [(item.status, item.reason) for item in result.results] == [(RESULT_SKIPPED, REASON_GENERATED_SOURCE)]


def test_saved_settings_are_not_rewritten(cpu_without_sse42, monkeypatch, tmp_path, schema_session):
    from app.config import settings

    saved = list(settings.subsync.enabled_engines)
    _sync(tmp_path, ['ffsubsync'], monkeypatch, schema_session)
    assert list(settings.subsync.enabled_engines) == saved


def test_empty_engine_list_keeps_its_old_behaviour(cpu_without_sse42, monkeypatch, tmp_path, schema_session):
    result, _subtitle, _progress = _sync(tmp_path, [], monkeypatch, schema_session)

    assert not result.success
    assert result.results == []
