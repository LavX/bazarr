# coding=utf-8
import os
import sys
from types import SimpleNamespace

import pytest


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


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


def test_configure_captcha_func_exports_captchaai(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "settings", _fake_settings("captchaai", captchaai_key="key-123"))
    config.configure_captcha_func()
    assert os.environ["ANTICAPTCHA_CLASS"] == "CaptchaAIProxyLess"
    assert os.environ["ANTICAPTCHA_ACCOUNT_KEY"] == "key-123"


def test_configure_captcha_func_captchaai_empty_key_disables(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "settings", _fake_settings("captchaai", captchaai_key=""))
    config.configure_captcha_func()
    assert os.environ["ANTICAPTCHA_CLASS"] == ""


def test_configure_captcha_func_anti_captcha_still_works(monkeypatch):
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

    config.save_settings([("settings-captchaai-captchaai_key", ["key-456"])])

    assert called, "saving the CaptchaAI key must re-run captcha configuration"
    assert config.settings.captchaai.captchaai_key == "key-456"


@pytest.mark.parametrize(
    "name", ["CaptchaAIProxyLess", "CaptchaAIImageToText"]
)
def test_pitcher_registry_has_captchaai(name):
    from subliminal_patch.pitcher import pitchers

    cls = pitchers.get_pitcher(name)
    assert cls.name == name


def test_pitcher_registry_resolves_captchaai_from_env(monkeypatch):
    monkeypatch.setenv("ANTICAPTCHA_CLASS", "CaptchaAIProxyLess")
    from subliminal_patch.pitcher import pitchers

    cls = pitchers.get_pitcher()
    assert cls.name == "CaptchaAIProxyLess"
