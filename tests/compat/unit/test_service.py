import pytest
from compat import service


def test_guessit_returns_native_dict():
    r = service.guessit_filename("The.Matrix.1999.1080p.BluRay.x264.mkv")
    assert r["title"].lower().startswith("the matrix")
    assert r.get("year") == 1999


def test_guessit_rejects_null_bytes():
    with pytest.raises(ValueError):
        service.guessit_filename("bad\x00file.mkv")


def test_guessit_caps_length():
    with pytest.raises(ValueError):
        service.guessit_filename("x" * 2000)


def test_title_search_cache_is_separate_from_hub_and_title_year_context():
    from compat.cache import build_key
    from subzero.language import Language

    args = ("movie", "tt0133093", None, None, [Language("eng")], ["example"])
    hub = build_key(*args, query="1917")
    title = build_key(*args, query="1917", matching_mode="title", year=2019)
    assert hub != title
    assert title != build_key(*args, query="1917", matching_mode="title", year=2020)


def test_pool_restoration_is_opt_in_lazy_and_preserves_discards(monkeypatch):
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.extensions import provider_registry

    names = ["discover_restore", "discover_discard"]
    for name in names:
        monkeypatch.setitem(provider_registry.providers, name, HubProxyProvider)
    available = []
    monkeypatch.setattr(service, "_compat_pool", None)
    monkeypatch.setattr(service, "get_providers_sorted", lambda: list(available))
    monkeypatch.setattr(service, "get_providers_auth", lambda: {names[0]: {"fixture_setting": "current"}})
    monkeypatch.setattr(service, "get_provider_language_hook", lambda: None)
    pool = service._get_compat_pool()
    pool.discarded_providers.add(names[1])
    available.extend(names)
    # Legacy Hub acquisition does not reconcile or initialize any providers.
    assert service._get_compat_pool() is pool
    assert pool.providers == []
    assert service._get_compat_pool(restore_available=True) is pool
    assert pool.providers == [names[0]]
    assert pool.initialized_providers == {}
    assert pool.provider_configs[names[0]] == {"fixture_setting": "current"}
    assert pool.discarded_providers == {names[1]}
