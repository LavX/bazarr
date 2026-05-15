"""Thread-safe MutableMapping wrapper around cachetools.LRUCache.

cachetools.LRUCache is built on top of an OrderedDict and mutates the
underlying linked list on every read (the LRU "touch") as well as on
writes. Bazarr serves requests via Waitress with threads=100, and the
shared dogpile memory regions are hit from those request threads.
dogpile's per-key mutex covers get_or_create() but bypasses set() and
delete(), so we cannot rely on it to serialise access to the LRU
structure itself.

The lock is a single threading.Lock rather than per-key because the LRU
mutation is global to the structure (eviction can rewire any node).
"""
from __future__ import annotations

import threading
from collections.abc import MutableMapping

from cachetools import LRUCache


class LockedLRU(MutableMapping):
    """Thread-safe MutableMapping wrapper around cachetools.LRUCache."""

    __slots__ = ('_cache', '_lock')

    def __init__(self, maxsize):
        self._cache = LRUCache(maxsize=maxsize)
        self._lock = threading.Lock()

    def __getitem__(self, key):
        with self._lock:
            return self._cache[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._cache[key] = value

    def __delitem__(self, key):
        with self._lock:
            del self._cache[key]

    def __iter__(self):
        with self._lock:
            return iter(list(self._cache.keys()))

    def __len__(self):
        with self._lock:
            return len(self._cache)

    def __contains__(self, key):
        with self._lock:
            return key in self._cache

    def get(self, key, default=None):
        with self._lock:
            return self._cache.get(key, default)

    def pop(self, key, *args):
        with self._lock:
            return self._cache.pop(key, *args)

    @property
    def maxsize(self):
        return self._cache.maxsize

    @property
    def currsize(self):
        with self._lock:
            return self._cache.currsize
