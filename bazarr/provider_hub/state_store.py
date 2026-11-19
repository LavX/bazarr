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

The releases of the installations (the active and staged versions of each plugin
with their bundle, environment and bindings) live in tables of their own,
``provider_hub_releases`` and ``provider_hub_release_bindings``, written in the
same transaction as the document row. See ``releases`` for how an installation
row is split into the document and its releases and put back together.

The read and the mutator run outside the write transaction. A mutator may
reach the network, and on SQLite an open write transaction would hold the
database lock against every other writer for that long.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import sqlalchemy as sa

from . import releases

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


_local_state: tuple[frozenset[str], Any] = (frozenset(), None)
_MIN_LOCAL_VALUE_LENGTH = 6


_MAX_LOCAL_VALUES = 256
_LONG_VALUE = 1024
_last_local_input: frozenset[str] = frozenset()


def rendered_forms(value: str, depth: int = 2) -> set[str]:
    """The spellings of ``value`` a log line can carry when it quotes the document, a statement or a repr of either.

    The raw text; the JSON-escaped form with and without ``ensure_ascii``
    (what a driver's DETAIL shows of the stored document), without the outer
    quotes; the repr and ascii-escaped form of each of those (what ``%r`` or an
    exception's text shows of a string that holds them); each of them with
    either quote character escaped, for the quote context it ends up in; the SQL
    literal form (an apostrophe doubled); and the repr of its UTF-8 bytes. The
    escaping is applied ``depth`` times, for a document quoted inside a message
    that is itself quoted. Not covered: a rendering that splits the value (a
    line wrap) or transforms it (a case change, a hash).
    """
    forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    for _level in range(depth):
        for form in set(forms):
            forms.add(repr(form)[1:-1])
            forms.add(ascii(form)[1:-1])
            forms.add(form.replace("'", "''"))
            forms.add(repr(form.encode("utf-8"))[2:-1])
            # repr picks its quote from the string itself, so a text that holds
            # an apostrophe is shown with double quotes and its apostrophe bare,
            # while the same text inside a larger string that repr quotes with
            # single quotes shows it escaped. Both contexts are spelled.
            for quote in ("'", '"'):
                escaped = form.replace("\\", "\\\\").replace(quote, "\\" + quote)
                forms.add(escaped)
                forms.add(escaped.encode("ascii", "backslashreplace").decode("ascii"))
    return {form for form in forms if form}


def _set_local_values(values: set[str]) -> None:
    """Publish the values the SQLAlchemy filter masks, with the regex that finds them, in one assignment.

    Every rendered form of a value is masked, not only its decoded text. The
    forms and the pattern are rebuilt only when the set of values changes, the
    longest values go first and at most ``_MAX_LOCAL_VALUES`` are kept, and a
    value over ``_LONG_VALUE`` characters gets one level of escaping instead of
    two, so the pattern stays small whatever the document holds.
    """
    global _local_state, _last_local_input
    wanted = frozenset(value for value in values if len(value) >= _MIN_LOCAL_VALUE_LENGTH)
    if wanted == _last_local_input:
        return
    cleaned: set[str] = set()
    for value in sorted(wanted, key=len, reverse=True)[:_MAX_LOCAL_VALUES]:
        cleaned |= rendered_forms(value, depth=1 if len(value) > _LONG_VALUE else 2)
    ordered = sorted(cleaned, key=len, reverse=True)
    regex = re.compile("|".join(re.escape(value) for value in ordered)) if ordered else None
    _local_state = (frozenset(cleaned), regex)
    _last_local_input = wanted


# PostgreSQL's DETAIL for a constraint error: the failing row, each column
# clipped. A credential that straddles a clip shows only a prefix, which no
# value mask matches, so the whole preview goes.
_ROW_PREVIEW = re.compile(r"Failing row contains \([^\n]*")


def _mask_local(text: str) -> str:
    text = _ROW_PREVIEW.sub("Failing row contains (***)", text)
    values, regex = _local_state
    if regex is not None:
        text = regex.sub("***", text)
    return text


def register_document_secrets(document: str, parsed: Any = None) -> None:
    """Make the credential values in a state document known to the log scrubbing, before it is bound.

    The statements here bind the whole document, and a document imported from
    an older state.json can hold plaintext credentials until the boot
    migration encrypts them. A database error or a driver's log record can
    quote the statement, so before it runs:

    * every string under an installation's ``config``, and under a top-level
      key this code does not understand, joins the set ``_SecretTextFilter``
      masks. That set is local to the SQLAlchemy loggers: it can include
      ordinary values (a region, a hostname) and must not mask them anywhere
      else.
    * the values of the fields the installation's manifest declares secret
      (or the persisted secret paths name) are also registered with the shared
      scrubber (``secret_store.scrub``), as the boot registers them, so they
      are masked in every log line (the scrubber derives the ``%r`` form of
      every value it registers).

    A value shorter than six characters is masked only as a whole token in a
    context, as everywhere else.

    ``parsed`` is the same document as a dict, with the releases put back into
    its rows. The stored text has the manifests moved out to the release
    tables, and the secret fields of an installation come from its manifest.
    """
    try:
        data = parsed if parsed is not None else json.loads(document)
        if not isinstance(data, dict):
            return
        local: set[str] = set()
        secret: set[str] = set()
        installations = data.get("installations")
        if isinstance(installations, dict):
            from secret_store.registry import installation_secret_fields
            for provider_id, row in installations.items():
                config = row.get("config") if isinstance(row, dict) else None
                if not isinstance(config, dict):
                    continue
                local.update(_document_strings(config))
                wanted = {str(field).lower()
                          for field in installation_secret_fields(provider_id, row, data.get("plugin_secret_paths"))}
                for field, value in config.items():
                    if isinstance(field, str) and field.lower() in wanted and isinstance(value, str):
                        secret.add(value)
        for key, value in data.items():
            if key not in _STRUCTURAL_KEYS:
                local.update(_document_strings(value))
        _set_local_values({value.strip() for value in local if value.strip()})
        secret = {value.strip() for value in secret if value.strip() and not value.startswith("enc:")}
        if secret:
            from secret_store import scrub
            # Added to the shared set without replacing it: a refresh that is
            # collecting, or the next publish of any other set, keeps these.
            scrub.add_declared_secret_values(secret)
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
    registered values, the values of the documents the store bound (kept in a
    set local to these loggers) and the known token shapes are masked: every other
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
        scrubbed = _mask_local(scrub_text(message))
        if scrubbed != message or record.args:
            record.msg = scrubbed
            record.args = ()
        if exception_text:
            record.exc_text = _mask_local(scrub_text(exception_text))
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


@contextmanager
def read_transaction(bind):
    """One read transaction on its own connection, so everything read in it is the same committed state.

    PostgreSQL takes REPEATABLE READ, which fixes the snapshot at the first statement. SQLite begins a deferred
    transaction by hand (the legacy driver does not), and in WAL mode the first read fixes the snapshot there.
    """
    hide_parameters(bind)
    sqlite = bind.dialect.name == "sqlite"
    level = "SERIALIZABLE" if sqlite else "REPEATABLE READ"
    with bind.connect().execution_options(isolation_level=level) as connection:
        with connection.begin():
            if sqlite:
                connection.exec_driver_sql("BEGIN")
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
    """Create the tables if they are missing, tolerating another process creating them first.

    ``checkfirst`` is a separate existence check, so two starts at once can both
    see no table and one of them fails with a duplicate-table error. That one
    is not a failure when the tables are there now; any other error, or a table
    that is still missing, is raised. The recheck can fail on its own (the
    database is down), and is sanitized like everything else.
    """
    failure = None
    for create in (lambda: _state_table.create(bind, checkfirst=True), lambda: releases.create_tables(bind)):
        try:
            create()
        except Exception as error:
            if not _is_driver_error(error):
                raise
            failure = failure or _describe(error)
    if failure is None:
        return
    wanted = {TABLE_NAME, releases.RELEASES_TABLE, releases.BINDINGS_TABLE}
    if wanted <= _sanitized(lambda: set(sa.inspect(bind).get_table_names())):
        logger.debug("The Plugin Hub state tables were created by another process")
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


def read_state(bind=None) -> tuple[int, str, list[dict[str, Any]]]:
    """The stored revision, the document text and the releases (the manifests of the active and staged ones), as
    one consistent read.

    The document and the release tables are written in one transaction, so a reader that reads both in one
    snapshot never gets the document of one revision with the releases of another.
    """
    bind = bind if bind is not None else engine()
    ensure_ready(bind)

    def fetch():
        with read_transaction(bind) as connection:
            first = connection.execute(
                sa.select(_state_table.c.revision, _state_table.c.document)
                .where(_state_table.c.id == ROW_ID)).first()
            if first is None:
                return None
            return first[0], first[1], releases.read_rows(connection)

    found = _sanitized(fetch)
    if found is None:
        raise RuntimeError("the Plugin Hub state row is missing")
    return int(found[0]), found[1], found[2]


def read_release_rows(bind=None, *, plugin_id: str | None = None) -> list[dict[str, Any]]:
    """Every stored release with its bindings, the active and staged ones with their manifests.

    The ones a write needs to settle slots against: it also needs the draining,
    retired and failed ones, which come without a manifest.
    """
    bind = bind if bind is not None else engine()
    ensure_ready(bind)

    def fetch():
        with bind.connect() as connection:
            return releases.read_rows(connection, plugin_id=plugin_id)

    return _sanitized(fetch)


def list_releases(plugin_id: str | None = None, *, states: tuple[str, ...] | None = None,
                  bind=None) -> list[releases.Release]:
    """The releases of one plugin, or of all, as typed records with their manifests. Oldest first."""
    bind = bind if bind is not None else engine()
    ensure_ready(bind)

    def fetch():
        with bind.connect() as connection:
            return releases.read_rows(connection, states=states, plugin_id=plugin_id,
                                      manifests=releases.RELEASE_STATES)

    return [releases.release_of(row) for row in _sanitized(fetch)]


class StepExtra:
    """What a document step hands back beside the new text: the release changes to store with it.

    ``composed`` is the same document as the dict the callers see (releases put
    back into the rows), which the credential scrub registration reads.
    """

    __slots__ = ("plan", "composed")

    def __init__(self, plan: "releases.Plan", composed: Any) -> None:
        self.plan = plan
        self.composed = composed


def _commit_document(connection, read_revision: int, document: str) -> None:
    register_document_secrets(document)
    result = connection.execute(
        sa.update(_state_table)
        .where(_state_table.c.id == ROW_ID, _state_table.c.revision == read_revision)
        .values(document=document, revision=read_revision + 1,
                schema_version=SCHEMA_VERSION, updated_at=datetime.now()))
    if result.rowcount != 1:
        raise _StaleRevision()


def update_document(step: Callable[[str], tuple], bind=None, *,
                    around: Callable[[], Any] | None = None,
                    after_commit: Callable[[Any], None] | None = None,
                    retry_on_conflict: bool = True) -> Any:
    """Read the document, let ``step`` compute the next one, store it.

    ``step`` gets the stored text and returns ``(new_text, result)``, or
    ``(new_text, result, StepExtra)`` when the write also changes release rows;
    the result is handed back. A failure anywhere leaves the stored row and the
    release tables exactly as they were. A lost revision race reruns ``step``
    once from a fresh read, so ``step`` must be a pure function of the text it is
    given; a step that cannot be passes ``retry_on_conflict=False`` and the race
    raises ``StateConflictError`` instead.

    ``around`` is a context manager factory entered just before the commit and
    left after ``after_commit`` ran, whose value ``after_commit`` receives. The
    caller uses it to order whatever follows a commit (the state.json copy)
    across processes: the commit and that step happen inside it together.
    """
    bind = bind if bind is not None else engine()
    for attempt in ((1, 2) if retry_on_conflict else (1,)):
        revision, text = read_document(bind)
        outcome = step(text)
        new_text, result = outcome[0], outcome[1]
        extra = outcome[2] if len(outcome) > 2 else None

        if extra is not None:
            # The stored text has the manifests moved out, and a credential field is found through its manifest.
            register_document_secrets(new_text, extra.composed)

        def commit():
            with (around() if around is not None else nullcontext()) as held:
                with write_transaction(bind) as connection:
                    _commit_document(connection, revision, new_text)
                    if extra is not None:
                        releases.apply_plan(connection, extra.plan)
                if after_commit is not None:
                    after_commit(held)

        try:
            _sanitized(commit)
        except _StaleRevision:
            logger.debug("The Plugin Hub state changed during a write (attempt %d)", attempt)
            continue
        return result
    raise StateConflictError("the Plugin Hub state kept changing under a write; nothing was saved")


def change_releases(change: Callable[[Any], Any], bind=None) -> Any:
    """Run ``change(connection)`` on the release tables in a write transaction that also bumps the revision.

    For a release moving between states with no change to the document (a drained release retiring): the bump
    makes a concurrent document write recompute its release changes from the new tables instead of overwriting
    them. It runs under the state lock, so a write of this process does not use up its one retry on it.
    Returns what ``change`` returns. A lost revision race runs it once more.
    """
    from .state import state_write_lock

    bind = bind if bind is not None else engine()
    ensure_ready(bind)
    with state_write_lock():
        for _attempt in (1, 2):
            revision, text = read_document(bind)
            outcome = []

            def commit():
                with write_transaction(bind) as connection:
                    _commit_document(connection, revision, text)
                    outcome[:] = [change(connection)]

            try:
                _sanitized(commit)
            except _StaleRevision:
                continue
            return outcome[0]
    raise StateConflictError("the Plugin Hub state kept changing under a write; nothing was saved")


# The states a release can be moved to outside a document write. A release in a slot is changed through the
# document, which is what the callers read and what state.json copies.
_MOVABLE_TO = (releases.DRAINING, releases.RETIRED, releases.FAILED)


def transition_release(release_id: int, target: str, *, error: str | None = None, bind=None) -> bool:
    """Move one release to another state, if the model allows it. Returns whether anything changed.

    Only ``draining``, ``retired`` and ``failed`` can be asked for here: making a release active or staged
    changes the installation row, so it goes through ``mutate_state``.
    """
    if target not in _MOVABLE_TO:
        raise releases.ReleaseTransitionError(f"a release cannot be made {target} outside a write of the state")
    return change_releases(lambda connection: releases.set_state(connection, release_id, target, error=error), bind)


def retire_draining(bind=None) -> int:
    """End the draining state of every release: nothing in a fresh process is still serving a call.

    The boot runs this, since a release left draining by the last run has no worker. Returns how many moved to
    retired. It writes nothing when no release is draining.
    """
    if not list_releases(states=(releases.DRAINING,), bind=bind):
        return 0

    def retire(connection):
        moved = 0
        for row in releases.read_rows(connection, states=(releases.DRAINING,), manifests=()):
            moved += bool(releases.set_state(connection, row["id"], releases.RETIRED))
        return moved

    return change_releases(retire, bind)


def orphan_plugin_ids(bind=None) -> list[str]:
    """The ids of plugins that have release rows and are not in the document.

    A document rebuilt after damage no longer lists the plugins it lost, and their releases are kept. A plugin
    that is listed again is matched to them by its id; the rest stay until ``discard_orphan_releases``.
    """
    document = json.loads(read_document(bind)[1])
    listed = set((document.get("installations") or {}))
    return sorted({row["plugin_id"] for row in read_release_rows(bind)} - listed)


def discard_orphan_releases(plugin_ids, bind=None) -> int:
    """Delete the releases of the named plugins, which must not be in the document. Returns how many went.

    The explicit end of what a damaged document lost: nothing else deletes them.
    """
    wanted = set(plugin_ids)

    def discard(connection):
        listed = set((json.loads(connection.execute(
            sa.select(_state_table.c.document).where(_state_table.c.id == ROW_ID)).scalar()).get("installations")
            or {}))
        ids = [row["id"] for row in releases.read_rows(connection, manifests=()) if row["plugin_id"] in wanted
               and row["plugin_id"] not in listed]
        if ids:
            releases.apply_plan(connection, releases.Plan(deletes=ids))
        return len(ids)

    return change_releases(discard, bind)
