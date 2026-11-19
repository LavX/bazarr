# coding=utf-8
"""Scan a source: ask its kind what is in each library, then keep the item rows in step with the answer.

Kind-agnostic. It consumes ``SourceKind.enumerate`` and does the rest, in three phases per library: walk and match
every file without writing, resolve the groups, then write in batches. A row is deleted only when its own stored
path is absent from a disk whose roots are readable, or when its path was reused by another row's file; a
listing that is incomplete, an empty one over a library that holds items, or a root that moved to another device
and holds none of the known files deletes nothing. An unmounted share must never empty a library.
"""
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.exc import DataError, IntegrityError

from app.jobs_queue import JobCancelled, JobFailed, jobs_queue

from . import groups as group_rules
from . import identify, identity, repository
from .db import source_transaction
from .indexer import SourceOutputOwnerIndex, index_item
from .kinds import get_kind
from .kinds.folder import unsigned

BATCH_SIZE = 500
# A scan that started this long ago and never finished is taken for a crashed one, not a running one.
STALE_SCAN_HOURS = 6


@dataclass
class LibraryScan:
    library_id: int
    status: str = 'ok'
    message: str = ''
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    moved: int = 0
    handed_over: int = 0
    held: int = 0
    skipped: int = 0
    deferred: int = 0
    item_ids: list = field(default_factory=list)


def _roots_readable(roots):
    return bool(roots) and all(os.path.isdir(root.local_path) and os.access(root.local_path, os.R_OK | os.X_OK)
                               for root in roots)


def _under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _chunks(values, size=None):
    size = size or BATCH_SIZE
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _file_values(found):
    return dict(file_dev=found.dev, file_ino=found.ino, file_size=found.size, file_mtime_ns=found.mtime_ns,
                last_seen_at=datetime.now(), missing_scans=0, server_missing=0)


def _item_values(found, identified, keep_identity=False):
    """The columns a scan writes for a file. An identity the user set is theirs: a changed or moved file keeps
    it, and only what the name alone says (the release name) is recomputed."""
    values = _file_values(found)
    if keep_identity:
        values['scene_name'] = identified['scene_name']
    else:
        values.update(identified)
    return values


def _root_devices(roots):
    """``{local_path: (current device, changed)}``. A root whose device differs from the one recorded at the
    last scan (a remount, a re-enumerated drive) compares inodes alone for this scan."""
    devices = {}
    for root in roots:
        try:
            current = unsigned(os.stat(root.local_path).st_dev)
        except OSError:
            continue
        devices[root.local_path] = (current, root.last_dev is not None and root.last_dev != current)
    return devices


def scan_library(source, library, *, cancel=None, session=None) -> LibraryScan:
    """Scan one library and record the outcome on it. Never raises for a readable or unreadable root."""
    result = LibraryScan(library_id=library.id)
    kind = get_kind(source.kind)
    roots = repository.get_roots(library.id, session)
    rows = repository.library_items(library.id, session)

    def refuse(status, message):
        result.status = status
        result.message = message
        repository.record_library_scan(library.id, status, message, session)
        return result

    if not _roots_readable(roots):
        return refuse('root_missing', 'The folder is missing or unreadable. Nothing was changed.')

    # The path was checked when the library was added; it is checked again, since a folder can become a link
    # into the configuration folder or a library can be changed in the database.
    problems = kind.validate_library(source, library)
    if problems:
        return refuse('error', ' '.join(problem.message for problem in problems) + ' Nothing was changed.')

    listing = kind.enumerate(source, library, cancel)
    discovered = list(listing)

    if not discovered and rows:
        # An empty walk over a library that holds items looks like an unmounted share.
        return refuse('root_missing', 'The folder looks empty while it still holds known files. Nothing was '
                                      'changed. If it was emptied on purpose, remove the library.')

    devices = _root_devices(roots)
    changed_roots = [path for path, (_, changed) in devices.items() if changed]

    def ignore_dev(found):
        return any(_under(found.path, root) for root in changed_roots)

    matching = identity.match_library(
        discovered, rows, ignore_dev=ignore_dev, library_content_kind=library.content_kind,
        foreign_lookup=lambda inodes, paths: repository.foreign_candidates(inodes, paths, library.id, session))

    if changed_roots and rows and not matching.identity_matched:
        # The root is on another device and holds none of the files this library knows. It may be the share
        # remounted empty-handed, or another filesystem altogether: either way nothing here can be vouched for.
        return refuse('root_missing', 'The folder is on a different device than at the last scan and holds none '
                                      'of the known files. Nothing was changed. If the library was replaced on '
                                      'purpose, remove it and add it again.')

    complete = listing.complete
    result.status = 'ok' if complete else 'partial'
    if listing.problems:
        result.message = '; '.join(listing.problems[:3])
    if cancel is not None:
        cancel()

    grouped = library.content_kind != 'movie'
    follows_directories = grouped and getattr(kind, "groups_are_directories", False)
    groups = {row.id: row for row in repository.library_groups(library.id, session)} if grouped else {}
    plan = None
    if follows_directories and complete:
        plan = _resolve_groups(groups, matching, devices, bool(changed_roots), session)
        group_for_dir = dict(plan.target)
    else:
        group_for_dir = {row.group_key: row.id for row in groups.values()}

    decisions = _decide(matching, rows, groups, plan, group_for_dir, grouped, follows_directories, complete)
    result.deferred = decisions.deferred
    group_for_dir.update(_create_groups(library, decisions, group_for_dir, groups, session))
    if cancel is not None:
        cancel()

    # 1. Rows closed or displaced go first, so that a path they held is free.
    for batch in _chunks(decisions.delete_ids):
        with source_transaction(session) as connection:
            repository.delete_items(batch, connection)
        result.deleted += len(batch)

    # 2. Rows that stayed: refresh what the walk saw, and join the group of the directory they are in.
    _write_unchanged(decisions, group_for_dir, groups, grouped, result, session, cancel)

    # 3. Rows that moved take a temporary path, then their final one: two files trading paths never collide.
    _write_repathed(library, decisions, group_for_dir, groups, grouped, result, session, cancel)

    # 4. Rows whose content changed at the same path, then the files nothing claimed.
    _write_path_only(library, decisions, groups, group_for_dir, result, session, cancel)
    inserted = _insert_new(library, decisions, group_for_dir, groups, result, session, cancel)

    if plan is not None and complete:
        # Empty groups: a group resolution found gone is deleted once it holds nothing.
        repository.delete_empty_groups(plan.gone, session)

    for path, (current, _) in devices.items():
        for root in roots:
            if root.local_path == path:
                repository.record_root_device(root.id, current, session)

    _identify_pending(library, session)
    _refresh_shadows(result, decisions, inserted, session)

    # The subtitle listing is rebuilt for every item on every scan, whether or not its video changed. One
    # ownership index serves the whole pass.
    ownership_index = SourceOutputOwnerIndex()
    for item_id in result.item_ids:
        if cancel is not None:
            cancel()
        index_item(item_id, session, ownership_index)

    result.message = _summary(result, matching, listing)
    repository.record_library_scan(library.id, result.status, result.message, session)
    return result


def _summary(result, matching, listing):
    count = result.unchanged + result.updated + result.inserted + result.moved + result.handed_over
    summary = f"{result.inserted} added, {result.updated} changed, {result.deleted} removed, {count} files"
    if result.moved or result.handed_over:
        summary += f"; {result.moved + result.handed_over} renamed or moved"
    held = Counter()
    for found, holder in matching.supplied:
        held[(holder.source_name, True)] += 1
    for found, holder in matching.held_elsewhere:
        held[(holder.source_name, False, holder.content_kind)] += 1
    for key, number in sorted(held.items(), key=str):
        if key[1]:
            summary += f"; {number} already supplied by {key[0]}"
        else:
            summary += f"; {number} held by {key[0]}, a {key[2]} library, which is disabled"
    result.held = sum(held.values())
    if result.skipped:
        summary += f"; {result.skipped} could not be added (see the log)"
    if result.deferred:
        summary += f"; {result.deferred} left for a scan that can read the whole folder"
    if listing.ungrouped:
        summary += f"; {listing.ungrouped} video file(s) directly in the library folder have no show and were not added"
    if listing.skipped_names:
        summary += f"; {listing.skipped_names} file(s) with a name that is not valid UTF-8 were skipped"
    return f"{summary}. {result.message}".strip() if result.message else summary


@dataclass
class _Decisions:
    unchanged: list = field(default_factory=list)       # (row, found)
    alias: list = field(default_factory=list)           # (row, found)
    repathed: list = field(default_factory=list)        # (row, found, handover: bool)
    path_only: list = field(default_factory=list)       # (row, found), kept
    new: list = field(default_factory=list)             # found
    delete_ids: list = field(default_factory=list)
    deferred: int = 0
    #: The walk was incomplete in a library whose groups follow directories: no group is created or changed.
    conservative: bool = False
    #: directory -> the old group a handover came from, whose settings an alias directory carries over
    carry_from: dict = field(default_factory=dict)


def _resolve_groups(groups, matching, devices, dev_changed, session):
    """Resolve every group of a folder library and apply the plan in one transaction, before any item write."""
    held_dirs = defaultdict(list)
    for row, found in matching.unchanged + matching.moved:
        if row.group_id is not None:
            held_dirs[row.group_id].append(found.group_key)
    new_content = Counter(found.group_key for found in matching.new)
    new_content.update(found.group_key for _, found in matching.path_only)
    plan = group_rules.plan_groups(list(groups.values()), held_dirs, new_content, group_rules.stat_directory,
                                   dev_changed=dev_changed)
    # Looked up before the transaction: a lookup can be slow and holds nothing.
    identities = {}
    for group_id in plan.reidentify:
        directory = plan.move.get(group_id) or groups[group_id].group_key
        identities[group_id] = identify.identify_group(directory)
    if not plan.writes_nothing:
        with source_transaction(session) as connection:
            group_rules.apply_plan(plan, connection, repository.update_group, identities)
    return plan


def _decide(matching, rows, groups, plan, group_for_dir, grouped, follows_directories, complete):
    """Turn the matching into writes. Nothing is deferred on a complete walk; on an incomplete one, anything that
    needs a group decision the walk cannot support, or a path a row that could not be deleted still holds, is
    left for a scan that can read the whole folder."""
    decisions = _Decisions()
    decisions.conservative = grouped and follows_directories and not complete
    # Rows that would be deleted because their path was reused cannot be, on an incomplete walk: whatever wants
    # their path waits.
    blocked = set() if complete else {row.path for row in matching.displaced}

    def usable(found):
        if found.path in blocked:
            return False
        return not (decisions.conservative and found.group_key not in group_for_dir)

    decisions.unchanged = list(matching.unchanged)
    decisions.alias = list(matching.alias_unchanged)
    for row, found in matching.moved:
        if usable(found):
            decisions.repathed.append((row, found, False))
        else:
            decisions.deferred += 1
    for holder, found in matching.handovers:
        if usable(found):
            decisions.repathed.append((holder, found, True))
            if holder.group_id is not None:
                decisions.carry_from.setdefault(found.group_key, holder.group_id)
        else:
            decisions.deferred += 1
    new = list(matching.new)
    for row, found in matching.path_only:
        if not grouped:
            decisions.path_only.append((row, found))
        elif plan is not None:
            if plan.target.get(found.group_key) == row.group_id:
                decisions.path_only.append((row, found))
            else:
                # The row's group left or is gone: the row is closed, and the file is new where it is.
                decisions.delete_ids.append(row.id)
                new.append(found)
        elif group_for_dir.get(found.group_key) == row.group_id:
            decisions.path_only.append((row, found))
        else:
            decisions.deferred += 1
    for found in new:
        if usable(found):
            decisions.new.append(found)
        else:
            decisions.deferred += 1
    if complete:
        decisions.delete_ids.extend(row.id for row in matching.displaced)
        displaced = {row.id for row in matching.displaced}
        decisions.delete_ids.extend(row.id for row in matching.unmatched
                                    if row.id not in displaced and identity.path_is_gone(row.path))
        decisions.delete_ids.extend(holder.id for holder in matching.closed_foreign)
    return decisions


def _create_groups(library, decisions, group_for_dir, groups, session):
    """A fresh group for every directory that holds a file to write and that no group holds (step 6), parsed and
    identified fresh from its own name. Returns ``{directory: group id}``."""
    needed = set()
    if library.content_kind == 'movie':
        return {}
    for row, found in ([] if decisions.conservative else decisions.unchanged):
        needed.add(found.group_key)
    for row, found, _ in decisions.repathed:
        needed.add(found.group_key)
    for row, found in decisions.path_only:
        needed.add(found.group_key)
    needed.update(found.group_key for found in decisions.new)
    needed = sorted(path for path in needed if path is not None and path not in group_for_dir)
    created = {}
    for batch in _chunks(needed):
        values = []
        for directory in batch:
            identified = identify.identify_group(directory)
            state = group_rules.stat_directory(directory)
            carried = _carried_settings(directory, decisions, state, groups, session)
            values.append((directory, {**identified, 'dir_dev': state.dev, 'dir_ino': state.ino, **carried}))
        with source_transaction(session) as connection:
            for directory, extra in values:
                extra = dict(extra)
                title = extra.pop('title')
                created[directory] = repository.create_group(
                    source_id=library.source_id, library_id=library.id, kind='show', group_key=directory,
                    title=title, target=connection, **extra)
    return created


def _carried_settings(directory, decisions, state, groups, session):
    """The group-level user settings a fresh group takes over, only when it is the same physical directory as
    the group the item came from (an alias of it): then it is the same show."""
    old_id = decisions.carry_from.get(directory)
    if old_id is None or state.status != 'dir':
        return {}
    old = repository.get_group(old_id, session)
    if old is None or old.dir_ino is None or (old.dir_dev, old.dir_ino) != (state.dev, state.ino):
        return {}
    carried = dict(profile_id=old.profile_id, monitored=old.monitored)
    if old.identity_source == 'user':
        carried.update(title=old.title, sort_title=old.sort_title, parsed_title=old.parsed_title, year=old.year,
                       tvdb_id=old.tvdb_id, tmdb_id=old.tmdb_id, imdb_id=old.imdb_id, poster=old.poster,
                       identity_source=old.identity_source, identity_state=old.identity_state,
                       identity_candidates=old.identity_candidates)
    return carried


def _group_state(group_id, groups, session):
    group = groups.get(group_id)
    if group is None and group_id is not None:
        group = repository.get_group(group_id, session)
        if group is not None:
            groups[group_id] = group
    return group.identity_state if group is not None else 'parsed'


def _identified(library, found, group_id, groups, session):
    state = _group_state(group_id, groups, session) if group_id is not None else 'parsed'
    return identify.identify_item(library.content_kind, found.path, found.group_key, state)


def _group_of(found, group_for_dir):
    return group_for_dir.get(found.group_key) if found.group_key is not None else None


def _write_unchanged(decisions, group_for_dir, groups, grouped, result, session, cancel):
    for batch in _chunks(decisions.unchanged):
        with source_transaction(session) as connection:
            for row, found in batch:
                values = dict(last_seen_at=datetime.now(), file_dev=found.dev, file_ino=found.ino)
                group_id = None if decisions.conservative else _group_of(found, group_for_dir)
                if grouped and group_id is not None and row.group_id != group_id:
                    values['group_id'] = group_id
                repository.update_item(row.id, connection, **values)
                result.item_ids.append(row.id)
                result.unchanged += 1
        if cancel is not None:
            cancel()
    for row, found in decisions.alias:
        # Another path to the same file: the row stays where it is.
        repository.update_item(row.id, session, last_seen_at=datetime.now())
        result.item_ids.append(row.id)
        result.unchanged += 1


def _write_repathed(library, decisions, group_for_dir, groups, grouped, result, session, cancel):
    repathed = decisions.repathed
    # Parsing and identifying are slow and hold nothing: they happen before the write lock is taken.
    prepared = []
    for row, found, handover in repathed:
        group_id = _group_of(found, group_for_dir) if grouped else None
        identified = _identified(library, found, group_id, groups, session)
        prepared.append((row, found, handover, group_id, identified))
    for batch in _chunks(prepared):
        with source_transaction(session) as connection:
            for row, *_ in batch:
                repository.update_item(row.id, connection, path=f'pending:{row.id}')
    for batch in _chunks(prepared):
        with source_transaction(session) as connection:
            for row, found, handover, group_id, identified in batch:
                values = _item_values(found, identified, keep_identity=row.identity_source == 'user')
                values['path'] = found.path
                if grouped:
                    values['group_id'] = group_id
                if handover:
                    values.update(source_id=library.source_id, library_id=library.id)
                repository.update_item(row.id, connection, **values)
                result.item_ids.append(row.id)
                if handover:
                    result.handed_over += 1
                else:
                    result.moved += 1
        if cancel is not None:
            cancel()


def _write_path_only(library, decisions, groups, group_for_dir, result, session, cancel):
    pending = [(row, found, _identified(library, found, row.group_id, groups, session))
               for row, found in decisions.path_only]
    for batch in _chunks(pending):
        with source_transaction(session) as connection:
            for row, found, identified in batch:
                values = _item_values(found, identified, keep_identity=row.identity_source == 'user')
                # The file changed: its codec cache described the old content.
                repository.update_item(row.id, connection, ffprobe_cache=None, **values)
                result.item_ids.append(row.id)
                result.updated += 1
        if cancel is not None:
            cancel()


def _insert_new(library, decisions, group_for_dir, groups, result, session, cancel):
    inserted = []
    item_kind = 'movie' if library.content_kind == 'movie' else 'episode'
    for batch in _chunks(decisions.new):
        _insert_batch(batch, library, item_kind, group_for_dir, groups, result, session, inserted)
        if cancel is not None:
            cancel()
    return inserted


def _insert_batch(batch, library, item_kind, group_for_dir, groups, result, session, inserted):
    """Insert a batch in one transaction. When the batch is refused as a whole (a path another row took in the
    meantime, a name the driver cannot bind), its files are tried one at a time: the ones that cannot be added
    are logged and skipped, and the rest are kept."""
    parses = []
    for found in batch:
        group_id = _group_of(found, group_for_dir) if item_kind != 'movie' else None
        parses.append((found, group_id, _identified(library, found, group_id, groups, session)))

    def insert(connection, found, group_id, identified):
        return repository.insert_item(connection, source_id=library.source_id, library_id=library.id,
                                      kind=item_kind, path=found.path, group_id=group_id,
                                      **_item_values(found, identified))
    try:
        with source_transaction(session) as connection:
            ids = [insert(connection, found, group_id, identified) for found, group_id, identified in parses]
    except (IntegrityError, DataError):
        for found, group_id, identified in parses:
            try:
                with source_transaction(session) as connection:
                    item_id = insert(connection, found, group_id, identified)
                result.item_ids.append(item_id)
                inserted.append((item_id, found))
                result.inserted += 1
            except (IntegrityError, DataError):
                logging.warning('BAZARR the source file could not be added and was skipped: %s', found.path)
                result.skipped += 1
        return
    result.item_ids.extend(ids)
    inserted.extend(zip(ids, batch))
    result.inserted += len(ids)


def _identify_pending(library, session):
    """Every item and group whose last lookup could not be answered is asked again, on every scan."""
    for row in repository.pending_items(library.id, session):
        if row.identity_source == 'user':
            repository.update_item(row.id, session, identify_pending=0)
            continue
        group = repository.get_group(row.group_id, session) if row.group_id is not None else None
        values = identify.identify_item(library.content_kind, row.path, group.group_key if group else None,
                                        group.identity_state if group else 'parsed')
        values.pop('scene_name', None)
        repository.update_item(row.id, session, **values)
    for group in repository.pending_groups(library.id, session):
        if group.identity_source != 'user':
            repository.update_group(group.id, session, **identify.identify_group(group.group_key))
        else:
            repository.update_group(group.id, session, identify_pending=0)


def _refresh_shadows(result, decisions, inserted, session):
    """Shadows are fully recomputed on every scan, both stages, for the files this scan saw."""
    try:
        owners = identity.ArrOwners.load(session)
        index = owners.index()
    except Exception:
        logging.exception('BAZARR could not read the arr rows to check source files against')
        return
    seen = {}
    for row, found in decisions.unchanged:
        seen[row.id] = found
    for row, found, _ in decisions.repathed:
        seen[row.id] = found
    for row, found in decisions.path_only:
        seen[row.id] = found
    for item_id, found in inserted:
        seen[item_id] = found
    if not seen:
        return
    current = {row.id: row for row in repository.shadow_state(list(seen), session)}
    if not index and not any(row.arr_shadow for row in current.values()):
        return
    for item_id, found in seen.items():
        row = current.get(item_id)
        if row is None:
            continue
        match = index.match(found.path, identity.FileStat(found.dev, found.ino, found.size, found.mtime_ns))
        changes = identity.shadow_changes(row, match)
        if changes is not None:
            repository.update_item(item_id, session, **changes)


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
