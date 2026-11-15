# coding=utf-8
"""One write path for the translation runtime's tables.

Reservations only keep two jobs from spending the same money if the read of what
is free and the write that takes it happen as one step. This module is that
step: a real transaction that takes the database's write lock before the first
read. The threads of this process take their own lock first, always, so two threads that nest
the helpers in opposite orders cannot each hold one lock and wait for the other's.

* SQLite: ``BEGIN IMMEDIATE``. A plain ``BEGIN`` that has read and then meets another
  connection's commit cannot become a writer in WAL mode; SQLite fails it at once
  with "database is locked" and ``busy_timeout`` does not apply. With
  ``BEGIN IMMEDIATE`` the other writers wait instead.
* PostgreSQL: a transaction in READ COMMITTED. The ledger also takes a transaction-scoped
  advisory lock, so a second process sharing the database waits for the first to commit and
  then reads its totals. Under REPEATABLE READ the snapshot would be taken before
  that wait ends and the totals would be stale, which is why the level is set here
  and not left to the server's default.

Lock order: a writer that touches both config.yaml (the engine pointers, the Lingarr settings, the cost caps)
and these tables takes the config lock first and opens its write transaction second, never the other way round.
``TranslatorStore.set_instance_enabled`` and ``hold_back_for_attention`` do it for the pointers, and an engine
save in the settings service does it for everything it writes.

Two helpers share the rules below: ``write_transaction`` (jobs, checkpoints, settings) and
``ledger_transaction`` (usage and history, which adds the advisory lock on PostgreSQL).

* A helper that finds none of this module's transactions open starts and ends one.
* A helper called inside another one only opens a savepoint. The outer helper owns the
  commit, so an inner step never commits the outer one early, and a failure in the inner
  step rolls back to its savepoint and leaves the decision to the caller.
* A helper that finds a transaction some other code opened cannot choose how that
  transaction began; it joins it and commits at the end.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from sqlalchemy import text

# One lock for every write transaction of the runtime, ledger and history included: the writes are small
# and short. Every outermost transaction takes it before it asks the database for its write lock, and
# never the other way round, so two threads cannot each hold one lock and wait for the other's. It is
# re-entrant, so a step that nests another one takes it again without waiting.
_WRITE_LOCK = threading.RLock()
# Arbitrary, fixed, and unlikely to meet another advisory lock in the same database.
_ADVISORY_KEY = 0x7472616E736C6564
# How many of this module's transactions the session is inside. A session belongs to one thread.
_DEPTH_KEY = "translation_write_depth"


def inside_write_transaction(session) -> bool:
    """True while the session is inside a transaction one of this module's helpers opened."""
    return session.info.get(_DEPTH_KEY, 0) > 0


@contextmanager
def _marked(session):
    session.info[_DEPTH_KEY] = session.info.get(_DEPTH_KEY, 0) + 1
    try:
        yield
    finally:
        session.info[_DEPTH_KEY] -= 1


@contextmanager
def _transaction(session, *, ledger: bool):
    if inside_write_transaction(session):
        with session.begin_nested():
            if ledger and session.get_bind().dialect.name == "postgresql":
                session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_KEY})
            yield
        return
    with _WRITE_LOCK:
        connection = session.connection()
        driver = connection.connection.driver_connection
        sqlite = connection.dialect.name == "sqlite"
        postgresql = connection.dialect.name == "postgresql"
        active = driver.in_transaction if sqlite else driver.info.transaction_status != 0
        own = not active
        begun = False
        try:
            if own:
                connection.exec_driver_sql("BEGIN IMMEDIATE" if sqlite else "BEGIN")
                begun = True
                if postgresql:
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            with _marked(session), session.begin_nested():
                if ledger and postgresql:
                    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_KEY})
                yield
            if own:
                connection.exec_driver_sql("COMMIT")
            else:
                session.commit()
        except BaseException:
            if begun:
                connection.exec_driver_sql("ROLLBACK")
            session.expire_all()
            raise


@contextmanager
def write_transaction(session):
    """Run the block as one durable write transaction, or as a savepoint inside the caller's."""
    with _transaction(session, ledger=False):
        yield


@contextmanager
def ledger_transaction(session):
    """Run the block as one durable transaction that no other ledger writer overlaps."""
    with _transaction(session, ledger=True):
        yield
