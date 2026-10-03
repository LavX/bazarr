# coding=utf-8
"""What a backup archive has to contain, and when a restore has to stop.

Two defects are pinned here. A PostgreSQL install used to write an archive
holding config.yaml alone, because the database was only copied when
PostgreSQL was disabled, and restoring that archive was then refused as a
partial backup: the operator found out the day they needed it. And a restore
whose database step failed logged the failure and carried on to the restart,
leaving the new configuration sitting beside the old database.
"""

import os
import sqlite3
import stat
import sys
import zipfile

from types import SimpleNamespace

import pytest

import utilities.backup as backup_module

# Taken before the fixture swaps it out, for the tests about the check itself.
_REAL_REACHABILITY_CHECK = backup_module._check_postgres_reachable


def _write_fake_tool(directory, name, exit_code=0, writes_output=False, stderr=''):
    """Put an executable on PATH that records how it was called.

    Returns the path of the log file it appends its argv and PGPASSWORD to.
    """
    os.makedirs(directory, exist_ok=True)
    log_path = os.path.join(directory, f'{name}.log')
    output_arg = ''
    if writes_output:
        # pg_dump is asked for --file <path>; fake the artifact it would write.
        output_arg = 'for i in "$@"; do if [ "$prev" = "--file" ]; then echo "dump" > "$i"; fi; prev="$i"; done'
    stderr_arg = ''
    if stderr:
        stderr_arg = "printf '%s\\n' " + ' '.join(f"'{line}'" for line in stderr.splitlines()) + ' >&2'
    script_path = os.path.join(directory, name)
    with open(script_path, 'w', encoding='utf-8') as script:
        script.write(
            '#!/bin/sh\n'
            f'echo "ARGV $*" >> "{log_path}"\n'
            f'echo "PGPASSWORD ${{PGPASSWORD:-unset}}" >> "{log_path}"\n'
            f'{output_arg}\n'
            f'{stderr_arg}\n'
            f'exit {exit_code}\n'
        )
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log_path


UNKNOWN_PARAMETER_STDERR = (
    'pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter '
    '"transaction_timeout"\n'
    'pg_restore: warning: errors ignored on restore: 1'
)

MIXED_ERROR_STDERR = (
    'pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter '
    '"transaction_timeout"\n'
    'pg_restore: error: could not execute query: ERROR:  relation "table_history" already exists\n'
    'pg_restore: warning: errors ignored on restore: 2'
)


def _write_marker_database(path, marker):
    """A real SQLite file, so sqlite3's backup API has something to copy."""
    connection = sqlite3.connect(str(path))
    with connection:
        connection.execute('CREATE TABLE IF NOT EXISTS marker (value TEXT)')
        connection.execute('DELETE FROM marker')
        connection.execute('INSERT INTO marker (value) VALUES (?)', (marker,))
    connection.close()


def _read_marker_database(path):
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute('SELECT value FROM marker').fetchone()[0]
    finally:
        connection.close()


@pytest.fixture
def backup_env(monkeypatch, tmp_path):
    """An isolated config tree, an empty PATH, and no real restart."""
    from app.config import settings

    config_dir = tmp_path / 'config_dir'
    for sub in ('config', 'db', 'backup', 'restore'):
        (config_dir / sub).mkdir(parents=True, exist_ok=True)
    (config_dir / 'config' / 'config.yaml').write_text('general:\n  port: 6767\n', encoding='utf-8')
    _write_marker_database(config_dir / 'db' / 'bazarr.db', 'live')

    monkeypatch.setattr(backup_module, 'args', SimpleNamespace(config_dir=str(config_dir)))
    monkeypatch.setattr(settings.backup, 'folder', str(config_dir / 'backup'))
    monkeypatch.setattr(backup_module, 'event_stream', lambda *a, **kw: None)
    monkeypatch.setattr(backup_module.jobs_queue, 'update_job_name', lambda **kw: True)

    restarts = []
    monkeypatch.setattr(backup_module, 'restart_bazarr', lambda: restarts.append(True))
    # stop_bazarr ends the process; record the exit code instead. raising=False
    # so a module that never stops the start fails the assertions, not the setup.
    stops = []
    monkeypatch.setattr(backup_module, 'stop_bazarr', lambda code: stops.append(code), raising=False)
    monkeypatch.setitem(sys.modules, 'app.server', SimpleNamespace(webserver=None))
    # There is no server behind the fake client tools. The tests that are about
    # the connection check put the real one back.
    monkeypatch.setattr(backup_module, '_check_postgres_reachable', lambda connection: None)

    tools_dir = tmp_path / 'bin'
    tools_dir.mkdir()
    original_path = os.environ.get('PATH', '')
    monkeypatch.setenv('PATH', str(tools_dir))
    monkeypatch.delenv('PGPASSWORD', raising=False)
    for variable in ('POSTGRES_DATABASE', 'POSTGRES_USERNAME', 'POSTGRES_PASSWORD',
                     'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_URL'):
        monkeypatch.delenv(variable, raising=False)

    return SimpleNamespace(module=backup_module, settings=settings, config_dir=config_dir,
                           tools_dir=tools_dir, restarts=restarts, stops=stops, original_path=original_path,
                           backup_dir=config_dir / 'backup', restore_dir=config_dir / 'restore')


def _enable_postgresql(backup_env, monkeypatch):
    postgresql = backup_env.settings.postgresql
    monkeypatch.setattr(postgresql, 'enabled', True)
    monkeypatch.setattr(postgresql, 'host', 'db.internal')
    monkeypatch.setattr(postgresql, 'port', 5433)
    monkeypatch.setattr(postgresql, 'database', 'bazarr')
    monkeypatch.setattr(postgresql, 'username', 'bazarr_user')
    monkeypatch.setattr(postgresql, 'password', 'sekrit')
    monkeypatch.setattr(postgresql, 'url', '')


def _archives(backup_env):
    return sorted(p for p in os.listdir(backup_env.backup_dir) if p.endswith('.zip'))


def test_postgres_backup_archive_contains_the_database(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', writes_output=True)

    backup_env.module.backup_to_zip(job_id=1)

    archives = _archives(backup_env)
    assert len(archives) == 1
    with zipfile.ZipFile(backup_env.backup_dir / archives[0]) as archive:
        assert sorted(archive.namelist()) == ['bazarr_postgres.dump', 'config.yaml']


def test_postgres_backup_keeps_the_password_off_the_command_line(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', writes_output=True)

    backup_env.module.backup_to_zip(job_id=1)

    invocation = open(log_path, encoding='utf-8').read()
    assert 'PGPASSWORD sekrit' in invocation
    assert 'sekrit' not in invocation.splitlines()[0]
    assert '--format=custom' in invocation
    assert '--dbname bazarr' in invocation


def test_backup_is_refused_when_pg_dump_is_missing(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)

    with caplog.at_level('ERROR'):
        queued = backup_env.module.backup_to_zip()

    assert queued is False
    assert _archives(backup_env) == []
    assert 'pg_dump' in caplog.text
    assert 'postgresql-client' in caplog.text


def test_backup_api_reports_the_failure(backup_env, monkeypatch):
    from flask import Flask

    # Importing the API package runs the application's init, which looks for
    # its helper binaries on PATH, and the fixture has stripped PATH down to
    # the fake PostgreSQL tools.
    monkeypatch.setenv('PATH', backup_env.original_path)
    import api.system.backups as backups_api
    monkeypatch.setenv('PATH', str(backup_env.tools_dir))

    _enable_postgresql(backup_env, monkeypatch)

    app = Flask(__name__)
    with app.test_request_context('/api/system/backups', method='POST'):
        body, status = backups_api.SystemBackups.post.__wrapped__(backups_api.SystemBackups())

    assert status == 500
    assert 'backup' in body.lower()


def test_overlapping_postgres_backups_do_not_share_a_staging_file(backup_env, monkeypatch):
    """A scheduled and a manual backup are separate jobs and can run together.

    The second one here starts and finishes while the first is still dumping,
    which is the overlap the queue allows. With one shared staging file the
    second overwrote the first one's dump and then deleted it, and the first
    failed to archive a file that was no longer there.
    """
    _enable_postgresql(backup_env, monkeypatch)
    dumped = []

    def fake_dump(dest_path):
        job = len(dumped) + 1
        dumped.append(dest_path)
        with open(dest_path, 'w', encoding='utf-8') as handle:
            handle.write(f'dump-{job}')
        if job == 1:
            backup_env.module.backup_to_zip(job_id=2)

    monkeypatch.setattr(backup_env.module, '_dump_postgres_database', fake_dump)

    assert backup_env.module.backup_to_zip(job_id=1) is True

    assert len(set(dumped)) == 2, f'both jobs staged into {dumped}'
    contents = set()
    for name in _archives(backup_env):
        with zipfile.ZipFile(backup_env.backup_dir / name) as archive:
            contents.add(archive.read('bazarr_postgres.dump').decode())
    assert 'dump-1' in contents
    assert [p for p in os.listdir(backup_env.backup_dir) if not p.endswith('.zip')] == []


def test_postgres_backup_writes_no_archive_when_pg_dump_fails(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', exit_code=1)

    with pytest.raises(backup_env.module.BackupError):
        backup_env.module.backup_to_zip(job_id=1)

    assert _archives(backup_env) == []
    assert [p for p in os.listdir(backup_env.backup_dir)] == []


def test_sqlite_backup_archive_still_contains_the_database(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)

    backup_env.module.backup_to_zip(job_id=1)

    archives = _archives(backup_env)
    assert len(archives) == 1
    with zipfile.ZipFile(backup_env.backup_dir / archives[0]) as archive:
        assert sorted(archive.namelist()) == ['bazarr.db', 'config.yaml']


def _stage_restore(backup_env, database_name):
    (backup_env.restore_dir / 'config.yaml').write_text('general:\n  port: 7000\n', encoding='utf-8')
    if database_name == 'bazarr.db':
        _write_marker_database(backup_env.restore_dir / database_name, 'restored')
    elif database_name:
        (backup_env.restore_dir / database_name).write_text('dump', encoding='utf-8')


def _write_backup_archive(backup_env, database_name, stamp='2026.09.16_12.00.00'):
    """A well-named archive the restore endpoint will accept."""
    archive = backup_env.backup_dir / f'bazarr_backup_v1.2.3_{stamp}.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('config.yaml', 'general:\n  port: 7000\n')
        zip_file.writestr(database_name, 'database-bytes')
    return archive


def _capture_timers(monkeypatch):
    """Record Timer(delay, fn) without starting the thread."""
    scheduled = []

    class CapturedTimer:
        def __init__(self, delay, function, args=None, kwargs=None):
            self.delay = delay
            self.function = function
            self.args = args or ()
            self.kwargs = kwargs or {}
            scheduled.append(self)

        def start(self):
            return None

        def fire(self):
            return self.function(*self.args, **self.kwargs)

    monkeypatch.setattr('threading.Timer', CapturedTimer)
    return scheduled


def _call_restore_patch(backup_env, monkeypatch, filename):
    """Invoke SystemBackups.patch the way the other backup API test does."""
    from flask import Flask

    monkeypatch.setenv('PATH', backup_env.original_path)
    import api.system.backups as backups_api
    monkeypatch.setenv('PATH', str(backup_env.tools_dir))

    app = Flask(__name__)
    with app.test_request_context('/api/system/backups', method='PATCH',
                                  data={'filename': filename}):
        return backups_api.SystemBackups.patch.__wrapped__(backups_api.SystemBackups())


def test_postgres_restore_runs_pg_restore_with_the_clean_flags(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    assert backup_env.module.restore_from_backup() is True

    invocation = open(log_path, encoding='utf-8').read()
    for flag in ('--clean', '--if-exists', '--no-owner'):
        assert flag in invocation
    assert 'PGPASSWORD sekrit' in invocation
    assert backup_env.restarts == [True]
    assert (backup_env.config_dir / 'config' / 'config.yaml').read_text(encoding='utf-8') == \
        'general:\n  port: 7000\n'


def test_postgres_restore_aborts_before_the_restart_when_pg_restore_fails(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1)
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert (backup_env.config_dir / 'config' / 'config.yaml').read_text(encoding='utf-8') == \
        'general:\n  port: 6767\n'


def test_sqlite_restore_aborts_before_the_restart_when_the_database_copy_fails(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    _stage_restore(backup_env, 'bazarr.db')

    real_copy = backup_env.module.shutil.copy

    def failing_copy(source, destination):
        if os.path.basename(str(source)) == 'bazarr.db':
            raise OSError('no space left on device')
        return real_copy(source, destination)

    monkeypatch.setattr(backup_env.module.shutil, 'copy', failing_copy)

    assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert (backup_env.config_dir / 'config' / 'config.yaml').read_text(encoding='utf-8') == \
        'general:\n  port: 6767\n'
    assert _read_marker_database(backup_env.config_dir / 'db' / 'bazarr.db') == 'live'
    assert not os.path.isfile(str(backup_env.config_dir / 'config' / 'config.yaml.restore'))


def test_sqlite_restore_replaces_config_and_database(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    _stage_restore(backup_env, 'bazarr.db')

    assert backup_env.module.restore_from_backup() is True

    assert backup_env.restarts == [True]
    assert backup_env.stops == []
    assert _read_marker_database(backup_env.config_dir / 'db' / 'bazarr.db') == 'restored'
    assert os.listdir(backup_env.restore_dir) == []


def test_restore_refuses_a_partial_backup_and_clears_it(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    _stage_restore(backup_env, None)

    assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert os.listdir(backup_env.restore_dir) == []


def test_prepare_restore_does_not_restart_on_a_corrupted_archive(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = backup_env.backup_dir / 'bazarr_backup_v1.2.3_2026.09.16_10.00.00.zip'
    archive.write_bytes(b'PK\x03\x04 this is not a zip file at all')

    assert backup_env.module.prepare_restore(archive.name) is False

    assert backup_env.restarts == []
    assert os.listdir(backup_env.restore_dir) == []


def test_prepare_restore_does_not_restart_on_an_archive_without_the_database(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = backup_env.backup_dir / 'bazarr_backup_v1.2.3_2026.09.16_10.00.01.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('config.yaml', 'general:\n  port: 7000\n')

    assert backup_env.module.prepare_restore(archive.name) is False

    assert backup_env.restarts == []
    assert os.listdir(backup_env.restore_dir) == []


@pytest.mark.parametrize('engine,database_name', [('sqlite', 'bazarr.db'),
                                                  ('postgresql', 'bazarr_postgres.dump')])
def test_prepare_restore_refuses_an_archive_without_the_configuration(backup_env, monkeypatch, caplog,
                                                                      engine, database_name):
    if engine == 'postgresql':
        _enable_postgresql(backup_env, monkeypatch)
    else:
        monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = backup_env.backup_dir / 'bazarr_backup_v1.2.3_2026.09.16_10.00.03.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr(database_name, 'database-bytes')

    with caplog.at_level('ERROR'):
        assert backup_env.module.prepare_restore(archive.name) is False

    assert backup_env.restarts == []
    assert 'does not contain the configuration' in caplog.text
    # Nothing is left for the next start to pick up.
    assert os.listdir(backup_env.restore_dir) == []


def test_restore_api_refuses_an_archive_without_the_configuration(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = backup_env.backup_dir / 'bazarr_backup_v1.2.3_2026.09.16_10.00.04.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('bazarr.db', 'database-bytes')
    scheduled = _capture_timers(monkeypatch)

    body, status = _call_restore_patch(backup_env, monkeypatch, archive.name)

    assert status == 500
    assert scheduled == [], 'a restart was scheduled for a restore the next start refuses'


PRE_LAUNCH_WORDING = 'refused before pg_restore was started'
MID_RESTORE_WORDING = 'partially restored'


def test_pg_restore_missing_from_path_does_not_claim_the_database_may_be_corrupt(backup_env, monkeypatch,
                                                                                 caplog):
    _enable_postgresql(backup_env, monkeypatch)
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert PRE_LAUNCH_WORDING in caplog.text
    assert 'nothing was changed' in caplog.text
    assert MID_RESTORE_WORDING not in caplog.text
    assert 'pg_restore was not found on PATH' in caplog.text
    assert os.listdir(backup_env.restore_dir) == []
    # The database was never touched, so the start carries on.
    assert backup_env.stops == []


def test_no_configured_database_does_not_claim_the_database_may_be_corrupt(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)
    monkeypatch.setattr(backup_env.settings.postgresql, 'database', '')
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert PRE_LAUNCH_WORDING in caplog.text
    assert MID_RESTORE_WORDING not in caplog.text
    assert 'nothing to restore into' in caplog.text


def test_an_unparseable_url_does_not_claim_the_database_may_be_corrupt(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)
    monkeypatch.setattr(backup_env.settings.postgresql, 'url', 'postgresql://user@host:notaport/db')
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert PRE_LAUNCH_WORDING in caplog.text
    assert MID_RESTORE_WORDING not in caplog.text
    assert 'cannot be parsed' in caplog.text


def test_a_dump_that_cannot_be_moved_aside_keeps_the_whole_staged_backup(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1,
                     stderr='pg_restore: error: connection to server failed')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    real_replace = backup_env.module.os.replace

    def failing_replace(source, destination):
        if str(destination).endswith('.failed'):
            raise OSError('read-only file system')
        return real_replace(source, destination)

    monkeypatch.setattr(backup_env.module.os, 'replace', failing_replace)

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert MID_RESTORE_WORDING in caplog.text
    assert 'could not be moved aside' in caplog.text
    assert 'the next start will try this restore again' in caplog.text
    # The whole staged set survives, so the next start can finish the job and
    # nothing deletes the dump the message points at.
    assert sorted(os.listdir(backup_env.restore_dir)) == ['bazarr_postgres.dump', 'config.yaml']


def test_a_failing_pg_restore_says_the_database_may_be_half_restored(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1,
                     stderr='pg_restore: error: connection to server failed')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert 'partially restored' in caplog.text
    assert 'restored again or rebuilt' in caplog.text
    # The dump survives, parked under a name the next start neither applies nor
    # deletes, so the restore can be retried from it.
    kept = backup_env.restore_dir / 'bazarr_postgres.dump.failed'
    assert kept.is_file()
    assert str(kept) in caplog.text
    assert not (backup_env.restore_dir / 'config.yaml').exists()


def test_a_failing_sqlite_restore_still_says_the_database_was_left_alone(backup_env, monkeypatch, caplog):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    _stage_restore(backup_env, 'bazarr.db')

    real_copy = backup_env.module.shutil.copy

    def failing_copy(source, destination):
        if os.path.basename(str(source)) == 'bazarr.db':
            raise OSError('no space left on device')
        return real_copy(source, destination)

    monkeypatch.setattr(backup_env.module.shutil, 'copy', failing_copy)

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    # The wording stays what it was on SQLite: a copy that failed changes
    # nothing the operator has to repair.
    assert 'The backup archive itself is untouched' in caplog.text
    assert 'partially restored' not in caplog.text
    assert 'pg_restore' not in caplog.text
    assert os.listdir(backup_env.restore_dir) == []
    # Nothing it did needs repairing, so the start goes on as it always has.
    assert backup_env.stops == []


# The restore runs at boot. When it leaves a database nothing may run on, the
# boot has to end there, and the next one too while the damage is unrepaired:
# returning False used to let init carry on and start Bazarr on it.

@pytest.mark.parametrize('dump_can_be_moved', [True, False], ids=['moved-aside', 'left-in-place'])
def test_a_pg_restore_that_fails_partway_stops_the_start(backup_env, monkeypatch, caplog, dump_can_be_moved):
    from literals import EXIT_RESTORE_ERROR

    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1,
                     stderr='pg_restore: error: connection to server failed')
    _stage_restore(backup_env, 'bazarr_postgres.dump')
    if not dump_can_be_moved:
        real_replace = backup_env.module.os.replace

        def failing_replace(source, destination):
            if str(destination).endswith('.failed'):
                raise OSError('read-only file system')
            return real_replace(source, destination)

        monkeypatch.setattr(backup_env.module.os, 'replace', failing_replace)

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.stops == [EXIT_RESTORE_ERROR]
    assert EXIT_RESTORE_ERROR != 0
    assert backup_env.restarts == []
    assert 'Bazarr is stopping' in caplog.text


def test_the_start_after_a_partial_pg_restore_is_refused_too(backup_env, monkeypatch, caplog):
    from literals import EXIT_RESTORE_ERROR

    _enable_postgresql(backup_env, monkeypatch)
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1,
                                stderr='pg_restore: error: connection to server failed')
    _stage_restore(backup_env, 'bazarr_postgres.dump')
    assert backup_env.module.restore_from_backup() is False
    kept = backup_env.restore_dir / 'bazarr_postgres.dump.failed'
    assert kept.is_file()
    runs = open(log_path, encoding='utf-8').read().count('ARGV')
    backup_env.stops.clear()
    caplog.clear()

    # The next start: nothing is staged any more, only the parked dump.
    with caplog.at_level('CRITICAL'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.stops == [EXIT_RESTORE_ERROR]
    assert open(log_path, encoding='utf-8').read().count('ARGV') == runs, 'pg_restore ran again'
    assert kept.is_file(), 'the dump the message points at is gone'
    # The message says exactly how to get out of it.
    assert str(kept) in caplog.text
    assert f'into {backup_env.restore_dir}' in caplog.text
    assert str(backup_env.backup_dir) in caplog.text
    assert 'pg_restore --clean --if-exists --no-owner' in caplog.text


@pytest.mark.parametrize('engine', ['sqlite', 'postgresql'])
def test_a_configuration_that_cannot_be_put_in_place_stops_the_start(backup_env, monkeypatch, caplog, engine):
    from literals import EXIT_RESTORE_ERROR

    if engine == 'postgresql':
        _enable_postgresql(backup_env, monkeypatch)
        _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
        _stage_restore(backup_env, 'bazarr_postgres.dump')
    else:
        monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
        _stage_restore(backup_env, 'bazarr.db')
    dest_config = backup_env.config_dir / 'config' / 'config.yaml'
    real_replace = backup_env.module.os.replace

    def failing_replace(source, destination):
        if str(destination) == str(dest_config):
            raise OSError('device or resource busy')
        return real_replace(source, destination)

    monkeypatch.setattr(backup_env.module.os, 'replace', failing_replace)

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert backup_env.stops == [EXIT_RESTORE_ERROR]
    assert backup_env.restarts == []
    # The configuration from the backup is kept for the operator to put in place.
    staged = backup_env.config_dir / 'config' / 'config.yaml.restore'
    assert staged.read_text(encoding='utf-8') == 'general:\n  port: 7000\n'
    assert f'kept at {staged}: copy it over {dest_config}' in caplog.text


def test_a_start_with_nothing_to_restore_goes_on(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)

    assert backup_env.module.restore_from_backup() is False

    assert backup_env.stops == []
    assert backup_env.restarts == []


def test_postgres_enabled_through_the_environment_is_not_backed_up_as_sqlite(backup_env, monkeypatch):
    # The configuration file says SQLite; the environment overrides it, exactly
    # as app.database reads it.
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    monkeypatch.setattr(backup_env.settings.postgresql, 'database', 'bazarr')
    monkeypatch.setattr(backup_env.settings.postgresql, 'username', 'bazarr_user')
    monkeypatch.setattr(backup_env.settings.postgresql, 'password', 'sekrit')
    monkeypatch.setattr(backup_env.settings.postgresql, 'url', '')
    monkeypatch.setenv('POSTGRES_ENABLED', 'true')
    _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', writes_output=True)

    backup_env.module.backup_to_zip(job_id=1)

    archives = _archives(backup_env)
    assert len(archives) == 1
    with zipfile.ZipFile(backup_env.backup_dir / archives[0]) as archive:
        assert sorted(archive.namelist()) == ['bazarr_postgres.dump', 'config.yaml']


def test_postgres_enabled_through_the_environment_restores_with_pg_restore(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    monkeypatch.setattr(backup_env.settings.postgresql, 'database', 'bazarr')
    monkeypatch.setenv('POSTGRES_ENABLED', 'true')
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    assert backup_env.module.restore_from_backup() is True

    assert '--clean' in open(log_path, encoding='utf-8').read()
    # The SQLite path was not taken, so the live database file is untouched.
    assert _read_marker_database(backup_env.config_dir / 'db' / 'bazarr.db') == 'live'


def test_postgres_backup_is_refused_through_the_environment_too(backup_env, monkeypatch, caplog):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    monkeypatch.setenv('POSTGRES_ENABLED', 'true')

    with caplog.at_level('ERROR'):
        assert backup_env.module.backup_to_zip() is False

    assert _archives(backup_env) == []
    assert 'pg_dump' in caplog.text


def test_unknown_server_parameters_alone_are_a_warning_not_a_failure(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1,
                     stderr=UNKNOWN_PARAMETER_STDERR)
    monkeypatch.setattr(backup_env.module, '_postgres_tool_major', lambda path: '17')
    monkeypatch.setattr(backup_env.module, '_postgres_server_major', lambda connection: '16')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('WARNING'):
        assert backup_env.module.restore_from_backup() is True

    assert backup_env.restarts == [True]
    assert 'unknown server parameters' in caplog.text
    assert 'Client major version is 17, server major version is 16' in caplog.text


class _ServerConnection:
    """Just enough of a driver connection to answer the server version."""

    closed = False

    def __init__(self, version):
        self.server_version = version
        self.info = SimpleNamespace(server_version=version)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self.closed = True


def _fake_driver(version, opened):
    def connect(conninfo, **arguments):
        opened.append((conninfo, arguments, _ServerConnection(version)))
        return opened[-1][2]
    return SimpleNamespace(connect=connect)


def test_the_server_version_is_read_with_psycopg2_which_the_image_ships(backup_env, monkeypatch):
    """Only psycopg 2 is installed in the image, so importing psycopg 3 alone
    worded every restore warning with an unknown server version."""
    _enable_postgresql(backup_env, monkeypatch)
    opened = []
    monkeypatch.setitem(sys.modules, 'psycopg2', _fake_driver(160004, opened))
    monkeypatch.setitem(sys.modules, 'psycopg', None)

    connection = backup_env.module._postgres_connection_settings()
    assert backup_env.module._postgres_server_major(connection) == '16'

    (conninfo, arguments, server), = opened
    assert conninfo == "host='db.internal' port='5433' dbname='bazarr' user='bazarr_user'"
    assert arguments == {'password': 'sekrit', 'connect_timeout': 5}
    # Leaving a psycopg 2 connection's with-block does not close it.
    assert server.closed is True


def test_the_server_version_probe_keeps_the_url_query_options(backup_env, monkeypatch):
    """The probe connected with the five fields alone, so a server that only
    takes client certificates or verify-full was worded as 'unknown'."""
    _enable_postgresql_by_url(backup_env, monkeypatch)
    opened = []
    monkeypatch.setitem(sys.modules, 'psycopg2', _fake_driver(160004, opened))

    connection = backup_env.module._postgres_connection_settings()
    assert backup_env.module._postgres_server_major(connection) == '16'

    (conninfo, arguments, server), = opened
    for option in ("host='url.host'", "port='6543'", "sslmode='verify-full'",
                   "sslcert='/certs/client.crt'", "sslrootcert='/certs/root.crt'"):
        assert option in conninfo
    assert 'url_password' not in conninfo
    assert arguments == {'password': 'url_password', 'connect_timeout': 5}
    assert server.closed is True


def test_without_psycopg2_the_server_version_is_read_with_psycopg_3(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    opened = []
    monkeypatch.setitem(sys.modules, 'psycopg2', None)
    monkeypatch.setitem(sys.modules, 'psycopg', _fake_driver(170002, opened))

    connection = backup_env.module._postgres_connection_settings()
    assert backup_env.module._postgres_server_major(connection) == '17'
    assert [server.closed for _, _, server in opened] == [True]

    monkeypatch.setitem(sys.modules, 'psycopg', None)
    assert backup_env.module._postgres_server_major(connection) is None


def test_an_unknown_parameter_mixed_with_a_real_error_is_still_a_failure(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1, stderr=MIXED_ERROR_STDERR)
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    assert backup_env.module.restore_from_backup() is False

    assert backup_env.restarts == []
    assert (backup_env.config_dir / 'config' / 'config.yaml').read_text(encoding='utf-8') == \
        'general:\n  port: 6767\n'


def test_an_archive_from_the_other_engine_says_which_one_it_is(backup_env, monkeypatch, caplog):
    _enable_postgresql(backup_env, monkeypatch)
    _stage_restore(backup_env, 'bazarr.db')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert 'This backup holds an SQLite database (bazarr.db)' in caplog.text
    assert 'this instance runs PostgreSQL and needs bazarr_postgres.dump' in caplog.text
    assert 'partial backup' not in caplog.text


def test_prepare_restore_names_the_engine_of_a_foreign_archive(backup_env, monkeypatch, caplog):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = backup_env.backup_dir / 'bazarr_backup_v1.2.3_2026.09.16_10.00.02.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('config.yaml', 'general:\n  port: 7000\n')
        zip_file.writestr('bazarr_postgres.dump', 'dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.prepare_restore(archive.name) is False

    assert backup_env.restarts == []
    assert 'This backup holds a PostgreSQL dump (bazarr_postgres.dump)' in caplog.text
    assert 'this instance runs SQLite and needs bazarr.db' in caplog.text
    assert os.listdir(backup_env.restore_dir) == []


def test_sqlite_backup_writes_no_archive_when_the_database_copy_fails(backup_env, monkeypatch, caplog):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    (backup_env.config_dir / 'db' / 'bazarr.db').write_text('not a database at all', encoding='utf-8')

    with caplog.at_level('ERROR'), pytest.raises(backup_env.module.BackupError):
        backup_env.module.backup_to_zip(job_id=1)

    assert os.listdir(backup_env.backup_dir) == []
    assert 'no backup file was written' in caplog.text


def test_a_failed_archive_write_leaves_no_temporary_dump_behind(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', writes_output=True)
    (backup_env.config_dir / 'config' / 'config.yaml').unlink()

    with pytest.raises(Exception):
        backup_env.module.backup_to_zip(job_id=1)

    assert os.listdir(backup_env.backup_dir) == []


def test_connection_settings_follow_the_same_precedence_as_the_application(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    monkeypatch.setenv('POSTGRES_DATABASE', 'from_environment')

    connection = backup_env.module._postgres_connection_settings()

    assert connection['database'] == 'from_environment'
    assert connection['host'] == 'db.internal'
    assert connection['password'] == 'sekrit'


def test_config_yaml_only_fills_what_the_connection_url_leaves_out(backup_env, monkeypatch):
    postgresql = backup_env.settings.postgresql
    monkeypatch.setattr(postgresql, 'enabled', True)
    # host and port are never empty in config.yaml: these are its defaults.
    monkeypatch.setattr(postgresql, 'host', 'localhost')
    monkeypatch.setattr(postgresql, 'port', 5432)
    monkeypatch.setattr(postgresql, 'database', 'configured_database')
    monkeypatch.setattr(postgresql, 'username', 'configured_user')
    monkeypatch.setattr(postgresql, 'password', 'configured_password')
    monkeypatch.setattr(postgresql, 'url', 'postgresql://url_user@url.host:6543/url_database')

    connection = backup_env.module._postgres_connection_settings()

    assert connection['host'] == 'url.host'
    assert connection['port'] == 6543
    assert connection['database'] == 'url_database'
    assert connection['username'] == 'url_user'
    # The URL carries no password, so config.yaml supplies it.
    assert connection['password'] == 'configured_password'


def test_an_unparseable_connection_url_does_not_escape_as_a_boot_failure(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    monkeypatch.setattr(backup_env.settings.postgresql, 'url', 'postgresql://user@host:notaport/db')

    with pytest.raises(backup_env.module.BackupError):
        backup_env.module._postgres_connection_settings()

    # And the caller that runs at boot turns it into a refusal, not a crash.
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
    _stage_restore(backup_env, 'bazarr_postgres.dump')
    assert backup_env.module.restore_from_backup() is False
    assert backup_env.restarts == []


RESTORE_STAGED_BODY = {
    'restart': True,
    'message': 'Restore staged; Bazarr will restart to apply it',
}


def test_restore_api_returns_success_before_restart(backup_env, monkeypatch):
    """PATCH must answer 2xx before restart_bazarr runs.

    prepare_restore used to call webserver.close_all() and restart_bazarr()
    (os._exit) on the request thread, so the 204 never left. Docker's
    supervisor proxy then answered 503 for the dropped upstream. This fails
    when restart runs before the view returns.
    """
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = _write_backup_archive(backup_env, 'bazarr.db')
    scheduled = _capture_timers(monkeypatch)

    order = []
    monkeypatch.setattr(backup_env.module, 'restart_bazarr', lambda: order.append('restart'))

    result = _call_restore_patch(backup_env, monkeypatch, archive.name)
    order.append('response')

    body, status = result
    assert (status, body, order) == (200, RESTORE_STAGED_BODY, ['response'])
    assert scheduled
    scheduled[0].fire()
    assert order == ['response', 'restart']


def test_restore_api_postgres_archive_also_answers_before_restart(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    archive = _write_backup_archive(backup_env, 'bazarr_postgres.dump',
                                    stamp='2026.09.16_12.00.01')
    scheduled = _capture_timers(monkeypatch)

    order = []
    monkeypatch.setattr(backup_env.module, 'restart_bazarr', lambda: order.append('restart'))

    result = _call_restore_patch(backup_env, monkeypatch, archive.name)
    order.append('response')

    body, status = result
    assert (status, body, order) == (200, RESTORE_STAGED_BODY, ['response'])
    assert scheduled
    scheduled[0].fire()
    assert order == ['response', 'restart']


def test_restore_api_failure_is_still_500_and_does_not_schedule_restart(backup_env, monkeypatch):
    monkeypatch.setattr(backup_env.settings.postgresql, 'enabled', False)
    archive = backup_env.backup_dir / 'bazarr_backup_v1.2.3_2026.09.16_12.00.02.zip'
    archive.write_bytes(b'PK\x03\x04 this is not a zip file at all')
    scheduled = _capture_timers(monkeypatch)

    body, status = _call_restore_patch(backup_env, monkeypatch, archive.name)

    assert status == 500
    assert body == 'Error while restoring backup. Check logs.'
    assert scheduled == []
    assert backup_env.restarts == []


# ---------------------------------------------- POSTGRES_URL query options

TLS_URL = ('postgresql://url_user:url_password@url.host:6543/url_database'
           '?sslmode=verify-full&sslcert=/certs/client.crt&sslkey=/certs/client.key'
           '&sslrootcert=/certs/root.crt')


def _enable_postgresql_by_url(backup_env, monkeypatch, url=TLS_URL):
    postgresql = backup_env.settings.postgresql
    monkeypatch.setattr(postgresql, 'enabled', True)
    # What config.yaml holds when only the URL was set: its validator defaults.
    monkeypatch.setattr(postgresql, 'host', 'localhost')
    monkeypatch.setattr(postgresql, 'port', 5432)
    for key in ('database', 'username', 'password'):
        monkeypatch.setattr(postgresql, key, '')
    monkeypatch.setattr(postgresql, 'url', url)


def _argv_line(log_path):
    return open(log_path, encoding='utf-8').read().splitlines()[0]


@pytest.mark.parametrize('tool', ['pg_dump', 'pg_restore'])
def test_the_client_tools_keep_the_url_query_options(backup_env, monkeypatch, tool):
    """Five separate fields dropped every query option: client certificates
    failed and verify-full quietly became unverified TLS."""
    _enable_postgresql_by_url(backup_env, monkeypatch)
    log_path = _write_fake_tool(str(backup_env.tools_dir), tool, writes_output=tool == 'pg_dump')

    if tool == 'pg_dump':
        backup_env.module.backup_to_zip(job_id=1)
    else:
        _stage_restore(backup_env, 'bazarr_postgres.dump')
        assert backup_env.module.restore_from_backup() is True

    argv = _argv_line(log_path)
    for option in ("sslmode='verify-full'", "sslcert='/certs/client.crt'", "sslkey='/certs/client.key'",
                   "sslrootcert='/certs/root.crt'", "host='url.host'", "port='6543'",
                   "dbname='url_database'", "user='url_user'"):
        assert option in argv
    assert '--no-password' in argv
    assert 'url_password' not in argv
    assert 'PGPASSWORD url_password' in open(log_path, encoding='utf-8').read()


@pytest.mark.parametrize('tool', ['pg_dump', 'pg_restore'])
def test_a_service_url_is_backed_up_and_restored_without_a_database_name(backup_env, monkeypatch, tool):
    """The service entry names the database, so the URL carries none, and the
    missing name refused every backup and restore."""
    _enable_postgresql_by_url(backup_env, monkeypatch, 'postgresql:///?service=bazarr')
    log_path = _write_fake_tool(str(backup_env.tools_dir), tool, writes_output=tool == 'pg_dump')

    if tool == 'pg_dump':
        backup_env.module.backup_to_zip(job_id=1)
    else:
        _stage_restore(backup_env, 'bazarr_postgres.dump')
        assert backup_env.module.restore_from_backup() is True

    argv = _argv_line(log_path)
    assert "--dbname service='bazarr'" in argv
    assert 'localhost' not in argv


@pytest.mark.parametrize('url, host_option, port_option', [
    ('postgresql:///?host=primary:5432&host=standby:5433', "host='primary,standby'",
     "port='5432,5433'"),
    ('postgresql:///?host=db:6543', "host='db'", "port='6543'"),
    # Entries without a port change nothing: the host list alone, and the port
    # the URL resolution already carried, here config.yaml's default.
    ('postgresql:///?host=primary&host=standby', "host='primary,standby'", "port='5432'"),
])
def test_the_conninfo_splits_a_query_host_port_off_its_host(backup_env, monkeypatch, url,
                                                            host_option, port_option):
    """The application's dialect splits the port a query host entry carries inline
    off before libpq sees it, host=db and port=6543 from ?host=db:6543, so the
    conninfo has to carry the same split: a host list that keeps the inline
    ports, host=primary:5432,standby:5433, is not a form libpq reads, and the
    reachability check, pg_dump and pg_restore would all fail against a
    database the application connects to. Entries without a port keep the
    conninfo they always had."""
    _enable_postgresql_by_url(backup_env, monkeypatch, url)

    conninfo = backup_env.module._postgres_conninfo(backup_env.module._postgres_connection_settings())

    assert host_option in conninfo
    assert port_option in conninfo


def test_a_password_in_the_url_query_stays_off_the_command_line(backup_env, monkeypatch):
    _enable_postgresql_by_url(backup_env, monkeypatch,
                              'postgresql://url.host/url_database?sslmode=require&password=query_secret')
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', writes_output=True)

    backup_env.module.backup_to_zip(job_id=1)

    assert 'query_secret' not in _argv_line(log_path)
    assert 'PGPASSWORD query_secret' in open(log_path, encoding='utf-8').read()


def test_the_five_field_connection_is_passed_as_before(backup_env, monkeypatch):
    _enable_postgresql(backup_env, monkeypatch)
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_dump', writes_output=True)

    backup_env.module.backup_to_zip(job_id=1)

    argv = _argv_line(log_path)
    assert argv.endswith('--host db.internal --port 5433 --username bazarr_user --no-password '
                         '--dbname bazarr')
    assert 'sekrit' not in argv


class _Refused(Exception):
    pass


def _driver(opened, refuse=False):
    class _Connection:
        def close(self):
            pass

    def connect(conninfo, **arguments):
        opened.append((conninfo, arguments))
        if refuse:
            raise _Refused('connection to server at "url.host", port 6543 failed: Connection refused\n')
        return _Connection()
    return SimpleNamespace(connect=connect)


def test_an_unreachable_database_leaves_the_restore_for_the_next_start(backup_env, monkeypatch, caplog):
    """pg_restore that cannot connect exits non-zero like one that failed
    partway, and was reported as a half-restored database that stopped every
    start, although nothing had touched it."""
    _enable_postgresql_by_url(backup_env, monkeypatch)
    monkeypatch.setattr(backup_env.module, '_check_postgres_reachable', _REAL_REACHABILITY_CHECK)
    opened = []
    monkeypatch.setitem(sys.modules, 'psycopg2', _driver(opened, refuse=True))
    log_path = _write_fake_tool(str(backup_env.tools_dir), 'pg_restore')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    # The check used the settings pg_restore would have had, password apart.
    (conninfo, arguments), = opened
    assert "sslmode='verify-full'" in conninfo and "sslcert='/certs/client.crt'" in conninfo
    assert 'url_password' not in conninfo and arguments['password'] == 'url_password'

    assert not os.path.exists(log_path), 'pg_restore ran'
    assert backup_env.stops == []
    assert backup_env.restarts == []
    assert 'could not be reached' in caplog.text
    assert 'nothing was changed' in caplog.text
    assert MID_RESTORE_WORDING not in caplog.text
    assert sorted(os.listdir(backup_env.restore_dir)) == ['bazarr_postgres.dump', 'config.yaml']
    assert (backup_env.config_dir / 'config' / 'config.yaml').read_text(encoding='utf-8') == \
        'general:\n  port: 6767\n'
    assert not (backup_env.config_dir / 'config' / 'config.yaml.restore').exists()


def test_a_reachable_database_that_fails_mid_restore_still_stops_the_start(backup_env, monkeypatch, caplog):
    from literals import EXIT_RESTORE_ERROR

    _enable_postgresql_by_url(backup_env, monkeypatch)
    monkeypatch.setattr(backup_env.module, '_check_postgres_reachable', _REAL_REACHABILITY_CHECK)
    opened = []
    monkeypatch.setitem(sys.modules, 'psycopg2', _driver(opened))
    _write_fake_tool(str(backup_env.tools_dir), 'pg_restore', exit_code=1,
                     stderr='pg_restore: error: could not execute query: ERROR:  out of shared memory')
    _stage_restore(backup_env, 'bazarr_postgres.dump')

    with caplog.at_level('ERROR'):
        assert backup_env.module.restore_from_backup() is False

    assert len(opened) == 1
    assert backup_env.stops == [EXIT_RESTORE_ERROR]
    assert MID_RESTORE_WORDING in caplog.text
    assert (backup_env.restore_dir / 'bazarr_postgres.dump.failed').is_file()


# ------------------------------------- one connection URL for app and backups

def _resolve(url='', environ=None, **configured):
    """The URL resolved from a config.yaml postgresql section and an environment."""
    from app.postgres_url import postgres_engine_url

    postgresql = dict(host='localhost', port=5432, database='', username='', password='', url=url)
    postgresql.update(configured)
    return postgres_engine_url(SimpleNamespace(postgresql=SimpleNamespace(**postgresql)),
                               environ={} if environ is None else environ)


def _target(url):
    return url.username, url.password, url.host, url.port, url.database


def test_a_url_only_setup_reaches_the_url_host_past_the_config_defaults():
    """config.yaml always carries localhost:5432, and those replaced the URL's
    host and port, so an install configured by POSTGRES_URL alone never connected."""
    url = _resolve(environ={'POSTGRES_URL': 'postgresql://u:p@db.example:6543/bazarr?sslmode=verify-full'})

    assert _target(url) == ('u', 'p', 'db.example', 6543, 'bazarr')
    assert url.query == {'sslmode': 'verify-full'}


def test_the_environment_still_overrides_the_url():
    url = _resolve('postgresql://u:p@db.example:6543/bazarr',
                   environ={'POSTGRES_HOST': 'other.example', 'POSTGRES_PORT': '7654',
                            'POSTGRES_USERNAME': 'env_user', 'POSTGRES_PASSWORD': 'env_password',
                            'POSTGRES_DATABASE': 'env_database'})

    assert (url.username, url.password, url.host, url.database) == (
        'env_user', 'env_password', 'other.example', 'env_database')
    assert int(url.port) == 7654


def test_the_url_wins_over_config_yaml_for_every_field():
    url = _resolve('postgresql://u:p@db.example:6543/bazarr', host='config.example', port=5433,
                   username='config_user', password='config_password', database='config_database')

    assert _target(url) == ('u', 'p', 'db.example', 6543, 'bazarr')


def test_config_yaml_fills_what_the_url_leaves_out():
    url = _resolve('postgresql:///bazarr', host='config.example', port=5433, username='config_user',
                   password='config_password')

    assert _target(url) == ('config_user', 'config_password', 'config.example', 5433, 'bazarr')


def test_a_host_in_the_url_query_gets_no_localhost_added():
    url = _resolve('postgresql://u@/bazarr?host=/run/postgresql')

    assert url.host is None
    assert url.query == {'host': '/run/postgresql'}


def test_a_service_entry_gets_nothing_from_config_yaml():
    """Anything set beside a service wins over the service file, so the
    config.yaml defaults sent a service URL to localhost:5432."""
    url = _resolve('postgresql:///?service=bazarr', username='config_user', password='config_password',
                   database='config_database')

    assert (url.username, url.password, url.host, url.port) == (None, None, None, None)
    assert not url.database
    assert url.query == {'service': 'bazarr'}


def test_a_hostaddr_in_the_url_query_gets_no_localhost_added():
    url = _resolve('postgresql://u@/bazarr?hostaddr=10.0.0.5')

    assert (url.host, url.port) == (None, 5432)


@pytest.mark.parametrize('field, variable, value', [
    ('password', 'POSTGRES_PASSWORD', 'env_password'),
    ('host', 'POSTGRES_HOST', 'env.host'),
])
def test_the_environment_also_overrides_the_url_query(field, variable, value):
    """SQLAlchemy lays the query over the URL's fields, so a variable that only
    replaced the field lost to the query in the application's own connection."""
    url = _resolve(f'postgresql://u@db.example/bazarr?{field}=from_query&sslmode=require',
                   environ={variable: value})

    assert getattr(url, field) == value
    assert url.query == {'sslmode': 'require'}


def test_the_environment_port_wins_over_the_port_a_query_host_carries_inline():
    """The dialect reads the port inside a query host entry, host=db:6543, over
    the port the URL itself carries, so a POSTGRES_PORT that replaced the
    URL's port still connected the application to the inline one. The entry
    is rewritten instead, db:6543 to db, the way the port query key is
    replaced, so the port the environment names is the one the connection
    uses. Without POSTGRES_PORT the inline port stays."""
    url = _resolve('postgresql:///?host=db:6543', environ={'POSTGRES_PORT': '7654'})

    assert int(url.port) == 7654
    assert url.query['host'] == 'db'

    url = _resolve('postgresql:///?host=db:6543')

    assert url.query['host'] == 'db:6543'


def test_a_backup_uses_the_query_password_the_application_connects_with(backup_env, monkeypatch):
    _enable_postgresql_by_url(backup_env, monkeypatch,
                              'postgresql://url_user:netloc_password@url.host/url_database?password=query_password')

    connection = backup_env.module._postgres_connection_settings()

    assert connection['password'] == 'query_password'


def test_without_a_url_the_five_fields_resolve_as_before():
    url = _resolve(host='config.example', port=5433, username='config_user', password='config_password',
                   database='config_database', environ={'POSTGRES_DATABASE': 'env_database',
                                                        'POSTGRES_PASSWORD': ''})

    assert url.drivername == 'postgresql+psycopg2'
    # An empty variable still blanks the config.yaml value when there is no URL.
    assert _target(url) == ('config_user', '', 'config.example', 5433, 'env_database')


def test_an_empty_environment_variable_changes_nothing_in_the_url():
    """POSTGRES_HOST= and POSTGRES_PORT= beside the URL were the way around the
    config.yaml defaults, so they still leave the URL alone and keep config.yaml
    out of the parts they name."""
    url = _resolve('postgresql://u:p@db.example/bazarr',
                   environ={'POSTGRES_HOST': '', 'POSTGRES_PORT': ''})

    assert (url.host, url.port) == ('db.example', None)


def test_a_backup_follows_the_same_precedence_for_a_url(backup_env, monkeypatch):
    _enable_postgresql_by_url(backup_env, monkeypatch,
                              'postgresql://url_user:url_password@url.host:6543/url_database')
    monkeypatch.setenv('POSTGRES_HOST', 'env.host')

    connection = backup_env.module._postgres_connection_settings()

    assert (connection['username'], connection['password'], connection['host'], connection['port'],
            connection['database']) == ('url_user', 'url_password', 'env.host', 6543, 'url_database')
