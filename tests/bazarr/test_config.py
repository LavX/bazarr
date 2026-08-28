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
    assert validator.default == 16
    operations = validator.operations
    assert operations.get('gte') == 4
    assert operations.get('lte') == 100
    assert isinstance(config.settings.general.web_server_threads, int)
