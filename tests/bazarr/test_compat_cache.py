"""Regression guard for the compat_region cache configuration.

If a future refactor drops the cachetools.LRUCache wiring (e.g. resets
arguments={} on the dogpile region), the underlying dict will grow without
bound. This test pins both the LRU eviction behaviour and the configured
maxsize.
"""
from cachetools import LRUCache

from compat.cache import compat_region


def test_compat_region_is_lru_bounded():
    backend_cache = compat_region.backend._cache
    assert isinstance(backend_cache, LRUCache), (
        f'compat_region backend cache must be an LRUCache, got '
        f'{type(backend_cache).__name__}'
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
        # backend._cache[key] = value, so cachetools.LRUCache enforces the
        # bound on each insert.
        assert len(backend_cache) <= maxsize

    assert len(backend_cache) == maxsize

    # Cleanup so we do not bleed cached entries into other tests.
    for i in range(maxsize + extra):
        compat_region.delete(f'smoke-key-{i}')
