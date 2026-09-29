"""Real transactions for sports reconciliation on the AUTOCOMMIT app engine."""
from contextlib import contextmanager
from dataclasses import dataclass
from sqlalchemy import Connection
from sqlalchemy.orm import Session


@dataclass
class SportsTransactionOutcome:
    rollback_confirmed: bool = False


def error_sqlstate(exc):
    """The SQLSTATE of the driver error a SQLAlchemy exception wraps, or None.

    psycopg2, which the image ships, calls it ``pgcode``; psycopg 3 calls it
    ``sqlstate``. Both are read, because the attribute is simply absent on the
    other driver, so a check that reads one spelling fails without a sound on
    the other. SQLite errors carry neither.
    """
    original = getattr(exc, 'orig', None)
    return getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)


def needs_sports_transaction(database):
    bind = database.get_bind()
    return not isinstance(bind, Connection) and bind.dialect._on_connect_isolation_level == 'AUTOCOMMIT'


@contextmanager
def sports_transaction(database, *, nowait=False, outcome=None):
    # Session.begin() alone cannot override the app engine's AUTOCOMMIT mode.
    # Use a separate connection so failed reconciliations roll back on both engines.
    if outcome is not None:
        outcome.rollback_confirmed = False
    engine = database.get_bind()
    if not needs_sports_transaction(database):
        with database.begin_nested() as transaction:
            try:
                yield database
            except BaseException:
                if outcome is not None:
                    transaction.rollback()
                    outcome.rollback_confirmed = True
                raise
        return
    with engine.connect().execution_options(isolation_level='SERIALIZABLE') as connection:
        timeout = None
        if nowait and connection.dialect.name == 'sqlite':
            timeout = connection.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
            connection.exec_driver_sql('PRAGMA busy_timeout=0')
            connection.commit()
        try:
            with connection.begin() as transaction:
                try:
                    if connection.dialect.name == 'sqlite':
                        # SQLite's legacy driver does not begin a transaction for SELECT.
                        # Acquire the write lock before checking the owner, not at the first write.
                        connection.exec_driver_sql('BEGIN IMMEDIATE')
                    with Session(bind=connection, expire_on_commit=False) as session:
                        yield session
                except BaseException:
                    if outcome is not None:
                        transaction.rollback()
                        outcome.rollback_confirmed = True
                    raise
        finally:
            if timeout is not None:
                connection.exec_driver_sql(f'PRAGMA busy_timeout={int(timeout)}')
                connection.commit()
