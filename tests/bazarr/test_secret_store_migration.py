# coding=utf-8
"""Tests for bazarr.secret_store.migration.

Sanity coverage for the encrypt/decrypt-dict helpers and the in-place
decrypter. Auto-migration end-to-end (load plaintext config -> live
plaintext memory -> persist as ciphertext -> reload -> decrypt back) is
covered in commit 5's test_secret_store_e2e.py.
"""
from unittest.mock import patch  # noqa: F401

import pytest

from secret_store.crypto import (
    SECRET_MARKER_PREFIX,
    encrypt_secret,
    get_master_key,  # noqa: F401
    is_encrypted,  # noqa: F401
)
from secret_store.migration import (
    decrypt_settings_dict,
    decrypt_settings_in_place,
    encrypt_settings_dict,
)


@pytest.fixture(autouse=True)
def fixed_master_key(monkeypatch):
    """Patch get_master_key so tests don't reach for app.config.settings."""
    key = "test-master-key-not-secret-but-stable-for-tests"
    monkeypatch.setattr(
        "secret_store.crypto.get_master_key",
        lambda settings_obj=None: key,
    )
    yield key


@pytest.fixture
def plaintext_dict():
    return {
        # Mirrors the post-bootstrap shape: get_master_key has already
        # generated and persisted general.secrets_encryption_key. The
        # encrypt-settings-dict path expects the master key to be in
        # the snapshot so on-disk ciphertext stays paired with the key
        # that decrypts it (Codex P1 fix on a first-write race).
        "general": {
            "flask_secret_key": "system",
            "instance_name": "Bazarr+",
            "secrets_encryption_key": "test-master-key-not-secret-but-stable-for-tests",
        },
        "sonarr": {"apikey": "sonarr-plain-key", "url": "http://sonarr"},
        "radarr": {"apikey": "radarr-plain-key"},
        "translator": {
            "gemini_keys": ["gemini-1", "gemini-2", ""],
            "openrouter_api_key": "openrouter-key",
        },
        "compat_endpoint": {
            "token": "compat-user-token",
            "jwt_secret": "system-jwt",
        },
    }


def test_encrypt_settings_dict_encrypts_user_visible(plaintext_dict):
    out = encrypt_settings_dict(plaintext_dict)
    # User-visible scalars get the marker
    assert out["sonarr"]["apikey"].startswith(SECRET_MARKER_PREFIX)
    assert out["radarr"]["apikey"].startswith(SECRET_MARKER_PREFIX)
    assert out["compat_endpoint"]["token"].startswith(SECRET_MARKER_PREFIX)
    assert out["translator"]["openrouter_api_key"].startswith(SECRET_MARKER_PREFIX)


def test_encrypt_settings_dict_leaves_system_secrets_alone(plaintext_dict):
    """SYSTEM_SECRETS are NOT encrypted by this helper - they're masked
    by the API serializer (commit 3) and otherwise stay plaintext on disk.
    encrypt_settings_dict's job is the user-visible tier only."""
    out = encrypt_settings_dict(plaintext_dict)
    assert out["general"]["flask_secret_key"] == "system"
    assert out["compat_endpoint"]["jwt_secret"] == "system-jwt"


def test_encrypt_settings_dict_handles_lists(plaintext_dict):
    out = encrypt_settings_dict(plaintext_dict)
    keys = out["translator"]["gemini_keys"]
    assert keys[0].startswith(SECRET_MARKER_PREFIX)
    assert keys[1].startswith(SECRET_MARKER_PREFIX)
    assert keys[2] == ""  # empty stays empty - no point encrypting nothing


def test_encrypt_settings_dict_does_not_mutate_input(plaintext_dict):
    """Caller hands in the live snapshot; we must not mutate it to
    ciphertext or in-memory reads break."""
    out = encrypt_settings_dict(plaintext_dict)
    assert plaintext_dict["sonarr"]["apikey"] == "sonarr-plain-key"
    assert out is not plaintext_dict


def test_encrypt_dict_idempotent_on_already_encrypted(plaintext_dict):
    """Re-encrypting an already-encrypted dict is a no-op (encrypt_secret
    short-circuits on its own marker)."""
    once = encrypt_settings_dict(plaintext_dict)
    twice = encrypt_settings_dict(once)
    assert twice == once


def test_decrypt_dict_inverts_encrypt(plaintext_dict):
    encrypted = encrypt_settings_dict(plaintext_dict)
    decrypted = decrypt_settings_dict(encrypted)
    assert decrypted == plaintext_dict


def test_decrypt_dict_passes_plaintext_through(plaintext_dict):
    """Pre-migration shape: disk has plaintext. The decrypt helper must
    not corrupt it - that's how auto-migration works."""
    out = decrypt_settings_dict(plaintext_dict)
    assert out == plaintext_dict


def test_decrypt_dict_tolerates_corrupt_cipher(plaintext_dict):
    """A bad ciphertext should not raise from decrypt_settings_dict;
    write_config uses this for comparison and a single bad cipher would
    otherwise crash every save. The bad value stays in the comparison
    form, which forces the diff-and-rewrite path."""
    encrypted = encrypt_settings_dict(plaintext_dict)
    # corrupt one cipher
    encrypted["sonarr"]["apikey"] = SECRET_MARKER_PREFIX + "garbage"
    out = decrypt_settings_dict(encrypted)
    assert out["sonarr"]["apikey"] == SECRET_MARKER_PREFIX + "garbage"


class _FakeSection(dict):
    """Stand-in for a dynaconf section: behaves like a dict and supports
    attribute access too (settings.sonarr.apikey)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name, value):
        self[name] = value


class _FakeSettings:
    """Stand-in for the live dynaconf settings root."""

    def __init__(self, data):
        for section, values in data.items():
            setattr(self, section, _FakeSection(values))


def test_decrypt_settings_in_place_decrypts_encrypted_values():
    encrypted_apikey = encrypt_secret("real-sonarr-key")
    settings = _FakeSettings({
        "sonarr": {"apikey": encrypted_apikey, "url": "http://x"},
        "radarr": {"apikey": "radarr-plain-already"},
        "translator": {"gemini_keys": [encrypt_secret("g1"), "g2-plain"]},
        "general": {},
        "auth": {},
        "compat_endpoint": {},
        "plex": {},
        "jellyfin": {},
        "proxy": {},
        "postgresql": {},
        "opensubtitles": {},
        "opensubtitlescom": {},
        "addic7ed": {},
        "legendasdivx": {},
        "legendasnet": {},
        "xsubs": {},
        "deathbycaptcha": {},
        "napisy24": {},
        "titlovi": {},
        "titulky": {},
        "karagarga": {},
        "assrt": {},
        "betaseries": {},
        "jimaku": {},
        "subdl": {},
        "subsource": {},
        "subx": {},
        "subsro": {},
        "omdb": {},
    })
    decrypt_settings_in_place(settings)
    assert settings.sonarr.apikey == "real-sonarr-key"
    assert settings.radarr.apikey == "radarr-plain-already"
    assert settings.translator.gemini_keys == ["g1", "g2-plain"]


def test_decrypt_settings_in_place_tolerates_bad_cipher_per_field(caplog):
    """A single corrupt cipher must not stop bazarr from booting. The
    bad value stays in place; the operator can rotate that one credential
    via the Settings page."""
    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    good = encrypt_secret("good-key")
    settings = _FakeSettings({
        "sonarr": {"apikey": bad},
        "radarr": {"apikey": good},
        "general": {}, "auth": {}, "compat_endpoint": {}, "plex": {},
        "jellyfin": {}, "translator": {}, "proxy": {}, "postgresql": {},
        "opensubtitles": {}, "opensubtitlescom": {}, "addic7ed": {},
        "legendasdivx": {}, "legendasnet": {}, "xsubs": {},
        "deathbycaptcha": {}, "napisy24": {}, "titlovi": {}, "titulky": {},
        "karagarga": {}, "assrt": {}, "betaseries": {}, "jimaku": {},
        "subdl": {}, "subsource": {}, "subx": {}, "subsro": {}, "omdb": {},
    })
    with caplog.at_level("ERROR"):
        decrypt_settings_in_place(settings)
    assert settings.radarr.apikey == "good-key"
    # bad value untouched, error logged without leaking the value
    assert settings.sonarr.apikey == bad
    assert any("Failed to decrypt secret" in r.message for r in caplog.records)
    # The error message must NOT include the section/key name (which is
    # narrow enough that "sonarr.apikey corrupt" leaks deployment shape).
    for r in caplog.records:
        assert "sonarr" not in r.message.lower()
