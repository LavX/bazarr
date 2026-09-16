# coding=utf-8

import os
import sqlite3
import shutil
import subprocess
import logging
import re

from contextlib import closing
from datetime import datetime, timedelta, timezone
from zipfile import ZipFile, BadZipFile, ZIP_DEFLATED
from glob import glob

from app.get_args import args
from app.config import settings
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from utilities.central import restart_bazarr

_BACKUP_FILENAME_RE = re.compile(r'^bazarr_backup_v[\w.\-]+\.zip$')

# Name the database artifact carries inside the archive, one per engine: a
# SQLite backup is the database file itself, a PostgreSQL backup is a pg_dump
# custom-format archive. Both names are stable, and restore looks for the one
# belonging to the engine this instance is configured for.
SQLITE_ARCHIVE_NAME = 'bazarr.db'
POSTGRES_ARCHIVE_NAME = 'bazarr_postgres.dump'

# Where a dump is parked when pg_restore failed partway. The name is outside
# everything restore looks for, so the next start neither applies it nor
# deletes it, and the operator still has the extracted file to retry from.
POSTGRES_FAILED_ARCHIVE_NAME = f'{POSTGRES_ARCHIVE_NAME}.failed'

# pg_restore keeps going past an error and reports how many it ignored, and it
# exits non-zero when it did. One error class is benign: a dump written by a
# client newer than the server carries SET statements for parameters the server
# has never heard of (transaction_timeout, added in PostgreSQL 17, is the one
# people meet). The data restores correctly, so those are reported as a warning
# and everything else is still a hard failure.
_PG_BENIGN_ERROR_MARKER = 'unrecognized configuration parameter'

# PostgreSQL backup and restore shell out to the client tools. There is no
# in-process equivalent: psycopg speaks the wire protocol, not pg_dump's custom
# format, and a hand-rolled dump that restores cleanly is not something to
# maintain here.
_PG_TOOLS_HINT = ('Install the PostgreSQL client tools and make sure they are on PATH: the '
                  'postgresql-client package on Debian and Ubuntu, postgresql-client on Alpine. '
                  'The Bazarr+ Docker image ships them.')


class BackupError(Exception):
    """A backup or restore step failed, so the operation must not continue."""
    pass


def _postgres_enabled():
    """Whether this instance runs on PostgreSQL, the way app.database decides it.

    POSTGRES_ENABLED wins over the configuration file there, so it has to win
    here too: an instance configured entirely through the environment would
    otherwise be backed up as if it were SQLite, shipping an empty db/bazarr.db
    and restoring that empty file over nothing while the real database sat
    untouched.
    """
    postgres_enabled_env = os.getenv('POSTGRES_ENABLED')
    if postgres_enabled_env:
        return postgres_enabled_env.lower() == 'true'
    return bool(settings.postgresql.enabled)


def _postgres_connection_settings():
    """Resolve the PostgreSQL connection the application itself uses.

    Mirrors app.database: the POSTGRES_* environment variables win over
    config.yaml, and a connection URL fills in whatever the individual keys
    leave empty.
    """
    connection = {
        'host': os.getenv('POSTGRES_HOST', settings.postgresql.host),
        'port': os.getenv('POSTGRES_PORT', settings.postgresql.port),
        'database': os.getenv('POSTGRES_DATABASE', settings.postgresql.database),
        'username': os.getenv('POSTGRES_USERNAME', settings.postgresql.username),
        'password': os.getenv('POSTGRES_PASSWORD', settings.postgresql.password),
    }

    postgres_url = os.getenv('POSTGRES_URL', settings.postgresql.url)
    if postgres_url:
        from sqlalchemy.engine import make_url
        try:
            parsed_url = make_url(postgres_url)
        except Exception as error:
            # Raised rather than allowed out: restore_from_backup runs during
            # boot, and an unparseable URL must fail the backup, not the start.
            raise BackupError(f'The configured PostgreSQL connection URL cannot be parsed: {error}') from error
        for key, value in (('host', parsed_url.host), ('port', parsed_url.port),
                           ('database', parsed_url.database), ('username', parsed_url.username),
                           ('password', parsed_url.password)):
            if not connection[key]:
                connection[key] = value

    return connection


def _postgres_connection_arguments(connection):
    """Command line arguments naming the database, without the password.

    --no-password is what keeps a missing or wrong password a failure instead
    of a client tool blocking forever on a prompt nobody can answer.
    """
    arguments = []
    if connection['host']:
        arguments += ['--host', str(connection['host'])]
    if connection['port']:
        arguments += ['--port', str(connection['port'])]
    if connection['username']:
        arguments += ['--username', str(connection['username'])]
    arguments += ['--no-password', '--dbname', str(connection['database'])]
    return arguments


def _run_postgres_tool(tool_name, command, connection):
    """Run a PostgreSQL client tool and raise BackupError unless it succeeds.

    The password goes through PGPASSWORD rather than the command line or the
    connection URL: every process on the host can read another process' command
    line.
    """
    environment = os.environ.copy()
    if connection['password']:
        environment['PGPASSWORD'] = str(connection['password'])
    else:
        environment.pop('PGPASSWORD', None)

    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    except OSError as error:
        raise BackupError(f'Unable to run {tool_name}: {error}') from error

    if result.returncode != 0:
        raise BackupError(f'{tool_name} exited with code {result.returncode}: '
                          f'{result.stderr.strip() or "no output"}')
    return result


def _postgres_error_lines(tool_output):
    """The error lines a PostgreSQL client tool reported, warnings excluded."""
    return [line.strip() for line in (tool_output or '').splitlines()
            if ': error:' in line or line.strip().startswith('ERROR:')]


def _errors_are_only_unknown_parameters(tool_output):
    """Whether every error reported is an unknown server parameter, and there is one.

    That is the one class a newer client produces against an older server: the
    dump sets a parameter the server does not have. Anything else, and anything
    mixed in with it, stays a failure.
    """
    error_lines = _postgres_error_lines(tool_output)
    if not error_lines:
        return False
    return all(_PG_BENIGN_ERROR_MARKER in line for line in error_lines)


def _postgres_tool_major(tool_path):
    """Major version a client tool reports, as a string, or None."""
    try:
        result = subprocess.run([tool_path, '--version'], capture_output=True, text=True, check=False)
    except OSError:
        return None
    match = re.search(r'(\d+)', result.stdout or '')
    return match.group(1) if match else None


def _postgres_server_major(connection):
    """Major version of the configured server, as a string, or None.

    Best effort and only used to word a warning, so every failure here is
    answered with None rather than an exception.
    """
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(host=connection['host'] or None,
                             port=int(connection['port']) if connection['port'] else None,
                             dbname=connection['database'],
                             user=connection['username'] or None,
                             password=str(connection['password']) if connection['password'] else None,
                             connect_timeout=5) as server_connection:
            return str(server_connection.info.server_version // 10000)
    except Exception:
        return None


def _dump_postgres_database(dest_path):
    """Write a pg_dump custom-format archive of the configured database."""
    pg_dump = shutil.which('pg_dump')
    if pg_dump is None:
        raise BackupError(f'pg_dump was not found on PATH. {_PG_TOOLS_HINT}')

    connection = _postgres_connection_settings()
    if not connection['database']:
        raise BackupError('No PostgreSQL database name is configured, so there is nothing to back up.')

    # Custom format rather than plain SQL: it is what pg_restore reads, and it
    # is compressed and selective on the way back in.
    command = [pg_dump, '--format=custom', '--file', dest_path] + _postgres_connection_arguments(connection)
    logging.debug('Dumping PostgreSQL database %s to %s', connection['database'], dest_path)
    _run_postgres_tool('pg_dump', command, connection)


def _restore_postgres_database(dump_path):
    """Load a pg_dump custom-format archive back into the configured database."""
    pg_restore = shutil.which('pg_restore')
    if pg_restore is None:
        raise BackupError(f'pg_restore was not found on PATH. {_PG_TOOLS_HINT}')

    connection = _postgres_connection_settings()
    if not connection['database']:
        raise BackupError('No PostgreSQL database name is configured, so there is nothing to restore into.')

    # --clean --if-exists drops what is already there first, --no-owner keeps
    # the restore working when the backup was taken as a different role. No
    # --exit-on-error: pg_restore already exits non-zero when it ignored an
    # error, so the flag buys no extra loudness and would only stop halfway
    # instead of at the end.
    command = ([pg_restore, '--clean', '--if-exists', '--no-owner'] +
               _postgres_connection_arguments(connection) + [dump_path])
    logging.debug('Restoring PostgreSQL database %s from %s', connection['database'], dump_path)
    try:
        _run_postgres_tool('pg_restore', command, connection)
    except BackupError as error:
        if not _errors_are_only_unknown_parameters(str(error)):
            raise
        logging.warning('pg_restore reported only unknown server parameters, which a dump written by a '
                        'newer client always sets on an older server. The data was restored. Client major '
                        'version is %s, server major version is %s; keeping the client at or above the '
                        'server version removes this warning. Reported: %s',
                        _postgres_tool_major(pg_restore) or 'unknown',
                        _postgres_server_major(connection) or 'unknown',
                        '; '.join(_postgres_error_lines(str(error))))


def _copy_sqlite_database(src_path, dest_path):
    """Copy the live SQLite database to `dest_path` with the sqlite3 backup API."""
    try:
        with closing(sqlite3.connect(src_path)) as database_src_con, \
                closing(sqlite3.connect(dest_path)) as database_backup_con:
            with database_backup_con:
                database_src_con.backup(database_backup_con)
    except Exception as error:
        raise BackupError(f'Unable to copy the SQLite database file {src_path}: {error}') from error


def _delete_file(path):
    """Delete a file we own, without making a missing one an error."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logging.exception(f'Unable to delete {path}')  # noqa: G004


def _database_archive_name():
    return POSTGRES_ARCHIVE_NAME if _postgres_enabled() else SQLITE_ARCHIVE_NAME


def _wrong_engine_archive_message():
    """Say what a complete archive from the other engine holds, or None.

    A backup taken on the other engine is not a partial backup, it is a
    complete one this instance cannot use, and saying so is the difference
    between a puzzle and an answer.
    """
    if _postgres_enabled():
        present, holds = SQLITE_ARCHIVE_NAME, f'an SQLite database ({SQLITE_ARCHIVE_NAME})'
        engine, expected = 'PostgreSQL', POSTGRES_ARCHIVE_NAME
    else:
        present, holds = POSTGRES_ARCHIVE_NAME, f'a PostgreSQL dump ({POSTGRES_ARCHIVE_NAME})'
        engine, expected = 'SQLite', SQLITE_ARCHIVE_NAME
    if not os.path.isfile(os.path.join(get_restore_path(), present)):
        return None
    return f'This backup holds {holds}; this instance runs {engine} and needs {expected}.' 


def _clear_restore_directory():
    """Drop whatever an extracted archive left in the restore directory.

    Anything left there is picked up again on the next boot, which is how one
    failed restore becomes a restore loop.
    """
    for name in ('config.yaml', 'config.ini', SQLITE_ARCHIVE_NAME, POSTGRES_ARCHIVE_NAME,
                 POSTGRES_FAILED_ARCHIVE_NAME):
        _delete_file(os.path.join(get_restore_path(), name))


def _backup_prerequisites_met():
    """Refuse to start a backup that could not include the database.

    Only PostgreSQL has a prerequisite outside this process. An archive holding
    the configuration alone is not a backup: restore refuses it as partial, and
    the operator finds out the day they need it.
    """
    if not _postgres_enabled():
        return True
    if shutil.which('pg_dump') is None:
        logging.error('Cannot back up the PostgreSQL database: pg_dump was not found on PATH, so no '
                      'backup file was written. %s', _PG_TOOLS_HINT)
        return False
    return True


def _validate_backup_filename(filename):
    """Return a safe filename if `filename` is a plain backup file name, else None."""
    if not filename:
        return None
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        return None
    if not _BACKUP_FILENAME_RE.match(safe_name):
        return None
    return safe_name


def _safe_extract(zip_obj, dest_path):
    """Extract a ZIP archive while rejecting entries that escape `dest_path`."""
    dest_real = os.path.realpath(dest_path)
    for member in zip_obj.namelist():
        member_real = os.path.realpath(os.path.join(dest_real, member))
        if member_real != dest_real and not member_real.startswith(dest_real + os.sep):
            raise BadZipFile(f'Unsafe path in backup archive: {member}')
    zip_obj.extractall(path=dest_path)


def get_backup_path():
    backup_dir = settings.backup.folder
    if not os.path.isdir(backup_dir):
        os.makedirs(backup_dir)
    logging.debug(f'Backup directory path is: {backup_dir}')  # noqa: G004
    return backup_dir


def get_restore_path():
    restore_dir = os.path.join(args.config_dir, 'restore')
    if not os.path.isdir(restore_dir):
        os.makedirs(restore_dir)
    logging.debug(f'Restore directory path is: {restore_dir}')  # noqa: G004
    return restore_dir


def get_backup_files(fullpath=True):
    backup_file_pattern = os.path.join(get_backup_path(), 'bazarr_backup_v*.zip')
    file_list = glob(backup_file_pattern)
    file_list.sort(key=os.path.getmtime, reverse=True)
    if fullpath:
        return file_list
    else:
        return [{
            'type': 'backup',
            'filename': os.path.basename(x),
            'size': sizeof_fmt(os.path.getsize(x)),
            'date': datetime.fromtimestamp(os.path.getmtime(x)).strftime("%b %d %Y")
        } for x in file_list]


def backup_to_zip(job_id=None, wait_for_completion=False):
    if not job_id:
        # Checked before the job exists so the caller that asked for a backup
        # is told why it did not happen, instead of a queued job dying out of
        # band. No local variable may be introduced in this branch:
        # add_job_from_function binds the caller's locals to its signature.
        if not _backup_prerequisites_met():
            return False
        jobs_queue.add_job_from_function("Backing up Database and Configuration File", is_progress=False,
                                         wait_for_completion=wait_for_completion)
        return True

    now = datetime.now()
    database_backup_file = None
    now_string = now.strftime("%Y.%m.%d_%H.%M.%S")
    backup_filename = f"bazarr_backup_v{os.environ['BAZARR_VERSION']}_{now_string}.zip"
    logging.debug(f'Backup filename will be: {backup_filename}')  # noqa: G004

    # Either engine: the database goes in or nothing is written. An archive
    # carrying the configuration alone reads as a backup in the list and is
    # refused on the way back in, which the operator discovers on the day they
    # need it.
    if _postgres_enabled():
        database_backup_file = os.path.join(get_backup_path(), 'bazarr_temp.dump')
        logging.debug(f'Database will be dumped with pg_dump to: {database_backup_file}')  # noqa: G004

        try:
            _dump_postgres_database(database_backup_file)
        except BackupError:
            _delete_file(database_backup_file)
            logging.error('Unable to back up the PostgreSQL database, no backup file was written.')
            raise
    else:
        database_src_file = os.path.join(args.config_dir, 'db', 'bazarr.db')
        logging.debug(f'Database file path to backup is: {database_src_file}')  # noqa: G004
        database_backup_file = os.path.join(get_backup_path(), 'bazarr_temp.db')

        try:
            _copy_sqlite_database(database_src_file, database_backup_file)
        except BackupError:
            _delete_file(database_backup_file)
            logging.error('Unable to back up the SQLite database, no backup file was written.')
            raise

    config_file = os.path.join(args.config_dir, 'config', 'config.yaml')
    logging.debug(f'Config file path to backup is: {config_file}')  # noqa: G004

    backup_file_path = os.path.join(get_backup_path(), backup_filename)
    try:
        with ZipFile(backup_file_path, 'w', compression=ZIP_DEFLATED, compresslevel=9) as backupZip:
            backupZip.write(database_backup_file, _database_archive_name())
            backupZip.write(config_file, 'config.yaml')
    except Exception:
        # Half an archive is worse than none: it lists as a backup and refuses
        # to restore.
        _delete_file(backup_file_path)
        logging.error('Unable to write the backup archive, no backup file was written.')
        raise
    finally:
        _delete_file(database_backup_file)

    jobs_queue.update_job_name(job_id=job_id, new_job_name="Backed up Database and Configuration File")
    event_stream(type='backup')
    return True


def _restore_database(restore_database_path, dest_database_path):
    """Put the backed up database back, raising BackupError if it cannot be done."""
    if _postgres_enabled():
        _restore_postgres_database(restore_database_path)
        return

    try:
        shutil.copy(restore_database_path, dest_database_path)
    except (OSError, shutil.Error) as error:
        raise BackupError(f'Unable to restore the database to {dest_database_path}: {error}') from error

    # The restored file is a complete database, so the write-ahead log and
    # shared-memory files sitting next to it describe one that no longer exists.
    for suffix in ('-shm', '-wal'):
        _delete_file(f'{dest_database_path}{suffix}')


def restore_from_backup():
    if os.path.isfile(os.path.join(get_restore_path(), 'config.yaml')):
        restore_config_path = os.path.join(get_restore_path(), 'config.yaml')
        dest_config_path = os.path.join(args.config_dir, 'config', 'config.yaml')
    else:
        restore_config_path = os.path.join(get_restore_path(), 'config.ini')
        dest_config_path = os.path.join(args.config_dir, 'config', 'config.ini')

    # The database artifact is named after the engine this instance runs on: a
    # SQLite install restores the database file, a PostgreSQL install restores
    # the pg_dump archive.
    restore_database_path = os.path.join(get_restore_path(), _database_archive_name())
    dest_database_path = os.path.join(args.config_dir, 'db', SQLITE_ARCHIVE_NAME)

    config_present = os.path.isfile(restore_config_path)
    database_present = os.path.isfile(restore_database_path)

    if not config_present and not database_present:
        logging.debug('No backup to restore.')
        return False

    if not (config_present and database_present):
        wrong_engine = _wrong_engine_archive_message()
        if wrong_engine:
            logging.error('%s It cannot be restored here.', wrong_engine)
        else:
            logging.error('Cannot restore a partial backup. You must have both config and database.')
        _clear_restore_directory()
        return False

    # Stage the configuration before touching the database. Copying it is what
    # proves the destination is readable and writable, and the os.replace at
    # the end makes the swap itself atomic, so a database step that fails
    # cannot leave a new configuration beside the old database.
    staged_config_path = f'{dest_config_path}.restore'
    try:
        shutil.copy(restore_config_path, staged_config_path)
    except (OSError, shutil.Error):
        logging.exception('Restoring the backup failed. Bazarr will not restart, and the files staged '
                          'for the restore have been discarded so the next start does not retry it '
                          'silently. The backup archive itself is untouched.')
        _delete_file(staged_config_path)
        _clear_restore_directory()
        return False

    try:
        _restore_database(restore_database_path, dest_database_path)
    except (BackupError, OSError, shutil.Error):
        _delete_file(staged_config_path)
        if _postgres_enabled():
            # pg_restore drops the existing objects before it loads the new
            # ones and there is no transaction around that, so a failure
            # partway leaves the database in neither state. Saying "nothing was
            # changed" here would be a lie the operator acts on.
            failed_dump_path = os.path.join(get_restore_path(), POSTGRES_FAILED_ARCHIVE_NAME)
            try:
                os.replace(restore_database_path, failed_dump_path)
            except OSError:
                failed_dump_path = restore_database_path
            logging.exception('Restoring the PostgreSQL database failed partway. pg_restore drops the '
                              'existing objects before it loads the new ones and cannot undo that, so '
                              'this database may now be partially restored and must not be used until '
                              'it has been restored again or rebuilt. Bazarr will not restart. The '
                              'extracted dump has been kept at %s and the backup archive it came from '
                              'is still in %s, so the restore can be retried.',
                              failed_dump_path, get_backup_path())
            _delete_file(restore_config_path)
        else:
            logging.exception('Restoring the backup failed. Bazarr will not restart, and the files '
                              'staged for the restore have been discarded so the next start does not '
                              'retry it silently. The backup archive itself is untouched.')
            _clear_restore_directory()
        return False

    try:
        os.replace(staged_config_path, dest_config_path)
    except OSError:
        logging.exception('The database was restored but the configuration could not be put in place, '
                          'so Bazarr will not restart. The restored database is complete; the '
                          'configuration is the one that was already here.')
        _delete_file(staged_config_path)
        _clear_restore_directory()
        return False

    _clear_restore_directory()

    logging.info('Backup restored successfully. Bazarr will restart.')
    from app.server import webserver
    if webserver is not None:
        webserver.close_all()
    restart_bazarr()
    return True


def prepare_restore(filename):
    filename = _validate_backup_filename(filename)
    if filename is None:
        logging.error('Invalid backup filename refused for restore.')
        return False
    src_zip_file_path = os.path.join(get_backup_path(), filename)
    dest_zip_file_path = os.path.join(get_restore_path(), filename)
    success = False
    try:
        shutil.copy(src_zip_file_path, dest_zip_file_path)
    except (OSError, FileNotFoundError):
        logging.exception(f'Unable to copy backup archive to {dest_zip_file_path}')  # noqa: G004
    else:
        try:
            with ZipFile(dest_zip_file_path, 'r') as zipObj:
                _safe_extract(zipObj, get_restore_path())
        except (BadZipFile, OSError):
            logging.exception(f'Unable to extract files from backup archive {dest_zip_file_path}')  # noqa: G004
        else:
            success = True

        if success and not os.path.isfile(os.path.join(get_restore_path(), _database_archive_name())):
            # Refused here rather than after the restart: the restore itself
            # runs at the next start, and a restart that only finds out then
            # costs the user a bounce for nothing.
            wrong_engine = _wrong_engine_archive_message()
            if wrong_engine:
                logging.error('Backup archive %s cannot be restored here. %s', filename, wrong_engine)
            else:
                logging.error('Backup archive %s does not contain the database for this instance, so it '
                              'cannot be restored.', filename)
            success = False

    if not success:
        # A failed extraction can still have written part of the archive, and
        # anything left in the restore directory is applied at the next start.
        _clear_restore_directory()

    try:
        os.remove(dest_zip_file_path)
    except (OSError, FileNotFoundError):
        logging.exception(f'Unable to delete backup archive {dest_zip_file_path}')  # noqa: G004

    if success:
        logging.debug('time to restart')
        from app.server import webserver
        if webserver is not None:
            webserver.close_all()
        restart_bazarr()

    return success


def backup_rotation():
    backup_retention = settings.backup.retention
    try:
        int(backup_retention)
    except ValueError:
        logging.error('Backup retention time must be a valid integer. Please fix this in your settings.')
        return

    backup_files = get_backup_files()

    logging.debug(f'Cleaning up backup files older than {backup_retention} days')  # noqa: G004
    for file in backup_files:
        if (datetime.fromtimestamp(os.path.getmtime(file), tz=timezone.utc) + timedelta(days=int(backup_retention)) <
                datetime.now(tz=timezone.utc)):
            logging.debug(f'Deleting old backup file {file}')  # noqa: G004
            try:
                os.remove(file)
            except (OSError, FileNotFoundError):
                logging.debug(f'Unable to delete backup file {file}')  # noqa: G004
    logging.debug('Finished cleaning up old backup files')


def delete_backup_file(filename):
    filename = _validate_backup_filename(filename)
    if filename is None:
        logging.error('Invalid backup filename refused for deletion.')
        return False
    backup_file_path = os.path.join(get_backup_path(), filename)
    try:
        os.remove(backup_file_path)
        return True
    except (OSError, FileNotFoundError):
        logging.debug(f'Unable to delete backup file {backup_file_path}')  # noqa: G004
    return False


def sizeof_fmt(num, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1000.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1000.0
    return f"{num:.1f} Y{suffix}"
