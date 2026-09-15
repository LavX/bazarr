# coding=utf-8
"""Seerr settings exist with safe defaults and the key is a user-visible secret."""


def test_seerr_defaults():
    from app.config import settings
    assert settings.general.use_seerr is False
    assert settings.seerr.url == ''
    assert settings.seerr.apikey == ''
    assert settings.seerr.verify_ssl is True
    assert settings.seerr.external_url == ''


def test_seerr_apikey_is_encrypted_at_rest_and_visible_to_the_settings_form():
    from secret_store.registry import USER_VISIBLE_SECRETS, IMPORT_ONLY_SECTIONS
    assert "seerr.apikey" in USER_VISIBLE_SECRETS
    assert "seerr" not in IMPORT_ONLY_SECTIONS


def test_use_seerr_is_not_a_native_master_key():
    from app.config import NATIVE_MASTER_KEYS
    assert 'settings-general-use_seerr' not in NATIVE_MASTER_KEYS
