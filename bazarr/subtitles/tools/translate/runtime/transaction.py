# coding=utf-8
"""One write path for the translation usage ledger and history.

Reservations only keep two jobs from spending the same money if the read of what
is free and the write that takes it happen as one step. This module is that
step: a lock for the threads of this process, and a real transaction that takes
the database's write lock before the first read.

* SQLite: ``BEGIN IMMEDIATE``. A plain ``BEGIN`` that has read and then meets another
  connection's commit cannot become a writer in WAL mode; SQLite fails it at once
  with "database is locked" and ``busy_timeout`` does not apply. With
  ``BEGIN IMMEDIATE`` the other writers wait for the ledger instead.
* PostgreSQL: a transaction in READ COMMITTED, then a transaction-scoped advisory
  lock, so a second process sharing the database waits for the first to commit and
  then reads its totals. Under REPEATABLE READ the snapshot would be taken before
  that wait ends and the totals would be stale, which is why the level is set here
  and not left to the server's default.

When the caller's session is already inside a transaction this helper cannot
choose how that transaction began; it joins it and commits at the end.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from sqlalchemy import text

# One lock for the whole ledger, history included: the writes are small and short.
_LEDGER_LOCK = threading.RLock()
# Arbitrary, fixed, and unlikely to meet another advisory lock in the same database.
_ADVISORY_KEY = 0x7472616E736C6564


@contextmanager
def ledger_transaction(session):
    """Run the block as one durable transaction that no other ledger writer overlaps."""
    with _LEDGER_LOCK:
        connection = session.connection()
        driver = connection.connection.driver_connection
        sqlite = connection.dialect.name == "sqlite"
        postgresql = connection.dialect.name == "postgresql"
        active = driver.in_transaction if sqlite else driver.info.transaction_status != 0
        own = not active
        try:
            if own:
                connection.exec_driver_sql("BEGIN IMMEDIATE" if sqlite else "BEGIN")
                if postgresql:
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            with session.begin_nested():
                if postgresql:
                    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_KEY})
                yield
            if own:
                connection.exec_driver_sql("COMMIT")
            else:
                session.commit()
        except BaseException:
            if own:
                connection.exec_driver_sql("ROLLBACK")
            session.expire_all()
            raise
