# coding=utf-8
"""Unit tests for bazarr.secrets.crypto.

Comprehensive end-to-end coverage (migration paths, masking, key rotation,
bad-cipher diagnostics) lives in commit 5's test_secrets_e2e.py. This file
covers just the crypto primitives.
"""
from unittest.mock import MagicMock
import json
from pathlib import Path

import pytest

from secret_store.crypto import (
    SECRET_MARKER_PREFIX,
    decrypt_secret,
    encrypt_secret,
    get_master_key,
    is_encrypted,
)


_OLD_CRYPTOGRAPHY = json.loads(
    (Path(__file__).with_name('fixtures') / 'cryptography49_fernet.json').read_text(encoding='utf-8')
)


@pytest.mark.parametrize('case', _OLD_CRYPTOGRAPHY['cases'], ids=['provider', 'translator', 'unicode'])
def test_ciphertext_from_cryptography_49_remains_readable(case):
    """Persisted synthetic fixtures were encrypted by the actual 49.0.0 wheel."""
    key = _OLD_CRYPTOGRAPHY['master_key']
    assert decrypt_secret(case['ciphertext'], master_key=key) == case['plaintext']
    assert encrypt_secret(case['ciphertext'], master_key=key) == case['ciphertext']
    with pytest.raises(ValueError, match='tampered|master key'):
        decrypt_secret(case['ciphertext'], master_key='synthetic-wrong-master-key')
    with pytest.raises(ValueError, match='tampered|master key'):
        decrypt_secret(case['ciphertext'][:-2] + 'AA', master_key=key)


@pytest.fixture
def settings_obj():
    """Minimal stand-in for the dynaconf settings root with a writable
    `general.secrets_encryption_key` slot."""
    s = MagicMock()
    # MagicMock auto-creates attributes, so `s.general.secrets_encryption_key`
    # returns another MagicMock unless we set it. Force it to a real string
    # to mirror dynaconf's typed Validator behavior.
    s.general.secrets_encryption_key = ""
    return s


@pytest.fixture
def master_key(settings_obj):
    return get_master_key(settings_obj)


def test_get_master_key_generates_when_missing(settings_obj):
    settings_obj.general.secrets_encryption_key = ""
    key = get_master_key(settings_obj)
    assert isinstance(key, str)
    assert len(key) >= 32  # token_urlsafe(32) base64 is ~43 chars
    # It was persisted back into settings so subsequent boots see the same key.
    assert settings_obj.general.secrets_encryption_key == key


def test_get_master_key_reuses_existing(settings_obj):
    settings_obj.general.secrets_encryption_key = "preexisting-key-must-not-change"
    key = get_master_key(settings_obj)
    assert key == "preexisting-key-must-not-change"


def test_get_master_key_regenerates_when_whitespace_only(settings_obj):
    """An accidentally-blanked key (user edited config.yaml) gets a fresh
    one rather than failing the boot. Same lifecycle as flask_secret_key."""
    settings_obj.general.secrets_encryption_key = "   "
    key = get_master_key(settings_obj)
    assert key.strip() != ""
    assert key != "   "


def test_encrypt_decrypt_roundtrip(master_key):
    plaintext = "sonarr-api-key-1234567890abcdef"
    ciphertext = encrypt_secret(plaintext, master_key=master_key)
    assert ciphertext != plaintext
    assert ciphertext.startswith(SECRET_MARKER_PREFIX)
    assert decrypt_secret(ciphertext, master_key=master_key) == plaintext


def test_encrypt_same_plaintext_yields_different_ciphertext(master_key):
    """Salt + timestamp inside the payload means equal plaintexts MUST
    NOT produce equal ciphertext - defends against equality inference if
    config.yaml leaks."""
    a = encrypt_secret("repeated", master_key=master_key)
    b = encrypt_secret("repeated", master_key=master_key)
    assert a != b
    assert decrypt_secret(a, master_key=master_key) == "repeated"
    assert decrypt_secret(b, master_key=master_key) == "repeated"


def test_encrypt_empty_returns_empty(master_key):
    """Empty / falsy plaintext skips encryption - empty credentials are
    just empty, no migration to do."""
    assert encrypt_secret("", master_key=master_key) == ""
    assert encrypt_secret(None, master_key=master_key) is None


def test_encrypt_idempotent_on_already_encrypted(master_key):
    """A re-encrypt pass on an already-encrypted value returns it
    unchanged. Otherwise every config write would rotate ciphertext."""
    once = encrypt_secret("token", master_key=master_key)
    twice = encrypt_secret(once, master_key=master_key)
    assert once == twice


def test_encrypt_rejects_non_string(master_key):
    with pytest.raises(TypeError, match="expects str"):
        encrypt_secret(42, master_key=master_key)


def test_decrypt_passes_plaintext_through(master_key):
    """A plaintext value (no marker) is returned as-is. The migrator
    rewrites it; the read path must not raise on first boot."""
    assert decrypt_secret("plaintext-not-yet-migrated", master_key=master_key) \
        == "plaintext-not-yet-migrated"


def test_decrypt_passes_empty_through(master_key):
    assert decrypt_secret("", master_key=master_key) == ""
    assert decrypt_secret(None, master_key=master_key) is None


def test_decrypt_raises_on_tampered_ciphertext(master_key):
    valid = encrypt_secret("authentic", master_key=master_key)
    tampered = valid[:-2] + "AA"  # corrupt the signature suffix
    with pytest.raises(ValueError, match="tampered|master key"):
        decrypt_secret(tampered, master_key=master_key)


def test_decrypt_raises_on_wrong_master_key(master_key):
    """Bootstrapping with a different key (e.g. user copied config.yaml
    from another instance without the matching general.secrets_encryption_key)
    must fail loudly so the operator can diagnose, not silently serve
    garbage."""
    encrypted = encrypt_secret("authentic", master_key=master_key)
    wrong_key = "completely-different-master-key-aaa"
    with pytest.raises(ValueError, match="tampered|master key"):
        decrypt_secret(encrypted, master_key=wrong_key)


def test_is_encrypted_recognizes_marker():
    assert is_encrypted(SECRET_MARKER_PREFIX + "anything")
    assert not is_encrypted("plaintext")
    assert not is_encrypted("")
    assert not is_encrypted(None)
    assert not is_encrypted(42)
