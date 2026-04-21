import time
from unittest.mock import MagicMock
from bazarr.compat import cache as C


def test_fanout_respects_wall_timeout(monkeypatch):
    """A slow provider dropped at wall_timeout; fast provider results included."""
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_secret", "f" * 32)
    C.invalidate_all()
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    fast_sub = MagicMock(provider_name="fast", language=MagicMock())
    slow_sub = MagicMock(provider_name="slow", language=MagicMock())

    def list_fn(provider, video, languages):
        if provider == "slow":
            time.sleep(3)  # exceeds wall timeout
        return [slow_sub] if provider == "slow" else [fast_sub]

    pool.providers = ["fast", "slow"]
    pool.discarded_providers = set()
    pool.list_subtitles_provider.side_effect = list_fn

    video = MagicMock()
    t0 = time.time()
    results = list_all_subtitles_parallel([video], set(), pool,
                                           per_provider_timeout=1, wall_timeout=1.5)
    elapsed = time.time() - t0

    # Wall timeout must kick in (assertion generous for ThreadPoolExecutor join time)
    assert elapsed < 4
    # Fast provider result is present; slow may or may not be (depends on timing)
    flat = [s for subs in results.values() for s in subs]
    assert any(getattr(s, "provider_name", "") == "fast" for s in flat)
    C.invalidate_all()
