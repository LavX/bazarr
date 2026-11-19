# coding=utf-8
"""Reads and writes of the source tables.

Every function takes a ``target``: a session, or the connection of a ``source_transaction``. With none it uses
the application's session. Nothing here touches an arr or Sportarr table.
"""
import logging
from datetime import datetime

import sqlalchemy as sa

from app.database import (TableMediaSourceLibraries, TableMediaSourceLibraryRoots, TableMediaSources,
                          TableSourceBlacklist, TableSourceGroups, TableSourceHistory, TableSourceItems)

from .db import handle

ITEM_KINDS = ('movie', 'episode', 'sports_event')
# The kind of group each kind of item belongs to. A movie has none.
GROUP_KIND_OF_ITEM = {'episode': 'show', 'sports_event': 'league'}
CONTENT_KINDS = ('movie', 'series', 'sports')
SOURCE_KINDS = ('folder', 'jellyfin', 'emby')
IDENTITY_SOURCES = ('filename', 'tmdb', 'media_server', 'user')
IDENTITY_STATES = ('identified', 'parsed', 'ambiguous', 'unmatched')

Sources = TableMediaSources.__table__
Libraries = TableMediaSourceLibraries.__table__
Roots = TableMediaSourceLibraryRoots.__table__
Groups = TableSourceGroups.__table__
Items = TableSourceItems.__table__
History = TableSourceHistory.__table__
Blacklist = TableSourceBlacklist.__table__


def _target(target):
    return handle() if target is None else target


def _insert(target, table, values):
    return _target(target).execute(sa.insert(table).values(**values)).inserted_primary_key[0]


# Sources, libraries and roots

def get_source(source_id, target=None):
    return _target(target).execute(sa.select(Sources).where(Sources.c.id == source_id)).first()


def list_sources(target=None):
    return _target(target).execute(sa.select(Sources).order_by(Sources.c.id)).all()


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


def delete_source(source_id, target=None):
    """Delete a source. Its libraries, roots, groups and items go with it (cascade); its history and blacklist
    rows stay, with their links set to NULL."""
    _target(target).execute(sa.delete(Sources).where(Sources.c.id == source_id))


# Groups

def get_group(group_id, target=None):
    return _target(target).execute(sa.select(Groups).where(Groups.c.id == group_id)).first()


def create_group(*, source_id, library_id, kind, group_key, title, identity_source='filename',
                 identity_state='parsed', target=None, **extra):
    now = datetime.now()
    return _insert(target, Groups, dict(
        source_id=source_id, library_id=library_id, kind=kind, group_key=group_key, title=title,
        sort_title=extra.pop('sort_title', None) or title.lower(), identity_source=identity_source,
        identity_state=identity_state, created_at=now, updated_at=now, **extra))


# Items

def get_item(item_id, target=None):
    return _target(target).execute(sa.select(Items).where(Items.c.id == item_id)).first()


def get_item_by_path(path, target=None):
    return _target(target).execute(sa.select(Items).where(Items.c.path == path)).first()


def list_items(*, source_id=None, library_id=None, kind=None, limit=100, offset=0, target=None):
    statement = sa.select(Items)
    if source_id is not None:
        statement = statement.where(Items.c.source_id == source_id)
    if library_id is not None:
        statement = statement.where(Items.c.library_id == library_id)
    if kind is not None:
        statement = statement.where(Items.c.kind == kind)
    statement = statement.order_by(Items.c.sort_title, Items.c.id).limit(limit).offset(offset)
    return _target(target).execute(statement).all()


def count_items(*, source_id=None, library_id=None, kind=None, target=None):
    statement = sa.select(sa.func.count()).select_from(Items)
    if source_id is not None:
        statement = statement.where(Items.c.source_id == source_id)
    if library_id is not None:
        statement = statement.where(Items.c.library_id == library_id)
    if kind is not None:
        statement = statement.where(Items.c.kind == kind)
    return _target(target).execute(statement).scalar_one()


def library_item_paths(library_id, target=None):
    """Every item of a library with what the rescan test compares: ``(id, path, file_size, file_mtime_ns)``."""
    return _target(target).execute(
        sa.select(Items.c.id, Items.c.path, Items.c.file_size, Items.c.file_mtime_ns)
        .where(Items.c.library_id == library_id)).all()


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


def update_item(item_id, target=None, **values):
    values['updated_at'] = datetime.now()
    return _target(target).execute(sa.update(Items).where(Items.c.id == item_id).values(**values)).rowcount


def delete_items(item_ids, target=None):
    """Delete items. Their history and blacklist rows stay, with ``item_id`` set to NULL."""
    item_ids = list(item_ids)
    if item_ids:
        _target(target).execute(sa.delete(Items).where(Items.c.id.in_(item_ids)))
    return len(item_ids)


def write_item_index(item_id, subtitles, missing_subtitles, target=None):
    """Store an item's subtitle listing and its missing languages. The update is by id and must change exactly
    one row: the arr indexers update by path, and an UPDATE that matches nothing succeeds silently, which is how
    a listing for a vanished item gets lost without a trace."""
    changed = _target(target).execute(sa.update(Items).where(Items.c.id == item_id).values(
        subtitles=subtitles, missing_subtitles=missing_subtitles, indexed_at=datetime.now())).rowcount
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
    """The ``(provider, subs_id)`` pairs the source pool of one item kind must skip."""
    return [(row.provider, row.subs_id) for row in _target(target).execute(
        sa.select(Blacklist.c.provider, Blacklist.c.subs_id).where(Blacklist.c.item_kind == item_kind)).all()]
