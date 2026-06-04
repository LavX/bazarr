"""only_providers (allow-list) resolution inside the fanout.

The allow-list is applied as an exclusion (everything-not-in-the-list is
excluded) so it can only NARROW. The operator's per-key exclusions, the health
discards, and the virtual-video skip list are always subtracted on top, so an
allow-list can never reach a provider those vetoed. These tests capture the
exact exclude set handed to list_all_subtitles_parallel.
"""
import types
from collections import defaultdict
from unittest.mock import MagicMock

import pytest

from compat import service


@pytest.fixture
def capture_exclude(monkeypatch):
    """Patch the heavy fanout deps; return a dict that records the exclude set
    passed to the parallel fanout for the most recent _do_fanout call."""
    captured = {}

    def _make(pool_providers):
        pool = MagicMock()
        pool.providers = list(pool_providers)
        pool.discarded_providers = set()
        monkeypatch.setattr(service, "_get_compat_pool", lambda: pool)
        # Virtual video with a non-existent file path: video_has_file is False
        # but os.path.exists() gets a real string (no MagicMock TypeError).
        monkeypatch.setattr(service, "_build_video",
                             lambda *a, **k: types.SimpleNamespace(name="/no/such/file.mkv"))
        health = MagicMock()
        health.currently_discarded.return_value = set()
        monkeypatch.setattr("subliminal_patch.provider_health.get_tracker",
                            lambda: health)
        from app.config import settings
        settings["compat_endpoint"]["serve_local_subs"] = False

        def _fake_parallel(videos, languages, pool_instance, **kw):
            captured["exclude"] = set(kw.get("exclude_providers") or ())
            return defaultdict(list)

        monkeypatch.setattr(service, "list_all_subtitles_parallel", _fake_parallel)
        return captured

    return _make


def test_only_providers_excludes_everything_else(capture_exclude):
    captured = capture_exclude(["p1", "p2", "p3"])
    service._do_fanout("tt1", None, None, [], "movie",
                       only_providers=["p1"], timeout_seconds=8)
    # p1 is kept (selected); the rest of the pool is excluded.
    assert "p1" not in captured["exclude"]
    assert {"p2", "p3"} <= captured["exclude"]


def test_per_key_exclusion_vetoes_the_allow_list(capture_exclude):
    """If a provider is both allow-listed AND excluded, exclusion wins."""
    captured = capture_exclude(["p1", "p2", "p3"])
    service._do_fanout("tt1", None, None, [], "movie",
                       only_providers=["p1"], exclude_providers=["p1"],
                       timeout_seconds=8)
    # Every provider ends up excluded: p1 by the explicit exclude, p2/p3 by the
    # allow-list narrowing. The operator's veto is honored -> empty fanout.
    assert {"p1", "p2", "p3"} <= captured["exclude"]


def test_unknown_only_provider_yields_full_exclusion(capture_exclude):
    """An allow-list that matches no real provider excludes the whole pool
    (contract-safe: the caller gets an empty result, never a 500)."""
    captured = capture_exclude(["p1", "p2"])
    service._do_fanout("tt1", None, None, [], "movie",
                       only_providers=["does-not-exist"], timeout_seconds=8)
    assert {"p1", "p2"} <= captured["exclude"]


def test_no_only_providers_leaves_pool_unrestricted(capture_exclude):
    """Without an allow-list, no provider is excluded on its account."""
    captured = capture_exclude(["p1", "p2", "p3"])
    service._do_fanout("tt1", None, None, [], "movie", timeout_seconds=8)
    assert captured["exclude"].isdisjoint({"p1", "p2", "p3"})
