# coding=utf-8
"""Keeping the arr shadows of source items true.

A source item is shadowed when an arr row of an enabled owner holds the same file: the arr side manages it, and
the source never searches it automatically. The scanner computes shadows for the files it sees. This module keeps
them true between scans, with no disk walk: when an arr instance is created, enabled, disabled or deleted, or a
master toggle changes, the arr-instance service and the settings save queue a refresh; before an automatic search
the path stage is checked once more; and the source wanted job re-validates every shadow against the row that set
it.
"""
import logging

import sqlalchemy as sa

from app.jobs_queue import jobs_queue

from . import identity, repository
from .db import handle

_PAGE = 1000


def _session(target):
    return handle(target)


def _queue_library_scan(item):
    """The item's own file cannot be statted right now (the arr renamed or upgraded it inside a source folder):
    the shadow is kept and the item's library is scanned, so the item is found again or deleted by the rules."""
    try:
        from .scanner import scan_media_source
        scan_media_source(item.source_id, item.library_id)
    except Exception:
        logging.exception('BAZARR could not queue a scan of the library of source item %s', item.id)


def revalidate_shadows(item_ids=None, *, owners=None, target=None):
    """Judge every shadowed item (or the given ones) against the row that set its shadow. Returns the counts of
    shadows kept, cleared and refreshed."""
    session = _session(target)
    owners = owners or identity.ArrOwners.load(target)
    query = sa.select(repository.Items.c.id, repository.Items.c.path, repository.Items.c.source_id,
                      repository.Items.c.library_id, *(repository.Items.c[name] for name in identity.SHADOW_COLUMNS)
                      ).where(repository.Items.c.arr_shadow == 1)
    if item_ids is not None:
        query = query.where(repository.Items.c.id.in_(list(item_ids)))
    counts = {'kept': 0, 'cleared': 0, 'refreshed': 0}
    for item in session.execute(query.order_by(repository.Items.c.id)).all():
        action, match = identity.revalidate_shadow(item, owners, queue_scan=_queue_library_scan)
        if action == 'clear':
            repository.update_item(item.id, session, **identity.CLEARED_SHADOW)
            counts['cleared'] += 1
        elif action == 'refresh':
            repository.update_item(item.id, session, **identity.shadow_values(match))
            counts['refreshed'] += 1
        else:
            counts['kept'] += 1
    return counts


def run_refresh(kind=None, *, target=None):
    """The shadow refresh, with no disk walk.

    First every flag whose owner no longer counts is cleared, including one whose ``shadow_instance_id`` is already
    NULL (its instance was deleted and the foreign key set it NULL), and the rest are re-validated. Then both
    stages run for the files nothing shadows, against the rows of the enabled owners of ``kind`` (all kinds when
    none is given), using each item's stored file identity so that no source file is statted."""
    session = _session(target)
    owners = identity.ArrOwners.load(target)
    counts = revalidate_shadows(owners=owners, target=target)
    index = owners.index({kind} if kind else None)
    counts['shadowed'] = 0
    if not index:
        return counts
    items = repository.Items
    last_id = 0
    while True:
        page = session.execute(
            sa.select(items.c.id, items.c.path, items.c.file_dev, items.c.file_ino, items.c.file_size,
                      items.c.file_mtime_ns)
            .where(items.c.arr_shadow == 0, items.c.id > last_id).order_by(items.c.id).limit(_PAGE)).all()
        if not page:
            break
        last_id = page[-1].id
        for item in page:
            stat = None
            if item.file_size is not None and item.file_mtime_ns is not None:
                stat = identity.FileStat(item.file_dev, item.file_ino, item.file_size, item.file_mtime_ns)
            match = index.match(item.path, stat)
            if match is not None:
                repository.update_item(item.id, session, **identity.shadow_values(match))
                counts['shadowed'] += 1
    return counts


def refresh_shadows(kind=None, job_id=None):
    """The refresh as a job. Called without a job id it queues itself, so it shows in the Jobs drawer."""
    if not job_id:
        return jobs_queue.add_job_from_function(f"Refreshing media source shadows ({kind or 'all kinds'})",
                                                is_progress=False)
    counts = run_refresh(kind)
    logging.info('BAZARR media source shadows refreshed: %s', counts)
    return None


def _has_items():
    return handle().execute(sa.select(repository.Items.c.id).limit(1)).first() is not None


def queue_shadow_refresh(kind=None):
    """Queue a refresh when something that decides a shadow changed: an arr instance was created, enabled,
    disabled or deleted, or a master toggle moved. Best effort and quiet: it runs after the change committed, an
    install with no source item has nothing to refresh, and a failure here is logged, never raised."""
    try:
        if not _has_items():
            return None
        return refresh_shadows(kind)
    except Exception:
        logging.exception('BAZARR could not queue a media source shadow refresh')
        return None


def path_stage_shadow(item_id, *, target=None):
    """Right before an automatic search: the path stage only, against the rows of enabled owners, one indexed
    lookup per owner and no stat. A match sets the shadow and returns True: the search is skipped."""
    session = _session(target)
    item = session.execute(sa.select(repository.Items.c.id, repository.Items.c.path, repository.Items.c.arr_shadow)
                           .where(repository.Items.c.id == item_id)).first()
    if item is None:
        return False
    if item.arr_shadow:
        return True
    match = identity.ArrOwners.load(target).path_stage(item.path)
    if match is None:
        return False
    repository.update_item(item_id, session, **identity.shadow_values(match))
    return True
