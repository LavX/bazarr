"""Regression guard for the compat_region cache configuration.

If a future refactor drops the LockedLRU/cachetools wiring (e.g. resets
arguments={} on the dogpile region), the underlying dict will grow without
bound or, worse, lose its thread-safety. These tests pin both the LRU
eviction behaviour and the configured maxsize, plus assert no corruption
under concurrent access from many threads.
"""
import concurrent.futures

import pytest
from cachetools import LRUCache

from compat.cache import compat_region
from utilities.locked_lru import LockedLRU


@pytest.fixture(autouse=True)
def _isolate_compat_region():
    """Hard-invalidate the region before AND after each test so failures
    do not leak smoke keys into the next test."""
    compat_region.invalidate(hard=True)
    yield
    compat_region.invalidate(hard=True)


def test_compat_region_is_lru_bounded():
    backend_cache = compat_region.backend._cache
    assert isinstance(backend_cache, LockedLRU), (
        f'compat_region backend cache must be a LockedLRU, got '
        f'{type(backend_cache).__name__}'
    )
    # And the inner cache stays a cachetools LRUCache so we still get LRU
    # eviction semantics rather than e.g. an unbounded dict.
    assert isinstance(backend_cache._cache, LRUCache), (
        f'LockedLRU must wrap a cachetools.LRUCache, got '
        f'{type(backend_cache._cache).__name__}'
    )

    maxsize = backend_cache.maxsize
    assert maxsize == 2048, (
        f'compat_region LRU maxsize must stay at 2048, got {maxsize}'
    )

    # Insert well past maxsize and confirm eviction kicks in. We use the
    # region API rather than poking the dict directly so any future change
    # to how dogpile stores values still flows through this assertion.
    extra = 64
    for i in range(maxsize + extra):
        compat_region.set(f'smoke-key-{i}', i)
        # The size invariant must hold after every single set, not just at
        # the end. dogpile.cache.memory MemoryBackend stores values via
        # backend._cache[key] = value, so the LRU enforces the bound on
        # each insert.
        assert len(backend_cache) <= maxsize

    assert len(backend_cache) == maxsize


def test_compat_region_thread_safety_under_concurrent_load():
    """Spawn N workers each doing tight set/get/delete loops against the
    same region. Asserts no exception, no crash, final size <= maxsize.
    This protects against future regressions where the underlying cache
    is swapped to something not lock-protected.

    Without LockedLRU, cachetools.LRUCache mutates the OrderedDict-based
    linked list on every read (the LRU touch). Concurrent threads would
    typically surface as KeyError on a key that was just inserted, or
    RuntimeError 'dictionary changed size during iteration'.
    """
    n_workers = 16
    ops_per_worker = 500
    errors = []

    def worker(worker_id):
        try:
            for i in range(ops_per_worker):
                k = f"thread-{worker_id}-{i % 50}"
                compat_region.set(k, {"w": worker_id, "i": i})
                compat_region.get(k)
                if i % 7 == 0:
                    compat_region.delete(k)
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(worker, w) for w in range(n_workers)]
        for f in futures:
            f.result()

    assert errors == [], f"thread-safety violations: {errors[:3]}"
    assert len(compat_region.backend._cache) <= 2048
