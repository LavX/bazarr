# coding=utf-8

import ast
import configparser
import hashlib
import json
import os
import sqlite3
import shutil
import subprocess
import logging
import re
import yaml

from contextlib import closing
from datetime import datetime, timedelta, timezone
from zipfile import ZipFile, BadZipFile, ZIP_DEFLATED
from glob import glob
from pathlib import Path, PureWindowsPath

from dynaconf import Dynaconf

from provider_hub import datadir, secret_paths

from app.get_args import args
from app.config import settings
from app.postgres_url import postgres_engine_url
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from literals import EXIT_RESTORE_ERROR
from utilities.central import restart_bazarr, stop_bazarr

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
_PLUGIN_RESTORE_PENDING = "pending_plugin_data"
_RESTORE_HANDOFF = ".restore-journal.json"
_APPLIED_PLUGIN_DATA = ".applied_plugin_data"
_DATABASE_STARTED = "database-started"
_DATABASE_COMPLETE = "database-complete"
_DATA_READY = "data-ready"
_PG_DESTINATION_QUERY = (
    "SELECT datname, oid, inet_server_addr()::text, inet_server_port(), current_setting('port'), "
    "current_setting('search_path') FROM pg_catalog.pg_database WHERE datname = current_database()"
)

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


class PostgresRestoreStartedError(BackupError):
    """pg_restore ran and failed, so the target database is no longer provably intact.

    Raised only once the process has been launched. Everything that goes wrong
    before that (no client tools, no database configured, an unparseable
    connection URL) leaves the database untouched, and telling an operator
    their database might be corrupt when it demonstrably is not sends them
    rebuilding it for nothing.
    """
    pass


class PostgresUnreachableError(BackupError):
    """The database refused a connection, so pg_restore was never started.

    Nothing was changed, and the same restore can succeed once the database
    answers, so the staged files are kept for the next start.
    """
    pass


class RestoreConnectionMismatch(BackupError):
    """The archived config cannot start Bazarr on the database receiving the backup."""


def _postgres_enabled(configuration=None):
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
    configuration = settings if configuration is None else configuration
    return bool(configuration.postgresql.enabled)


def _split_query_host_entries(value):
    """The query's host entries, each split from the port it may carry inline.

    A URL can name its hosts in the query string with the port inside the
    entry, the way postgresql:///?host=primary:5432&host=standby:5433 does.
    The application's dialect splits those into host='primary,standby' and
    port='5432,5433' before libpq sees them, so the same split is made here: a
    conninfo host list that keeps the inline ports, host=primary:5432,standby:5433,
    is not a form libpq reads, and the reachability check, pg_dump and
    pg_restore would all fail against a database the application connects to.

    An entry is split on its last colon, and only when what follows is digits
    or nothing, so an IPv6 address is not taken for a host and a port.

    Returns (hosts, ports): the hosts in order, and the port of each entry in
    the same order, None for an entry that carries none.
    """
    entries = value if isinstance(value, tuple) else value.split(',')
    hosts, ports = [], []
    for entry in entries:
        host, colon, port = entry.rpartition(':')
        if colon and (port.isdigit() or not port):
            hosts.append(host)
            ports.append(port)
        else:
            hosts.append(entry)
            ports.append(None)
    return hosts, ports


def _postgres_connection_settings(configuration=None):
    """Resolve the PostgreSQL connection the application itself uses.

    Goes through the application's own resolver, so a backup reaches the same
    database the instance runs on.
    """
    try:
        url = postgres_engine_url(settings if configuration is None else configuration)
    except Exception as error:
        # Raised rather than allowed out: restore_from_backup runs during
        # boot, and an unparseable URL must fail the backup, not the start.
        raise BackupError(f'The configured PostgreSQL connection URL cannot be parsed: {error}') from error

    # libpq options from the URL's query string, such as sslmode, sslcert or
    # service. The application's own connection uses them. A host entry's
    # inline port is split off the way the dialect splits it, so the conninfo
    # holds the host list and the port list libpq reads.
    options = {key: ','.join(value) if isinstance(value, tuple) else value
               for key, value in url.query.items() if key != 'host'}
    if 'host' in url.query:
        query_hosts, query_ports = _split_query_host_entries(url.query['host'])
        options['host'] = ','.join(query_hosts)
        if any(port is not None for port in query_ports):
            # The ports the entries carry win over the port the URL itself
            # holds, as they do for the application's own connection.
            options['port'] = ','.join(port or '' for port in query_ports)
    # A password in the query goes the way of every other password, through
    # PGPASSWORD, never onto the command line. It wins over the URL's own
    # password, as it does for the application's connection.
    query_password = options.pop('password', None)
    return {
        'host': url.host,
        'port': url.port,
        'database': url.database,
        'username': url.username,
        'password': query_password or url.password,
        'options': options,
    }


def _postgres_names_database(connection):
    """Whether the connection names a database, in its fields, its query or a service entry."""
    options = connection['options']
    return bool(connection['database'] or options.get('dbname') or options.get('service'))


def _postgres_conninfo(connection):
    """A libpq connection string for the connection, without the password."""
    parameters = {'host': connection['host'], 'port': connection['port'],
                  'dbname': connection['database'], 'user': connection['username']}
    # The query wins, as it does for the application's own connection.
    parameters.update(connection['options'])
    return ' '.join(
        "{}='{}'".format(key, str(value).replace('\\', '\\\\').replace("'", "\\'"))
        for key, value in parameters.items() if value not in (None, ''))


def _postgres_connection_arguments(connection):
    """Command line arguments naming the database, without the password.

    --no-password is what keeps a missing or wrong password a failure instead
    of a client tool blocking forever on a prompt nobody can answer.
    """
    if connection['options']:
        # Five separate fields would drop the URL's query options, and with
        # them client certificates, verify-full and service entries.
        return ['--no-password', '--dbname', _postgres_conninfo(connection)]
    arguments = []
    if connection['host']:
        arguments += ['--host', str(connection['host'])]
    if connection['port']:
        arguments += ['--port', str(connection['port'])]
    if connection['username']:
        arguments += ['--username', str(connection['username'])]
    arguments += ['--no-password', '--dbname', str(connection['database'])]
    return arguments


def _run_postgres_tool(tool_name, command, connection, ran_error=BackupError):
    """Run a PostgreSQL client tool and raise unless it succeeds.

    The password goes through PGPASSWORD rather than the command line or the
    connection URL: every process on the host can read another process' command
    line.

    `ran_error` is the class used when the tool ran and failed, so a caller
    whose tool changes the database as it goes can tell that apart from a tool
    that never started.
    """
    environment = os.environ.copy()
    if connection['password']:
        environment['PGPASSWORD'] = str(connection['password'])
    else:
        environment.pop('PGPASSWORD', None)

    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    except OSError as error:
        # The process never started, so nothing it would have done happened.
        raise BackupError(f'Unable to run {tool_name}: {error}') from error

    if result.returncode != 0:
        raise ran_error(f'{tool_name} exited with code {result.returncode}: '
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

    The image ships psycopg 2 (postgres-requirements.txt), and CI installs the
    same file. psycopg 3 answers only where psycopg 2 is missing, as on a
    source install that chose it. The connection is made the way the
    reachability check makes it, so the URL's query options apply here too.
    """
    try:
        try:
            import psycopg2 as driver
        except ImportError:
            import psycopg as driver
        arguments = {'connect_timeout': 5}
        if connection['password']:
            arguments['password'] = str(connection['password'])
        # A psycopg 2 connection used as a context manager ends the transaction
        # and stays open, so it is closed explicitly.
        with closing(driver.connect(_postgres_conninfo(connection), **arguments)) as server_connection:
            return str(server_connection.info.server_version // 10000)
    except Exception:
        return None


def _check_postgres_reachable(connection):
    """Raise PostgresUnreachableError unless the database takes a connection.

    Uses the settings pg_restore gets. pg_restore exits non-zero when it cannot
    connect, the same as when it fails partway, and only the second one can
    have changed the database.
    """
    try:
        try:
            import psycopg2 as driver
        except ImportError:
            import psycopg as driver
    except ImportError:
        return
    arguments = {'connect_timeout': 10}
    if connection['password']:
        arguments['password'] = str(connection['password'])
    try:
        with closing(driver.connect(_postgres_conninfo(connection), **arguments)) as database:
            with database.cursor() as cursor:
                cursor.execute(_PG_DESTINATION_QUERY)
                return _fingerprint(cursor.fetchone())
    except Exception as error:
        raise PostgresUnreachableError(
            f'The PostgreSQL database could not be reached: {str(error).strip()}') from error


def _dump_postgres_database(dest_path):
    """Write a pg_dump custom-format archive of the configured database."""
    pg_dump = shutil.which('pg_dump')
    if pg_dump is None:
        raise BackupError(f'pg_dump was not found on PATH. {_PG_TOOLS_HINT}')

    connection = _postgres_connection_settings()
    if not _postgres_names_database(connection):
        raise BackupError('No PostgreSQL database name is configured, so there is nothing to back up.')

    # Custom format rather than plain SQL: it is what pg_restore reads, and it
    # is compressed and selective on the way back in.
    command = [pg_dump, '--format=custom', '--file', dest_path] + _postgres_connection_arguments(connection)
    logging.debug('Dumping PostgreSQL database %s to %s', connection['database'], dest_path)
    _run_postgres_tool('pg_dump', command, connection)


def _restore_postgres_database(dump_path, before_restore):
    """Load a pg_dump custom-format archive back into the configured database."""
    pg_restore = shutil.which('pg_restore')
    if pg_restore is None:
        raise BackupError(f'pg_restore was not found on PATH. {_PG_TOOLS_HINT}')

    connection = _postgres_connection_settings()
    if not _postgres_names_database(connection):
        raise BackupError('No PostgreSQL database name is configured, so there is nothing to restore into.')
    destination = _check_postgres_reachable(connection)

    # --clean --if-exists drops what is already there first, --no-owner keeps
    # the restore working when the backup was taken as a different role. No
    # --exit-on-error: pg_restore already exits non-zero when it ignored an
    # error, so the flag buys no extra loudness and would only stop halfway
    # instead of at the end.
    command = ([pg_restore, '--clean', '--if-exists', '--no-owner'] +
               _postgres_connection_arguments(connection) + [dump_path])
    logging.debug('Restoring PostgreSQL database %s from %s', connection['database'], dump_path)
    before_restore(destination)
    try:
        _run_postgres_tool('pg_restore', command, connection, ran_error=PostgresRestoreStartedError)
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
                 POSTGRES_FAILED_ARCHIVE_NAME, secret_paths.HISTORY_ARCHIVE, datadir.OMISSIONS_ARCHIVE):
        _delete_file(os.path.join(get_restore_path(), name))
    try:
        datadir.remove(Path(get_restore_path()) / datadir.ARCHIVE_DIR)
    except OSError:
        logging.warning('Could not clear the staged plugin data from the restore directory.', exc_info=True)


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
    internal = tuple(os.path.join(dest_real, name) for name in (
        _PLUGIN_RESTORE_PENDING, _RESTORE_HANDOFF, _RESTORE_HANDOFF + ".tmp", _APPLIED_PLUGIN_DATA))
    for member in zip_obj.namelist():
        if member.startswith(datadir.ARCHIVE_DIR + "/"):
            parts = member.rstrip("/").split("/")
            if (len(parts) < 3 or "\\" in member
                    or any(part in ("", ".", "..") or PureWindowsPath(part).drive for part in parts)):
                raise BadZipFile(f'Unsafe path in backup archive: {member}')
        member_real = os.path.realpath(os.path.join(dest_real, member))
        if any(member_real == path or member_real.startswith(path + os.sep) for path in internal):
            raise BadZipFile('Backup archive contains an internal restore path')
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

    with datadir.release_lock:
        return _write_backup(job_id)


def _write_backup(job_id):
    now = datetime.now()
    database_backup_file = None
    now_string = now.strftime("%Y.%m.%d_%H.%M.%S")
    backup_filename = f"bazarr_backup_v{os.environ['BAZARR_VERSION']}_{now_string}.zip"
    logging.debug(f'Backup filename will be: {backup_filename}')  # noqa: G004

    # Either engine: the database goes in or nothing is written. An archive
    # carrying the configuration alone reads as a backup in the list and is
    # refused on the way back in, which the operator discovers on the day they
    # need it.
    #
    # The staging file is named after the job: a scheduled and a manual backup
    # are separate jobs that can run at the same time, and with one shared name
    # each could overwrite or delete the file the other was about to archive.
    if _postgres_enabled():
        database_backup_file = os.path.join(get_backup_path(), f'bazarr_temp_{job_id}.dump')
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
        database_backup_file = os.path.join(get_backup_path(), f'bazarr_temp_{job_id}.db')

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
            datadir.write_backup(backupZip)
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


def _restore_database(restore_database_path, dest_database_path, before_restore):
    """Put the backed up database back, raising BackupError if it cannot be done."""
    if _postgres_enabled():
        _restore_postgres_database(restore_database_path, before_restore)
        return

    destination = Path(dest_database_path)
    staged = destination.with_suffix('.db.restore')
    try:
        shutil.copy(restore_database_path, staged)
        datadir.sync_file(staged)
        before_restore(None)
        os.replace(staged, destination)
        # The WAL and shared-memory files describe the prior database.
        for suffix in ('-shm', '-wal'):
            destination.with_name(destination.name + suffix).unlink(missing_ok=True)
        datadir.sync_directory(destination.parent)
    except (OSError, shutil.Error) as error:
        raise BackupError(f'Unable to restore the database to {dest_database_path}: {error}') from error
    finally:
        _delete_file(staged)


def _refuse_start_after_failed_restore(failed_dump_path):
    """Stop the start while a dump that pg_restore failed on is still parked.

    That file is the only sign left that the database may be half restored:
    the configuration beside it is gone, so without this check the next start
    finds nothing to restore and runs on the damaged database.
    """
    logging.critical('An earlier restore of the PostgreSQL database failed partway, so the database '
                     'may be partially restored, and Bazarr will not start on it. The dump that was '
                     'being restored is kept at %s. To retry the restore from Bazarr, extract the '
                     'backup archive it came from (in %s) into %s, delete %s, and start Bazarr. To '
                     'restore the database yourself instead, run pg_restore --clean --if-exists '
                     '--no-owner --dbname <database> %s, then delete that file and start Bazarr.',
                     failed_dump_path, get_backup_path(), get_restore_path(), failed_dump_path,
                     failed_dump_path)
    stop_bazarr(EXIT_RESTORE_ERROR)


def _validate_restored_database_connection(path):
    """Compare what boot will open after the swap with the connection receiving the dump."""
    from app.config import validators
    from secret_store.crypto import decrypt_secret, is_encrypted

    try:
        archived = Dynaconf(settings_file=path, core_loaders=['YAML'], apply_default_on_none=True)
        for validator in validators:
            if any(name.startswith('postgresql.') for name in getattr(validator, 'names', ())):
                validator.validate(archived)
        current_pg = _postgres_enabled()
        if _postgres_enabled(archived) != current_pg:
            raise RestoreConnectionMismatch('The backup configuration selects a different database engine.')
        if not current_pg:
            return
        password = archived.postgresql.password
        if 'POSTGRES_PASSWORD' not in os.environ and is_encrypted(password):
            key = archived.get('general.secrets_encryption_key')
            if not key:
                raise ValueError('missing archive encryption key')
            archived.postgresql.password = decrypt_secret(password, master_key=key)
        try:
            archived_url = postgres_engine_url(archived)
            current_url = postgres_engine_url(settings)
        except Exception:
            raise RestoreConnectionMismatch('The PostgreSQL connection URL cannot be parsed.') from None
        if archived_url != current_url:
            raise RestoreConnectionMismatch('The backup would change the effective PostgreSQL connection settings.')
    except RestoreConnectionMismatch:
        raise
    except Exception:
        # Resolver and validator exceptions can contain credentials.
        raise RestoreConnectionMismatch('The backup database connection could not be verified.') from None


def _prepare_restored_configuration(source_path, staged_path):
    """Stage a restored config with a generation no existing cookie can carry.

    The Flask signing key stays as archived: named API tokens are HMACed with
    it, and their plaintext is unavailable for re-keying. Legacy INI archives
    become YAML here because a live config.yaml takes precedence at startup.
    This runs before the database restore, while a refusal can leave both live
    files untouched.
    """
    try:
        if source_path.endswith('.ini'):
            parser = configparser.RawConfigParser()
            with open(source_path, encoding='utf-8') as handle:
                parser.read_file(handle)
            restored = {}
            for section in parser.sections():
                values = {}
                for key, raw_value in parser.items(section):
                    try:
                        values[key] = ast.literal_eval(raw_value)
                    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                        values[key] = raw_value
                restored[section.lower()] = values
        else:
            with open(source_path, encoding='utf-8') as handle:
                restored = yaml.safe_load(handle)
    except (OSError, UnicodeError, configparser.Error, yaml.YAMLError) as error:
        raise BackupError('The backup configuration could not be read') from error

    if not isinstance(restored, dict):
        raise BackupError('The backup configuration is not a mapping')

    def existing_key(mapping, wanted):
        matches = [key for key in mapping if isinstance(key, str) and key.lower() == wanted]
        if len(matches) > 1:
            raise BackupError('The backup authentication settings are ambiguous')
        return matches[0] if matches else wanted

    auth_key = existing_key(restored, 'auth')
    auth = restored.setdefault(auth_key, {})
    if not isinstance(auth, dict):
        raise BackupError('The backup authentication settings are invalid')
    generation_key = existing_key(auth, 'session_generation')
    archived_generation = auth.get(generation_key, 0)
    if type(archived_generation) is not int or archived_generation < 0:
        raise BackupError('The backup session generation is invalid')

    from app.auth.generation import generation_for_restore
    auth[generation_key] = generation_for_restore(archived_generation)

    prepared_path = f'{staged_path}.prepared.yaml'
    try:
        with open(prepared_path, 'w', encoding='utf-8') as handle:
            yaml.safe_dump(restored, handle)
        _validate_restored_database_connection(prepared_path)
        shutil.copymode(staged_path, prepared_path)
        datadir.sync_file(Path(prepared_path))
        os.replace(prepared_path, staged_path)
        datadir.sync_directory(Path(staged_path).parent)
    except (yaml.YAMLError, TypeError) as error:
        raise BackupError('The backup configuration could not be staged') from error
    finally:
        _delete_file(prepared_path)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()


def _restore_destination():
    if not _postgres_enabled():
        return 'sqlite'
    connection = _postgres_connection_settings()
    # Only route selectors enter the digest. Libpq passwords, passfiles, SSL
    # keys, tokens and arbitrary connection options are excluded.
    route = {'host': connection['host'], 'port': connection['port'], 'dbname': connection['database'],
             'user': connection['username']}
    route.update(connection['options'])
    selectors = ('host', 'hostaddr', 'port', 'dbname', 'user', 'service', 'target_session_attrs', 'load_balance_hosts')
    return _fingerprint([route.get(key) or os.getenv('PGDATABASE' if key == 'dbname'
                                                   else 'PG' + key.replace('_', '').upper()) for key in selectors])


def _write_restore_journal(path, record):
    staged = path.with_name(path.name + '.tmp')
    with staged.open('w', encoding='ascii') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, path)
    datadir.sync_directory(path.parent)
    if record['phase'] == _DATABASE_STARTED:
        datadir.sync_directory(path.parent.parent)


def _read_restore_journal(path):
    def valid_digest(value):
        return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)

    try:
        record = json.loads(path.read_text(encoding='ascii'))
        if (record['phase'] not in {_DATABASE_STARTED, _DATABASE_COMPLETE, _DATA_READY}
                or type(record['has_data']) is not bool
                or (record['destination'] != 'sqlite' and not valid_digest(record['destination']))
                or (record['server'] is not None and not valid_digest(record['server']))):
            raise ValueError('unrecognized journal contents')
        return record
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise BackupError('The restore journal could not be read. The staged restore has been kept.') from error


def _require_restore_destination(record, *, use_application_engine=False):
    try:
        if record['destination'] == _restore_destination():
            if record['server'] is None:
                return
            if use_application_engine:
                from app.database import engine

                with engine.connect() as database:
                    target = _fingerprint(tuple(database.exec_driver_sql(_PG_DESTINATION_QUERY).one()))
            else:
                target = _check_postgres_reachable(_postgres_connection_settings())
            if target == record['server']:
                return
    except Exception:
        pass  # The connection or parser error may contain sensitive settings.
    raise BackupError('The recorded restore destination could not be verified. Restore the original database settings.')


def _finish_restore_journal(path):
    path.unlink(missing_ok=True)
    datadir.sync_directory(path.parent)


def _refuse_indeterminate_restore(journal):
    logging.critical('The database restore may have run before Bazarr stopped. It will not be replayed automatically. '
                     'The staged files are in %s. Complete or verify the restore on the original database, then set '
                     'the phase in %s to database-complete. See the PostgreSQL backups and restores documentation.',
                     journal.parent, journal)
    stop_bazarr(EXIT_RESTORE_ERROR)


def restore_from_backup():
    restore = Path(get_restore_path())
    handoff = restore / _RESTORE_HANDOFF
    failed_dump_path = os.path.join(get_restore_path(), POSTGRES_FAILED_ARCHIVE_NAME)
    if os.path.isfile(failed_dump_path):
        _refuse_start_after_failed_restore(failed_dump_path)
        return False
    if handoff.exists():
        try:
            record = _read_restore_journal(handoff)
            if record['phase'] == _DATABASE_STARTED:
                _refuse_indeterminate_restore(handoff)
                return False
            if record['phase'] == _DATA_READY:
                return False  # The next boot step verifies the destination and applies the folders.
            _require_restore_destination(record)
        except BackupError as error:
            logging.error('%s', error)
            stop_bazarr(EXIT_RESTORE_ERROR)
            return False
        return _finish_database_restore(restore, handoff, record)

    if os.path.isfile(os.path.join(get_restore_path(), 'config.yaml')):
        restore_config_path = os.path.join(get_restore_path(), 'config.yaml')
    else:
        restore_config_path = os.path.join(get_restore_path(), 'config.ini')
    dest_config_path = os.path.join(args.config_dir, 'config', 'config.yaml')

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
        _prepare_restored_configuration(restore_config_path, staged_config_path)
    except RestoreConnectionMismatch as error:
        if _postgres_enabled():
            logging.error('Restore refused before pg_restore was started. %s', error)
        else:
            logging.error('Restore refused before changing the database. %s', error)
        _delete_file(staged_config_path)
        _clear_restore_directory()
        return False
    except (BackupError, OSError, shutil.Error):
        logging.error('Restoring the backup configuration failed. Bazarr will not restart, and the '
                      'files staged for the restore have been discarded. The backup archive is untouched.')
        _delete_file(staged_config_path)
        _clear_restore_directory()
        return False

    extracted = restore / datadir.ARCHIVE_DIR
    record = {'phase': _DATABASE_STARTED, 'has_data': extracted.is_dir(), 'destination': _restore_destination(),
              'server': None}

    def before_restore(server):
        record['server'] = server
        datadir.sync_file(Path(restore_database_path))
        datadir.sync_file(Path(restore_config_path))
        if record['has_data']:
            datadir.sync_tree(extracted)
        for name in (secret_paths.HISTORY_ARCHIVE, datadir.OMISSIONS_ARCHIVE):
            if (restore / name).is_file():
                datadir.sync_file(restore / name)
        _write_restore_journal(handoff, record)

    try:
        _restore_database(restore_database_path, dest_database_path, before_restore)
    except (BackupError, OSError, shutil.Error) as error:
        if isinstance(error, PostgresUnreachableError):
            _delete_file(staged_config_path)
            # Kept rather than cleared: the database is exactly as it was, and
            # this restore can still go through once it answers.
            logging.error('%s. The restore did not start, so the database was not touched and nothing '
                          'was changed. The extracted backup stays in %s and the restore is tried again '
                          'at the next start.', error, get_restore_path())
            return False
        if isinstance(error, PostgresRestoreStartedError):
            # pg_restore drops the existing objects before it loads the new
            # ones and there is no transaction around that, so a failure
            # partway leaves the database in neither state. Saying "nothing was
            # changed" here would be a lie the operator acts on, and so would
            # carrying on with the start: this runs at boot, and every step after
            # it would use that database.
            try:
                os.replace(restore_database_path, failed_dump_path)
                datadir.sync_directory(restore)
                _finish_restore_journal(handoff)
            except OSError:
                dump_moved_aside = False
            else:
                dump_moved_aside = True

            if dump_moved_aside:
                logging.exception('Restoring the PostgreSQL database failed partway. pg_restore drops '
                                  'the existing objects before it loads the new ones and cannot undo '
                                  'that, so this database may now be partially restored and must not be '
                                  'used until it has been restored again or rebuilt. Bazarr is stopping '
                                  'and will not start while %s exists. The extracted dump has been '
                                  'kept there and the backup archive it came from is still in %s, so '
                                  'the restore can be retried.',
                                  failed_dump_path, get_backup_path())
                _delete_file(restore_config_path)
                _delete_file(staged_config_path)
            else:
                # The dump could not be moved aside, so the staged set is left
                # whole. Deleting the configuration beside it would make the
                # next start read the dump as a partial backup and delete the
                # very file this message points at.
                logging.exception('Restoring the PostgreSQL database failed partway. pg_restore drops '
                                  'the existing objects before it loads the new ones and cannot undo '
                                  'that, so this database may now be partially restored and must not be '
                                  'used until it has been restored again or rebuilt. Bazarr is '
                                  'stopping. The extracted dump could not be moved aside either, so the '
                                  'staged backup has been kept in %s. The restore will need manual recovery.',
                                  get_restore_path())
            stop_bazarr(EXIT_RESTORE_ERROR)
        elif handoff.exists():
            logging.exception('The database restore did not complete. The staged files have been kept.')
            _refuse_indeterminate_restore(handoff)
        elif _postgres_enabled():
            _delete_file(staged_config_path)
            # Nothing reached pg_restore, so the database is exactly as it was.
            logging.exception('The restore was refused before pg_restore was started, so the database '
                              'was not touched and nothing was changed. Fix the problem reported above '
                              'and try the restore again. Bazarr will not restart, the files staged for '
                              'the restore have been discarded so the next start does not retry it '
                              'silently, and the backup archive itself is untouched.')
            _clear_restore_directory()
        else:
            _delete_file(staged_config_path)
            logging.exception('Restoring the backup failed. Bazarr will not restart, and the files '
                              'staged for the restore have been discarded so the next start does not '
                              'retry it silently. The backup archive itself is untouched.')
            _clear_restore_directory()
        return False

    # Pin the fact that the database was restored while the original
    # configuration is still in place. A retry must never run pg_restore
    # against a connection loaded from the replacement configuration.
    try:
        _require_restore_destination(record)
        record['phase'] = _DATABASE_COMPLETE
        _write_restore_journal(handoff, record)
    except (OSError, BackupError):
        logging.exception('Could not record the completed database restore. The extracted backup is kept.')
        stop_bazarr(EXIT_RESTORE_ERROR)
        return False
    return _finish_database_restore(restore, handoff, record)


def _finish_database_restore(restore, handoff, record):
    """Install the configuration and stage plugin data after the database is in place."""
    from provider_hub.state import provider_hub_dir

    extracted = restore / datadir.ARCHIVE_DIR
    pending = restore / _PLUGIN_RESTORE_PENDING
    config = Path(args.config_dir) / "config" / "config.yaml"
    staged = config.with_suffix(".yaml.restore")
    try:
        try:
            secret_paths.restore_backup_history(restore / secret_paths.HISTORY_ARCHIVE, provider_hub_dir())
        except (OSError, ValueError, AttributeError):
            logging.exception('The plugin field history from the backup could not be restored.')
        if staged.is_file():
            # A successful atomic swap consumes the staged file. On retry
            # its absence means the restored config is already installed.
            os.replace(staged, config)
        elif not config.is_file():
            raise OSError("The restored configuration is missing")
        datadir.sync_directory(config.parent)
        if record['has_data']:
            if extracted.is_dir():
                datadir.remove(pending)
                extracted.rename(pending)
            elif not pending.is_dir():
                raise OSError("The plugin data staged for restore is missing")
        else:
            datadir.remove(pending)
        datadir.sync_directory(restore)
        if (restore / datadir.OMISSIONS_ARCHIVE).is_file():
            logging.warning('Some release data was omitted when this backup was taken. Its folders will be left as they are.')
        _clear_restore_directory()
        if any((restore / name).exists() for name in (
                'config.yaml', 'config.ini', SQLITE_ARCHIVE_NAME, POSTGRES_ARCHIVE_NAME)):
            raise OSError("The staged database or configuration could not be cleared")
        record['phase'] = _DATA_READY
        _write_restore_journal(handoff, record)
    except (OSError, UnicodeError):
        logging.exception('The database is restored. Bazarr will retry the remaining restore steps on startup. '
                          'Any configuration still waiting to be installed is kept at %s. Plugin data remains '
                          'staged in %s.', staged, restore)
        stop_bazarr(EXIT_RESTORE_ERROR)
        return False
    logging.info('Database and configuration restored. Bazarr will restart before applying plugin data.')
    from app.server import webserver
    if webserver is not None:
        webserver.close_all()
    restart_bazarr()
    return True


def apply_pending_plugin_restore():
    """Complete the backup restore using the database opened from the restored config."""
    pending = Path(get_restore_path()) / _PLUGIN_RESTORE_PENDING
    applied = pending.with_name(_APPLIED_PLUGIN_DATA)
    journal = pending.with_name(_RESTORE_HANDOFF)
    if not journal.exists() and not pending.exists() and not pending.is_symlink():
        _clear_applied_plugin_data(applied)
        return
    from provider_hub.sandbox.sweep import previous_workers_stopped
    from provider_hub.state import provider_hub_dir

    try:
        record = _read_restore_journal(journal)
        if record['phase'] != _DATA_READY:
            raise BackupError('The database restore must complete before applying plugin data.')
        _require_restore_destination(record, use_application_engine=True)
        if record['has_data']:
            if pending.exists() or pending.is_symlink():
                if (not (pending / datadir.RESTORE_APPLIED_MARKER).is_file()
                        and not previous_workers_stopped(args.config_dir)):
                    raise BackupError("Previous plugin workers are still active or their shutdown could not be verified")
                datadir.restore_backup(pending, provider_hub_dir())
                datadir.remove(applied)
                pending.rename(applied)
                datadir.sync_directory(pending.parent)
            elif not (applied / datadir.RESTORE_APPLIED_MARKER).is_file():
                raise BackupError('The plugin data staged for restore is missing.')
        _finish_restore_journal(journal)
    except Exception:
        logging.exception('Plugin data restore failed. The staged data remains available and Bazarr will retry it at startup.')
        stop_bazarr(EXIT_RESTORE_ERROR)
        return
    _clear_applied_plugin_data(applied)
    logging.info('Plugin data restored successfully.')


def _clear_applied_plugin_data(applied):
    try:
        datadir.remove(applied)
    except OSError:
        logging.warning('Could not clear the completed plugin data restore.', exc_info=True)


def prepare_restore(filename):
    filename = _validate_backup_filename(filename)
    if filename is None:
        logging.error('Invalid backup filename refused for restore.')
        return False
    if (Path(get_restore_path()) / _RESTORE_HANDOFF).exists():
        logging.error('An earlier restore is still in progress. Finish its recovery before staging a new restore.')
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
            datadir.remove(Path(get_restore_path()) / datadir.ARCHIVE_DIR)
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

        if success and not any(os.path.isfile(os.path.join(get_restore_path(), name))
                               for name in ('config.yaml', 'config.ini')):
            # The same reason: the start that applies it refuses an archive
            # without a configuration as partial and restores nothing.
            logging.error('Backup archive %s does not contain the configuration (config.yaml or '
                          'config.ini), so it cannot be restored.', filename)
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
        logging.debug('Restore staged')

    return success


# Close and os._exit on the request thread is what dropped the PATCH before
# 2xx could leave; a short delay lets the response flush first.
RESTORE_RESTART_DELAY_SECONDS = 1.0


def _restart_after_restore():
    from app.server import webserver
    if webserver is not None:
        webserver.close_all()
    restart_bazarr()


def schedule_restore_restart():
    """Queue the process restart after the restore HTTP response can leave."""
    from threading import Timer
    Timer(RESTORE_RESTART_DELAY_SECONDS, _restart_after_restore).start()


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
