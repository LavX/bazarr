# coding=utf-8
"""The hourly task that scans every enabled source whose last scan is older than its own frequency.

It is registered while an enabled source exists and removed when none does. The source and library endpoints, and
the settings that decide what is enabled, call ``update_configurable_tasks`` after they change it.
"""
import logging
import sys
from datetime import datetime, timedelta

import sqlalchemy as sa

from . import repository
from .db import handle
from .scanner import STALE_SCAN_HOURS, scan_media_source

JOB_ID = 'scan_media_sources'


def has_enabled_source(session=None):
    return handle(session).execute(
        sa.select(repository.Sources.c.id).where(repository.Sources.c.enabled == 1).limit(1)).first() is not None


def due_sources(now=None, session=None):
    """The enabled sources a scan is due for. Frequency 0 means manual scans only; a source with no enabled library
    has nothing to scan; a source whose scan started recently and has not finished is running, and is left alone.
    One source with a value that cannot be worked with never stops the others being found."""
    now = now or datetime.now()
    with_libraries = {row.source_id for row in handle(session).execute(
        sa.select(repository.Libraries.c.source_id).where(repository.Libraries.c.enabled == 1)).all()}
    due = []
    for source in repository.list_sources(session):
        if not source.enabled or not source.scan_frequency_hours or source.id not in with_libraries:
            continue
        try:
            started, finished = source.last_scan_started_at, source.last_scan_finished_at
            if started is not None and (finished is None or started > finished) \
                    and now - started < timedelta(hours=STALE_SCAN_HOURS):
                continue
            if finished is not None and now - finished < timedelta(hours=source.scan_frequency_hours):
                continue
        except OverflowError:
            # Larger than any date: no scan is ever due.
            logging.warning('BAZARR the scan frequency of media source %s is out of range', source.name)
            continue
        due.append(source)
    return due


def scan_due_media_sources():
    """The hourly task: queue a scan job for each source that is due."""
    try:
        due = due_sources()
    except Exception:
        logging.exception('BAZARR could not work out which media sources are due for a scan')
        return
    for source in due:
        try:
            scan_media_source(source.id)
        except Exception:
            logging.exception('BAZARR could not queue a scan of media source %s', source.name)


def configure_source_jobs(aps_scheduler, session=None):
    """Register the hourly task while an enabled source exists, and remove it when none does."""
    if has_enabled_source(session):
        aps_scheduler.add_job(scan_due_media_sources, 'interval', hours=1, max_instances=1, coalesce=True,
                              misfire_grace_time=15, id=JOB_ID, name='Scan Media Sources', replace_existing=True)
    elif aps_scheduler.get_job(JOB_ID):
        aps_scheduler.remove_job(JOB_ID)


def refresh_tasks():
    """Re-register the scheduled tasks after something that decides what is enabled changed. Best effort: the
    change is already committed, so a scheduler that is not running must not fail the request."""
    # The scheduler registers every task when it starts, so one that has not been imported yet has nothing to
    # refresh: importing it from here would start it.
    module = sys.modules.get('app.scheduler')
    if module is None:
        return
    try:
        module.scheduler.update_configurable_tasks()
    except Exception:
        logging.exception('BAZARR could not refresh the scheduled tasks after a media source change')
