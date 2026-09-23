# coding=utf-8
"""Recalculating missing subtitles after a save, as a queued job.

Saving language profiles, the toggles that change what counts as missing, and
the mass profile edit all recomputed missing subtitles inside the save
request: the whole library for the first two, every edited item for the last.
On a large library that held the request for minutes, and a proxy that gave up
first showed "Save failed" for a save that had in fact gone through. The save
now finishes on its own and this job does the recalculation after it.
"""

import logging
import uuid

from app.config import settings
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue

JOB_LABEL = "Recalculating missing subtitles"
JOB_MODULE = "subtitles.indexer.missing_refresh"
JOB_FUNC = "recalculate_missing_subtitles"


def _normalise(items):
    if items is None:
        return None
    return [[int(upstream_id), None if owner is None else int(owner)] for upstream_id, owner in items]


def _job_label(series, movies):
    if series is None and movies is None:
        return JOB_LABEL
    parts = []
    if series:
        parts.append(f"{len(series)} series")
    if movies:
        parts.append(f"{len(movies)} {'movie' if len(movies) == 1 else 'movies'}")
    return f"{JOB_LABEL} for {' and '.join(parts)}" if parts else JOB_LABEL


def _pending_job_for(series, movies):
    """A queued, not yet started, recalculation of exactly this scope.

    Only a pending one absorbs a new request: it has not read anything yet, so
    it will see what this save wrote. A running one may already have passed
    the items this save changed, so a second save while it runs queues one
    more pass rather than being folded into it.
    """
    for job in list(jobs_queue.jobs_pending_queue):
        if job.module != JOB_MODULE or job.func != JOB_FUNC or job.status != 'pending':
            continue
        if job.kwargs.get('series') == series and job.kwargs.get('movies') == movies:
            return job.job_id
    return None


def queue_missing_subtitles_recalculation(series=None, movies=None):
    """Queue the recalculation and return its job id without waiting for it.

    With neither argument the whole library is recalculated. Otherwise
    ``series`` and ``movies`` are lists of ``(upstream id, owning instance)``
    pairs, and only those items are.

    Never raises: the save that called this has already been written, and
    failing its response now would report a save that did happen as one that
    did not. The scheduled indexer picks up anything missed.
    """
    try:
        series = _normalise(series)
        movies = _normalise(movies)
        if series is not None and movies is not None and not series and not movies:
            return None
        existing = _pending_job_for(series, movies)
        if existing:
            return existing
        return jobs_queue.feed_jobs_pending_queue(
            job_name=_job_label(series, movies),
            module=JOB_MODULE,
            func=JOB_FUNC,
            # The request token keeps the queue's own duplicate check, which also
            # matches running jobs, from dropping a pass that is still needed.
            kwargs={'series': series, 'movies': movies, 'request': uuid.uuid4().hex},
            is_progress=True,
        ) or None
    except Exception:
        logging.exception("BAZARR could not queue the missing subtitles recalculation")
        return None


def _library_steps():
    steps = []
    if settings.general.use_sonarr:
        from subtitles.indexer.series import list_missing_subtitles
        steps.append(("series", list_missing_subtitles))
    if settings.general.use_radarr:
        from subtitles.indexer.movies import list_missing_subtitles_movies
        steps.append(("movies", list_missing_subtitles_movies))
    # Gated like its two siblings above. Ungated, saving any setting on
    # an install with Sportarr switched off still walked every sports
    # event row, one transaction and one locking select each.
    if settings.general.use_sportarr:
        from subtitles.indexer.sports import list_missing_subtitles_sports
        steps.append(("sports events", list_missing_subtitles_sports))
    return steps


def recalculate_missing_subtitles(series=None, movies=None, request=None, job_id=None):
    from app.job_errors import reason_of
    from app.jobs_queue import JobFailed

    if series is None and movies is None:
        steps = _library_steps()
        jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=max(len(steps), 1))
        for index, (name, recalculate) in enumerate(steps):
            jobs_queue.update_job_progress(job_id=job_id, progress_value=index,
                                           progress_message=f"Recalculating {name}")
            try:
                recalculate()
            except Exception as error:
                raise JobFailed(f"Recalculating missing subtitles for {name} failed: {reason_of(error)}") from error
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='Done')
        return

    from app.database import TableEpisodes, database, select
    from arr_instances.resolution import scoped
    from subtitles.indexer.movies import list_missing_subtitles_movies
    from subtitles.indexer.series import list_missing_subtitles

    series = series or []
    movies = movies or []
    total = len(series) + len(movies)
    jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=max(total, 1))
    done = 0
    for series_id, owner in series:
        try:
            list_missing_subtitles(no=series_id, arr_instance_id=owner)
        except Exception as error:
            raise JobFailed(f"Recalculating missing subtitles for series {series_id} failed: "
                            f"{reason_of(error)}") from error
        event_stream(type='series', payload=series_id)
        episodes = database.execute(
            scoped(select(TableEpisodes.sonarrEpisodeId).where(TableEpisodes.sonarrSeriesId == series_id),
                   TableEpisodes.arr_instance_id, owner)).all()
        for episode in episodes:
            event_stream(type='episode-wanted', payload=episode.sonarrEpisodeId)
        done += 1
        jobs_queue.update_job_progress(job_id=job_id, progress_value=done)
    for radarr_id, owner in movies:
        try:
            list_missing_subtitles_movies(no=radarr_id, arr_instance_id=owner)
        except Exception as error:
            raise JobFailed(f"Recalculating missing subtitles for movie {radarr_id} failed: "
                            f"{reason_of(error)}") from error
        event_stream(type='movie', payload=radarr_id)
        event_stream(type='movie-wanted', payload=radarr_id)
        done += 1
        jobs_queue.update_job_progress(job_id=job_id, progress_value=done)
    event_stream(type='badges')
