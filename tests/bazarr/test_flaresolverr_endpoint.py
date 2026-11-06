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

        assert status == {"url": "", "source": "none", "detected": endpoints.FLARESOLVERR_SIBLING_URL}
        assert stored == _settings("")
        configs = _overlay(monkeypatch, [_installation("alpha", trusted=True)], {}, stored)
        assert configs["alpha"][FIELD] == ""

    def test_the_hint_is_hidden_once_that_address_is_in_use(self, monkeypatch):
        monkeypatch.setattr(endpoints, "_probe_flaresolverr", lambda url: True)
        status = endpoints.flaresolverr_status(_settings(endpoints.FLARESOLVERR_SIBLING_URL))
        assert status["detected"] is None
        assert status["source"] == "setting"

    def test_the_probe_only_accepts_a_flaresolverr_reply(self, monkeypatch):
        import requests

        def fake_get(payload, status=200):
            class Response:
                status_code = status

                @staticmethod
                def json():
                    if isinstance(payload, Exception):
                        raise payload
                    return payload

            return lambda self, url, **kwargs: Response()

        monkeypatch.setattr(requests.Session, "get", fake_get({"msg": "FlareSolverr is ready!"}))
        assert endpoints._probe_flaresolverr("http://x") is True
        monkeypatch.setattr(requests.Session, "get", fake_get({"msg": "hello"}))
        assert endpoints._probe_flaresolverr("http://x") is False
        monkeypatch.setattr(requests.Session, "get", fake_get(ValueError("html")))
        assert endpoints._probe_flaresolverr("http://x") is False
        monkeypatch.setattr(requests.Session, "get", fake_get({"msg": "FlareSolverr"}, status=302))
        assert endpoints._probe_flaresolverr("http://x") is False
