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


def _write_fake_tool(directory, name, exit_code=0, writes_output=False):
    """Put an executable on PATH that records how it was called.

    Returns the path of the log file it appends its argv and PGPASSWORD to.
    """
    os.makedirs(directory, exist_ok=True)
    log_path = os.path.join(directory, f'{name}.log')
    output_arg = ''
    if writes_output:
        # pg_dump is asked for --file <path>; fake the artifact it would write.
        output_arg = 'for i in "$@"; do if [ "$prev" = "--file" ]; then echo "dump" > "$i"; fi; prev="$i"; done'
    script_path = os.path.join(directory, name)
    with open(script_path, 'w', encoding='utf-8') as script:
        script.write(
            '#!/bin/sh\n'
            f'echo "ARGV $*" >> "{log_path}"\n'
            f'echo "PGPASSWORD ${{PGPASSWORD:-unset}}" >> "{log_path}"\n'
            f'{output_arg}\n'
            f'exit {exit_code}\n'
        )
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return log_path


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
    monkeypatch.setitem(sys.modules, 'app.server', SimpleNamespace(webserver=None))

    tools_dir = tmp_path / 'bin'
    tools_dir.mkdir()
    original_path = os.environ.get('PATH', '')
    monkeypatch.setenv('PATH', str(tools_dir))
    monkeypatch.delenv('PGPASSWORD', raising=False)
    for variable in ('POSTGRES_DATABASE', 'POSTGRES_USERNAME', 'POSTGRES_PASSWORD',
                     'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_URL'):
        monkeypatch.delenv(variable, raising=False)

    return SimpleNamespace(module=backup_module, settings=settings, config_dir=config_dir,
                           tools_dir=tools_dir, restarts=restarts, original_path=original_path,
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
