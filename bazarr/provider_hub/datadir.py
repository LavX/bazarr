"""Private data trees owned by a Plugin Hub release."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import stat
import tempfile
import threading
import time
from concurrent.futures import Future, wait
from functools import wraps
from pathlib import Path, PurePosixPath, PureWindowsPath

ARCHIVE_DIR = "plugin_data"
OMISSIONS_ARCHIVE = "plugin_data_omitted.json"
RESTORE_APPLIED_MARKER = ".applied"
PROBE_DIRECTORY = ".probe"
_PROBE_PREFIX = ".probe-"
QUIESCE_TIMEOUT = 15 * 60
PROBE_COPY_MAX_ENTRIES = 4096
PROBE_COPY_MAX_BYTES = 64 * 1024 * 1024
PROBE_COPY_MAX_DEPTH = 64
PROBE_COPY_CHUNK_BYTES = 256 * 1024
# Keep an archive's database snapshot and data folders on the same side of an activation or removal.
release_lock = threading.RLock()
_PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_DATA_KEY = re.compile(r"[0-9a-f]{32}")
_DIR_FDS = os.open in os.supports_dir_fd and os.listdir in os.supports_fd
logger = logging.getLogger(__name__)


class DataFolderError(ValueError):
    """A data tree could not be safely copied or read."""


class SnapshotRefused(DataFolderError):
    """A release snapshot could not be taken safely at this time."""


class SnapshotBusy(SnapshotRefused):
    """Contention or a time or work limit deferred a snapshot."""


class _ProbeCopyBudget:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self.entries_left = PROBE_COPY_MAX_ENTRIES
        self.bytes_left = PROBE_COPY_MAX_BYTES

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise SnapshotBusy("The plugin data copy timed out. Try again.")

    def entry(self) -> None:
        self.check()
        self.entries_left -= 1
        if self.entries_left < 0:
            raise SnapshotBusy("The plugin data has too many entries for this check.")

    def directory(self, relative: Path) -> None:
        self.check()
        if len(relative.parts) > PROBE_COPY_MAX_DEPTH:
            raise SnapshotBusy("The plugin data is too deeply nested for this check.")

    def file(self, size: int) -> None:
        self.check()
        if size > self.bytes_left:
            raise SnapshotBusy("The plugin data is too large for this check.")

    def consume(self, size: int) -> None:
        self.file(size)
        self.bytes_left -= size


def declared(manifest) -> bool:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        return False
    permissions = manifest.get("permissions")
    return isinstance(permissions, (list, tuple)) and "data_dir" in permissions


def plugin_root(root: Path, plugin_id: str) -> Path:
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise DataFolderError("Invalid plugin id for a data folder")
    folder = root / "data" / plugin_id
    for location in (folder.parent, folder):
        if location.is_symlink() or location.is_junction():
            raise DataFolderError(f"{location.name} links outside its data folder")
    return folder


def release_path(root: Path, plugin_id: str, key: str) -> Path:
    if not isinstance(key, str) or not _DATA_KEY.fullmatch(key):
        raise DataFolderError("Invalid plugin release data key")
    return plugin_root(root, plugin_id) / key


def legacy_data_path(root: Path, plugin_id: str) -> Path:
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise DataFolderError("Invalid plugin id for a data folder")
    parent = root / "run" / plugin_id
    if any(path.is_symlink() or path.is_junction() for path in (parent.parent, parent, parent / "data")):
        raise DataFolderError("The legacy plugin data folder is a link")
    return parent / "data"


def legacy_path(root: Path, plugin_id: str, manifest) -> Path | None:
    if declared(manifest) and manifest.get("kind") in {"translator", "subtitle_provider"}:
        return legacy_data_path(root, plugin_id)
    return None


def _portable_link(root: Path, relative: Path, link: str) -> str:
    destination = PurePosixPath(link)
    if destination.is_absolute() or PureWindowsPath(link).drive or "\\" in link:
        raise DataFolderError(f"{relative} links outside the data folder")
    # A portable relative link cannot traverse above the root even
    # temporarily. Resolve the existing tree as well, since intervening
    # directory links affect how the path is interpreted.
    depth = len(relative.parent.parts)
    for part in destination.parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                raise DataFolderError(f"{relative} links outside the data folder")
        else:
            depth += 1
    try:
        target = (root / relative.parent / destination).resolve()
        if not target.is_relative_to(root.resolve()):
            raise DataFolderError(f"{relative} links outside the data folder")
        return posixpath.relpath(target.relative_to(root.resolve()).as_posix(), relative.parent.as_posix())
    except (OSError, RuntimeError) as error:
        raise DataFolderError(f"Cannot resolve data folder link {relative}") from error


def _open_regular(path: Path | str, recorded, parent_fd: int | None):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_BINARY", 0), dir_fd=parent_fd)
    current = os.fstat(fd)
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
            or not os.path.samestat(recorded, current)):
        os.close(fd)
        raise DataFolderError("A regular data file changed while it was opened")
    return os.fdopen(fd, "rb")


def _transfer(source: Path, target: Path | None, *, backup: bool = False, excluded=(),
              budget: _ProbeCopyBudget | None = None) -> str | None:
    """Hash and optionally copy one tree. Open files relative to checked directories where supported."""
    if budget is not None:
        budget.check()
    try:
        root_stat = source.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(root_stat.st_mode) or source.is_junction():
        raise DataFolderError("The plugin data root is not a regular directory")

    digest = hashlib.sha256()

    def record(kind, relative, data=b""):
        digest.update(kind + hashlib.sha256(os.fsencode(relative.as_posix())).digest()
                      + hashlib.sha256(data).digest())

    def walk(relative, recorded, parent_fd=None):
        if budget is not None:
            budget.directory(relative)
        path = source / relative
        if _DIR_FDS:
            fd = os.open(relative.name if parent_fd is not None else path,
                         os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            if not os.path.samestat(recorded, os.fstat(fd)):
                os.close(fd)
                raise DataFolderError("A data directory changed while it was opened")
        else:
            fd = None
        try:
            if budget is None:
                names = os.listdir(path if fd is None else fd)
            else:
                # Bound enumeration before sorting, including omitted entries.
                # These checks cannot interrupt an individual OS operation.
                names = []
                budget.check()
                with os.scandir(path if fd is None else fd) as entries:
                    for entry in entries:
                        budget.entry()
                        names.append(entry.name)
                budget.check()
            for name in sorted(names):
                if budget is not None:
                    budget.check()
                child = relative / name
                parts = tuple(part.casefold() for part in child.parts) if excluded else ()
                if any(parts[i:i + len(rule)] == rule
                       for rule in excluded for i in range(len(parts) - len(rule) + 1)):
                    continue
                path_to_open = source / child if fd is None else name
                entry = os.stat(path_to_open, dir_fd=fd, follow_symlinks=False)
                out = target / child if target is not None else None
                symlink = stat.S_ISLNK(entry.st_mode) or (fd is None and (source / child).is_junction())
                if symlink:
                    if backup:
                        continue
                    link = os.readlink(path_to_open, dir_fd=fd)
                    portable = _portable_link(source, child, link)
                    if out is not None:
                        out.symlink_to(portable)
                    record(b"L", child, os.fsencode(portable))
                elif stat.S_ISDIR(entry.st_mode):
                    if budget is not None:
                        budget.directory(child)
                    if out is not None:
                        out.mkdir(mode=0o700)
                    record(b"D", child)
                    walk(child, entry, fd)
                elif stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
                    if budget is not None:
                        budget.file(entry.st_size)
                    content = hashlib.sha256()
                    with _open_regular(path_to_open, entry, fd) as contents, contextlib.ExitStack() as stack:
                        output = stack.enter_context(out.open("xb")) if out is not None else None
                        while True:
                            if budget is not None:
                                budget.check()
                                chunk_size = min(PROBE_COPY_CHUNK_BYTES, budget.bytes_left + 1)
                            else:
                                chunk_size = 1024 * 1024
                            chunk = contents.read(chunk_size)
                            if budget is not None:
                                budget.consume(len(chunk))
                            if not chunk:
                                break
                            content.update(chunk)
                            if output is not None:
                                output.write(chunk)
                            if budget is not None:
                                budget.check()
                    if out is not None:
                        out.chmod(0o600 | (entry.st_mode & 0o100))
                    record(b"F", child, content.digest() + bytes([bool(entry.st_mode & 0o100)]))
                elif not backup:
                    raise DataFolderError(f"{child} is a special file or has another hard link")
        finally:
            if fd is not None:
                os.close(fd)

    walk(Path(), root_stat)
    if budget is not None:
        budget.check()
    return digest.hexdigest()


def fingerprint(root: Path | None) -> str | None:
    return _transfer(root, None) if root is not None else None


def copy_folder(source: Path | None, target: Path, *, backup: bool = False, excluded=(),
                probe_deadline: float | None = None) -> str | None:
    """Replace the destination only after the complete copy succeeds.

    A probe deadline enables cooperative checks and finite copy limits. It
    cannot interrupt filesystem calls or guarantee a wall-clock bound.
    """
    budget = _ProbeCopyBudget(probe_deadline) if probe_deadline is not None else None
    if budget is not None:
        budget.check()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".copy-", dir=target.parent))
    staged, old = temporary / "new", temporary / "old"
    retain_old = False
    try:
        staged.mkdir(mode=0o700)
        result = (_transfer(source, staged, backup=backup, excluded=excluded, budget=budget)
                  if source is not None else None)
        if budget is not None:
            budget.check()
        if target.exists() or target.is_symlink():
            target.rename(old)
        try:
            staged.rename(target)
        except OSError:
            if old.exists() or old.is_symlink():
                retain_old = True
                old.rename(target)
                retain_old = False
            raise
        return result
    finally:
        if retain_old:
            logger.error("Plugin data could not be put back; the original remains in %s", old)
        else:
            shutil.rmtree(temporary)


_gate = threading.Condition()
_paused: set[str] = set()
_entering: dict[str, int] = {}


@contextlib.contextmanager
def dispatch(plugin_id: str):
    """Wait before entering a dispatch while this plugin's data is quiesced."""
    with _gate:
        _gate.wait_for(lambda: plugin_id not in _paused)
        _entering[plugin_id] = _entering.get(plugin_id, 0) + 1
    try:
        yield
    finally:
        with _gate:
            left = _entering[plugin_id] - 1
            if left:
                _entering[plugin_id] = left
            else:
                del _entering[plugin_id]
            _gate.notify_all()


def _resume_dispatch(plugin_id: str) -> None:
    with _gate:
        _paused.discard(plugin_id)
        _gate.notify_all()


def _settle_async(plugin_id: str, settle, deadline: float) -> Future:
    result = Future()

    def run() -> None:
        try:
            remaining = deadline - time.monotonic()
            result.set_result(remaining > 0 and settle(plugin_id, remaining))
        except BaseException as error:
            result.set_exception(error)

    threading.Thread(target=run, name="plugin-data-settle", daemon=True).start()
    return result


@contextlib.contextmanager
def quiesced(plugin_id: str, settle, timeout: float, *, bounded: bool = False):
    """Close admission, let dispatches enter their pools, then wait for workers to end."""
    deadline = time.monotonic() + timeout
    with _gate:
        acquired = plugin_id not in _paused
        if acquired:
            _paused.add(plugin_id)
    # Some callers already hold release_lock. They must not wait for a fence
    # owner that may need that lock to take its snapshot.
    if not acquired:
        yield False
        return
    release_pause = True
    try:
        with _gate:
            ready = _gate.wait_for(lambda: plugin_id not in _entering,
                                   timeout=max(0.0, deadline - time.monotonic()))
        remaining = deadline - time.monotonic()
        if not ready or remaining <= 0:
            yield False
        elif bounded:
            # A launcher's shutdown can outlast the wait for its proof. Keep
            # admission closed until that shutdown step has finished.
            result = _settle_async(plugin_id, settle, deadline)
            done, _ = wait([result], timeout=max(0.0, deadline - time.monotonic()))
            if not done:
                release_pause = False
                result.add_done_callback(lambda _: _resume_dispatch(plugin_id))
            yield bool(done) and result.result()
        else:
            yield settle(plugin_id, remaining)
    finally:
        if release_pause:
            _resume_dispatch(plugin_id)


def serialized(function):
    """Serialize changes to release data with backup and restore."""
    @wraps(function)
    def locked(*args, **kwargs):
        with release_lock:
            return function(*args, **kwargs)

    return locked


def remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _probe_root(root: Path) -> Path:
    directory = root / PROBE_DIRECTORY
    if directory.is_symlink() or directory.is_junction():
        raise DataFolderError("The plugin probe directory is a link")
    return directory


def probe_workspace(root: Path) -> tempfile.TemporaryDirectory:
    directory = _probe_root(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=_PROBE_PREFIX, dir=directory)


def cleanup_probe_workspaces(root: Path, workers_stopped) -> None:
    """Remove temporary copies left by the previous host before starting new probes."""
    directory = _probe_root(root)
    if not directory.is_dir():
        return
    folders = [folder for folder in directory.glob(f"{_PROBE_PREFIX}*")
               if not folder.is_symlink() and not folder.is_junction() and folder.is_dir()]
    if folders and workers_stopped():
        for folder in folders:
            remove(folder)


def cleanup(root: Path, state) -> None:
    """Clear unreferenced release folders without touching a rollback, orphan, or kept copy."""
    data = root / "data"
    if not data.is_dir() or data.is_symlink() or data.is_junction():
        return
    kept = {}
    for plugin_id, row in (state.get("installations") or {}).items():
        if isinstance(row, dict):
            kept[plugin_id] = {row.get("active_data_key"), row.get("staged_data_key")}
    for release in getattr(state, "retained_releases", ()):
        kept.setdefault(release["plugin_id"], set()).add(release.get("data_key"))
    for plugin in data.iterdir():
        if (not _PLUGIN_ID.fullmatch(plugin.name) or plugin.is_symlink()
                or plugin.is_junction() or not plugin.is_dir()):
            continue
        for folder in plugin.iterdir():
            if _DATA_KEY.fullmatch(folder.name) and folder.name not in kept.get(plugin.name, set()):
                remove(folder)


def login_paths(manifest) -> tuple[tuple[str, ...], ...] | None:
    """Return relative sign-in paths, or None when the folder cannot be safely backed up."""
    from .plugin_manifest import ManifestValidationError, _login_paths

    if not isinstance(manifest, dict):
        return None
    if "login" not in manifest:
        permissions = manifest.get("permissions") or ()
        return () if isinstance(permissions, (list, tuple)) and "interactive_login" not in permissions else None
    login = manifest["login"]
    try:
        paths = _login_paths(login.get("paths")) if isinstance(login, dict) else None
    except ManifestValidationError:
        return None
    if not paths:
        return None
    rules = []
    for path in paths:
        if PureWindowsPath(path).drive or "\x00" in path:
            return None
        parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
        if not parts:
            return None
        rules.append(parts)
        rules.extend((*parts[:-1], parts[-1] + suffix) for suffix in ("-journal", "-wal", "-shm"))
    return tuple(rules)


def archive_folder(root: Path, plugin_id: str, key: str) -> Path:
    if key == "legacy":
        return legacy_data_path(root, plugin_id)
    return release_path(root, plugin_id, key)


def _release_inventory():
    from . import releases, secret_paths, state, state_store

    document = state.load_state_strict()
    history = dict(document.get(secret_paths.HISTORY_FIELD, {}))
    document[secret_paths.HISTORY_FIELD] = {plugin: list(records) for plugin, records in history.items()}
    previous = {}
    if not state._uses_file(None):
        for row in state_store.list_releases(states=(releases.RETIRED, releases.DRAINING)):
            secret_paths.remember_manifest(document, row.plugin_id, row.manifest)
            if row.state == releases.RETIRED:
                held = previous.get(row.plugin_id)
                if held is None or (row.ended_seq or 0, row.id) > (held.ended_seq or 0, held.id):
                    previous[row.plugin_id] = row
    return document, previous


def _release_folders(document, previous):
    for plugin_id, row in (document.get("installations") or {}).items():
        if (not isinstance(row, dict) or row.get("state") == "removed"
                or not _PLUGIN_ID.fullmatch(plugin_id)):
            continue
        candidates = []
        if row.get("active_version"):
            candidates.append((row.get("active_data_key"), row.get("manifest"), True))
        if row.get("staged_data_rollback") and row.get("staged_version"):
            candidates.append((row.get("staged_data_key"), row.get("staged_manifest"), False))
        elif prior := previous.get(plugin_id):
            candidates.append((prior.data_key, prior.manifest, False))
        seen = set()
        for key, manifest, active in candidates:
            if (not key and declared(manifest) and manifest.get("kind") in {"translator", "subtitle_provider"}):
                key = "legacy"
            if not isinstance(key, str) or (key != "legacy" and not _DATA_KEY.fullmatch(key)) or key in seen:
                continue
            seen.add(key)
            yield plugin_id, key, active, row, [item[1] for item in candidates]


def _login_exclusions(document, root, plugin_id, candidates):
    from . import secret_paths

    historical = secret_paths.archived_manifests(root, plugin_id, strict=True)
    rules = [login_paths(manifest) for manifest in (
        *candidates, *historical, *secret_paths.protected_history(document, plugin_id))]
    if None in rules:
        raise DataFolderError("its sign-in paths could not be read")
    return tuple(path for group in rules for path in group)


def write_backup(archive) -> None:
    """Add active and previous folders using the same release inventory as the database snapshot."""
    from . import secret_paths, state
    from .kinds import get_kind, kind_of

    if state.database_held():
        return
    root = state.provider_hub_dir()
    if not root.is_dir():
        return
    try:
        document, previous = _release_inventory()
    except Exception:
        logger.warning("Could not read the release inventory for the plugin data backup", exc_info=True)
        archive.writestr(OMISSIONS_ARCHIVE, "[]")
        return
    secret_paths.write_backup_history(archive, root, document)
    omissions = []
    with tempfile.TemporaryDirectory(prefix=".backup-data-", dir=root) as temporary:
        for plugin_id, key, active, row, candidates in _release_folders(document, previous):
            snapshot = Path(temporary) / "snapshot"
            name = f"{ARCHIVE_DIR}/{plugin_id}/{key}"
            try:
                source = archive_folder(root, plugin_id, key)
                excluded = _login_exclusions(document, root, plugin_id, candidates)
                idle = (quiesced(plugin_id, get_kind(kind_of(row)).settle_data, QUIESCE_TIMEOUT)
                        if active and source.exists() else contextlib.nullcontext(True))
                with idle as settled:
                    if not settled:
                        raise DataFolderError("it was still busy")
                    if copy_folder(source, snapshot, backup=True, excluded=excluded) is None:
                        raise DataFolderError("the data folder was missing")
            except Exception as error:
                logger.warning("Leaving %s release data out of the backup: %s", plugin_id, error)
                omissions.append(f"{plugin_id}/{key}")
                remove(snapshot)
                continue
            try:
                archive.write(snapshot, name)
                for path in sorted(snapshot.rglob("*")):
                    archive.write(path, f"{name}/{path.relative_to(snapshot).as_posix()}")
            finally:
                remove(snapshot)
    if omissions:
        archive.writestr(OMISSIONS_ARCHIVE, json.dumps(omissions))


def _checked_directory(path: Path) -> bool:
    if path.is_symlink() or path.is_junction() or (path.exists() and not path.is_dir()):
        raise DataFolderError("A plugin restore path is not a regular directory")
    return path.is_dir()


def sync_file(path: Path) -> None:
    # Windows flushes a file through a handle opened with write access.
    with path.open("rb+" if os.name == "nt" else "rb") as stream:
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return  # Windows does not support directory fsync through os.open.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(fd)


def sync_tree(root: Path) -> None:
    def cannot_walk(error):
        raise error

    for directory, _, files in os.walk(root, topdown=False, onerror=cannot_walk):
        path = Path(directory)
        for name in files:
            sync_file(path / name)
        sync_directory(path)


def _sync_swap(target, stage):
    sync_directory(stage)
    sync_directory(target.parent)


def _recover_restore_folder(target: Path, stage: Path) -> None:
    """Restore the original before clearing the journal for an unfinished swap."""
    if not _checked_directory(stage):
        return
    old, replacement = stage / "old", stage / "new"
    if _checked_directory(old):
        if _checked_directory(target):
            if _checked_directory(replacement):
                raise DataFolderError("Both an original and an unexpected destination are present")
            target.rename(replacement)
            _sync_swap(target, stage)
        old.rename(target)
        _sync_swap(target, stage)
    elif (stage / "absent").is_file() and not _checked_directory(replacement) and _checked_directory(target):
        target.rename(replacement)
        _sync_swap(target, stage)
    remove(stage)
    sync_directory(target.parent)


def restore_backup(source: Path, root: Path) -> None:
    """Restore a staged backup against the DB inventory opened on this boot."""
    if not _checked_directory(source):
        return
    document, previous = _release_inventory()
    wanted = {(plugin_id, key) for plugin_id, key, *_ in _release_folders(document, previous)}
    # This marker is written only after every swap. Until then the original
    # trees live beside their destinations, including if Bazarr exits midway.
    committed = source / RESTORE_APPLIED_MARKER
    folders = []
    for plugin in sorted(source.iterdir()):
        if plugin == committed:
            continue
        if not _checked_directory(plugin):
            raise DataFolderError("The backup's plugin directory is missing")
        for folder in sorted(plugin.iterdir()):
            if (plugin.name, folder.name) not in wanted:
                raise DataFolderError("The backup contains a data folder outside the restored release inventory")
            if not _checked_directory(folder):
                raise DataFolderError("A data folder in the backup is missing")
            target = archive_folder(root, plugin.name, folder.name)
            folders.append((folder, target, target.parent / (".restore-data-" + target.name)))

    if not committed.is_file():
        for _, target, stage in folders:
            _recover_restore_folder(target, stage)
        try:
            for folder, target, stage in folders:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                stage.mkdir(mode=0o700)
                replacement = stage / "new"
                replacement.mkdir(mode=0o700)
                if _transfer(folder, replacement, backup=True) is None:
                    raise DataFolderError("A data folder in the backup is missing")
                if not _checked_directory(target):
                    (stage / "absent").touch()
                sync_tree(stage)
                for directory in (target.parent, *target.parent.parents):
                    sync_directory(directory)
                    if directory == root.parent:
                        break
            for _, target, stage in folders:
                if _checked_directory(target):
                    target.rename(stage / "old")
                    _sync_swap(target, stage)
                (stage / "new").rename(target)
                _sync_swap(target, stage)
        except BaseException:
            for _, target, stage in reversed(folders):
                try:
                    _recover_restore_folder(target, stage)
                except (OSError, DataFolderError):
                    logger.exception("Could not put the original plugin folder back at %s; kept in %s", target, stage)
            raise
        # Once written, the marker decides the retry. An interruption in or
        # after this step must not undo swaps that may already be committed.
        committed.touch(exist_ok=False)
    sync_file(committed)
    sync_directory(source)
    for _, _, stage in folders:
        if _checked_directory(stage):
            remove(stage)
