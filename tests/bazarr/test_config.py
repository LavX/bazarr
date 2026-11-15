import pytest
import yaml
from dynaconf import Dynaconf
from dynaconf.validator import ValidationError

from app import config


def test_get_settings():
    assert isinstance(config.get_settings(), dict)


def test_web_server_threads_default_and_bounds():
    # The waitress worker-thread count is configurable with a measured
    # default; the validator pins the default and the sane range.
    validator = next(
        v for v in config.validators
        if v.names == ('general.web_server_threads',)
    )
    assert validator.default == 32
    operations = validator.operations
    assert operations.get('gte') == 4
    assert operations.get('lte') == 100
    assert isinstance(config.settings.general.web_server_threads, int)


def test_sports_tag_enabled_mirrors_the_series_and_movies_keys():
    # The Languages page offers tag-based profile selection for Series and
    # Movies; the sports key mirrors them so a League tag can pick a profile.
    validator = next(
        v for v in config.validators
        if v.names == ('general.sports_tag_enabled',)
    )
    assert validator.default is False
    assert isinstance(config.settings.general.sports_tag_enabled, bool)


def test_min_source_wait_minutes_defaults_to_off_and_is_bounded_to_a_week(tmp_path):
    # The wait before automatic translation is off by default, and the bounds
    # keep a typo from holding translations back for longer than a week.
    validator = next(
        v for v in config.validators
        if v.names == ('translator.min_source_wait_minutes',)
    )
    assert validator.default == 0
    assert validator.operations.get('gte') == 0
    assert validator.operations.get('lte') == 10080
    stored = tmp_path / 'config.yaml'

    def loaded(minutes):
        stored.write_text(yaml.safe_dump({'translator': {'min_source_wait_minutes': minutes}}), encoding='utf-8')
        return Dynaconf(settings_files=[str(stored)], core_loaders=['YAML'])

    for accepted in (0, 10080):
        validator.validate(loaded(accepted))
    for refused in (-1, 10081):
        with pytest.raises(ValidationError):
            validator.validate(loaded(refused))


def test_min_source_wait_minutes_refuses_a_yaml_boolean(tmp_path):
    # YAML true loads as a bool, and bool passes isinstance(value, int), so the
    # condition is what refuses it.
    validator = next(
        v for v in config.validators
        if v.names == ('translator.min_source_wait_minutes',)
    )
    stored = tmp_path / 'config.yaml'
    stored.write_text(yaml.safe_dump({'translator': {'min_source_wait_minutes': True}}), encoding='utf-8')
    with pytest.raises(ValidationError):
        validator.validate(Dynaconf(settings_files=[str(stored)], core_loaders=['YAML']))
