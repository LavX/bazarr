# coding=utf-8
"""History and blacklist rows for source items.

Written by the caller that did the work (the search job, a manual download, a sync), never inside
``process_subtitle``. Every row carries snapshots of the item's path and titles and of the source's name, so it
stays meaningful once the item or the source is deleted and its links are NULL.
"""
import ast
import logging
from dataclasses import dataclass

import sqlalchemy as sa

from app.config import settings
from app.database import get_profiles_list
from subliminal_patch.score import MAX_SCORES
from subtitles.score_override import MANUAL_SCORE_ACTION
from subtitles.tools.subsync_engines import regular_subtitle_version

from . import identity, repository
from .analysis import local_content_stamp
from .context import SourceItemContext
from .db import handle
from .wanted import arr_claim_blocks_wanted, automatic_items


def _source_upgrade_query():
    from subtitles.upgrade import (EMBEDDED_SOURCE_ACTION, UPLOAD_ACTION, _FILE_WRITING_ACTIONS,
                                   _ai_translated_subtitles_are_upgrade_candidates,
                                   _retirement_evidence_actions, get_queries_condition_parameters, latest_row_actions)

    table, items = repository.History, repository.Items.c
    history = table.c
    newer = table.alias('newer_source_history').c
    minimum_timestamp, actions = get_queries_condition_parameters()
    after = sa.or_(newer.timestamp > history.timestamp,
                   sa.and_(newer.timestamp == history.timestamp, newer.id > history.id))
    same_language = sa.or_(newer.language == history.language,
                           newer.language == history.language + ':hi',
                           newer.language + ':hi' == history.language)
    # A file another operation replaced cannot be upgraded using the old score.
    replaces_file = sa.and_(newer.subtitles_path == history.subtitles_path,
                            newer.action.in_((*_FILE_WRITING_ACTIONS, MANUAL_SCORE_ACTION)))
    replaces_language = sa.and_(newer.language == history.language, newer.action.in_(latest_row_actions(actions)))
    changes = [replaces_file, replaces_language]
    if settings.general.use_embedded_subs:
        changes.append(sa.and_(newer.action == EMBEDDED_SOURCE_ACTION, same_language))
    superseded = sa.select(newer.id).where(
        newer.item_id == history.item_id, newer.video_path == history.video_path, after, sa.or_(*changes)
    ).correlate(table).exists()

    upgraded = table.alias('upgraded_source_history').c
    upgraded_already = sa.select(upgraded.id).where(
        upgraded.upgradedFromId == history.id, upgraded.item_id == history.item_id
    ).correlate(table).exists()

    retired = table.alias('retired_translation_source_history').c
    replacement_actions = _retirement_evidence_actions(actions)
    retired_translation = sa.select(retired.id).where(
        retired.item_id == history.item_id, retired.video_path == history.video_path,
        retired.timestamp > minimum_timestamp, retired.action.in_(replacement_actions),
        sa.or_(retired.language == history.language, retired.language == history.language + ':hi',
               retired.language + ':hi' == history.language),
        sa.or_(retired.action != UPLOAD_ACTION, retired.timestamp >= history.timestamp)
    ).correlate(table).exists()

    max_score = sa.case((items.kind == 'episode', MAX_SCORES['episode']), else_=MAX_SCORES['movie'])
    low_score = [sa.and_(history.score.is_(None), history.action == 6),
                 history.score < sa.func.coalesce(history.score_out_of, max_score) - 3]
    if _ai_translated_subtitles_are_upgrade_candidates():
        low_score.append(history.ai_translated == 1)

    return automatic_items(history.id, history.item_id, history.item_title, history.video_path, history.language,
                           history.score, history.subtitles_path, history.upgradedFromId,
                           items.created_at, items.subtitles, include_profile=True).join(
        table, sa.and_(history.item_id == items.id, history.source_id == items.source_id,
                       history.item_kind == items.kind, history.video_path == items.path)
    ).where(history.timestamp > minimum_timestamp, history.action.in_(actions), sa.or_(*low_score),
            history.subtitles_path.is_not(None), history.language.is_not(None), ~superseded, ~upgraded_already,
            sa.or_(history.action != 6, ~retired_translation))


def source_upgrade_candidates(*, item_ids=None, history_ids=None, target=None, check_arr_path=True):
    """Select current provider subtitles that can be replaced, including items with nothing missing."""
    if not settings.general.upgrade_subs:
        return []

    from subtitles.upgrade import _language_still_desired

    session = handle(target)
    query = _source_upgrade_query()
    if check_arr_path:
        query = query.where(~identity.arr_claims_stored_path(repository.Items.c.path, session))
    if item_ids is not None:
        query = query.where(repository.Items.c.id.in_(item_ids))
    if history_ids is not None:
        query = query.where(repository.History.c.id.in_(history_ids))

    candidates = []
    for row in session.execute(query.order_by(repository.History.c.id)):
        if not _language_still_desired(row.language, row.profile_id):
            continue
        paths = {entry[1] for entry in ast.literal_eval(row.subtitles or '[]') if entry[1]}
        if row.subtitles_path in paths and regular_subtitle_version(row.subtitles_path) is not None:
            candidates.append(row)
    return candidates


def get_upgradable_source_subtitles(history_id_list=None, *, item_ids=None, target=None):
    return {row.id: row.upgradedFromId for row in source_upgrade_candidates(
        history_ids=history_id_list, item_ids=item_ids, target=target)}


def source_subtitle_summary(items):
    """Enrich a page of source items using only its item IDs and stored local paths."""
    from api.utils import chunked, lowest_subtitle_scores

    by_id = {item.id: item.subtitles for item in items}
    session = handle()
    scores = lowest_subtitle_scores(session, by_id, repository.History.c.item_id, repository.History.c)
    upgradable_ids = set()
    for ids in chunked(list(by_id)):
        upgradable_ids.update(row.item_id for row in source_upgrade_candidates(item_ids=ids, target=session))
    return {item_id: {'lowest_subtitle_score': scores.get(item_id), 'upgradable': item_id in upgradable_ids}
            for item_id in by_id}


@dataclass(frozen=True)
class SourceSubtitleUpgrade:
    history_id: int
    original_id: int
    minimum_score: int
    subtitles_path: str
    subtitle_version: tuple
    video_stamp: tuple
    profile: dict

    def can_publish(self, source, *, target=None):
        if (local_content_stamp(source.path) != self.video_stamp
                or get_profiles_list(profile_id=source.profile_id) != self.profile
                or regular_subtitle_version(self.subtitles_path) != self.subtitle_version
                or arr_claim_blocks_wanted(source, target=target)):
            return False
        return bool(source_upgrade_candidates(item_ids=[source.item_id], history_ids=[self.history_id],
                                              target=target, check_arr_path=False))


def _snapshot(context: SourceItemContext, item, source):
    return dict(
        video_path=context.path,
        item_title=item.title if item is not None and item.path == context.path else context.title,
        group_title=context.group_title,
        source_name=source.name if source is not None else context.source_name,
    )


def _owners(context, target, *, lock=False):
    """Resolve the original owners and hold their foreign keys for a transactional insert."""
    source = repository.get_source(context.source_id, target, lock_key=lock)
    if source is not None and context.source_created_at is not None and source.created_at != context.source_created_at:
        source = None
    item = repository.get_item(context.item_id, target, for_update=lock)
    if item is not None and not context.same_item(item):
        item = None
    return item, source


def history_log_source(action, context: SourceItemContext, result, fake_provider=None, fake_score=None,
                       upgraded_from_id=None, target=None):
    """Record what happened to a source item. ``result`` is a ``ProcessSubtitlesResult``."""
    provider = fake_provider or result.provider
    score = fake_score or result.score
    ai_translated = (1 if getattr(result, 'ai_translated', False) is True else 0) if action in (1, 2, 3) else None
    full = MAX_SCORES['episode' if context.kind == 'episode' else 'movie']
    item, source = _owners(context, target, lock=target is not None)
    snapshot = _snapshot(context, item, source)
    snapshot['video_path'] = result.path or snapshot['video_path']
    return repository.insert_history(
        target, item_id=item.id if item is not None else None, source_id=source.id if source is not None else None,
        item_kind=context.kind, action=action,
        description=result.message, language=result.language_code, provider=provider,
        ai_translated=ai_translated, score=score,
        score_out_of=full if score or (ai_translated == 1 and score == 0) else None,
        subs_id=result.subs_id, subtitles_path=result.subs_path,
        matched=str(result.matched) if result.matched else None,
        not_matched=str(result.not_matched) if result.not_matched else None,
        upgradedFromId=upgraded_from_id, **snapshot)


def blacklist_log_source(context: SourceItemContext, provider, subs_id, language, target=None):
    item, source = _owners(context, target)
    return repository.insert_blacklist(
        target, item_id=item.id if item is not None else None, source_id=source.id if source is not None else None,
        item_kind=context.kind, provider=provider, subs_id=subs_id, language=language, **_snapshot(context, item, source))


def get_blacklist_source(item_kind, target=None):
    """What the source pool of one item kind must skip. The arr and Sportarr blacklists are never read here."""
    return repository.blacklist_pairs(item_kind, target)


def blacklist_log_source_guarded(context, provider, subs_id, language):
    """The provider callback's entry: an attribution fault must not take the whole provider search down."""
    try:
        blacklist_log_source(context, provider, subs_id, language)
    except Exception:
        logging.exception('BAZARR could not record the source blacklist for %s release %s', provider, subs_id)
