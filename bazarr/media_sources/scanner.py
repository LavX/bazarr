# coding=utf-8
"""Scan a source: ask its kind what is in each library, then keep the item rows in step with the answer.

Kind-agnostic. It consumes ``SourceKind.enumerate`` and does the rest. It handles movie libraries so far: files
are matched to rows by path, changed files are parsed again, and a row is deleted only when its own path is gone
from a disk whose roots are readable. A listing that is incomplete, or an empty one over a library that holds
items, deletes nothing: an unmounted share must never empty a library.
"""
import errno
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.exc import DataError, IntegrityError

from app.jobs_queue import JobCancelled, JobFailed, jobs_queue

from . import repository
from .db import source_transaction
from .indexer import SourceOutputOwnerIndex, index_item
from .kinds import get_kind
from .kinds.folder import unsigned
from .parse import parse_movie_file

BATCH_SIZE = 500


@dataclass
class LibraryScan:
    library_id: int
    status: str = 'ok'
    message: str = ''
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    held: int = 0
    skipped: int = 0
    item_ids: list = field(default_factory=list)


def _roots_readable(roots):
    return bool(roots) and all(os.path.isdir(root.local_path) and os.access(root.local_path, os.R_OK | os.X_OK)
                               for root in roots)


def _path_is_gone(path):
    """True only when the file is known to be absent. Any other failure to stat it means "do not know"."""
    try:
        os.stat(path)
    except OSError as error:
        return error.errno in (errno.ENOENT, errno.ENOTDIR)
    return False


def _item_values(found, parse, keep_identity=False):
    """The columns a scan writes for a file. An identity the user set is theirs: a changed file keeps it."""
    values = dict(
        file_dev=found.dev, file_ino=found.ino, file_size=found.size, file_mtime_ns=found.mtime_ns,
        scene_name=parse.scene_name, last_seen_at=datetime.now(), missing_scans=0, server_missing=0)
    if not keep_identity:
        values.update(title=parse.title, sort_title=parse.title.lower(), parsed_title=parse.parsed_title,
                      year=str(parse.year) if parse.year else None, identity_source='filename',
                      identity_state=parse.identity_state)
    return values


def _chunks(values, size=None):
    size = size or BATCH_SIZE
    for start in range(0, len(values), size):
        yield values[start:start + size]


def scan_library(source, library, *, cancel=None, session=None) -> LibraryScan:
    """Scan one library and record the outcome on it. Never raises for a readable or unreadable root."""
    result = LibraryScan(library_id=library.id)
    kind = get_kind(source.kind)
    roots = repository.get_roots(library.id, session)
    existing = {row.path: row for row in repository.library_item_paths(library.id, session)}

    if not _roots_readable(roots):
        result.status = 'root_missing'
        result.message = 'The folder is missing or unreadable. Nothing was changed.'
        repository.record_library_scan(library.id, result.status, result.message, session)
        return result

    # The path was checked when the library was added; it is checked again, since a folder can become a link
    # into the configuration folder or a library can be changed in the database.
    problems = kind.validate_library(source, library)
    if problems:
        result.status = 'error'
        result.message = ' '.join(problem.message for problem in problems) + ' Nothing was changed.'
        repository.record_library_scan(library.id, result.status, result.message, session)
        return result

    listing = kind.enumerate(source, library, cancel)
    discovered = list(listing)

    if not discovered and existing:
        # An empty walk over a library that holds items looks like an unmounted share.
        result.status = 'root_missing'
        result.message = ('The folder looks empty while it still holds known files. Nothing was changed. If it '
                          'was emptied on purpose, remove the library.')
        repository.record_library_scan(library.id, result.status, result.message, session)
        return result

    result.status = 'ok' if listing.complete else 'partial'
    if listing.problems:
        result.message = '; '.join(listing.problems[:3])

    # One item per file: a file another library holds is not inserted again, whatever its enabled state.
    holders = repository.holders_of([found.path for found in discovered if found.path not in existing],
                                    library.id, session)
    held_notes = {}
    pending_insert, pending_update = [], []
    for found in discovered:
        row = existing.get(found.path)
        if row is None and found.path in holders:
            name, active = holders[found.path]
            held_notes[(name, active)] = held_notes.get((name, active), 0) + 1
            result.held += 1
        elif row is None:
            pending_insert.append(found)
        elif row.file_size == found.size and row.file_mtime_ns == found.mtime_ns:
            pending_update.append((row.id, found, False, False))
        else:
            pending_update.append((row.id, found, True, row.identity_source == 'user'))

    for batch in _chunks(pending_insert):
        _insert_batch(batch, library, result, session)
        if cancel is not None:
            cancel()

    for batch in _chunks(pending_update):
        # Parsing is slow and holds nothing: it happens before the write lock is taken.
        parses = {found.path: parse_movie_file(found.path) for _, found, changed, _ in batch if changed}
        with source_transaction(session) as connection:
            for item_id, found, changed, user_set in batch:
                result.item_ids.append(item_id)
                if changed:
                    values = _item_values(found, parses[found.path], keep_identity=user_set)
                    # The file changed: its codec cache described the old content.
                    repository.update_item(item_id, connection, ffprobe_cache=None, **values)
                    result.updated += 1
                else:
                    repository.update_item(item_id, connection, last_seen_at=datetime.now(),
                                           file_dev=found.dev, file_ino=found.ino)
                    result.unchanged += 1
        if cancel is not None:
            cancel()

    # Each root's device is recorded below but not yet compared here: refusing to delete after a root moved to
    # another device belongs to the scanner's device-change rules, with the group and rename handling.
    if listing.complete:
        found_paths = {found.path for found in discovered}
        gone = [row.id for path, row in existing.items() if path not in found_paths and _path_is_gone(path)]
        for batch in _chunks(gone):
            with source_transaction(session) as connection:
                repository.delete_items(batch, connection)
            result.deleted += len(batch)

    for root in roots:
        try:
            repository.record_root_device(root.id, unsigned(os.stat(root.local_path).st_dev), session)
        except OSError:
            logging.debug('BAZARR could not read the device of %s', root.local_path)

    # The subtitle listing is rebuilt for every item on every scan, whether or not its video changed. One
    # ownership index serves the whole pass.
    ownership_index = SourceOutputOwnerIndex()
    for item_id in result.item_ids:
        if cancel is not None:
            cancel()
        index_item(item_id, session, ownership_index)

    summary = (f"{result.inserted} added, {result.updated} changed, {result.deleted} removed, "
               f"{result.unchanged + result.updated + result.inserted} files")
    for (name, active), count in sorted(held_notes.items()):
        summary += (f"; {count} already supplied by {name}" if active
                    else f"; {count} held by {name}, which is disabled")
    if result.skipped:
        summary += f"; {result.skipped} could not be added (see the log)"
    if listing.skipped_names:
        summary += f"; {listing.skipped_names} file(s) with a name that is not valid UTF-8 were skipped"
    result.message = f"{summary}. {result.message}".strip() if result.message else summary
    repository.record_library_scan(library.id, result.status, result.message, session)
    return result


def _insert_batch(batch, library, result, session):
    """Insert a batch in one transaction. When the batch is refused as a whole (a path another row took in the
    meantime, a name the driver cannot bind), its files are tried one at a time: the ones that cannot be added
    are logged and skipped, and the rest are kept."""
    parses = [(found, parse_movie_file(found.path)) for found in batch]

    def insert(connection, found, parse):
        return repository.insert_item(connection, source_id=library.source_id, library_id=library.id,
                                      kind='movie', path=found.path, **_item_values(found, parse))
    try:
        with source_transaction(session) as connection:
            ids = [insert(connection, found, parse) for found, parse in parses]
    except (IntegrityError, DataError):
        for found, parse in parses:
            try:
                with source_transaction(session) as connection:
                    result.item_ids.append(insert(connection, found, parse))
                result.inserted += 1
            except (IntegrityError, DataError):
                logging.warning('BAZARR the source file could not be added and was skipped: %s', found.path)
                result.skipped += 1
        return
    result.item_ids.extend(ids)
    result.inserted += len(ids)


def run_scan(source_id, library_id=None, *, cancel=None, session=None):
    """Scan the enabled libraries of a source, or one library. Returns the per-library results."""
    source = repository.get_source(source_id, session)
    if source is None:
        raise LookupError(f'Media source {source_id} does not exist')
    if library_id is not None:
        library = repository.get_library(library_id, session)
        if library is None or library.source_id != source_id:
            raise LookupError(f'Library {library_id} does not belong to media source {source_id}')
        libraries = [library]
    else:
        libraries = repository.list_libraries(source_id, session)
    # A disabled source or library is not scanned, whoever asked: its items stay listed and nothing else.
    libraries = [row for row in libraries if source.enabled and row.enabled]
    if not libraries:
        return []
    repository.record_source_scan(source_id, started=datetime.now(), target=session)
    results = []
    status = 'ok'
    try:
        for library in libraries:
            outcome = scan_library(source, library, cancel=cancel, session=session)
            results.append(outcome)
            if outcome.status != 'ok':
                status = 'partial'
    except BaseException as error:
        # A stop is not a failure: what was read before it was kept.
        repository.record_source_scan(source_id, finished=datetime.now(), target=session,
                                      status='partial' if isinstance(error, JobCancelled) else 'error')
        raise
    repository.record_source_scan(source_id, finished=datetime.now(), status=status, target=session)
    return results


def scan_media_source(source_id, library_id=None, job_id=None):
    """The scan as a job. Called without a job id it queues itself, so it shows in the Jobs drawer."""
    if not job_id:
        return jobs_queue.add_job_from_function(f"Scanning media source {_source_name(source_id)}",
                                                is_progress=True)
    reason = disabled_reason(source_id, library_id)
    if reason:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message=reason)
        return None
    jobs_queue.update_job_progress(job_id=job_id, progress_message='Scanning')
    try:
        results = run_scan(source_id, library_id, cancel=lambda: jobs_queue.raise_if_cancelled(job_id))
    except LookupError as error:
        raise JobFailed(str(error)) from error
    jobs_queue.update_job_progress(
        job_id=job_id, progress_value='max',
        progress_message=', '.join(f"{result.message}" for result in results) or 'Nothing to scan')
    return None


def disabled_reason(source_id, library_id=None, session=None):
    """Why a source (or one of its libraries) must not be scanned, or None."""
    source = repository.get_source(source_id, session)
    if source is not None and not source.enabled:
        return f'The media source {source.name} is disabled'
    if library_id is not None:
        library = repository.get_library(library_id, session)
        if library is not None and not library.enabled:
            return 'The library is disabled'
    elif source is not None and not any(row.enabled for row in repository.list_libraries(source_id, session)):
        return f'The media source {source.name} has no enabled library'
    return None


def _source_name(source_id):
    source = repository.get_source(source_id)
    return source.name if source is not None else str(source_id)
