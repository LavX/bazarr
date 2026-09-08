"""Subtitle discovery and profile state for exact, owned sports file rows."""
import ast
import logging
import os
import pickle

from sqlalchemy import select
from subliminal_patch import core, search_external_subtitles

from app.config import settings
from app.jobs_queue import jobs_queue
from app.database import (database, get_audio_profile_languages, get_profile_cutoff, get_profiles_list,
                          TableArrInstances, TableSportsEvents)
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha2_from_alpha3, get_language_set
from sportarr.analysis import parse_video_metadata
from sportarr.connection import check_cancelled
from sportarr.db import sports_transaction
from sportarr.identity import resolve_event_in_session
from sportarr.parser import positive_id
from sportarr.settings import get_sports_settings
from sportarr.sync.leagues import notify, require_sportarr
from subtitles.adaptive_searching import is_search_given_up
from subtitles.indexer.utils import (add_combined_outputs, add_sync_engine_outputs, get_external_subtitles_path,
                                     guess_external_subtitles, normalize_subtitle_language_variant,
                                     subtitle_language_with_combined_modifier, subtitle_language_with_sync_modifier)
from subtitles.tools.subsync_engines import SyncOutputOwnerIndex, subtitle_write_locks
from utilities.helper import get_subtitle_destination_folder, get_target_folder
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings
from utilities.video_analyzer import embedded_subtitles_from_metadata


def _file_signature(context, row, mappings):
    stat = os.stat(context.mapped_path)
    return (context.event_id, context.arr_instance_id, context.file_id, context.original_path,
            context.mapped_path, os.path.realpath(context.mapped_path), mappings, row.file_size,
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _snapshot(event_id, owner, cancel=None):
    check_cancelled(cancel)
    context = resolve_event_in_session(database, event_id, owner)
    instance = require_sportarr(database, context.arr_instance_id)
    row = database.get(TableSportsEvents, event_id, populate_existing=True)
    if row is None:
        raise ValueError('Sports event not found for this owner')
    mappings = instance.path_mappings
    try:
        signature = _file_signature(context, row, mappings)
    except OSError:
        # A new mapping may point at an unavailable file. Do not keep displaying
        # subtitles from the previous mapped file while a later repair retries.
        with sports_transaction(database) as session:
            check_cancelled(cancel)
            require_sportarr(session, context.arr_instance_id)
            current = resolve_event_in_session(session, event_id, context.arr_instance_id)
            if current == context:
                row = session.get(TableSportsEvents, event_id, populate_existing=True, with_for_update=True)
                row.subtitles = '[]'
                row.missing_subtitles = str(_missing(context.profile_id, [], row.audio_language, row.failedAttempts))
                row.ffprobe_cache = None
                session.flush()
                check_cancelled(cancel)
        raise
    check_cancelled(cancel)
    return context, mappings, signature


def _validated_row(session, context, signature, cancel=None):
    check_cancelled(cancel)
    # Lock the owner before re-resolving context. SQLite's BEGIN IMMEDIATE is
    # supplied by sports_transaction; PostgreSQL also locks the exact file row.
    instance = require_sportarr(session, context.arr_instance_id)
    current = resolve_event_in_session(session, context.event_id, context.arr_instance_id)
    row = session.execute(select(TableSportsEvents).where(
        TableSportsEvents.id == context.event_id, TableSportsEvents.arr_instance_id == context.arr_instance_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one()
    if current != context or _file_signature(current, row, instance.path_mappings) != signature:
        raise ValueError('Sports file changed during subtitle indexing')
    check_cancelled(cancel)
    return row


def _cached(row, signature):
    try:
        data = pickle.loads(row.ffprobe_cache)
        if isinstance(data, dict) and data.get('sports_file') == signature:
            return data
    except (TypeError, ValueError, EOFError, pickle.UnpicklingError):
        pass
    return {}


def _metadata(context, signature, cached, use_cache, cancel=None):
    check_cancelled(cancel)
    parser = settings.general.embedded_subtitles_parser
    if use_cache and cached.get(parser):
        return cached
    data = parse_video_metadata(context.mapped_path, os.path.getsize(context.mapped_path), use_cache=False, cancel=cancel)
    check_cancelled(cancel)
    if not data or not data.get(parser):
        raise OSError('Could not analyze sports video')
    return data | {'sports_file': signature}


def parse_sports_video_metadata(event_id, arr_instance_id=None, use_cache=True, file=None, *, cancel=None):
    """Probe one local event and publish cache only while its ownership still matches."""
    context, _, signature = _snapshot(event_id, arr_instance_id, cancel)
    if file is not None and file != context.mapped_path:
        raise ValueError('Sports probe path does not match the local event')
    row = database.get(TableSportsEvents, event_id, populate_existing=True)
    data = _metadata(context, signature, _cached(row, signature), use_cache, cancel)
    with sports_transaction(database) as session:
        row = _validated_row(session, context, signature, cancel)
        # A probe alone must never turn a pending subtitle scan into a completed one.
        data = data | {'sports_indexed': bool(_cached(row, signature).get('sports_indexed'))}
        row.ffprobe_cache = pickle.dumps(data, pickle.HIGHEST_PROTOCOL)
        session.flush()
        check_cancelled(cancel)
    return data


def _missing(profile_id, subtitles, audio_language, failed_attempts, *, profile=None):
    if profile_id is None:
        return []
    supplied_profile = profile is not None
    profile = profile if supplied_profile else get_profiles_list(profile_id=profile_id)
    if not profile:
        return []
    audio = {item['code2'] for item in get_audio_profile_languages(audio_language)}
    actual = set()
    for language, path, _ in subtitles:
        if not path and not settings.general.use_embedded_subs:
            continue
        base, *variants = language.split(':')
        if any(item.startswith('combined-') for item in variants):
            continue
        actual.add((base, 'forced' in variants, 'hi' in variants))
        if 'hi' in variants and 'forced' not in variants:
            actual.add((base, False, False))

    def key(item):
        return item['language'], item['forced'] == 'True', item['hi'] == 'True'

    def audio_allowed(item):
        return item.get('audio_only_include') != 'True' or item['language'] in audio

    def excluded(item):
        return item.get('audio_exclude') == 'True' and item['language'] in audio

    cutoff = ([item for item in profile['items']
               if profile.get('cutoff') in (item['id'], 65535)]
              if supplied_profile else get_profile_cutoff(profile_id))
    for item in cutoff or []:
        if audio_allowed(item) and (excluded(item) or key(item) in actual):
            return []
    missing = []
    for item in profile['items']:
        if (not audio_allowed(item) or excluded(item) or key(item) in actual
                or is_search_given_up(item['language'], failed_attempts or '[]')):
            continue
        base, forced, hi = key(item)
        missing.append(base + (':forced' if forced else ':hi' if hi else ''))
    return missing


def _external(context, mappings, ownership_index, cancel=None):
    check_cancelled(cancel)
    dest_folder = get_subtitle_destination_folder()
    # Discovery also searches CUSTOM_PATHS. Set this in the same way as the
    # existing indexers; the destination policy is global for all media kinds.
    core.CUSTOM_PATHS = [dest_folder] if dest_folder else []
    found = search_external_subtitles(context.mapped_path, languages=get_language_set(),
                                     only_one=settings.general.single_language)
    folder = get_target_folder(context.mapped_path, create=False) or os.path.dirname(context.mapped_path)
    found = add_sync_engine_outputs(folder, found, video_path=context.mapped_path, ownership_index=ownership_index)
    found = add_combined_outputs(folder, found, video_filename=os.path.basename(context.mapped_path))
    reverse = lambda path: apply_sports_mapping(path, mappings, reverse=True)  # noqa: E731
    actual = []
    for subtitle, language in found.items():
        check_cancelled(cancel)
        path = get_external_subtitles_path(context.mapped_path, subtitle)
        if not path or not os.path.isfile(path):
            continue
        # Existing sidecars may be beside the video even when new downloads go
        # into a configured folder. Analyze the actual discovered file.
        language = guess_external_subtitles(os.path.dirname(path), {subtitle: language}, 'sports',
                                             path_reverse=reverse)[subtitle]
        if not language or not alpha2_from_alpha3(language.alpha3):
            continue
        custom = CustomLanguage.found_external(subtitle, path)
        code = custom or normalize_subtitle_language_variant(str(language), forced=language.forced, hi=language.hi)
        if code.split(':')[0] == 'und':
            continue
        code = subtitle_language_with_sync_modifier(code, subtitle)
        code = subtitle_language_with_combined_modifier(code, subtitle)
        actual.append([code, reverse(path), os.stat(path).st_size])
    check_cancelled(cancel)
    return actual


def store_subtitles_sports(event_id, arr_instance_id=None, *, use_cache=None, ownership_index=None, cancel=None):
    """Index an exact playable-file row. Failures leave the row retryable."""
    context, raw_mappings, signature = _snapshot(event_id, arr_instance_id, cancel)
    mappings = read_sports_mappings(raw_mappings)
    instance = require_sportarr(database, context.arr_instance_id)
    if use_cache is None:
        use_cache = get_sports_settings(instance)['use_ffprobe_cache']
    destination = os.path.join(get_target_folder(context.mapped_path, create=False)
                               or os.path.dirname(context.mapped_path), '.destination')
    with subtitle_write_locks(context.mapped_path, destination, cancel=cancel):
        with sports_transaction(database) as session:
            row = _validated_row(session, context, signature, cancel)
            cached = _cached(row, signature)
            if not cached:
                row.subtitles = row.missing_subtitles = row.failedAttempts = '[]'
            row.ffprobe_cache = (pickle.dumps(cached | {'sports_indexed': False}, pickle.HIGHEST_PROTOCOL)
                                 if cached else None)
            session.flush()
        data = cached | {'sports_file': signature}
        actual = []
        if settings.general.use_embedded_subs:
            data = _metadata(context, signature, cached, use_cache, cancel)
            for language, forced, hi, codec in embedded_subtitles_from_metadata(data):
                if codec and ((settings.general.ignore_pgs_subs and codec.lower() == 'pgs') or
                              (settings.general.ignore_vobsub_subs and codec.lower() == 'vobsub') or
                              (settings.general.ignore_ass_subs and codec.lower() == 'ass')):
                    continue
                code = alpha2_from_alpha3(language)
                if code:
                    actual.append([normalize_subtitle_language_variant(code, forced=forced, hi=hi), None, None])
        actual.extend(_external(context, mappings, ownership_index, cancel))
        with sports_transaction(database) as session:
            row = _validated_row(session, context, signature, cancel)
            row.subtitles = str(actual)
            row.missing_subtitles = str(_missing(context.profile_id, actual, row.audio_language, row.failedAttempts))
            row.ffprobe_cache = pickle.dumps(data | {'sports_indexed': True}, pickle.HIGHEST_PROTOCOL)
            session.flush()
            check_cancelled(cancel)
    check_cancelled(cancel)
    notify([event_id])
    return actual


def refresh_sports_files(event_ids, arr_instance_id, cancel=None):
    """Retry incomplete scans, including files changed only by path mapping."""
    check_cancelled(cancel)
    index = SyncOutputOwnerIndex()
    for event_id in event_ids:
        check_cancelled(cancel)
        try:
            context, _, signature = _snapshot(event_id, arr_instance_id, cancel)
            row = database.get(TableSportsEvents, event_id, populate_existing=True)
            if not _cached(row, signature).get('sports_indexed'):
                store_subtitles_sports(event_id, context.arr_instance_id, ownership_index=index, cancel=cancel)
        except Exception:
            check_cancelled(cancel)
            logging.exception('Could not index sports event %s for owner %s; next sync will retry',
                              event_id, arr_instance_id)
    check_cancelled(cancel)


def _events(event_id=None, league_id=None, arr_instance_id=None):
    for value in (event_id, league_id, arr_instance_id):
        if value is not None and not positive_id(value):
            raise ValueError('Sports filters require positive local IDs')
    query = select(TableSportsEvents.id, TableSportsEvents.arr_instance_id).join(
        TableArrInstances, TableSportsEvents.arr_instance_id == TableArrInstances.id).where(
        TableArrInstances.kind == 'sportarr', TableArrInstances.enabled == 1)
    if arr_instance_id is not None:
        require_sportarr(database, arr_instance_id)
        query = query.where(TableSportsEvents.arr_instance_id == arr_instance_id)
    if event_id is not None:
        context = resolve_event_in_session(database, event_id, arr_instance_id)
        if league_id is not None and context.league_id != league_id:
            raise ValueError('Sports event does not belong to this league')
        query = query.where(TableSportsEvents.id == event_id)
    if league_id is not None:
        from sportarr.library import get_league
        if get_league(database, league_id, arr_instance_id) is None:
            raise ValueError('Sports league not found for this owner')
        query = query.where(TableSportsEvents.league_id == league_id)
    return database.execute(query.order_by(TableSportsEvents.id)).all()


def list_missing_subtitles_sports(event_id=None, league_id=None, arr_instance_id=None):
    ids = _events(event_id, league_id, arr_instance_id)
    for local_id, owner in ids:
        with sports_transaction(database) as session:
            require_sportarr(session, owner)
            context = resolve_event_in_session(session, local_id, owner)
            row = session.execute(select(TableSportsEvents).where(
                TableSportsEvents.id == local_id, TableSportsEvents.arr_instance_id == owner)
                .with_for_update().execution_options(populate_existing=True)).scalar_one()
            row.missing_subtitles = str(_missing(context.profile_id, ast.literal_eval(row.subtitles or '[]'),
                                                 row.audio_language, row.failedAttempts))
            session.flush()
    notify([local_id for local_id, _ in ids])


def sports_full_scan_subtitles(job_id=None, arr_instance_id=None, *, wait_for_completion=False):
    if not job_id:
        if not _events(arr_instance_id=arr_instance_id):
            return
        return jobs_queue.add_job_from_function('Indexing sports subtitles', is_progress=True,
                                                wait_for_completion=wait_for_completion)
    ids = _events(arr_instance_id=arr_instance_id)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(ids), progress_message='Indexing')
    index = SyncOutputOwnerIndex()
    failures = []
    for position, (event_id, owner) in enumerate(ids, 1):
        try:
            store_subtitles_sports(event_id, owner, ownership_index=index)
        except Exception:
            failures.append(event_id)
            logging.exception('Could not index sports event %s for owner %s', event_id, owner)
        jobs_queue.update_job_progress(job_id=job_id, progress_value=position)
    if failures:
        raise OSError(f'Could not index {len(failures)} sports files; see the log')
    jobs_queue.update_job_name(job_id=job_id, new_job_name='Indexed sports subtitles')
