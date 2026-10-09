# coding=utf-8

"""The items whose last translation job failed, held back from re-queueing.

A failed translation leaves nothing the next scan can act on: the history row
and the file are written only on success, so without this record every scan
tick offered the same doomed job again, once per missing item per scan. The
record is in memory, keyed by instance, media type, media id and both languages,
and it expires after a day so a fixed engine is enough to try again even when
no other clear path runs.
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


def record_failed_translation(arr_instance_id, media_type, media_id, from_lang, to_lang):
    """Remember that translating this item failed, until it expires.

    Called from the translation job's failure path, so an item a scan keeps
    seeing missing stops being offered a job that dies the same way.
    """
    with _lock:
        _failures[(arr_instance_id, media_type, media_id, from_lang, to_lang)] = _now()


def translation_recently_failed(arr_instance_id, media_type, media_id, from_lang, to_lang):
    """Whether this item failed to translate within the expiry window."""
    key = (arr_instance_id, media_type, media_id, from_lang, to_lang)
    with _lock:
        failed_at = _failures.get(key)
        if failed_at is None:
            return False
        if _now() - failed_at >= FAILURE_TTL_SECONDS:
            del _failures[key]
            return False
        return True


def clear_failed_translation(arr_instance_id, media_type, media_id, from_lang, to_lang):
    """Forget one failure, because that item's translation succeeded."""
    with _lock:
        _failures.pop((arr_instance_id, media_type, media_id, from_lang, to_lang), None)


def clear_failed_translations():
    """Forget every failure, because the world the record described changed.

    Called when the translator settings or the language profiles are saved:
    a hold that described the old engine or the old rules has nothing left to
    say about the next attempt.
    """
    with _lock:
        _failures.clear()
