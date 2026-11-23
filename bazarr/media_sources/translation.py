# coding=utf-8
"""Eligibility and queueing shared by source downloads and later wanted searches."""

import ast
import logging
import os

from sqlalchemy import select

from app.config import settings
from app.database import get_audio_profile_languages, get_profile_cutoff, get_profiles_list
from subtitles.indexer.entries import entry_codec
from subtitles.language_profiles import build_translate_from_map
from subtitles.tools.subsync_engines import (capture_subtitle_versions, subtitle_source_version,
                                            sync_output_owner_is_unique)
from subtitles.wanted.utils import SOURCE_HISTORY_ACTIONS, source_history_for_wait, source_wait_elapsed

from . import repository
from .context import resolve_same_item
from .db import handle


def live_source(source, target=None):
    """Resolve the same active source file, accounting for SQLite's reusable row ids."""
    current = resolve_same_item(source, target)
    if current is None:
        return None
    state = repository.item_state(current.item_id, target)
    if state is None or not state.source_enabled or not state.library_enabled or not os.path.isfile(current.path):
        return None
    if source.wanted_file_stamp is not None:
        from .analysis import local_content_stamp
        if local_content_stamp(current.path) != source.wanted_file_stamp:
            return None
    return current


def _active_item(source, target=None):
    item = repository.get_item(source.item_id, target)
    if (item is None or not source.same_item(item) or not item.monitored or item.arr_shadow
            or item.server_missing or item.identity_state not in ('identified', 'parsed')):
        return None
    return item


def _source_paths(item, language):
    """Plain external subtitles first, then HI. Generated and forced variants cannot seed translations."""
    entries = ast.literal_eval(item.subtitles or '[]')
    for code in (language, f'{language}:hi'):
        for entry in entries:
            if entry and len(entry) >= 2 and entry[0] == code and entry[1] and os.path.isfile(entry[1]):
                yield entry[1]


def _source_hold(source, subtitle_path, *, new_download=False, score=None, target=None):
    wait_minutes = settings.translator.min_source_wait_minutes
    min_score = settings.translator.min_source_score
    if new_download and wait_minutes:
        return 'wait'
    if score is not None and score < min_score:
        return 'score'
    if not (wait_minutes or (min_score and score is None)):
        return None
    history = repository.History.c
    rows = handle(target).execute(select(history.subtitles_path, history.action, history.timestamp,
                                  history.score, history.score_out_of).where(
        history.item_id == source.item_id, history.subtitles_path == subtitle_path,
        history.action.in_(SOURCE_HISTORY_ACTIONS)
    ).order_by(history.timestamp.desc(), history.id.desc())).all()
    written_at, hand_upload = source_history_for_wait(rows, subtitle_path, lambda path: (path,))
    if wait_minutes and not (hand_upload or source_wait_elapsed(subtitle_path, written_at)):
        return 'wait'
    if min_score and score is None and written_at is not None and not hand_upload:
        row = next((row for row in rows if row.action != 5), None)
        if (row is not None and row.score is not None and row.score_out_of
                and row.score * 100 / row.score_out_of < min_score):
            return 'score'
    return None


def _target_code(to_lang, forced, hi):
    return f'{to_lang}:forced' if forced else f'{to_lang}:hi' if hi else to_lang


def prepare_source_sidecars(source, ownership_index, written_paths):
    """Classify the current external tracks under subtitle locks before the DB write lock."""
    from .indexer import list_external_subtitles

    item = repository.get_item(source.item_id)
    if item is None or not source.same_item(item) or item.path != source.path:
        return []
    return list_external_subtitles(
        source.path, ast.literal_eval(item.subtitles or '[]'), ownership_index,
        publication=True, written_paths=written_paths)


def _current_missing(item, profile_id, profile, *, external_subtitles=None):
    # Profile saves queue an index refresh. While that work waits, evaluate the live
    # policy against the indexed tracks instead of trusting its old missing list.
    from .indexer import compute_missing_subtitles

    ignored = {'pgs': settings.general.ignore_pgs_subs, 'vobsub': settings.general.ignore_vobsub_subs,
               'ass': settings.general.ignore_ass_subs}
    indexed = ast.literal_eval(item.subtitles or '[]')
    listing = [entry for entry in indexed if not entry[1] and not ignored.get(entry_codec(entry))]
    if external_subtitles is None:
        listing.extend(entry for entry in indexed if entry[1] and os.path.isfile(entry[1]))
    else:
        listing.extend(external_subtitles)
    audio_codes = {entry['code2'] for entry in get_audio_profile_languages(item.audio_language)}
    return compute_missing_subtitles(profile, get_profile_cutoff(profile_id), listing, item.failed_attempts,
                                     matches_audio=lambda language: language['language'] in audio_codes)


def _language_candidates(codes):
    return [(code.split(':', 1)[0], str(code.endswith(':hi')), str(code.endswith(':forced')))
            for code in codes if code is not None]


def stored_missing_language_candidates(item):
    return _language_candidates(ast.literal_eval(item.missing_subtitles or '[]'))


def missing_language_candidates(source, *, external_subtitles=None, target=None):
    """Read the live source policy and return the shared provider-search language tuples."""
    current = live_source(source, target)
    if current is None or current.profile_id is None:
        return []
    item = repository.get_item(current.item_id, target)
    if item is None or not current.same_item(item):
        return []
    profile = get_profiles_list(profile_id=current.profile_id)
    return _language_candidates(_current_missing(item, current.profile_id, profile, external_subtitles=external_subtitles))


def automatic_translation_is_current(source, subtitle_path, from_lang, to_lang, forced, hi, *, connection=None):
    """Recheck the live profile, indexed inputs, wait and threshold at dispatch and publication."""
    item = _active_item(source, connection)
    if item is None or not source.profile_id:
        return False
    if source.wanted_file_stamp is not None:
        from .analysis import local_content_stamp
        from .wanted import arr_claim_blocks_wanted

        if (local_content_stamp(source.path) != source.wanted_file_stamp
                or (connection is None and arr_claim_blocks_wanted(source))):
            return False
    target = _target_code(to_lang, forced, hi)
    profile = get_profiles_list(profile_id=source.profile_id)
    rule = build_translate_from_map(profile).get(target)
    return (rule is not None and rule['from'] == from_lang
            and target in _current_missing(item, source.profile_id, profile)
            and subtitle_path in _source_paths(item, from_lang)
            and _source_hold(source, subtitle_path, target=connection) is None)


def _already_translated(source, target, owners):
    history = repository.History.c
    path = handle().scalar(select(history.subtitles_path).where(
        history.item_id == source.item_id, history.video_path == source.path,
        history.language == target, history.action == 6
    ).order_by(history.timestamp.desc(), history.id.desc()).limit(1))
    return bool(path and os.path.isfile(path)
                and sync_output_owner_is_unique(source.path, path, ownership_index=owners))


def queue_missing_translations(source, *, downloaded_path=None, downloaded_language=None, score=None,
                               wanted=False):
    """Queue eligible source translations and return the target codes their jobs cover.

    Wanted uses the stored missing list and also holds targets waiting on a source file. A just downloaded
    subtitle is held for a positive wait. A later search with no download arguments measures the existing
    subtitle's age from source history, and falls back to the file's time for externally added subtitles.
    Once eligibility is checked, a target already present at the owned destination is covered as well.
    """
    from subtitles.tools.translate.availability import translation_available
    from subtitles.tools.translate.core.translator_utils import translated_destination_path
    from subtitles.tools.translate.failure_record import translation_recently_failed
    from subtitles.tools.translate.main import translate_subtitles_file
    from .indexer import SourcePublicationOwnerIndex

    source = live_source(source)
    if source is None or not source.profile_id:
        return set()
    source_video_version = subtitle_source_version(source.path)
    if source_video_version is None:
        return set()
    item = _active_item(source)
    if item is None or not translation_available().available:
        return set()
    profile = get_profiles_list(profile_id=source.profile_id)
    rules = build_translate_from_map(profile)
    if not rules:
        return set()
    missing = set(ast.literal_eval(item.missing_subtitles or '[]') if wanted
                  else _current_missing(item, source.profile_id, profile))
    owners = SourcePublicationOwnerIndex()
    covered = set()
    for target, rule in rules.items():
        from_lang = rule['from']
        to_lang = target.split(':', 1)[0]
        forced, hi = rule['forced'], rule['hi']
        if target not in missing or to_lang == from_lang:
            continue
        if _already_translated(source, target, owners):
            covered.add(target)
            continue
        candidates = _source_paths(item, from_lang)
        if downloaded_path is not None:
            if from_lang != downloaded_language:
                continue
            candidates = (candidate for candidate in candidates if candidate == downloaded_path)
        subtitle_path = next((candidate for candidate in candidates if
                              sync_output_owner_is_unique(source.path, candidate, ownership_index=owners)), None)
        if not subtitle_path or translation_recently_failed(
                None, 'source', source.item_id, from_lang, to_lang, forced, hi, source_created_at=source.created_at):
            continue
        hold = _source_hold(source, subtitle_path, new_download=downloaded_path is not None, score=score)
        if hold is not None:
            if wanted and hold == 'wait':
                covered.add(target)
            continue
        destination = translated_destination_path(source.path, to_lang, forced, hi)
        if not sync_output_owner_is_unique(source.path, destination, ownership_index=owners):
            logging.debug('BAZARR source translation target ownership is ambiguous: %s', destination)
            continue
        versions = capture_subtitle_versions(source.path, (destination, subtitle_path))
        if versions[0].version is not None:
            covered.add(target)
            continue
        if versions[1].version is None:
            continue
        job_id = translate_subtitles_file(
            video_path=source.path, source_srt_file=subtitle_path, from_lang=from_lang, to_lang=to_lang,
            forced=forced, hi=hi, media_type='episode' if source.kind == 'episode' else 'movies',
            sonarr_series_id=None, sonarr_episode_id=None, radarr_id=None, metadata=None,
            source=source, expected_destination_versions=versions, source_video_version=source_video_version)
        if job_id:
            covered.add(target)
    return covered
