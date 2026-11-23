# coding=utf-8

import ast
import configparser
import os
import sqlite3
import shutil
import subprocess
import logging
import re
import secrets
import tempfile
import yaml

from contextlib import closing, suppress
from datetime import datetime, timedelta, timezone
from zipfile import ZipFile, BadZipFile, ZIP_DEFLATED
from glob import glob
from pathlib import Path, PureWindowsPath
from string import ascii_lowercase, ascii_uppercase

from provider_hub import datadir, secret_paths

from app.get_args import args
from app.config import settings
from app.postgres_url import postgres_engine_url
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from literals import EXIT_RESTORE_ERROR
from utilities.central import (BACKUP_STAGE_DIR, OLD_RESTORE_STATE, PLUGIN_RESTORE_PHASE, restart_bazarr, stop_bazarr)

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
_CONFIG_ARCHIVE_NAMES = ('config.yaml', 'config.ini')
_DATABASE_ARCHIVE_NAMES = (SQLITE_ARCHIVE_NAME, POSTGRES_ARCHIVE_NAME, POSTGRES_FAILED_ARCHIVE_NAME)
_PLUGIN_ARCHIVE_NAMES = (datadir.ARCHIVE_DIR, datadir.OMISSIONS_ARCHIVE, secret_paths.HISTORY_ARCHIVE)
_BACKUP_STAGE = BACKUP_STAGE_DIR
_RETIRED_RESTORE = '.backup-consumed'
_RETIRED_FOLDERS = '.applied_plugin_data'
_PLUGIN_RESTORE_PHASE = PLUGIN_RESTORE_PHASE
_DATABASE_STARTED = 'database-started'
_DATABASE_COMPLETE = 'database-complete'
_DATA_READY = 'data-ready'
_OLD_RESTORE_STATE = OLD_RESTORE_STATE

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


class DatabaseRestoreUncertainError(BackupError):
    """The database may have changed without a confirmed restore result."""


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


def _postgres_connection_settings():
    """Resolve the PostgreSQL connection the application itself uses.

    Goes through the application's own resolver, so a backup reaches the same
    database the instance runs on.
    """
    try:
        url = postgres_engine_url(settings)
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
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
    except OSError as error:
        raise BackupError(f'Unable to run {tool_name}: {error}') from error

    restores_database = ran_error is PostgresRestoreStartedError
    try:
        stdout, stderr = process.communicate()
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        # A communication failure gives no evidence about the child or the database.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()
        failure = DatabaseRestoreUncertainError if restores_database else BackupError
        raise failure(f'Unable to read the result of {tool_name}') from error

    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if restores_database and (result.returncode < 0 or (os.name == 'nt' and result.returncode > 255)):
        raise DatabaseRestoreUncertainError(f'{tool_name} was interrupted (exit status {result.returncode})')
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


_SEARCH_PATH_NAME = re.compile(r'\s*(?:"((?:[^"]|"")*)"|([^\s,"]+))\s*(?:,|$)')
_POSTGRES_IDENTIFIER_CASE = str.maketrans(ascii_uppercase, ascii_lowercase)


def _postgres_search_path_names(search_path, role):
    """Read configured names, including those PostgreSQL skipped because they do not exist yet."""
    names = []
    offset = 0
    while offset < len(search_path):
        match = _SEARCH_PATH_NAME.match(search_path, offset)
        if match is None:
            return []
        quoted, unquoted = match.groups()
        name = quoted.replace('""', '"') if quoted is not None else unquoted.translate(_POSTGRES_IDENTIFIER_CASE)
        if name == '$user':
            name = role
        if name != 'pg_temp':
            names.append(name)
        offset = match.end()
    return names


def _check_postgres_reachable(connection, *, resolve_plugin_schemas=False):
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
        with closing(driver.connect(_postgres_conninfo(connection), **arguments)) as client:
            if resolve_plugin_schemas:
                with closing(client.cursor()) as cursor:
                    cursor.execute('''
                        SELECT (SELECT n.nspname
                            FROM pg_catalog.pg_class AS c
                            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                            WHERE c.oid = pg_catalog.to_regclass('provider_hub_state')),
                            pg_catalog.current_schema(), pg_catalog.current_setting('search_path'), current_user,
                            NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class AS c
                                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                                WHERE pg_catalog.left(n.nspname, 3) <> 'pg_'
                                AND n.nspname <> 'information_schema')
                    ''')
                    inventory, current, search_path, role, empty = cursor.fetchone()
                schemas = [inventory or current]
                # On an empty database a full dump can create the configured
                # namespace. Once there is existing data, keep the authority
                # of the inventory (or default schema) this connection sees.
                if inventory is None and empty:
                    schemas.extend(_postgres_search_path_names(search_path, role))
                return tuple(dict.fromkeys(schema for schema in schemas if schema))
    except Exception as error:
        raise PostgresUnreachableError(
            f'The PostgreSQL database could not be reached: {str(error).strip()}') from error


def _verify_postgres_plugin_schema(dump_path):
    """Require the dump's only plugin inventory to replace the one this connection reads."""
    pg_restore = shutil.which('pg_restore')
    if pg_restore is None:
        raise BackupError(f'pg_restore was not found on PATH. {_PG_TOOLS_HINT}')
    connection = _postgres_connection_settings()
    if not _postgres_names_database(connection):
        raise BackupError('No PostgreSQL database name is configured for the plugin data restore.')
    schemas = _check_postgres_reachable(connection, resolve_plugin_schemas=True)
    if not schemas:
        raise BackupError('The configured PostgreSQL connection does not resolve a plugin inventory schema.')

    def inventory_entries(*selection):
        # Let pg_restore select names, including schemas with spaces. The list
        # contains object names and IDs, never data from any archived rows.
        result = subprocess.run([pg_restore, '--list', '--table=provider_hub_state', *selection, dump_path],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors='replace')
        if result.returncode != 0:
            raise BackupError('The PostgreSQL dump catalog could not be read.')
        return [entry for line in result.stdout.splitlines()
                if (entry := re.match(r'^\d+;\s+\d+\s+\d+\s+(TABLE DATA|TABLE) ', line))]

    archived = inventory_entries()
    target = inventory_entries(*(f'--schema={schema}' for schema in schemas))
    expected = ['TABLE', 'TABLE DATA']
    if sorted(entry[1] for entry in archived) != expected or sorted(entry[1] for entry in target) != expected:
        raise BackupError('The PostgreSQL backup must contain a single plugin inventory in the schema '
                          'the configured connection uses. The database was not changed.')


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


def _restore_postgres_database(dump_path):
    """Load a pg_dump custom-format archive back into the configured database."""
    pg_restore = shutil.which('pg_restore')
    if pg_restore is None:
        raise BackupError(f'pg_restore was not found on PATH. {_PG_TOOLS_HINT}')

    connection = _postgres_connection_settings()
    if not _postgres_names_database(connection):
        raise BackupError('No PostgreSQL database name is configured, so there is nothing to restore into.')
    _check_postgres_reachable(connection)

    # --clean --if-exists drops what is already there first, --no-owner keeps
    # the restore working when the backup was taken as a different role. No
    # --exit-on-error: pg_restore already exits non-zero when it ignored an
    # error, so the flag buys no extra loudness and would only stop halfway
    # instead of at the end.
    command = ([pg_restore, '--clean', '--if-exists', '--no-owner'] +
               _postgres_connection_arguments(connection) + [dump_path])
    logging.debug('Restoring PostgreSQL database %s from %s', connection['database'], dump_path)
    try:
        _run_postgres_tool('pg_restore', command, connection, ran_error=PostgresRestoreStartedError)
    except PostgresRestoreStartedError as error:
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


def _wrong_engine_archive_message(restore):
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
    if not (restore / present).is_file():
        return None
    return f'This backup holds {holds}; this instance runs {engine} and needs {expected}.'


def _restore_paths():
    restore = Path(get_restore_path())
    stage = restore / _BACKUP_STAGE
    if stage.is_symlink() or stage.is_junction() or (stage.exists() and not stage.is_dir()):
        raise BackupError('The staged backup path is not a regular directory.')
    if not stage.is_dir():
        return restore, restore
    payload = stage / 'payload'
    if payload.is_symlink() or payload.is_junction() or not payload.is_dir():
        raise BackupError('The staged backup is missing its extraction directory.')
    return stage, payload


def _remove_retired_restore(path):
    try:
        datadir.remove(path)
    except OSError:
        logging.warning('Could not remove consumed restore files at %s.', path, exc_info=True)


def _clear_restore_directory(stage, payload):
    """Retire an API restore as a unit. Clean only known files for a manual restore."""
    if stage != payload:
        retired = stage.with_name(_RETIRED_RESTORE)
        datadir.remove(retired)
        stage.rename(retired)
        datadir.sync_directory(stage.parent)
        _remove_retired_restore(retired)
        return

    for name in (*_CONFIG_ARCHIVE_NAMES, *_DATABASE_ARCHIVE_NAMES, *_PLUGIN_ARCHIVE_NAMES[1:]):
        (payload / name).unlink(missing_ok=True)
    folders, retired = payload / datadir.ARCHIVE_DIR, payload / _RETIRED_FOLDERS
    if folders.exists() or folders.is_symlink():
        datadir.remove(retired)
        folders.rename(retired)
        datadir.sync_directory(payload)
    (stage / _PLUGIN_RESTORE_PHASE).unlink(missing_ok=True)
    datadir.sync_directory(stage)
    _remove_retired_restore(retired)


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
        if member.startswith(datadir.ARCHIVE_DIR + '/'):
            parts = member.rstrip('/').split('/')
            if (len(parts) < 3 or '\\' in member
                    or any(part in ('', '.', '..') or PureWindowsPath(part).drive for part in parts)):
                raise BadZipFile('Unsafe plugin data path in backup archive')
        member_real = os.path.realpath(os.path.join(dest_real, member))
        if member_real != dest_real and not member_real.startswith(dest_real + os.sep):
            raise BadZipFile(f'Unsafe path in backup archive: {member}')
    zip_obj.extractall(path=dest_path)
    if (dest_path / datadir.ARCHIVE_DIR / datadir.RESTORE_APPLIED_MARKER).exists():
        raise BadZipFile('Backup archive contains a plugin restore completion marker')


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


def _restore_database(restore_database_path, dest_database_path):
    """Put the backed up database back, raising BackupError if it cannot be done."""
    if _postgres_enabled():
        _restore_postgres_database(restore_database_path)
        return

    destination = Path(dest_database_path)
    fd, temporary = tempfile.mkstemp(prefix=f'.{destination.name}-restore-', dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy(restore_database_path, temporary)
        datadir.sync_file(Path(temporary))
        os.replace(temporary, destination)
    except (OSError, shutil.Error) as error:
        raise BackupError(f'Unable to restore the database to {dest_database_path}: {error}') from error
    finally:
        _delete_file(temporary)

    # The restored file is a complete database, so the write-ahead log and
    # shared-memory files sitting next to it describe one that no longer exists.
    try:
        for suffix in ('-shm', '-wal'):
            Path(f'{dest_database_path}{suffix}').unlink(missing_ok=True)
        datadir.sync_directory(destination.parent)
    except OSError as error:
        raise DatabaseRestoreUncertainError('Unable to finish installing the SQLite database') from error


def _refuse_start_after_failed_restore(failed_dump_path, journal):
    """Stop the start while a dump that pg_restore failed on is still parked.

    The parked dump means the database may be half restored. Without this
    check the next start could run on the damaged database.
    """
    if journal.is_file():
        logging.critical('An earlier PostgreSQL restore failed partway. Bazarr will not start on this '
                         'database. Keep the dump at %s and the checkpoint at %s. Verify that the old '
                         'process has exited and follow the backup documentation to recover the '
                         'database before clearing the checkpoint.', failed_dump_path, journal)
        stop_bazarr(EXIT_RESTORE_ERROR)
        return
    logging.critical('An earlier restore of the PostgreSQL database failed partway, so the database '
                     'may be partially restored, and Bazarr will not start on it. The dump that was '
                     'being restored is kept at %s. To retry the restore from Bazarr, extract the '
                     'backup archive it came from (in %s) into %s, delete %s, and start Bazarr. To '
                     'restore the database yourself instead, run pg_restore --clean --if-exists '
                     '--no-owner --dbname <database> %s, then delete that file and start Bazarr.',
                     failed_dump_path, get_backup_path(), os.path.dirname(failed_dump_path), failed_dump_path,
                     failed_dump_path)
    stop_bazarr(EXIT_RESTORE_ERROR)


def _prepare_restored_configuration(source_path, staged_path, *, preserve_database=False):
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
            raise BackupError(f'The backup has ambiguous settings for {wanted}')
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

    if preserve_database:
        # The dump went to the currently configured database. The next boot
        # must read its plugin inventory through the same connection settings.
        database = dict(settings.postgresql)
        from secret_store.registry import is_user_visible_secret

        credentials = [field for field, value in database.items()
                       if value and is_user_visible_secret(f'postgresql.{field.lower()}')]
        if credentials:
            from secret_store.crypto import encrypt_secret

            general = restored.setdefault(existing_key(restored, 'general'), {})
            if not isinstance(general, dict):
                raise BackupError('The backup general settings are invalid')
            key = existing_key(general, 'secrets_encryption_key')
            master_key = general.get(key) or secrets.token_urlsafe(32)
            if not isinstance(master_key, str):
                raise BackupError('The backup encryption key is invalid')
            general[key] = master_key
            for field in credentials:
                database[field] = encrypt_secret(database[field], master_key=master_key)
        restored[existing_key(restored, 'postgresql')] = database

    prepared_path = f'{staged_path}.prepared'
    try:
        with open(prepared_path, 'w', encoding='utf-8') as handle:
            yaml.safe_dump(restored, handle)
        shutil.copymode(staged_path, prepared_path)
        os.replace(prepared_path, staged_path)
    except (yaml.YAMLError, TypeError) as error:
        raise BackupError('The backup configuration could not be staged') from error
    finally:
        _delete_file(prepared_path)


def _write_plugin_restore_phase(path, phase):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as handle:
            handle.write(phase + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        datadir.sync_directory(path.parent)
    finally:
        _delete_file(temporary)


def _read_plugin_restore_phase(path):
    if path.is_symlink():
        raise BackupError('The plugin restore checkpoint is a link.')
    if not path.exists():
        return None
    if not path.is_file():
        raise BackupError('The plugin restore checkpoint is not a regular file.')
    phase = path.read_text(encoding='ascii').strip()
    if phase not in (_DATABASE_STARTED, _DATABASE_COMPLETE, _DATA_READY):
        raise BackupError('The plugin restore checkpoint could not be read.')
    return phase


def restore_from_backup():
    """Run the ordinary restore, or finish a plugin restore that already ran it."""
    root = Path(get_restore_path())
    manual_failure = root / POSTGRES_FAILED_ARCHIVE_NAME
    if manual_failure.is_file():
        _refuse_start_after_failed_restore(manual_failure, root / _PLUGIN_RESTORE_PHASE)
        return False
    try:
        if any((root / name).exists() for name in _OLD_RESTORE_STATE):
            raise BackupError('An earlier restore format left recovery files in the restore directory.')
        stage, payload = _restore_paths()
        return _restore_from_archive(stage, payload)
    except (BackupError, OSError, UnicodeError):
        logging.exception('Backup restore stopped. Keep the staged files in %s for recovery.', root)
        stop_bazarr(EXIT_RESTORE_ERROR)
        return False


def _restore_from_archive(stage, payload):
    journal = stage / _PLUGIN_RESTORE_PHASE
    failed_dump_path = payload / POSTGRES_FAILED_ARCHIVE_NAME
    if os.path.isfile(failed_dump_path):
        _refuse_start_after_failed_restore(failed_dump_path, journal)
        return False

    dest_config_path = os.path.join(args.config_dir, 'config', 'config.yaml')
    staged_config_path = f'{dest_config_path}.restore'
    phase = _read_plugin_restore_phase(journal)
    if phase == _DATABASE_STARTED:
        raise BackupError(f'The database restore outcome could not be confirmed. Keep {payload} and '
                          f'{journal} for recovery before retrying.')
    if phase == _DATABASE_COMPLETE:
        return _finish_plugin_database_restore(payload, journal, staged_config_path, dest_config_path)
    if phase == _DATA_READY:
        return False

    restore_config_path = next((str(payload / name) for name in _CONFIG_ARCHIVE_NAMES
                                if (payload / name).is_file()), str(payload / 'config.ini'))
    # The database artifact is named after the engine this instance runs on: a
    # SQLite install restores the database file, a PostgreSQL install restores
    # the pg_dump archive.
    restore_database_path = str(payload / _database_archive_name())
    dest_database_path = os.path.join(args.config_dir, 'db', SQLITE_ARCHIVE_NAME)

    config_present = os.path.isfile(restore_config_path)
    database_present = os.path.isfile(restore_database_path)

    if not config_present and not database_present:
        logging.debug('No backup to restore.')
        return False

    if not (config_present and database_present):
        wrong_engine = _wrong_engine_archive_message(payload)
        if wrong_engine:
            logging.error('%s It cannot be restored here.', wrong_engine)
        else:
            logging.error('Cannot restore a partial backup. You must have both config and database.')
        _clear_restore_directory(stage, payload)
        return False

    has_plugins = any((payload / name).exists() for name in _PLUGIN_ARCHIVE_NAMES)
    if has_plugins and (payload / datadir.ARCHIVE_DIR / datadir.RESTORE_APPLIED_MARKER).exists():
        raise BackupError('The extracted backup contains a plugin restore completion marker.')

    # Stage the configuration before touching the database. Copying it is what
    # proves the destination is readable and writable, and the os.replace at
    # the end makes the swap itself atomic, so a database step that fails
    # cannot leave a new configuration beside the old database.
    try:
        shutil.copy(restore_config_path, staged_config_path)
        _prepare_restored_configuration(restore_config_path, staged_config_path, preserve_database=has_plugins)
    except (BackupError, OSError, shutil.Error):
        logging.error('Restoring the backup configuration failed. Bazarr will not restart, and the '
                      'files staged for the restore have been discarded. The backup archive is untouched.')
        _delete_file(staged_config_path)
        _clear_restore_directory(stage, payload)
        return False

    postgres = _postgres_enabled()
    if postgres and (payload / datadir.ARCHIVE_DIR).is_dir():
        _verify_postgres_plugin_schema(restore_database_path)
    if has_plugins or postgres:
        datadir.sync_file(Path(staged_config_path))
        datadir.sync_directory(Path(staged_config_path).parent)
        _write_plugin_restore_phase(journal, _DATABASE_STARTED)
    try:
        _restore_database(restore_database_path, dest_database_path)
    except (BackupError, OSError, shutil.Error) as error:
        if isinstance(error, DatabaseRestoreUncertainError):
            if not has_plugins and postgres:
                # The checkpoint already fences replay even if parking fails.
                os.replace(restore_database_path, failed_dump_path)
                datadir.sync_directory(payload)
            logging.exception('The database restore outcome could not be confirmed. Bazarr is stopping. '
                              'Keep the extracted backup in %s and the staged configuration for recovery.',
                              payload)
            stop_bazarr(EXIT_RESTORE_ERROR)
            return False
        if isinstance(error, PostgresUnreachableError):
            # Kept rather than cleared: the database is exactly as it was, and
            # this restore can still go through once it answers.
            logging.error('%s. The restore did not start, so the database was not touched and nothing '
                          'was changed. The extracted backup stays in %s and the restore is tried again '
                          'at the next start.', error, payload)
            if has_plugins or postgres:
                journal.unlink()
                datadir.sync_directory(stage)
            _delete_file(staged_config_path)
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
                                  'whole extracted backup has been left in %s for manual recovery.', payload)
            stop_bazarr(EXIT_RESTORE_ERROR)
        elif postgres:
            # Nothing reached pg_restore, so the database is exactly as it was.
            logging.exception('The restore was refused before pg_restore was started, so the database '
                              'was not touched and nothing was changed. Fix the problem reported above '
                              'and try the restore again. Bazarr will not restart, the files staged for '
                              'the restore have been discarded so the next start does not retry it '
                              'silently, and the backup archive itself is untouched.')
            _delete_file(staged_config_path)
            _clear_restore_directory(stage, payload)
        elif has_plugins:
            logging.exception('The SQLite database restore failed. Keep the extracted backup in %s '
                              'to recover the database before starting Bazarr.', payload)
            stop_bazarr(EXIT_RESTORE_ERROR)
        else:
            logging.exception('Restoring the backup failed. Bazarr will not restart, and the files '
                              'staged for the restore have been discarded so the next start does not '
                              'retry it silently. The backup archive itself is untouched.')
            _delete_file(staged_config_path)
            _clear_restore_directory(stage, payload)
        return False

    if has_plugins:
        _write_plugin_restore_phase(journal, _DATABASE_COMPLETE)
        return _finish_plugin_database_restore(payload, journal, staged_config_path, dest_config_path)

    try:
        os.replace(staged_config_path, dest_config_path)
    except OSError:
        # The database is the restored one now, so going on with the start
        # would run it with the configuration it was never paired with. The
        # staged copy is kept, because it is the one file the operator needs.
        logging.exception('The database was restored but the configuration could not be put in place. '
                          'Bazarr is stopping so it does not run the restored database with the '
                          'configuration that was already here. The configuration from the backup is '
                          'kept at %s: copy it over %s, then start Bazarr again.',
                          staged_config_path, dest_config_path)
        _clear_restore_directory(stage, payload)
        stop_bazarr(EXIT_RESTORE_ERROR)
        return False

    _clear_restore_directory(stage, payload)

    logging.info('Backup restored successfully. Bazarr will restart.')
    _restart_after_restore()
    return True


def _finish_plugin_database_restore(payload, journal, staged_config_path, dest_config_path):
    from provider_hub.state import provider_hub_dir

    # History must be available when configuration classifies its secrets on
    # the next boot. The importer is idempotent if startup stopped here.
    try:
        secret_paths.restore_backup_history(payload / secret_paths.HISTORY_ARCHIVE, provider_hub_dir())
    except (OSError, ValueError, AttributeError) as error:
        raise BackupError('The plugin field history from the backup could not be restored.') from error

    config = Path(dest_config_path)
    if Path(staged_config_path).is_file():
        os.replace(staged_config_path, dest_config_path)
    elif not config.is_file():
        raise BackupError('The restored configuration is missing.')
    datadir.sync_directory(config.parent)
    _write_plugin_restore_phase(journal, _DATA_READY)
    logging.info('Database and configuration restored. Bazarr will restart before applying plugin data.')
    _restart_after_restore()
    return True


def apply_pending_plugin_restore():
    """Consume plugin data on the boot that opened the restored configuration."""
    try:
        stage, payload = _restore_paths()
        root = stage if stage == payload else stage.parent
        _remove_retired_restore(root / _RETIRED_RESTORE)
        if _read_plugin_restore_phase(stage / _PLUGIN_RESTORE_PHASE) != _DATA_READY:
            _remove_retired_restore(root / _RETIRED_FOLDERS)
            return
        from provider_hub.sandbox.sweep import previous_workers_stopped
        from provider_hub.state import database_held, provider_hub_dir

        folders = payload / datadir.ARCHIVE_DIR
        if folders.exists() or folders.is_symlink():
            if (not (folders / datadir.RESTORE_APPLIED_MARKER).is_file()
                    and not previous_workers_stopped(args.config_dir)):
                raise BackupError('Previous plugin workers are still active or their shutdown could not be verified.')
            if database_held():
                raise BackupError('The restored plugin inventory is not available.')
            datadir.restore_backup(folders, provider_hub_dir())
        if (payload / datadir.OMISSIONS_ARCHIVE).is_file():
            logging.warning('Some release data was omitted when this backup was taken. Its folders will be left as they are.')
        _clear_restore_directory(stage, payload)
    except Exception:
        logging.exception('Plugin data restore failed. The staged files remain available for the next start.')
        stop_bazarr(EXIT_RESTORE_ERROR)
        return
    logging.info('Plugin data restored successfully.')


@datadir.serialized
def prepare_restore(filename):
    filename = _validate_backup_filename(filename)
    if filename is None:
        logging.error('Invalid backup filename refused for restore.')
        return False
    restore = Path(get_restore_path())
    stage = restore / _BACKUP_STAGE
    if any((restore / name).exists() or (restore / name).is_symlink() for name in (
            _BACKUP_STAGE, _PLUGIN_RESTORE_PHASE, *_OLD_RESTORE_STATE,
            *_CONFIG_ARCHIVE_NAMES, *_DATABASE_ARCHIVE_NAMES, *_PLUGIN_ARCHIVE_NAMES)):
        logging.error('An earlier backup is still staged. Finish its recovery before staging a new restore.')
        return False

    source = Path(get_backup_path()) / filename
    try:
        with tempfile.TemporaryDirectory(prefix='.backup-extract-', dir=restore) as temporary:
            work = Path(temporary)
            payload = work / 'payload'
            payload.mkdir(mode=0o700)
            with ZipFile(source) as archive:
                _safe_extract(archive, payload)

            if not (payload / _database_archive_name()).is_file():
                wrong_engine = _wrong_engine_archive_message(payload)
                if wrong_engine:
                    logging.error('Backup archive %s cannot be restored here. %s', filename, wrong_engine)
                else:
                    logging.error('Backup archive %s does not contain the database for this instance, so it '
                                  'cannot be restored.', filename)
                return False

            if not any((payload / name).is_file() for name in _CONFIG_ARCHIVE_NAMES):
                logging.error('Backup archive %s does not contain the configuration (config.yaml or '
                              'config.ini), so it cannot be restored.', filename)
                return False

            datadir.sync_tree(work)
            work.rename(stage)
    except (BadZipFile, OSError, RuntimeError):
        logging.exception('Unable to stage backup archive %s.', filename)
        return False

    # The rename is the publication point. A directory sync error does not
    # unpublish an already visible restore.
    try:
        datadir.sync_directory(restore)
    except OSError:
        logging.warning('Restore staged, but the staging directory could not be flushed.', exc_info=True)
    logging.debug('Restore staged')
    return True


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
