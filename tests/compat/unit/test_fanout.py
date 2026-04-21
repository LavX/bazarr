import time
from unittest.mock import MagicMock


def test_list_all_subtitles_parallel_as_completed_short_circuits_slow():
    """A slow provider must not starve the wall-timeout."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    fast_subs = [MagicMock(provider_name="fast", language=MagicMock())]
    slow_subs = [MagicMock(provider_name="slow", language=MagicMock())]

    def list_fn(provider, video, languages):
        if provider == "slow":
            time.sleep(2.5)  # exceeds per-provider timeout and wall timeout
        return slow_subs if provider == "slow" else fast_subs

    pool.providers = ["fast", "slow"]
    pool.discarded_providers = set()
    pool.list_subtitles_provider.side_effect = list_fn

    video = MagicMock()
    t0 = time.time()
    results = list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1,
        wall_timeout=2,
    )
    elapsed = time.time() - t0
    assert elapsed < 3, "wall timeout must kick in"
    # "fast" provider's subtitles present
    assert any(getattr(s, "provider_name", "") == "fast" for s in results[video])
