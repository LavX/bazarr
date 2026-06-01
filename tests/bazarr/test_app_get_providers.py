import inspect

import pytest
from subliminal_patch.core import Language

from app import get_providers


def test_get_providers_auth():
    for val in get_providers.get_providers_auth().values():
        assert isinstance(val, dict)


def test_get_providers_auth_with_provider_registry():
    """Make sure all providers will be properly initialized with bazarr
    configs"""
    from subliminal_patch.extensions import provider_registry

    auths = get_providers.get_providers_auth()
    for key, val in auths.items():
        provider = provider_registry[key]
        sign = inspect.signature(provider.__init__)
        for sub_key in val.keys():
            if sub_key not in sign.parameters:
                raise ValueError(f"'{sub_key}' parameter not present in {provider}")

            assert sign.parameters[sub_key] is not None


def test_get_providers_auth_embeddedsubtitles():
    item = get_providers.get_providers_auth()["embeddedsubtitles"]
    assert isinstance(item["included_codecs"], list)
    assert isinstance(item["hi_fallback"], bool)
    assert isinstance(item["cache_dir"], str)
    assert isinstance(item["ffprobe_path"], str)
    assert isinstance(item["ffmpeg_path"], str)
    assert isinstance(item["timeout"], int)
    assert isinstance(item["unknown_as_fallback"], bool)
    assert isinstance(item["fallback_lang"], str)


def test_get_providers_auth_karagarga():
    item = get_providers.get_providers_auth()["karagarga"]
    assert item["username"] is not None
    assert item["password"] is not None
    assert item["f_username"] is not None
    assert item["f_password"] is not None


def test_get_language_equals_default_settings():
    assert isinstance(get_providers.get_language_equals(), list)


def test_get_language_equals_injected_settings_invalid():
    config = get_providers.settings
    config.set("general.language_equals", ["invalid"])
    assert not get_providers.get_language_equals(config)


def test_get_language_equals_injected_settings_valid():
    config = get_providers.settings
    config.set("general.language_equals", ["spa:spa-MX"])

    result = get_providers.get_language_equals(config)
    assert result == [(Language("spa"), Language("spa", "MX"))]


@pytest.mark.parametrize(
    "config_value,expected",
    [
        (["spa:spl"], (Language("spa"), Language("spa", "MX"))),
        (["por:pob"], (Language("por"), Language("por", "BR"))),
        (["zho:zht"], (Language("zho"), Language("zho", "TW"))),
    ],
)
def test_get_language_equals_injected_settings_custom_lang_alpha3(
    config_value, expected
):
    config = get_providers.settings

    config.set("general.language_equals", config_value)

    result = get_providers.get_language_equals(config)
    assert result == [expected]


def test_get_language_equals_injected_settings_multiple():
    config = get_providers.settings

    config.set(
        "general.language_equals",
        ["eng@hi:eng", "spa:spl", "spa@hi:spl", "spl@hi:spl"],
    )

    result = get_providers.get_language_equals(config)
    assert len(result) == 4


def test_get_language_equals_injected_settings_valid_multiple():
    config = get_providers.settings
    config.set("general.language_equals", ["spa:spa-MX", "spa-MX:spa"])

    result = get_providers.get_language_equals(config)
    assert result == [
        (Language("spa"), Language("spa", "MX")),
        (Language("spa", "MX"), Language("spa")),
    ]


def test_get_language_equals_injected_settings_hi():
    config = get_providers.settings
    config.set("general.language_equals", ["eng@hi:eng"])

    result = get_providers.get_language_equals(config)
    assert result == [(Language("eng", hi=True), Language("eng"))]


def test_get_provider_language_exclusions_parses_configured_languages():
    config = get_providers.settings
    original = getattr(config.general, "provider_languages", {})
    config.set(
        "general.provider_languages",
        {
            "opensubtitlescom": ["eng", "bul"],
            "subsunacs": [],
        },
    )

    try:
        result = get_providers.get_provider_language_exclusions(config)
    finally:
        config.set("general.provider_languages", original)

    assert result == {
        "opensubtitlescom": {Language("eng"), Language("bul")},
    }


def test_get_provider_language_hook_returns_configured_exclusions():
    config = get_providers.settings
    original = getattr(config.general, "provider_languages", {})
    config.set(
        "general.provider_languages",
        {
            "opensubtitlescom": ["eng"],
        },
    )

    try:
        hook = get_providers.get_provider_language_hook(config)
        result = hook("opensubtitlescom")
        unrestricted = hook("subsunacs")
    finally:
        config.set("general.provider_languages", original)

    assert result == {Language("eng")}
    assert unrestricted is None


def test_native_subtitle_pool_uses_provider_language_hook(monkeypatch):
    from subtitles import pool as subtitle_pool

    captured = {}

    class CapturingPool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(subtitle_pool, "provider_pool", lambda: CapturingPool)
    monkeypatch.setattr(
        subtitle_pool, "get_providers_sorted", lambda: ["opensubtitlescom"]
    )
    monkeypatch.setattr(subtitle_pool, "get_providers_auth", lambda: {})
    monkeypatch.setattr(subtitle_pool, "get_blacklist", lambda: [])
    monkeypatch.setattr(subtitle_pool, "get_blacklist_movie", lambda: [])
    monkeypatch.setattr(
        subtitle_pool,
        "get_ban_list",
        lambda profile_id: {"must_contain": [], "must_not_contain": []},
    )
    monkeypatch.setattr(subtitle_pool, "get_language_equals", lambda: [])
    monkeypatch.setattr(
        subtitle_pool,
        "get_provider_language_hook",
        lambda: lambda provider: {Language("eng")},
    )

    subtitle_pool._init_pool("movie")

    assert callable(captured["language_hook"])
    assert captured["language_hook"]("opensubtitlescom") == {Language("eng")}


def _get_error():
    try:
        raise ValueError("Some error" * 100)
    except ValueError as error:
        return error


def test_get_traceback_info():
    error_ = _get_error()

    if error_ is not None:
        msg = get_providers._get_traceback_info(error_)
        assert len(msg) == 100
