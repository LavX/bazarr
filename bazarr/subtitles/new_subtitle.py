# coding=utf-8
"""Writing a subtitle file that did not exist before.

The editor's create route and Extract to file both put a new external
subtitle next to a video, and OCR will too. They share this: the names are
checked and the files written under the directory locks every replacement
takes, each file atomically, and an existing file is never overwritten. A
VobSub pair (.idx and .sub) is one write: both names are checked before
either is written, and a failure part way leaves neither behind.

The locks only hold Bazarr+'s own writers back. Another program (an arr
importing extra files, say) can create the name between the check and the
publish, so a new file is published with a hard link, which fails when the
name exists, rather than a rename, which would replace it.
"""

import errno
import logging
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import nullcontext

from app.config import settings
from media_servers.events import publication_callback
from subtitles.tools.subsync_engines import subtitle_mutation, subtitle_write_locks

logger = logging.getLogger(__name__)

# What a filesystem without hard links (FAT, exFAT, many SMB mounts) answers
# os.link with. Anything else is a real failure.
_NO_HARD_LINKS = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EMLINK, errno.ENOSYS}


class SubtitleFileExists(Exception):
    """A file with the name a new subtitle would take is already there."""

    def __init__(self, path):
        super().__init__(f'A subtitle file named {os.path.basename(path)} already exists')
        self.path = path


# A new file is created with O_EXCL, never through an existing name, and in
# binary mode on Windows.
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)


def _new_staging_file(directory):
    """Create a uniquely named temporary file in ``directory``: ``(fd, path)``.

    Created with mode 0o666, so the kernel applies the umask the way it does
    for a downloaded subtitle. mkstemp creates 0o600, which a link or rename
    keeps, and a subtitle a media server running as another user cannot read
    is no use to it.
    """
    for _attempt in range(100):
        path = os.path.join(directory, f'.bazarr-new-{secrets.token_hex(8)}.tmp')
        try:
            return os.open(path, _NEW_FILE_FLAGS, 0o666), path
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, 'No free temporary file name', directory)


def _staging_file(directory, replace):
    # A replacement keeps the existing temporary-file behaviour of the routes
    # that save over a file.
    return tempfile.mkstemp(dir=directory) if replace else _new_staging_file(directory)


def _fd_identity(fd):
    try:
        stat = os.fstat(fd)
    except (OSError, TypeError, ValueError):
        return None
    return stat.st_dev, stat.st_ino


def file_identity(path):
    """``(device, inode)`` of the file at ``path``, or None when there is none.

    The writers below return the identity of the file they published, taken
    from the file they wrote rather than read back from the name, so a
    rollback can tell its own file from one another program put there.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def discard_if_owned(path, identity):
    """Remove ``path`` only when it is still the file ``identity`` names; say whether it was."""
    if identity is not None and file_identity(path) == identity:
        _discard(path)
        return True
    logger.warning('BAZARR left %s in place: it is no longer the file Bazarr+ wrote there', path)
    return False


def _discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning('BAZARR could not remove the temporary file %s', path)


def _write_all(fd, data):
    """os.write may write less than it was given; write until every byte is out."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _publish_without_replacing(tmp_path, path):
    """Give the finished ``tmp_path`` the name ``path``, never over a file already there.

    Returns the identity of a file the fallback created, or None when the
    name is a link to the temporary file itself.
    """
    created = None
    try:
        os.link(tmp_path, path)
    except FileExistsError:
        _discard(tmp_path)
        raise SubtitleFileExists(path) from None
    except OSError as error:
        if error.errno not in _NO_HARD_LINKS:
            _discard(tmp_path)
            raise
        created = _copy_into_new_name(tmp_path, path)
    _discard(tmp_path)
    return created


def _copy_into_new_name(tmp_path, path):
    """The fallback without hard links: claim the name exclusively, then fill it."""
    try:
        fd = os.open(path, _NEW_FILE_FLAGS, 0o666)
    except FileExistsError:
        _discard(tmp_path)
        raise SubtitleFileExists(path) from None
    except BaseException:
        _discard(tmp_path)
        raise
    identity = _fd_identity(fd)
    try:
        with os.fdopen(fd, 'wb') as target, open(tmp_path, 'rb') as origin:
            shutil.copyfileobj(origin, target)
    except BaseException:
        # The name was claimed by this call, so the partial file is ours.
        discard_if_owned(path, identity)
        _discard(tmp_path)
        raise
    return identity


def _backup_for_replacement(path):
    """Copy the file at ``path`` beside it and return the copy's name.

    The copy is the only thing that can put the original back when a write is
    rejected after it has already replaced the file, for example by the sports
    publication boundary. Returns None when there is no file to keep.
    """
    try:
        source = open(path, 'rb')
    except FileNotFoundError:
        return None
    with source:
        fd, backup = tempfile.mkstemp(prefix='.bazarr-replaced-', suffix='.tmp',
                                      dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'wb') as target:
                shutil.copyfileobj(source, target)
            shutil.copymode(path, backup)
        except BaseException:
            _discard(backup)
            raise
    return backup


def _publish(tmp_path, path, replace):
    if replace:
        # os.replace keeps the temporary file's mode (mkstemp creates 0600), so
        # publish with the mode the destination already had, or with the mode a
        # new subtitle would get when there was no destination.
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            return _publish_without_replacing(tmp_path, path)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, mode)
        except OSError:
            logger.warning('BAZARR could not keep the mode of %s', path)
        return None
    return _publish_without_replacing(tmp_path, path)


def write_bytes_atomically(path, data, *, replace=True):
    """Write ``data`` to ``path`` through a temporary name in the same folder.

    ``replace=False`` raises SubtitleFileExists instead of replacing a file
    that is there by the time the write is published. Returns the published
    file's identity (file_identity()), or None when it could not be taken.
    """
    fd, tmp_path = _staging_file(os.path.dirname(path), replace)
    try:
        identity = _fd_identity(fd)
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)

        return _publish(tmp_path, path, replace) or identity
    except SubtitleFileExists:
        raise
    except Exception:
        _discard(tmp_path)
        raise


def copy_file_atomically(path, source, *, replace=True):
    """Copy ``source`` to ``path`` through a temporary name in the same folder.

    Returns the published file's identity, as write_bytes_atomically does.
    """
    fd, tmp_path = _staging_file(os.path.dirname(path), replace)
    try:
        identity = _fd_identity(fd)
        with os.fdopen(fd, 'wb') as target, open(source, 'rb') as origin:
            shutil.copyfileobj(origin, target)
        return _publish(tmp_path, path, replace) or identity
    except SubtitleFileExists:
        raise
    except Exception:
        _discard(tmp_path)
        raise


def apply_subtitle_chmod(path):
    if settings.general.chmod_enabled:
        try:
            chmod_value = int(settings.general.chmod, 8)
            os.chmod(path, chmod_value)
        except Exception:
            pass


def write_new_subtitle(video_path, outputs, media_type, arr_instance_id=None, *, operation='edit',
                       publication_guard=None, also_check=(), also_refuse=None,
                       replace=False, replaced=None):
    """Write each ``(path, data)`` of ``outputs`` as a new file and publish it.

    ``data`` is the file's bytes, or the path of a file to copy. Raises
    SubtitleFileExists, and writes nothing, when any of the paths or of
    ``also_check`` is already a file, including one another program created
    while the file was being written. ``publication_guard`` is the sports
    owned boundary (called with the first path); the other media types pass
    None. ``also_refuse`` is called under the locks and returns a path that
    stops the write, or None, for files no fixed list of names covers.
    Returns the written paths, first one first.

    ``replace`` lets the paths themselves be replaced, never ``also_check``.
    Only the editor passes it, for its one file, after the user confirmed the
    overwrite dialog; Extract to file and OCR never do.

    ``replaced``, when given, is a one-element list the write fills with
    whether the first path was already a file under its own lock. The editor's
    create route reads it to answer 200 or 201 from the state the write saw,
    not from a check taken before the lock. It is filled before the write, so
    a caller must not read it after a raised SubtitleFileExists.
    """
    paths = [path for path, _data in outputs]
    with subtitle_write_locks(video_path, *paths):
        if replaced is not None:
            replaced.append(os.path.isfile(paths[0]))
        for path in (*(() if replace else paths), *also_check):
            if os.path.isfile(path):
                raise SubtitleFileExists(path)
        refused = also_refuse() if also_refuse is not None else None
        if refused:
            raise SubtitleFileExists(refused)
        with subtitle_mutation(video_path, *paths):
            written = []
            backups = []
            try:
                with (publication_guard(output_path=paths[0])
                      if publication_guard is not None else nullcontext()):
                    for path, data in outputs:
                        if replace:
                            backup = _backup_for_replacement(path)
                            if backup is not None:
                                backups.append((path, backup))
                        if isinstance(data, (bytes, bytearray)):
                            identity = write_bytes_atomically(path, data, replace=replace)
                        else:
                            identity = copy_file_atomically(path, data, replace=replace)
                        written.append((path, identity))
                # Only after the publication boundary has accepted the write:
                # its rejection on the way out must not reach the media servers
                # as a mutation, and it must be able to put the original back.
                publication_callback(media_type, video_path, operation, arr_instance_id)(paths[0])
                for path in paths:
                    apply_subtitle_chmod(path)
            except BaseException:
                # A pair is published whole or not at all, but only the files
                # this call wrote are taken back: another program may have put
                # its own under a name meanwhile. A replacement also puts back
                # what it found, because the boundary that rejected it runs
                # after the file was already replaced.
                for path, identity in written:
                    discard_if_owned(path, identity)
                for path, backup in reversed(backups):
                    try:
                        os.replace(backup, path)
                    except OSError:
                        logger.warning('BAZARR could not put %s back after a failed save', path)
                raise
            else:
                for _path, backup in backups:
                    _discard(backup)
    return paths
