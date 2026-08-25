# coding=utf-8
"""A config change replaces a provider instance. The old one must be terminated.

Every provider holds something: a requests Session at minimum, and for a
Provider Hub provider a worker subprocess plus the threads pumping its stdio.
Dropping the reference releases none of that, so a settings save that touches
one provider's config leaked a worker for the life of the process. Bazarr saves
settings often enough that this accumulates.
"""

import pytest

from subliminal_patch.core import SZProviderPool
from subliminal_patch.extensions import provider_registry


class _FakeProvider:
    """Stands in for a provider that owns something worth releasing."""

    instances = []

    def __init__(self, **config):
        self.config = config
        self.initialized = False
        self.terminated = False
        _FakeProvider.instances.append(self)

    def initialize(self):
        self.initialized = True

    def terminate(self):
        self.terminated = True


@pytest.fixture
def registered(monkeypatch):
    _FakeProvider.instances = []
    monkeypatch.setitem(provider_registry.providers, "fake", _FakeProvider)
    yield _FakeProvider


def _pool_with_initialized_provider(registered):
    pool = SZProviderPool(providers=["fake"], provider_configs={"fake": {"token": "old"}})
    first = pool["fake"]
    assert first.initialized
    return pool, first


def test_a_config_change_terminates_the_provider_it_replaces(registered):
    pool, first = _pool_with_initialized_provider(registered)

    pool.provider_configs.update({"fake": {"token": "new"}})

    assert first.terminated, (
        "the replaced provider was dropped without terminate(); a Provider Hub "
        "provider would leave its worker process and stdio threads behind"
    )
    assert pool["fake"] is not first
    assert pool["fake"].config["token"] == "new"


def test_a_provider_that_was_never_initialized_is_not_terminated(registered):
    pool = SZProviderPool(providers=["fake"], provider_configs={"fake": {"token": "old"}})

    pool.provider_configs.update({"fake": {"token": "new"}})

    # Nothing was running, so nothing should have been built just to tear down.
    assert [p for p in registered.instances if p.terminated] == []


def test_an_unchanged_config_leaves_the_running_provider_alone(registered):
    pool, first = _pool_with_initialized_provider(registered)

    pool.provider_configs.update({"fake": {"token": "old"}})

    assert not first.terminated
    assert pool["fake"] is first


def test_a_failed_replacement_keeps_the_running_provider(registered, monkeypatch):
    pool, first = _pool_with_initialized_provider(registered)

    class _Broken(_FakeProvider):
        def initialize(self):
            raise RuntimeError("no")

    monkeypatch.setitem(provider_registry.providers, "fake", _Broken)
    throttled = []
    pool.throttle_callback = lambda name, error: throttled.append(name)

    pool.provider_configs.update({"fake": {"token": "new"}})

    assert throttled == ["fake"]
    assert not first.terminated, "the working provider was torn down for a replacement that never started"
    assert pool.initialized_providers["fake"] is first
