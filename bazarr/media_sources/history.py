# coding=utf-8
"""History and blacklist rows for source items.

Written by the caller that did the work (the search job, a manual download, a sync), never inside
``process_subtitle``. Every row carries snapshots of the item's path and titles and of the source's name, so it
stays meaningful once the item or the source is deleted and its links are NULL.
"""
import logging

from subliminal_patch.score import MAX_SCORES

from . import repository
from .context import SourceItemContext
from .db import lock_source_release, source_transaction


def _snapshot(context: SourceItemContext, item, source):
    title = context.title
    if context.kind != 'sports_event' and item is not None and item.path == context.path:
        title = item.title
    return dict(
        video_path=context.path,
        item_title=title,
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
    # Deletion passes its publication transaction, which reserved the release before it touched the file.
    if target is None:
        with source_transaction() as connection:
            lock_source_release(connection, (context.kind, provider, subs_id))
            return blacklist_log_source(context, provider, subs_id, language, target=connection)
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
