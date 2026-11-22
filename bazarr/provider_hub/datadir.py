"""Private data trees owned by a Plugin Hub release."""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path, PurePosixPath, PureWindowsPath

ARCHIVE_DIR = "plugin_data"
QUIESCE_TIMEOUT = 15 * 60
# Keep an archive's database snapshot and data folders on the same side of an activation or removal.
release_lock = threading.RLock()
_PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_DATA_KEY = re.compile(r"[0-9a-f]{32}")
_DIR_FDS = os.open in os.supports_dir_fd and os.listdir in os.supports_fd
logger = logging.getLogger(__name__)


class DataFolderError(ValueError):
    """A data tree could not be safely copied or read."""


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


def _legacy_data(root: Path, plugin_id: str) -> Path:
    if not _PLUGIN_ID.fullmatch(plugin_id):
        raise DataFolderError("Invalid plugin id for a data folder")
    parent = root / "run" / plugin_id
    if any(path.is_symlink() or path.is_junction() for path in (parent.parent, parent, parent / "data")):
        raise DataFolderError("The legacy plugin data folder is a link")
    return parent / "data"


def legacy_path(root: Path, plugin_id: str, manifest) -> Path | None:
    if declared(manifest) and manifest.get("kind") in {"translator", "subtitle_provider"}:
        return _legacy_data(root, plugin_id)
    return None


def _check_link(root: Path, relative: Path, link: str) -> None:
    destination = PurePosixPath(link)
    if destination.is_absolute() or PureWindowsPath(link).drive or "\\" in link:
        raise DataFolderError(f"{relative} links outside the data folder")
    target = relative.parent / destination
    try:
        if not (root / target).resolve().is_relative_to(root.resolve()):
            raise DataFolderError(f"{relative} links outside the data folder")
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


def _transfer(source: Path, target: Path | None, *, backup: bool = False, excluded=()) -> str | None:
    """Hash and optionally copy one tree. Open files relative to checked directories where supported."""
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
            for name in sorted(os.listdir(path if fd is None else fd)):
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
                    _check_link(source, child, link)
                    if out is not None:
                        out.symlink_to(link)
                    record(b"L", child, os.fsencode(link))
                elif stat.S_ISDIR(entry.st_mode):
                    if out is not None:
                        out.mkdir(mode=0o700)
                    record(b"D", child)
                    walk(child, entry, fd)
                elif stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
                    content = hashlib.sha256()
                    with _open_regular(path_to_open, entry, fd) as contents, contextlib.ExitStack() as stack:
                        output = stack.enter_context(out.open("xb")) if out is not None else None
                        while chunk := contents.read(1024 * 1024):
                            content.update(chunk)
                            if output is not None:
                                output.write(chunk)
                    if out is not None:
                        out.chmod(0o600 | (entry.st_mode & 0o100))
                    record(b"F", child, content.digest() + bytes([bool(entry.st_mode & 0o100)]))
                elif not backup:
                    raise DataFolderError(f"{child} is a special file or has another hard link")
        finally:
            if fd is not None:
                os.close(fd)

    walk(Path(), root_stat)
    return digest.hexdigest()


def fingerprint(root: Path | None) -> str | None:
    return _transfer(root, None) if root is not None else None


def copy_folder(source: Path | None, target: Path, *, backup: bool = False, excluded=()) -> str | None:
    """Replace the destination only after the complete copy succeeds."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".copy-", dir=target.parent))
    staged, old = temporary / "new", temporary / "old"
    retain_old = False
    try:
        staged.mkdir(mode=0o700)
        result = _transfer(source, staged, backup=backup, excluded=excluded) if source is not None else None
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


@contextlib.contextmanager
def quiesced(plugin_id: str, settle, timeout: float):
    """Close admission, let dispatches enter their pools, then wait for workers to end."""
    deadline = time.monotonic() + timeout
    with _gate:
        _paused.add(plugin_id)
    try:
        with _gate:
            ready = _gate.wait_for(lambda: plugin_id not in _entering, timeout=timeout)
        yield ready and settle(plugin_id, max(0.0, deadline - time.monotonic()))
    finally:
        with _gate:
            _paused.discard(plugin_id)
            _gate.notify_all()


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
        return _legacy_data(root, plugin_id)
    return release_path(root, plugin_id, key)


def write_backup(archive) -> None:
    """Add active and previous folders using the same release inventory as the database snapshot."""
    from . import releases, secret_paths, state, state_store
    from .kinds import get_kind, kind_of

    if state.database_held():
        return
    root = state.provider_hub_dir()
    if not (root / "data").exists() and not (root / "run").exists():
        return
    try:
        installations = state.load_state_strict().get("installations") or {}
        previous = {}
        if not state._uses_file(None):
            for row in state_store.list_releases(states=(releases.RETIRED,)):
                held = previous.get(row.plugin_id)
                if held is None or (row.ended_seq or 0, row.id) > (held.ended_seq or 0, held.id):
                    previous[row.plugin_id] = row
    except Exception:
        logger.warning("Could not read the release inventory for the plugin data backup", exc_info=True)
        return
    with tempfile.TemporaryDirectory(prefix=".backup-data-", dir=root) as temporary:
        for plugin_id, row in installations.items():
            if (not isinstance(row, dict) or row.get("state") == "removed"
                    or not _PLUGIN_ID.fullmatch(plugin_id)):
                continue
            candidates = []
            if row.get("active_version"):
                candidates.append((row.get("active_data_key"), row.get("manifest"), True))
            if prior := previous.get(plugin_id):
                candidates.append((prior.data_key, prior.manifest, False))
            # A staged copy can carry paths an earlier version used for sign-in.
            # They stay private even after the current manifest drops those paths.
            historical = secret_paths.archived_manifests(root, plugin_id)
            rules = [login_paths(manifest) for _, manifest, _ in candidates]
            rules.extend(login_paths(manifest) for manifest in historical)
            excluded = None if None in rules else tuple(path for group in rules for path in group)
            seen = set()
            for key, manifest, active in candidates:
                if (not key and declared(manifest)
                        and manifest.get("kind") in {"translator", "subtitle_provider"}):
                    key = "legacy"
                if not isinstance(key, str) or (key != "legacy" and not _DATA_KEY.fullmatch(key)) or key in seen:
                    continue
                seen.add(key)
                snapshot = Path(temporary) / "snapshot"
                name = f"{ARCHIVE_DIR}/{plugin_id}/{key}"
                try:
                    source = archive_folder(root, plugin_id, key)
                    if excluded is None:
                        raise DataFolderError("its sign-in paths could not be read")
                    idle = (quiesced(plugin_id, get_kind(kind_of(row)).settle_data, QUIESCE_TIMEOUT)
                            if active and source.exists()
                            else contextlib.nullcontext(True))
                    with idle as settled:
                        if not settled:
                            raise DataFolderError("it was still busy")
                        copy_folder(source, snapshot, backup=True, excluded=excluded)
                except Exception as error:
                    logger.warning("Leaving %s release data out of the backup: %s", plugin_id, error)
                    # Restore an empty folder, even if the target host already has a folder of this release.
                    archive.writestr(f"{name}/", b"")
                    continue
                try:
                    archive.write(snapshot, name)
                    for path in sorted(snapshot.rglob("*")):
                        archive.write(path, f"{name}/{path.relative_to(snapshot).as_posix()}")
                finally:
                    remove(snapshot)


def restore_backup(source: Path, root: Path) -> None:
    if not source.is_dir() or source.is_symlink() or source.is_junction():
        return
    for plugin in source.iterdir():
        if not plugin.is_dir() or plugin.is_symlink() or plugin.is_junction():
            continue
        for folder in plugin.iterdir():
            try:
                target = archive_folder(root, plugin.name, folder.name)
                copy_folder(folder, target, backup=True)
                if not any(target.iterdir()):
                    logger.info("Restored an empty data folder for %s", plugin.name)
            except (OSError, DataFolderError):
                logger.warning("Could not restore the data folder of %s", plugin.name, exc_info=True)
