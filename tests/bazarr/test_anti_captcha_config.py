# coding=utf-8
import os
import sys
from types import SimpleNamespace

import pytest


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


@pytest.fixture
def captcha_env(monkeypatch):
    """Seed both captcha env vars so configure_captcha_func's writes are
    restored on teardown; these tests run in one shared CI process."""
    monkeypatch.setenv("ANTICAPTCHA_CLASS", "sentinel-class")
    monkeypatch.setenv("ANTICAPTCHA_ACCOUNT_KEY", "sentinel-key")


def _fake_settings(provider, anticaptcha_key="", dbc_user="", dbc_pass="", captchaai_key=""):
    return SimpleNamespace(
        general=SimpleNamespace(anti_captcha_provider=provider),
        anticaptcha=SimpleNamespace(anti_captcha_key=anticaptcha_key),
        deathbycaptcha=SimpleNamespace(username=dbc_user, password=dbc_pass),
        captchaai=SimpleNamespace(captchaai_key=captchaai_key),
    )


def test_validator_accepts_captchaai():
    from app import config

    validator = next(
        v for v in config.validators if "general.anti_captcha_provider" in v.names
    )
    assert "captchaai" in validator.operations["is_in"]


def test_configure_captcha_func_exports_captchaai(captcha_env, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "settings", _fake_settings("captchaai", captchaai_key="key-123"))
    config.configure_captcha_func()
    assert os.environ["ANTICAPTCHA_CLASS"] == "CaptchaAIProxyLess"
    assert os.environ["ANTICAPTCHA_ACCOUNT_KEY"] == "key-123"


def test_configure_captcha_func_captchaai_empty_key_disables(captcha_env, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "settings", _fake_settings("captchaai", captchaai_key=""))
    config.configure_captcha_func()
    assert os.environ["ANTICAPTCHA_CLASS"] == ""
    # Disabling must also drop the previously exported credential.
    assert os.environ["ANTICAPTCHA_ACCOUNT_KEY"] == ""


def test_configure_captcha_func_anti_captcha_still_works(captcha_env, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "settings", _fake_settings("anti-captcha", anticaptcha_key="ak"))
    config.configure_captcha_func()
    assert os.environ["ANTICAPTCHA_CLASS"] == "AntiCaptchaProxyLess"
    assert os.environ["ANTICAPTCHA_ACCOUNT_KEY"] == "ak"


def test_save_settings_reconfigures_captcha_on_captchaai_key(monkeypatch):
    from app import config

    called = []
    monkeypatch.setattr(config, "write_config", lambda: None)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setattr(config, "configure_captcha_func", lambda: called.append(True))
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: None),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )

    previous = config.settings.captchaai.captchaai_key
    try:
        config.save_settings([("settings-captchaai-captchaai_key", ["key-456"])])

        assert called, "saving the CaptchaAI key must re-run captcha configuration"
        assert config.settings.captchaai.captchaai_key == "key-456"
    finally:
        config.settings.captchaai.captchaai_key = previous


@pytest.mark.parametrize("name", ["CaptchaAIProxyLess"])
def test_pitcher_registry_has_captchaai(name):
    from subliminal_patch.pitcher import pitchers

    cls = pitchers.get_pitcher(name)
    assert cls.name == name


def test_pitcher_registry_has_captchaai_proxy_variant():
    from subliminal_patch.pitcher import pitchers

    cls = pitchers.get_pitcher("CaptchaAI", with_proxy=True)
    assert cls.name == "CaptchaAI"


def test_pitcher_registry_source_aliases_unchanged():
    """The by-source lookups must keep resolving to the reCAPTCHA pitchers;
    an image class re-claiming a domain's alias would silently break them."""
    from subliminal_patch.pitcher import pitchers

    assert pitchers.get_pitcher("anti-captcha.com").name == "AntiCaptchaProxyLess"
    assert pitchers.get_pitcher("deathbycaptcha.com").name == "DeathByCaptchaProxyLess"
    assert pitchers.get_pitcher("captchaai.com").name == "CaptchaAIProxyLess"


def test_pitcher_registry_resolves_captchaai_from_env(monkeypatch):
    monkeypatch.setenv("ANTICAPTCHA_CLASS", "CaptchaAIProxyLess")
    from subliminal_patch.pitcher import pitchers

    cls = pitchers.get_pitcher()
    assert cls.name == "CaptchaAIProxyLess"


def test_captchaai_proxyless_forwards_caller_context(monkeypatch):
    monkeypatch.delenv("ANTICAPTCHA_ACCOUNT_KEY", raising=False)
    from subliminal_patch.pitcher import pitchers

    cls = pitchers.get_pitcher("CaptchaAIProxyLess")
    pitcher = cls(
        "Addic7ed",
        "https://example.org/login",
        "site-key",
        client_key="explicit-key",
        user_agent="UA/1.0",
        cookies={"session": "abc"},
        is_invisible=True,
    )
    params = pitcher.in_params
    assert params["invisible"] == 1
    assert params["userAgent"] == "UA/1.0"
    # The provider's session cookies must NOT be shipped to the vendor.
    assert "cookies" not in params
    assert params["key"] == "explicit-key"
