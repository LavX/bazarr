# coding=utf-8

import ast
import logging
import os

from app.config import settings
from app.database import TableEpisodes, TableMovies, TableHistory, TableHistoryMovie, TableShows, database, select
from app.jobs_queue import jobs_queue
from utilities.path_mappings import path_mappings
from utilities.video_analyzer import languages_from_colon_seperated_string

logger = logging.getLogger(__name__)


def _get_synced_episode_paths():
    """Get set of subtitle paths that have been synced (action=5) from episode history."""
    results = database.execute(
        select(TableHistory.subtitles_path)
        .where(TableHistory.action == 5)
    ).all()
    return {r.subtitles_path for r in results if r.subtitles_path}


def _get_synced_movie_paths():
    """Get set of subtitle paths that have been synced (action=5) from movie history."""
    results = database.execute(
        select(TableHistoryMovie.subtitles_path)
        .where(TableHistoryMovie.action == 5)
    ).all()
    return {r.subtitles_path for r in results if r.subtitles_path}


def _parse_subtitles_column(subtitles_raw):
    """Parse the subtitles TEXT column into a list of (lang_string, path) tuples."""
    if not subtitles_raw:
        return []
    try:
        parsed = ast.literal_eval(subtitles_raw)
        return [(entry[0], entry[1]) for entry in parsed if len(entry) >= 2 and entry[1]]
    except (ValueError, SyntaxError):
        return []


def _process_episodes(series_ids=None, episode_ids=None, options=None, force_resync=False):
    """Queue sync jobs for episode subtitles."""
    options = options or {}
    max_offset = str(options.get('max_offset_seconds', settings.subsync.max_offset_seconds))
    gss = options.get('gss', settings.subsync.gss)
    no_fix_framerate = options.get('no_fix_framerate', settings.subsync.no_fix_framerate)

    query = select(
        TableEpisodes.sonarrEpisodeId,
        TableEpisodes.sonarrSeriesId,
        TableEpisodes.path,
        TableEpisodes.subtitles,
    )

    from sqlalchemy import or_
    filters = []
    if episode_ids:
        filters.append(TableEpisodes.sonarrEpisodeId.in_(episode_ids))
    if series_ids:
        filters.append(TableEpisodes.sonarrSeriesId.in_(series_ids))
    if filters:
        query = query.where(or_(*filters))

    episodes = database.execute(query).all()

    synced_paths = set() if force_resync else _get_synced_episode_paths()
    queued = 0
    skipped = 0
    errors = []

    for ep in episodes:
        subtitles = _parse_subtitles_column(ep.subtitles)
        video_path = path_mappings.path_replace(ep.path)

        for lang_string, sub_path in subtitles:
            lang_info = languages_from_colon_seperated_string(lang_string)

            if lang_info['forced']:
                skipped += 1
                continue

            mapped_sub_path = path_mappings.path_replace(sub_path)
            if not os.path.isfile(mapped_sub_path):
                skipped += 1
                continue

            reversed_path = path_mappings.path_replace_reverse(mapped_sub_path)
            if not force_resync and reversed_path in synced_paths:
                skipped += 1
                continue

            try:
                jobs_queue.feed_jobs_pending_queue(
                    job_name=f"Mass Syncing {os.path.basename(mapped_sub_path)}",
                    module='subtitles.sync',
                    func='sync_subtitles',
                    kwargs={
                        'video_path': video_path,
                        'srt_path': mapped_sub_path,
                        'srt_lang': lang_string.split(':')[0],
                        'forced': lang_info['forced'],
                        'hi': lang_info['hi'],
                        'percent_score': 0,
                        'sonarr_series_id': ep.sonarrSeriesId,
                        'sonarr_episode_id': ep.sonarrEpisodeId,
                        'radarr_id': None,
                        'max_offset_seconds': max_offset,
                        'no_fix_framerate': no_fix_framerate,
                        'gss': gss,
                        'force_sync': True,
                    }
                )
                queued += 1
            except Exception as e:
                logger.error(f'Error queuing sync for {sub_path}: {e}')
                errors.append(str(e))
                skipped += 1

    return queued, skipped, errors


def _process_movies(movie_ids=None, options=None, force_resync=False):
    """Queue sync jobs for movie subtitles."""
    options = options or {}
    max_offset = str(options.get('max_offset_seconds', settings.subsync.max_offset_seconds))
    gss = options.get('gss', settings.subsync.gss)
    no_fix_framerate = options.get('no_fix_framerate', settings.subsync.no_fix_framerate)

    query = select(
        TableMovies.radarrId,
        TableMovies.path,
        TableMovies.subtitles,
    )

    if movie_ids:
        query = query.where(TableMovies.radarrId.in_(movie_ids))

    movies = database.execute(query).all()

    synced_paths = set() if force_resync else _get_synced_movie_paths()
    queued = 0
    skipped = 0
    errors = []

    for movie in movies:
        subtitles = _parse_subtitles_column(movie.subtitles)
        video_path = path_mappings.path_replace_movie(movie.path)

        for lang_string, sub_path in subtitles:
            lang_info = languages_from_colon_seperated_string(lang_string)

            if lang_info['forced']:
                skipped += 1
                continue

            mapped_sub_path = path_mappings.path_replace_movie(sub_path)
            if not os.path.isfile(mapped_sub_path):
                skipped += 1
                continue

            reversed_path = path_mappings.path_replace_reverse_movie(mapped_sub_path)
            if not force_resync and reversed_path in synced_paths:
                skipped += 1
                continue

            try:
                jobs_queue.feed_jobs_pending_queue(
                    job_name=f"Mass Syncing {os.path.basename(mapped_sub_path)}",
                    module='subtitles.sync',
                    func='sync_subtitles',
                    kwargs={
                        'video_path': video_path,
                        'srt_path': mapped_sub_path,
                        'srt_lang': lang_string.split(':')[0],
                        'forced': lang_info['forced'],
                        'hi': lang_info['hi'],
                        'percent_score': 0,
                        'sonarr_series_id': None,
                        'sonarr_episode_id': None,
                        'radarr_id': movie.radarrId,
                        'max_offset_seconds': max_offset,
                        'no_fix_framerate': no_fix_framerate,
                        'gss': gss,
                        'force_sync': True,
                    }
                )
                queued += 1
            except Exception as e:
                logger.error(f'Error queuing sync for {sub_path}: {e}')
                errors.append(str(e))
                skipped += 1

    return queued, skipped, errors


def mass_sync_subtitles(items=None, options=None, job_id=None):
    """Main entry point for mass subtitle sync.

    Args:
        items: List of dicts with 'type' and IDs. If None, syncs entire library.
        options: Dict with max_offset_seconds, gss, no_fix_framerate, force_resync.
        job_id: Job ID for scheduled task tracking.
    """
    if not job_id and items is None:
        jobs_queue.add_job_from_function("Mass Syncing All Subtitles", is_progress=False)
        return

    options = options or {}
    force_resync = options.get('force_resync', False)

    total_queued = 0
    total_skipped = 0
    all_errors = []

    if items is None:
        # Sync entire library
        logger.info('BAZARR starting mass sync for all subtitles')
        if settings.general.use_sonarr:
            q, s, e = _process_episodes(options=options, force_resync=force_resync)
            total_queued += q
            total_skipped += s
            all_errors.extend(e)

        if settings.general.use_radarr:
            q, s, e = _process_movies(options=options, force_resync=force_resync)
            total_queued += q
            total_skipped += s
            all_errors.extend(e)
    else:
        # Sync specific items
        series_ids = []
        episode_ids = []
        movie_ids = []

        for item in items:
            item_type = item.get('type')
            if item_type == 'series':
                series_ids.append(item.get('sonarrSeriesId'))
            elif item_type == 'episode':
                episode_ids.append(item.get('sonarrEpisodeId'))
            elif item_type == 'movie':
                movie_ids.append(item.get('radarrId'))

        if series_ids or episode_ids:
            q, s, e = _process_episodes(
                series_ids=series_ids or None,
                episode_ids=episode_ids or None,
                options=options,
                force_resync=force_resync,
            )
            total_queued += q
            total_skipped += s
            all_errors.extend(e)

        if movie_ids:
            q, s, e = _process_movies(
                movie_ids=movie_ids,
                options=options,
                force_resync=force_resync,
            )
            total_queued += q
            total_skipped += s
            all_errors.extend(e)

    logger.info(f'BAZARR mass sync complete: {total_queued} queued, {total_skipped} skipped, {len(all_errors)} errors')
    return {'queued': total_queued, 'skipped': total_skipped, 'errors': all_errors}
