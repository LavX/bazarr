# coding=utf-8
"""The folder kind: a library is a directory Bazarr+ can read, and the listing is a walk of it."""
import errno
import logging
import os
import re
from typing import Callable, Optional

from subliminal_patch.core import VIDEO_EXTENSIONS

from ..groups import DirState, stat_directory
from ..identity import unsigned
from . import DiscoveredFile, IdentityHints, Listing, Problem, SourceHealth

# Folders whose files are not the library's own titles.
_SKIPPED_DIRECTORIES = frozenset({'sample', 'samples', 'extras', 'featurettes', 'behind the scenes'})
_SAMPLE_FILE = re.compile(r'(^|[\W_])sample([\W_]|$)', re.IGNORECASE)


def is_video_file(name):
    return name.lower().endswith(tuple(VIDEO_EXTENSIONS))


def is_skipped_file(name):
    stem = os.path.splitext(name)[0]
    return name.startswith('.') or bool(_SAMPLE_FILE.search(stem))


def is_storable(path):
    try:
        path.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def is_skipped_directory(name):
    return name.startswith('.') or name.lower() in _SKIPPED_DIRECTORIES


def config_directory():
    from app.get_args import args
    return os.path.realpath(args.config_dir)


class FolderKind:
    kind = 'folder'
    groups_are_directories = True

    def validate_library(self, source, library):
        """Refuse a path Bazarr+ cannot use. ``library`` carries ``locator`` and ``content_kind``."""
        path = library.locator
        if not isinstance(path, str) or '\x00' in path:
            return [Problem('not_absolute', 'The folder path must be absolute.')]
        try:
            return self._validate(path, library)
        except (OSError, ValueError):
            return [Problem('unreadable', 'Bazarr+ cannot read the folder.')]

    def _validate(self, path, library):
        problems = []
        if not os.path.isabs(path):
            return [Problem('not_absolute', 'The folder path must be absolute.')]
        if os.path.normpath(path) == os.path.abspath(os.sep):
            problems.append(Problem('root', 'The root of the filesystem cannot be a library.'))
        if not os.path.isdir(path):
            return problems + [Problem('not_a_directory', 'The folder does not exist or is not a directory.')]
        if not os.access(path, os.R_OK | os.X_OK):
            problems.append(Problem('unreadable', 'Bazarr+ cannot read the folder.'))
        config = config_directory()
        real = os.path.realpath(path)
        if real == config or real.startswith(config + os.sep) or config.startswith(real.rstrip(os.sep) + os.sep):
            problems.append(Problem('config_directory', "The folder cannot contain or sit inside Bazarr+'s own "
                                                        "configuration folder."))
        if library.content_kind not in ('movie', 'series'):
            problems.append(Problem('content_kind', 'Only movie and series libraries can be scanned so far.'))
        return problems

    def enumerate(self, source, library, cancel: Optional[Callable[[], None]] = None):
        listing = Listing(files=iter(()))
        listing.files = self._walk(library.locator, listing, cancel, grouped=library.content_kind != 'movie')
        return listing

    def _walk(self, root, listing, cancel, grouped=False):
        """Every video file under ``root``. Marks the listing complete only when the whole tree was read.

        In a series or sports library each first-level directory is a show or league: the files below it carry
        its path as their group key, and a video file directly in the root has no group and is only counted."""
        seen = set()
        pending = [(root, None, stat_directory(root))]
        readable = True
        while pending:
            if cancel is not None:
                cancel()
            directory, group_key, state = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as error:
                listing.problems.append(f"Cannot read {directory}: {error.strerror or error}")
                readable = False
                if error.errno in (errno.ENOENT, errno.ENOTDIR):
                    listing.unstable = True
                continue
            listing.directory_states[directory] = state
            subdirectories = []
            with_files = []
            for entry in entries:
                try:
                    # A directory symlink is not followed; a file symlink is.
                    if entry.is_dir(follow_symlinks=False):
                        if not is_skipped_directory(entry.name):
                            info = entry.stat(follow_symlinks=False)
                            state = DirState('dir', unsigned(info.st_dev), unsigned(info.st_ino))
                            subdirectories.append((entry.path, entry.path if grouped and group_key is None
                                                   else group_key, state))
                    elif is_video_file(entry.name) and not is_skipped_file(entry.name):
                        with_files.append(entry)
                except OSError as error:
                    listing.problems.append(f"Cannot read {entry.path}: {error.strerror or error}")
                    readable = False
                    if error.errno in (errno.ENOENT, errno.ENOTDIR):
                        listing.unstable = True
            for entry in with_files:
                if grouped and group_key is None:
                    listing.ungrouped += 1
                    continue
                if not is_storable(entry.path):
                    # os.scandir hands back such a name as surrogate escapes, which neither database driver can
                    # bind: one of them would fail the scan of the whole library. No stored row can have this
                    # name, so leaving it out loses nothing and the listing stays complete.
                    logging.warning('BAZARR skipping a file whose name is not valid UTF-8: %r', entry.path)
                    listing.skipped_names += 1
                    continue
                try:
                    info = entry.stat()
                except OSError as error:
                    listing.problems.append(f"Cannot read {entry.path}: {error.strerror or error}")
                    readable = False
                    continue
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                yield DiscoveredFile(path=entry.path, size=info.st_size, mtime_ns=info.st_mtime_ns,
                                     dev=unsigned(info.st_dev), ino=unsigned(info.st_ino), group_key=group_key)
            pending.extend(reversed(subdirectories))
        # DirEntry and the videos yielded above describe the filesystem at traversal time. Verify the directory
        # identities again so a bare mount point or a same-device replacement cannot validate that old view.
        for directory, state in listing.directory_states.items():
            if stat_directory(directory) != state or not os.access(directory, os.R_OK | os.X_OK):
                listing.unstable = True
                break
        listing.complete = readable and not listing.unstable

    def health(self, source):
        return SourceHealth(ok=True)

    def series_identity(self, source, group_key) -> Optional[IdentityHints]:
        return None
