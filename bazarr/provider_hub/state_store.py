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
# What a first start stores when there is no state.json to import. Not an empty
# object: an empty object is a damaged document at any revision.
EMPTY_DOCUMENT = '{"installations": {}}'

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


class StateStoreError(RuntimeError):
    """A database statement of the store failed.

    Carries the kind of failure and nothing the driver reported: a constraint
    error's DETAIL previews the failing row, which is the state document and
    can hold a credential, and no engine option hides it.
    """


class _StaleRevision(Exception):
    """The row moved on since it was read: the compare-and-swap matched nothing."""


_engine_override: Any = None
_ready: dict[int, Any] = {}
_ready_lock = threading.Lock()


_private_engine: Any = None


# Top-level keys of the state document whose contents the store understands.
# A string under any other key is something this code did not put there, so it
# is treated as a possible credential.
_STRUCTURAL_KEYS = frozenset({
    "catalog_sources", "catalog_entries", "installations", "jobs", "plugin_secret_paths", "plugin_envs",
})


def _document_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _document_strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _document_strings(value)


def register_document_secrets(document: str) -> None:
    """Register the credential values in a state document with the log scrubber, before it is bound.

    The statements here bind the whole document, and a document imported from
    an older state.json can hold plaintext credentials until the boot
    migration encrypts them. A database error or a driver's log record can
    quote the statement, so every value that could be a credential is known to
    the shared scrubber (``secret_store.scrub``) before the statement runs:
    every string under an installation's ``config``, and every string under a
    top-level key this code does not understand. Registered values are masked
    wherever the scrubber is applied, and by ``_SecretTextFilter`` on the
    SQLAlchemy loggers, which covers the records written before the app's log
    handlers exist (Alembic, early boot). A value shorter than the scrubber's
    minimum is not masked, as everywhere else.
    """
    try:
        from secret_store import scrub
        data = json.loads(document)
        if not isinstance(data, dict):
            return
        found: set[str] = set()
        for row in (data.get("installations") or {}).values() if isinstance(data.get("installations"), dict) else ():
            if isinstance(row, dict):
                found.update(_document_strings(row.get("config")))
        for key, value in data.items():
            if key not in _STRUCTURAL_KEYS:
                found.update(_document_strings(value))
        found = {value.strip() for value in found if value.strip()}
        with scrub._refresh_lock:
            known = scrub.registered_secret_values()
            if not found <= known:
                scrub.register_secret_values(known | found)
    except Exception:  # never let the scrub registration stand in the way of storing state
        logger.debug("Could not register the state document's credential values for scrubbing", exc_info=False)


class _SecretTextFilter(logging.Filter):
    """Mask the registered credential values in the records SQLAlchemy logs itself.

    SQLAlchemy logs some failures before any exception reaches this store: a
    cursor that cannot be closed while a statement error is handled, a
    connection the pool cannot close. Those records carry the driver's
    message, and PostgreSQL's DETAIL quotes the failing row, which is the
    state document. The app's log handlers scrub formatted text, but only once
    ``configure_logging`` has run, so the same scrubber is applied here, on
    the SQLAlchemy loggers, to the message and to the exception text. Only the
    registered values and the known token shapes are masked: every other
    SQLAlchemy warning or error keeps its driver detail. The exception is
    rendered into ``exc_text`` (masked) and ``exc_info`` is dropped from these
    records, so no handler formats the raw traceback.

    If the scrubber cannot be imported, the records of the logger are
    withheld instead (message and exception removed), since nothing can be
    masked.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from secret_store.scrub import scrub_text
        except Exception:
            scrub_text = None
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        exception_text = record.exc_text
        if record.exc_info and not exception_text:
            exception_text = logging.Formatter().formatException(record.exc_info)
        if scrub_text is None:
            if record.levelno >= logging.WARNING or record.exc_info or record.exc_text:
                record.msg = "a database operation reported a problem; the details are withheld"
                record.args = ()
                record.exc_info = record.exc_text = record.stack_info = None
            return True
        scrubbed = scrub_text(message)
        if scrubbed != message or record.args:
            record.msg = scrubbed
            record.args = ()
        if exception_text:
            record.exc_text = scrub_text(exception_text)
            record.exc_info = None
        return True


_log_filter = _SecretTextFilter()
_filtered_logger_count = -1


def _install_log_filter() -> None:
    """Put the filter on every logger under ``sqlalchemy``, including ones created since the last call.

    A filter on a logger sees only the records logged on that logger, not its
    children's, so each one is covered. The engine and pool loggers exist once
    their engine does, which is before the store uses it.
    """
    global _filtered_logger_count
    registry = logging.Logger.manager.loggerDict
    if len(registry) == _filtered_logger_count:
        return
    for name, candidate in list(registry.items()):
        if (name == "sqlalchemy" or name.startswith("sqlalchemy.")) and isinstance(candidate, logging.Logger):
            if _log_filter not in candidate.filters:
                candidate.addFilter(_log_filter)
    _filtered_logger_count = len(registry)


def hide_parameters(bind):
    """Keep bound values out of this store's database error text.

    The statements here bind the whole state document, and a document
    imported from an older state.json can still hold plaintext credentials.
    SQLAlchemy prints the bound values in a failed statement's message, which
    reaches the log and the traceback, so every engine the store touches has
    to hide them. The app engine and the private one are created with the flag;
    this covers an engine handed in from outside (tests, tooling, an alembic
    connection) and is a no-op for the others. It also installs the filter that
    masks credential values in the log records SQLAlchemy writes itself; see
    ``_SecretTextFilter``.
    """
    _install_log_filter()
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
    _install_log_filter()
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
    """The text to import from a state.json, or an empty inventory without one.

    The file is read by the same strict reader the file backend uses. One that
    is not valid JSON, whose root is not an object, or that has no installation
    inventory is damaged: a copy is kept beside it and ``StateCorruptError`` is
    raised, which the boot handles. Importing an empty inventory in its place
    would hide what is on disk and let the next sync of the file copy replace
    the original. The file stays where it is either way.
    """
    if path is None or not path.exists():
        return EMPTY_DOCUMENT
    from .state import _read_document
    data = _read_document(path)
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
    register_document_secrets(document)
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

    Callers run it through ``_sanitized``: a database error here can quote the
    document.
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


_DRIVER_MODULES = frozenset({"sqlite3", "_sqlite3", "psycopg2", "psycopg", "_psycopg", "psycopg_pool"})


def _is_driver_error(error: BaseException) -> bool:
    """Whether ``error`` came from SQLAlchemy or the database driver, whose text can quote the document."""
    return isinstance(error, sa.exc.SQLAlchemyError) or type(error).__module__.split(".")[0] in _DRIVER_MODULES


def _sanitized(function):
    """Run ``function``, replacing a database or driver error with ``StateStoreError``.

    Every operation of this store that touches the driver runs through it:
    connecting, executing, fetching, committing, creating the table. The
    replacement is raised after the handler has finished, so it has no
    ``__cause__`` and no ``__context__``: nothing that prints or walks the
    exception chain reaches the driver's message, whose DETAIL can quote the
    failing row, which is the state document.
    """
    failure = None
    try:
        return function()
    except Exception as error:
        if not _is_driver_error(error):
            raise
        failure = _describe(error)
    raise StateStoreError(failure)


def _describe(error: BaseException) -> str:
    original = getattr(error, "orig", None) or error
    code = getattr(original, "pgcode", None) or getattr(original, "sqlite_errorname", None)
    return f"the Plugin Hub state could not be stored ({type(original).__name__}{', ' + code if code else ''})"


def _create_table(bind) -> None:
    """Create the table if it is missing, tolerating another process creating it first.

    ``checkfirst`` is a separate existence check, so two starts at once can both
    see no table and one of them fails with a duplicate-table error. That one
    is not a failure when the table is there now; any other error, or a table
    that is still missing, is raised. The recheck can fail on its own (the
    database is down), and is sanitized like everything else.
    """
    failure = None
    try:
        _state_table.create(bind, checkfirst=True)
    except Exception as error:
        if not _is_driver_error(error):
            raise
        failure = _describe(error)
    if failure is None:
        return
    if _sanitized(lambda: sa.inspect(bind).has_table(TABLE_NAME)):
        logger.debug("The Plugin Hub state table was created by another process")
        return
    raise StateStoreError(failure)


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
        _create_table(bind)

        def import_row():
            with write_transaction(bind) as connection:
                if import_legacy_state(connection, legacy_state_path()):
                    logger.info("Imported the Plugin Hub state from %s into the database",
                                legacy_state_path())

        _sanitized(import_row)
        _ready[id(bind)] = bind


def read_document(bind=None) -> tuple[int, str]:
    """The stored revision and document text."""
    bind = bind if bind is not None else engine()
    ensure_ready(bind)

    def fetch():
        with bind.connect() as connection:
            return connection.execute(
                sa.select(_state_table.c.revision, _state_table.c.document)
                .where(_state_table.c.id == ROW_ID)).first()

    row = _sanitized(fetch)
    if row is None:
        raise RuntimeError("the Plugin Hub state row is missing")
    return int(row[0]), row[1]


def _commit_document(connection, read_revision: int, document: str) -> None:
    register_document_secrets(document)
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
        def commit():
            with (around() if around is not None else nullcontext()) as held:
                with write_transaction(bind) as connection:
                    _commit_document(connection, revision, new_text)
                if after_commit is not None:
                    after_commit(held)

        try:
            _sanitized(commit)
        except _StaleRevision:
            logger.debug("The Plugin Hub state changed during a write (attempt %d)", attempt)
            continue
        return result
    raise StateConflictError("the Plugin Hub state kept changing under a write; nothing was saved")
