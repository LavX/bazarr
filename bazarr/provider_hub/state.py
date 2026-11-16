# coding=utf-8
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import contextlib

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .kinds import SUBTITLE_PROVIDER, kind_of


OFFICIAL_CATALOG_SOURCE_ID = "official"
OFFICIAL_CATALOG_SOURCE_NAME = "Official Bazarr Provider Catalog"
OFFICIAL_CATALOG_URL = "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json"
OFFICIAL_CATALOG_REPO = "LavX/bazarr-provider-catalog"
# Catalog entries read from the snapshot bundled in the image carry this origin.
ORIGIN_BUNDLED = "bundled"
# The trust reason of an official source whose catalog came from that snapshot.
RELEASE_FLOOR_TRUST_REASON = "release floor"
_STATE_LOCK = threading.RLock()
# The mutate_state calls this thread is inside, each as the store it edits and
# the document it holds. A nested call coalesces only into one on the same store.
_ACTIVE = threading.local()


@dataclass(frozen=True)
class ProviderHubInstallation:
    provider_id: str
    name: str
    active_version: str | None
    state: str
    pending_restart: bool
    manifest: dict[str, Any]
    active_path: str | None = None
    python_path: str | None = None
    staged_version: str | None = None
    staged_path: str | None = None
    staged_python_path: str | None = None
    last_error: str | None = None
    trusted: bool = False
    origin: str = "catalog"
    source_id: str | None = None
    kind: str = SUBTITLE_PROVIDER
    needs_repair: bool = False


def official_catalog_source() -> dict[str, Any]:
    return {
        "id": OFFICIAL_CATALOG_SOURCE_ID,
        "name": OFFICIAL_CATALOG_SOURCE_NAME,
        "type": "github",
        "url": OFFICIAL_CATALOG_URL,
        "enabled": True,
        "official": True,
        "trusted": True,
        "dev_ref": None,
        "last_checked_at": None,
        "last_error": None,
    }


def spoofs_official_catalog_source(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {
        OFFICIAL_CATALOG_SOURCE_ID.casefold(),
        OFFICIAL_CATALOG_SOURCE_NAME.casefold(),
    }


def catalog_source_for_entry(
    sources: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    source_id = entry.get("source")
    if isinstance(source_id, str):
        source = sources.get(source_id)
        if isinstance(source, dict):
            return source
    source_name = entry.get("source_name")
    if isinstance(source_name, str):
        for key, source in sources.items():
            if key == OFFICIAL_CATALOG_SOURCE_ID:
                continue
            if isinstance(source, dict) and source.get("name") == source_name:
                return source
    return {}


logger = logging.getLogger(__name__)


class StateCorruptError(ValueError):
    """The stored state document is not valid JSON or its root is not an object."""


def default_state() -> dict[str, Any]:
    return {
        "catalog_sources": {
            OFFICIAL_CATALOG_SOURCE_ID: official_catalog_source(),
        },
        "catalog_entries": {},
        "installations": {},
        "jobs": [],
        # `provider_id.field` paths of plugin secret fields. Persisted so a
        # removed plugin keeps its registrations: the row goes at the next
        # restart, the path never leaves.
        "plugin_secret_paths": [],
        # fingerprint -> {"path": venv directory}. Which environment each
        # dependency set runs in; see env_registry.
        "plugin_envs": {},
    }


def _default_state_file() -> Path:
    try:
        from app.get_args import args
        return Path(args.config_dir) / "provider_hub" / "state.json"
    except Exception:
        return Path("provider_hub") / "state.json"


def state_file() -> Path:
    """Where state.json lives.

    The state itself is in the database. The file is what an upgrade imports
    once, then a copy refreshed after every commit: settings load reads it
    before the database is open, and an older build finds it after a
    downgrade. ``BAZARR_PROVIDER_HUB_STATE`` points the whole state API at a
    plain file instead, for tests and tooling.
    """
    override = os.environ.get("BAZARR_PROVIDER_HUB_STATE")
    if override:
        return Path(override)
    return _default_state_file()


def provider_hub_dir() -> Path:
    return state_file().parent


@contextlib.contextmanager
def state_write_lock():
    with _STATE_LOCK:
        yield


# Bumped by every successful state write, so caches derived from the state
# (the decrypt-blocked provider set in the Hub service) can key on it and be
# invalidated by mutate_state deterministically, even where the filesystem's
# mtime granularity would leave a rewritten file looking unchanged. The
# database is written by this process only, so the counter covers it too.
_STATE_VERSION = 0


def state_version() -> int:
    """How many times the state has been written by this process."""
    return _STATE_VERSION


def corrupt_copies(path: str | os.PathLike[str] | None = None) -> list[Path]:
    """The copies kept of state documents that could not be read, oldest first.

    A copy stays until an operator removes it: while one exists, nothing that
    deletes on the strength of the state may run.
    """
    current = Path(path) if path is not None else state_file()
    try:
        return sorted(current.parent.glob(f"{current.name}.*.corrupt"))
    except OSError:
        return []


def _keep_corrupt_copy(current: Path, content: bytes | None = None) -> Path | None:
    """Copy an unreadable state document aside, once per distinct content. Never deletes or moves it.

    ``content`` is the document itself when it is not the file at ``current``
    (the database row); the copy is kept beside the state file either way, where
    ``corrupt_copies`` looks for it.
    """
    try:
        if content is None:
            content = current.read_bytes()
        for existing in corrupt_copies(current):
            if existing.read_bytes() == content:
                return existing
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        target = current.with_name(f"{current.name}.{stamp}.corrupt")
        counter = 0
        while target.exists():
            counter += 1
            target = current.with_name(f"{current.name}.{stamp}-{counter}.corrupt")
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(content)
        os.replace(partial, target)
        return target
    except OSError:
        logger.exception("Provider Hub: could not keep a copy of the unreadable state document")
        return None


def _unreadable(current: Path, reason: str, content: bytes | None = None) -> StateCorruptError:
    kept = _keep_corrupt_copy(current, content)
    logger.error(
        "Provider Hub: the state document %s is unreadable (%s). A copy was kept at %s and nothing was written.",
        current, reason, kept,
    )
    return StateCorruptError(f"the Provider Hub state document is unreadable ({reason})")


class StateNotReadyError(RuntimeError):
    """The database is held back and the state cannot be written yet."""


_DATABASE_HELD = False


def hold_database() -> None:
    """Keep the state off the database until ``release_database``.

    Settings load before the database can be opened: the database password is
    itself one of the stored credentials, and it is decrypted only at the end
    of that step, which needs the plugin credential paths read from the state.
    While held, reads answer from the last copy written to state.json (every
    commit refreshes it) and writes are refused.
    """
    global _DATABASE_HELD
    _DATABASE_HELD = True


def release_database() -> None:
    global _DATABASE_HELD
    _DATABASE_HELD = False


def database_held() -> bool:
    return _DATABASE_HELD and not _uses_file(None)


def _snapshot_state(*, strict: bool) -> dict[str, Any]:
    current = state_file()
    if not current.exists():
        return default_state()
    with current.open("r", encoding="utf-8") as handle:
        return _parse_document(handle.read(), strict=strict)


_SNAPSHOT_LOCK_WAIT = 120.0
_LOCK_HELD = threading.local()


def _snapshot_lock_path() -> Path:
    current = state_file()
    return current.with_name(current.name + ".lock")


def _lock_file(handle) -> bool:
    """Try once for an exclusive lock on the open lock file. False when it is taken."""
    try:
        import fcntl
    except ImportError:
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_file(handle) -> None:
    try:
        import fcntl
    except ImportError:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def process_lock(path: Path, wait: float = _SNAPSHOT_LOCK_WAIT):
    """An exclusive lock on a file, shared by every process of this hub.

    Reentrant within a thread. Yields False when the lock cannot be had (an
    unwritable folder, or the wait ran out); callers decide what that means for
    them. The file stays where it is: only the lock on it is taken and dropped.
    """
    held_here = getattr(_LOCK_HELD, "paths", None)
    if held_here is None:
        held_here = _LOCK_HELD.paths = {}
    key = str(path)
    if held_here.get(key):
        held_here[key] += 1
        try:
            yield True
        finally:
            held_here[key] -= 1
        return
    handle = None
    held = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
        deadline = time.monotonic() + wait
        while True:
            if _lock_file(handle):
                held = True
                break
            if time.monotonic() >= deadline:
                logger.warning("Provider Hub: timed out waiting for the lock %s", path.name)
                break
            time.sleep(0.02)
    except OSError as error:
        logger.warning("Provider Hub: could not take the lock %s: %s", path.name, type(error).__name__)
    if held:
        held_here[key] = 1
    try:
        yield held
    finally:
        if held:
            held_here[key] = 0
            try:
                _unlock_file(handle)
            except OSError:
                pass
        if handle is not None:
            handle.close()


def snapshot_lock():
    """Serialize database commits and state.json publication across processes.

    A commit and the refresh of state.json that follows it are one step: a
    writer that commits revision 2 and is slow to publish must not overwrite
    the file after another has already published revision 3. The lock is a file
    beside the snapshot, held from just before the commit until the copy is
    replaced, and is taken in the same order by every writer, so they cannot
    wait on each other. Yields False when the lock cannot be had: the caller
    then leaves the file alone rather than publish out of order.
    """
    return process_lock(_snapshot_lock_path())


def _write_snapshot(data: dict[str, Any]) -> None:
    """Refresh state.json from the committed state, best effort.

    The file is the copy settings load reads before the database is open, and
    the one an older build finds after a downgrade. The database is the state:
    a failure here is logged and the write that was just committed stands.
    Callers hold ``snapshot_lock``. ``sync_snapshot`` is the strict form for the
    callers that must not leave the file behind.
    """
    try:
        write_state_file(data, state_file())
    except Exception as error:
        logger.warning("Could not refresh the Provider Hub state file: %s", type(error).__name__)


def sync_snapshot() -> bool:
    """Make state.json match the database, raising if it cannot.

    For the credential migration: a plaintext credential encrypted in the
    database must not stay readable in the file, whether the refresh after the
    write failed or an earlier run left the two apart. Returns True when the
    file was rewritten. Does nothing for the file backend, where the file is
    the state. Raises ``StateCorruptError`` for a damaged database document,
    after keeping a copy, and leaves the file as it is.
    """
    if _uses_file(None) or _DATABASE_HELD:
        return False
    with state_write_lock(), snapshot_lock() as held:
        if not held:
            raise OSError("the Provider Hub state file is locked by another writer")
        # Read under the lock: a commit that lands after this read waits for it.
        # The strict reader: a damaged document raises, with a copy kept, and
        # the file that is there stays. The permissive reader would answer an
        # empty default and this would write it over the last good copy.
        data = load_state_strict()
        current = state_file()
        try:
            with current.open("r", encoding="utf-8") as handle:
                if json.load(handle) == data:
                    return False
        except FileNotFoundError:
            if data == default_state():
                return False
        except (OSError, ValueError):
            pass
        write_state_file(data, current)
        return True


def write_state_file(data: dict[str, Any], current: Path) -> None:
    """Replace ``current`` with ``data`` in one step, through a temp file of its own.

    Each writer gets its own temp name, so two writers never share a half
    written file, and the rename keeps a reader from ever seeing one.
    """
    current.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=current.parent, prefix=current.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
        try:
            os.chmod(name, current.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(name, current)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _uses_file(path: str | os.PathLike[str] | None) -> bool:
    """Whether this call reads and writes a file rather than the database.

    An explicit path always does. The environment override does too: it is
    how tests and tooling point the state at a file of their own.
    """
    return path is not None or bool(os.environ.get("BAZARR_PROVIDER_HUB_STATE"))


def _bump_version() -> None:
    global _STATE_VERSION
    _STATE_VERSION += 1


def _parse_document(text: str, *, strict: bool) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        if strict:
            raise ValueError("the Provider Hub state document is not an object")
        return default_state()
    return _normalized_state(data)


def _target(path: str | os.PathLike[str] | None) -> str:
    """Which store a call edits: the database, or one file by its path."""
    if _uses_file(path):
        return "file:" + str(Path(path) if path is not None else state_file())
    return "database"


@contextlib.contextmanager
def _editing(target: str, data: dict[str, Any]):
    stack = getattr(_ACTIVE, "stack", None)
    if stack is None:
        stack = _ACTIVE.stack = []
    stack.append((target, data))
    try:
        yield
    finally:
        stack.pop()


def _enclosing(target: str) -> dict[str, Any] | None:
    for held_target, data in reversed(getattr(_ACTIVE, "stack", None) or []):
        if held_target == target:
            return data
    return None


def _commit_to_database(step: Callable[[str], tuple[str, Any]], written: list[dict[str, Any]],
                        retry_on_conflict: bool) -> Any:
    """Store the document ``step`` computes, then refresh the state.json copy.

    The commit and the copy happen under ``snapshot_lock``, so the copy always
    ends as the last committed state whichever process commits when.
    ``written`` holds the document of the attempt that committed.
    """
    from . import state_store

    def publish(held: bool) -> None:
        if held:
            _write_snapshot(written[-1])
        else:
            logger.warning("Provider Hub: the state file copy was not refreshed, another writer holds its lock")

    result = state_store.update_document(
        step, around=snapshot_lock, after_commit=publish, retry_on_conflict=retry_on_conflict)
    _bump_version()
    return result


def mutate_state(
    mutator: Callable[[dict[str, Any]], Any],
    path: str | os.PathLike[str] | None = None,
    *,
    recover_corrupt: bool = False,
    retry_on_conflict: bool = True,
) -> Any:
    """Read, change and write the state document.

    On the database, a write that loses a race with another process runs
    ``mutator`` once more on a fresh read. A mutator that retries must be a
    pure change to the document it is handed: no settings writes, no files, no
    network, nothing that must happen once. Do those after this call returns,
    when the commit is known. A mutator that cannot be pure passes
    ``retry_on_conflict=False`` and the call fails with ``StateConflictError``
    on a lost race instead of running it again.

    A document that cannot be read (text that is not JSON, or a root that is
    not an object) is never overwritten: a copy is kept beside it and the call
    raises ``StateCorruptError``, because writing the default over it would turn
    the damage into an empty installation inventory. A missing file starts from
    the default. ``recover_corrupt`` is the one documented recovery path: it
    keeps the copy and then starts from the default, and exists for an explicit
    operator action, not for any routine writer.
    """
    with state_write_lock():
        target = _target(path)
        nested = _enclosing(target)
        if nested is not None:
            # A mutator that calls mutate_state on the store it is already
            # editing changes the document the outer call holds and commits
            # with it, on either backend. A second write would only be
            # overwritten by the outer one (file) or make the outer one lose
            # its revision (database). Another store is written as usual.
            return mutator(nested)
        if _uses_file(path):
            current = Path(path) if path is not None else state_file()
            try:
                data = _read_document(current)
            except StateCorruptError:
                if not recover_corrupt:
                    raise
                data = default_state()
            else:
                data = default_state() if data is None else _normalized_state(data)
            with _editing(target, data):
                result = mutator(data)
            save_state(data, path)
            return result
        if _DATABASE_HELD:
            raise StateNotReadyError("the Provider Hub state is read-only until the database is open")

        def step(text: str) -> tuple[str, Any]:
            data = _document_for_mutation(text, recover_corrupt)
            with _editing(target, data):
                result = mutator(data)
            written.append(data)
            return json.dumps(data, sort_keys=True), result

        written: list[dict[str, Any]] = []
        return _commit_to_database(step, written, retry_on_conflict)


def _read_document(current: Path) -> dict[str, Any] | None:
    """The stored root object, None when there is no file, or ``StateCorruptError`` after keeping a copy."""
    if not current.exists():
        return None
    try:
        with current.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as error:
        raise _unreadable(current, "not valid JSON") from error
    if not isinstance(data, dict):
        raise _unreadable(current, "the root is not an object")
    if not isinstance(data.get("installations"), dict):
        # Every document this module writes carries the inventory. One without
        # it is damaged: normalising it would fill in an empty inventory that
        # reads as "nothing is installed" and is then saved as such.
        raise _unreadable(current, "the installation inventory is missing or not an object")
    return data


def mutate_state_checked(
    mutator: Callable[[dict[str, Any]], Any],
    validate: Callable[[Any], None],
    path: str | os.PathLike[str] | None = None,
    *,
    retry_on_conflict: bool = True,
) -> Any:
    """``mutate_state`` for a decision that must not run on a state it cannot read.

    The stored document is read as it is, under the write lock, and handed to
    ``validate`` before anything else happens. A missing file, text that is not
    JSON, or a ``validate`` that raises ends the call with that error and writes
    nothing: ``load_state`` would have turned any of them into an empty default.
    """
    with state_write_lock():
        if _uses_file(path):
            data = read_state_document(path)
            validate(data)
            data = _normalized_state(data)
            with _editing(_target(path), data):
                result = mutator(data)
            save_state(data, path)
            return result
        if _DATABASE_HELD:
            raise StateNotReadyError("the Provider Hub state is read-only until the database is open")

        def step(text: str) -> tuple[str, Any]:
            data = _stored_document(text)
            validate(data)
            data = _normalized_state(data)
            with _editing("database", data):
                result = mutator(data)
            written.append(data)
            return json.dumps(data, sort_keys=True), result

        written: list[dict[str, Any]] = []
        return _commit_to_database(step, written, retry_on_conflict)


def _stored_document(text: str) -> dict[str, Any]:
    """The database document as stored, or an error: never a default.

    The same rule the file backend applies. A document that is not JSON, whose
    root is not an object, or that has no installation inventory is damaged:
    a copy is kept beside the state file and ``StateCorruptError`` is raised,
    because normalizing it would fill in an empty inventory that reads as
    "nothing is installed" and then be stored over the damaged document. An
    empty object is damaged at every revision: a first start with no file to
    import stores a document with an empty inventory, not an empty object.
    """
    current = state_file()
    content = text.encode("utf-8")
    try:
        data = json.loads(text)
    except ValueError as error:
        raise _unreadable(current, "not valid JSON", content) from error
    if not isinstance(data, dict):
        raise _unreadable(current, "the root is not an object", content)
    if not isinstance(data.get("installations"), dict):
        raise _unreadable(current, "the installation inventory is missing or not an object", content)
    return data


def _document_for_mutation(text: str, recover_corrupt: bool) -> dict[str, Any]:
    """The stored document ready to change, normalized; a default only when asked to recover a damaged one."""
    try:
        return _normalized_state(_stored_document(text))
    except StateCorruptError:
        if not recover_corrupt:
            raise
        return default_state()


def read_state_document(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The stored document as it is, or an error: never a default.

    Raises ``FileNotFoundError`` when there is no file and ``StateCorruptError``
    (after keeping a copy of a damaged file) when it is not JSON or its root is
    not an object.
    """
    if not _uses_file(path):
        if _DATABASE_HELD:
            current = state_file()
            if not current.exists():
                raise FileNotFoundError(str(current))
            return _stored_document(current.read_text(encoding="utf-8"))
        from . import state_store
        return _stored_document(state_store.read_document()[1])
    current = Path(path) if path is not None else state_file()
    data = _read_document(current)
    if data is None:
        raise FileNotFoundError(str(current))
    return data


def load_state(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    if not _uses_file(path):
        if _DATABASE_HELD:
            return _snapshot_state(strict=False)
        from . import state_store
        return _parse_document(state_store.read_document()[1], strict=False)
    current = Path(path) if path is not None else state_file()
    if not current.exists():
        return default_state()
    with current.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return default_state()
    return _normalized_state(data)


def load_state_strict(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The state as stored, refusing a document whose root is not an object.

    ``load_state`` answers a default for a malformed document, which is the
    resilient answer for every reader that only wants the state there now. A
    rule that refuses work when the state cannot be read needs the
    distinction: a document whose root is not an object is the state being
    unreadable, not an empty installation inventory, so this reader raises
    and the caller fails closed instead of reading no installations.
    """
    if not _uses_file(path):
        if _DATABASE_HELD:
            return _snapshot_state(strict=True)
        from . import state_store
        return _normalized_state(_stored_document(state_store.read_document()[1]))
    current = Path(path) if path is not None else state_file()
    data = _read_document(current)
    return default_state() if data is None else _normalized_state(data)


def _normalized_state(data: dict[str, Any]) -> dict[str, Any]:
    sources = data.setdefault("catalog_sources", {})
    if not isinstance(sources, dict):
        sources = {}
        data["catalog_sources"] = sources
    if OFFICIAL_CATALOG_SOURCE_ID not in sources:
        sources[OFFICIAL_CATALOG_SOURCE_ID] = official_catalog_source()
    for source_id, source in list(sources.items()):
        if not isinstance(source, dict):
            continue
        is_official = source_id == OFFICIAL_CATALOG_SOURCE_ID
        if is_official:
            # Trust is kept as the last refresh computed it from the commit it
            # resolved; only the record's identity is rebuilt here.
            official = official_catalog_source()
            official.update(
                {
                    key: value
                    for key, value in source.items()
                    if key not in ("official", "url", "type")
                }
            )
            official["trusted"] = bool(official.get("trusted"))
            if official.get("trust_reason") == RELEASE_FLOOR_TRUST_REASON and (
                official.get("dev_ref") or not isinstance(official.get("floor_seed"), dict)
            ):
                # The floor vouches for the release's own catalog only: a source
                # on a dev_ref, or one the seed never marked, cannot hold it.
                official["trusted"] = False
            official["id"] = OFFICIAL_CATALOG_SOURCE_ID
            official["name"] = OFFICIAL_CATALOG_SOURCE_NAME
            sources[source_id] = official
        else:
            source["official"] = False
            source["trusted"] = False
            if spoofs_official_catalog_source(source.get("id")) or spoofs_official_catalog_source(source.get("name")):
                source["name"] = source_id
        if "dev_ref" not in source:
            source["dev_ref"] = None
    entries = data.setdefault("catalog_entries", {})
    if isinstance(entries, dict):
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            source = catalog_source_for_entry(sources, entry)
            entry["trusted"] = bool(source.get("trusted", False))
    installations = data.setdefault("installations", {})
    if isinstance(installations, dict):
        for installation in installations.values():
            # Every row written before kinds existed is a subtitle provider.
            if isinstance(installation, dict) and not installation.get("kind"):
                installation["kind"] = SUBTITLE_PROVIDER
    data.setdefault("jobs", [])
    data.setdefault("plugin_secret_paths", [])
    data.setdefault("plugin_envs", {})
    return data


def save_state(data: dict[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    with state_write_lock():
        if not _uses_file(path):
            if _DATABASE_HELD:
                raise StateNotReadyError("the Provider Hub state is read-only until the database is open")
            document = json.dumps(data, sort_keys=True)
            _commit_to_database(lambda _stored: (document, None), [data], True)
            return
        write_state_file(data, Path(path) if path is not None else state_file())
        _bump_version()


def _installation_from_row(provider_id: str, item: dict[str, Any],
                          manifest: dict[str, Any]) -> ProviderHubInstallation:
    """One installation row as the typed record the callers walk."""
    return ProviderHubInstallation(
        provider_id=str(item.get("provider_id") or provider_id),
        name=str(item.get("name") or manifest.get("name") or provider_id),
        active_version=item.get("active_version"),
        state=str(item.get("state")),
        pending_restart=bool(item.get("pending_restart", False)),
        manifest=manifest,
        active_path=item.get("active_path"),
        python_path=item.get("python_path"),
        staged_version=item.get("staged_version"),
        staged_path=item.get("staged_path"),
        staged_python_path=item.get("staged_python_path"),
        last_error=item.get("last_error"),
        trusted=bool(item.get("trusted", False)),
        origin=str(item.get("origin") or "catalog"),
        source_id=item.get("source_id"),
        kind=kind_of(item),
        needs_repair=bool(item.get("needs_repair", False)),
    )


def active_installations(
    path: str | os.PathLike[str] | None = None,
    *,
    kind: str | None = SUBTITLE_PROVIDER,
) -> list[ProviderHubInstallation]:
    """Installations whose active release runs now, of one kind.

    Defaults to subtitle providers, which is what every caller before kinds
    meant. ``kind=None`` returns every kind and is for kind-neutral code only.
    """
    data = load_state(path)
    installations = []
    for provider_id, item in (data.get("installations") or {}).items():
        if not isinstance(item, dict):
            continue
        if kind is not None and kind_of(item) != kind:
            continue
        if item.get("state") != "active" or item.get("pending_restart") or item.get("blocked"):
            continue
        manifest = item.get("manifest")
        if not isinstance(manifest, dict):
            continue
        installations.append(_installation_from_row(provider_id, item, manifest))
    return installations


def blocked_installations(
    path: str | os.PathLike[str] | None = None,
    *,
    kind: str | None = SUBTITLE_PROVIDER,
) -> list[ProviderHubInstallation]:
    """Installations held back at boot because what they rely on is not running.

    They are installed and keep their settings, so a caller that decides what is gone from what is running
    reads this to tell the two apart."""
    data = load_state(path)
    installations = []
    for provider_id, item in (data.get("installations") or {}).items():
        if not isinstance(item, dict) or not item.get("blocked") or not item.get("active_version"):
            continue
        if kind is not None and kind_of(item) != kind:
            continue
        manifest = item.get("manifest")
        if isinstance(manifest, dict):
            installations.append(_installation_from_row(provider_id, item, manifest))
    return installations


def running_installations(
    path: str | os.PathLike[str] | None = None,
    *,
    kind: str | None = SUBTITLE_PROVIDER,
) -> list[ProviderHubInstallation]:
    """Installations whose active code is running, keyed off active_version.

    The active provider class stays registered until restart, even while an
    update or a removal is staged, so the population the worker uses is
    every row with an active_version, whatever its state and restart
    flag. The settings overlay walks exactly this population: cutting it to
    the non-pending rows would let a provider be admitted on a
    settings-section credential that never reaches its worker config.
    Defaults to subtitle providers; ``kind=None`` returns every kind."""
    data = load_state(path)
    installations = []
    for provider_id, item in (data.get("installations") or {}).items():
        if not isinstance(item, dict):
            continue
        if kind is not None and kind_of(item) != kind:
            continue
        # A plugin held back at boot because what it relies on is not running does not run.
        if not item.get("active_version") or item.get("blocked"):
            continue
        manifest = item.get("manifest")
        if not isinstance(manifest, dict):
            continue
        installations.append(_installation_from_row(provider_id, item, manifest))
    return installations
