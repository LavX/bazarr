# coding=utf-8

import ast
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.event_handler import event_stream
from app.database import (TableArrInstances, TableEpisodes, TableLanguagesProfiles, TableMovies, TableHistory,
                          TableHistoryMovie, TableHistorySports, TableShows, TableSportsEvents, TableSportsLeagues,
                          database, get_audio_profile_languages, select)
from app.jobs_queue import JobCancelled, JobFailed, jobs_queue
from subtitles.sync import SYNC_SKIPPED, sync_subtitles
from subtitles.tools.subsync_engines import is_sync_engine_output
from subtitles.tools.mods import subtitles_apply_mods
from subtitles.indexer.series import series_scan_subtitles
from subtitles.indexer.movies import movies_scan_subtitles
from subtitles.mass_download.series import series_download_subtitles
from subtitles.mass_download.movies import movies_download_subtitles
from subtitles.upgrade import upgrade_episodes_subtitles, upgrade_movies_subtitles
from sportarr.workflows import upgrade_sports_subtitles
from utilities.path_mappings import path_mappings
from sqlalchemy import or_
from subtitles.job_errors import describe_failures
from subtitles.indexer.entries import entry_codec

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    'sync', 'translate', 'OCR_fixes', 'common', 'remove_HI',
    'remove_tags', 'fix_uppercase', 'reverse_rtl', 'emoji',
    'scan-disk', 'search-missing', 'upgrade', 'extract', 'ocr',
}

MEDIA_ACTIONS = {'scan-disk', 'search-missing', 'upgrade'}

MOD_ACTIONS = {'OCR_fixes', 'common', 'remove_HI', 'remove_tags', 'fix_uppercase', 'reverse_rtl', 'emoji'}

# Returned by _process_subtitle_item for an item that was deliberately left
# alone, so it counts as skipped rather than failed.
ITEM_SKIPPED = object()
_SOURCE_SRT_FILE_UNSET = object()

# Why an item that returned no result failed, for the job's failure reason.
_NO_RESULT_REASONS = {
    'sync': 'no sync engine produced output',
    'translate': 'the embedded track could not be extracted',
}


def _media_item_display_name(item):
    """What a failed media action item is called in the job's failure reason."""
    item_type = item.get('type') or 'item'
    for key in ('sonarrEpisodeId', 'sonarrSeriesId', 'radarrId', 'sportsEventId', 'sportsLeagueId'):
        if item.get(key):
            return f'{item_type} {item[key]}'
    return item_type


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


def _parse_subtitles_column(subtitles_raw, include_embedded=False, with_codec=False):
    """Parse the subtitles TEXT column into a list of (lang_string, path) tuples.

    The indexer records an in-container track with no path (``[language, None,
    None, codec]``, three elements on rows indexed before the codec was stored),
    so an entry without a path is an embedded track rather than a broken row.
    Those are left out by default, because most callers want a file they can
    open. Translation does not: it can extract the track first, which is the
    only way to translate a release whose only subtitles are embedded.

    ``with_codec`` adds a third element: the embedded track's codec, or None
    (always None for a file).
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
            entries.append((entry[0], entry[1], None) if with_codec else (entry[0], entry[1]))
        elif include_embedded:
            entries.append((entry[0], None, entry_codec(entry)) if with_codec else (entry[0], None))
    return entries


def _extract_sources(subtitles_raw):
    """The embedded tracks a mass extract writes out, and how many it leaves.

    Read from the stored listing, which never holds a track the user hid, so a
    hidden track is never extracted. A track whose language key already has a
    subtitle file is left alone: the language is covered, and an extraction
    would find its name taken. Returns ``([(language key, codec)], skipped)``.
    """
    entries = _parse_subtitles_column(subtitles_raw, include_embedded=True, with_codec=True)
    files = {_subtitle_variant_key(lang) for lang, path, _codec in entries if path}
    sources, skipped = [], 0
    for lang, path, codec in entries:
        if path:
            continue
        if _subtitle_variant_key(lang) in files:
            skipped += 1
            continue
        sources.append((lang, codec))
    return sources, skipped


def _extract_items(subtitles_raw, video_path, ids):
    """Mass extract items for one media row; ``ids`` are the item's id fields."""
    sources, skipped = _extract_sources(subtitles_raw)
    items = []
    for lang, codec in sources:
        base, hi, forced = _subtitle_variant_key(lang)
        items.append({'video_path': video_path, 'srt_path': None, 'srt_lang': base, 'embedded': True,
                      'hi': hi, 'forced': forced, 'codec': codec, 'sonarr_series_id': None,
                      'sonarr_episode_id': None, 'radarr_id': None, **ids})
    return items, skipped


def _profile_wants():
    """Language profile id -> the subtitles it wants, as ``(language, hi, forced)`` keys.

    One key per profile item, the way the wanted list names it: forced wins
    over HI, and an item that is neither wants plain subtitles. The item's
    audio rules go along, so the collectors can apply them like the wanted
    list does: ``audio_exclude`` drops the item when the title's audio matches
    it, ``audio_only_include`` when it does not.
    """
    wants = {}
    for profile_id, items in database.execute(
            select(TableLanguagesProfiles.profileId, TableLanguagesProfiles.items)).all():
        try:
            wants[profile_id] = [
                (item['language'],
                 item.get('hi') == 'True' and item.get('forced') != 'True',
                 item.get('forced') == 'True',
                 item.get('audio_exclude') == 'True',
                 item.get('audio_only_include') == 'True')
                for item in json.loads(items or '[]')]
        except (ValueError, TypeError, KeyError, AttributeError):
            wants[profile_id] = []
    return wants


def _audio_codes(audio_language):
    """The title's audio languages as alpha-2 codes, for a profile item's audio rules."""
    if not audio_language:
        return set()
    return {language['code2'] for language in get_audio_profile_languages(audio_language)
            if language.get('code2')}


def _wanted_for_audio(wanted, audio):
    """The wants the profile's audio rules leave for a title with ``audio``.

    ``wanted`` items are the shape ``_profile_wants()`` returns, or bare
    ``(language, hi, forced)`` keys (which carry no audio restriction).
    """
    allowed = []
    for want in wanted:
        language, hi, forced = want[:3]
        audio_exclude = len(want) > 3 and want[3]
        audio_only_include = len(want) > 4 and want[4]
        matches = language in audio
        if audio_exclude and matches:
            continue
        if audio_only_include and not matches:
            continue
        allowed.append((language, hi, forced))
    return allowed


def _ocr_sources(subtitles_raw, wanted, audio=()):
    """The image tracks a mass OCR reads, and how many wanted subtitles it leaves.

    Read from the stored listing, which never holds a track the user hid. For
    each subtitle the title's profile wants (``wanted``, ``_profile_wants()``
    keys, filtered through the item's audio rules against ``audio``), one PGS
    or VobSub track of exactly that variant is read, unless the variant has a
    subtitle file or an embedded text track already (Extract to file gives
    that text in seconds). A combined subtitle or a generated sync output is
    not that subtitle and covers nothing. As for wanted subtitles, an HI subtitle also fills
    a want for plain ones: a plain want is covered by either, and reads the HI
    image track when the title has no plain one. A DVB track or one of unknown
    format is never read. Returns ``([(language key, codec)], skipped)``, the
    tracks in listing order.
    """
    from subtitles.tools.embedded_streams import is_bitmap_codec, is_ocr_codec

    entries = _parse_subtitles_column(subtitles_raw, include_embedded=True, with_codec=True)
    covered = {_subtitle_variant_key(lang) for lang, path, codec in entries
               if (path and not _is_generated_artifact(lang, path))
               or (codec is not None and not is_bitmap_codec(codec))}
    images = {}
    for lang, path, codec in entries:
        if not path and is_ocr_codec(codec):
            images.setdefault(_subtitle_variant_key(lang), (lang, codec))
    chosen, skipped = set(), 0
    for language, hi, forced in _wanted_for_audio(wanted, audio):
        keys = [(language, hi, forced)] + ([(language, True, False)] if not hi and not forced else [])
        readable = [key for key in keys if key in images]
        if not readable:
            continue
        if any(key in covered for key in keys):
            skipped += 1
            continue
        chosen.add(readable[0])
    return [source for key, source in images.items() if key in chosen], skipped


def _ocr_items(subtitles_raw, video_path, ids, wanted, audio=()):
    """Mass OCR items for one media row; ``ids`` are the item's id fields."""
    sources, skipped = _ocr_sources(subtitles_raw, wanted, audio=audio)
    items = []
    for lang, codec in sources:
        base, hi, forced = _subtitle_variant_key(lang)
        items.append({'video_path': video_path, 'srt_path': None, 'srt_lang': base, 'embedded': True,
                      'hi': hi, 'forced': forced, 'codec': codec, 'sonarr_series_id': None,
                      'sonarr_episode_id': None, 'radarr_id': None, **ids})
    return items, skipped


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


def _is_generated_artifact(lang_string, path):
    """Whether a path-bearing entry is a combined subtitle or a sync engine output.

    Neither is a subtitle the user asked for, so neither can stand in for an
    embedded track: a source choice or a coverage check must not count them.
    """
    modifiers = [part.lower() for part in lang_string.split(':')[1:]]
    if any(modifier.startswith('combined-') for modifier in modifiers):
        return True
    return is_sync_engine_output(path)


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
    return not _is_generated_artifact(lang_string, path)


def _translate_output_present(subtitles, target_lang, hi, usable):
    """True when the file this one source would produce is already on disk.

    Asked per source, not per item. An item with an ``en`` and an ``en:hi``
    source and an existing ``nl`` file is not fully satisfied, because ``nl:hi``
    is missing, but deciding that at the item level queues both sources and the
    plain one then overwrites the ``nl`` file that was already there. On a paid
    translator that is billed work for a file the user already had.

    Translation carries the source variant through and never produces a forced
    subtitle, so the comparison is on base language plus the hearing-impaired
    flag. ``usable`` rejects a row naming a file that is gone, and generated
    sync or combined artifacts, which are not subtitles the user asked for.
    """
    for lang_string, path in subtitles:
        if not path:
            continue
        base, existing_hi, forced = _subtitle_variant_key(lang_string)
        if forced or base != target_lang or existing_hi != hi:
            continue
        if usable(lang_string, path):
            return True
    return False


def _translate_item_satisfied(subtitles, target_lang, source_lang, usable):
    """True when every source on this item already has its output on disk.

    A fast path only. It exists so a fully covered item still reports as one
    skipped item rather than one per subtitle entry, which is what the counter
    meant before. The per-source check in the loop is what handles the partial
    case, where some outputs exist and some do not.
    """
    wanted = []
    for lang_string, path in subtitles:
        base, hi, forced = _subtitle_variant_key(lang_string)
        if forced:
            continue
        if source_lang and base != source_lang:
            continue
        if path and not usable(lang_string, path):
            continue
        wanted.append(hi)

    if not wanted:
        return False
    return all(_translate_output_present(subtitles, target_lang, hi, usable)
               for hi in wanted)


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


def _get_synced_sports_paths():
    """Get set of subtitle paths that have been synced (action=5) from sports history."""
    results = database.execute(
        select(TableHistorySports.arr_instance_id, TableHistorySports.subtitles_path)
        .where(TableHistorySports.action == 5)
    ).all()
    return {(r.arr_instance_id, r.subtitles_path) for r in results if r.subtitles_path}


def _sports_event_ids_for_leagues(league_ids, league_instance):
    """Expand selected leagues into their event ids, keeping each event's owner.

    A league selection is the sports equivalent of selecting a series, and the
    collector below works on events the way _collect_episodes works on episodes.
    """
    if not league_ids:
        return {}
    rows = database.execute(
        select(TableSportsEvents.id, TableSportsEvents.league_id,
               TableSportsEvents.arr_instance_id)
        .join(TableArrInstances, TableSportsEvents.arr_instance_id == TableArrInstances.id)
        .where(TableSportsEvents.league_id.in_(league_ids),
               TableArrInstances.kind == 'sportarr', TableArrInstances.enabled == 1)
    ).all()
    expanded = {}
    for row in rows:
        if not _instance_filter_matches(row.arr_instance_id,
                                        league_instance.get(row.league_id)):
            continue
        expanded.setdefault(row.id, set()).add(row.arr_instance_id)
    return expanded


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
    # 'sports' names an event, the way it does everywhere else in the sports
    # code, and 'sportsLeague' names a league the way 'series' names a show.
    sports_ids = []
    sports_league_ids = []
    sports_instance = {}
    sports_league_instance = {}

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
            elif item_type == 'sports':
                eid = item.get('sportsEventId')
                if eid is not None:
                    sports_ids.append(eid)
                    _add_instance_filter(sports_instance, eid, inst)
            elif item_type == 'sportsLeague':
                lid = item.get('sportsLeagueId')
                if lid is not None:
                    sports_league_ids.append(lid)
                    _add_instance_filter(sports_league_instance, lid, inst)

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

    # Collect sports subtitles
    for event_id, owners in _sports_event_ids_for_leagues(
            sports_league_ids, sports_league_instance).items():
        sports_ids.append(event_id)
        sports_instance.setdefault(event_id, set()).update(owners)

    should_collect_sports = (items is None and settings.general.use_sportarr) or sports_ids
    if should_collect_sports:
        sports_items, sports_skipped = _collect_sports(
            event_ids=sports_ids or None,
            action=action,
            force_resync=force_resync,
            max_offset=max_offset,
            gss=gss,
            no_fix_framerate=no_fix_framerate,
            output_mode=output_mode,
            enabled_engines=enabled_engines,
            target_lang=target_lang,
            source_lang=source_lang,
            sports_instance=sports_instance,
        )
        all_items.extend(sports_items)
        total_skipped += sports_skipped

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
    elif action == 'ocr':
        # The series' language profile says which languages to read, and the
        # episode's audio languages which of its items the audio rules leave.
        # Outer join, so an episode whose show row is missing is still
        # collected (and, wanting nothing, read for nothing).
        columns.extend([TableShows.profileId, TableEpisodes.audio_language])
        query = select(*columns).join(
            TableShows, TableEpisodes.series_id == TableShows.id, isouter=True)
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
    wanted = _profile_wants() if action == 'ocr' else {}

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

        if action == 'extract':
            extract_items, extract_skipped = _extract_items(ep.subtitles, video_path, {
                'sonarr_series_id': ep.sonarrSeriesId, 'sonarr_episode_id': ep.sonarrEpisodeId,
                'arr_instance_id': ep.arr_instance_id})
            items.extend(extract_items)
            skipped += extract_skipped
            continue

        if action == 'ocr':
            ocr_items, ocr_skipped = _ocr_items(
                ep.subtitles, video_path,
                {'sonarr_series_id': ep.sonarrSeriesId, 'sonarr_episode_id': ep.sonarrEpisodeId,
                 'arr_instance_id': ep.arr_instance_id},
                wanted.get(ep.profileId, []), audio=_audio_codes(ep.audio_language))
            items.extend(ocr_items)
            skipped += ocr_skipped
            continue

        if want_embedded:
            def _usable(lang_string, sub_path, _owner=ep):
                if not _usable_as_translate_source(lang_string, sub_path):
                    return False
                return os.path.isfile(path_mappings.path_replace_instance(
                    sub_path, _owner.arr_instance_id, 'episode'))

            subtitles = _drop_embedded_duplicates(subtitles, _usable)

        # For translate: skip only when every subtitle this run would write is
        # already there. Compared per variant, not per base language, because
        # translation carries the source variant through: a forced file in the
        # target language is a different subtitle, and so is the plain one when
        # the source is hearing-impaired. A row naming a file that is gone, or
        # naming a sync or combined artifact, does not count either.
        def _target_usable(lang_string, sub_path, _owner=ep):
            mapped = path_mappings.path_replace_instance(
                sub_path, _owner.arr_instance_id, 'episode')
            if not os.path.isfile(mapped):
                return False
            modifiers = [part.lower() for part in lang_string.split(':')[1:]]
            if any(m.startswith('combined-') for m in modifiers):
                return False
            return not is_sync_engine_output(mapped)

        if action == 'translate' and target_lang and _translate_item_satisfied(
                subtitles, target_lang, source_lang, _target_usable):
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

            # And only when the file this source would produce is not already
            # there. Per source, because two sources of the same language can
            # produce different outputs: en gives nl, en:hi gives nl:hi.
            if action == 'translate' and target_lang and _translate_output_present(
                    subtitles, target_lang, sub_hi, _target_usable):
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
    elif action == 'ocr':
        # The movie's language profile says which languages to read, and its
        # audio languages which of its items the audio rules leave.
        columns.extend([TableMovies.profileId, TableMovies.audio_language])
    query = select(*columns)

    if movie_ids:
        query = query.where(TableMovies.radarrId.in_(movie_ids))

    movies = database.execute(query).all()
    wanted = _profile_wants() if action == 'ocr' else {}

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

        if action == 'extract':
            extract_items, extract_skipped = _extract_items(movie.subtitles, video_path, {
                'radarr_id': movie.radarrId, 'arr_instance_id': movie.arr_instance_id})
            items.extend(extract_items)
            skipped += extract_skipped
            continue

        if action == 'ocr':
            ocr_items, ocr_skipped = _ocr_items(
                movie.subtitles, video_path,
                {'radarr_id': movie.radarrId, 'arr_instance_id': movie.arr_instance_id},
                wanted.get(movie.profileId, []), audio=_audio_codes(movie.audio_language))
            items.extend(ocr_items)
            skipped += ocr_skipped
            continue

        if want_embedded:
            def _usable(lang_string, sub_path, _owner=movie):
                if not _usable_as_translate_source(lang_string, sub_path):
                    return False
                return os.path.isfile(path_mappings.path_replace_instance(
                    sub_path, _owner.arr_instance_id, 'movie'))

            subtitles = _drop_embedded_duplicates(subtitles, _usable)

        # For translate: skip only when every subtitle this run would write is
        # already there. Compared per variant, not per base language, because
        # translation carries the source variant through: a forced file in the
        # target language is a different subtitle, and so is the plain one when
        # the source is hearing-impaired. A row naming a file that is gone, or
        # naming a sync or combined artifact, does not count either.
        def _target_usable(lang_string, sub_path, _owner=movie):
            mapped = path_mappings.path_replace_instance(
                sub_path, _owner.arr_instance_id, 'movie')
            if not os.path.isfile(mapped):
                return False
            modifiers = [part.lower() for part in lang_string.split(':')[1:]]
            if any(m.startswith('combined-') for m in modifiers):
                return False
            return not is_sync_engine_output(mapped)

        if action == 'translate' and target_lang and _translate_item_satisfied(
                subtitles, target_lang, source_lang, _target_usable):
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

            # And only when the file this source would produce is not already
            # there. Per source, because two sources of the same language can
            # produce different outputs: en gives nl, en:hi gives nl:hi.
            if action == 'translate' and target_lang and _translate_output_present(
                    subtitles, target_lang, sub_hi, _target_usable):
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


def _collect_sports(event_ids=None, action='sync', force_resync=False,
                    max_offset='60', gss=True, no_fix_framerate=True,
                    output_mode=None, enabled_engines=None, target_lang=None, source_lang=None,
                    sports_instance=None):
    """Collect sports event subtitles from the database.

    Mirrors _collect_movies. The differences are all consequences of ownership:
    an event is addressed by its local id rather than an upstream one, its paths
    resolve through the owning instance's own mapping with no global fallback,
    and the item carries that owner so the processor can build the publication
    guard a sports write needs.

    Embedded-track branch: only translation can use an in-container track, and
    only when the user still has embedded subtitles turned on. The extraction
    helper has its own sports arm, so the item runs exactly like an episodes one
    would: it is extracted when the item runs, not when the batch was collected.
    """
    sports_instance = sports_instance or {}
    # Joined to the owner and filtered to enabled Sportarr instances. A sports
    # path mapping has no global fallback: resolving one against a disabled
    # owner raises, and in library mode that exception would come out of the
    # collector and take the whole scheduled mass sync with it.
    query = (
        select(
            TableSportsEvents.id,
            TableSportsEvents.arr_instance_id,
            TableSportsEvents.path,
            TableSportsEvents.subtitles,
        )
        .join(TableArrInstances, TableSportsEvents.arr_instance_id == TableArrInstances.id)
        .where(TableArrInstances.kind == 'sportarr', TableArrInstances.enabled == 1)
    )
    if action == 'ocr':
        # The league's language profile says which languages to read.
        query = query.add_columns(TableSportsLeagues.profileId, TableSportsEvents.audio_language).join(
            TableSportsLeagues, (TableSportsEvents.league_id == TableSportsLeagues.id)
            & (TableSportsEvents.arr_instance_id == TableSportsLeagues.arr_instance_id), isouter=True)
    if event_ids:
        query = query.where(TableSportsEvents.id.in_(event_ids))

    events = database.execute(query).all()
    wanted = _profile_wants() if action == 'ocr' else {}

    synced_paths = set()
    if action == 'sync' and not force_resync:
        synced_paths = _get_synced_sports_paths()

    items = []
    skipped = 0

    for event in events:
        req_instances = sports_instance.get(event.id)
        if not _instance_filter_matches(event.arr_instance_id, req_instances):
            continue

        # Only translation can use an in-container track, and only when the
        # user still has embedded subtitles turned on: the rows outlive the
        # setting until the next index, so the check has to happen here too.
        want_embedded = action == 'translate' and settings.general.use_embedded_subs
        subtitles = _parse_subtitles_column(event.subtitles, include_embedded=want_embedded)
        video_path = path_mappings.path_replace_instance(
            event.path, event.arr_instance_id, 'sports')

        if action == 'extract':
            extract_items, extract_skipped = _extract_items(event.subtitles, video_path, {
                'sports_event_id': event.id, 'arr_instance_id': event.arr_instance_id})
            items.extend(extract_items)
            skipped += extract_skipped
            continue

        if action == 'ocr':
            ocr_items, ocr_skipped = _ocr_items(
                event.subtitles, video_path,
                {'sports_event_id': event.id, 'arr_instance_id': event.arr_instance_id},
                wanted.get(event.profileId, []), audio=_audio_codes(event.audio_language))
            items.extend(ocr_items)
            skipped += ocr_skipped
            continue

        if want_embedded:
            def _usable(lang_string, sub_path, _owner=event):
                if not _usable_as_translate_source(lang_string, sub_path):
                    return False
                return os.path.isfile(path_mappings.path_replace_instance(
                    sub_path, _owner.arr_instance_id, 'sports'))

            subtitles = _drop_embedded_duplicates(subtitles, _usable)

        def _target_usable(lang_string, sub_path, _owner=event):
            mapped = path_mappings.path_replace_instance(
                sub_path, _owner.arr_instance_id, 'sports')
            if not os.path.isfile(mapped):
                return False
            modifiers = [part.lower() for part in lang_string.split(':')[1:]]
            if any(m.startswith('combined-') for m in modifiers):
                return False
            return not is_sync_engine_output(mapped)

        if action == 'translate' and target_lang and _translate_item_satisfied(
                subtitles, target_lang, source_lang, _target_usable):
            skipped += 1
            continue

        for lang_string, sub_path in subtitles:
            sub_lang, sub_hi, sub_forced = _subtitle_variant_key(lang_string)

            if sub_forced and action in ('sync', 'translate'):
                skipped += 1
                continue

            if action == 'translate' and source_lang and sub_lang != source_lang:
                skipped += 1
                continue

            if action == 'translate' and target_lang and _translate_output_present(
                    subtitles, target_lang, sub_hi, _target_usable):
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
                mapped_sub_path = path_mappings.path_replace_instance(
                    sub_path, event.arr_instance_id, 'sports')
                if not os.path.isfile(mapped_sub_path):
                    skipped += 1
                    continue

            modifiers = [p.lower() for p in lang_string.split(':')[1:]]
            is_combined = any(m.startswith('combined-') for m in modifiers)
            if action in ('sync', 'translate') and not is_embedded and (
                    is_sync_engine_output(mapped_sub_path) or is_combined):
                skipped += 1
                continue

            if action == 'sync' and not force_resync:
                reversed_path = path_mappings.path_replace_reverse_instance(
                    mapped_sub_path, event.arr_instance_id, 'sports')
                if (event.arr_instance_id, reversed_path) in synced_paths:
                    skipped += 1
                    continue

            items.append({
                'video_path': video_path,
                'srt_path': mapped_sub_path,
                'srt_lang': sub_lang,
                'embedded': is_embedded,
                'forced': sub_forced,
                'hi': sub_hi,
                'sonarr_series_id': None,
                'sonarr_episode_id': None,
                'radarr_id': None,
                'sports_event_id': event.id,
                'arr_instance_id': event.arr_instance_id,
                'max_offset_seconds': max_offset,
                'no_fix_framerate': no_fix_framerate,
                'gss': gss,
                'output_mode': output_mode,
                'enabled_engines': enabled_engines,
                # A sports translation carries no native media metadata, and
                # translate_subtitles_file refuses one that does.
                'metadata': None,
            })

    return items, skipped


def _item_outcome(sync_result):
    """A sync left alone on purpose is a skipped item, not a failed one."""
    return ITEM_SKIPPED if sync_result is SYNC_SKIPPED else sync_result


def _extract_translate_source(item):
    """Extract one translation source with its owning instance and track flags."""
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    if item.get('sports_event_id'):
        media_type = 'sports'
    else:
        media_type = 'episode' if item.get('sonarr_series_id') else 'movies'
    source_srt_file = extract_embedded_subtitle(
        item['video_path'], item['srt_lang'], media_type,
        hi=item.get('hi', False), forced=item.get('forced', False),
        arr_instance_id=item.get('arr_instance_id'))
    if not source_srt_file:
        logger.warning(
            'BAZARR could not extract the embedded %s track from %s, skipping translation',
            item['srt_lang'], item['video_path'])
    return source_srt_file


def _extract_translate_source_in_pool(item):
    """Run one extraction on a pool thread, then release that thread's database session."""
    try:
        return _extract_translate_source(item)
    finally:
        database.remove()


def _process_subtitle_item(item, action, options, job_id, children=None, *,
                           source_srt_file=_SOURCE_SRT_FILE_UNSET):
    """Process a single subtitle item based on the action.

    Returns True on success, False on failure, ITEM_SKIPPED for an item that
    was deliberately left alone.

    ``children``, for a translate batch, is the list the id of the translation
    job queued for this item is appended to, so the batch can publish them.
    ``source_srt_file`` supplies an extraction result, including None when
    extraction failed. Without it, an embedded source is extracted inline.
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
        sports_event_id = item.get('sports_event_id')
        if sports_event_id is None:
            return _item_outcome(sync_subtitles(**sync_kwargs))
        # Sports publishes under an owned boundary pinning the file signature,
        # so a batch item collected before a resync replaced the recording
        # cannot write over the new file. The other two media types pass None
        # and keep their behaviour untouched.
        from sportarr.subtitles import sports_manual_operation
        with sports_manual_operation(
                sports_event_id, item.get('arr_instance_id')) as (
                    sports_context, sports_validate, sports_guard, sports_path):
            sync_kwargs['video_path'] = sports_path
            sync_kwargs['context'] = sports_context
            sync_kwargs['validate'] = sports_validate
            sync_kwargs['publication_guard'] = sports_guard
            return _item_outcome(sync_subtitles(**sync_kwargs))
    elif action == 'translate':
        # Re-asked per item: the entry check answered before the job was
        # queued, and an operator can switch translation off between two
        # items. The item fails with the gate's reason instead of queueing
        # one doomed translation job after another.
        from subtitles.tools.translate.availability import translation_available
        availability = translation_available()
        if not availability.available:
            raise ValueError(availability.reason)
        from subtitles.tools.translate.main import translate_subtitles_file
        if item.get('sports_event_id'):
            media_type = 'sports'
        else:
            media_type = 'episode' if item['sonarr_series_id'] else 'movies'

        if source_srt_file is _SOURCE_SRT_FILE_UNSET:
            source_srt_file = (_extract_translate_source(item) if item.get('embedded')
                               else item['srt_path'])
        if item.get('embedded') and not source_srt_file:
            return False
        sports_operation = None
        if media_type == 'sports':
            # Bound per item rather than once for the batch: the guard pins one
            # source, one destination and one file signature, so a batch-wide
            # operation would authorise writes it never inspected.
            from sportarr.profile_hooks import manual_translation_operation
            try:
                sports_operation = manual_translation_operation(
                    item['sports_event_id'], item.get('arr_instance_id'),
                    source_srt_file, options.get('to_lang', 'en'),
                    from_language=options.get('from_lang', item['srt_lang']),
                    forced=item['forced'], hi=item['hi'],
                )
            except ValueError as e:
                # Most often the profile does not want this target, which is a
                # skip for this item and not a failure of the batch.
                logger.warning('BAZARR skipping sports translation for event %s: %s',
                               item['sports_event_id'], e)
                return ITEM_SKIPPED
        # Don't pass the batch job_id to translate. translate_subtitles_file
        # has its own job/progress lifecycle that would hijack the batch job.
        # Calling without job_id makes it queue as its own separate job.
        child_id = translate_subtitles_file(
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
            arr_instance_id=item.get('arr_instance_id'),
            sports_operation=sports_operation,
        )
        # An identical job that was already queued answers with its own id, and
        # two items can share it. The batch itself is never its own child.
        if (children is not None and isinstance(child_id, int) and not isinstance(child_id, bool)
                and child_id != job_id and child_id not in children):
            children.append(child_id)
        return True
    elif action == 'extract':
        from subtitles.new_subtitle import SubtitleFileExists
        from subtitles.tools.embedded_extract import materialise_embedded_track

        media_type, media_id = _embedded_item_media(item)
        try:
            materialise_embedded_track(media_type, media_id, item['arr_instance_id'], item['srt_lang'],
                                       hi=item['hi'], forced=item['forced'], codec=item.get('codec'))
        except SubtitleFileExists:
            # Written since the batch was collected: the language is covered.
            return ITEM_SKIPPED
        return True
    elif action == 'ocr':
        from subtitles.new_subtitle import SubtitleFileExists
        from subtitles.tools.embedded_ocr import ocr_embedded, progress_message

        media_type, media_id = _embedded_item_media(item)
        name = _item_display_name(item)

        def progress(step, value, maximum):
            # The batch owns the progress bar; each step names itself, and a
            # stopped batch stops the item's run here.
            jobs_queue.update_job_progress(
                job_id=job_id, progress_message=f"ocr: {name}: {progress_message(step, value, maximum)}")

        try:
            ocr_embedded(media_type, media_id, item['arr_instance_id'], item['srt_lang'], hi=item['hi'],
                         forced=item['forced'], codec=item.get('codec'), progress=progress)
        except SubtitleFileExists:
            # Written since the batch was collected: the language is covered.
            return ITEM_SKIPPED
        return True
    elif action in MOD_ACTIONS:
        sports_kwargs = {}
        if item.get('sports_event_id'):
            from sportarr.workflows import SportsJobSignal

            sports_kwargs = {'sports_event_id': item['sports_event_id'],
                             'cancel': SportsJobSignal(item['arr_instance_id'], job_id)}
        output_path = subtitles_apply_mods(
            item['srt_lang'],
            item['srt_path'],
            [action],
            item['video_path'],
            # Resolve keep-lyrics against the per-item owning instance (#227).
            arr_instance_id=item.get('arr_instance_id'),
            media_type=('sports' if item.get('sports_event_id')
                        else 'episode' if item['sonarr_series_id'] else 'movies'),
            **sports_kwargs,
        )
        if item.get('sports_event_id'):
            if not output_path:
                return False
            from subtitles.indexer.sports import store_subtitles_sports

            owner = item['arr_instance_id']
            store_subtitles_sports(item['sports_event_id'], owner)
            try:
                event_stream(type='sports', payload=item['sports_event_id'])
            except Exception:
                logger.exception('Sports subtitles were indexed but the UI refresh failed')
        return True
    return False


def _embedded_item_media(item):
    """(media type, id) of an embedded-track item, as the embedded primitives take them."""
    if item.get('sports_event_id'):
        return 'sports', item['sports_event_id']
    if item['sonarr_episode_id']:
        return 'episode', item['sonarr_episode_id']
    return 'movie', item['radarr_id']


def _scan_sports(item, job_id=None):
    """Re-index a selected sports event, or every event in a selected league.

    Returns False when the selection names nothing indexable, so the caller can
    count it as skipped rather than queued.

    Cancellation is threaded the way _search_sports below threads it. A league
    indexes every one of its events in this one call, each probing a recording,
    and the batch updates progress only before entering here: without a signal
    a stopped batch kept probing the whole league before anything noticed.
    """
    from sportarr.workflows import SportsJobSignal
    from sportarr.connection import check_cancelled
    from subtitles.indexer.sports import store_subtitles_sports

    arr_instance_id = item.get('arr_instance_id')
    if not arr_instance_id:
        return False
    # Only with a job to answer to: the signal reads the owner's enabled state
    # and reports progress against the job, and a direct call carrying neither
    # has nothing to be cancelled by. The batch always supplies one.
    cancel = SportsJobSignal(arr_instance_id, job_id) if job_id else None
    if item.get('type') == 'sports':
        event_id = item.get('sportsEventId')
        if not event_id:
            return False
        store_subtitles_sports(event_id, arr_instance_id, cancel=cancel)
        return True
    league_id = item.get('sportsLeagueId')
    if not league_id:
        return False
    rows = database.execute(
        select(TableSportsEvents.id).where(
            TableSportsEvents.league_id == league_id,
            TableSportsEvents.arr_instance_id == arr_instance_id,
        )
    ).scalars().all()
    for event_id in rows:
        check_cancelled(cancel)
        store_subtitles_sports(event_id, arr_instance_id, cancel=cancel)
    return bool(rows)


def _search_sports(item, job_id):
    """Search the missing languages of a selected event, or of a whole league."""
    from sportarr.automatic import search_event
    from sportarr.workflows import SportsJobSignal, sports_download_subtitles

    arr_instance_id = item.get('arr_instance_id')
    if not arr_instance_id:
        return False
    if item.get('type') == 'sports':
        event_id = item.get('sportsEventId')
        if not event_id:
            return False
        search_event(event_id, arr_instance_id, job_id=job_id,
                     cancel=SportsJobSignal(arr_instance_id, job_id))
        return True
    league_id = item.get('sportsLeagueId')
    if not league_id:
        return False
    sports_download_subtitles(league_id, arr_instance_id, job_id=job_id)
    return True


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
        sports_selections = {}
        for item in items:
            owner = item.get('arr_instance_id')
            if item.get('type') not in ('sports', 'sportsLeague') or not owner:
                continue
            event_id, league_id = item.get('sportsEventId'), item.get('sportsLeagueId')
            if not event_id and not league_id:
                continue
            selection = sports_selections.setdefault(owner, {'event_ids': set(), 'league_ids': set()})
            if event_id:
                selection['event_ids'].add(event_id)
            elif league_id:
                selection['league_ids'].add(league_id)
        try:
            if sonarr_series_filters:
                upgrade_episodes_subtitles(job_id=job_id, sonarr_series_filters=sonarr_series_filters)
            if radarr_filters:
                upgrade_movies_subtitles(job_id=job_id, radarr_filters=radarr_filters)
            for owner, selection in sorted(sports_selections.items()):
                upgrade_sports_subtitles(job_id=job_id, arr_instance_id=owner,
                                        **{key: sorted(ids) for key, ids in selection.items()})
            queued = len(sonarr_series_filters) + len(radarr_filters) + len(sports_selections)
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f'Error during upgrade: {e}')  # noqa: G004
            errors.append(str(e))
        _raise_for_batch_failures(action, errors, len(items), queued, 0)
        return {'queued': queued, 'skipped': 0, 'errors': errors}

    # Sports disc scans come in two shapes. The wanted page's Scan All sends
    # one representative row per owner with no media id, meaning the whole
    # library: the sports rescan job builds its own candidate list from the
    # owner's events and takes an instance, not a media filter. The league
    # toolbar and the events table send the selected league (or event) and
    # expect exactly that selection to be re-indexed. Gating the owner-wide arm
    # on the absence of a media id keeps a league-scoped action from silently
    # widening into a whole-library scan.
    sports_scan_owners = sorted({
        i.get('arr_instance_id')
        for i in items
        if action == 'scan-disk' and i.get('type') in ('sports', 'sportsLeague')
        and not i.get('sportsEventId') and not i.get('sportsLeagueId')
        and i.get('arr_instance_id')
    })
    if action == 'scan-disk' and sports_scan_owners:
        from subtitles.indexer.sports import sports_full_scan_subtitles

        try:
            for owner in sports_scan_owners:
                sports_full_scan_subtitles(job_id=job_id, arr_instance_id=owner)
            queued += len(sports_scan_owners)
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f'Error during sports scan-disk: {e}')  # noqa: G004
            errors.append(str(e))

    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(items))

    searched_series = set()
    searched_movies = set()
    for i, item in enumerate(items, start=1):
        item_type = item.get('type')
        jobs_queue.update_job_progress(
            job_id=job_id,
            progress_value=i,
            progress_message=f"Processing {item_type} ({i}/{len(items)})"
        )

        try:
            if action == 'scan-disk':
                if item_type in ('sports', 'sportsLeague'):
                    if not item.get('sportsEventId') and not item.get('sportsLeagueId'):
                        # Representative Scan All row, handled once per owner
                        # above. One without an owner can never resolve a
                        # mapping, so it is skipped rather than dropped.
                        if not item.get('arr_instance_id'):
                            skipped += 1
                        continue
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
                elif item_type in ('sports', 'sportsLeague'):
                    scanned = _scan_sports(item, job_id)
                    if not scanned:
                        skipped += 1
                        continue
                else:
                    skipped += 1
                    continue
            elif action == 'search-missing':
                if item_type in ('series', 'episode'):
                    series_id = item.get('sonarrSeriesId')
                    if not series_id:
                        skipped += 1
                        continue
                    # Episode selections can share a series, and another
                    # request may already have this show queued or running.
                    # Inline searches need the deduplication previously
                    # supplied by the queue, or two searches race file by file
                    # and spend provider quota twice. The claim also publishes
                    # this batch's own inline run, so a Search on the show's
                    # page follows this job instead of starting a second one.
                    # The kwargs mirror the ones the series route's self-queue
                    # binds.
                    series_key = (item.get('arr_instance_id'), series_id)
                    if series_key in searched_series:
                        skipped += 1
                        continue
                    claim = {'no': series_id, 'job_sub_function': False,
                             'arr_instance_id': item.get('arr_instance_id')}
                    owner = jobs_queue.claim_inline_job('subtitles.mass_download.series',
                                                        'series_download_subtitles',
                                                        kwargs=claim, job_id=job_id)
                    if owner != job_id:
                        skipped += 1
                        continue
                    searched_series.add(series_key)
                    try:
                        series_download_subtitles(series_id, job_id=job_id, job_sub_function=True,
                                                   arr_instance_id=item.get('arr_instance_id'))
                    finally:
                        jobs_queue.release_inline_job('subtitles.mass_download.series',
                                                      'series_download_subtitles', kwargs=claim)
                elif item_type == 'movie':
                    radarr_id = item.get('radarrId')
                    if not radarr_id:
                        skipped += 1
                        continue
                    # A selection can repeat a movie, and another request may
                    # already have this movie queued or running. Inline
                    # searches need the deduplication previously supplied by
                    # the queue, or two searches race file by file and spend
                    # provider quota twice. The claim also publishes this
                    # batch's own inline run, so a Search on the movie's page
                    # follows this job instead of starting a second one. The
                    # kwargs mirror the ones the movie route's self-queue
                    # binds.
                    movie_key = (item.get('arr_instance_id'), radarr_id)
                    if movie_key in searched_movies:
                        skipped += 1
                        continue
                    claim = {'no': radarr_id, 'job_sub_function': False,
                             'arr_instance_id': item.get('arr_instance_id')}
                    owner = jobs_queue.claim_inline_job('subtitles.mass_download.movies',
                                                        'movies_download_subtitles',
                                                        kwargs=claim, job_id=job_id)
                    if owner != job_id:
                        skipped += 1
                        continue
                    searched_movies.add(movie_key)
                    try:
                        # Keep the selected movies inside this cancellable
                        # batch job instead of enqueuing one independent job
                        # per movie.
                        movies_download_subtitles(
                            radarr_id, job_id=job_id, job_sub_function=True,
                            arr_instance_id=item.get('arr_instance_id'))
                    finally:
                        jobs_queue.release_inline_job('subtitles.mass_download.movies',
                                                     'movies_download_subtitles', kwargs=claim)
                elif item_type in ('sports', 'sportsLeague'):
                    searched = _search_sports(item, job_id)
                    if not searched:
                        skipped += 1
                        continue
                else:
                    skipped += 1
                    continue
            queued += 1
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f'Error processing {action} for {item}: {e}')  # noqa: G004
            errors.append(f'{_media_item_display_name(item)} ({e})')

    _raise_for_batch_failures(action, errors, len(items), queued, skipped)
    return {'queued': queued, 'skipped': skipped, 'errors': errors}


class _TranslationGateClosed(Exception):
    """A mass translate refused by the gate before a job existed.

    str() is the gate's reason."""


def _translation_gate_refusal(job_id=None):
    """Refuse a mass translate while the gate is closed.

    A batch that is already running, one with a job id, fails with the reason:
    the queue marks a job that returns as completed, and a closed gate must not
    read as success. A batch still being scheduled is refused through
    _TranslationGateClosed, which mass_batch_operation answers with the payload
    its callers expect. An open gate returns quietly.
    """
    from subtitles.tools.translate.availability import translation_available

    availability = translation_available()
    if availability.available:
        return
    if job_id:
        raise JobFailed(availability.reason)
    raise _TranslationGateClosed(availability.reason)


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

    # A translate refuses before a job exists, exactly like the API above it:
    # without the gate a switched-off or half-configured translator turned a
    # mass translate into one doomed job per item, whichever door the request
    # came through. The refusal arrives as an exception and leaves no local
    # behind, because the scheduling call below rebinds every local of this
    # frame against this function's signature: a name the signature does not
    # accept would be handed to the job as a keyword argument, and the
    # open-gate self-schedule would crash with a TypeError.
    if action == 'translate':
        try:
            _translation_gate_refusal(job_id)
        except _TranslationGateClosed as refusal:
            return {'queued': 0, 'skipped': 0, 'errors': [str(refusal)]}

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

    if action == 'ocr':
        from subtitles.tools.embedded_ocr import BUSY, ocr_job_waiting_or_running
        from subtitles.tools.ocr.tesseract import UNAVAILABLE, ocr_available

        if not ocr_available():
            raise JobFailed(UNAVAILABLE, retryable=False)
        # One OCR job at a time, this batch included: a row's OCR to text or
        # another batch queued or running first keeps this one from starting.
        if ocr_job_waiting_or_running(exclude=job_id):
            raise JobFailed(BUSY)

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
    # What the collection left out is part of what was selected, so the
    # failure summary counts it in the total it is a share of.
    collection_skipped = total_skipped

    # Consume items in order, with bounded extraction ahead for translation.
    total_count = len(all_items)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=total_count)

    if total_count == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    processed = 0
    failed = 0
    all_errors = []
    # One line per failed item, "name (reason)", for the job's failure reason.
    failures = []
    # A translate batch only queues one translation job per item and ends, so
    # it publishes their ids for the client to wait on before it refreshes the
    # tables. It never waits on them itself: a job that waits on jobs queued
    # behind it deadlocks a queue that runs one job at a time.
    children = []
    published_children = 0
    item_kwargs = {'children': children} if action == 'translate' else {}

    extraction_pool = None
    try:
        if action == 'translate':
            workers = settings.general.mass_embedded_extract_workers
            extraction_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='mass-extract')
            embedded_items = ((index, item) for index, item in enumerate(all_items, start=1)
                              if item.get('embedded'))
            pending_extractions = {}

            def refill_extractions():
                while len(pending_extractions) < workers:
                    next_item = next(embedded_items, None)
                    if next_item is None:
                        break
                    index, embedded_item = next_item
                    pending_extractions[index] = extraction_pool.submit(_extract_translate_source_in_pool, embedded_item)

            refill_extractions()

        for i, item in enumerate(all_items, start=1):
            jobs_queue.update_job_progress(
                job_id=job_id,
                progress_value=i - 1,
                progress_message=f"{action}: {_item_display_name(item)} ({i}/{total_count})"
            )

            try:
                if action == 'translate' and item.get('embedded'):
                    future = pending_extractions.pop(i)
                    try:
                        source_srt_file = future.result()
                    finally:
                        # Even a completed future occupies the window until
                        # its result is consumed, including a failed result.
                        refill_extractions()
                    result = _process_subtitle_item(
                        item, action, options, job_id, source_srt_file=source_srt_file, **item_kwargs)
                else:
                    result = _process_subtitle_item(item, action, options, job_id, **item_kwargs)
                if result is ITEM_SKIPPED:
                    total_skipped += 1
                elif result:
                    processed += 1
                else:
                    failed += 1
                    failures.append(f'{_item_display_name(item)} ({_NO_RESULT_REASONS.get(action, "no output was written")})')
            except JobCancelled:
                raise
            except Exception as e:
                logger.error(f'Error during {action} on {_item_display_name(item)}: {e}')  # noqa: G004
                all_errors.append(str(e))
                failures.append(f'{_item_display_name(item)} ({e})')
                failed += 1
            finally:
                jobs_queue.update_job_progress(
                    job_id=job_id,
                    progress_value=i,
                    progress_message=f"{action}: {_item_display_name(item)} ({i}/{total_count})"
                )
                if len(children) > published_children:
                    published_children = len(children)
                    jobs_queue.publish_job_result(job_id, {'child_jobs': list(children)})
    finally:
        if extraction_pool is not None:
            extraction_pool.shutdown(wait=False, cancel_futures=True)

    jobs_queue.update_job_name(
        job_id=job_id,
        new_job_name=f"Mass {action} complete: {processed} done, {total_skipped} skipped"
    )
    logger.info(
        f'BAZARR mass {action} complete: {processed} processed, {failed} failed, '  # noqa: G004
        f'{total_skipped} skipped, {len(all_errors)} errors'
    )
    _raise_for_batch_failures(action, failures, total_count + collection_skipped, processed, total_skipped)
    return {'queued': processed, 'skipped': total_skipped + failed, 'errors': all_errors}


def _raise_for_batch_failures(action, failures, total, done, skipped):
    """Fail the batch job when any item failed, naming the first few.

    The queue marks a job failed only when it raises. The per-item errors used
    to live only in the returned value, which nothing shows, so a batch where
    every item failed still read as completed.
    """
    if not failures:
        return
    raise JobFailed(f'Mass {action}: {len(failures)} of {total} items failed ({done} done, '
                           f'{skipped} skipped). {describe_failures(failures)}')
