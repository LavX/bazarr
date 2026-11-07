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

Every write goes to a temporary name beside the target and is renamed into
place, so a reader never sees a half-written file.
"""

from __future__ import annotations

import errno
import os
import shutil

from pathlib import Path
from typing import Union

PathLike = Union[str, "os.PathLike[str]"]

__all__ = ["copy_file", "copy_tree", "move", "touch", "write_file"]


def _temp_beside(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.part")


def copy_file(src: PathLike, dst: PathLike) -> Path:
    """Copy the contents of ``src`` to the file ``dst``, replacing it. Returns ``dst``.

    ``dst`` is the target file, not a directory to copy into.
    """
    target = Path(dst)
    temp = _temp_beside(target)
    try:
        shutil.copyfile(src, temp)
        os.replace(temp, target)
    except BaseException:
        _discard(temp)
        raise
    return target


def write_file(path: PathLike, data: Union[bytes, str], *, encoding: str = "utf-8") -> Path:
    """Write ``data`` to ``path`` through a temporary file and a rename. Returns ``path``."""
    target = Path(path)
    temp = _temp_beside(target)
    payload = data.encode(encoding) if isinstance(data, str) else data
    try:
        with open(temp, "wb") as handle:
            handle.write(payload)
        os.replace(temp, target)
    except BaseException:
        _discard(temp)
        raise
    return target


def touch(path: PathLike) -> Path:
    """Create an empty file if ``path`` does not exist. An existing file is left as it is.

    Unlike ``Path.touch`` it does not update the modification time.
    """
    target = Path(path)
    with open(target, "ab"):
        pass
    return target


def copy_tree(src: PathLike, dst: PathLike, *, dirs_exist_ok: bool = False, symlinks: bool = True) -> Path:
    """Copy the directory ``src`` to ``dst``. Returns ``dst``.

    Directories are created with the default mode, files are copied by
    :func:`copy_file`. With ``symlinks`` (the default) a link is recreated as a
    link; without it the file or directory it points to is copied.
    ``dirs_exist_ok`` allows ``dst`` to exist already, as in ``shutil.copytree``.
    """
    source = Path(src)
    target = Path(dst)
    if not source.is_dir():
        raise NotADirectoryError(str(source))
    target.mkdir(parents=True, exist_ok=dirs_exist_ok)
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        destination = target / entry.name
        if entry.is_symlink() and symlinks:
            _discard(destination)
            os.symlink(os.readlink(entry.path), destination)
        elif entry.is_dir():
            copy_tree(entry.path, destination, dirs_exist_ok=dirs_exist_ok, symlinks=symlinks)
        else:
            copy_file(entry.path, destination)
    return target


def move(src: PathLike, dst: PathLike) -> Path:
    """Move the file or directory ``src`` to the path ``dst``. Returns ``dst``.

    A rename is tried first. When the two paths are on different filesystems,
    or the sandbox refuses a rename across directories, the data is copied with
    :func:`copy_file` or :func:`copy_tree` and the source removed.
    """
    source = Path(src)
    target = Path(dst)
    try:
        os.replace(source, target)
        return target
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    if source.is_dir() and not source.is_symlink():
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
