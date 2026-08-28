# -*- coding: utf-8 -*-
"""Guards for SZProviderPool.__getitem__ provider adoption.

A download can name a provider the pool was not built with, so __getitem__
adopts registered providers instead of raising. These tests pin that behaviour
and the ordered-list shape of pool.providers that it relies on.
"""

import pytest

from subliminal_patch import core


class _FakeProvider:
    languages = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.initialized = False

    @classmethod
    def check(cls, video):
        return True

    def initialize(self):
        self.initialized = True

    def terminate(self):
        pass


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(
        core, "provider_registry", {"alpha": _FakeProvider, "beta": _FakeProvider}
    )


def test_pool_providers_is_an_ordered_list(registry):
    pool = core.SZProviderPool(["beta", "alpha", "beta"], {})

    assert pool.providers == ["beta", "alpha"]

    pool.update(["alpha", "beta"], {}, [], {"must_contain": [], "must_not_contain": []})

    assert pool.providers == ["alpha", "beta"]


def test_pool_getitem_adopts_registered_provider_missing_from_pool(registry):
    pool = core.SZProviderPool(["alpha"], {})

    provider = pool["beta"]

    assert isinstance(provider, _FakeProvider)
    assert provider.initialized
    # appended, not inserted, so the configured priority order survives
    assert pool.providers == ["alpha", "beta"]
    # and it is only initialized once
    assert pool["beta"] is provider
    assert pool.providers == ["alpha", "beta"]


def test_pool_getitem_rejects_unregistered_provider(registry):
    pool = core.SZProviderPool(["alpha"], {})

    with pytest.raises(KeyError):
        pool["nosuchprovider"]

    assert pool.providers == ["alpha"]


def test_pool_getitem_never_resurrects_a_discarded_provider(registry):
    # Absence from pool.providers is how the pool encodes a discarded
    # provider; adoption must not undo that.
    pool = core.SZProviderPool(["alpha"], {})
    pool.discarded_providers.add("beta")

    with pytest.raises(KeyError):
        pool["beta"]

    assert pool.providers == ["alpha"]


def test_pool_getitem_honors_adoption_gate_veto(registry):
    # The gate is the caller's enabled-and-not-throttled check: a registered
    # provider the configuration currently excludes must not be adopted.
    pool = core.SZProviderPool(["alpha"], {}, adoption_gate=lambda name: False)

    with pytest.raises(KeyError):
        pool["beta"]

    assert pool.providers == ["alpha"]
    assert "beta" not in pool.initialized_providers


def test_pool_getitem_adopts_when_gate_allows(registry):
    pool = core.SZProviderPool(["alpha"], {}, adoption_gate=lambda name: name == "beta")

    provider = pool["beta"]

    assert isinstance(provider, _FakeProvider)
    assert pool.providers == ["alpha", "beta"]


def test_pool_getitem_gate_not_consulted_for_configured_providers(registry):
    # The gate only guards adoption; providers the pool was built with
    # initialize regardless (get_providers already filtered them).
    calls = []
    pool = core.SZProviderPool(
        ["alpha"], {}, adoption_gate=lambda name: calls.append(name) or False
    )

    assert isinstance(pool["alpha"], _FakeProvider)
    assert calls == []


def test_excluded_download_neither_throttles_nor_discards(registry):
    # A vetoed adoption during download must be a quiet no-op: routing it
    # through the generic handler would call throttle_callback with an
    # unmapped exception, REPLACING an existing long backoff with the
    # 10-minute default.
    throttled = []
    pool = core.SZProviderPool(
        ["alpha"],
        {},
        throttle_callback=lambda name, exc, ids=None, language=None: throttled.append(name),
        adoption_gate=lambda name: False,
    )
    subtitle = type(
        "FakeSubtitle", (), {"provider_name": "beta", "language": None}
    )()

    assert pool.download_subtitle(subtitle) is False
    assert throttled == []
    assert "beta" not in pool.discarded_providers


def test_excluded_search_neither_throttles_nor_discards(registry):
    throttled = []
    pool = core.SZProviderPool(
        ["alpha"],
        {},
        throttle_callback=lambda name, exc, ids=None, language=None: throttled.append(name),
        adoption_gate=lambda name: False,
    )
    video = type("FakeVideo", (), {})()
    language = core.Language("eng")
    _FakeProvider.languages = {language}
    try:
        assert pool.list_subtitles_provider("beta", video, {language}) is None
    finally:
        _FakeProvider.languages = set()
    assert throttled == []
    assert "beta" not in pool.discarded_providers
