# coding=utf-8

"""Series and league combine, run as one queued job each.

The routes used to loop over every episode or event inside the request, so a
long series held the request for the whole run and nothing showed in Jobs. The
route now queues one of these, which reports per-item progress and fails with
a summary when any item failed.
"""

import logging

from app.database import TableEpisodes, TableShows, TableSportsEvents, TableSportsLeagues, database, select
from app.jobs_queue import jobs_queue, JobFailed
from arr_instances.resolution import scoped
from subtitles.job_errors import describe_failures
from utilities.path_mappings import path_mappings

from .main import try_combine_for_video

logger = logging.getLogger(__name__)


class CombineTally:
    def __init__(self, noun):
        self.noun = noun
        self.built = 0
        self.skipped = 0
        self.warnings = 0
        self.failures = []
        self.details = []

    def add(self, name, detail):
        self.details.append(detail)
        status = detail.get('status')
        if status == 'built':
            self.built += 1
            if detail.get('error'):
                # Published, but a follow-up step such as the index refresh
                # did not complete.
                self.warnings += 1
        elif status == 'skipped':
            self.skipped += 1
        else:
            reason = detail.get('error') or detail.get('reason') or 'combine failed'
            self.failures.append(f'{name} ({reason})')

    @property
    def failed(self):
        return len(self.failures)

    def summary(self):
        return {'status': 'batch_complete', 'built': self.built, 'skipped': self.skipped,
                'failed': self.failed, 'warnings': self.warnings, 'details': self.details}

    def counts(self):
        text = f'built {self.built}, skipped {self.skipped}, failed {self.failed}'
        if self.warnings:
            text += f', {self.warnings} needing attention'
        return text

    def finish(self, job_id, title):
        total = len(self.details)
        if self.failures:
            raise JobFailed(f'Combine for {title}: {self.failed} of {total} {self.noun} failed '
                                   f'({self.counts()}). {describe_failures(self.failures)}')
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f'Combined subtitles for {title}: {self.counts()}')
        return self.summary()


def _report(job_id, index, total, name):
    jobs_queue.update_job_progress(job_id=job_id, progress_value=index, progress_max=total,
                                   progress_message=f'{name} ({index}/{total})')


def combine_series_subtitles(series_id, languages=None, format=None, arr_instance_id=None, job_id=None):
    """Build the combined subtitle for every episode of a series."""
    rows = database.execute(scoped(
        select(TableEpisodes.sonarrEpisodeId, TableEpisodes.sonarrSeriesId, TableEpisodes.path,
               TableEpisodes.arr_instance_id, TableEpisodes.season, TableEpisodes.episode,
               TableShows.title)
        .select_from(TableEpisodes)
        .join(TableShows)
        .where(TableEpisodes.sonarrSeriesId == series_id)
        .order_by(TableEpisodes.season, TableEpisodes.episode),
        TableEpisodes.arr_instance_id, arr_instance_id)).all()
    if not rows:
        raise JobFailed('The series has no episodes in the library.')

    title = rows[0].title
    tally = CombineTally('episodes')
    total = len(rows)
    for index, row in enumerate(rows):
        name = f'S{(row.season or 0):02d}E{(row.episode or 0):02d}'
        _report(job_id, index, total, name)
        try:
            video_path = path_mappings.path_replace_instance(row.path, row.arr_instance_id, 'episode')
            result = try_combine_for_video(
                video_path=video_path,
                media_type='series',
                radarr_id=None,
                sonarr_series_id=row.sonarrSeriesId,
                sonarr_episode_id=row.sonarrEpisodeId,
                languages=languages,
                format=format,
                arr_instance_id=row.arr_instance_id,
            )
            detail = {'episodeId': row.sonarrEpisodeId, 'status': result.status, 'path': result.path,
                      'reason': result.reason, 'error': result.error}
        except (ValueError, OSError) as exc:
            # One unreadable or moved file must not end the batch.
            logger.warning('BAZARR combine failed for %s %s: %s', title, name, exc)
            detail = {'episodeId': row.sonarrEpisodeId, 'status': 'failed', 'path': '', 'reason': '',
                      'error': str(exc)}
        tally.add(name, detail)
    _report(job_id, total, total, 'Done')
    return tally.finish(job_id, title)


def combine_league_subtitles(league_id, arr_instance_id, job_id=None):
    """Build the combined subtitle for every event of a league that asks for one."""
    from sportarr.identity import resolve_event_in_session
    from sportarr.profile_hooks import capture_profile_operation
    from sportarr.subtitles import candidate_signature

    league = database.execute(
        select(TableSportsLeagues.title)
        .where(TableSportsLeagues.id == league_id, TableSportsLeagues.arr_instance_id == arr_instance_id)
    ).first()
    events = database.execute(
        select(TableSportsEvents.id, TableSportsEvents.title)
        .where(TableSportsEvents.league_id == league_id, TableSportsEvents.arr_instance_id == arr_instance_id)
        .order_by(TableSportsEvents.id)
    ).all()
    if not events:
        raise JobFailed('The league has no events in the library.')

    title = league.title if league else f'league {league_id}'
    tally = CombineTally('events')
    total = len(events)
    for index, event in enumerate(events):
        name = event.title or f'event {event.id}'
        _report(job_id, index, total, name)
        try:
            context = resolve_event_in_session(database, event.id, arr_instance_id)
            operation = capture_profile_operation(context, candidate_signature(context))
            if not operation.profile:
                detail = {'eventId': event.id, 'status': 'skipped', 'path': '',
                          'reason': 'no language profile is assigned', 'error': ''}
            else:
                result = try_combine_for_video(
                    video_path=context.mapped_path,
                    media_type='sports',
                    sports_operation=operation,
                )
                detail = {'eventId': event.id, 'status': result.status, 'path': result.path,
                          'reason': result.reason, 'error': result.error}
        except (ValueError, OSError) as exc:
            logger.warning('BAZARR combine failed for %s %s: %s', title, name, exc)
            detail = {'eventId': event.id, 'status': 'failed', 'path': '', 'reason': '', 'error': str(exc)}
        tally.add(name, detail)
    _report(job_id, total, total, 'Done')
    return tally.finish(job_id, title)
