# coding=utf-8

"""The items whose last translation job failed, held back from re-queueing.

A failed translation leaves nothing the next scan can act on: the history row
and the file are written only on success, so without this record every scan
tick offered the same doomed job again, once per missing item per scan. The
record is in memory, keyed by instance, media type, media id, both languages
and the forced and hearing-impaired variants, and it expires after a day so a
fixed engine is enough to try again even when no other clear path runs.

The variant is part of the key because one variant failing says nothing about
its siblings: a plain translation that died must not hold back the forced and
hearing-impaired translations of the same item.
"""

import threading
import time


# A hold older than this has described a world the current settings may no
# longer match, so the next scan may offer the item again.
FAILURE_TTL_SECONDS = 24 * 60 * 60

_lock = threading.Lock()
_failures = {}


def _now():
    return time.time()


def _key(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi, source_created_at):
    key = (arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi)
    if media_type != 'source':
        return key
    if source_created_at is None:
        from media_sources.repository import get_item

        item = get_item(media_id)
        source_created_at = item.created_at if item is not None else None
    return (*key, source_created_at)


def record_failed_translation(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi,
                              *, source_created_at=None):
    """Remember that translating this variant of this item failed, until it expires.

    Called from the translation job's failure path, so an item a scan keeps
    seeing missing stops being offered a job that dies the same way.
    """
    key = _key(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi, source_created_at)
    with _lock:
        _failures[key] = _now()


def translation_recently_failed(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi,
                                *, source_created_at=None):
    """Whether this variant of this item failed to translate within the expiry window."""
    key = _key(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi, source_created_at)
    with _lock:
        failed_at = _failures.get(key)
        if failed_at is None:
            return False
        if _now() - failed_at >= FAILURE_TTL_SECONDS:
            del _failures[key]
            return False
        return True


def clear_failed_translation(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi,
                             *, source_created_at=None):
    """Forget one failure, because that variant's translation succeeded."""
    key = _key(arr_instance_id, media_type, media_id, from_lang, to_lang, forced, hi, source_created_at)
    with _lock:
        _failures.pop(key, None)


def clear_failed_translations():
    """Forget every failure, because the world the record described changed.

    Called when the translator settings or the language profiles are saved:
    a hold that described the old engine or the old rules has nothing left to
    say about the next attempt.
    """
    with _lock:
        _failures.clear()
