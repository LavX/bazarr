# coding=utf-8
"""The database handle and the one transaction helper every source module uses.

The application engine runs in AUTOCOMMIT on both backends, so ``rollback()`` on its session does nothing and
``begin_nested`` fails on PostgreSQL. A write that must be all or nothing opens a connection of its own with
a real transaction instead, the way the Sportarr library does.
"""
import json
from contextlib import contextmanager
from hashlib import blake2b

from sqlalchemy import text

from sqlalchemy.engine import Connection

from app import database as _database_module


def handle(session=None):
    """The session a call works on: the caller's, or the application's, looked up at call time."""
    return session if session is not None else _database_module.database


@contextmanager
def source_transaction(session=None):
    """A connection inside a transaction that commits on success and rolls back on any error."""
    if isinstance(session, Connection):
        with session.begin_nested():
            yield session
        return

    engine = handle(session).get_bind()
    # The engine is AUTOCOMMIT, so a transaction needs an isolation level of its own. PostgreSQL waits for a
    # concurrent writer at READ COMMITTED instead of failing the batch with a serialization error; SQLite has
    # one writer at a time whatever the level.
    isolation = 'READ COMMITTED' if engine.dialect.name == 'postgresql' else 'SERIALIZABLE'
    with engine.connect().execution_options(isolation_level=isolation) as connection:
        with connection.begin():
            if connection.dialect.name == 'sqlite':
                # The legacy driver does not begin a transaction for a SELECT: take the write lock first.
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            yield connection


def lock_source_release(connection, release):
    """Serialize publication and exclusion for one (kind, provider, subtitle ID) until commit.

    Take it before library row locks. SQLite already serializes all source write transactions.
    """
    if connection.dialect.name == 'postgresql':
        identity = tuple(str(value) if value is not None else None for value in release)
        digest = blake2b(json.dumps(identity).encode('utf-8'), digest_size=8, person=b'source-exclusion')
        key = int.from_bytes(digest.digest(), 'big', signed=True)
        connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
