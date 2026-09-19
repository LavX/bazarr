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
