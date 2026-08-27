# coding=utf-8

import ast
import logging
import os

from app.config import settings
from app.database import TableEpisodes, TableMovies, TableHistory, TableHistoryMovie, TableShows, database, select
from app.jobs_queue import jobs_queue
from subtitles.sync import sync_subtitles
from subtitles.tools.subsync_engines import is_sync_engine_output
from subtitles.tools.mods import subtitles_apply_mods
from subtitles.indexer.series import series_scan_subtitles
from subtitles.indexer.movies import movies_scan_subtitles
from subtitles.mass_download.series import series_download_subtitles
from subtitles.mass_download.movies import movies_download_subtitles
from subtitles.upgrade import upgrade_episodes_subtitles, upgrade_movies_subtitles
from utilities.path_mappings import path_mappings
from sqlalchemy import or_

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    'sync', 'translate', 'OCR_fixes', 'common', 'remove_HI',
    'remove_tags', 'fix_uppercase', 'reverse_rtl', 'emoji',
    'scan-disk', 'search-missing', 'upgrade',
}

MEDIA_ACTIONS = {'scan-disk', 'search-missing', 'upgrade'}

MOD_ACTIONS = {'OCR_fixes', 'common', 'remove_HI', 'remove_tags', 'fix_uppercase', 'reverse_rtl', 'emoji'}


def _item_display_name(item):
    """What the job progress calls this item.

    An embedded track has no file to name, so the video and the track's
    language are what identify it. Naming the video alone would be ambiguous
    on a container holding several tracks.
    """
    srt_path = item.get('srt_path')
    if srt_path:
        return os.path.basename(srt_path)
    video_path = item.get('video_path') or ''
    language = item.get('srt_lang') or 'embedded'
    return f'{os.path.basename(video_path)} ({language} track)'


def _parse_subtitles_column(subtitles_raw, include_embedded=False):
    """Parse the subtitles TEXT column into a list of (lang_string, path) tuples.

    The indexer records an in-container track as ``[language, None, None]``, so
    an entry without a path is an embedded track rather than a broken row.
    Those are left out by default, because most callers want a file they can
    open. Translation does not: it can extract the track first, which is the
    only way to translate a release whose only subtitles are embedded.
    """
    if not subtitles_raw:
        return []
    try:
        parsed = ast.literal_eval(subtitles_raw)
    except (ValueError, SyntaxError):
        return []

    entries = []
    for entry in parsed:
        if len(entry) < 2:
            continue
        if entry[1]:
            entries.append((entry[0], entry[1]))
        elif include_embedded:
            entries.append((entry[0], None))
    return entries


def _subtitle_variant_key(lang_string):
    """The identity that decides whether two sources collide.

    Base language plus the hi and forced variants, because that triple is what
    ends up in the translated file's name: an ``en:hi`` source writes
    ``.nl.hi.srt`` and a plain ``en`` source writes ``.nl.srt``. Collapsing to
    the base language alone would treat those as the same output and throw one
    of them away. Every modifier is inspected, not just the first, since a
    track can be both (``en:hi:forced``).
    """
    parts = lang_string.split(':')
    modifiers = {part.lower() for part in parts[1:]}
    return parts[0], 'hi' in modifiers, 'forced' in modifiers


def _usable_as_translate_source(lang_string, path):
    """Whether a path-bearing entry can actually be a translate source.

    A generated sync output or a combined artifact is excluded further down the
    collector, and a forced subtitle is never translated. If one of those is
    the only file for a language, it cannot stand in for the embedded track,
    and letting it suppress that track leaves nothing at all.

    The file existing on disk is checked separately, by the caller, because it
    needs the per-instance path mapping applied first.
    """
    _base, _hi, forced = _subtitle_variant_key(lang_string)
    if forced:
        return False
    modifiers = [part.lower() for part in lang_string.split(':')[1:]]
    if any(modifier.startswith('combined-') for modifier in modifiers):
        return False
    return not is_sync_engine_output(path)


def _drop_embedded_duplicates(subtitles, usable):
    """Remove an embedded track when a usable file covers the same variant.

    Both would translate into the same output file, so the two jobs would
    duplicate the work and race each other for the result. The file wins: it is
    already on disk and needs no extraction.

    ``usable`` decides which path-bearing entries count. A file that the
    collector is about to reject anyway, because it is missing, forced, a sync
    output or a combined artifact, must not suppress the embedded track it
    would otherwise stand in for.
    """
    covered = {_subtitle_variant_key(lang) for lang, path in subtitles
               if path and usable(lang, path)}

    kept = []
    for lang, path in subtitles:
        if path:
            kept.append((lang, path))
            continue
        key = _subtitle_variant_key(lang)
        if key in covered:
            continue
        # A container often carries one language twice, and the indexer writes
        # an entry per track without de-duplicating, so the same variant can
        # appear more than once. Every copy selects the same stream and writes
        # the same output, which on a paid translator is billed twice.
        covered.add(key)
        kept.append((lang, path))
    return kept


def _add_instance_filter(mapping, upstream_id, arr_instance_id):
    mapping.setdefault(upstream_id, set()).add(arr_instance_id)


def _instance_filter_matches(row_instance_id, requested_instances):
    if not requested_instances or None in requested_instances:
        return True
    return row_instance_id in requested_instances


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
        options: Dict with force_resync, max_offset_seconds, gss, no_fix_framerate, output_mode.

    Returns:
        Tuple of (items_list, skipped_count).
    """
    options = options or {}
    force_resync = options.get('force_resync', False)
    max_offset = str(options.get('max_offset_seconds', settings.subsync.max_offset_seconds))
    gss = options.get('gss', settings.subsync.gss)
    no_fix_framerate = options.get('no_fix_framerate', settings.subsync.no_fix_framerate)
    output_mode = options.get('output_mode')
    enabled_engines = options.get('enabled_engines')

    # Parse item types
    series_ids = []
    episode_ids = []
    movie_ids = []
    # Per-item owning instance (#156): maps an upstream id to the arr_instance_id
    # values the caller requested, so colliding ids under different instances can
    # be handled in the same batch. None entries (legacy/single-instance) impose
    # no filter -> byte-identical.
    series_instance = {}
    episode_instance = {}
    movie_instance = {}

    if items is None:
        # Entire library mode
        pass
    else:
        for item in items:
            item_type = item.get('type')
            inst = item.get('arr_instance_id')
            if item_type == 'series':
                sid = item.get('sonarrSeriesId')
                if sid is not None:
                    series_ids.append(sid)
                    _add_instance_filter(series_instance, sid, inst)
            elif item_type == 'episode':
                eid = item.get('sonarrEpisodeId')
                if eid is not None:
                    episode_ids.append(eid)
                    _add_instance_filter(episode_instance, eid, inst)
            elif item_type == 'movie':
                rid = item.get('radarrId')
                if rid is not None:
                    movie_ids.append(rid)
                    _add_instance_filter(movie_instance, rid, inst)

    all_items = []
    total_skipped = 0
    target_lang = options.get('to_lang') if action == 'translate' else None
    source_lang = options.get('from_lang') if action == 'translate' else None

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
            output_mode=output_mode,
            enabled_engines=enabled_engines,
            target_lang=target_lang,
            source_lang=source_lang,
            episode_instance=episode_instance,
            series_instance=series_instance,
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
            output_mode=output_mode,
            enabled_engines=enabled_engines,
            target_lang=target_lang,
            source_lang=source_lang,
            movie_instance=movie_instance,
        )
        all_items.extend(mov_items)
        total_skipped += mov_skipped

    return all_items, total_skipped


def _collect_episodes(series_ids=None, episode_ids=None, action='sync',
                      force_resync=False, max_offset='60', gss=True, no_fix_framerate=True,
                      output_mode=None, enabled_engines=None, target_lang=None, source_lang=None,
                      episode_instance=None, series_instance=None):
    """Collect episode subtitles from the database."""
    episode_instance = episode_instance or {}
    series_instance = series_instance or {}
    columns = [
        TableEpisodes.sonarrEpisodeId,
        TableEpisodes.sonarrSeriesId,
        TableEpisodes.arr_instance_id,
        TableEpisodes.path,
        TableEpisodes.subtitles,
    ]
    if action == 'translate':
        # translate_subtitles_file consumes show-level metadata (imdbId, tvdbId,
        # season, episode) via postprocess_subtitles. Other actions do not, so
        # the join to TableShows is scoped to translate to avoid dropping
        # orphaned episodes from sync/mods batches.
        columns.extend([
            TableEpisodes.season,
            TableEpisodes.episode,
            TableShows.imdbId,
            TableShows.tvdbId,
        ])
        query = select(*columns).join(TableShows)
    else:
        query = select(*columns)

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
        # Drop a row whose owner differs from the instance the caller asked for
        # (#156); a requested instance of None imposes no filter (byte-identical).
        req_instances = set()
        if ep.sonarrEpisodeId in episode_instance:
            req_instances.update(episode_instance[ep.sonarrEpisodeId])
        if ep.sonarrSeriesId in series_instance:
            req_instances.update(series_instance[ep.sonarrSeriesId])
        if not _instance_filter_matches(ep.arr_instance_id, req_instances):
            continue

        # Only translation can use an in-container track, and only when the
        # user still has embedded subtitles turned on: the rows outlive the
        # setting until the next index, so the check has to happen here too.
        want_embedded = action == 'translate' and settings.general.use_embedded_subs
        subtitles = _parse_subtitles_column(ep.subtitles, include_embedded=want_embedded)
        # Apply the owning instance's per-instance path_mappings (#156).
        video_path = path_mappings.path_replace_instance(ep.path, ep.arr_instance_id, 'episode')

        if want_embedded:
            def _usable(lang_string, sub_path, _owner=ep):
                if not _usable_as_translate_source(lang_string, sub_path):
                    return False
                return os.path.isfile(path_mappings.path_replace_instance(
                    sub_path, _owner.arr_instance_id, 'episode'))

            subtitles = _drop_embedded_duplicates(subtitles, _usable)

        # For translate: check if target language already exists. Files only:
        # an embedded track counting here would skip an item that has no
        # target-language file at all, which is a regression for runs that
        # never involved an embedded source.
        if action == 'translate' and target_lang:
            existing_langs = {lang_str.split(':')[0] for lang_str, path in subtitles if path}
            if target_lang in existing_langs:
                skipped += 1
                continue

        for lang_string, sub_path in subtitles:
            # Every modifier, not just the first: a track can be both hi and
            # forced. Reading only the first would let a forced one past the
            # guard below and then pick the wrong stream at extraction time.
            sub_lang, sub_hi, sub_forced = _subtitle_variant_key(lang_string)

            # Forced subs can't be synced or translated, but mods are fine
            if sub_forced and action in ('sync', 'translate'):
                skipped += 1
                continue

            # For translate: only queue subtitles matching the requested source language
            if action == 'translate' and source_lang and sub_lang != source_lang:
                skipped += 1
                continue

            # An entry with no path is an in-container track. Only translate can
            # use one, by extracting it first; there is nothing for sync or the
            # mod actions to open.
            is_embedded = not sub_path
            if is_embedded:
                if action != 'translate':
                    skipped += 1
                    continue
                mapped_sub_path = None
            else:
                # Apply per-instance path_mappings to the stored subtitle path (#156).
                mapped_sub_path = path_mappings.path_replace_instance(sub_path, ep.arr_instance_id, 'episode')
                if not os.path.isfile(mapped_sub_path):
                    skipped += 1
                    continue

            # Never use a generated sync output or a combined artifact as a source.
            # For translate they would queue a duplicate job targeting the same
            # output language/file as the real subtitle (e.g. en + en:sync-ffsubsync
            # both translate-from en), causing duplicate work and overwrite races; a
            # sync output also cannot be meaningfully re-synced.
            modifiers = [p.lower() for p in lang_string.split(':')[1:]]
            is_combined = any(m.startswith('combined-') for m in modifiers)
            if action in ('sync', 'translate') and not is_embedded and (
                    is_sync_engine_output(mapped_sub_path) or is_combined):
                skipped += 1
                continue

            if action == 'sync' and not force_resync:
                reversed_path = path_mappings.path_replace_reverse_instance(mapped_sub_path, ep.arr_instance_id, 'episode')
                if reversed_path in synced_paths:
                    skipped += 1
                    continue

            item = {
                'video_path': video_path,
                'srt_path': mapped_sub_path,
                'srt_lang': sub_lang,
                'embedded': is_embedded,
                'forced': sub_forced,
                'hi': sub_hi,
                'sonarr_series_id': ep.sonarrSeriesId,
                'sonarr_episode_id': ep.sonarrEpisodeId,
                'radarr_id': None,
                'arr_instance_id': ep.arr_instance_id,
                'max_offset_seconds': max_offset,
                'no_fix_framerate': no_fix_framerate,
                'gss': gss,
                'output_mode': output_mode,
                'enabled_engines': enabled_engines,
            }
            if action == 'translate':
                item['metadata'] = ep
            items.append(item)

    return items, skipped


def _collect_movies(movie_ids=None, action='sync', force_resync=False,
                    max_offset='60', gss=True, no_fix_framerate=True,
                    output_mode=None, enabled_engines=None, target_lang=None, source_lang=None,
                    movie_instance=None):
    """Collect movie subtitles from the database."""
    movie_instance = movie_instance or {}
    columns = [
        TableMovies.radarrId,
        TableMovies.arr_instance_id,
        TableMovies.path,
        TableMovies.subtitles,
    ]
    if action == 'translate':
        # See _collect_episodes for why metadata columns are translate-only.
        columns.extend([
            TableMovies.imdbId,
            TableMovies.tmdbId,
        ])
    query = select(*columns)

    if movie_ids:
        query = query.where(TableMovies.radarrId.in_(movie_ids))

    movies = database.execute(query).all()

    synced_paths = set()
    if action == 'sync' and not force_resync:
        synced_paths = _get_synced_movie_paths()

    items = []
    skipped = 0

    for movie in movies:
        # Drop a row whose owner differs from the requested instance (#156);
        # a requested instance of None imposes no filter (byte-identical).
        req_instances = movie_instance.get(movie.radarrId)
        if not _instance_filter_matches(movie.arr_instance_id, req_instances):
            continue

        # Only translation can use an in-container track, and only when the
        # user still has embedded subtitles turned on: the rows outlive the
        # setting until the next index, so the check has to happen here too.
        want_embedded = action == 'translate' and settings.general.use_embedded_subs
        subtitles = _parse_subtitles_column(movie.subtitles, include_embedded=want_embedded)
        # Apply the owning instance's per-instance path_mappings (#156).
        video_path = path_mappings.path_replace_instance(movie.path, movie.arr_instance_id, 'movie')

        if want_embedded:
            def _usable(lang_string, sub_path, _owner=movie):
                if not _usable_as_translate_source(lang_string, sub_path):
                    return False
                return os.path.isfile(path_mappings.path_replace_instance(
                    sub_path, _owner.arr_instance_id, 'movie'))

            subtitles = _drop_embedded_duplicates(subtitles, _usable)

        # For translate: check if target language already exists. Files only:
        # an embedded track counting here would skip an item that has no
        # target-language file at all, which is a regression for runs that
        # never involved an embedded source.
        if action == 'translate' and target_lang:
            existing_langs = {lang_str.split(':')[0] for lang_str, path in subtitles if path}
            if target_lang in existing_langs:
                skipped += 1
                continue

        for lang_string, sub_path in subtitles:
            # Every modifier, not just the first: a track can be both hi and
            # forced. Reading only the first would let a forced one past the
            # guard below and then pick the wrong stream at extraction time.
            sub_lang, sub_hi, sub_forced = _subtitle_variant_key(lang_string)

            # Forced subs can't be synced or translated, but mods are fine
            if sub_forced and action in ('sync', 'translate'):
                skipped += 1
                continue

            # For translate: only queue subtitles matching the requested source language
            if action == 'translate' and source_lang and sub_lang != source_lang:
                skipped += 1
                continue

            # An entry with no path is an in-container track. Only translate can
            # use one, by extracting it first; there is nothing for sync or the
            # mod actions to open.
            is_embedded = not sub_path
            if is_embedded:
                if action != 'translate':
                    skipped += 1
                    continue
                mapped_sub_path = None
            else:
                # Apply per-instance path_mappings to the stored subtitle path (#156).
                mapped_sub_path = path_mappings.path_replace_instance(sub_path, movie.arr_instance_id, 'movie')
                if not os.path.isfile(mapped_sub_path):
                    skipped += 1
                    continue

            # Never use a generated sync output or a combined artifact as a source.
            # For translate they would queue a duplicate job targeting the same
            # output language/file as the real subtitle (e.g. en + en:sync-ffsubsync
            # both translate-from en), causing duplicate work and overwrite races; a
            # sync output also cannot be meaningfully re-synced.
            modifiers = [p.lower() for p in lang_string.split(':')[1:]]
            is_combined = any(m.startswith('combined-') for m in modifiers)
            if action in ('sync', 'translate') and not is_embedded and (
                    is_sync_engine_output(mapped_sub_path) or is_combined):
                skipped += 1
                continue

            if action == 'sync' and not force_resync:
                reversed_path = path_mappings.path_replace_reverse_instance(mapped_sub_path, movie.arr_instance_id, 'movie')
                if reversed_path in synced_paths:
                    skipped += 1
                    continue

            item = {
                'video_path': video_path,
                'srt_path': mapped_sub_path,
                'srt_lang': sub_lang,
                'embedded': is_embedded,
                'forced': sub_forced,
                'hi': sub_hi,
                'sonarr_series_id': None,
                'sonarr_episode_id': None,
                'radarr_id': movie.radarrId,
                'arr_instance_id': movie.arr_instance_id,
                'max_offset_seconds': max_offset,
                'no_fix_framerate': no_fix_framerate,
                'gss': gss,
                'output_mode': output_mode,
                'enabled_engines': enabled_engines,
            }
            if action == 'translate':
                item['metadata'] = movie
            items.append(item)

    return items, skipped


def _process_subtitle_item(item, action, options, job_id):
    """Process a single subtitle item based on the action.

    Returns True on success, False on failure.
    """
    if action == 'sync':
        sync_kwargs = {
            'video_path': item['video_path'],
            'srt_path': item['srt_path'],
            'srt_lang': item['srt_lang'],
            'forced': item['forced'],
            'hi': item['hi'],
            'percent_score': 0,
            'sonarr_series_id': item['sonarr_series_id'],
            'sonarr_episode_id': item['sonarr_episode_id'],
            'radarr_id': item['radarr_id'],
            'max_offset_seconds': item['max_offset_seconds'],
            'no_fix_framerate': item['no_fix_framerate'],
            'gss': item['gss'],
            'force_sync': True,
            'job_id': job_id,
            'track_job_progress': False,
            # Thread the per-item owning instance (#156) so the subsync
            # original-language lookup hits the exact owner.
            'arr_instance_id': item.get('arr_instance_id'),
        }
        if item.get('output_mode') is not None:
            sync_kwargs['output_mode'] = item.get('output_mode')
        if item.get('enabled_engines') is not None:
            sync_kwargs['enabled_engines'] = item.get('enabled_engines')
        return sync_subtitles(**sync_kwargs)
    elif action == 'translate':
        from subtitles.tools.translate.main import translate_subtitles_file
        media_type = 'episode' if item['sonarr_series_id'] else 'movies'

        source_srt_file = item['srt_path']
        if item.get('embedded'):
            # Extracted when the item runs rather than when the batch was
            # collected, so a run that is cancelled or fails leaves behind only
            # what it actually reached. A run that completes does extract every
            # track, because every collected item is processed.
            #
            # The owning instance goes along (#156): the collector mapped the
            # video path with that instance's mapping, so the reverse lookup
            # inside has to use the same one or it finds no row at all.
            from subtitles.tools.translate.batch import extract_embedded_subtitle
            source_srt_file = extract_embedded_subtitle(
                item['video_path'], item['srt_lang'], media_type,
                hi=item.get('hi', False), forced=item.get('forced', False),
                arr_instance_id=item.get('arr_instance_id'))
            if not source_srt_file:
                # Usually a bitmap track (PGS, VobSub) that cannot become an
                # SRT. That is this item's problem; the batch carries on.
                logger.warning(
                    'BAZARR could not extract the embedded %s track from %s, skipping translation',
                    item['srt_lang'], item['video_path'])
                return False
        # Don't pass the batch job_id to translate. translate_subtitles_file
        # has its own job/progress lifecycle that would hijack the batch job.
        # Calling without job_id makes it queue as its own separate job.
        translate_subtitles_file(
            video_path=item['video_path'],
            source_srt_file=source_srt_file,
            from_lang=options.get('from_lang', item['srt_lang']),
            to_lang=options.get('to_lang', 'en'),
            forced=item['forced'],
            hi=item['hi'],
            media_type=media_type,
            sonarr_series_id=item['sonarr_series_id'],
            sonarr_episode_id=item['sonarr_episode_id'],
            radarr_id=item['radarr_id'],
            metadata=item['metadata'],
        )
        return True
    elif action in MOD_ACTIONS:
        subtitles_apply_mods(
            item['srt_lang'],
            item['srt_path'],
            [action],
            item['video_path'],
            # Resolve keep-lyrics against the per-item owning instance (#227).
            arr_instance_id=item.get('arr_instance_id'),
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

    if action == 'upgrade':
        sonarr_series_filters = [(i.get('sonarrSeriesId'), i.get('arr_instance_id')) for i in items
                                 if i.get('type') in ('series', 'episode') and i.get('sonarrSeriesId')]
        radarr_filters = [(i.get('radarrId'), i.get('arr_instance_id')) for i in items
                          if i.get('type') == 'movie' and i.get('radarrId')]
        try:
            if sonarr_series_filters:
                upgrade_episodes_subtitles(job_id=job_id, sonarr_series_filters=sonarr_series_filters)
            if radarr_filters:
                upgrade_movies_subtitles(job_id=job_id, radarr_filters=radarr_filters)
            queued = len(sonarr_series_filters) + len(radarr_filters)
        except Exception as e:
            logger.error(f'Error during upgrade: {e}')  # noqa: G004
            errors.append(str(e))
        return {'queued': queued, 'skipped': 0, 'errors': errors}

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
                    if not series_id:
                        skipped += 1
                        continue
                    arr_instance_id = item.get('arr_instance_id')
                    if arr_instance_id is None:
                        series_scan_subtitles(series_id)
                    else:
                        series_scan_subtitles(series_id, arr_instance_id=arr_instance_id)
                elif item_type == 'movie':
                    radarr_id = item.get('radarrId')
                    if not radarr_id:
                        skipped += 1
                        continue
                    arr_instance_id = item.get('arr_instance_id')
                    if arr_instance_id is None:
                        movies_scan_subtitles(radarr_id)
                    else:
                        movies_scan_subtitles(radarr_id, arr_instance_id=arr_instance_id)
                else:
                    skipped += 1
                    continue
            elif action == 'search-missing':
                if item_type in ('series', 'episode'):
                    series_id = item.get('sonarrSeriesId')
                    if not series_id:
                        skipped += 1
                        continue
                    series_download_subtitles(series_id, arr_instance_id=item.get('arr_instance_id'))
                elif item_type == 'movie':
                    radarr_id = item.get('radarrId')
                    if not radarr_id:
                        skipped += 1
                        continue
                    movies_download_subtitles(radarr_id, arr_instance_id=item.get('arr_instance_id'))
                else:
                    skipped += 1
                    continue
            queued += 1
        except Exception as e:
            logger.error(f'Error processing {action} for {item}: {e}')  # noqa: G004
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
        return {'queued': 0, 'skipped': 0, 'errors': [f'Invalid action: {action}']}

    options = options or {}

    # When called without a job_id (e.g. from the scheduler), create one so that
    # downstream functions like sync_subtitles run inline instead of re-queuing
    # themselves as individual jobs.
    if not job_id:
        jobs_queue.add_job_from_function(
            f"Mass {action.replace('_', ' ').replace('-', ' ').title()} "
            f"({'Library' if items is None else f'{len(items)} items'})",
            is_progress=True,
        )
        return

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
            progress_value=i - 1,
            progress_message=f"{action}: {_item_display_name(item)} ({i}/{total_count})"
        )

        try:
            result = _process_subtitle_item(item, action, options, job_id)
            if result:
                processed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f'Error during {action} on {_item_display_name(item)}: {e}')  # noqa: G004
            all_errors.append(str(e))
            failed += 1
        finally:
            jobs_queue.update_job_progress(
                job_id=job_id,
                progress_value=i,
                progress_message=f"{action}: {_item_display_name(item)} ({i}/{total_count})"
            )

    jobs_queue.update_job_name(
        job_id=job_id,
        new_job_name=f"Mass {action} complete: {processed} done, {total_skipped} skipped"
    )
    logger.info(
        f'BAZARR mass {action} complete: {processed} processed, {failed} failed, '  # noqa: G004
        f'{total_skipped} skipped, {len(all_errors)} errors'
    )
    return {'queued': processed, 'skipped': total_skipped + failed, 'errors': all_errors}
