# coding=utf-8
"""Shared eligibility for source Wanted lists, badges and automatic searches."""

import ast
import logging
from threading import Lock

import sqlalchemy as sa

from app.config import settings
from app.jobs_queue import JobFailed, jobs_queue

from . import identity, repository, shadows
from .db import handle

WANTED_JOB_ID = 'wanted_search_missing_subtitles_sources'
_PAGE_SIZE = 500
_run_lock = Lock()


def wanted_items(*columns, kind=None, profile_id=None):
    """Select items with stored missing subtitles and an effective, enabled profile."""
    items, sources, libraries, groups = repository.Items, repository.Sources, repository.Libraries, repository.Groups
    parents = items.join(sources, items.c.source_id == sources.c.id).join(
        libraries, items.c.library_id == libraries.c.id).outerjoin(groups, items.c.group_id == groups.c.id)
    effective_profile = sa.func.coalesce(items.c.profile_id, groups.c.profile_id, libraries.c.profile_id)
    profile = effective_profile.is_not(None) if profile_id is None else effective_profile == profile_id
    query = sa.select(*columns).select_from(parents).where(
        # Keep the literal predicate in the partial index usable in both dialects.
        items.c.missing_subtitles.is_not(None), items.c.missing_subtitles != sa.literal_column("'[]'"),
        sources.c.enabled == 1, libraries.c.enabled == 1, items.c.monitored == 1, items.c.arr_shadow == 0,
        items.c.server_missing == 0, items.c.identity_state.in_(('identified', 'parsed')), profile)
    return query if kind is None else query.where(items.c.kind == kind)


def configured_item_kinds(session):
    items = repository.Items
    return set(session.scalars(sa.select(items.c.kind).distinct()))


def wanted_counts(session=None):
    items = repository.Items
    counts = dict.fromkeys(repository.ITEM_KINDS, 0)
    for kind, missing in handle(session).execute(wanted_items(items.c.kind, items.c.missing_subtitles)):
        counts[kind] += len(ast.literal_eval(missing))
    return counts


def wanted_search_missing_subtitles_sources(kind=None, job_id=None, wait_for_completion=False):
    """Queue a Wanted search, or run it within its JobsQueue job."""
    if not job_id:
        return jobs_queue.add_job_from_function('Search for Missing Source Subtitles', is_progress=True,
                                                wait_for_completion=wait_for_completion, return_existing=True)
    from .service import search_wanted_item

    with _run_lock:
        jobs_queue.raise_if_cancelled(job_id)
        owners = identity.ArrOwners.load()
        refreshed = shadows.revalidate_shadows(owners=owners)
        if refreshed['cleared'] or refreshed['refreshed']:
            notify_wanted_changes()
        items = repository.Items
        query = wanted_items(items.c.id, items.c.created_at, items.c.title, kind=kind)
        session = handle()
        last_id = 0
        while True:
            page = session.execute(query.where(items.c.id > last_id).order_by(items.c.id).limit(_PAGE_SIZE)).all()
            if not page:
                break
            last_id = page[-1].id
            for item_id, created_at, title in page:
                jobs_queue.raise_if_cancelled(job_id)
                jobs_queue.update_job_progress(job_id=job_id, progress_message=title)
                try:
                    search_wanted_item(item_id, created_at, job_id)
                except JobFailed as error:
                    logging.info('BAZARR source item %s could not be searched: %s', item_id, error)
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='Search completed')



def queue_wanted_search(kind):
    """Queue Search All only for a kind with at least one enabled library."""
    sources, libraries = repository.Sources, repository.Libraries
    content_kind = 'series' if kind == 'episode' else kind
    enabled = sa.select(libraries.c.id).join(sources, libraries.c.source_id == sources.c.id).where(
        libraries.c.content_kind == content_kind, libraries.c.enabled == 1, sources.c.enabled == 1)
    if handle().scalar(sa.select(enabled.exists())):
        return wanted_search_missing_subtitles_sources(kind=kind)
    return None



def arr_wanted_condition(kind, owner_column):
    """Match the active Arr owners when a Wanted caller opts into a mixed list."""
    from app.database import TableArrInstances

    if not getattr(settings.general, f'use_{kind}'):
        return sa.false()
    instances = sa.select(TableArrInstances.id).where(TableArrInstances.kind == kind)
    enabled = instances.where(TableArrInstances.enabled == 1)
    legacy = settings[kind]
    if legacy.get('ip') and legacy.get('apikey'):
        return sa.or_(owner_column.in_(enabled), sa.and_(owner_column.is_(None), ~instances.exists()))
    return owner_column.in_(enabled)



def notify_wanted_changes(kind=None):
    """Invalidate Wanted pages by kind; source and Arr item IDs occupy different spaces."""
    from app.event_handler import event_stream

    if kind in (None, 'episode'):
        event_stream(type='reset-episode-wanted')
    if kind in (None, 'movie'):
        event_stream(type='reset-movie-wanted')
    if kind in (None, 'sports_event'):
        event_stream(type='sports')
    event_stream(type='badges')
