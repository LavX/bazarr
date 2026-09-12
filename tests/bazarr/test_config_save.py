# coding=utf-8
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
        config.settings.unset(provider_id.upper())


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
        config.settings.unset(provider_id.upper())


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
def test_unchanged_metadata_and_legacy_save_preserve_followup_exception(metadata_save_environment, monkeypatch, token):
    env = metadata_save_environment
    before = env.config.get_settings()["discover"]
    def fail(statement):
        raise RuntimeError("legacy-followup-error")
    monkeypatch.setattr(sys.modules["app.database"].database, "execute", fail)
    items = [("settings-general-page_size", ["50"])]
    if token is not None:
        items.append(("settings-discover-tmdb_access_token", [token]))
    with pytest.raises(RuntimeError, match="legacy-followup-error"):
        env.config.save_settings(items)
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

    try:
        config.save_settings(
            [
                ("settings-sportarr-excluded_sports", ["Golf"]),
                ("settings-sportarr-only_monitored", ["false"]),
            ]
        )
        assert config.settings.sportarr["excluded_sports"] == ["Golf"]
        assert config.settings.sportarr["only_monitored"] is False
    finally:
        config.settings.unset("sportarr")

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
    way the movie and series ones do."""
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
    previous_jellyfish = config.settings.jellyfin.sports_library
    previous_jellyfin_ids = config.settings.jellyfin.sports_library_ids
    try:
        config.save_settings(
            [
                ("settings-plex-sports_library", ["Sports"]),
                ("settings-plex-sports_library_ids", ["3"]),
                ("settings-jellyfin-sports_library", ["Sports"]),
                ("settings-jellyfin-sports_library_ids", ["9"]),
            ]
        )
        assert config.settings.plex.sports_library == ["Sports"]
        assert config.settings.plex.sports_library_ids == ["3"]
        assert config.settings.jellyfin.sports_library == ["Sports"]
        assert config.settings.jellyfin.sports_library_ids == ["9"]
    finally:
        config.settings.plex.sports_library = previous_plex
        config.settings.plex.sports_library_ids = previous_plex_ids
        config.settings.jellyfin.sports_library = previous_jellyfish
        config.settings.jellyfin.sports_library_ids = previous_jellyfin_ids
