# coding=utf-8
"""A config change replaces a provider instance. The old one must be terminated.

Every provider holds something: a requests Session at minimum, and for a
Provider Hub provider a worker subprocess plus the threads pumping its stdio.
Dropping the reference releases none of that, so a settings save that touches
one provider's config leaked a worker for the life of the process. Bazarr saves
settings often enough that this accumulates.
"""

import shutil

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


def test_a_failed_replacement_leaves_nothing_installed_and_is_retried(registered, monkeypatch):
    """The stale-config instance goes either way. Leaving it installed would
    keep serving the configuration the user just changed, and the pool
    re-initializes on next access, so a provider that is merely unreachable
    right now recovers on its own with the new config."""
    pool, first = _pool_with_initialized_provider(registered)

    class _Broken(_FakeProvider):
        def initialize(self):
            raise RuntimeError("no")

    monkeypatch.setitem(provider_registry.providers, "fake", _Broken)
    throttled = []
    pool.throttle_callback = lambda name, error: throttled.append(name)

    pool.provider_configs.update({"fake": {"token": "new"}})

    assert throttled == ["fake"]
    assert first.terminated
    assert "fake" not in pool.initialized_providers

    monkeypatch.setitem(provider_registry.providers, "fake", _FakeProvider)
    assert pool["fake"].config["token"] == "new"


def test_the_replacement_is_built_after_the_old_one_is_torn_down(registered, tmp_path):
    """Ordering, and it is not academic: EmbeddedSubtitles.terminate() rmtree's
    a cache directory shared by every instance of it, and initialize() creates
    that same directory. Tearing the old one down last therefore deletes what
    the replacement just set up, and embedded extraction fails until something
    reinitializes the provider."""
    shared = tmp_path / "cache"

    class _OwnsSharedState(_FakeProvider):
        def initialize(self):
            shared.mkdir(exist_ok=True)
            super().initialize()

        def terminate(self):
            shutil.rmtree(shared, ignore_errors=True)
            super().terminate()

    provider_registry.providers["fake"] = _OwnsSharedState
    pool = SZProviderPool(providers=["fake"], provider_configs={"fake": {"token": "old"}})
    assert pool["fake"].initialized

    pool.provider_configs.update({"fake": {"token": "new"}})

    assert shared.is_dir(), "the replacement's shared state was destroyed by the teardown that followed it"


def test_a_teardown_failure_does_not_throttle_the_healthy_replacement(registered, monkeypatch):
    """throttle_callback takes the provider name out of the rotation for the
    throttle interval. The instance being discarded misbehaving on the way out
    is no reason to do that to the one that just started cleanly."""
    class _BadTerminate(_FakeProvider):
        def terminate(self):
            raise RuntimeError("teardown blew up")

    monkeypatch.setitem(provider_registry.providers, "fake", _BadTerminate)
    pool = SZProviderPool(providers=["fake"], provider_configs={"fake": {"token": "old"}})
    assert pool["fake"].initialized
    throttled = []
    pool.throttle_callback = lambda name, error: throttled.append(name)

    pool.provider_configs.update({"fake": {"token": "new"}})

    assert throttled == []
    assert pool.initialized_providers["fake"].config["token"] == "new"
