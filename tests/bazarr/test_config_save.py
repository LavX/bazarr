# coding=utf-8
import sys
from types import SimpleNamespace

import pytest


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


def test_save_settings_creates_missing_provider_section_for_hub_config(monkeypatch):
    from app import config

    provider_id = "testhubdynamic"
    executed = []

    monkeypatch.setattr(config, "write_config", lambda: None)
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

    monkeypatch.setattr(config, "write_config", lambda: None)
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

    monkeypatch.setattr(config, "write_config", lambda: None)
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

    monkeypatch.setattr(config, "write_config", lambda: None)
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
    monkeypatch.setattr(config, 'write_config', lambda: saved.append(
        list(config.settings.translator.openrouter_provider_order)))
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
    monkeypatch.setattr(config, 'write_config', lambda: saved.append(dict(
        routing=config.settings.translator.openrouter_provider_routing,
        order=list(config.settings.translator.openrouter_provider_order),
    )))
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
