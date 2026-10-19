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
import shutil
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
    """Give the finished ``tmp_path`` the name ``path``, never over a file already there."""
    try:
        os.link(tmp_path, path)
    except FileExistsError:
        _discard(tmp_path)
        raise SubtitleFileExists(path) from None
    except OSError as error:
        if error.errno not in _NO_HARD_LINKS:
            _discard(tmp_path)
            raise
        _copy_into_new_name(tmp_path, path)
    _discard(tmp_path)


def _copy_into_new_name(tmp_path, path):
    """The fallback without hard links: claim the name exclusively, then fill it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        _discard(tmp_path)
        raise SubtitleFileExists(path) from None
    except BaseException:
        _discard(tmp_path)
        raise
    try:
        with os.fdopen(fd, 'wb') as target, open(tmp_path, 'rb') as origin:
            shutil.copyfileobj(origin, target)
    except BaseException:
        # The name was claimed by this call, so the partial file is ours.
        _discard(path)
        _discard(tmp_path)
        raise


def _publish(tmp_path, path, replace):
    if replace:
        os.replace(tmp_path, path)
    else:
        _publish_without_replacing(tmp_path, path)


def write_bytes_atomically(path, data, *, replace=True):
    """Write ``data`` to ``path`` through a temporary name in the same folder.

    ``replace=False`` raises SubtitleFileExists instead of replacing a file
    that is there by the time the write is published.
    """
    subtitle_dir = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=subtitle_dir)
    try:
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)

        _publish(tmp_path, path, replace)
    except SubtitleFileExists:
        raise
    except Exception:
        _discard(tmp_path)
        raise


def copy_file_atomically(path, source, *, replace=True):
    """Copy ``source`` to ``path`` through a temporary name in the same folder."""
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'wb') as target, open(source, 'rb') as origin:
            shutil.copyfileobj(origin, target)
        _publish(tmp_path, path, replace)
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
                       publication_guard=None, also_check=()):
    """Write each ``(path, data)`` of ``outputs`` as a new file and publish it.

    ``data`` is the file's bytes, or the path of a file to copy. Raises
    SubtitleFileExists, and writes nothing, when any of the paths or of
    ``also_check`` is already a file, including one another program created
    while the file was being written. ``publication_guard`` is the sports
    owned boundary (called with the first path); the other media types pass
    None. Returns the written paths, first one first.
    """
    paths = [path for path, _data in outputs]
    with subtitle_write_locks(video_path, *paths):
        for path in (*paths, *also_check):
            if os.path.isfile(path):
                raise SubtitleFileExists(path)
        with subtitle_mutation(video_path, *paths):
            with (publication_guard(output_path=paths[0])
                  if publication_guard is not None else nullcontext()):
                written = []
                try:
                    for path, data in outputs:
                        if isinstance(data, (bytes, bytearray)):
                            write_bytes_atomically(path, data, replace=False)
                        else:
                            copy_file_atomically(path, data, replace=False)
                        written.append(path)
                except BaseException:
                    # A pair is published whole or not at all.
                    for path in written:
                        _discard(path)
                    raise
                publication_callback(media_type, video_path, operation, arr_instance_id)(paths[0])
                for path in paths:
                    apply_subtitle_chmod(path)
    return paths
