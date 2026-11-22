# coding=utf-8
"""What a search pool does when the plugin it is searching is detached from the provider registry while it runs.

The registry hands out None for a name it does not hold, so a pool that looked a class up twice could find it gone the
second time. A search looks its class up once, and a provider name with no class is excluded quietly.
"""
import threading
import time

import pytest

from subzero.language import Language


@pytest.fixture
def registry():
    from provider_hub import registry as hub_registry
    from subliminal_patch.extensions import provider_registry

    before = {name: provider_registry[name] for name in provider_registry}
    withdrawn = set(hub_registry._WITHDRAWN_PROVIDER_IDS)
    yield provider_registry
    for name in provider_registry:
        provider_registry.unregister(name)
    for name, provider_class in before.items():
        provider_registry.register(name, provider_class)
    hub_registry._WITHDRAWN_PROVIDER_IDS.clear()
    hub_registry._WITHDRAWN_PROVIDER_IDS.update(withdrawn)


def _movie():
    from subliminal.video import Movie

    return Movie("movie.mkv", "Movie", year=2000)


def _plugin(name="gone", **attrs):
    from provider_hub.registry import HubProxyProvider

    return type("Plugin", (HubProxyProvider,), {"provider_name": name, "languages": {Language("eng")}, **attrs})


def _pool(*names):
    from subliminal_patch.core import SZProviderPool

    return SZProviderPool(providers=list(names), provider_configs={})


def test_a_name_with_no_class_is_excluded_when_the_pool_builds_it_and_not_a_call_on_none(registry):
    from subliminal_patch.core import ProviderExcludedError

    pool = _pool("gone")

    with pytest.raises(ProviderExcludedError):
        pool._build_and_install("gone", lambda: {})
    assert "gone" not in pool.initialized_providers


def test_a_search_for_a_provider_that_is_not_registered_is_skipped_quietly(registry):
    result = _pool("gone").list_subtitles_provider("gone", _movie(), {Language("eng")}, detailed=True)

    assert (result.status, result.reason) == ("skipped", "provider_excluded")


def test_a_search_uses_the_class_it_started_with_when_the_registry_loses_it_half_way(registry):
    """The plugin leaves the registry while the search is checking what the provider supports. The search holds the
    class it looked up, so every check after that answers from it, and the search ends as an exclusion at the point
    it needs an instance, not as an error."""
    seen = []

    def check(cls, video):
        registry.unregister("gone")  # detached while the search is running
        seen.append("checked")
        return True

    plugin = _plugin(check=classmethod(check))
    registry.register("gone", plugin)

    result = _pool("gone").list_subtitles_provider("gone", _movie(), {Language("eng")}, detailed=True)

    assert seen == ["checked"]
    assert (result.status, result.reason) == ("skipped", "provider_excluded")


def test_an_instance_being_built_while_its_class_leaves_is_dropped_and_the_build_is_excluded(registry):
    from subliminal_patch.core import ProviderExcludedError

    entered, proceed, terminated = threading.Event(), threading.Event(), []

    from provider_hub.registry import HubProxyProvider

    class Slow(HubProxyProvider):
        provider_name = "gone"
        languages = {Language("eng")}

        def __init__(self, **config):
            super().__init__(**config)
            entered.set()
            proceed.wait(10)

        def terminate(self):
            terminated.append(self)

    registry.register("gone", Slow)
    pool = _pool("gone")
    outcome = {}

    def build():
        try:
            outcome["value"] = pool["gone"]
        except BaseException as error:  # the test reads it
            outcome["error"] = error

    thread = threading.Thread(target=build, daemon=True)
    thread.start()
    assert entered.wait(10)

    # The plugin is detached while its class is being constructed: what the pool was building is dropped, and the
    # build starts over from a registry that has nothing to build.
    from provider_hub.registry import retire_provider_instances, unregister_provider_class

    unregister_provider_class("gone")
    retire_provider_instances("gone")
    proceed.set()
    thread.join(10)

    assert isinstance(outcome.get("error"), ProviderExcludedError)
    assert len(terminated) == 1 and "gone" not in pool.initialized_providers


def test_the_pools_admission_check_refuses_a_provider_that_was_detached_and_not_one_that_was_never_there(registry):
    from app import get_providers
    from provider_hub import registry as hub_registry

    class Settings:
        enabled_providers = ["gone", "other"]

    hub_registry._WITHDRAWN_PROVIDER_IDS.add("gone")
    original = get_providers.settings.general
    get_providers.settings.general = Settings()
    try:
        assert get_providers.provider_is_usable("gone") is False
        # Registered again (a release took its place): usable once more.
        registry.register("gone", _plugin())
        assert get_providers.provider_is_usable("gone") is True
        assert get_providers.provider_is_usable("other") is True
    finally:
        get_providers.settings.general = original


def test_the_registry_is_safe_to_walk_while_plugins_are_detached(registry):
    stop = threading.Event()
    errors = []

    def churn():
        index = 0
        while not stop.is_set():
            name = f"churn{index % 50}"
            registry.register(name, object)
            registry.unregister(name)
            index += 1

    thread = threading.Thread(target=churn, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                list(registry)
                registry.names()
            except Exception as error:  # a changed-size error is what this guards against
                errors.append(error)
    finally:
        stop.set()
        thread.join(5)

    assert errors == []
