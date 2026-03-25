# coding=utf-8

import ast
import logging
import os

from app.config import settings
from app.database import TableEpisodes, TableMovies, TableHistory, TableHistoryMovie, database, select
from app.jobs_queue import jobs_queue
from subtitles.sync import sync_subtitles
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


def _collect_episode_items(series_ids=None, episode_ids=None, options=None, force_resync=False):
    """Collect episode subtitles to sync (without processing them)."""
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
    items = []
    skipped = 0

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

            items.append({
                'video_path': video_path,
                'srt_path': mapped_sub_path,
                'srt_lang': lang_string.split(':')[0],
                'forced': lang_info['forced'],
                'hi': lang_info['hi'],
                'sonarr_series_id': ep.sonarrSeriesId,
                'sonarr_episode_id': ep.sonarrEpisodeId,
                'radarr_id': None,
                'max_offset_seconds': max_offset,
                'no_fix_framerate': no_fix_framerate,
                'gss': gss,
            })

    return items, skipped


def _collect_movie_items(movie_ids=None, options=None, force_resync=False):
    """Collect movie subtitles to sync (without processing them)."""
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
    items = []
    skipped = 0

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

            items.append({
                'video_path': video_path,
                'srt_path': mapped_sub_path,
                'srt_lang': lang_string.split(':')[0],
                'forced': lang_info['forced'],
                'hi': lang_info['hi'],
                'sonarr_series_id': None,
                'sonarr_episode_id': None,
                'radarr_id': movie.radarrId,
                'max_offset_seconds': max_offset,
                'no_fix_framerate': no_fix_framerate,
                'gss': gss,
            })

    return items, skipped


def mass_sync_subtitles(items=None, options=None, job_id=None):
    """Main entry point for mass subtitle sync.

    Runs as a single job with progress tracking, processing subtitles
    sequentially. This avoids creating thousands of individual jobs
    that would overwhelm the frontend polling system.

    Args:
        items: List of dicts with 'type' and IDs. If None, syncs entire library.
        options: Dict with max_offset_seconds, gss, no_fix_framerate, force_resync.
        job_id: Job ID for scheduled task tracking.
    """
    if not job_id and items is None:
        jobs_queue.add_job_from_function("Mass Syncing All Subtitles", is_progress=True)
        return

    options = options or {}
    force_resync = options.get('force_resync', False)

    # Phase 1: Collect all subtitle items to sync
    all_items = []
    total_skipped = 0

    if items is None:
        logger.info('BAZARR starting mass sync for all subtitles')
        if settings.general.use_sonarr:
            ep_items, ep_skipped = _collect_episode_items(options=options, force_resync=force_resync)
            all_items.extend(ep_items)
            total_skipped += ep_skipped

        if settings.general.use_radarr:
            mov_items, mov_skipped = _collect_movie_items(options=options, force_resync=force_resync)
            all_items.extend(mov_items)
            total_skipped += mov_skipped
    else:
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
            ep_items, ep_skipped = _collect_episode_items(
                series_ids=series_ids or None,
                episode_ids=episode_ids or None,
                options=options,
                force_resync=force_resync,
            )
            all_items.extend(ep_items)
            total_skipped += ep_skipped

        if movie_ids:
            mov_items, mov_skipped = _collect_movie_items(
                movie_ids=movie_ids,
                options=options,
                force_resync=force_resync,
            )
            all_items.extend(mov_items)
            total_skipped += mov_skipped

    # Phase 2: Process items sequentially within this single job
    total_count = len(all_items)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=total_count)

    if total_count == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    synced = 0
    failed = 0
    all_errors = []

    for i, item in enumerate(all_items, start=1):
        jobs_queue.update_job_progress(
            job_id=job_id,
            progress_value=i,
            progress_message=f"Syncing {os.path.basename(item['srt_path'])} ({i}/{total_count})"
        )

        try:
            result = sync_subtitles(
                video_path=item['video_path'],
                srt_path=item['srt_path'],
                srt_lang=item['srt_lang'],
                forced=item['forced'],
                hi=item['hi'],
                percent_score=0,
                sonarr_series_id=item['sonarr_series_id'],
                sonarr_episode_id=item['sonarr_episode_id'],
                radarr_id=item['radarr_id'],
                max_offset_seconds=item['max_offset_seconds'],
                no_fix_framerate=item['no_fix_framerate'],
                gss=item['gss'],
                force_sync=True,
                job_id=job_id,
            )
            if result:
                synced += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f'Error syncing {item["srt_path"]}: {e}')
            all_errors.append(str(e))
            failed += 1

    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Mass sync complete: {synced} synced, {total_skipped} skipped")
    logger.info(f'BAZARR mass sync complete: {synced} synced, {failed} failed, {total_skipped} skipped, {len(all_errors)} errors')
    return {'queued': synced, 'skipped': total_skipped + failed, 'errors': all_errors}
