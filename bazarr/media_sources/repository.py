# coding=utf-8
"""Reads and writes of the source tables.

Every function takes a ``target``: a session, or the connection of a ``source_transaction``. With none it uses
the application's session. Nothing here touches an arr or Sportarr table.
"""
import logging
import os
from datetime import datetime

import sqlalchemy as sa

from app.database import (TableMediaSourceLibraries, TableMediaSourceLibraryRoots, TableMediaSources,
                          TableSourceBlacklist, TableSourceGroups, TableSourceHistory, TableSourceItems)

from .db import handle
from .identity import SHADOW_COLUMNS

ITEM_KINDS = ('movie', 'episode', 'sports_event')
# The kind of group each kind of item belongs to. A movie has none.
GROUP_KIND_OF_ITEM = {'episode': 'show', 'sports_event': 'league'}
CONTENT_KINDS = ('movie', 'series', 'sports')
SOURCE_KINDS = ('folder', 'jellyfin', 'emby')
IDENTITY_SOURCES = ('filename', 'tmdb', 'media_server', 'user')
IDENTITY_STATES = ('identified', 'parsed', 'ambiguous', 'unmatched')

def _table(model):
    # Importing the API package imports this module, and several test files import it while ``app.database`` is
    # replaced by a stub whose classes have no ``__table__``. Reading it must not be what fails the import; with
    # the real models it is always there, and a query is the first thing that needs it.
    return getattr(model, '__table__', None)


Sources = _table(TableMediaSources)
Libraries = _table(TableMediaSourceLibraries)
Roots = _table(TableMediaSourceLibraryRoots)
Groups = _table(TableSourceGroups)
Items = _table(TableSourceItems)
History = _table(TableSourceHistory)
Blacklist = _table(TableSourceBlacklist)


def _target(target):
    return handle() if target is None else target


def _insert(target, table, values):
    return _target(target).execute(sa.insert(table).values(**values)).inserted_primary_key[0]


# Sources, libraries and roots

def get_source(source_id, target=None, *, lock_key=False):
    statement = sa.select(Sources).where(Sources.c.id == source_id)
    if lock_key:
        statement = statement.with_for_update(read=True, key_share=True)
    return _target(target).execute(statement).first()


def list_sources(target=None):
    return _target(target).execute(sa.select(Sources).order_by(Sources.c.id)).all()


def has_sources(target=None):
    return Sources is not None and bool(_target(target).execute(sa.select(sa.exists().select_from(Sources))).scalar())


def name_taken(name, target=None):
    return _target(target).execute(sa.select(Sources.c.id).where(Sources.c.name == name)).first() is not None


def create_source(*, kind, name, enabled=True, scan_frequency_hours=12, media_server_instance_id=None,
                  target=None):
    now = datetime.now()
    return _insert(target, Sources, dict(
        kind=kind, name=name, enabled=1 if enabled else 0, scan_frequency_hours=scan_frequency_hours,
        media_server_instance_id=media_server_instance_id, created_at=now, updated_at=now))


def create_library(*, source_id, content_kind, locator, name=None, profile_id=None, enabled=True,
                   local_roots=None, target=None):
    """Add a library and its roots. A folder library has exactly one, equal to its locator."""
    now = datetime.now()
    library_id = _insert(target, Libraries, dict(
        source_id=source_id, content_kind=content_kind, locator=locator, name=name, profile_id=profile_id,
        enabled=1 if enabled else 0, created_at=now, updated_at=now))
    for local_path in local_roots or [locator]:
        _insert(target, Roots, dict(library_id=library_id, local_path=local_path))
    return library_id


def get_library(library_id, target=None):
    return _target(target).execute(sa.select(Libraries).where(Libraries.c.id == library_id)).first()


def list_libraries(source_id, target=None):
    return _target(target).execute(
        sa.select(Libraries).where(Libraries.c.source_id == source_id).order_by(Libraries.c.id)).all()


def get_roots(library_id, target=None):
    return _target(target).execute(
        sa.select(Roots).where(Roots.c.library_id == library_id).order_by(Roots.c.id)).all()


def configured_library_root_paths(target=None):
    """All configured local paths, including disabled libraries."""
    return _target(target).execute(sa.select(Roots.c.local_path).distinct()).scalars().all()


def enabled_item_roots(item_id, source_id, library_id, path, target=None):
    """Current local roots for a request whose item, path and owners still match."""
    return _target(target).execute(
        sa.select(Roots.c.local_path)
        .select_from(Items.join(Libraries, sa.and_(Items.c.library_id == Libraries.c.id,
                                                 Items.c.source_id == Libraries.c.source_id))
                     .join(Sources, Libraries.c.source_id == Sources.c.id)
                     .join(Roots, Libraries.c.id == Roots.c.library_id))
        .where(Items.c.id == item_id, Items.c.source_id == source_id, Items.c.library_id == library_id,
               Items.c.path == path, Libraries.c.enabled == 1, Sources.c.enabled == 1)
    ).scalars().all()


def record_library_scan(library_id, status, message=None, target=None):
    _target(target).execute(sa.update(Libraries).where(Libraries.c.id == library_id).values(
        last_scan_status=status, last_scan_message=message, updated_at=datetime.now()))


def record_source_scan(source_id, *, started=None, finished=None, status=None, target=None):
    values = {}
    if started is not None:
        values['last_scan_started_at'] = started
    if finished is not None:
        values['last_scan_finished_at'] = finished
    if status is not None:
        values['last_scan_status'] = status
    if values:
        _target(target).execute(sa.update(Sources).where(Sources.c.id == source_id).values(**values))


def record_root_device(root_id, device, target=None):
    _target(target).execute(sa.update(Roots).where(Roots.c.id == root_id).values(last_dev=device))


def update_source(source_id, target=None, **values):
    values['updated_at'] = datetime.now()
    return _target(target).execute(sa.update(Sources).where(Sources.c.id == source_id).values(**values)).rowcount


def update_library(library_id, target=None, **values):
    values['updated_at'] = datetime.now()
    return _target(target).execute(sa.update(Libraries).where(Libraries.c.id == library_id).values(**values)).rowcount


def delete_library(library_id, target=None):
    """Delete a library. Its roots, groups and items go (cascade); their history and blacklist rows stay, with
    their links set to NULL."""
    _target(target).execute(sa.delete(Libraries).where(Libraries.c.id == library_id))


def replace_roots(library_id, roots, target=None):
    """Replace a library's roots with ``[(server_path, local_path)]``. A root whose local path is kept keeps its
    recorded device."""
    kept = {row.local_path: row.last_dev for row in get_roots(library_id, target)}
    _target(target).execute(sa.delete(Roots).where(Roots.c.library_id == library_id))
    for server_path, local_path in roots:
        _insert(target, Roots, dict(library_id=library_id, server_path=server_path, local_path=local_path,
                                    last_dev=kept.get(local_path)))


def delete_source(source_id, target=None):
    """Delete a source. Its libraries, roots, groups and items go with it (cascade); its history and blacklist
    rows stay, with their links set to NULL."""
    _target(target).execute(sa.delete(Sources).where(Sources.c.id == source_id))


# Groups

def get_group(group_id, target=None, *, for_update=False):
    statement = sa.select(Groups).where(Groups.c.id == group_id)
    if for_update:
        statement = statement.with_for_update(key_share=True)
    return _target(target).execute(statement).first()


def get_item_group(item, target=None):
    if item.group_id is None:
        return None
    statement = sa.select(Groups).where(Groups.c.id == item.group_id, Groups.c.source_id == item.source_id,
                                        Groups.c.library_id == item.library_id)
    return _target(target).execute(statement).first()


def library_groups(library_id, target=None):
    return _target(target).execute(
        sa.select(Groups).where(Groups.c.library_id == library_id).order_by(Groups.c.id)).all()


def update_group(group_id, target=None, *, owner_library_id=None, only_automatic=False,
                 protect_user=(), **values):
    values['updated_at'] = datetime.now()
    statement = sa.update(Groups).where(Groups.c.id == group_id)
    if owner_library_id is not None:
        statement = statement.where(Groups.c.library_id == owner_library_id)
    if only_automatic:
        statement = statement.where(Groups.c.identity_source != 'user')
    return _target(target).execute(statement.values(**_protect_user(Groups, values, protect_user))).rowcount


def _protect_user(table, values, fields):
    # A manual edit may have been saved after the scan read this row. Keep its
    # identity while still applying file and group bookkeeping from the scan.
    for field in fields:
        values[field] = sa.case((table.c.identity_source == 'user', table.c[field]), else_=values[field])
    return values


def delete_empty_groups(group_ids, target=None, *, owner_library_id=None):
    """Delete the groups among ``group_ids`` that hold no item. Returns how many were deleted."""
    group_ids = list(group_ids)
    if not group_ids:
        return 0
    held = sa.select(Items.c.id).where(Items.c.group_id == Groups.c.id).exists()
    statement = sa.delete(Groups).where(Groups.c.id.in_(group_ids), ~held)
    if owner_library_id is not None:
        statement = statement.where(Groups.c.library_id == owner_library_id)
    return _target(target).execute(statement).rowcount


def create_group(*, source_id, library_id, kind, group_key, title, identity_source='filename',
                 identity_state='parsed', target=None, **extra):
    now = datetime.now()
    return _insert(target, Groups, dict(
        source_id=source_id, library_id=library_id, kind=kind, group_key=group_key, title=title,
        sort_title=extra.pop('sort_title', None) or title.lower(), identity_source=identity_source,
        identity_state=identity_state, created_at=now, updated_at=now, **extra))


# Items

def get_item(item_id, target=None, *, group_id=None, for_update=False, nowait=False):
    statement = sa.select(Items).where(Items.c.id == item_id)
    if group_id is not None:
        statement = statement.where(group_item_scope(group_id))
    if for_update:
        statement = statement.with_for_update(nowait=nowait)
    return _target(target).execute(statement).first()


def get_item_by_path(path, target=None):
    return _target(target).execute(sa.select(Items).where(Items.c.path == path)).first()


def group_item_scope(group_id):
    return sa.and_(Items.c.group_id == group_id, sa.select(Groups.c.id).where(
        Groups.c.id == group_id, Groups.c.source_id == Items.c.source_id,
        Groups.c.library_id == Items.c.library_id,
    ).exists())


def list_items(*, source_id=None, library_id=None, group_id=None, kind=None, limit=100, offset=0, target=None):
    statement = sa.select(Items)
    if source_id is not None:
        statement = statement.where(Items.c.source_id == source_id)
    if library_id is not None:
        statement = statement.where(Items.c.library_id == library_id)
    if group_id is not None:
        statement = statement.where(group_item_scope(group_id)).order_by(
            sa.func.coalesce(Items.c.season, 0), sa.func.coalesce(Items.c.episode, 0), Items.c.part_number)
    if kind is not None:
        statement = statement.where(Items.c.kind == kind)
    statement = statement.order_by(Items.c.sort_title, Items.c.id).limit(limit).offset(offset)
    return _target(target).execute(statement).all()


def count_items(*, source_id=None, library_id=None, group_id=None, kind=None, target=None):
    statement = sa.select(sa.func.count()).select_from(Items)
    if source_id is not None:
        statement = statement.where(Items.c.source_id == source_id)
    if library_id is not None:
        statement = statement.where(Items.c.library_id == library_id)
    if group_id is not None:
        statement = statement.where(group_item_scope(group_id))
    if kind is not None:
        statement = statement.where(Items.c.kind == kind)
    return _target(target).execute(statement).scalar_one()


_MATCH_COLUMNS = ('id', 'path', 'kind', 'group_id', 'source_id', 'library_id', 'file_dev', 'file_ino', 'file_size',
                  'file_mtime_ns', 'identity_source')


def library_items(library_id, target=None):
    """Every item of a library with what matching a discovered file compares. Not the whole row: the codec cache
    of a large library is megabytes the scan has no use for."""
    return _target(target).execute(
        sa.select(*(Items.c[name] for name in _MATCH_COLUMNS)).where(Items.c.library_id == library_id)).all()


def foreign_candidates(inodes, paths, library_id, target=None):
    """Items of other libraries, of any source, that may be the same file as a discovered one: the ones whose
    stored inode is among ``inodes`` or whose path is among ``paths``. Each comes with its library's content
    kind and the enabled state of its library and source, and its source's name."""
    inodes, paths = list(inodes), list(paths)
    seen = {}
    for start in range(0, max(len(inodes), len(paths)), 500):
        condition = sa.or_(Items.c.file_ino.in_(inodes[start:start + 500]),
                           Items.c.path.in_(paths[start:start + 500]))
        for row in _target(target).execute(
                sa.select(*(Items.c[name] for name in _MATCH_COLUMNS),
                          Libraries.c.content_kind, Libraries.c.enabled.label('library_enabled'),
                          Sources.c.enabled.label('source_enabled'), Sources.c.name.label('source_name'))
                .select_from(Items.join(Libraries, Items.c.library_id == Libraries.c.id)
                             .join(Sources, Items.c.source_id == Sources.c.id))
                .where(Items.c.library_id != library_id, condition)).all():
            seen[row.id] = row
    return list(seen.values())


def pending_movies(library_id, target=None):
    return _target(target).execute(
        sa.select(Items.c.id, Items.c.identity_source, Items.c.parsed_title, Items.c.title, Items.c.year,
                  Items.c.path, Items.c.scene_name, Items.c.identity_state)
        .where(Items.c.library_id == library_id, Items.c.kind == 'movie', Items.c.identify_pending == 1)).all()


def _episode_needs_reparse():
    return sa.and_(Items.c.identity_source == 'filename', Items.c.identity_state == 'unmatched',
                   Items.c.scene_name.is_(None), Items.c.identify_pending == 1)


def pending_episode_parses(library_id, target=None):
    return _target(target).execute(
        sa.select(Items.c.id, Items.c.path, Items.c.kind, Items.c.library_id, Items.c.group_id, Groups.c.group_key)
        .select_from(Items.join(Groups, Items.c.group_id == Groups.c.id))
        .where(Items.c.library_id == library_id, Items.c.kind == 'episode', _episode_needs_reparse())).all()


def group_episode_paths(group_id, target=None):
    return _target(target).execute(
        sa.select(Items.c.path).where(Items.c.group_id == group_id, Items.c.kind == 'episode')).scalars().all()


def lock_show_groups(connection, group_ids):
    """Lock shows before writing or removing any of their episodes."""
    statement = sa.select(Groups.c.id, Groups.c.group_key).where(Groups.c.id.in_(group_ids))
    return dict(connection.execute(statement.order_by(Groups.c.id).with_for_update(key_share=True)).all())


def write_episode_identity(connection, group_id, save, *, previous_group_id=None, group_key=None):
    """Lock the affected shows, then save and refresh the episode before commit."""
    group_ids = {group_id}
    if previous_group_id is not None:
        group_ids.add(previous_group_id)
    shows = lock_show_groups(connection, group_ids)
    if group_id not in shows or (group_key is not None and shows[group_id] != group_key):
        return None
    item_id = save()
    if item_id is not None:
        sync_episode_identities(connection, item_id=item_id)
    return item_id


def update_show_identity(group_id, connection, **values):
    """Write the show identity and its dependent episode states in one transaction."""
    updated = update_group(group_id, connection, **values)
    if updated:
        sync_episode_identities(connection, group_id=group_id)
    return updated


def sync_episode_identities(connection, *, group_id=None, item_id=None):
    """Refresh episode state with the parent locked by the caller."""
    group = sa.select(Groups).where(Groups.c.id == Items.c.group_id)
    group_state = group.with_only_columns(Groups.c.identity_state).scalar_subquery()
    # Keep unlocated legacy rows queued until their filenames have been parsed,
    # including when someone corrects the show before its next scan.
    pending = sa.case((Items.c.identity_source == 'user', 0), (_episode_needs_reparse(), 1),
                      else_=group.with_only_columns(Groups.c.identify_pending).scalar_subquery())
    state = sa.case(
        (sa.or_(Items.c.season.is_(None), Items.c.episode.is_(None), group_state == 'unmatched'), 'unmatched'),
        (group_state == 'ambiguous', 'ambiguous'),
        (Items.c.identity_source.in_(('media_server', 'user')), 'identified'),
        else_='parsed',
    )
    statement = sa.update(Items).where(Items.c.kind == 'episode',
                                       sa.or_(Items.c.identity_state != state, Items.c.identify_pending != pending))
    if group_id is not None:
        statement = statement.where(Items.c.group_id == group_id)
    if item_id is not None:
        statement = statement.where(Items.c.id == item_id)
    connection.execute(statement.values(identity_state=state, identify_pending=pending, updated_at=datetime.now()))


def shadow_state(item_ids, target=None):
    """Each item's id, path, library and the columns of its arr shadow."""
    item_ids = list(item_ids)
    rows = []
    for start in range(0, len(item_ids), 500):
        rows.extend(_target(target).execute(
            sa.select(Items.c.id, Items.c.path, Items.c.library_id, *(Items.c[name] for name in SHADOW_COLUMNS))
            .where(Items.c.id.in_(item_ids[start:start + 500]))).all())
    return rows


def _check_parent(target, kind, group_id):
    """A movie has no group, an episode belongs to a show and a sports event to a league."""
    if kind not in ITEM_KINDS:
        raise ValueError(f'Unknown item kind {kind!r}')
    expected = GROUP_KIND_OF_ITEM.get(kind)
    if expected is None:
        if group_id is not None:
            raise ValueError('A movie has no group')
        return
    if group_id is None:
        raise ValueError(f'A {kind} needs a group')
    row = _target(target).execute(sa.select(Groups.c.kind).where(Groups.c.id == group_id)).first()
    if row is None:
        raise ValueError(f'Group {group_id} does not exist')
    if row.kind != expected:
        raise ValueError(f'A {kind} belongs to a {expected}, not a {row.kind}')


def insert_item(target=None, **values):
    _check_parent(target, values['kind'], values.get('group_id'))
    now = datetime.now()
    values.setdefault('sort_title', values['title'].lower())
    values.setdefault('created_at', now)
    values.setdefault('updated_at', now)
    return _insert(target, Items, values)


def update_item(item_id, target=None, *, owner_library_id=None, only_automatic=False,
                protect_user=(), previous_group_id=None, **values):
    values['updated_at'] = datetime.now()
    statement = sa.update(Items).where(Items.c.id == item_id)
    if owner_library_id is not None:
        statement = statement.where(Items.c.library_id == owner_library_id)
    if previous_group_id is not None:
        statement = statement.where(Items.c.group_id == previous_group_id)
    if only_automatic:
        statement = statement.where(Items.c.identity_source != 'user')
    return _target(target).execute(statement.values(**_protect_user(Items, values, protect_user))).rowcount


def delete_items(item_ids, target=None, *, owner_library_id=None):
    """Delete items. Their history and blacklist rows stay, with ``item_id`` set to NULL."""
    item_ids = list(item_ids)
    deleted = 0
    # In batches: SQLite refuses an IN list of more than 32,766 values.
    for start in range(0, len(item_ids), 500):
        statement = sa.delete(Items).where(Items.c.id.in_(item_ids[start:start + 500]))
        if owner_library_id is not None:
            statement = statement.where(Items.c.library_id == owner_library_id)
        deleted += _target(target).execute(statement).rowcount
    return deleted


def write_item_index(item_id, subtitles, missing_subtitles, target=None, *, indexed=True, **metadata):
    """Store an item's subtitle listing and its missing languages. The update is by id and must change exactly
    one row: the arr indexers update by path, and an UPDATE that matches nothing succeeds silently, which is how
    a listing for a vanished item gets lost without a trace."""
    if indexed:
        metadata['indexed_at'] = datetime.now()
    changed = _target(target).execute(sa.update(Items).where(Items.c.id == item_id).values(
        subtitles=subtitles, missing_subtitles=missing_subtitles, **metadata)).rowcount
    if changed != 1:
        logging.warning('BAZARR the subtitle index of source item %s changed %s rows instead of one',
                        item_id, changed)
    return changed == 1


def set_failed_attempts(item_id, value, target=None):
    _target(target).execute(sa.update(Items).where(Items.c.id == item_id).values(failed_attempts=value))


# History and blacklist

def insert_history(target=None, **values):
    values.setdefault('timestamp', datetime.now())
    return _insert(target, History, values)


def insert_blacklist(target=None, **values):
    values.setdefault('timestamp', datetime.now())
    return _insert(target, Blacklist, values)


def blacklist_pairs(item_kind, target=None):
    """The ``(provider, subs_id)`` pairs the source pool of one item kind must skip.

    Every row of the kind, whichever source it was written for and whether or not that source still exists: a
    moved file keeps its exclusions, and a removed source's rows stay listed as removed until the user clears
    them."""
    return [(row.provider, row.subs_id) for row in _target(target).execute(
        sa.select(Blacklist.c.provider, Blacklist.c.subs_id).where(Blacklist.c.item_kind == item_kind)).all()]


def retained_activity_by_kind(target=None):
    """History and blacklist rows can outlive both the source and its library."""
    by_kind = {'movie': 'movie', 'series': 'episode', 'sports': 'sports_event'}
    checks = [sa.or_(sa.exists().where(History.c.item_kind == item_kind),
                     sa.exists().where(Blacklist.c.item_kind == item_kind)).label(content_kind)
              for content_kind, item_kind in by_kind.items()]
    checks.append(sa.or_(*(sa.exists().where(table.c.item_kind == 'sports_event', table.c.source_id.is_(None))
                           for table in (History, Blacklist))).label('removed_sports'))
    return _target(target).execute(sa.select(*checks)).mappings().one()


# Enabled flags, overlap and holders

def item_state(item_id, target=None):
    """The availability and owner details of an item, or None."""
    row = _target(target).execute(
        sa.select(Items.c.id, Items.c.title, Items.c.server_missing, Items.c.file_dev, Items.c.file_ino,
                  Libraries.c.enabled.label('library_enabled'),
                  Sources.c.enabled.label('source_enabled'), Sources.c.name.label('source_name'))
        .select_from(Items.join(Libraries, Items.c.library_id == Libraries.c.id)
                     .join(Sources, Items.c.source_id == Sources.c.id))
        .where(Items.c.id == item_id)).first()
    return row


def normalize_path(path):
    """The form a folder path is stored and compared in: normalised, with no doubled leading slash."""
    path = os.path.normpath(path)
    if path.startswith('//') and not path.startswith('///'):
        # POSIX keeps exactly two leading slashes, which name the same folder as one.
        path = path[1:]
    return path


def _normal(path):
    return os.path.normcase(normalize_path(path))


def folder_identity(path):
    """``(st_dev, st_ino)`` of a folder, or None when it cannot be read. Two paths with one identity are one
    folder, which no spelling of the path shows for a bind mount."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino)


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def overlapping_root(path, target=None, exclude_library_id=None):
    """The ``(source name, root path)`` of a library root that is inside ``path`` or contains it, or None.

    Compared by the spelling, by the real path (a symlink to a root is that root) and by device and inode (a bind
    mount of a root is that root, whatever its path). The roots of ``exclude_library_id`` do not count: a library
    does not overlap itself."""
    wanted = [_normal(path), os.path.normcase(os.path.realpath(path))]
    identity = folder_identity(path)
    rows = _target(target).execute(
        sa.select(Roots.c.local_path, Roots.c.library_id, Sources.c.name)
        .select_from(Roots.join(Libraries, Roots.c.library_id == Libraries.c.id)
                     .join(Sources, Libraries.c.source_id == Sources.c.id))).all()
    for row in rows:
        if exclude_library_id is not None and row.library_id == exclude_library_id:
            continue
        existing = {_normal(row.local_path), os.path.normcase(os.path.realpath(row.local_path))}
        if any(_inside(one, other) or _inside(other, one) for one in wanted for other in existing):
            return row.name, row.local_path
        if identity is not None and folder_identity(row.local_path) == identity:
            return row.name, row.local_path
    return None
