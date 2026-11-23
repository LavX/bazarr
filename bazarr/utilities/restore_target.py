"""Identify a restore destination before importing the application's database."""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa

from app.postgres_url import postgres_engine_url

JOURNAL = '.restore-journal.json'
FAILED_POSTGRES_DUMP = 'bazarr_postgres.dump.failed'
STAGED = 'staged'
DATABASE_STARTED = 'database-started'
DATABASE_COMPLETE = 'database-complete'
DATA_READY = 'data-ready'
RECEIPTS = 'bazarr_restore_receipts'
DESTINATION_QUERY = (
    "SELECT datname, oid, inet_server_addr()::text, inet_server_port(), current_setting('port'), "
    "current_setting('search_path') FROM pg_catalog.pg_database WHERE datname = current_database()"
)


class RestoreTargetError(ValueError):
    """The configured restore destination cannot be verified."""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()


def postgres_enabled(settings):
    flag = os.getenv('POSTGRES_ENABLED')
    return flag.lower() == 'true' if flag else bool(settings.postgresql.enabled)


def route(settings):
    if not postgres_enabled(settings):
        return 'sqlite'
    url = postgres_engine_url(settings)
    use = {'host': url.host, 'port': url.port, 'dbname': url.database, 'user': url.username}
    use.update(url.query)
    selectors = ('host', 'hostaddr', 'port', 'dbname', 'user', 'service', 'target_session_attrs', 'load_balance_hosts')
    return fingerprint([use.get(key) or os.getenv('PGDATABASE' if key == 'dbname'
                                                  else 'PG' + key.replace('_', '').upper()) for key in selectors])


def sqlite_file(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return fingerprint((info.st_dev, info.st_ino))


@contextmanager
def postgres_connection(settings):
    engine = sa.create_engine(postgres_engine_url(settings), isolation_level='AUTOCOMMIT',
                              hide_parameters=True, connect_args={'connect_timeout': 10})
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def postgres_database(connection):
    return fingerprint(tuple(connection.exec_driver_sql(DESTINATION_QUERY).one()))


def reserve(settings, config_dir, token):
    if not postgres_enabled(settings):
        return sqlite_file(Path(config_dir) / 'db' / 'bazarr.db'), None
    with postgres_connection(settings) as connection:
        destination = postgres_database(connection)
        schema = connection.exec_driver_sql('SELECT current_schema()').scalar_one()
        table = receipt_table(schema)
        connection.exec_driver_sql(f'CREATE TABLE IF NOT EXISTS {table} (token text PRIMARY KEY, complete boolean)')
        connection.execute(sa.text(f'INSERT INTO {table} (token, complete) VALUES (:token, false)'), {'token': token})
        return destination, schema


def receipt_table(schema):
    if not isinstance(schema, str) or not schema:
        raise RestoreTargetError('A PostgreSQL schema is required for the restore receipt.')
    return '"' + schema.replace('"', '""') + '".' + RECEIPTS


def check_postgres(connection, record):
    if record['server'] != postgres_database(connection):
        raise RestoreTargetError('The recorded PostgreSQL database could not be verified.')
    if token := record.get('receipt'):
        table = receipt_table(record['schema'])
        complete = connection.execute(sa.text(f'SELECT complete FROM {table} WHERE token = :token'),
                                      {'token': token}).scalar_one_or_none()
        _check_receipt(complete, record)


def _check_receipt(complete, record):
    if complete is False and record['phase'] == DATABASE_STARTED:
        raise RestoreTargetError('The PostgreSQL restore transaction has not committed. '
                                 'Keep the staged files and follow the PostgreSQL backups documentation.')
    if complete is None or (record['phase'] != STAGED and not complete):
        raise RestoreTargetError('The recorded PostgreSQL restore receipt could not be verified.')


def check(settings, config_dir, record):
    try:
        destination = route(settings)
        if destination != record['destination']:
            raise RestoreTargetError('The recorded restore destination could not be verified.')
        if destination == 'sqlite':
            if record.get('server') != sqlite_file(Path(config_dir) / 'db' / 'bazarr.db'):
                raise RestoreTargetError('The recorded SQLite database file could not be verified.')
        else:
            with postgres_connection(settings) as connection:
                check_postgres(connection, record)
    except RestoreTargetError:
        raise
    except Exception:
        # Driver errors can contain connection values and must stay out of logs.
        raise RestoreTargetError('The recorded restore destination could not be verified.') from None


def startup_record(config_dir):
    folder = Path(config_dir) / 'restore'
    if (folder / FAILED_POSTGRES_DUMP).is_file():
        raise RestoreTargetError('A failed PostgreSQL restore needs recovery. See the PostgreSQL backups documentation.')
    path = folder / JOURNAL
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding='ascii'))
        phase = record['phase']
        if phase == DATABASE_STARTED and not record.get('receipt'):
            raise RestoreTargetError('The database restore may have run and will not be replayed automatically. '
                                     'Keep the staged files and follow the PostgreSQL backups documentation.')
        if phase not in {STAGED, DATABASE_STARTED, DATABASE_COMPLETE, DATA_READY}:
            raise ValueError('unrecognized restore phase')
        return record
    except RestoreTargetError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise RestoreTargetError('The restore journal could not be read. The staged files have been kept.') from None


def guard_engine(engine, record):
    if record is None or record['destination'] == 'sqlite':
        return

    def checkout(database, *_):
        with database.cursor() as cursor:
            cursor.execute(DESTINATION_QUERY)
            if fingerprint(cursor.fetchone()) != record['server']:
                raise RestoreTargetError('The application reached a different PostgreSQL database during recovery.')
            if token := record.get('receipt'):
                table = receipt_table(record['schema'])
                cursor.execute(f'SELECT complete FROM {table} WHERE token = %s', (token,))
                row = cursor.fetchone()
                _check_receipt(row[0] if row else None, record)

    sa.event.listen(engine, 'checkout', checkout)
    engine._restore_checkout_guard = checkout


def release_engine(engine):
    if guard := getattr(engine, '_restore_checkout_guard', None):
        sa.event.remove(engine, 'checkout', guard)
        del engine._restore_checkout_guard
