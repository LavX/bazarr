# coding=utf-8
"""One FlareSolverr address for every provider that can use it."""

import copy
import sys
from types import SimpleNamespace

import pytest

from provider_hub import endpoints
from provider_hub.kinds.subtitle_provider import apply_settings_overlay

GLOBAL_URL = "http://solver.lan:8191"
FIELD = "flaresolverr_url"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv(endpoints.FLARESOLVERR_ENV, raising=False)
    monkeypatch.setattr(endpoints, "_sibling_cache", {"checked_at": None, "url": None})
    monkeypatch.setattr(endpoints, "_probe_flight", None)
    yield
    _join_background_threads()


def _join_background_threads():
    """Every probe and refresh thread this module starts ends before the next test."""
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith("flaresolverr-") and thread is not threading.current_thread():
            thread.join(5)
            assert not thread.is_alive(), f"{thread.name} did not finish"


def _installation(provider_id, properties=None, trusted=False, manifest_extra=None):
    manifest = {"config_schema": {"type": "object", "properties": (
        {FIELD: {"type": "string", "default": ""}, "apikey": {"type": "string"}}
        if properties is None else properties)}}
    manifest.update(manifest_extra or {})
    return SimpleNamespace(provider_id=provider_id, manifest=manifest, trusted=trusted)


def _overlay(monkeypatch, installations, stored, section_settings, blocked=frozenset()):
    import provider_hub.service as hub_service
    import provider_hub.state as hub_state

    monkeypatch.setattr(hub_state, "running_installations", lambda: list(installations))
    monkeypatch.setattr(hub_service, "runtime_provider_configs", lambda: copy.deepcopy(stored))
    configs = {}
    apply_settings_overlay(configs, section_settings, set(), set(blocked))
    return configs


def _settings(url=GLOBAL_URL, **sections):
    return {"endpoints": {endpoints.FLARESOLVERR_SETTING_KEY: url}, **sections}


class TestResolution:
    def test_setting_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv(endpoints.FLARESOLVERR_ENV, "http://from-env:8191")
        assert endpoints.flaresolverr_url_source(_settings()) == (GLOBAL_URL, "setting")

    def test_environment_is_used_when_the_setting_is_empty(self, monkeypatch):
        monkeypatch.setenv(endpoints.FLARESOLVERR_ENV, " http://from-env:8191 ")
        assert endpoints.flaresolverr_url_source(_settings("")) == ("http://from-env:8191", "env")

    def test_nothing_set_resolves_to_nothing_and_never_probes(self, monkeypatch):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr",
                            lambda url: pytest.fail("resolution must not probe"))
        assert endpoints.flaresolverr_url_source(_settings("")) == ("", "none")
        assert endpoints.flaresolverr_url_source({}) == ("", "none")

    @pytest.mark.parametrize("bad", ["solver:8191", "ftp://solver", "http://", "   ", None, 5])
    def test_an_unusable_address_is_ignored(self, bad):
        assert endpoints.flaresolverr_url_source(_settings(bad)) == ("", "none")


class TestFill:
    def test_an_empty_field_of_a_trusted_installation_is_filled(self, monkeypatch):
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)],
                           {"alpha": {FIELD: ""}}, _settings())
        assert configs["alpha"][FIELD] == GLOBAL_URL

    def test_a_missing_stored_config_is_filled_too(self, monkeypatch):
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)], {}, _settings())
        assert configs["alpha"][FIELD] == GLOBAL_URL

    def test_an_untrusted_plugin_whose_manifest_is_flagged_is_filled(self, monkeypatch):
        configs = _overlay(monkeypatch,
                           [_installation("alpha", manifest_extra={"flaresolverr": True})],
                           {}, _settings())
        assert configs["alpha"][FIELD] == GLOBAL_URL

    def test_an_untrusted_unflagged_plugin_never_gets_the_address(self, monkeypatch):
        configs = _overlay(monkeypatch, [_installation("alpha")], {"alpha": {FIELD: ""}}, _settings())
        assert configs["alpha"][FIELD] == ""

    @pytest.mark.parametrize("flag", [False, "true", 1, None])
    def test_only_a_literal_true_flag_counts(self, monkeypatch, flag):
        configs = _overlay(monkeypatch,
                           [_installation("alpha", manifest_extra={"flaresolverr": flag})],
                           {}, _settings())
        assert configs["alpha"][FIELD] == ""

    def test_a_value_stored_for_the_provider_is_kept(self, monkeypatch):
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)],
                           {"alpha": {FIELD: "http://own:8191"}}, _settings())
        assert configs["alpha"][FIELD] == "http://own:8191"

    def test_a_value_typed_in_the_plugin_section_is_kept(self, monkeypatch):
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)],
                           {"alpha": {FIELD: ""}}, _settings(alpha={FIELD: "http://section:8191"}))
        assert configs["alpha"][FIELD] == "http://section:8191"

    def test_a_schema_default_that_is_not_empty_is_kept(self, monkeypatch):
        properties = {FIELD: {"type": "string", "default": "http://default:8191"}}
        configs = _overlay(monkeypatch, [_installation("alpha", properties, trusted=True)], {}, _settings())
        assert configs["alpha"][FIELD] == "http://default:8191"

    def test_a_schema_without_the_field_is_untouched(self, monkeypatch):
        properties = {"apikey": {"type": "string"}}
        configs = _overlay(monkeypatch, [_installation("alpha", properties, trusted=True)],
                           {"alpha": {"apikey": "k"}}, _settings())
        assert configs["alpha"] == {"apikey": "k"}

    def test_an_eligible_empty_field_is_set_to_an_empty_string_when_no_address_exists(self, monkeypatch):
        properties = {FIELD: {"type": "string"}}
        configs = _overlay(monkeypatch, [_installation("alpha", properties, trusted=True)], {}, _settings(""))
        assert configs["alpha"] == {FIELD: ""}

    def test_clearing_the_address_reaches_a_pool_that_still_holds_the_old_one(self, monkeypatch):
        """A pool merges a partial config over what it has, so an absent key keeps the old address."""
        from subliminal_patch.core import SZProviderPool
        from subliminal_patch.extensions import provider_registry

        class Provider:
            # The class registered under the id stands for the plugin's proxy.
            hub_proxy = True

            def __init__(self, **config):
                self.config = config

            def initialize(self):
                pass

            def terminate(self):
                pass

        monkeypatch.setitem(provider_registry, "alpha", Provider)
        properties = {FIELD: {"type": "string"}}
        installations = [_installation("alpha", properties, trusted=True)]
        pool = SZProviderPool(providers=["alpha"], provider_configs=_overlay(
            monkeypatch, installations, {}, _settings(GLOBAL_URL)))
        assert pool.provider_configs["alpha"][FIELD] == GLOBAL_URL

        pool.provider_configs.update(_overlay(monkeypatch, installations, {}, _settings("")))

        assert pool.provider_configs["alpha"][FIELD] == ""
        assert pool["alpha"].config[FIELD] == ""

    def test_a_blocked_provider_is_skipped(self, monkeypatch):
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)],
                           {"alpha": {FIELD: ""}}, _settings(), blocked={"alpha"})
        assert configs["alpha"][FIELD] == ""

    def test_the_environment_address_is_used_when_no_setting_is_saved(self, monkeypatch):
        monkeypatch.setenv(endpoints.FLARESOLVERR_ENV, "http://from-env:8191")
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)], {}, _settings(""))
        assert configs["alpha"][FIELD] == "http://from-env:8191"

    def test_with_no_address_every_config_is_byte_identical(self, monkeypatch):
        installations = [_installation("alpha", trusted=True), _installation("beta")]
        stored = {"alpha": {FIELD: "", "apikey": "a"}, "beta": {"apikey": "b"}}
        with_setting_off = _overlay(monkeypatch, installations, stored, _settings(""))
        baseline = _overlay(monkeypatch, installations, stored, {})
        assert repr(with_setting_off) == repr(baseline)

    def test_unrelated_fields_and_other_plugins_are_not_changed(self, monkeypatch):
        installations = [
            _installation("alpha", trusted=True),
            _installation("plain", {"apikey": {"type": "string"}}, trusted=True),
            _installation("owned", trusted=True),
        ]
        stored = {"alpha": {"apikey": "a"}, "plain": {"apikey": "p"}, "owned": {FIELD: "http://own:8191"}}
        configs = _overlay(monkeypatch, installations, stored, _settings())
        assert configs["alpha"] == {FIELD: GLOBAL_URL, "apikey": "a"}
        assert configs["plain"] == {"apikey": "p"}
        assert configs["owned"][FIELD] == "http://own:8191"


class TestPluginIdNamespace:
    def test_endpoints_is_a_reserved_section_no_plugin_can_claim(self):
        from provider_hub.manifest import ManifestValidationError, _validate_provider_id, reserved_settings_sections

        assert endpoints.ENDPOINTS_SECTION in reserved_settings_sections()
        with pytest.raises(ManifestValidationError):
            _validate_provider_id(endpoints.ENDPOINTS_SECTION, set())

    def test_no_installed_catalog_or_registered_plugin_id_is_endpoints(self):
        from provider_hub import state as hub_state

        ids = {item.provider_id for item in hub_state.running_installations(kind=None)}
        assert endpoints.ENDPOINTS_SECTION not in ids


class TestValidator:
    @staticmethod
    def _check(value):
        from app import config
        from dynaconf import Dynaconf
        from dynaconf.validator import ValidationError

        validator = next(item for item in config.validators
                         if endpoints.FLARESOLVERR_SETTING_PATH in item.names)
        fake = Dynaconf(core_loaders=[], endpoints={endpoints.FLARESOLVERR_SETTING_KEY: value})
        try:
            validator.validate(fake)
        except ValidationError:
            return False
        return True

    @pytest.mark.parametrize("value", ["", "http://flaresolverr:8191", "https://solver.lan/path"])
    def test_accepts_empty_and_http_addresses(self, value):
        assert self._check(value)

    @pytest.mark.parametrize("value", ["solver:8191", "ftp://solver", "http://", " http://solver:8191"])
    def test_refuses_anything_else(self, value):
        assert not self._check(value)

    def test_unset_defaults_to_empty(self):
        from app import config
        from dynaconf import Dynaconf

        validator = next(item for item in config.validators
                         if endpoints.FLARESOLVERR_SETTING_PATH in item.names)
        fake = Dynaconf(core_loaders=[])
        validator.validate(fake)
        assert fake.endpoints.flaresolverr_v1_url == ""


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


class TestSave:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        from app import config
        from dynaconf import Dynaconf
        from dynaconf.loaders.yaml_loader import write
        from secret_store import encrypt_settings_dict

        path = tmp_path / "config.yaml"
        initial = config.settings.as_dict()
        initial["ENDPOINTS"] = {endpoints.FLARESOLVERR_SETTING_KEY: ""}
        write(str(path), encrypt_settings_dict({key.lower(): value for key, value in initial.items()}),
              merge=False)
        fake = Dynaconf(settings_file=str(path), core_loaders=["YAML"])
        config.decrypt_settings_in_place(fake)
        monkeypatch.setattr(config, "settings", fake)
        monkeypatch.setattr(config, "config_yaml_file", str(path))
        monkeypatch.setattr(config, "_force_first_save_migration", False)
        monkeypatch.setattr(config, "validate_log_regex", lambda: None)
        monkeypatch.setattr(config, "_unfinished_login_resets", {"caches": set(), "throttles": False})
        events = []
        real_write = config.write_config

        def recording_write():
            result = real_write()
            events.append(("write", fake.endpoints.flaresolverr_v1_url))
            return result

        monkeypatch.setattr(config, "write_config", recording_write)
        monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: None),
            update=lambda model: _FakeUpdate(), System=object))
        monkeypatch.setitem(sys.modules, "compat.service", SimpleNamespace(
            reset_compat_pool=lambda: events.append(("reset", fake.endpoints.flaresolverr_v1_url))))
        return SimpleNamespace(config=config, events=events, settings=fake)

    def test_saving_the_address_resets_the_pool_once_after_the_write(self, env):
        env.config.save_settings([(endpoints.FLARESOLVERR_FORM_KEY, [GLOBAL_URL])])

        assert env.settings.endpoints.flaresolverr_v1_url == GLOBAL_URL
        resets = [event for event in env.events if event[0] == "reset"]
        assert resets == [("reset", GLOBAL_URL)]
        assert env.events.index(resets[0]) > max(
            index for index, event in enumerate(env.events) if event[0] == "write")

    def test_a_pasted_address_with_surrounding_whitespace_is_trimmed_not_refused(self, env):
        env.config.save_settings([(endpoints.FLARESOLVERR_FORM_KEY, [f"  {GLOBAL_URL} \n"])])

        assert env.settings.endpoints.flaresolverr_v1_url == GLOBAL_URL

    def test_an_unrelated_save_does_not_reset_the_pool(self, env):
        env.config.save_settings([("settings-general-page_size", ["77"])])
        assert [event for event in env.events if event[0] == "reset"] == []

    def test_clearing_the_address_resets_the_pool_too(self, env):
        env.config.save_settings([(endpoints.FLARESOLVERR_FORM_KEY, [GLOBAL_URL])])
        env.events.clear()
        env.config.save_settings([(endpoints.FLARESOLVERR_FORM_KEY, [""])])
        assert env.settings.endpoints.flaresolverr_v1_url == ""
        assert [event for event in env.events if event[0] == "reset"] == [("reset", "")]


class TestDetect:
    def test_a_sibling_that_answers_like_flaresolverr_is_reported(self, monkeypatch):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        assert endpoints.detect_flaresolverr_sibling() == endpoints.FLARESOLVERR_SIBLING_URL

    def test_nothing_answering_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: False)
        assert endpoints.detect_flaresolverr_sibling() is None

    def test_the_result_is_cached_until_a_refresh(self, monkeypatch):
        calls = []
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: calls.append(url) or True)
        endpoints.detect_flaresolverr_sibling()
        endpoints.detect_flaresolverr_sibling()
        assert len(calls) == 1
        endpoints.detect_flaresolverr_sibling(refresh=True)
        assert len(calls) == 2

    def test_detecting_never_writes_the_setting_or_feeds_providers(self, monkeypatch):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        stored = _settings("")
        status = endpoints.flaresolverr_status(stored, refresh=True)

        assert status == {"url": "", "source": "none", "detected": endpoints.FLARESOLVERR_SIBLING_URL,
                          "detecting": False, "sibling_found": True}
        assert stored == _settings("")
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)], {}, stored)
        assert configs["alpha"][FIELD] == ""

    def test_the_hint_is_hidden_once_that_address_is_in_use(self, monkeypatch):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        endpoints.detect_flaresolverr_sibling(refresh=True)
        status = endpoints.flaresolverr_status(_settings(endpoints.FLARESOLVERR_SIBLING_URL))
        assert status["detected"] is None
        assert status["sibling_found"] is True
        assert status["source"] == "setting"

    @staticmethod
    def _serve(monkeypatch, body, status=200):
        """Replace the HTTP call with a response that streams ``body`` and records what was read."""
        import requests

        record = {"chunks": 0, "closed": False, "stream": None}

        class Response:
            status_code = status

            @staticmethod
            def iter_content(chunk_size):
                data = body
                while True:
                    record["chunks"] += 1
                    yield data[:chunk_size] if not callable(data) else data()
                    if not callable(data):
                        data = data[chunk_size:]
                        if not data:
                            return

            @staticmethod
            def close():
                record["closed"] = True

        def get(self, url, **kwargs):
            record["stream"] = kwargs.get("stream")
            return Response()

        monkeypatch.setattr(requests.Session, "get", get)
        return record

    def test_the_probe_only_accepts_a_flaresolverr_reply(self, monkeypatch):
        self._serve(monkeypatch, b'{"msg": "FlareSolverr is ready!"}')
        assert endpoints._probe_flaresolverr("http://x") is True
        self._serve(monkeypatch, b'{"msg": "hello"}')
        assert endpoints._probe_flaresolverr("http://x") is False
        self._serve(monkeypatch, b"<html>")
        assert endpoints._probe_flaresolverr("http://x") is False
        self._serve(monkeypatch, b'{"msg": "FlareSolverr"}', status=302)
        assert endpoints._probe_flaresolverr("http://x") is False

    def test_the_probe_streams_and_closes_the_response(self, monkeypatch):
        record = self._serve(monkeypatch, b'{"msg": "FlareSolverr"}')
        assert endpoints._probe_flaresolverr("http://x") is True
        assert record["stream"] is True
        assert record["closed"] is True

    def test_the_probe_stops_reading_an_endless_body(self, monkeypatch):
        record = self._serve(monkeypatch, lambda: b"x" * endpoints.SIBLING_PROBE_MAX_BYTES)
        assert endpoints._probe_flaresolverr("http://x") is False
        assert record["chunks"] <= 3
        assert record["closed"] is True

    def test_a_body_just_over_the_limit_is_refused_even_if_it_would_match(self, monkeypatch):
        body = b'{"msg": "FlareSolverr", "pad": "' + b"x" * endpoints.SIBLING_PROBE_MAX_BYTES + b'"}'
        self._serve(monkeypatch, body)
        assert endpoints._probe_flaresolverr("http://x") is False

    def test_the_offered_address_is_the_v1_endpoint(self):
        assert endpoints.FLARESOLVERR_SIBLING_URL == "http://flaresolverr:8191/v1"
        assert endpoints.FLARESOLVERR_SIBLING_ROOT == "http://flaresolverr:8191"

    @pytest.mark.parametrize("saved", [
        "http://flaresolverr:8191/v1",
        "http://flaresolverr:8191/v1/",
        "http://flaresolverr:8191",
        "http://flaresolverr:8191/",
        "HTTP://FlareSolverr:8191/V1",
    ])
    def test_a_saved_address_for_the_same_service_is_not_offered_again(self, monkeypatch, saved):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        endpoints.detect_flaresolverr_sibling(refresh=True)
        status = endpoints.flaresolverr_status(_settings(saved))
        assert status["detected"] is None
        assert status["sibling_found"] is True

    @pytest.mark.parametrize("saved", ["http://other:8191/v1", "http://flaresolverr:8192/v1",
                                       "http://flaresolverr:8191/proxy"])
    def test_a_different_service_still_gets_the_offer(self, monkeypatch, saved):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        endpoints.detect_flaresolverr_sibling(refresh=True)
        status = endpoints.flaresolverr_status(_settings(saved))
        assert status["detected"] == endpoints.FLARESOLVERR_SIBLING_URL


class TestNeverBlocksTheCaller:
    """A page load reads the cache. Only a background thread or an explicit Detect probes."""

    @staticmethod
    def _gated_probe(monkeypatch, found=True):
        import threading

        release = threading.Event()
        calls = []

        def probe(url):
            calls.append(url)
            release.wait(10)
            return found

        monkeypatch.setattr(endpoints, "_probe_flaresolverr", probe)
        return release, calls

    def test_a_status_read_answers_at_once_while_the_probe_is_still_running(self, monkeypatch):
        import time

        release, _calls = self._gated_probe(monkeypatch)
        started = time.monotonic()
        status = endpoints.flaresolverr_status(_settings(""))
        elapsed = time.monotonic() - started
        try:
            assert elapsed < 1.0
            assert status["detected"] is None
            assert status["detecting"] is True
        finally:
            release.set()

    def test_the_background_answer_is_served_by_the_next_read(self, monkeypatch):
        release, _calls = self._gated_probe(monkeypatch)
        endpoints.flaresolverr_status(_settings(""))
        release.set()
        for _ in range(100):
            status = endpoints.flaresolverr_status(_settings(""))
            if not status["detecting"]:
                break
            import time
            time.sleep(0.05)
        assert status["detected"] == endpoints.FLARESOLVERR_SIBLING_URL
        assert status["detecting"] is False

    def test_a_stale_answer_is_served_while_a_refresh_runs_behind_it(self, monkeypatch):
        endpoints._sibling_cache.update(checked_at=time_ago(endpoints.SIBLING_CACHE_SECONDS + 1),
                                        url=endpoints.FLARESOLVERR_SIBLING_URL)
        release, calls = self._gated_probe(monkeypatch, found=False)
        try:
            status = endpoints.flaresolverr_status(_settings(""))
            assert status["detected"] == endpoints.FLARESOLVERR_SIBLING_URL
            # The page polls only while this is true, so a running refresh of a
            # stale answer has to say so or the open page never sees the result.
            assert status["detecting"] is True
        finally:
            release.set()

    def test_a_poll_after_a_stale_refresh_completes_gets_the_new_result(self, monkeypatch):
        import time

        endpoints._sibling_cache.update(checked_at=time_ago(endpoints.SIBLING_CACHE_SECONDS + 1),
                                        url=endpoints.FLARESOLVERR_SIBLING_URL)
        release, _calls = self._gated_probe(monkeypatch, found=False)
        first = endpoints.flaresolverr_status(_settings(""))
        assert first["detecting"] is True
        release.set()
        for _ in range(100):
            status = endpoints.flaresolverr_status(_settings(""))
            if not status["detecting"]:
                break
            time.sleep(0.05)

        assert status["detecting"] is False
        assert status["detected"] is None
        assert status["sibling_found"] is False

    def test_a_refresh_that_completes_before_the_answer_is_read_is_not_reported_stale(self, monkeypatch):
        """The old address must never come back paired with "not detecting"."""
        endpoints._sibling_cache.update(checked_at=time_ago(endpoints.SIBLING_CACHE_SECONDS + 1),
                                        url=endpoints.FLARESOLVERR_SIBLING_URL)
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: False)
        real_schedule = endpoints.schedule_sibling_refresh

        def schedule_and_finish():
            thread = real_schedule()
            thread.join(5)
            return thread

        monkeypatch.setattr(endpoints, "schedule_sibling_refresh", schedule_and_finish)

        assert endpoints.sibling_hint() == (None, False)

    def test_a_fresh_answer_starts_no_probe(self, monkeypatch):
        import time

        endpoints._sibling_cache.update(checked_at=time.monotonic(), url=None)
        calls = []
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: calls.append(url) or True)
        status = endpoints.flaresolverr_status(_settings(""))
        assert status["detecting"] is False
        assert status["sibling_found"] is False
        _join_background_threads()
        assert calls == []

    def test_an_explicit_refresh_probes_once_with_a_recording_stub(self, monkeypatch):
        calls = []
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: calls.append(url) or True)
        thread = endpoints.schedule_sibling_refresh()
        assert thread is not None
        thread.join(5)
        _join_background_threads()
        # The root is probed; the /v1 endpoint is what gets offered.
        assert calls == [endpoints.FLARESOLVERR_SIBLING_ROOT]
        assert endpoints.flaresolverr_status(_settings(""))["detected"] == endpoints.FLARESOLVERR_SIBLING_URL

    def test_only_one_refresh_runs_at_a_time(self, monkeypatch):
        release, calls = self._gated_probe(monkeypatch)
        try:
            first = endpoints.schedule_sibling_refresh()
            assert first is not None
            assert endpoints.schedule_sibling_refresh() is None
            endpoints.flaresolverr_status(_settings(""))
            endpoints.flaresolverr_status(_settings(""))
        finally:
            release.set()
            first.join(5)
        assert len(calls) == 1

    def test_a_probe_stuck_in_name_lookup_is_given_up_on_at_the_deadline(self, monkeypatch):
        import time

        release, _calls = self._gated_probe(monkeypatch)
        monkeypatch.setattr(endpoints, "SIBLING_PROBE_DEADLINE_SECONDS", 0.2)
        started = time.monotonic()
        try:
            assert endpoints.detect_flaresolverr_sibling(refresh=True) is None
            assert time.monotonic() - started < 2.0
        finally:
            release.set()

    def test_a_second_detect_while_the_first_probe_is_still_blocked_starts_no_new_probe(self, monkeypatch):
        """The deadline stops the callers' wait, not the network call, so the flight must outlive it."""
        import time

        release, calls = self._gated_probe(monkeypatch)
        monkeypatch.setattr(endpoints, "SIBLING_PROBE_DEADLINE_SECONDS", 0.2)
        try:
            assert endpoints.detect_flaresolverr_sibling(refresh=True) is None
            assert endpoints._probe_flight is not None
            started = time.monotonic()
            assert endpoints.detect_flaresolverr_sibling(refresh=True) is None
            assert time.monotonic() - started < 2.0
            assert endpoints.schedule_sibling_refresh() is None
            assert len(calls) == 1
        finally:
            release.set()
        _join_background_threads()
        assert len(calls) == 1
        assert endpoints._probe_flight is None

    def test_a_result_that_lands_after_the_deadline_replaces_the_timeout_answer(self, monkeypatch):
        release, _calls = self._gated_probe(monkeypatch)
        monkeypatch.setattr(endpoints, "SIBLING_PROBE_DEADLINE_SECONDS", 0.2)
        assert endpoints.detect_flaresolverr_sibling(refresh=True) is None
        release.set()
        _join_background_threads()
        assert endpoints.detect_flaresolverr_sibling() == endpoints.FLARESOLVERR_SIBLING_URL

    def test_the_page_keeps_polling_while_a_probe_past_its_deadline_is_still_out(self, monkeypatch):
        import time

        release, _calls = self._gated_probe(monkeypatch, found=True)
        monkeypatch.setattr(endpoints, "SIBLING_PROBE_DEADLINE_SECONDS", 0.2)
        try:
            explicit = endpoints.flaresolverr_status(_settings(""), refresh=True)
            assert explicit["detected"] is None
            assert explicit["detecting"] is True
            polled = endpoints.flaresolverr_status(_settings(""))
            assert polled["detecting"] is True
        finally:
            release.set()
        _join_background_threads()
        for _ in range(100):
            polled = endpoints.flaresolverr_status(_settings(""))
            if not polled["detecting"]:
                break
            time.sleep(0.05)
        assert polled["detecting"] is False
        assert polled["detected"] == endpoints.FLARESOLVERR_SIBLING_URL

    def test_a_pending_startup_probe_and_an_explicit_detect_share_one_probe(self, monkeypatch):
        """The startup probe is slow and negative; Detect must not race it with a second probe."""
        import threading
        import time

        release = threading.Event()
        calls = []

        def probe(url):
            calls.append(url)
            if len(calls) == 1:
                release.wait(10)
                return False
            return True

        monkeypatch.setattr(endpoints, "_probe_flaresolverr", probe)
        endpoints.start_flaresolverr_hint_probe()
        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        result = {}
        detect = threading.Thread(
            target=lambda: result.update(found=endpoints.detect_flaresolverr_sibling(refresh=True)))
        detect.start()
        time.sleep(0.2)
        release.set()
        detect.join(5)
        _join_background_threads()

        assert len(calls) == 1
        assert result["found"] is None
        assert endpoints._sibling_cache["url"] is None
        assert endpoints._probe_flight is None

    def test_a_detect_that_starts_the_probe_lets_the_background_refresh_join_it(self, monkeypatch):
        import threading
        import time

        release, calls = self._gated_probe(monkeypatch)
        result = {}
        detect = threading.Thread(
            target=lambda: result.update(found=endpoints.detect_flaresolverr_sibling(refresh=True)))
        detect.start()
        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        try:
            assert endpoints.schedule_sibling_refresh() is None
        finally:
            release.set()
            detect.join(5)
        assert result["found"] == endpoints.FLARESOLVERR_SIBLING_URL
        assert len(calls) == 1

    def test_detect_reports_against_the_address_in_use_after_the_probe_ends(self, monkeypatch):
        """A setting saved while Detect waits must decide whether the hint is still worth showing."""
        import threading
        import time

        release, calls = self._gated_probe(monkeypatch)
        settings = _settings("")
        result = {}
        detect = threading.Thread(
            target=lambda: result.update(status=endpoints.flaresolverr_status(settings, refresh=True)))
        detect.start()
        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        settings["endpoints"][endpoints.FLARESOLVERR_SETTING_KEY] = endpoints.FLARESOLVERR_SIBLING_URL
        release.set()
        detect.join(5)

        status = result["status"]
        assert status["url"] == endpoints.FLARESOLVERR_SIBLING_URL
        assert status["source"] == "setting"
        assert status["detected"] is None
        assert status["sibling_found"] is True

    def test_the_api_read_never_probes_in_the_request(self, monkeypatch):
        """GET answers from the cache; only POST asks for a probe, and neither writes the setting."""
        from flask import Flask
        from flask_restx import Api

        from api.providers import flaresolverr as api_module
        from app.config import settings

        monkeypatch.setattr(endpoints, "_probe_flaresolverr",
                            lambda url: pytest.fail("the request must not wait on a probe"))
        monkeypatch.setattr(endpoints, "schedule_sibling_refresh", lambda: None)
        app = Flask(__name__)
        api = Api(app)
        api.add_namespace(api_module.api_ns_providers_flaresolverr, path="/")
        headers = {"X-API-KEY": settings.auth.apikey}
        client = app.test_client()

        response = client.get("/providers/flaresolverr", headers=headers)
        assert response.status_code == 200, response.data
        body = response.get_json()
        assert body["data"]["detecting"] is True
        assert body["data"]["detected"] is None

        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        body = client.post("/providers/flaresolverr", headers=headers).get_json()
        assert body["data"]["detected"] == endpoints.FLARESOLVERR_SIBLING_URL
        assert body["data"]["detecting"] is False


class TestAuthentication:
    @staticmethod
    def _client():
        from flask import Flask
        from flask_restx import Api

        from api.providers import flaresolverr as api_module

        app = Flask(__name__)
        api = Api(app)
        api.add_namespace(api_module.api_ns_providers_flaresolverr, path="/")
        return app.test_client()

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_no_key_is_refused_and_nothing_is_probed(self, monkeypatch, method):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr",
                            lambda url: pytest.fail("an unauthenticated request must not probe"))
        monkeypatch.setattr(endpoints, "schedule_sibling_refresh",
                            lambda: pytest.fail("an unauthenticated request must not probe"))
        client = self._client()

        assert getattr(client, method)("/providers/flaresolverr").status_code == 401
        assert getattr(client, method)("/providers/flaresolverr",
                                       headers={"X-API-KEY": "wrong"}).status_code == 401


def time_ago(seconds):
    import time

    return time.monotonic() - seconds
