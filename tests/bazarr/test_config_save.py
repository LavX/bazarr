# coding=utf-8
import os
import sys
from types import SimpleNamespace

import pytest
from dynaconf.validator import ValidationError


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


def test_save_settings_creates_missing_provider_section_for_hub_config(monkeypatch):
    from app import config

    provider_id = "testhubdynamic"
    executed = []

    # A write that reached disk. Anything else is refused.
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )

    try:
        config.save_settings(
            [
                (f"settings-{provider_id}-profile_name", ["Smoke profile"]),
                (f"settings-{provider_id}-api_token", ["token-value"]),
            ]
        )

        assert config.settings[provider_id]["profile_name"] == "Smoke profile"
        assert config.settings[provider_id]["api_token"] == "token-value"
        assert executed
    finally:
        config.settings.unset(provider_id.upper(), force=True)


def test_save_settings_resets_compat_pool_for_dynamic_provider_hub_config(monkeypatch):
    from app import config

    provider_id = "sub_scene"
    executed = []
    reset_calls = []

    def record_compat_pool_reset():
        reset_calls.append(config.settings[provider_id]["flaresolverr_url"])

    # A write that reached disk. Anything else is refused.
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "compat.service",
        SimpleNamespace(reset_compat_pool=record_compat_pool_reset),
    )
    monkeypatch.setitem(
        sys.modules,
        "provider_hub.state",
        SimpleNamespace(active_installations=lambda: [SimpleNamespace(provider_id=provider_id)]),
    )

    try:
        config.save_settings(
            [
                (f"settings-{provider_id}-flaresolverr_url", ["http://solver:8191"]),
            ]
        )

        assert config.settings[provider_id]["flaresolverr_url"] == "http://solver:8191"
        assert reset_calls == ["http://solver:8191"]
        assert executed
    finally:
        config.settings.unset(provider_id.upper(), force=True)


def test_provider_hub_settings_saves_clear_runtime_quota(monkeypatch):
    from app import config
    from provider_hub import runtime_status

    provider_id = "examplehub"
    previous_enabled = list(config.settings.general.enabled_providers)
    executed = []
    reset_calls = []
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "compat.service",
        SimpleNamespace(reset_compat_pool=lambda: reset_calls.append("pool")),
    )
    monkeypatch.setitem(
        sys.modules,
        "provider_hub.state",
        SimpleNamespace(
            active_installations=lambda: [],
            load_state=lambda: {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "active_version": "0.1.5",
                        "state": "staged",
                        "pending_restart": True,
                    }
                }
            },
        ),
    )

    runtime_status.clear()
    config.settings.general.enabled_providers = [provider_id]
    try:
        runtime_status.consume(provider_id, [
            {"type": "translation_quota", "remaining": 10},
        ])
        first_generation = runtime_status.generation(provider_id)
        config.save_settings([
            (f"settings-{provider_id}-api_key", ["replacement"]),
        ])
        assert runtime_status.get(provider_id) is None
        assert runtime_status.generation(provider_id) == first_generation + 1

        runtime_status.consume(provider_id, [
            {"type": "translation_quota", "remaining": 9},
        ])
        second_generation = runtime_status.generation(provider_id)
        config.save_settings([
            ("settings-general-enabled_providers", ["otherhub"]),
        ])
        assert runtime_status.get(provider_id) is None
        assert runtime_status.generation(provider_id) == second_generation + 1
        assert reset_calls == ["pool", "pool"]
        assert len(executed) == 2
    finally:
        runtime_status.clear()
        config.settings.general.enabled_providers = previous_enabled
        config.settings.unset(provider_id.upper(), force=True)


def test_save_settings_invalidates_the_compat_cache_for_a_score_modifier(monkeypatch):
    """A cached compat envelope carries the projected scores with it, so an
    edited modifier would leave external clients on the old numbers for the
    rest of the cache TTL, up to a day."""
    from app import config

    executed = []
    invalidations = []

    # A write that reached disk. Anything else is refused.
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "compat.cache",
        SimpleNamespace(invalidate_all=lambda: invalidations.append(
            config.settings.general.provider_score_modifiers)),
    )

    previous = config.settings.general.provider_score_modifiers
    try:
        config.save_settings(
            [("settings-general-provider_score_modifiers", ['{"whisperai": 25}'])]
        )

        assert config.settings.general.provider_score_modifiers == {"whisperai": 25}
        assert invalidations == [{"whisperai": 25}]
    finally:
        config.settings.general.provider_score_modifiers = previous


def test_save_settings_invalidates_the_compat_cache_for_the_ai_translated_penalty(monkeypatch):
    """The AI-translated penalty is projected into the same cached scores as a
    provider modifier, so an edit to it has to retire the cache too."""
    from app import config

    invalidations = []

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda _statement: None),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "compat.cache",
        SimpleNamespace(invalidate_all=lambda: invalidations.append(
            config.settings.general.ai_translated_score_penalty)),
    )

    previous = config.settings.general.ai_translated_score_penalty
    try:
        config.save_settings([("settings-general-ai_translated_score_penalty", ["30"])])

        assert config.settings.general.ai_translated_score_penalty == 30
        assert invalidations == [30]
    finally:
        config.settings.general.ai_translated_score_penalty = previous


def test_save_settings_leaves_the_compat_cache_alone_for_an_unrelated_setting(monkeypatch):
    from app import config

    invalidations = []

    # A write that reached disk. Anything else is refused.
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda _statement: None),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "compat.cache",
        SimpleNamespace(invalidate_all=lambda: invalidations.append(True)),
    )

    config.save_settings([("settings-general-page_size", ["50"])])

    assert invalidations == []


@pytest.mark.parametrize('submitted, expected', [
    (['DeepInfra'], ['deepinfra']),
    ([' PARASAIL/fp8 ', 'deepinfra', 'parasail/fp8'], ['parasail/fp8', 'deepinfra']),
    ([''], []),
    ([], []),
    (['p' * 160], ['p' * 160]),
    ([f'provider-{i}' for i in range(20)], [f'provider-{i}' for i in range(20)]),
])
def test_provider_order_setting_saves_normalized_lists(monkeypatch, submitted, expected):
    from app import config

    saved = []
    def _record_write():
        saved.append(list(config.settings.translator.openrouter_provider_order))
        return True

    monkeypatch.setattr(config, 'write_config', _record_write)
    monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
    monkeypatch.setitem(sys.modules, 'app.database', SimpleNamespace(
        database=SimpleNamespace(execute=lambda _statement: None),
        update=lambda _model: _FakeUpdate(), System=object,
    ))
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['previous'])

    config.save_settings([('settings-translator-openrouter_provider_order', submitted)])

    assert saved == [expected]
    assert list(config.settings.translator.openrouter_provider_order) == expected


@pytest.mark.parametrize('submitted', [[''], ['bad provider'], ['https://host'], [1], ['provider'] * 21,
                                      ['p' * 161]])
def test_provider_order_setting_rejects_invalid_lists(submitted):
    from dynaconf import Dynaconf
    from dynaconf.validator import ValidationError
    from app import config

    validator = next(v for v in config.validators
                     if v.names == ('translator.openrouter_provider_order',))
    candidate = Dynaconf(TRANSLATOR={'openrouter_provider_order': submitted})

    with pytest.raises(ValidationError):
        validator.validate(candidate)


def test_invalid_provider_order_does_not_partially_apply_settings(monkeypatch):
    from dynaconf.validator import ValidationError
    from app import config

    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    with pytest.raises(ValidationError):
        config.save_settings([
            ('settings-translator-openrouter_provider_routing', ['custom']),
            ('settings-translator-openrouter_provider_order', ['', 'deepinfra']),
        ])

    assert config.settings.translator.openrouter_provider_routing == 'throughput'
    assert config.settings.translator.openrouter_provider_order == ['deepinfra']


def _settings_save_harness(monkeypatch):
    """save_settings with its persistence and schedule side effects stubbed out."""
    from app import config

    saved = []
    def _record_write():
        saved.append(dict(
            routing=config.settings.translator.openrouter_provider_routing,
            order=list(config.settings.translator.openrouter_provider_order),
        ))
        return True

    monkeypatch.setattr(config, 'write_config', _record_write)
    monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
    monkeypatch.setitem(sys.modules, 'app.database', SimpleNamespace(
        database=SimpleNamespace(execute=lambda _statement: None),
        update=lambda _model: _FakeUpdate(), System=object,
    ))
    return saved


@pytest.mark.parametrize('starting_routing, starting_order, items', [
    # Selecting custom while nothing is chosen, and nothing is stored either.
    ('throughput', [], [('settings-translator-openrouter_provider_routing', ['custom'])]),
    ('throughput', ['deepinfra'], [('settings-translator-openrouter_provider_routing', ['custom']),
                                   ('settings-translator-openrouter_provider_order', [''])]),
    # Clearing the list of a routing that is already custom.
    ('custom', ['deepinfra'], [('settings-translator-openrouter_provider_order', [''])]),
])
def test_custom_routing_without_a_provider_is_refused_at_the_settings_boundary(
        monkeypatch, starting_routing, starting_order, items):
    # Stored apart, the pair saves cleanly and then fails every translation, and by then
    # the only signal is a failed job. Both keys arrive in the same request, so the
    # contradiction is visible here.
    from dynaconf.validator import ValidationError
    from app import config

    saved = _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', starting_routing)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', starting_order)

    with pytest.raises(ValidationError) as refusal:
        config.save_settings(items)

    assert 'provider' in str(refusal.value).lower()
    assert saved == []
    assert config.settings.translator.openrouter_provider_routing == starting_routing
    assert config.settings.translator.openrouter_provider_order == starting_order


@pytest.mark.parametrize('items, expected_order', [
    ([('settings-translator-openrouter_provider_routing', ['custom']),
      ('settings-translator-openrouter_provider_order', ['DeepInfra'])], ['deepinfra']),
    # The list already stored counts: changing only the routing is allowed when it does.
    ([('settings-translator-openrouter_provider_routing', ['custom'])], ['deepinfra']),
])
def test_custom_routing_with_a_provider_saves(monkeypatch, items, expected_order):
    from app import config

    saved = _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order',
                        ['deepinfra'] if len(items) == 1 else [])

    config.save_settings(items)

    assert saved == [{'routing': 'custom', 'order': expected_order}]


@pytest.mark.parametrize('routing', ['throughput', 'smartfast'])
def test_an_empty_provider_list_is_fine_for_every_other_routing(monkeypatch, routing):
    from app import config

    saved = _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    config.save_settings([
        ('settings-translator-openrouter_provider_routing', [routing]),
        ('settings-translator-openrouter_provider_order', ['']),
    ])

    assert saved == [{'routing': routing, 'order': []}]


def test_a_case_variant_of_the_provider_order_key_is_normalized_too(monkeypatch):
    # The settings store underneath is case-insensitive, so matching the form key
    # exactly let a case variant past the normalizer and left a second, unvalidated
    # copy of the same setting in the config file.
    from app import config

    saved = _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', [])

    config.save_settings([('settings-translator-OPENROUTER_PROVIDER_ORDER', [' DeepInfra ', 'deepinfra'])])

    assert saved == [{'routing': 'throughput', 'order': ['deepinfra']}]


def test_a_case_variant_carrying_an_invalid_list_is_refused_too(monkeypatch):
    from dynaconf.validator import ValidationError
    from app import config

    _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    with pytest.raises(ValidationError):
        config.save_settings([('settings-translator-OPENROUTER_PROVIDER_ORDER', ['bad provider'])])

    assert config.settings.translator.openrouter_provider_order == ['deepinfra']


def test_a_save_touching_neither_routing_key_is_never_blocked_by_them(monkeypatch):
    # Reading the pair off stored settings for every save made one bad translator config
    # reject saves on every other settings page, which is a worse failure than the one
    # being prevented and lands on a page that cannot fix it.
    from app import config

    saved = _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', [])

    config.save_settings([('settings-general-instance_name', ['Home'])])

    assert saved and config.settings.general.instance_name == 'Home'


def test_a_decoy_key_in_another_section_cannot_satisfy_the_custom_routing_check(monkeypatch):
    # The check used to key on the last dash-segment across every section, so a key
    # naming any other section answered for the translator's.
    from dynaconf.validator import ValidationError
    from app import config

    _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    with pytest.raises(ValidationError):
        config.save_settings([
            ('settings-translator-openrouter_provider_order', ['']),
            ('settings-zzz-openrouter_provider_routing', ['throughput']),
        ])

    assert config.settings.translator.openrouter_provider_order == ['deepinfra']


def test_another_section_cannot_be_redirected_into_the_translator_provider_order(monkeypatch):
    # Canonicalising on the name alone rewrote any section's key into the translator's,
    # which is a silent cross-section write.
    from app import config

    _settings_save_harness(monkeypatch)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    config.save_settings([('settings-general-openrouter_provider_order', ['parasail'])])

    assert config.settings.translator.openrouter_provider_order == ['deepinfra']


@pytest.mark.parametrize("value", [["a", "b"], [None], ["line\nbreak"], ["enc:v1:cipher"], [" "], ["a" * 4097]])
def test_tmdb_form_prevalidation_rejects_invalid_values_before_other_mutations(monkeypatch, value):
    from app import config
    previous = config.settings.general.page_size
    monkeypatch.setattr(config, "write_config", lambda: None)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None),
        update=lambda model: _FakeUpdate(), System=object))
    try:
        with pytest.raises(ValidationError, match="Invalid TMDB access token"):
            config.save_settings([("settings-general-page_size", ["87"]),
                                  ("settings-discover-tmdb_access_token", value)])
        assert config.settings.general.page_size == previous
    finally:
        config.settings.general.page_size = previous


@pytest.fixture
def metadata_save_environment(monkeypatch, tmp_path):
    from app import config
    from dynaconf import Dynaconf
    from discover import metadata
    from secret_store import encrypt_settings_dict
    from dynaconf.loaders.yaml_loader import write

    path = tmp_path / "config.yaml"
    initial = config.settings.as_dict()
    initial["DISCOVER"] = {"tmdb_access_token": "5ecafe00cafe00cafe00cafe00cafe00", "locale": "en-US"}
    write(str(path), encrypt_settings_dict({key.lower(): value for key, value in initial.items()}), merge=False)
    fake = Dynaconf(settings_file=str(path), core_loaders=["YAML"])
    config.decrypt_settings_in_place(fake)
    monkeypatch.setattr(config, "settings", fake)
    monkeypatch.setattr(config, "config_yaml_file", str(path))
    monkeypatch.setattr(config, "_force_first_save_migration", False)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(metadata, "_current", None)
    executed = []
    resets = []
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
        update=lambda model: _FakeUpdate(), System=object))
    monkeypatch.setitem(sys.modules, "compat.cache", SimpleNamespace(invalidate_all=lambda: resets.append("cache")))
    monkeypatch.setitem(sys.modules, "compat.service", SimpleNamespace(reset_compat_pool=lambda: resets.append("pool")))
    return SimpleNamespace(config=config, settings=fake, path=path, executed=executed, resets=resets, metadata=metadata)


@pytest.mark.parametrize("value", ["5ecafe11cafe11cafe11cafe11cafe11", "", "12345", "true", "null"])
def test_tmdb_save_encrypts_exact_value_and_only_invalidates_metadata(metadata_save_environment, value):
    env = metadata_save_environment
    before = env.metadata.configuration().revision
    env.config.save_settings([("settings-discover-tmdb_access_token", [value])])
    assert env.settings.discover.tmdb_access_token == value
    assert env.metadata.configuration().revision != before
    assert env.resets == []
    assert len(env.executed) == 1
    import yaml
    stored = yaml.safe_load(env.path.read_text())["discover"]["tmdb_access_token"]
    assert stored == "" if value == "" else stored.startswith("enc:v1:")
    env.settings.reload()
    env.config.decrypt_settings_in_place(env.settings)
    assert env.settings.discover.tmdb_access_token == value
    assert "tmdb_access_token" not in env.config.get_settings()["discover"]


@pytest.mark.parametrize("value", ["***", "5ecafe00cafe00cafe00cafe00cafe00", None])
def test_tmdb_unchanged_legacy_mask_and_unrelated_save_preserve_revision(metadata_save_environment, value):
    env = metadata_save_environment
    before = env.metadata.configuration().revision
    items = [("settings-general-page_size", ["51"])] if value is None else [("settings-discover-tmdb_access_token", [value])]
    env.config.save_settings(items)
    assert env.settings.discover.tmdb_access_token == "5ecafe00cafe00cafe00cafe00cafe00"
    assert env.metadata.configuration().revision == before
    assert env.resets == []


@pytest.mark.parametrize("previous,replacement", [("", "5ecafe22cafe22cafe22cafe22cafe22"), ("saved", "replacement"), ("5ecafe00cafe00cafe00cafe00cafe00", "")])
@pytest.mark.parametrize("failure", ["write", "move"])
def test_failed_tmdb_persistence_restores_effective_metadata_and_never_advertises_success(
    metadata_save_environment, monkeypatch, previous, replacement, failure,
):
    env = metadata_save_environment
    env.settings.discover.tmdb_access_token = previous
    env.config.write_config()
    before_disk = env.path.read_bytes()
    before = env.config.get_settings()["discover"]
    def fail(*args, **kwargs):
        raise OSError("synthetic-sensitive-exception")
    monkeypatch.setattr(env.config, failure, fail)
    with pytest.raises(env.config.MetadataPersistenceError) as error:
        env.config.save_settings([("settings-discover-tmdb_access_token", [replacement]),
                                  ("settings-discover-locale", ["hu-HU"])])
    assert "synthetic-sensitive-exception" not in str(error.value)
    assert env.config.get_settings()["discover"] == before
    assert env.settings.discover.tmdb_access_token == previous
    assert env.settings.discover.locale == "en-US"
    assert env.path.read_bytes() == before_disk
    assert env.executed == env.resets == []


def test_validation_failure_reloads_and_decrypts_write_only_credential(metadata_save_environment, monkeypatch):
    env = metadata_save_environment
    before = env.metadata.configuration().revision
    def fail():
        raise ValidationError("Invalid unrelated setting.")
    monkeypatch.setattr(env.settings.validators, "validate", fail)
    with pytest.raises(ValidationError):
        env.config.save_settings([("settings-discover-tmdb_access_token", ["failed-replacement"])])
    assert env.settings.discover.tmdb_access_token == "5ecafe00cafe00cafe00cafe00cafe00"
    assert env.metadata.configuration().revision == before
    assert env.resets == env.executed == []


def test_settings_reader_cannot_observe_a_credential_while_its_save_fails(metadata_save_environment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    env = metadata_save_environment
    before = env.config.get_settings()["discover"]
    started = threading.Event()
    release = threading.Event()
    reading = threading.Event()
    def fail_write(*args, **kwargs):
        started.set()
        assert release.wait(3)
        raise OSError("fixture I/O failure")
    monkeypatch.setattr(env.config, "write", fail_write)
    def read():
        reading.set()
        return env.config.get_settings()["discover"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(env.config.save_settings, [("settings-discover-tmdb_access_token", ["never-saved"]),
                                                     ("settings-discover-locale", ["hu-HU"])])
        assert started.wait(3)
        reader = pool.submit(read)
        assert reading.wait(3)
        assert not reader.done()
        release.set()
        with pytest.raises(env.config.MetadataPersistenceError):
            save.result()
        assert reader.result() == before


@pytest.mark.parametrize("failure", ["write", "move"])
def test_locale_only_persistence_failure_is_reported_and_preserves_metadata(metadata_save_environment, monkeypatch, failure):
    env = metadata_save_environment
    before = env.config.get_settings()["discover"]
    disk = env.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("fixture I/O failure")
    monkeypatch.setattr(env.config, failure, fail)
    with pytest.raises(env.config.MetadataPersistenceError):
        env.config.save_settings([("settings-discover-locale", ["hu-HU"])])
    assert env.config.get_settings()["discover"] == before
    assert env.path.read_bytes() == disk
    assert env.executed == env.resets == []


@pytest.mark.parametrize("replacement", ["5ecafe22cafe22cafe22cafe22cafe22", "5ecafe33cafe33cafe33cafe33cafe33", "", None])
@pytest.mark.parametrize("stage", ["configured_db", "logging_service"])
def test_metadata_durable_write_survives_followup_failure(metadata_save_environment, monkeypatch, replacement, stage):
    import yaml
    from secret_store import decrypt_settings_dict
    env = metadata_save_environment
    if replacement == "5ecafe22cafe22cafe22cafe22cafe22":
        env.settings.discover.tmdb_access_token = ""
        env.config.write_config()
    before = env.config.get_settings()["discover"]
    def fail(statement):
        raise RuntimeError("synthetic-private-followup-error")
    if stage == "configured_db":
        monkeypatch.setattr(sys.modules["app.database"].database, "execute", fail)
    else:
        monkeypatch.setitem(sys.modules, "app.logger", SimpleNamespace(configure_logging=fail))
    items = [("settings-discover-locale", ["hu-HU"]), ("settings-general-debug", ["true"])]
    if replacement is not None:
        items.append(("settings-discover-tmdb_access_token", [replacement]))
    expected = "5ecafe00cafe00cafe00cafe00cafe00" if replacement is None else replacement
    with pytest.raises(Exception) as error:
        env.config.save_settings(items)
    stored = yaml.safe_load(env.path.read_text())
    assert stored["discover"]["tmdb_access_token"] == "" if not expected else stored["discover"]["tmdb_access_token"].startswith("enc:v1:")
    assert decrypt_settings_dict(stored)["discover"] == dict(env.settings.discover)
    assert env.settings.discover.tmdb_access_token == expected
    after = env.config.get_settings()["discover"]
    assert after["metadata_revision"] != before["metadata_revision"]
    # Clearing the reader's key does not leave Discover unconfigured: the
    # application's own built-in key is what it falls back to. Whether the
    # reader has a key of their own is the separate fact, and it is the one
    # that has to follow the save.
    assert after["tmdb_configured"] is True
    assert after["tmdb_token_stored"] is bool(expected)
    assert after["locale"] == "hu-HU"
    assert "tmdb_access_token" not in after
    assert type(error.value).__name__ == "MetadataFollowupError"
    assert "synthetic-private-followup-error" not in str(error.value)
    assert env.resets == []


@pytest.mark.parametrize("token", ["***", "5ecafe00cafe00cafe00cafe00cafe00", None])
def test_unchanged_metadata_and_ordinary_save_report_a_followup_failure_as_saved(
    metadata_save_environment, monkeypatch, caplog, token,
):
    """A save without new metadata is on disk too when what follows it fails.

    The failure used to escape as it was, so the settings endpoint could not tell
    the save had been written: it answered 500 and left the languages, profiles and
    notifiers sent with it unwritten, while the file already held the new settings.
    """
    import yaml

    env = metadata_save_environment
    before = env.config.get_settings()["discover"]
    def fail(statement):
        raise RuntimeError("legacy-followup-error")
    monkeypatch.setattr(sys.modules["app.database"].database, "execute", fail)
    items = [("settings-general-page_size", ["51"])]
    if token is not None:
        items.append(("settings-discover-tmdb_access_token", [token]))
    with pytest.raises(env.config.SettingsFollowupError) as error:
        env.config.save_settings(items)
    assert not isinstance(error.value, env.config.MetadataFollowupError)
    assert "legacy-followup-error" not in str(error.value)
    assert "legacy-followup-error" in caplog.text
    assert yaml.safe_load(env.path.read_text())["general"]["page_size"] == 51
    assert env.settings.general.page_size == 51
    assert env.config.get_settings()["discover"] == before


@pytest.mark.parametrize("previous,replacement", [("", "5ecafe22cafe22cafe22cafe22cafe22"), ("5ecafe00cafe00cafe00cafe00cafe00", "5ecafe44cafe44cafe44cafe44cafe44"), ("5ecafe00cafe00cafe00cafe00cafe00", "")])
@pytest.mark.parametrize("other_writer", ["ordinary_save", "direct_write"])
@pytest.mark.parametrize("fail_move", [False, True])
def test_all_config_writers_serialize_with_metadata_save(
    metadata_save_environment, monkeypatch, previous, replacement, other_writer, fail_move,
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    import threading
    import yaml
    from secret_store import decrypt_settings_dict
    env = metadata_save_environment
    env.settings.discover.tmdb_access_token = previous
    env.config.write_config()
    before_revision = env.metadata.configuration().revision
    original_move = env.config.move
    ready, release, other_started = threading.Event(), threading.Event(), threading.Event()

    def scheduled_move(source, target):
        if threading.current_thread().name.startswith("metadata-writer"):
            ready.set()
            assert release.wait(3)
            if fail_move:
                raise OSError("synthetic move failure")
        return original_move(source, target)

    def ordinary_writer():
        other_started.set()
        if other_writer == "ordinary_save":
            env.config.save_settings([("settings-general-page_size", ["51"])])
        else:
            env.config.write_config()

    monkeypatch.setattr(env.config, "move", scheduled_move)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata-writer") as primary, \
            ThreadPoolExecutor(max_workers=1) as secondary:
        saving = primary.submit(env.config.save_settings, [("settings-discover-tmdb_access_token", [replacement])])
        assert ready.wait(3)
        ordinary = secondary.submit(ordinary_writer)
        try:
            assert other_started.wait(3)
            with pytest.raises(TimeoutError):
                ordinary.result(timeout=0.1)
        finally:
            release.set()
        if fail_move:
            with pytest.raises(env.config.MetadataPersistenceError):
                saving.result()
        else:
            saving.result()
        ordinary.result()
    stored = decrypt_settings_dict(yaml.safe_load(env.path.read_text()))
    assert stored["discover"] == dict(env.settings.discover)
    assert env.settings.discover.tmdb_access_token == (previous if fail_move else replacement)
    assert (env.metadata.configuration().revision == before_revision) is fail_move
    if other_writer == "ordinary_save":
        assert stored["general"]["page_size"] == env.settings.general.page_size == 51


def test_sports_exclusion_save_emits_a_sports_event(monkeypatch):
    """The sports wanted rows are computed live against the exclusion and
    monitoring settings, so saving them has to invalidate the client's cached
    sports queries. The 'sports' socketio event is the one the reducer maps to
    the whole sports query root, wanted included."""
    from app import config

    executed = []
    events = []

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.event_handler",
        SimpleNamespace(event_stream=lambda **kwargs: events.append(kwargs)),
    )

    # Only these two are put back afterwards. Removing the whole section would
    # take every other sports setting away from the tests that run after this one.
    monkeypatch.setattr(config.settings.sportarr, "excluded_sports", [])
    monkeypatch.setattr(config.settings.sportarr, "only_monitored", True)

    config.save_settings(
        [
            ("settings-sportarr-excluded_sports", ["Golf"]),
            ("settings-sportarr-only_monitored", ["false"]),
        ]
    )
    assert config.settings.sportarr["excluded_sports"] == ["Golf"]
    assert config.settings.sportarr["only_monitored"] is False

    assert {"type": "badges"} in events
    assert {"type": "sports"} in events


def test_unrelated_saves_do_not_emit_a_sports_event(monkeypatch):
    from app import config

    executed = []
    events = []

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.event_handler",
        SimpleNamespace(event_stream=lambda **kwargs: events.append(kwargs)),
    )

    config.save_settings(
        [("settings-general-instance_name", ["Bazarr"])]
    )
    assert all(event.get("type") != "sports" for event in events)


def test_sports_library_settings_survive_a_save(monkeypatch):
    """The sports library selections round-trip through the settings save the
    way the movie and series ones do.

    Only Plex's, now: Jellyfin's libraries are instance rows and its whole
    section is import-only, so a settings save carrying one is refused rather
    than writing a value nothing reads."""
    from app import config

    executed = []

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: executed.append(statement)),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )

    previous_plex = config.settings.plex.sports_library
    previous_plex_ids = config.settings.plex.sports_library_ids
    try:
        config.save_settings(
            [
                ("settings-plex-sports_library", ["Sports"]),
                ("settings-plex-sports_library_ids", ["3"]),
            ]
        )
        assert config.settings.plex.sports_library == ["Sports"]
        assert config.settings.plex.sports_library_ids == ["3"]
        with pytest.raises(ValidationError):
            config.save_settings([("settings-jellyfin-sports_library_ids", ["9"])])
    finally:
        config.settings.plex.sports_library = previous_plex
        config.settings.plex.sports_library_ids = previous_plex_ids


@pytest.mark.parametrize('enabled', [True, False])
def test_audio_parsing_changes_rescan_sports_and_refresh_native_libraries(monkeypatch, enabled):
    from app import config

    effects = []
    scans = []
    def schedule_scan(**kwargs):
        scans.append(kwargs)
        effects.append(('sports', config.settings.general.parse_embedded_audio_track))
    monkeypatch.setattr(config, 'write_config', lambda: True)
    monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
    monkeypatch.setattr(config.settings.validators, 'validate', lambda: None)
    monkeypatch.setattr(config.settings.general, 'parse_embedded_audio_track', not enabled)
    monkeypatch.setattr(config.settings.general, 'use_sonarr', True)
    monkeypatch.setattr(config.settings.general, 'use_radarr', True)
    monkeypatch.setattr(config.settings.general, 'use_sportarr', True)
    monkeypatch.setitem(sys.modules, 'app.database', SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None),
        update=lambda model: _FakeUpdate(), System=object))
    monkeypatch.setitem(sys.modules, 'app.scheduler', SimpleNamespace(scheduler=None))
    monkeypatch.setitem(sys.modules, 'subtitles.indexer.sports', SimpleNamespace(
        sports_full_scan_subtitles=schedule_scan))
    monkeypatch.setitem(sys.modules, 'sonarr.sync.series', SimpleNamespace(
        update_series=lambda: effects.append(('series', config.settings.general.parse_embedded_audio_track))))
    monkeypatch.setitem(sys.modules, 'radarr.sync.movies', SimpleNamespace(
        update_movies=lambda: effects.append(('movies', config.settings.general.parse_embedded_audio_track))))

    config.save_settings([('settings-general-parse_embedded_audio_track', [str(enabled).lower()])])

    assert sorted(effects) == [('movies', enabled), ('series', enabled), ('sports', enabled)]
    assert scans[0]['refresh_audio'] is True
    assert scans[0]['audio_mode'] is enabled
    first_request = scans[0]['audio_refresh_id']
    assert isinstance(first_request, str) and first_request
    config.save_settings([('settings-general-parse_embedded_audio_track', [str(not enabled).lower()])])
    config.save_settings([('settings-general-parse_embedded_audio_track', [str(enabled).lower()])])
    assert [scan['audio_mode'] for scan in scans] == [enabled, not enabled, enabled]
    assert len({scan['audio_refresh_id'] for scan in scans}) == 3
    effects.clear()
    config.save_settings([('settings-general-instance_name', ['Bazarr'])])
    assert effects == []


@pytest.mark.parametrize('key', ['enabled', 'serve_local_subs'])
def test_hub_local_availability_refreshes_recording_schedule_after_save(monkeypatch, key):
    from app import config

    calls = []
    monkeypatch.setattr(config, 'write_config', lambda: True)
    monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
    monkeypatch.setattr(config.settings.validators, 'validate', lambda: None)
    monkeypatch.setitem(sys.modules, 'app.database', SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None), update=lambda model: _FakeUpdate(), System=object))
    monkeypatch.setitem(sys.modules, 'app.scheduler', SimpleNamespace(scheduler=SimpleNamespace(
        update_configurable_tasks=lambda: calls.append(bool(getattr(config.settings.compat_endpoint, key))))))
    monkeypatch.setitem(sys.modules, 'app.event_handler', SimpleNamespace(event_stream=lambda **kwargs: None))
    config.save_settings([(f'settings-compat_endpoint-{key}', [True])])
    assert calls == [True]


@pytest.mark.parametrize("outcome", ["saved", "invalid", "unwritable"])
def test_a_missing_subtitles_input_queues_the_recalculation_only_once_saved(monkeypatch, outcome):
    """A refused save changed nothing, so it must not start a library-wide pass.

    The recalculation used to be queued while the submitted values were still
    being applied, before validation and before the write, so a save refused
    for another field or by a full disk still recomputed every library.
    """
    from app import config
    from subtitles.indexer import missing_refresh

    queued = []
    monkeypatch.setattr(missing_refresh, "queue_missing_subtitles_recalculation",
                        lambda *a, **kw: queued.append(True))
    monkeypatch.setattr(config, "write_config", lambda: outcome != "unwritable")
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)

    def validate():
        if outcome == "invalid":
            raise ValidationError("synthetic invalid value")

    monkeypatch.setattr(config.settings.validators, "validate", validate)
    monkeypatch.setattr(config, "restore_persisted_settings", lambda: None)
    monkeypatch.setattr(config.settings.general, "use_embedded_subs", True)
    monkeypatch.setattr(config.settings.general, "use_sportarr", False)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda _statement: None),
        update=lambda _model: _FakeUpdate(), System=object,
    ))

    items = [("settings-general-use_embedded_subs", ["false"])]
    if outcome == "saved":
        config.save_settings(items)
        assert queued == [True]
    else:
        with pytest.raises(ValidationError):
            config.save_settings(items)
        assert queued == [], "a refused save queued a library-wide recalculation"


# What a save sets off from the values it carries, keyed by the submitted setting.
# Each value is (settings the save starts from, items submitted, effects expected
# once the save is written).
_SAVE_SIDE_EFFECT_CASES = {
    "undefined embedded subtitles language": (
        {"general": {"default_und_embedded_subtitles_lang": ""}},
        [("settings-general-default_und_embedded_subtitles_lang", ["en"])],
        ["series scan", "movies scan", "sports scan"]),
    "embedded subtitles": (
        {"general": {"use_embedded_subs": True}},
        [("settings-general-use_embedded_subs", ["false"])],
        ["sports scan", "missing subtitles recalculation"]),
    "embedded subtitles parser": (
        {"general": {"embedded_subtitles_parser": "ffprobe"}},
        [("settings-general-embedded_subtitles_parser", ["mediainfo"])],
        ["sports scan"]),
    "audio track parsing": (
        {"general": {"parse_embedded_audio_track": False}},
        [("settings-general-parse_embedded_audio_track", ["true"])],
        ["sports scan", "sonarr sync", "radarr sync"]),
    "addic7ed login": (
        {"addic7ed": {"username": "before"}},
        [("settings-addic7ed-username", ["after"])],
        ["clear addic7ed_data", "throttled providers", "compat pool"]),
    "legendasdivx login": (
        {"legendasdivx": {"password": "before"}},
        [("settings-legendasdivx-password", ["after"])],
        ["clear legendasdivx_cookies2", "throttled providers", "compat pool"]),
    "opensubtitles login": (
        {"opensubtitles": {"username": "before"}},
        [("settings-opensubtitles-username", ["after"])],
        ["clear os_token", "throttled providers", "compat pool"]),
    "opensubtitles.com login": (
        {"opensubtitlescom": {"password": "before"}},
        [("settings-opensubtitlescom-password", ["after"])],
        ["clear oscom_token", "throttled providers", "compat pool"]),
    "titlovi login": (
        {"titlovi": {"username": "before"}},
        [("settings-titlovi-username", ["after"])],
        ["clear titlovi_token", "throttled providers", "compat pool"]),
    "subsource key": (
        {"subsource": {"apikey": "before"}},
        [("settings-subsource-apikey", ["after"])],
        ["throttled providers", "compat pool"]),
    "enabled providers": (
        {"general": {"enabled_providers": []}},
        [("settings-general-enabled_providers", ["subsource"])],
        ["compat pool"]),
    "fan-out pool size": (
        {"compat_endpoint": {"fanout_max_workers": 32}},
        [("settings-compat_endpoint-fanout_max_workers", ["48"])],
        ["fanout pool"]),
    "score modifiers": (
        {"general": {"provider_score_modifiers": {}}},
        [("settings-general-provider_score_modifiers", ['{"whisperai": 25}'])],
        ["compat cache"]),
    # Subtitles saved from then on are named with it.
    "hearing-impaired extension": (
        {"general": {"hi_extension": "hi"}},
        [("settings-general-hi_extension", ["sdh"])],
        ["hi extension sdh"]),
    # The same login sent back unchanged is not a new login.
    "unchanged opensubtitles login": (
        {"opensubtitles": {"username": "same"}},
        [("settings-opensubtitles-username", ["same"])],
        []),
}


@pytest.mark.parametrize("outcome", ["saved", "invalid", "unwritable"])
@pytest.mark.parametrize("case", sorted(_SAVE_SIDE_EFFECT_CASES))
def test_a_refused_save_queues_no_library_job_and_resets_no_provider(monkeypatch, case, outcome):
    """Library jobs, provider logins and provider pools follow a save only once it is written.

    They used to be set off while the submitted values were still being applied,
    before validation and before the write. A save refused for another field or by
    the disk then still reindexed sports with the rejected audio mode, synced every
    series and movie, signed providers out and rebuilt their pools, while the page
    reported that nothing had been saved.
    """
    from app import config

    starting, items, expected = _SAVE_SIDE_EFFECT_CASES[case]
    for section, values in starting.items():
        for name, value in values.items():
            monkeypatch.setattr(getattr(config.settings, section), name, value)
    for name in ("use_sonarr", "use_radarr", "use_sportarr"):
        monkeypatch.setattr(config.settings.general, name, True)
    monkeypatch.setenv("SZ_HI_EXTENSION", "hi")

    effects = _record_save_side_effects(monkeypatch)
    record = effects.append

    def validate():
        if outcome == "invalid":
            raise ValidationError("synthetic invalid value")

    restored = []
    monkeypatch.setattr(config.settings.validators, "validate", validate)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config, "write_config", lambda: outcome != "unwritable")
    monkeypatch.setattr(config, "restore_persisted_settings", lambda: restored.append(True))

    if outcome == "saved":
        config.save_settings(items)
    else:
        with pytest.raises(ValidationError):
            config.save_settings(items)
    if os.environ["SZ_HI_EXTENSION"] != "hi":
        record(f"hi extension {os.environ['SZ_HI_EXTENSION']}")

    if outcome == "saved":
        assert sorted(effects) == sorted(expected)
        assert restored == []
    else:
        assert effects == [], "a refused save acted on values it did not keep"
        assert restored == [True]


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_save_its_own_validators_refuse_starts_no_library_job(metadata_save_environment, monkeypatch, kind):
    """The same, with the real validators: a boolean timeout sent beside a new audio setting.

    On a settings file of its own, since validating replaces whole sections of the
    settings it checks, and a value patched on the replaced section is never put back.
    """
    env = metadata_save_environment
    env.settings.validators.register(*env.config.validators)
    env.settings.general.parse_embedded_audio_track = False
    getattr(env.settings, kind).http_timeout = 60
    for name in ("use_sonarr", "use_radarr", "use_sportarr"):
        setattr(env.settings.general, name, True)
    assert env.config.write_config() is True
    disk = env.path.read_bytes()
    effects = _record_save_side_effects(monkeypatch)

    with pytest.raises(ValidationError, match=f"{kind}.http_timeout"):
        env.config.save_settings([("settings-general-parse_embedded_audio_track", ["true"]),
                                  (f"settings-{kind}-http_timeout", ["true"])])

    assert effects == []
    assert env.path.read_bytes() == disk
    assert env.settings.general.parse_embedded_audio_track is False
    assert getattr(env.settings, kind).http_timeout == 60


@pytest.mark.parametrize("failing", ["clear os_token", "throttled providers", "sports scan",
                                     "sonarr sync", "missing subtitles recalculation"])
def test_a_written_metadata_save_keeps_its_values_when_a_follow_up_fails(
    metadata_save_environment, monkeypatch, failing,
):
    """A login reset or a job that fails once the save is on disk undoes nothing.

    The save is reported as written with a failed refresh, and the live settings keep
    what the file holds. Treated as unsaved, the new TMDB token was taken back out of
    the live settings only, and the next save of anything else wrote the old one to disk.
    """
    import yaml
    from secret_store import decrypt_settings_dict

    env = metadata_save_environment
    env.settings.general.parse_embedded_audio_track = False
    env.settings.general.use_embedded_subs = True
    env.settings.opensubtitles.username = "before"
    for name in ("use_sonarr", "use_radarr", "use_sportarr"):
        setattr(env.settings.general, name, True)
    assert env.config.write_config() is True
    before = env.config.get_settings()["discover"]
    effects = _record_save_side_effects(monkeypatch, fail=failing)
    replacement = "5ecafe33cafe33cafe33cafe33cafe33"

    with pytest.raises(env.config.MetadataFollowupError):
        env.config.save_settings([
            ("settings-discover-tmdb_access_token", [replacement]),
            ("settings-general-parse_embedded_audio_track", ["true"]),
            ("settings-general-use_embedded_subs", ["false"]),
            ("settings-opensubtitles-username", ["after"]),
        ])

    assert effects[-1] == failing
    stored = decrypt_settings_dict(yaml.safe_load(env.path.read_text()))
    assert stored["discover"]["tmdb_access_token"] == replacement
    assert stored["general"]["parse_embedded_audio_track"] is True
    assert env.settings.discover.tmdb_access_token == replacement
    assert env.settings.general.parse_embedded_audio_track is True
    assert env.config.get_settings()["discover"]["metadata_revision"] != before["metadata_revision"]


@pytest.mark.parametrize("failing", ["clear os_token", "throttled providers", "sports scan",
                                     "sonarr sync", "missing subtitles recalculation"])
def test_a_written_save_whose_follow_up_fails_is_reported_as_saved(
    metadata_save_environment, monkeypatch, failing,
):
    """A login reset or a library job that fails once the save is on disk is a refresh failure.

    Reported as the plain failure, the settings endpoint took the save for an unsaved
    one, and the languages, profiles and notifiers of the same request were not written.
    """
    import yaml
    from secret_store import decrypt_settings_dict

    env = metadata_save_environment
    env.settings.general.parse_embedded_audio_track = False
    env.settings.general.use_embedded_subs = True
    env.settings.opensubtitles.username = "before"
    for name in ("use_sonarr", "use_radarr", "use_sportarr"):
        setattr(env.settings.general, name, True)
    assert env.config.write_config() is True
    effects = _record_save_side_effects(monkeypatch, fail=failing)

    with pytest.raises(env.config.SettingsFollowupError) as error:
        env.config.save_settings([
            ("settings-general-parse_embedded_audio_track", ["true"]),
            ("settings-general-use_embedded_subs", ["false"]),
            ("settings-opensubtitles-username", ["after"]),
        ])

    assert not isinstance(error.value, env.config.MetadataFollowupError)
    assert effects[-1] == failing
    stored = decrypt_settings_dict(yaml.safe_load(env.path.read_text()))
    assert stored["general"]["parse_embedded_audio_track"] is True
    assert stored["opensubtitles"]["username"] == "after"
    assert env.settings.general.parse_embedded_audio_track is True
    assert env.settings.opensubtitles.username == "after"


def test_a_written_master_switch_save_keeps_the_switch_when_a_follow_up_fails(
    metadata_save_environment, monkeypatch,
):
    """The media-server switch the file holds is the one left live after a failed job."""
    import yaml
    from media_servers import dispatcher

    env = metadata_save_environment
    env.settings.general.use_silo = False
    env.settings.general.parse_embedded_audio_track = False
    env.settings.general.use_sportarr = True
    assert env.config.write_config() is True
    monkeypatch.setattr(dispatcher, "_configuration", dispatcher.NativeConfiguration(env.settings))
    effects = _record_save_side_effects(monkeypatch, fail="sports scan")

    with pytest.raises(env.config.SettingsFollowupError):
        env.config.save_settings([("settings-general-use_silo", ["true"]),
                                  ("settings-general-parse_embedded_audio_track", ["true"])])

    assert effects[-1] == "sports scan"
    assert yaml.safe_load(env.path.read_text())["general"]["use_silo"] is True
    assert env.settings.general.use_silo is True
    assert dispatcher.get_native_configuration().masters["silo"] is True


def _record_save_side_effects(monkeypatch, fail=None):
    """Stand in for everything a save can set off, and list what it did, in order.

    Each stand-in replaces a whole module, so nothing of the application is imported
    for it, not even while a test has put a stand-in database in place. The one named
    by `fail` raises once it has been recorded.
    """
    from app import config

    effects = []

    def record(effect):
        effects.append(effect)
        if effect == fail:
            raise RuntimeError("synthetic follow-up failure")

    monkeypatch.setitem(sys.modules, "subtitles.indexer.series", SimpleNamespace(
        series_full_scan_subtitles=lambda **_kw: record("series scan")))
    monkeypatch.setitem(sys.modules, "subtitles.indexer.movies", SimpleNamespace(
        movies_full_scan_subtitles=lambda **_kw: record("movies scan")))
    monkeypatch.setitem(sys.modules, "subtitles.indexer.sports", SimpleNamespace(
        sports_full_scan_subtitles=lambda **_kw: record("sports scan")))
    monkeypatch.setitem(sys.modules, "sonarr.sync.series", SimpleNamespace(
        update_series=lambda: record("sonarr sync")))
    monkeypatch.setitem(sys.modules, "radarr.sync.movies", SimpleNamespace(
        update_movies=lambda: record("radarr sync")))
    monkeypatch.setitem(sys.modules, "subtitles.indexer.missing_refresh", SimpleNamespace(
        queue_missing_subtitles_recalculation=lambda *_a, **_kw: record("missing subtitles recalculation")))
    monkeypatch.setattr(config, "region", SimpleNamespace(delete=lambda key: record(f"clear {key}")))
    monkeypatch.setitem(sys.modules, "app.get_providers", SimpleNamespace(
        reset_throttled_providers=lambda **_kw: record("throttled providers")))
    monkeypatch.setitem(sys.modules, "subliminal_patch.core_persistent", SimpleNamespace(
        reset_pool=lambda: record("fanout pool")))
    monkeypatch.setitem(sys.modules, "compat.service", SimpleNamespace(
        reset_compat_pool=lambda: record("compat pool")))
    monkeypatch.setitem(sys.modules, "compat.cache", SimpleNamespace(
        invalidate_all=lambda: record("compat cache")))
    monkeypatch.setitem(sys.modules, "provider_hub.state", SimpleNamespace(
        load_state=lambda: {"installations": {}}))
    monkeypatch.setitem(sys.modules, "app.scheduler", SimpleNamespace(scheduler=None))
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda _statement: None),
        update=lambda _model: _FakeUpdate(), System=object,
    ))
    return effects


def test_a_section_a_save_created_can_be_removed_for_good(metadata_save_environment):
    """The startup cleanup of retired provider sections removes one a save created.

    Dynaconf keeps a section created through set() as a default of its own: unset()
    skipped it unless forced, and a later reload() put a forced one back. A save
    creates a missing section, so a cleanup running after one left it in place.
    """
    import yaml

    env = metadata_save_environment
    env.config.save_settings([("settings-retiredhub-api_token", ["token-value"])])
    assert yaml.safe_load(env.path.read_text())["retiredhub"] == {"api_token": "token-value"}

    env.config.remove_settings_section("retiredhub")
    env.config.write_config()

    assert "RETIREDHUB" not in env.settings.store
    assert "retiredhub" not in {key.lower() for key in env.settings.as_dict()}
    assert "retiredhub" not in yaml.safe_load(env.path.read_text())
    # A refused save reloads the settings from disk, which must not bring it back.
    env.config.restore_persisted_settings()
    assert env.settings.get("retiredhub") is None


@pytest.mark.parametrize("assignment", ["set", "item", "attribute"])
def test_a_section_code_added_can_be_removed_for_good(metadata_save_environment, assignment):
    """A section added in code, not loaded from the file, is removed and stays removed.

    Each of these assignments keeps the section as a Dynaconf default: unset() skipped
    it unless forced, and a forced unset left the default behind for the next reload(),
    such as the one that undoes a refused save, to put back with its old values.
    """
    env = metadata_save_environment
    if assignment == "set":
        env.settings.set("codehub", {"api_token": "token-value"})
    elif assignment == "item":
        env.settings["codehub"] = {"api_token": "token-value"}
    else:
        env.settings.codehub = {"api_token": "token-value"}
    assert env.settings.codehub.api_token == "token-value"

    env.config.remove_settings_section("codehub")

    assert "CODEHUB" not in env.settings.store
    assert "codehub" not in {key.lower() for key in env.settings.as_dict()}
    env.config.restore_persisted_settings()
    assert env.settings.get("codehub") is None
    assert "codehub" not in {key.lower() for key in env.settings.as_dict()}


@pytest.mark.parametrize("outcome", ["invalid", "unwritable"])
def test_a_refused_save_keeps_no_section_it_created(metadata_save_environment, monkeypatch, outcome):
    """A section that first appears in a refused save is gone again afterwards.

    Reloading from disk is what undoes a refused save, and it kept a section the
    save had created, so a new provider's rejected credentials stayed live and the
    next save of anything else wrote them to disk.
    """
    env = metadata_save_environment
    if outcome == "invalid":
        def fail():
            raise ValidationError("synthetic invalid value")
        monkeypatch.setattr(env.settings.validators, "validate", fail)
    else:
        monkeypatch.setattr(env.config, "write_config", lambda: False)
    disk = env.path.read_bytes()

    with pytest.raises(ValidationError):
        env.config.save_settings([("settings-newhub-api_token", ["rejected-token"])])

    assert env.settings.get("newhub") is None
    assert "newhub" not in {key.lower() for key in env.settings.as_dict()}
    assert env.path.read_bytes() == disk
    assert env.executed == env.resets == []


def test_a_section_a_save_created_survives_the_reload_of_a_later_refused_save(metadata_save_environment):
    env = metadata_save_environment
    env.config.save_settings([("settings-newhub-api_token", ["kept-token"])])

    env.config.restore_persisted_settings()

    assert env.settings.newhub.api_token == "kept-token"
