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


def _snapshot(context: SourceItemContext, target=None):
    item = repository.get_item(context.item_id, target)
    source = repository.get_source(context.source_id, target)
    return dict(
        video_path=context.path,
        item_title=item.title if item is not None else context.title,
        group_title=context.group_title,
        source_name=source.name if source is not None else None,
    )


def _links(context, target):
    """The links to store: an item or source deleted since the context was built is stored as NULL."""
    item_id = context.item_id if repository.get_item(context.item_id, target) is not None else None
    source_id = context.source_id if repository.get_source(context.source_id, target) is not None else None
    return item_id, source_id


def history_log_source(action, context: SourceItemContext, result, fake_provider=None, fake_score=None,
                       upgraded_from_id=None, target=None):
    """Record what happened to a source item. ``result`` is a ``ProcessSubtitlesResult``."""
    provider = fake_provider or result.provider
    score = fake_score or result.score
    ai_translated = (1 if getattr(result, 'ai_translated', False) is True else 0) if action in (1, 2, 3) else None
    full = MAX_SCORES['episode' if context.kind == 'episode' else 'movie']
    item_id, source_id = _links(context, target)
    snapshot = _snapshot(context, target)
    snapshot['video_path'] = result.path or snapshot['video_path']
    return repository.insert_history(
        target, item_id=item_id, source_id=source_id, item_kind=context.kind, action=action,
        description=result.message, language=result.language_code, provider=provider,
        ai_translated=ai_translated, score=score,
        score_out_of=full if score or (ai_translated == 1 and score == 0) else None,
        subs_id=result.subs_id, subtitles_path=result.subs_path,
        matched=str(result.matched) if result.matched else None,
        not_matched=str(result.not_matched) if result.not_matched else None,
        upgradedFromId=upgraded_from_id, **snapshot)


def blacklist_log_source(context: SourceItemContext, provider, subs_id, language, target=None):
    item_id, source_id = _links(context, target)
    return repository.insert_blacklist(
        target, item_id=item_id, source_id=source_id, item_kind=context.kind, provider=provider,
        subs_id=subs_id, language=language, **_snapshot(context, target))


def get_blacklist_source(item_kind, target=None):
    """What the source pool of one item kind must skip. The arr and Sportarr blacklists are never read here."""
    return repository.blacklist_pairs(item_kind, target)


def blacklist_log_source_guarded(context, provider, subs_id, language):
    """The provider callback's entry: an attribution fault must not take the whole provider search down."""
    try:
        blacklist_log_source(context, provider, subs_id, language)
    except Exception:
        logging.exception('BAZARR could not record the source blacklist for %s release %s', provider, subs_id)
