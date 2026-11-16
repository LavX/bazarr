"""File helpers for code that runs inside a sandboxed worker.

The sandbox a worker runs in refuses calls that change file metadata: ``chmod``,
``chown``, ``utime`` and the extended attribute calls fail with ``EPERM``. The
standard library reaches for them in places that look harmless:

* ``shutil.copy``, ``shutil.copy2``, ``shutil.copymode``, ``shutil.copystat``
  and ``shutil.copytree`` copy the permission bits and times after the data;
* ``shutil.move`` falls back to ``copy2`` when the rename crosses a filesystem;
* ``Path.touch`` on a file that exists sets its times;
* ``venv.create`` copies or links the interpreter and sets its mode.

Use these helpers instead. They move bytes and names only: a copy gets the
default mode of a new file, never the mode, owner, times or attributes of its
source, and nothing here calls a metadata function.

Every write goes to a uniquely named temporary file beside the target, is
flushed to disk and is renamed into place, so a reader never sees a half-written file.
That has three consequences. The folder of the target must be writable, not only
the target. A symbolic link at the target is replaced, not written through. A
rewritten file keeps the permission bits of the one it replaces (the umask still
applies); a new file gets the default mode.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat

from pathlib import Path
from typing import BinaryIO, Callable, Union

PathLike = Union[str, "os.PathLike[str]"]

__all__ = ["copy_file", "copy_link", "copy_tree", "move", "touch", "write_file"]


def _temp_name() -> str:
    """A short temporary name, whatever the name of the target is, so it never exceeds NAME_MAX."""
    return f".bazarr-{secrets.token_hex(6)}.part"


def _create_temp(target: Path) -> tuple[int, Path]:
    """Create a new, empty file beside ``target`` that no other writer can hold.

    The name is random and the file is opened with ``O_EXCL``, so two writers of
    one destination, in one process or in two, never share a temporary file. It
    gets the mode of the file it will replace, or the default mode of a new file
    when there is none (the process umask applies either way).
    """
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError:
        mode = 0o666
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    while True:
        temp = target.with_name(_temp_name())
        try:
            return os.open(temp, flags, mode), temp
        except FileExistsError:
            continue


def _write_temp(target: Path, fill: Callable[[BinaryIO], None]) -> None:
    """Fill a fresh temporary file, flush it to disk and rename it over ``target``.

    On any failure only this call's own temporary file is removed.
    """
    fd, temp = _create_temp(target)
    try:
        with os.fdopen(fd, "wb") as handle:
            fill(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        _discard(temp)
        raise


def copy_file(src: PathLike, dst: PathLike) -> Path:
    """Copy the contents of ``src`` to the file ``dst``, replacing it. Returns ``dst``.

    ``dst`` is the target file, not a directory to copy into.
    """
    target = Path(dst)

    def fill(handle: BinaryIO) -> None:
        # Opening a named pipe would wait for a writer, so look before opening.
        if not stat.S_ISREG(os.stat(src).st_mode):
            raise shutil.SpecialFileError(f"{os.fspath(src)!r} is not a regular file")
        with open(src, "rb") as source:
            shutil.copyfileobj(source, handle)

    _write_temp(target, fill)
    return target


def write_file(path: PathLike, data: Union[bytes, str], *, encoding: str = "utf-8") -> Path:
    """Write ``data`` to ``path`` through a temporary file and a rename. Returns ``path``."""
    target = Path(path)
    payload = data.encode(encoding) if isinstance(data, str) else data
    _write_temp(target, lambda handle: handle.write(payload))
    return target


def touch(path: PathLike) -> Path:
    """Create an empty file if ``path`` does not exist. Whatever exists there is left as it is.

    Unlike ``Path.touch`` it does not update the modification time, and it never
    opens an existing file for writing: the file is created exclusively.
    """
    target = Path(path)
    try:
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o666))
    except FileExistsError:
        pass
    return target


def copy_tree(src: PathLike, dst: PathLike, *, dirs_exist_ok: bool = False, symlinks: bool = True) -> Path:
    """Copy the directory ``src`` to ``dst``. Returns ``dst``.

    Directories are created with the default mode, files are copied by
    :func:`copy_file`. With ``symlinks`` (the default) a link is recreated as a
    link, a dangling one included; without it the file or directory it points
    to is copied. ``dirs_exist_ok`` allows ``dst`` to exist already, as in
    ``shutil.copytree``.

    The copy goes on past what it cannot copy, and a :class:`shutil.Error` naming
    each of those is raised at the end. That covers a named pipe, a socket or a
    device, a folder or file that cannot be read, and with ``symlinks=False`` a
    link that leads back into the folder being copied. A destination inside
    ``src`` is refused at once with a :class:`shutil.Error`.
    """
    source = Path(src)
    target = Path(dst)
    if not source.is_dir():
        raise NotADirectoryError(str(source))
    if _inside(target, source):
        raise shutil.Error(f"Cannot copy the directory {str(source)!r} into itself, {str(target)!r}.")
    return _copy_tree(source, target, dirs_exist_ok, symlinks, frozenset())


def _copy_tree(source: Path, target: Path, dirs_exist_ok: bool, symlinks: bool, above: frozenset[str]) -> Path:
    here = above | {os.path.realpath(source)}
    # Read the source before the destination exists, so nothing made below can be listed.
    entries = sorted(os.scandir(source), key=lambda item: item.name)
    target.mkdir(parents=True, exist_ok=dirs_exist_ok)
    failures: list[tuple[str, str, str]] = []
    for entry in entries:
        destination = target / entry.name
        try:
            if entry.is_symlink() and symlinks:
                copy_link(entry.path, destination)
            elif entry.is_dir():
                if os.path.realpath(entry.path) in here:
                    raise shutil.Error([(entry.path, str(destination), "a link that leads back into the folder being copied")])
                _copy_tree(Path(entry.path), destination, dirs_exist_ok, symlinks, here)
            else:
                copy_file(entry.path, destination)
        except shutil.Error as error:
            failures.extend(error.args[0])
        except OSError as error:  # a special file, an unreadable entry, a name that cannot be made
            failures.append((entry.path, str(destination), str(error)))
    if failures:
        raise shutil.Error(failures)
    return target


def _inside(path: Path, folder: Path) -> bool:
    """True when ``path`` is ``folder`` or lies under it, after resolving links."""
    resolved, base = os.path.realpath(path), os.path.realpath(folder)
    return resolved == base or resolved.startswith(base + os.sep)


def copy_link(src: PathLike, dst: PathLike) -> Path:
    """Recreate the symbolic link ``src`` as ``dst``, whatever it points at. Returns ``dst``.

    The link is read first and the new one is made under a temporary name beside
    ``dst``, then renamed over it, so an existing ``dst`` is never missing and a
    failure leaves it as it was.
    """
    target = Path(dst)
    pointing_at = os.readlink(src)
    while True:
        temp = target.with_name(_temp_name())
        try:
            os.symlink(pointing_at, temp)
            break
        except FileExistsError:
            continue
    try:
        os.replace(temp, target)
    except BaseException:
        _discard(temp)
        raise
    return target


def move(src: PathLike, dst: PathLike) -> Path:
    """Move the file or directory ``src`` to the path ``dst``. Returns ``dst``.

    A rename is tried first. When the two paths are on different filesystems
    (``EXDEV``), or the sandbox refuses a rename across directories (``EXDEV`` or
    ``EPERM``), the data is copied with
    :func:`copy_file`, :func:`copy_tree` or :func:`copy_link` and the source
    removed. A symbolic link moves as a link.
    """
    source = Path(src)
    target = Path(dst)
    try:
        os.replace(source, target)
        return target
    except OSError as error:
        if error.errno not in (errno.EXDEV, errno.EPERM):
            raise
    if source.is_symlink():
        # Like ``shutil.move``: the link moves, not what it points at. A dangling one works too.
        copy_link(source, target)
        os.unlink(source)
    elif source.is_dir():
        copy_tree(source, target)
        shutil.rmtree(source)
    else:
        copy_file(source, target)
        os.unlink(source)
    return target


def _discard(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
