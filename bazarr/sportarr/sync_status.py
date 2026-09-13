"""Sync status for an owned event listing with one history and queue snapshot."""

import os
from datetime import datetime, timedelta

from sqlalchemy import func, select, tuple_

from app.config import settings
from app.database import TableHistorySports
from app.jobs_queue import jobs_queue
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings
from utilities.security_guards import subtitle_path_within_area


def add_sync_status(session, rows, data):
    if not rows:
        return
    identities = [(event.arr_instance_id, event.id) for event, _, _ in rows]
    history = {
        (owner, event_id, path): timestamp
        for owner, event_id, path, timestamp in session.execute(
            select(TableHistorySports.arr_instance_id, TableHistorySports.event_id,
                   TableHistorySports.subtitles_path, func.max(TableHistorySports.timestamp))
            .where(TableHistorySports.action == 5,
                   tuple_(TableHistorySports.arr_instance_id, TableHistorySports.event_id).in_(identities))
            .group_by(TableHistorySports.arr_instance_id, TableHistorySports.event_id,
                      TableHistorySports.subtitles_path))
    }
    jobs = tuple(jobs_queue.jobs_running_queue) + tuple(jobs_queue.jobs_pending_queue)
    active = {}
    legacy = []
    for job in jobs:
        if getattr(job, 'status', None) not in ('pending', 'running'):
            continue
        kwargs = getattr(job, 'kwargs', {}) or {}
        owner, event_id = kwargs.get('arr_instance_id'), kwargs.get('sports_event_id')
        state = {'jobStatus': job.status, 'jobId': getattr(job, 'job_id', None)}
        if kwargs.get('srt_path'):
            path = os.path.normcase(os.path.abspath(kwargs['srt_path']))
            active.setdefault((owner, event_id, path), state)
        elif 'sync' in (getattr(job, 'job_name', '') or '').lower():
            legacy.append((owner, event_id, job.job_name, state))
    for (event, _, raw_mapping), result in zip(rows, data):
        mapping = read_sports_mappings(raw_mapping)
        states = result['sync_status'] = {}
        for item in result['subtitles']:
            if (not isinstance(item, (list, tuple)) or len(item) < 2
                    or not isinstance(item[0], str) or not isinstance(item[1], str) or not item[1]
                    or any(part.startswith('combined-') for part in item[0].lower().split(':')[1:])):
                continue
            language, remote_path = item[:2]
            path = apply_sports_mapping(remote_path, mapping)
            if not subtitle_path_within_area(path, result['mapped_path'],
                    subfolder_mode=settings.general.subfolder,
                    custom_subfolder=settings.general.subfolder_custom):
                continue
            try:
                modified = os.stat(path).st_mtime
            except OSError:
                continue
            if language in states and states[language]['lastModified'] >= modified:
                continue
            history_path = apply_sports_mapping(path, mapping, reverse=True)
            timestamp = history.get((event.arr_instance_id, event.id, history_path))
            edited = bool(timestamp and datetime.fromtimestamp(modified) > timestamp + timedelta(seconds=1))
            key = os.path.normcase(os.path.abspath(path))
            job = next((active[identity] for identity in (
                (event.arr_instance_id, event.id, key), (event.arr_instance_id, None, key),
                (None, event.id, key), (None, None, key)) if identity in active), None)
            if job is None:
                job = next((state for owner, event_id, name, state in legacy
                            if owner in (None, event.arr_instance_id) and event_id in (None, event.id)
                            and path in name), None)
            states[language] = {
                'synced': bool(timestamp), 'confirmed': bool(timestamp) and not edited and not job,
                'editedAfterSync': edited, 'lastModified': modified,
                'lastSyncTimestamp': timestamp.isoformat() if timestamp else None,
                'jobStatus': job['jobStatus'] if job else None, 'jobId': job['jobId'] if job else None,
            }
