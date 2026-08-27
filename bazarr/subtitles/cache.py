# coding=utf-8

import logging
import sys
import time
import threading
import uuid


log = logging.getLogger("subtitle_cache")


_TTL_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Shared backing store
# ---------------------------------------------------------------------------
# HISTORICAL, and kept as a guard rather than as a live workaround.
#
# The process puts BOTH ``/app/bazarr`` (the script dir) and ``/app`` (its
# parent, added by ``bazarr/app/libs.py``) onto ``sys.path``, so this module can
# resolve under two dotted names, ``subtitles.cache`` and
# ``bazarr.subtitles.cache``. Each name produces a separate module object with
# its own ``subtitle_cache = _SubtitleCache()`` singleton, so the UUID stored by
# manual_search() was never found by the queued download job and the user saw
# "Subtitle not found in cache" every time.
#
# What created the second name was ``jobs_queue`` deriving a job's module
# relative to the repository root rather than the package directory, so every
# job was queued as ``bazarr.<x>``. That is fixed, and nothing in the app
# process imports ``bazarr.<x>`` any more.
#
# The shared store stays regardless. It costs nothing, and ``sys.path`` still
# admits the second name, so the split is one stray ``import bazarr.something``
# away. Treat it as a guard against that, not as a description of what the
# process does today.
#
# Solution: stash the actual storage dict on ``sys.modules`` under a stable
# key. Any module copy hands out ``_SubtitleCache`` instances that share that
# single dict, so .store() and .get() always agree.
# ---------------------------------------------------------------------------

_SHARED_STORE_KEY = "_bazarr_subtitle_cache_shared_store"


def _get_shared_store():
    """Return the process-global dict that backs every _SubtitleCache instance.

    Why: ``sys.modules`` is the only object guaranteed to be unique per
    interpreter regardless of how many times this module is re-imported under
    different dotted names. Storing the dict here makes the cache truly
    singleton across import paths.
    What: Lazily creates ``sys.modules[_SHARED_STORE_KEY]`` as ``{}`` and
    returns it.
    Test: Import this module twice under different names and assert that
    ``_get_shared_store() is _get_shared_store()`` (same id).
    """
    return sys.modules.setdefault(_SHARED_STORE_KEY, {})


class _SubtitleCache:
    """Thread-safe TTL cache for subliminal subtitle objects keyed by UUID.

    Why: Manual search returns subtitle objects to the UI as opaque UUIDs;
    the follow-up download request must resolve the UUID back to the original
    object. Storage MUST survive across the two distinct import paths the
    process uses (see module docstring above).
    What: Wraps a shared dict (via :func:`_get_shared_store`) with a lock and
    TTL-based expiry.
    Test: ``c = _SubtitleCache(); k = c.store(obj); assert c.get(k) is obj``;
    instantiate a second ``_SubtitleCache`` and assert it sees the same key.
    """

    def __init__(self):
        # Use the module-level shared dict so every _SubtitleCache instance
        # in this process operates on the same storage.
        self._cache = _get_shared_store()
        self._lock = sys.modules.setdefault("_bazarr_subtitle_cache_shared_lock", threading.Lock())

    def _purge_expired(self):
        now = time.monotonic()
        expired = [k for k, (_, expiry) in list(self._cache.items()) if now >= expiry]
        for k in expired:
            try:
                del self._cache[k]
            except KeyError:
                pass  # already deleted by another instance's concurrent purge

    def store(self, subtitle):
        """Store a subtitle object and return its cache key (UUID string).

        Why: Hands the UI an opaque token so it can ask us to download a
        specific result later without round-tripping the full subtitle object.
        What: Generates a UUID4, stores ``(subtitle, expiry)`` under it, and
        returns the UUID.
        Test: Store any object, assert the returned key is a UUID string, and
        assert ``get(key)`` returns the same object.
        """
        key = str(uuid.uuid4())
        expiry = time.monotonic() + _TTL_SECONDS
        with self._lock:
            self._purge_expired()
            self._cache[key] = (subtitle, expiry)
            log.debug("CACHE store key=%s total_keys=%d id(cache)=%s module=%s",
                      key, len(self._cache), id(self._cache), __name__)
        return key

    def get(self, key):
        """Return the subtitle object for the given key, or None if not found/expired.

        Why: Resolves the opaque UUID handed to the UI back into the original
        subtitle object so the download path can act on it.
        What: Looks up ``key`` in the shared store, returns the subtitle if
        non-expired, otherwise deletes the entry and returns ``None``.
        Test: ``c.get(c.store(obj)) is obj``; with TTL set to 0,
        ``c.get(c.store(obj))`` returns ``None``.
        """
        with self._lock:
            entry = self._cache.get(key)
            found = entry is not None
            log.debug(
                "CACHE get key=%s found=%s total_keys=%d id(cache)=%s module=%s",
                key, found, len(self._cache), id(self._cache), __name__,
            )
            if entry is None:
                return None
            subtitle, expiry = entry
            if time.monotonic() >= expiry:
                del self._cache[key]
                return None
            return subtitle


subtitle_cache = _SubtitleCache()
