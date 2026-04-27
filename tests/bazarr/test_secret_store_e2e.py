# coding=utf-8
"""End-to-end tests for the at-rest encryption pipeline.

Builds on the unit tests in:
- test_secret_store_crypto.py (encrypt / decrypt primitives)
- test_secret_store_registry.py (USER_VISIBLE vs SYSTEM tier)
- test_secret_store_migration.py (per-field decrypt / encrypt helpers)

Covers the integration paths that the per-module tests can't:
- Full bootstrap simulation (plaintext config -> migrate -> persist
  ciphertext -> reboot -> decrypt -> plaintext in memory)
- Key-rotation failure mode (master key changes between reboots)
- Legacy Plex compatibility (apikey_encrypted=True under the old
  per-namespace key; new pipeline must rescue them)
- Bad-cipher tolerance (corrupt one credential, others keep working,
  bad one stays so the user can rotate it via Settings)

These are higher-cost tests that exercise crypto + registry + migration
together; the simpler unit suites already prove each piece in
isolation.
"""
from unittest.mock import MagicMock, patch

import pytest
from itsdangerous import URLSafeSerializer

from secret_store.crypto import (
    SECRET_MARKER_PREFIX,
    encrypt_secret,
    is_encrypted,
)
from secret_store.migration import (
    decrypt_settings_dict,
    decrypt_settings_in_place,
    encrypt_settings_dict,
    migrate_legacy_plex_encryption,
)


@pytest.fixture(autouse=True)
def stable_master_key(monkeypatch):
    key = "deterministic-test-master-key-do-not-rotate-during-test"
    monkeypatch.setattr(
        "secret_store.crypto.get_master_key",
        lambda settings_obj=None: key,
    )
    yield key


class _FakeSection(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name, value):
        self[name] = value


class _FakeSettings:
    """Stand-in for the dynaconf settings root: attribute-and-dict access
    on each section, with a `get` that returns the section if present."""

    _sections = (
        "general", "auth", "compat_endpoint", "sonarr", "radarr", "plex",
        "jellyfin", "translator", "proxy", "postgresql", "opensubtitles",
        "opensubtitlescom", "addic7ed", "legendasdivx", "legendasnet",
        "xsubs", "deathbycaptcha", "napisy24", "titlovi", "titulky",
        "karagarga", "assrt", "betaseries", "jimaku", "subdl", "subsource",
        "subx", "subsro", "omdb",
    )

    def __init__(self, data):
        for section in self._sections:
            setattr(self, section, _FakeSection(data.get(section, {})))


# --- E2E: full plaintext-to-ciphertext-and-back -----------------------------


def test_e2e_plaintext_first_boot_persists_ciphertext_then_decrypts_on_reboot():
    """Simulate the auto-migration story end to end:
    1. config.yaml on disk is plaintext (post-upgrade, pre-migration).
    2. decrypt_settings_in_place is a passthrough (no marker).
    3. encrypt_settings_dict produces ciphertext for write_config.
    4. Reboot: ciphertext on disk -> decrypt_settings_in_place restores
       plaintext in memory.
    """
    initial_disk = {
        "sonarr": {"apikey": "tt-sonarr"},
        "radarr": {"apikey": "tt-radarr"},
        "compat_endpoint": {"token": "tt-compat", "jwt_secret": "system"},
        "translator": {"gemini_keys": ["g1", "g2"]},
    }

    # Step 1: load disk (plaintext) into live settings.
    settings = _FakeSettings(initial_disk)

    # Step 2: passthrough decrypter (no markers present).
    decrypt_settings_in_place(settings)
    assert settings.sonarr.apikey == "tt-sonarr"
    assert settings.radarr.apikey == "tt-radarr"
    assert settings.compat_endpoint.token == "tt-compat"
    assert settings.translator.gemini_keys == ["g1", "g2"]

    # Step 3: write_config snapshots in-memory and encrypts before persist.
    snapshot = {s: dict(getattr(settings, s)) for s in _FakeSettings._sections}
    encrypted_disk = encrypt_settings_dict(snapshot)

    assert is_encrypted(encrypted_disk["sonarr"]["apikey"])
    assert is_encrypted(encrypted_disk["radarr"]["apikey"])
    assert is_encrypted(encrypted_disk["compat_endpoint"]["token"])
    assert all(is_encrypted(k) for k in encrypted_disk["translator"]["gemini_keys"])
    # System secret stays plaintext at the migration layer (the API
    # serializer is what masks it; the on-disk file keeps the raw value).
    assert encrypted_disk["compat_endpoint"]["jwt_secret"] == "system"

    # Step 4: reboot. Build a fresh live settings from the encrypted disk
    # form, decrypt in place, confirm plaintext restored.
    rebooted = _FakeSettings(encrypted_disk)
    decrypt_settings_in_place(rebooted)
    assert rebooted.sonarr.apikey == "tt-sonarr"
    assert rebooted.radarr.apikey == "tt-radarr"
    assert rebooted.compat_endpoint.token == "tt-compat"
    assert rebooted.translator.gemini_keys == ["g1", "g2"]


def test_e2e_first_save_persists_master_key_alongside_ciphertext():
    """Codex P1 regression: the master key MUST land in the snapshot
    that gets written to disk. Bug pre-fix: encrypt_settings_dict
    deep-copies the input, then encrypt_secret lazy-generates the
    master key on the LIVE settings object - the snapshot still has
    empty general.secrets_encryption_key, so disk gets enc:v1: values
    paired with no key, and next reboot can't decrypt anything (and
    silently uses ciphertext as the credential)."""
    # Input shape: post-validate, pre-first-save. master key is empty;
    # user-visible secrets are non-empty plaintext.
    initial = {
        "general": {"secrets_encryption_key": "", "flask_secret_key": "f"},
        "sonarr": {"apikey": "real-sonarr-key"},
    }
    encrypted = encrypt_settings_dict(initial)

    # The snapshot we'd persist must have the master key set.
    persisted_master = encrypted["general"]["secrets_encryption_key"]
    assert persisted_master  # non-empty
    assert persisted_master != ""

    # That same master must successfully decrypt the ciphertext we
    # produced (otherwise the disk file is unrecoverable).
    from secret_store.crypto import decrypt_secret
    decrypted = decrypt_secret(
        encrypted["sonarr"]["apikey"],
        master_key=persisted_master,
    )
    assert decrypted == "real-sonarr-key"


def test_has_plaintext_secrets_on_disk_detects_clear_text_credential():
    """Force-migration trigger: if any USER_VISIBLE_SECRETS path is
    non-empty plaintext (no marker), bootstrap must KNOW so it can call
    write_config unconditionally. Otherwise the lazy migration story
    leaves clear-text credentials sitting on disk indefinitely until
    the operator happens to touch a Settings field."""
    from secret_store.migration import has_plaintext_secrets_on_disk

    # Mixed shape: one clear-text password, one already-encrypted token.
    settings = _FakeSettings({
        "sonarr": {"apikey": "still-clear-text-from-an-edit"},
        "compat_endpoint": {"token": SECRET_MARKER_PREFIX + "ignored"},
    })
    assert has_plaintext_secrets_on_disk(settings) is True

    # All ciphertext / empty -> nothing to migrate.
    fully_encrypted = _FakeSettings({
        "sonarr": {"apikey": encrypt_secret("real-apikey")},
        "compat_endpoint": {"token": encrypt_secret("real-token")},
    })
    assert has_plaintext_secrets_on_disk(fully_encrypted) is False

    # Empty values are not flagged - empty stays empty, no migration.
    empty = _FakeSettings({"sonarr": {"apikey": ""}})
    assert has_plaintext_secrets_on_disk(empty) is False


def test_has_plaintext_secrets_on_disk_detects_clear_text_in_list():
    """Same trigger for USER_VISIBLE_SECRET_LISTS - a single plaintext
    list element flips the bit."""
    from secret_store.migration import has_plaintext_secrets_on_disk
    settings = _FakeSettings({
        "translator": {"gemini_keys": [encrypt_secret("g1"), "still-plain"]},
    })
    assert has_plaintext_secrets_on_disk(settings) is True


def test_e2e_save_after_first_boot_does_not_double_encrypt():
    """Once a credential is on disk in ciphertext form, a normal save
    cycle (no user edit) must not produce a fresh ciphertext - encrypt
    on top of the in-memory plaintext, get bytes that decrypt back to
    the same value. The non-determinism of encrypt_secret would otherwise
    make every save churn config.yaml even when nothing changed."""
    initial_disk = {"sonarr": {"apikey": "tt-sonarr"}}
    settings = _FakeSettings(initial_disk)
    decrypt_settings_in_place(settings)

    snap1 = {s: dict(getattr(settings, s)) for s in _FakeSettings._sections}
    encrypted1 = encrypt_settings_dict(snap1)

    # Pretend write_config ran. Reload, decrypt in place, encrypt again.
    settings2 = _FakeSettings(encrypted1)
    decrypt_settings_in_place(settings2)
    snap2 = {s: dict(getattr(settings2, s)) for s in _FakeSettings._sections}
    encrypted2 = encrypt_settings_dict(snap2)

    # Bytes differ (salt + timestamp), but plaintext compare is identity.
    assert encrypted1 != encrypted2
    assert decrypt_settings_dict(encrypted1) == decrypt_settings_dict(encrypted2)


# --- Key rotation -----------------------------------------------------------


def test_e2e_master_key_change_is_detected_and_isolates_failure():
    """If config.yaml is restored from another instance (different master
    key) the bad cipher must NOT crash bazarr - it leaves the bad value
    in place, the rest of the tier decrypts normally, and the user can
    re-paste the affected credential."""
    encrypted_apikey = encrypt_secret("real-sonarr-key")
    encrypted_radarr = encrypt_secret("real-radarr-key")

    settings = _FakeSettings({
        "sonarr": {"apikey": encrypted_apikey},
        "radarr": {"apikey": encrypted_radarr},
    })

    # Simulate boot under a different master key by patching get_master_key
    # to return something else.
    with patch(
        "secret_store.crypto.get_master_key",
        lambda settings_obj=None: "completely-different-master-key-aaa",
    ):
        decrypt_settings_in_place(settings)

    # Both fail to decrypt; both stay as the original (encrypted) bytes
    # so the operator can see "something's wrong" and rotate.
    assert settings.sonarr.apikey == encrypted_apikey
    assert settings.radarr.apikey == encrypted_radarr


def test_e2e_partial_corruption_isolated_to_one_field():
    """Corrupting one ciphertext must not block the others from decrypting."""
    good = encrypt_secret("good-sonarr")
    bad = SECRET_MARKER_PREFIX + "tampered-payload-here"

    settings = _FakeSettings({
        "sonarr": {"apikey": bad},
        "radarr": {"apikey": good},
    })
    decrypt_settings_in_place(settings)

    assert settings.sonarr.apikey == bad  # left alone, operator can rotate
    assert settings.radarr.apikey == "good-sonarr"  # neighbor still works


# --- API-serializer masking integrated with at-rest plaintext memory --------


def test_e2e_api_serializer_masks_only_system_secrets():
    """Reproduces the get_settings() behavior: USER_VISIBLE pass plaintext,
    SYSTEM are masked with ***, empty system stays empty (so UI can tell
    'configured-but-hidden' from 'not configured')."""
    from secret_store import is_system_secret

    settings_dict = {
        "auth": {"apikey": "user-can-see-this"},
        "sonarr": {"apikey": "user-can-see-this-too"},
        "compat_endpoint": {
            "token": "user-pastes-this-into-vlsub",  # USER_VISIBLE
            "jwt_secret": "internal-signing-key",     # SYSTEM
            "file_id_secret": "",                      # SYSTEM, empty
        },
        "general": {
            "flask_secret_key": "session-signing",
            "secrets_encryption_key": "master-encryption",
        },
        "plex": {"encryption_key": "legacy-plex-key"},
    }

    # Mimic get_settings() masking behaviour.
    serialized = {}
    for section, fields in settings_dict.items():
        serialized[section] = {}
        for k, v in fields.items():
            full = f"{section}.{k.lower()}"
            if is_system_secret(full):
                serialized[section][k] = "***" if v else v
            else:
                serialized[section][k] = v

    # USER_VISIBLE: plaintext flows through.
    assert serialized["auth"]["apikey"] == "user-can-see-this"
    assert serialized["sonarr"]["apikey"] == "user-can-see-this-too"
    assert serialized["compat_endpoint"]["token"] == "user-pastes-this-into-vlsub"

    # SYSTEM: masked when set.
    assert serialized["compat_endpoint"]["jwt_secret"] == "***"
    assert serialized["general"]["flask_secret_key"] == "***"
    assert serialized["general"]["secrets_encryption_key"] == "***"
    assert serialized["plex"]["encryption_key"] == "***"

    # SYSTEM empty stays empty (no false positive of "configured").
    assert serialized["compat_endpoint"]["file_id_secret"] == ""


# --- Legacy Plex compatibility ---------------------------------------------


def _legacy_plex_encrypt(plaintext: str, legacy_key: str) -> str:
    """Reproduces the legacy api/plex/security.py:TokenManager.encrypt
    output shape: URLSafeSerializer.dumps of a dict, NO marker prefix."""
    return URLSafeSerializer(legacy_key).dumps({
        "token": plaintext,
        "salt": "deterministic-salt-for-tests",
        "timestamp": 1700000000,
    })


def test_legacy_plex_migration_recovers_plaintext():
    """Pre-secret_store, plex.apikey was stored as URLSafeSerializer dump
    under plex.encryption_key, with apikey_encrypted=True. The unified
    pipeline can't recognise this format (no enc:v1: marker), so the
    legacy migration runs first to convert the values to plaintext in
    memory and clear the flag. After that the standard pipeline takes
    over and write_config re-encrypts under the master key."""
    legacy_key = "legacy-plex-encryption-key-pre-unification"
    plaintext_apikey = "real-plex-apikey-12345"
    plaintext_token = "real-plex-oauth-token-67890"
    legacy_apikey = _legacy_plex_encrypt(plaintext_apikey, legacy_key)
    legacy_token = _legacy_plex_encrypt(plaintext_token, legacy_key)

    settings = _FakeSettings({
        "plex": {
            "apikey": legacy_apikey,
            "token": legacy_token,
            "encryption_key": legacy_key,
            "apikey_encrypted": True,
        },
    })

    migrate_legacy_plex_encryption(settings)

    # Plaintext recovered into in-memory settings.
    assert settings.plex.apikey == plaintext_apikey
    assert settings.plex.token == plaintext_token
    # Flag cleared so the migration doesn't re-trigger on next boot.
    assert settings.plex.apikey_encrypted is False
    # Legacy encryption_key intentionally left in place (still SYSTEM,
    # masked by the API serializer) for downgrade safety.
    assert settings.plex.encryption_key == legacy_key


def test_legacy_plex_migration_skips_when_flag_unset():
    """Fresh installs have apikey_encrypted=False (or absent). The
    migration must not try to legacy-decrypt plaintext - that would
    URLSafeSerializer.loads() a non-payload and raise, replacing real
    creds with garbage."""
    settings = _FakeSettings({
        "plex": {"apikey": "fresh-install-plaintext", "apikey_encrypted": False},
    })
    migrate_legacy_plex_encryption(settings)
    assert settings.plex.apikey == "fresh-install-plaintext"


def test_legacy_plex_migration_handles_missing_encryption_key():
    """An install with apikey_encrypted=True but no encryption_key is in
    an inconsistent state. The migration must clear the flag (so it
    doesn't retry forever) and surface a warning, leaving the bytes
    alone so the operator can fix from Settings."""
    settings = _FakeSettings({
        "plex": {
            "apikey": "<unreadable-without-key>",
            "apikey_encrypted": True,
            "encryption_key": "",
        },
    })
    migrate_legacy_plex_encryption(settings)
    # Flag cleared, value left for operator to manually rotate.
    assert settings.plex.apikey_encrypted is False
    assert settings.plex.apikey == "<unreadable-without-key>"


def test_legacy_plex_migration_already_unified_is_passthrough():
    """If a value already carries the enc:v1: marker (e.g. a previous
    migration ran and apikey_encrypted got reset to True somehow), the
    legacy migration must NOT try to legacy-decrypt unified ciphertext
    - that would corrupt it."""
    unified_cipher = encrypt_secret("real-plex-key")
    settings = _FakeSettings({
        "plex": {
            "apikey": unified_cipher,
            "apikey_encrypted": True,  # somehow stuck
            "encryption_key": "irrelevant",
        },
    })
    migrate_legacy_plex_encryption(settings)
    assert settings.plex.apikey == unified_cipher  # left unchanged
    assert settings.plex.apikey_encrypted is False  # flag cleared
