# coding=utf-8

import ast
import logging
import os

from app.config import settings
from app.database import TableEpisodes, TableMovies, TableHistory, TableHistoryMovie, database, select
from app.jobs_queue import jobs_queue
from subtitles.sync import sync_subtitles
from subtitles.tools.mods import subtitles_apply_mods
from subtitles.indexer.series import series_scan_subtitles
from subtitles.indexer.movies import movies_scan_subtitles
from subtitles.mass_download.series import series_download_subtitles
from subtitles.mass_download.movies import movies_download_subtitles
from subtitles.upgrade import upgrade_episodes_subtitles, upgrade_movies_subtitles
from utilities.path_mappings import path_mappings
from utilities.video_analyzer import languages_from_colon_seperated_string
from sqlalchemy import or_

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    'sync', 'translate', 'OCR_fixes', 'common', 'remove_HI',
    'remove_tags', 'fix_uppercase', 'reverse_rtl', 'scan-disk', 'search-missing', 'upgrade',
}

MEDIA_ACTIONS = {'scan-disk', 'search-missing', 'upgrade'}

MOD_ACTIONS = {'OCR_fixes', 'common', 'remove_HI', 'remove_tags', 'fix_uppercase', 'reverse_rtl'}


def _parse_subtitles_column(subtitles_raw):
    """Parse the subtitles TEXT column into a list of (lang_string, path) tuples."""
    if not subtitles_raw:
        return []
    try:
        parsed = ast.literal_eval(subtitles_raw)
        return [(entry[0], entry[1]) for entry in parsed if len(entry) >= 2 and entry[1]]
    except (ValueError, SyntaxError):
        return []


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


def _collect_subtitle_items(items, action, options):
    """Collect subtitle items from the database for processing.

    Args:
        items: List of dicts with 'type' and IDs, or None to collect entire library.
        action: The action to perform (sync, translate, mod, etc.).
        options: Dict with force_resync, max_offset_seconds, gss, no_fix_framerate.

    Returns:
        Tuple of (items_list, skipped_count).
    """
    options = options or {}
    force_resync = options.get('force_resync', False)
    max_offset = str(options.get('max_offset_seconds', settings.subsync.max_offset_seconds))
    gss = options.get('gss', settings.subsync.gss)
    no_fix_framerate = options.get('no_fix_framerate', settings.subsync.no_fix_framerate)

    # Parse item types
    series_ids = []
    episode_ids = []
    movie_ids = []

    if items is None:
        # Entire library mode
        pass
    else:
        for item in items:
            item_type = item.get('type')
            if item_type == 'series':
                series_ids.append(item.get('sonarrSeriesId'))
            elif item_type == 'episode':
                episode_ids.append(item.get('sonarrEpisodeId'))
            elif item_type == 'movie':
                movie_ids.append(item.get('radarrId'))

    all_items = []
    total_skipped = 0
    target_lang = options.get('to_lang') if action == 'translate' else None

    # Collect episode subtitles
    should_collect_episodes = (items is None and settings.general.use_sonarr) or series_ids or episode_ids
    if should_collect_episodes:
        ep_items, ep_skipped = _collect_episodes(
            series_ids=series_ids or None,
            episode_ids=episode_ids or None,
            action=action,
            force_resync=force_resync,
            max_offset=max_offset,
            gss=gss,
            no_fix_framerate=no_fix_framerate,
            target_lang=target_lang,
        )
        all_items.extend(ep_items)
        total_skipped += ep_skipped

    # Collect movie subtitles
    should_collect_movies = (items is None and settings.general.use_radarr) or movie_ids
    if should_collect_movies:
        mov_items, mov_skipped = _collect_movies(
            movie_ids=movie_ids or None,
            action=action,
            force_resync=force_resync,
            max_offset=max_offset,
            gss=gss,
            no_fix_framerate=no_fix_framerate,
            target_lang=target_lang,
        )
        all_items.extend(mov_items)
        total_skipped += mov_skipped

    return all_items, total_skipped


def _collect_episodes(series_ids=None, episode_ids=None, action='sync',
                      force_resync=False, max_offset='60', gss=True, no_fix_framerate=True,
                      target_lang=None):
    """Collect episode subtitles from the database."""
    query = select(
        TableEpisodes.sonarrEpisodeId,
        TableEpisodes.sonarrSeriesId,
        TableEpisodes.path,
        TableEpisodes.subtitles,
    )

    filters = []
    if episode_ids:
        filters.append(TableEpisodes.sonarrEpisodeId.in_(episode_ids))
    if series_ids:
        filters.append(TableEpisodes.sonarrSeriesId.in_(series_ids))
    if filters:
        query = query.where(or_(*filters))

    episodes = database.execute(query).all()

    synced_paths = set()
    if action == 'sync' and not force_resync:
        synced_paths = _get_synced_episode_paths()

    items = []
    skipped = 0

    for ep in episodes:
        subtitles = _parse_subtitles_column(ep.subtitles)
        video_path = path_mappings.path_replace(ep.path)

        # For translate: check if target language already exists
        if action == 'translate' and target_lang:
            existing_langs = {lang_str.split(':')[0] for lang_str, _ in subtitles}
            if target_lang in existing_langs:
                skipped += 1
                continue

        for lang_string, sub_path in subtitles:
            lang_info = languages_from_colon_seperated_string(lang_string)

            if lang_info['forced']:
                skipped += 1
                continue

            mapped_sub_path = path_mappings.path_replace(sub_path)
            if not os.path.isfile(mapped_sub_path):
                skipped += 1
                continue

            if action == 'sync' and not force_resync:
                reversed_path = path_mappings.path_replace_reverse(mapped_sub_path)
                if reversed_path in synced_paths:
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


def _collect_movies(movie_ids=None, action='sync', force_resync=False,
                    max_offset='60', gss=True, no_fix_framerate=True,
                    target_lang=None):
    """Collect movie subtitles from the database."""
    query = select(
        TableMovies.radarrId,
        TableMovies.path,
        TableMovies.subtitles,
    )

    if movie_ids:
        query = query.where(TableMovies.radarrId.in_(movie_ids))

    movies = database.execute(query).all()

    synced_paths = set()
    if action == 'sync' and not force_resync:
        synced_paths = _get_synced_movie_paths()

    items = []
    skipped = 0

    for movie in movies:
        subtitles = _parse_subtitles_column(movie.subtitles)
        video_path = path_mappings.path_replace_movie(movie.path)

        # For translate: check if target language already exists
        if action == 'translate' and target_lang:
            existing_langs = {lang_str.split(':')[0] for lang_str, _ in subtitles}
            if target_lang in existing_langs:
                skipped += 1
                continue

        for lang_string, sub_path in subtitles:
            lang_info = languages_from_colon_seperated_string(lang_string)

            if lang_info['forced']:
                skipped += 1
                continue

            mapped_sub_path = path_mappings.path_replace_movie(sub_path)
            if not os.path.isfile(mapped_sub_path):
                skipped += 1
                continue

            if action == 'sync' and not force_resync:
                reversed_path = path_mappings.path_replace_reverse_movie(mapped_sub_path)
                if reversed_path in synced_paths:
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


def _process_subtitle_item(item, action, options, job_id):
    """Process a single subtitle item based on the action.

    Returns True on success, False on failure.
    """
    if action == 'sync':
        return sync_subtitles(
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
    elif action == 'translate':
        from subtitles.tools.translate.main import translate_subtitles_file
        media_type = 'series' if item['sonarr_series_id'] else 'movies'
        # Don't pass the batch job_id to translate. translate_subtitles_file
        # has its own job/progress lifecycle that would hijack the batch job.
        # Calling without job_id makes it queue as its own separate job.
        translate_subtitles_file(
            video_path=item['video_path'],
            source_srt_file=item['srt_path'],
            from_lang=options.get('from_lang', item['srt_lang']),
            to_lang=options.get('to_lang', 'en'),
            forced=item['forced'],
            hi=item['hi'],
            media_type=media_type,
            sonarr_series_id=item['sonarr_series_id'],
            sonarr_episode_id=item['sonarr_episode_id'],
            radarr_id=item['radarr_id'],
        )
        return True
    elif action in MOD_ACTIONS:
        subtitles_apply_mods(
            item['srt_lang'],
            item['srt_path'],
            [action],
            item['video_path'],
        )
        return True
    return False


def _process_media_action(items, action, job_id):
    """Handle scan-disk, search-missing, and upgrade actions for series/movies.

    Args:
        items: List of dicts with 'type' and IDs.
        action: 'scan-disk', 'search-missing', or 'upgrade'.
        job_id: Job ID for progress tracking.

    Returns:
        Dict with queued, skipped, errors.
    """
    queued = 0
    skipped = 0
    errors = []

    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(items))

    for i, item in enumerate(items, start=1):
        item_type = item.get('type')
        jobs_queue.update_job_progress(
            job_id=job_id,
            progress_value=i,
            progress_message=f"Processing {item_type} ({i}/{len(items)})"
        )

        try:
            if action == 'scan-disk':
                if item_type in ('series', 'episode'):
                    series_id = item.get('sonarrSeriesId')
                    series_scan_subtitles(series_id)
                elif item_type == 'movie':
                    radarr_id = item.get('radarrId')
                    movies_scan_subtitles(radarr_id)
                else:
                    skipped += 1
                    continue
            elif action == 'search-missing':
                if item_type in ('series', 'episode'):
                    series_id = item.get('sonarrSeriesId')
                    series_download_subtitles(series_id)
                elif item_type == 'movie':
                    radarr_id = item.get('radarrId')
                    movies_download_subtitles(radarr_id)
                else:
                    skipped += 1
                    continue
            elif action == 'upgrade':
                if item_type in ('series', 'episode'):
                    upgrade_episodes_subtitles(job_id=job_id)
                elif item_type == 'movie':
                    upgrade_movies_subtitles(job_id=job_id)
                else:
                    skipped += 1
                    continue
            queued += 1
        except Exception as e:
            logger.error(f'Error processing {action} for {item}: {e}')
            errors.append(str(e))

    return {'queued': queued, 'skipped': skipped, 'errors': errors}


def mass_batch_operation(items=None, action='sync', options=None, job_id=None):
    """Main entry point for all batch operations on subtitles.

    Handles sync, translate, subtitle mods, scan-disk, and search-missing
    in a unified interface. Runs as a single job with progress tracking,
    processing items sequentially.

    Args:
        items: List of dicts with 'type' and IDs. If None, processes entire library.
        action: One of VALID_ACTIONS.
        options: Dict with action-specific options (force_resync, from_lang, to_lang, etc.).
        job_id: Job ID for scheduled task tracking.

    Returns:
        Dict with queued, skipped, errors. Or None if scheduling a job.
    """
    if action not in VALID_ACTIONS:
        return {'error': f'Invalid action: {action}'}

    options = options or {}

    # Media actions (scan-disk, search-missing) work on media items directly
    if action in MEDIA_ACTIONS:
        if not items:
            return {'queued': 0, 'skipped': 0, 'errors': []}
        return _process_media_action(items, action, job_id)

    # Subtitle actions: collect subtitle files, then process them
    if items is not None and len(items) == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_max=0)
        return {'queued': 0, 'skipped': 0, 'errors': []}

    all_items, total_skipped = _collect_subtitle_items(items, action, options)

    # Process items sequentially within this single job
    total_count = len(all_items)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=total_count)

    if total_count == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    processed = 0
    failed = 0
    all_errors = []

    for i, item in enumerate(all_items, start=1):
        jobs_queue.update_job_progress(
            job_id=job_id,
            progress_value=i,
            progress_message=f"{action}: {os.path.basename(item['srt_path'])} ({i}/{total_count})"
        )

        try:
            result = _process_subtitle_item(item, action, options, job_id)
            if result:
                processed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f'Error during {action} on {item["srt_path"]}: {e}')
            all_errors.append(str(e))
            failed += 1

    jobs_queue.update_job_name(
        job_id=job_id,
        new_job_name=f"Mass {action} complete: {processed} done, {total_skipped} skipped"
    )
    logger.info(
        f'BAZARR mass {action} complete: {processed} processed, {failed} failed, '
        f'{total_skipped} skipped, {len(all_errors)} errors'
    )
    return {'queued': processed, 'skipped': total_skipped + failed, 'errors': all_errors}
