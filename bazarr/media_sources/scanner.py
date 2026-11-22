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
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.exc import DataError, IntegrityError, OperationalError

from app.jobs_queue import JobCancelled, JobFailed, jobs_queue

from . import groups as group_rules
from . import identify, identity, repository
from .db import source_transaction
from .indexer import SourceOutputOwnerIndex, index_item
from .kinds import UnknownSourceKind, get_kind

BATCH_SIZE = 500
# PostgreSQL refuses an index entry of more than about 2,700 bytes. A path above this is logged and skipped, on
# every dialect, so that a library behaves the same wherever it runs.
MAX_PATH_BYTES = 2600
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


def _root_snapshot(roots):
    """The identity of each readable root, or None when any root cannot be checked."""
    if not roots:
        return None
    states = {}
    for root in roots:
        state = group_rules.stat_directory(root.local_path)
        if state.status != 'dir' or not os.access(root.local_path, os.R_OK | os.X_OK):
            return None
        states[root.local_path] = state
    return states


def _under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _chunks(values, size=None):
    size = size or BATCH_SIZE
    for start in range(0, len(values), size):
        yield values[start:start + size]


_library_locks = {}
_library_locks_guard = threading.Lock()
_scan_lock = threading.Lock()


def library_lock(library_id):
    """The lock one scan of a library holds. Two scans of one library at once would race on the same group keys and
    paths, and the second would end in a constraint error."""
    with _library_locks_guard:
        return _library_locks.setdefault(library_id, threading.Lock())


class ShadowContext:
    """The arr rows a scan run checks source files against, read once per run however many libraries it scans."""

    def __init__(self, session=None):
        self.session = session
        self._index = None
        self._read = False

    def index(self):
        if not self._read:
            self._read = True
            try:
                self._index = identity.ArrOwners.load(self.session).index()
            except Exception:
                logging.exception('BAZARR could not read the arr rows to check source files against')
        return self._index


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


def scan_library(source, library, *, cancel=None, session=None, shadow_context=None) -> LibraryScan:
    """Scan one library and record the outcome on it. Never raises for a readable or unreadable root. A library
    already being scanned is not scanned again: the answer says so and nothing is changed or recorded."""
    lock = library_lock(library.id)
    if not lock.acquire(blocking=False):
        return LibraryScan(library_id=library.id, status='busy',
                           message='Another scan of this library is already running. Nothing was changed.')
    try:
        # A scan can take another library's item and put it in a different group. Serialize scans across
        # libraries so no scan can apply item or group decisions made before another scan's handover.
        while not _scan_lock.acquire(timeout=0.25):
            if cancel is not None:
                cancel()
        try:
            current_source = repository.get_source(source.id, session)
            current_library = repository.get_library(library.id, session)
            if current_source is None or current_library is None or current_library.source_id != source.id:
                return LibraryScan(library.id, status='unavailable', message='The library was removed before its scan.')
            if not current_source.enabled or not current_library.enabled:
                return LibraryScan(library.id, status='disabled', message='The library was disabled before its scan.')
            return _scan_library(current_source, current_library, cancel, session,
                                 shadow_context or ShadowContext(session))
        finally:
            _scan_lock.release()
    finally:
        lock.release()


def _roots_empty(roots):
    """True when no root holds anything at all, not even a folder. An unreadable root counts as empty: nothing
    can be vouched for."""
    for root in roots:
        try:
            with os.scandir(root.local_path) as entries:
                if any(True for _ in entries):
                    return False
        except OSError:
            continue
    return True


def _group_directory_vouches(group):
    """The group's folder is still there and is the very folder recorded at the last scan (the device ignored)."""
    state = group_rules.stat_directory(group.group_key)
    return state.status == 'dir' and group.dir_ino is not None and state.ino == group.dir_ino


class _ForeignGuard:
    """Read each foreign library once, and verify its mount again before relying on that walk."""

    def __init__(self, session, cancel):
        self.session = session
        self.cancel = cancel
        self.walks = {}

    def _walk(self, holder):
        library = repository.get_library(holder.library_id, self.session)
        source = repository.get_source(holder.source_id, self.session)
        if library is None or source is None:
            return None
        roots = repository.get_roots(holder.library_id, self.session)
        before = _root_snapshot(roots)
        if before is None or any(root.last_dev != before[root.local_path].dev for root in roots):
            return None
        try:
            kind = get_kind(source.kind)
        except UnknownSourceKind:
            return None
        if kind.validate_library(source, library):
            return None
        listing = kind.enumerate(source, library, self.cancel)
        found_video = False
        for _ in listing:
            found_video = True
        if found_video and listing.complete and not listing.unstable and _root_snapshot(roots) == before:
            return roots, before, listing.directory_states
        return None

    def vouches(self, holder, found):
        if holder.library_id not in self.walks:
            self.walks[holder.library_id] = self._walk(holder)
        walk = self.walks[holder.library_id]
        if walk is None:
            return False
        roots, snapshot, directories = walk
        if _root_snapshot(roots) != snapshot:
            self.walks[holder.library_id] = None
            return False
        # A mount can sit below a configured root. Recheck the ancestors of the path being released.
        directory = os.path.dirname(holder.path)
        while directory and directory != os.path.dirname(directory):
            if directory in directories and directory not in snapshot:
                if (group_rules.stat_directory(directory) != directories[directory]
                        or not os.access(directory, os.R_OK | os.X_OK)):
                    return False
            directory = os.path.dirname(directory)
        return identity.path_can_release(holder.path, found)

    def can_release(self, holder, found, alias):
        if alias:
            live = identity.stat_file(holder.path)
            return live is not None and (live.dev, live.ino) == (found.dev, found.ino)
        return self.vouches(holder, found)


@contextmanager
def _admission_transaction(session, guard, outer_releases=None):
    """Verify every release just before commit. Savepoints pass their releases to the outer transaction."""
    releases = []
    with source_transaction(session) as connection:
        yield connection, releases
        if outer_releases is None:
            for holder, found, alias in releases:
                if not guard.can_release(holder, found, alias):
                    raise _ForeignClaimChanged
    if outer_releases is not None:
        outer_releases.extend(releases)


def _scan_library(source, library, cancel, session, shadow_context) -> LibraryScan:
    result = LibraryScan(library_id=library.id)
    kind = get_kind(source.kind)
    roots = repository.get_roots(library.id, session)
    rows = repository.library_items(library.id, session)
    grouped = library.content_kind != 'movie'
    groups = {row.id: row for row in repository.library_groups(library.id, session)} if grouped else {}

    def refuse(status, message):
        result.status = status
        result.message = message
        repository.record_library_scan(library.id, status, message, session)
        return result

    root_snapshot = _root_snapshot(roots)
    if root_snapshot is None:
        return refuse('root_missing', 'The folder is missing or unreadable. Nothing was changed.')

    # The path was checked when the library was added; it is checked again, since a folder can become a link
    # into the configuration folder or a library can be changed in the database.
    problems = kind.validate_library(source, library)
    if problems:
        return refuse('error', ' '.join(problem.message for problem in problems) + ' Nothing was changed.')

    listing = kind.enumerate(source, library, cancel)
    discovered = list(listing)
    if listing.unstable or _root_snapshot(roots) != root_snapshot:
        return refuse('root_missing', 'A folder became unavailable or changed during the scan. Nothing was changed.')

    if not discovered and (rows or (groups and _roots_empty(roots))):
        # An empty walk over a library that holds items, or shows, looks like an unmounted share.
        return refuse('root_missing', 'The folder looks empty while it still holds known files. Nothing was '
                                      'changed. If it was emptied on purpose, remove the library.')

    changed_roots = {root.local_path for root in roots if root.last_dev is not None
                     and root.last_dev != root_snapshot[root.local_path].dev}

    def ignore_dev(found):
        return any(_under(found.path, root) for root in changed_roots)

    too_long = [found for found in discovered if len(found.path.encode('utf-8')) > MAX_PATH_BYTES]
    for found in too_long:
        logging.warning('BAZARR the source file path is too long to be stored and was skipped: %s', found.path)
    if too_long:
        discovered = [found for found in discovered if len(found.path.encode('utf-8')) <= MAX_PATH_BYTES]
        result.skipped += len(too_long)

    complete = listing.complete
    foreign_guard = _ForeignGuard(session, cancel)
    matching = identity.match_library(
        discovered, rows, ignore_dev=ignore_dev, library_content_kind=library.content_kind,
        foreign_lookup=lambda inodes, paths: repository.foreign_candidates(inodes, paths, library.id, session),
        foreign_vouched=lambda holder, found: complete and foreign_guard.vouches(holder, found))

    # A remounted root has to vouch for what this library knows: a file it holds by identity, or, for a library
    # that holds only folders, a folder that is the one recorded.
    vouched = matching.identity_matched or (not rows and any(_group_directory_vouches(group)
                                                             for group in groups.values()))
    if changed_roots and (rows or groups) and not vouched:
        # The root is on another device and holds none of the files this library knows. It may be the share
        # remounted empty-handed, or another filesystem altogether: either way nothing here can be vouched for.
        return refuse('root_missing', 'The folder is on a different device than at the last scan and holds none '
                                      'of the known files. Nothing was changed. If the library was replaced on '
                                      'purpose, remove it and add it again.')

    result.status = 'ok' if complete else 'partial'
    if listing.problems:
        result.message = '; '.join(listing.problems[:3])
    if cancel is not None:
        cancel()

    follows_directories = grouped and getattr(kind, "groups_are_directories", False)
    plan = None
    group_identities = {}
    if follows_directories and complete:
        plan, group_identities = _plan_groups(groups, matching, bool(changed_roots), roots)
        group_for_dir = dict(plan.target)
    elif follows_directories and groups:
        # An incomplete walk decides no group: a folder is trusted only when its group has a file matched at home
        # in this very walk, which is step one of the rules decided on what was seen.
        group_for_dir = {found.group_key: row.group_id for row, found in matching.unchanged + matching.moved
                         if row.group_id in groups and groups[row.group_id].group_key == found.group_key}
    else:
        group_for_dir = {row.group_key: row.id for row in groups.values()}

    decisions = _decide(matching, groups, plan, group_for_dir, grouped, follows_directories, complete, roots,
                        foreign_guard)
    if _root_snapshot(roots) != root_snapshot:
        return refuse('root_missing', 'A folder became unavailable or changed during the scan. Nothing was changed.')

    result.deferred = decisions.deferred
    write_args = (library, decisions, plan, group_identities, group_for_dir, result, cancel)
    if decisions.required_paths:
        # These incoming files release rows whose groups give up their keys. A rejected write must also
        # roll back the group keys, so these scans keep the key changes and item writes together.
        try:
            with _admission_transaction(session, decisions.foreign_guard) as (connection, releases):
                inserted = _write_library(*write_args, connection, releases)
        except (_GroupedAdmissionRefused, _ForeignClaimChanged):
            result = LibraryScan(library.id)
            return refuse('partial', 'An incoming file could not be written. No item or group changes were saved.')
    else:
        inserted = _write_library(*write_args, session)

    if plan is not None:
        for line in plan.log:
            logging.info(line)

    for root in roots:
        if root.local_path in changed_roots and not complete:
            # Some rows still carry the old device number: the next scan must go on ignoring it.
            continue
        repository.record_root_device(root.id, root_snapshot[root.local_path].dev, session)

    _identify_pending(source, kind, library, decisions, cancel, session)
    _refresh_shadows(library.id, decisions, inserted, set(result.item_ids), session, shadow_context)

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


def _write_library(library, decisions, plan, group_identities, group_for_dir, result, cancel, session,
                   outer_releases=None):
    if plan is not None and not plan.writes_nothing:
        with source_transaction(session) as connection:
            def update_group(group_id, connection, **values):
                identity_fields = group_identities.get(group_id, {}).keys() & values.keys()
                write = repository.update_show_identity if identity_fields else repository.update_group
                write(group_id, connection, owner_library_id=library.id, protect_user=identity_fields, **values)

            group_rules.apply_plan(plan, connection, update_group, group_identities)

    grouped = library.content_kind != 'movie'
    group_for_dir.update(_create_groups(library, decisions, group_for_dir, session))
    if cancel is not None:
        cancel()

    for batch in _chunks(decisions.delete_ids):
        with source_transaction(session) as connection:
            result.deleted += repository.delete_items(batch, connection, owner_library_id=library.id)

    _write_unchanged(decisions, group_for_dir, grouped, result, session, cancel)
    _write_repathed(library, decisions, group_for_dir, grouped, result, session, cancel, outer_releases)
    _write_path_only(library, decisions, result, session, cancel, outer_releases)
    inserted = _insert_new(library, decisions, group_for_dir, result, session, cancel, outer_releases)

    if plan is not None:
        repository.delete_empty_groups(plan.gone, session, owner_library_id=library.id)
    return inserted


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
    for found, holder in matching.deferred_foreign:
        held[(holder.source_name, None, holder.content_kind)] += 1
    for key, number in sorted(held.items(), key=str):
        if key[1]:
            summary += f"; {number} already supplied by {key[0]}"
        elif key[1] is None:
            summary += (f"; {number} held by {key[0]}, a {key[2]} library that cannot be checked right now "
                        f"(its own scan decides)")
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
    foreign_guard: _ForeignGuard
    unchanged: list = field(default_factory=list)       # (row, found)
    alias: list = field(default_factory=list)           # (row, found, the folder of the row's own path)
    repathed: list = field(default_factory=list)        # (row, found, handover: bool)
    path_only: list = field(default_factory=list)       # (row, found), kept
    new: list = field(default_factory=list)             # found
    delete_ids: list = field(default_factory=list)
    admission_displaced: dict = field(default_factory=dict)  # destination path -> the displaced local row
    foreign_to_close: dict = field(default_factory=dict)  # destination path -> the foreign row
    foreign_aliases: set = field(default_factory=set)
    required_paths: set = field(default_factory=set)      # incoming paths whose groups give up their keys
    deferred: int = 0
    #: The walk was incomplete in a library whose groups follow directories: no group is created or changed.
    conservative: bool = False
    #: directory -> the old group a handover came from, whose settings an alias directory carries over
    carry_from: dict = field(default_factory=dict)


def _own_directory(path, roots):
    """The first-level folder (the show) a path lies in, under whichever root holds it, or None."""
    for root in roots:
        base = root.local_path
        if path.startswith(base.rstrip(os.sep) + os.sep):
            first = path[len(base.rstrip(os.sep)) + 1:].split(os.sep)[0]
            if os.sep in path[len(base.rstrip(os.sep)) + 1:]:
                return os.path.join(base, first)
    return None


def _plan_groups(groups, matching, dev_changed, roots):
    """Resolve every group of a folder library before making any changes."""
    held_dirs = defaultdict(list)
    for row, found in matching.unchanged + matching.moved:
        if row.group_id is not None:
            held_dirs[row.group_id].append(found.group_key)
    for row, found in matching.alias_unchanged:
        # The file is found at another path while the row's own path still holds it: the row is where it was.
        own = _own_directory(row.path, roots)
        if row.group_id is not None and own is not None:
            held_dirs[row.group_id].append(own)
    new_content = Counter(found.group_key for found in matching.new)
    new_content.update(found.group_key for _, found in matching.path_only)
    plan = group_rules.plan_groups(list(groups.values()), held_dirs, new_content, group_rules.stat_directory,
                                   dev_changed=dev_changed, protected={row.group_id for row in matching.protected})
    # Looked up before the transaction: a lookup can be slow and holds nothing.
    identities = {}
    for group_id in plan.reidentify:
        directory = plan.move.get(group_id) or groups[group_id].group_key
        identities[group_id] = identify.identify_group(directory)
    return plan, identities


def _decide(matching, groups, plan, group_for_dir, grouped, follows_directories, complete, roots, foreign_guard):
    """Turn the matching into writes. Nothing is deferred on a complete walk. On an incomplete one nothing that
    needs a group decision is done, and nothing is written to a path that a row which has to wait still holds:
    only a folder whose group has a file matched at home may take files, and only rows whose identity matched in
    this walk are touched."""
    decisions = _Decisions(foreign_guard)
    decisions.foreign_to_close.update((found.path, holder) for holder, found in matching.closed_foreign)
    decisions.foreign_aliases.update(found.path for _, found, alias in matching.handovers if alias)
    # With no group yet (the first scan) nothing can be moving anywhere, so a fresh group is always safe.
    decisions.conservative = grouped and follows_directories and not complete and bool(groups)
    # Rows that would be deleted because their path was reused cannot be, on an incomplete walk: whatever wants
    # their path waits, and so does whatever wants the path of a row that waits.
    blocked = set() if complete else {row.path for row in matching.displaced}

    def trusted(found):
        return not (decisions.conservative and found.group_key not in group_for_dir)

    moves = [(row, found, False) for row, found in matching.moved]
    moves += [(holder, found, True) for holder, found, _ in matching.handovers]
    waiting = set()
    changed = True
    while changed:
        changed = False
        for index, (row, found, handover) in enumerate(moves):
            if index in waiting:
                continue
            if found.path in blocked or not trusted(found):
                waiting.add(index)
                if not handover:
                    blocked.add(row.path)
                changed = True
    for index, (row, found, handover) in enumerate(moves):
        if index in waiting:
            decisions.deferred += 1
            continue
        decisions.repathed.append((row, found, handover))
        if handover and row.group_id is not None:
            decisions.carry_from.setdefault(found.group_key, row.group_id)

    decisions.unchanged = list(matching.unchanged)
    decisions.alias = [(row, found, _own_directory(row.path, roots) if grouped else None)
                       for row, found in matching.alias_unchanged]
    new = list(matching.new)
    for row, found in matching.path_only:
        if not grouped:
            decisions.path_only.append((row, found))
        elif plan is not None:
            if plan.target.get(found.group_key) == row.group_id:
                decisions.path_only.append((row, found))
            else:
                # The row's group left or is gone: the row is closed, and the file is new where it is.
                if found.path in decisions.foreign_to_close:
                    decisions.admission_displaced[found.path] = row
                else:
                    decisions.delete_ids.append(row.id)
                new.append(found)
        elif group_for_dir.get(found.group_key) == row.group_id:
            decisions.path_only.append((row, found))
        else:
            decisions.deferred += 1
    for found in new:
        if found.path not in blocked and trusted(found):
            decisions.new.append(found)
        else:
            decisions.deferred += 1
    if complete:
        incoming = {found.path for _, found, handover in decisions.repathed if handover}
        for row in matching.displaced:
            if row.path in incoming:
                decisions.admission_displaced[row.path] = row
            else:
                decisions.delete_ids.append(row.id)
        for row in matching.unmatched:
            if identity.path_is_gone(row.path):
                decisions.delete_ids.append(row.id)
    if plan is not None:
        rekeyed = plan.gone | plan.move.keys()
        decisions.required_paths.update(path for path, row in decisions.admission_displaced.items()
                                        if row.group_id in rekeyed)
    return decisions


def _create_groups(library, decisions, group_for_dir, session):
    """A fresh group for every directory that holds a file to write and that no group holds (step 6), parsed and
    identified fresh from its own name. Returns ``{directory: group id}``."""
    needed = set()
    if library.content_kind == 'movie':
        return {}
    for row, found in ([] if decisions.conservative else decisions.unchanged):
        needed.add(found.group_key)
    if not decisions.conservative:
        needed.update(own for _, _, own in decisions.alias if own is not None)
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
            carried = _carried_settings(directory, decisions, state, session)
            values.append((directory, {**identified, 'dir_dev': state.dev, 'dir_ino': state.ino, **carried}))
        with source_transaction(session) as connection:
            for directory, extra in values:
                extra = dict(extra)
                title = extra.pop('title')
                created[directory] = repository.create_group(
                    source_id=library.source_id, library_id=library.id, kind='show', group_key=directory,
                    title=title, target=connection, **extra)
    return created


def _carried_settings(directory, decisions, state, session):
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


_EPISODE_STATE_FIELDS = frozenset(('group_id', 'identity_state', 'identity_source', 'identify_pending',
                                   'season', 'episode'))


def _update_scanned_item(row, connection, *, reparse=False, **values):
    episode_identity = row.kind == 'episode' and not _EPISODE_STATE_FIELDS.isdisjoint(values)
    previous_group_id = row.group_id if episode_identity else None

    def save():
        if reparse:
            current = repository.get_item(row.id, connection, for_update=True)
            if (current is None or current.group_id != row.group_id or current.path != row.path
                    or current.scene_name is not None):
                return None
        if repository.update_item(row.id, connection, owner_library_id=row.library_id,
                                  previous_group_id=previous_group_id, **values):
            return row.id
        return None

    if episode_identity:
        group_id = values.get('group_id', row.group_id)
        return repository.write_episode_identity(connection, group_id, save, previous_group_id=previous_group_id,
                                                 group_key=row.group_key if reparse else None)
    return save()


def _identified(library, found):
    return identify.identify_item(library.content_kind, found.path, found.group_key, 'parsed', found.hints)


def _group_of(found, group_for_dir):
    return group_for_dir.get(found.group_key) if found.group_key is not None else None


def _write_unchanged(decisions, group_for_dir, grouped, result, session, cancel):
    for batch in _chunks(decisions.unchanged):
        with source_transaction(session) as connection:
            for row, found in batch:
                values = dict(last_seen_at=datetime.now(), file_dev=found.dev, file_ino=found.ino)
                group_id = None if decisions.conservative else _group_of(found, group_for_dir)
                if grouped and group_id is not None and row.group_id != group_id:
                    values['group_id'] = group_id
                if _update_scanned_item(row, connection, **values):
                    result.item_ids.append(row.id)
                    result.unchanged += 1
        if cancel is not None:
            cancel()
    for row, found, own_directory in decisions.alias:
        # Another path to the same file: the row stays where it is, in the group of the folder it is in.
        values = dict(last_seen_at=datetime.now())
        group_id = group_for_dir.get(own_directory) if own_directory is not None else None
        if grouped and not decisions.conservative and group_id is not None and row.group_id != group_id:
            values['group_id'] = group_id
        if 'group_id' in values:
            with source_transaction(session) as connection:
                updated = _update_scanned_item(row, connection, **values)
        else:
            updated = _update_scanned_item(row, session, **values)
        if updated:
            result.item_ids.append(row.id)
            result.unchanged += 1


def _write_repathed(library, decisions, group_for_dir, grouped, result, session, cancel, outer_releases):
    repathed = decisions.repathed
    # Parsing and identifying are slow and hold nothing: they happen before the write lock is taken.
    prepared = []
    for row, found, handover in repathed:
        group_id = _group_of(found, group_for_dir) if grouped else None
        identified = _identified(library, found)
        prepared.append((row, found, handover, group_id, identified))
    for batch in _chunks([entry for entry in prepared if not entry[2]]):
        with source_transaction(session) as connection:
            for row, *_ in batch:
                repository.update_item(row.id, connection, owner_library_id=row.library_id, path=f'pending:{row.id}')
    for batch in _chunks(prepared):
        try:
            with _admission_transaction(session, decisions.foreign_guard, outer_releases) as (connection, releases):
                moved = [(entry, _write_final_path(library, entry, grouped, decisions, connection, releases))
                         for entry in batch]
        except (IntegrityError, _ForeignClaimChanged):
            # Retry each move on its own. An unsuccessful local move is recovered from its temporary path
            # on the next scan. A handover keeps the old owner and path until it succeeds.
            for entry in batch:
                try:
                    with _admission_transaction(session, decisions.foreign_guard, outer_releases) as (
                            connection, releases):
                        deleted = _write_final_path(library, entry, grouped, decisions, connection, releases)
                except (IntegrityError, _ForeignClaimChanged) as error:
                    if entry[1].path in decisions.required_paths:
                        raise _GroupedAdmissionRefused from error
                    logging.warning('BAZARR the source file could not be moved to its new path and waits for the '
                                    'next scan: %s', entry[1].path)
                    result.deferred += 1
                    continue
                if deleted is not None:
                    _count_repathed(entry, result, deleted)
            if cancel is not None:
                cancel()
            continue
        for entry, deleted in moved:
            if deleted is not None:
                _count_repathed(entry, result, deleted)
        if cancel is not None:
            cancel()


class _ForeignClaimChanged(Exception):
    """The prior owner's mount, path or row changed before a transfer could commit."""


class _GroupedAdmissionRefused(Exception):
    """A replacement that must succeed with a group key change could not be written."""


def _delete_owned(row, connection):
    if row is None:
        return 0
    if row.kind == 'episode':
        # A concurrent show correction locks the show before its episodes.
        repository.lock_show_groups(connection, (row.group_id,))
    return repository.delete_items([row.id], connection, owner_library_id=row.library_id)


def _write_final_path(library, entry, grouped, decisions, connection, releases):
    row, found, handover, group_id, identified = entry
    values = _item_values(found, identified, keep_identity=row.identity_source == 'user')
    values['path'] = found.path
    if grouped:
        values['group_id'] = group_id
    displaced = 0
    if handover:
        # Release the occupied path only in the transaction that makes the incoming item its new owner.
        displaced = _delete_owned(decisions.admission_displaced.get(found.path), connection)
        values.update(source_id=library.source_id, library_id=library.id)
    protected = (identified.keys() & values.keys()) - {'scene_name'}
    updated = _update_scanned_item(row, connection, protect_user=protected, **values)
    if handover:
        if not updated:
            raise _ForeignClaimChanged
        releases.append((row, found, found.path in decisions.foreign_aliases))
    return displaced if updated else None


def _count_repathed(entry, result, deleted):
    row, found, handover, *_ = entry
    result.item_ids.append(row.id)
    result.deleted += deleted
    if handover:
        result.handed_over += 1
    else:
        result.moved += 1


def _close_foreign(holder, found, connection, releases):
    if holder is None:
        return 0
    deleted = _delete_owned(holder, connection)
    if not deleted:
        raise _ForeignClaimChanged
    releases.append((holder, found, False))
    return deleted


def _write_path_only(library, decisions, result, session, cancel, outer_releases):
    pending = [(row, found, _identified(library, found))
               for row, found in decisions.path_only]

    def update(connection, row, found, identified):
        values = _item_values(found, identified, keep_identity=row.identity_source == 'user')
        protected = (identified.keys() & values.keys()) - {'scene_name'}
        # The cached streams and subtitle index describe the previous file.
        values.update(ffprobe_cache=None, audio_language=None, audio_codec=None, video_codec=None, resolution=None,
                      subtitles=None, missing_subtitles=None, indexed_at=None)
        return _update_scanned_item(row, connection, protect_user=protected, **values)

    local = [entry for entry in pending if entry[1].path not in decisions.foreign_to_close]
    for batch in _chunks(local):
        with source_transaction(session) as connection:
            for row, found, identified in batch:
                if update(connection, row, found, identified):
                    result.item_ids.append(row.id)
                    result.updated += 1
        if cancel is not None:
            cancel()

    for row, found, identified in pending:
        holder = decisions.foreign_to_close.get(found.path)
        if holder is None:
            continue
        try:
            with _admission_transaction(session, decisions.foreign_guard, outer_releases) as (connection, releases):
                updated = update(connection, row, found, identified)
                deleted = _close_foreign(holder, found, connection, releases) if updated else 0
        except _ForeignClaimChanged:
            result.deferred += 1
        else:
            if updated:
                result.item_ids.append(row.id)
                result.updated += 1
                result.deleted += deleted
        if cancel is not None:
            cancel()


def _insert_new(library, decisions, group_for_dir, result, session, cancel, outer_releases):
    inserted = []
    item_kind = 'movie' if library.content_kind == 'movie' else 'episode'
    for batch in _chunks(decisions.new):
        _insert_batch(batch, library, item_kind, group_for_dir, result, session, inserted, decisions,
                      outer_releases)
        if cancel is not None:
            cancel()
    return inserted


def _is_refused_row(error):
    """A row the database refuses on its own account: a constraint, a value it cannot bind, or (PostgreSQL) an
    index entry beyond its size limit, which the driver raises as an operational error."""
    if isinstance(error, (IntegrityError, DataError)):
        return True
    return isinstance(error, OperationalError) and getattr(error.orig, 'pgcode', None) == '54000'


def _insert_batch(batch, library, item_kind, group_for_dir, result, session, inserted, decisions,
                  outer_releases):
    """Insert a batch in one transaction. When the batch is refused as a whole (a path another row took in the
    meantime, a name the driver cannot bind), its files are tried one at a time: the ones that cannot be added
    are logged and skipped, and the rest are kept."""
    parses = []
    for found in batch:
        group_id = _group_of(found, group_for_dir) if item_kind != 'movie' else None
        parses.append((found, group_id, _identified(library, found)))

    def insert(connection, releases, found, group_id, identified):
        holder = decisions.foreign_to_close.get(found.path)
        deleted = _delete_owned(decisions.admission_displaced.get(found.path), connection)
        def save():
            return repository.insert_item(connection, source_id=library.source_id, library_id=library.id,
                                          kind=item_kind, path=found.path, group_id=group_id,
                                          **_item_values(found, identified))

        if item_kind == 'episode':
            item_id = repository.write_episode_identity(connection, group_id, save)
            if item_id is None:
                raise _ForeignClaimChanged
        else:
            item_id = save()
        return item_id, deleted + _close_foreign(holder, found, connection, releases)
    try:
        with _admission_transaction(session, decisions.foreign_guard, outer_releases) as (connection, releases):
            written = [insert(connection, releases, found, group_id, identified)
                       for found, group_id, identified in parses]
    except (IntegrityError, DataError, OperationalError, _ForeignClaimChanged) as error:
        if not isinstance(error, _ForeignClaimChanged) and not _is_refused_row(error):
            raise
        for found, group_id, identified in parses:
            try:
                with _admission_transaction(session, decisions.foreign_guard, outer_releases) as (
                        connection, releases):
                    item_id, deleted = insert(connection, releases, found, group_id, identified)
                result.item_ids.append(item_id)
                inserted.append((item_id, found))
                result.inserted += 1
                result.deleted += deleted
            except (IntegrityError, DataError, OperationalError, _ForeignClaimChanged) as refusal:
                foreign_changed = isinstance(refusal, _ForeignClaimChanged)
                if not foreign_changed and not _is_refused_row(refusal):
                    raise
                if found.path in decisions.required_paths:
                    raise _GroupedAdmissionRefused from refusal
                if foreign_changed:
                    result.deferred += 1
                    continue
                logging.warning('BAZARR the source file could not be added and was skipped: %s', found.path)
                result.skipped += 1
        return
    ids, deleted = zip(*written)
    result.item_ids.extend(ids)
    inserted.extend(zip(ids, batch))
    result.inserted += len(ids)
    result.deleted += sum(deleted)


def _reparse_episode(row, session):
    values = identify.identify_item('series', row.path, row.group_key)
    values.pop('external_id', None)
    with source_transaction(session) as connection:
        _update_scanned_item(row, connection, reparse=True, only_automatic=True, **values)


def _identify_pending(source, kind, library, decisions, cancel, session):
    """Apply new hints and confirm pending filename identities after the scanner commits its writes."""
    # Hints can arrive or change while the file's size, name and mtime stay the same.
    for row, found in decisions.unchanged:
        if found.hints is None or row.identity_source == 'user':
            continue
        if cancel is not None:
            cancel()
        values = _identified(library, found)
        if values['identity_source'] != 'media_server':
            values = {'external_id': found.hints.external_id} if found.hints.external_id else {}
        if row.kind == 'episode' and not _EPISODE_STATE_FIELDS.isdisjoint(values):
            current = repository.get_item(row.id, session)
            if current is not None and current.library_id == library.id and current.path == found.path:
                with source_transaction(session) as connection:
                    _update_scanned_item(current, connection, only_automatic=True, **values)
        elif values:
            _update_scanned_item(row, session, only_automatic=True, **values)

    if library.content_kind == 'movie':
        for row in repository.pending_movies(library.id, session):
            if cancel is not None:
                cancel()
            if row.identity_source == 'user':
                repository.update_item(row.id, session, owner_library_id=library.id, identify_pending=0)
                continue
            values = {}
            if (row.scene_name is None and row.identity_source == 'filename' and row.identity_state == 'unmatched'
                    and (row.parsed_title is None or row.year is None)):
                parsed = identify.identify_item('movie', row.path)
                values = {field: parsed[field] for field in
                          ('title', 'sort_title', 'parsed_title', 'year', 'scene_name', 'identity_state')}
            values.update(identify.confirm_title(values.get('parsed_title', row.parsed_title),
                                                 values.get('year', row.year), 'movie'))
            if row.parsed_title and row.title != row.parsed_title and 'title' not in values:
                values.update(title=row.parsed_title, sort_title=row.parsed_title.lower())
            if values != {'identify_pending': 1}:
                repository.update_item(row.id, session, owner_library_id=library.id, only_automatic=True, **values)
        return
    if library.content_kind != 'series':
        return

    for row in repository.pending_episode_parses(library.id, session):
        if cancel is not None:
            cancel()
        _reparse_episode(row, session)

    for group in repository.library_groups(library.id, session):
        if cancel is not None:
            cancel()
        if group.identity_source == 'user':
            continue
        lookup = getattr(kind, 'series_identity', None)
        hints = lookup(source, group.group_key) if lookup else None
        if identify.has_provider_id(hints):
            values = identify.group_hints(group, hints)
        elif group.identify_pending or (group.identity_source == 'filename' and not group.parsed_title):
            directory_key = getattr(kind, 'groups_are_directories', True)
            if group.parsed_title and directory_key:
                values = identify.confirm_title(group.parsed_title, group.year, 'show')
            else:
                paths = repository.group_episode_paths(group.id, session)
                values = identify.identify_group(group.group_key, paths, directory_key=directory_key)
                values.pop('external_id', None)
                values.update(identify.confirm_title(values['parsed_title'], values['year'], 'show'))
        else:
            values = {}
        if hints and hints.external_id:
            values['external_id'] = hints.external_id
        changes = {key: value for key, value in values.items() if getattr(group, key) != value}
        if changes:
            with source_transaction(session) as connection:
                repository.update_show_identity(group.id, connection, owner_library_id=library.id,
                                                only_automatic=True, **changes)


def _refresh_shadows(library_id, decisions, inserted, written_ids, session, context):
    """Shadows are fully recomputed on every scan, both stages, for the files this scan saw."""
    # ``{item id: (the path to match, its stat)}``. A row reached through an alias is matched by its own path.
    seen = {row.id: (found.path, identity.FileStat(found.dev, found.ino, found.size, found.mtime_ns))
            for row, found in decisions.unchanged}
    seen.update({row.id: (found.path, identity.FileStat(found.dev, found.ino, found.size, found.mtime_ns))
                 for row, found, _ in decisions.repathed})
    seen.update({row.id: (found.path, identity.FileStat(found.dev, found.ino, found.size, found.mtime_ns))
                 for row, found in decisions.path_only})
    seen.update({row.id: (row.path, identity.FileStat(found.dev, found.ino, found.size, found.mtime_ns))
                 for row, found, _ in decisions.alias})
    seen.update({item_id: (found.path, identity.FileStat(found.dev, found.ino, found.size, found.mtime_ns))
                 for item_id, found in inserted})
    seen = {item_id: file for item_id, file in seen.items() if item_id in written_ids}
    if not seen:
        return
    index = context.index()
    if index is None:
        return
    current = {row.id: row for row in repository.shadow_state(list(seen), session)
               if row.library_id == library_id and row.path == seen[row.id][0]}
    if not index and not any(row.arr_shadow for row in current.values()):
        return
    for item_id, (path, stat) in seen.items():
        row = current.get(item_id)
        if row is None:
            continue
        changes = identity.shadow_changes(row, index.match(path, stat))
        if changes is not None:
            repository.update_item(item_id, session, owner_library_id=row.library_id, **changes)


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
    # A scan of one library says nothing about the others: only a scan of the whole source stamps the source, and
    # so only that one moves its next scheduled scan.
    whole = library_id is None
    if whole:
        repository.record_source_scan(source_id, started=datetime.now(), target=session)
    results = []
    status = 'ok'
    shadow_context = ShadowContext(session)
    try:
        for library in libraries:
            outcome = scan_library(source, library, cancel=cancel, session=session, shadow_context=shadow_context)
            results.append(outcome)
            if outcome.status != 'ok':
                status = 'partial'
    except BaseException as error:
        # A stop is not a failure: what was read before it was kept.
        if whole:
            repository.record_source_scan(source_id, finished=datetime.now(), target=session,
                                          status='partial' if isinstance(error, JobCancelled) else 'error')
        raise
    if whole:
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
