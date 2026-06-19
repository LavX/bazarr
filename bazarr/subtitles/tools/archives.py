# coding=utf-8
"""Extract subtitle files out of an uploaded archive (.zip/.rar/.7z).

Users can upload a compressed file instead of pre-extracting it; we pull out
just the subtitle entries and discard everything else, then feed each one
through the normal per-file upload flow.
See https://github.com/LavX/bazarr/issues/233
"""
import os
import zipfile
from io import BytesIO

ARCHIVE_EXTENSIONS = ('.zip', '.rar', '.7z')

# Guards against archive bombs: cap how many subtitle entries we return and the
# total uncompressed payload we hold in memory.
_MAX_ENTRIES = 500
_MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MiB


class ArchiveError(Exception):
    """Raised when an archive is unsupported, corrupt, or unreadable."""


def is_archive(filename):
    """Whether ``filename`` looks like a supported archive by extension."""
    return bool(filename) and filename.lower().endswith(ARCHIVE_EXTENSIONS)


def _is_subtitle(name):
    # Mirror the upload endpoint's accepted set so extracted files pass the same
    # validation. Imported lazily to avoid importing the heavy provider stack.
    from subliminal_patch.core import SUBTITLE_EXTENSIONS
    return name.lower().endswith(tuple(SUBTITLE_EXTENSIONS))


def _keep(name):
    """A returnable subtitle entry: a real subtitle file, not a directory or a
    macOS resource-fork sidecar (the ``__MACOSX/._name`` entries zip adds)."""
    if not name or name.endswith('/'):
        return False
    base = os.path.basename(name.replace('\\', '/'))
    if not base or base.startswith('._') or '__MACOSX' in name.split('/'):
        return False
    return _is_subtitle(base)


def _collect(pairs):
    """Apply the per-archive guards to an iterable of (name, content) pairs and
    return [(basename, content), ...]. ``basename`` defuses zip-slip paths."""
    out = []
    total = 0
    for name, content in pairs:
        if len(out) >= _MAX_ENTRIES:
            break
        total += len(content)
        if total > _MAX_TOTAL_BYTES:
            raise ArchiveError("Archive expands beyond the allowed size limit.")
        out.append((os.path.basename(name.replace('\\', '/')), content))
    return out


def _extract_zip(data):
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return _collect(
                (info.filename, zf.read(info))
                for info in zf.infolist()
                if not info.is_dir() and _keep(info.filename)
            )
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"Not a valid zip archive: {e}") from e


def _extract_rar(data):
    import rarfile
    try:
        with rarfile.RarFile(BytesIO(data)) as rf:
            return _collect(
                (info.filename, rf.read(info))
                for info in rf.infolist()
                if not info.is_dir() and _keep(info.filename)
            )
    except rarfile.Error as e:
        raise ArchiveError(f"Could not read rar archive: {e}") from e


def _extract_7z(data):
    import py7zr
    from py7zr.io import BufferOverflow, BytesIOFactory
    # Extract into memory (never to disk) so a traversal member name cannot
    # escape onto the filesystem; the factory also caps total memory.
    factory = BytesIOFactory(_MAX_TOTAL_BYTES)
    try:
        with py7zr.SevenZipFile(BytesIO(data), 'r') as zf:
            zf.extractall(factory=factory)
    except BufferOverflow as e:
        raise ArchiveError("Archive expands beyond the allowed size limit.") from e
    except py7zr.exceptions.ArchiveError as e:
        raise ArchiveError(f"Could not read 7z archive: {e}") from e
    return _collect(
        (name, bio.read())
        for name, bio in factory.products.items()
        if _keep(name)
    )


def extract_subtitles_from_archive(filename, data):
    """Return ``[(basename, content_bytes), ...]`` for the subtitle entries in
    the archive ``data``, discarding directories and non-subtitle files.

    ``filename`` selects the format by extension. Raises :class:`ArchiveError`
    for an unsupported extension or a corrupt/unreadable archive.
    """
    lower = (filename or "").lower()
    if lower.endswith('.zip'):
        return _extract_zip(data)
    if lower.endswith('.rar'):
        return _extract_rar(data)
    if lower.endswith('.7z'):
        return _extract_7z(data)
    raise ArchiveError(f"Unsupported archive type: {filename}")
