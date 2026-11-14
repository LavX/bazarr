# coding=utf-8
"""The Plugin Hub state as one versioned document row in the database.

``state.py`` keeps the dict-shaped API every caller already uses; this module
is the storage behind it. The whole state is one JSON document in the single
row of ``provider_hub_state``, next to a ``revision`` that every write checks.

Writes follow the app's AUTOCOMMIT rules. Under AUTOCOMMIT a bare
``rollback()`` does nothing and ``begin_nested`` fails on PostgreSQL, so a
write opens its own connection with a real transaction (the shape of
``sportarr.db.sports_transaction``) and updates the row only where the
revision is still the one the mutator read. Zero rows updated means another
writer got there first: the mutate is run once more from a fresh read, and
raises ``StateConflictError`` if that loses as well.

The read and the mutator run outside the write transaction. A mutator may
reach the network, and on SQLite an open write transaction would hold the
database lock against every other writer for that long.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import sqlalchemy as sa

logger = logging.getLogger(__name__)

TABLE_NAME = "provider_hub_state"
ROW_ID = 1
SCHEMA_VERSION = 1

# The same columns the ORM model and the migration declare. Defined here on
# its own metadata so the store does not import app.database, which cannot be
# imported while the settings are still loading.
_state_table = sa.Table(
    TABLE_NAME,
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("document", sa.Text, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.CheckConstraint("id = 1", name="ck_provider_hub_state_single_row"),
)


class StateConflictError(RuntimeError):
    """The state changed under two attempts in a row, so nothing was written."""


class _StaleRevision(Exception):
    """The row moved on since it was read: the compare-and-swap matched nothing."""


_engine_override: Any = None
_ready: dict[int, Any] = {}
_ready_lock = threading.Lock()


_private_engine: Any = None


def hide_parameters(bind):
    """Keep bound values out of this store's database error text.

    The statements here bind the whole state document, and a document
    imported from an older state.json can still hold plaintext credentials.
    SQLAlchemy prints the bound values in a failed statement's message, which
    reaches the log and the traceback, so every engine the store touches has
    to hide them. The app engine and the private one are created with the flag;
    this covers an engine handed in from outside (tests, tooling, an alembic
    connection) and is a no-op for the others.
    """
    target = getattr(bind, "engine", bind)
    try:
        if not getattr(target, "hide_parameters", False):
            target.hide_parameters = True
    except AttributeError:  # pragma: no cover - not a SQLAlchemy engine
        pass
    return bind


def _build_private_engine():
    """An engine on the app's own database, for before app.database exists.

    Settings load reads and writes plugin state before app.database can be
    imported (it needs the finished settings itself). This builds the same
    connection from the same settings, without pooling, and is only used until
    the app engine is there.
    """
    import os
    from sqlalchemy.pool import NullPool
    from app.config import settings
    from app.get_args import args
    configured = os.getenv("POSTGRES_ENABLED")
    postgresql = configured.lower() == "true" if configured else settings.postgresql.enabled
    if postgresql:
        from app.postgres_url import postgres_engine_url
        return sa.create_engine(postgres_engine_url(settings), poolclass=NullPool,
                                isolation_level="AUTOCOMMIT", hide_parameters=True)
    # The startup code creates this folder after the settings are loaded, which
    # on a first start is after the first read of the state.
    database_dir = os.path.join(args.config_dir, "db")
    os.makedirs(database_dir, exist_ok=True)
    created = sa.create_engine(f'sqlite:///{os.path.join(database_dir, "bazarr.db")}',
                               poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True)

    @sa.event.listens_for(created, "connect")
    def _configure(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
        finally:
            cursor.close()

    return created


def engine():
    global _private_engine
    if _engine_override is not None:
        return hide_parameters(_engine_override)
    module = sys.modules.get("app.database")
    app_engine = getattr(module, "engine", None) if module is not None else None
    if app_engine is not None:
        return hide_parameters(app_engine)
    if _private_engine is None:
        _private_engine = _build_private_engine()
    return _private_engine


def use_engine(candidate) -> None:
    """Point the store at another engine. For tests and tooling."""
    global _engine_override
    _engine_override = candidate
    _ready.clear()


def reset() -> None:
    use_engine(None)


def legacy_state_path() -> Path:
    from .state import _default_state_file
    return _default_state_file()


def _insert_if_absent(values: dict[str, Any], dialect_name: str):
    """An INSERT that does nothing when the row exists, on either engine."""
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover - the app runs on SQLite or PostgreSQL only
        return sa.insert(_state_table).values(**values)
    return insert(_state_table).values(**values).on_conflict_do_nothing(index_elements=["id"])


def _legacy_document(path: Path | None) -> str:
    """The text to import from a state.json, or an empty object without one.

    A file that is not valid JSON raises: importing nothing in its place would
    start the Hub from an empty inventory and hide what is on disk. The file
    stays where it is either way.
    """
    if path is None or not path.exists():
        return "{}"
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        logger.warning("The Plugin Hub state file is not an object; importing an empty state")
        return "{}"
    return json.dumps(data, sort_keys=True)


def import_legacy_state(connection, path: Path | None) -> bool:
    """Create the document row from state.json if there is no row yet.

    Returns True when this call created it. The file is only read: it stays on
    disk as the backup of what was imported, and an existing row is never
    touched, so running this again is harmless.
    """
    hide_parameters(connection)
    present = connection.execute(
        sa.select(_state_table.c.id).where(_state_table.c.id == ROW_ID)).first()
    if present is not None:
        # The row is the state. The file is not opened, so a copy that is
        # corrupt or unreadable cannot block a database that is fine.
        return False
    document = _legacy_document(path)
    result = connection.execute(_insert_if_absent({
        "id": ROW_ID,
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "document": document,
        "updated_at": datetime.now(),
    }, connection.dialect.name))
    return result.rowcount == 1


@contextmanager
def write_transaction(bind):
    """One real transaction on its own connection.

    The app engine is AUTOCOMMIT, which a Session cannot override and where
    ``rollback()`` is a no-op, so the isolation level is set per connection.
    PostgreSQL keeps READ COMMITTED: the revision compare-and-swap is what
    detects a competing writer, and under that level a concurrent update makes
    the UPDATE wait and then match nothing, instead of raising a serialization
    failure. SQLite takes the write lock up front.
    """
    hide_parameters(bind)
    sqlite = bind.dialect.name == "sqlite"
    level = "SERIALIZABLE" if sqlite else "READ COMMITTED"
    with bind.connect().execution_options(isolation_level=level) as connection:
        with connection.begin():
            if sqlite:
                # The legacy driver does not begin a transaction on its own.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection


def ensure_ready(bind=None) -> None:
    """Make sure the table and its row exist. Cheap after the first call.

    Plugin state is read at startup before the schema step has run, and on a
    database that has not been upgraded yet, so the first access creates the
    table and imports state.json itself. The migration does the same work and
    this is a no-op after it.
    """
    bind = hide_parameters(bind if bind is not None else engine())
    if id(bind) in _ready:
        return
    with _ready_lock:
        if id(bind) in _ready:
            return
        _state_table.create(bind, checkfirst=True)
        with write_transaction(bind) as connection:
            if import_legacy_state(connection, legacy_state_path()):
                logger.info("Imported the Plugin Hub state from %s into the database",
                            legacy_state_path())
        _ready[id(bind)] = bind


def read_document(bind=None) -> tuple[int, str]:
    """The stored revision and document text."""
    bind = bind if bind is not None else engine()
    ensure_ready(bind)
    with bind.connect() as connection:
        row = connection.execute(
            sa.select(_state_table.c.revision, _state_table.c.document)
            .where(_state_table.c.id == ROW_ID)).first()
    if row is None:
        raise RuntimeError("the Plugin Hub state row is missing")
    return int(row[0]), row[1]


def _commit_document(connection, read_revision: int, document: str) -> None:
    result = connection.execute(
        sa.update(_state_table)
        .where(_state_table.c.id == ROW_ID, _state_table.c.revision == read_revision)
        .values(document=document, revision=read_revision + 1,
                schema_version=SCHEMA_VERSION, updated_at=datetime.now()))
    if result.rowcount != 1:
        raise _StaleRevision()


def update_document(step: Callable[[str], tuple[str, Any]], bind=None, *,
                    around: Callable[[], Any] | None = None,
                    after_commit: Callable[[Any], None] | None = None,
                    retry_on_conflict: bool = True) -> Any:
    """Read the document, let ``step`` compute the next one, store it.

    ``step`` gets the stored text and returns ``(new_text, result)``; the
    result is handed back. A failure anywhere leaves the stored row exactly as
    it was. A lost revision race reruns ``step`` once from a fresh read, so
    ``step`` must be a pure function of the text it is given; a step that
    cannot be passes ``retry_on_conflict=False`` and the race raises
    ``StateConflictError`` instead.

    ``around`` is a context manager factory entered just before the commit and
    left after ``after_commit`` ran, whose value ``after_commit`` receives. The
    caller uses it to order whatever follows a commit (the state.json copy)
    across processes: the commit and that step happen inside it together.
    """
    bind = bind if bind is not None else engine()
    for attempt in ((1, 2) if retry_on_conflict else (1,)):
        revision, text = read_document(bind)
        new_text, result = step(text)
        try:
            with (around() if around is not None else nullcontext()) as held:
                with write_transaction(bind) as connection:
                    _commit_document(connection, revision, new_text)
                if after_commit is not None:
                    after_commit(held)
        except _StaleRevision:
            logger.debug("The Plugin Hub state changed during a write (attempt %d)", attempt)
            continue
        return result
    raise StateConflictError("the Plugin Hub state kept changing under a write; nothing was saved")
