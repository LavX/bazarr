# coding=utf-8
"""The database handle and the one transaction helper every source module uses.

The application engine runs in AUTOCOMMIT on both backends, so ``rollback()`` on its session does nothing and
``begin_nested`` fails on PostgreSQL. A write that must be all or nothing opens a connection of its own with
a real transaction instead, the way the Sportarr library does.
"""
from contextlib import contextmanager


from app import database as _database_module


def handle(session=None):
    """The session a call works on: the caller's, or the application's, looked up at call time."""
    return session if session is not None else _database_module.database


@contextmanager
def source_transaction(session=None):
    """A connection inside a transaction that commits on success and rolls back on any error."""
    engine = handle(session).get_bind()
    with engine.connect().execution_options(isolation_level='SERIALIZABLE') as connection:
        with connection.begin():
            if connection.dialect.name == 'sqlite':
                # The legacy driver does not begin a transaction for a SELECT: take the write lock first.
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            yield connection
