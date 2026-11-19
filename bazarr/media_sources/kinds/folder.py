# coding=utf-8
"""The folder kind: a library is a directory Bazarr+ can read, and the listing is a walk of it."""
import os
import re
from typing import Callable, Optional

from subliminal_patch.core import VIDEO_EXTENSIONS

from . import DiscoveredFile, IdentityHints, Listing, Problem, SourceHealth

# Folders whose files are not the library's own titles.
_SKIPPED_DIRECTORIES = frozenset({'sample', 'samples', 'extras', 'featurettes', 'behind the scenes'})
_SAMPLE_FILE = re.compile(r'(^|[\W_])sample([\W_]|$)', re.IGNORECASE)


def unsigned(value):
    """st_dev and st_ino as unsigned decimal text, whatever width the platform reports them at."""
    return str(value & 0xFFFFFFFFFFFFFFFF)


def is_video_file(name):
    return name.lower().endswith(tuple(VIDEO_EXTENSIONS))


def is_skipped_file(name):
    stem = os.path.splitext(name)[0]
    return name.startswith('.') or bool(_SAMPLE_FILE.search(stem))


def is_skipped_directory(name):
    return name.startswith('.') or name.lower() in _SKIPPED_DIRECTORIES


def config_directory():
    from app.get_args import args
    return os.path.realpath(args.config_dir)


class FolderKind:
    kind = 'folder'

    def validate_library(self, source, library):
        """Refuse a path Bazarr+ cannot use. ``library`` carries ``locator`` and ``content_kind``."""
        problems = []
        path = library.locator
        if not isinstance(path, str) or not os.path.isabs(path):
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
        if library.content_kind != 'movie':
            problems.append(Problem('content_kind', 'Only movie libraries can be scanned so far.'))
        return problems

    def enumerate(self, source, library, cancel: Optional[Callable[[], None]] = None):
        listing = Listing(files=iter(()))
        listing.files = self._walk(library.locator, listing, cancel)
        return listing

    def _walk(self, root, listing, cancel):
        """Every video file under ``root``. Marks the listing complete only when the whole tree was read."""
        seen = set()
        pending = [root]
        readable = True
        while pending:
            if cancel is not None:
                cancel()
            directory = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as error:
                listing.problems.append(f"Cannot read {directory}: {error.strerror or error}")
                readable = False
                continue
            subdirectories = []
            with_files = []
            for entry in entries:
                try:
                    # A directory symlink is not followed; a file symlink is.
                    if entry.is_dir(follow_symlinks=False):
                        if not is_skipped_directory(entry.name):
                            subdirectories.append(entry.path)
                    elif is_video_file(entry.name) and not is_skipped_file(entry.name):
                        with_files.append(entry)
                except OSError as error:
                    listing.problems.append(f"Cannot read {entry.path}: {error.strerror or error}")
                    readable = False
            for entry in with_files:
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
                                     dev=unsigned(info.st_dev), ino=unsigned(info.st_ino))
            pending.extend(reversed(subdirectories))
        listing.complete = readable

    def health(self, source):
        return SourceHealth(ok=True)

    def series_identity(self, source, group_key) -> Optional[IdentityHints]:
        return None
