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


def _guarded(entries):
    """Read subtitle entries lazily under the per-archive caps.

    ``entries`` yields ``(name, declared_size, read)`` where ``read()`` returns
    the member bytes. The declared (uncompressed) size is checked BEFORE the
    member is inflated so a single bomb member cannot blow past the budget, and
    the entry count is capped. Returns ``[(basename, content), ...]``;
    ``basename`` defuses zip-slip paths.
    """
    out = []
    total = 0
    for name, declared_size, read in entries:
        if len(out) >= _MAX_ENTRIES:
            raise ArchiveError(
                f"Archive contains more than {_MAX_ENTRIES} subtitle files.")
        total += declared_size or 0
        if total > _MAX_TOTAL_BYTES:
            raise ArchiveError("Archive expands beyond the allowed size limit.")
        out.append((os.path.basename(name.replace('\\', '/')), read()))
    return out


def _read_member(reader, info, label):
    """Read one archive member, mapping any read failure (encrypted member,
    CRC error, truncated data, ...) to ArchiveError instead of leaking the
    backend library's own exception type."""
    try:
        return reader(info)
    except ArchiveError:
        raise
    except Exception as e:
        raise ArchiveError(f"Could not read {label} member: {e}") from e


def _extract_zip(data):
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"Not a valid zip archive: {e}") from e
    with zf:
        return _guarded(
            (info.filename, info.file_size,
             lambda info=info: _read_member(zf.read, info, "zip"))
            for info in zf.infolist()
            if not info.is_dir() and _keep(info.filename)
        )


def _extract_rar(data):
    import rarfile
    try:
        rf = rarfile.RarFile(BytesIO(data))
    except rarfile.Error as e:
        raise ArchiveError(f"Could not read rar archive: {e}") from e
    with rf:
        return _guarded(
            (info.filename, info.file_size,
             lambda info=info: _read_member(rf.read, info, "rar"))
            for info in rf.infolist()
            if not info.is_dir() and _keep(info.filename)
        )


def _extract_7z(data):
    import py7zr
    from py7zr.exceptions import ArchiveError as Py7zError
    from py7zr.exceptions import PasswordRequired
    from py7zr.io import BufferOverflow, BytesIOFactory
    # Extract into memory (never to disk) so a traversal member name cannot
    # escape onto the filesystem. Only the subtitle members are targeted so
    # non-subtitle files are never inflated. py7zr's BytesIOFactory limit is
    # per-output-object, not cumulative, so enforce the entry and total-size
    # budgets from the header BEFORE extracting anything.
    factory = BytesIOFactory(_MAX_TOTAL_BYTES)
    try:
        with py7zr.SevenZipFile(BytesIO(data), 'r') as zf:
            members = [fi for fi in zf.list()
                       if not fi.is_directory and _keep(fi.filename)]
            if not members:
                return []
            if len(members) > _MAX_ENTRIES:
                raise ArchiveError(
                    f"Archive contains more than {_MAX_ENTRIES} subtitle files.")
            if sum(fi.uncompressed or 0 for fi in members) > _MAX_TOTAL_BYTES:
                raise ArchiveError(
                    "Archive expands beyond the allowed size limit.")
            zf.reset()
            zf.extract(targets=[fi.filename for fi in members], factory=factory)
    except ArchiveError:
        raise
    except PasswordRequired as e:
        raise ArchiveError("Encrypted 7z archives are not supported.") from e
    except BufferOverflow as e:
        raise ArchiveError("Archive expands beyond the allowed size limit.") from e
    except Py7zError as e:
        raise ArchiveError(f"Could not read 7z archive: {e}") from e
    return [(os.path.basename(name.replace('\\', '/')), bio.read())
            for name, bio in factory.products.items()]


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
