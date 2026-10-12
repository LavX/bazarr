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
import logging
import sys
from unittest.mock import MagicMock, patch  # noqa: F401

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
        "deathbycaptcha", "napisy24", "titlovi", "titulky",
        "karagarga", "assrt", "betaseries", "jimaku", "subdl", "subsource",
        "subx", "subsro", "omdb",
    )

    def __init__(self, data):
        for section in self._sections:
            setattr(self, section, _FakeSection(data.get(section, {})))


class _DynamicSettings:
    """Same stand-in, but with the sections the test names: plugin sections
    are not part of the fixed list above."""

    def __init__(self, data):
        for name, values in data.items():
            setattr(self, name, _FakeSection(values))


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
    """Regression: the master key MUST land in the snapshot
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
    key) the bad cipher must NOT crash bazarr - the affected values are
    withheld from every consumer (never sent anywhere as the credential),
    the rest of the tier decrypts normally, and the user is told which
    credential to re-enter."""
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

    # Both fail to decrypt; both are withheld so no consumer can send the
    # ciphertext anywhere, and the failure names the setting for re-entry.
    from secret_store.migration import decrypt_failure_issues

    assert settings.sonarr.apikey == ""
    assert settings.radarr.apikey == ""
    assert {issue["object"] for issue in decrypt_failure_issues()} == {
        "sonarr.apikey", "radarr.apikey"}


def test_e2e_partial_corruption_isolated_to_one_field():
    """Corrupting one ciphertext must not block the others from decrypting."""
    good = encrypt_secret("good-sonarr")
    bad = SECRET_MARKER_PREFIX + "tampered-payload-here"

    settings = _FakeSettings({
        "sonarr": {"apikey": bad},
        "radarr": {"apikey": good},
    })
    decrypt_settings_in_place(settings)

    assert settings.sonarr.apikey == ""  # withheld, never sent as the credential
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


def test_legacy_plex_migration_does_not_import_the_api_package(monkeypatch, caplog):
    """app.config runs this migration while it is still importing. Loading
    the api package from here pulls app.config back in half-initialised,
    so the migration must decode the legacy payload without it. Blocking
    the api package reproduces the startup import failure."""
    legacy_key = "legacy-plex-encryption-key-pre-unification"
    settings = _FakeSettings({
        "plex": {
            "apikey": _legacy_plex_encrypt("real-plex-apikey-12345", legacy_key),
            "token": _legacy_plex_encrypt("real-plex-oauth-token-67890", legacy_key),
            "encryption_key": legacy_key,
            "apikey_encrypted": True,
        },
    })
    for name in ("api", "api.plex", "api.plex.security"):
        monkeypatch.setitem(sys.modules, name, None)

    with caplog.at_level(logging.ERROR, logger="secret_store.migration"):
        migrate_legacy_plex_encryption(settings)

    assert settings.plex.apikey == "real-plex-apikey-12345"
    assert settings.plex.token == "real-plex-oauth-token-67890"
    assert settings.plex.apikey_encrypted is False
    assert not caplog.records


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


def test_legacy_plex_migration_recovers_oauth_token_without_apikey_flag():
    """The Plex OAuth flow stored `settings.plex.token = encrypt_token(...)`
    but never set `apikey_encrypted` - that flag was scoped to the
    apikey path only. Review flagged that gating migration on the flag
    leaves OAuth users with a legacy ciphertext that the unified
    pipeline then re-encrypts as if it were plaintext, breaking login
    after upgrade.

    Detection has to be value-shaped (try legacy decrypt; succeed -> use
    plaintext) so the OAuth-no-flag case is covered alongside the
    apikey-with-flag case."""
    legacy_key = "legacy-plex-encryption-key-pre-unification"
    plaintext_token = "real-plex-oauth-token-from-myplex-account"
    legacy_token = _legacy_plex_encrypt(plaintext_token, legacy_key)

    settings = _FakeSettings({
        "plex": {
            # apikey path NOT used by this OAuth install
            "apikey": "",
            # OAuth token IS encrypted under the legacy scheme
            "token": legacy_token,
            "encryption_key": legacy_key,
            # Critically: NO apikey_encrypted=True flag, because OAuth
            # never wrote it.
            "apikey_encrypted": False,
        },
    })

    migrate_legacy_plex_encryption(settings)

    assert settings.plex.token == plaintext_token
    assert settings.plex.encryption_key == legacy_key


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


def test_write_only_token_first_save_and_reload_preserve_plaintext_only_in_memory(stable_master_key):
    from dynaconf import Dynaconf
    from secret_store import has_plaintext_secrets_on_disk
    initial = {"general": {"secrets_encryption_key": stable_master_key},
               "discover": {"tmdb_access_token": "synthetic-write-only-token", "locale": "en-US"}}
    live = Dynaconf(environments=False)
    live.update(initial)
    assert has_plaintext_secrets_on_disk(live)
    encrypted = encrypt_settings_dict(initial)
    assert is_encrypted(encrypted["discover"]["tmdb_access_token"])
    rebooted = Dynaconf(environments=False)
    rebooted.update(encrypted)
    assert not has_plaintext_secrets_on_disk(rebooted)
    decrypt_settings_in_place(rebooted)
    assert rebooted.discover.tmdb_access_token == "synthetic-write-only-token"
    assert encrypt_settings_dict(encrypted) == encrypted


# --- Plugin secrets through the real settings API ----------------------------


@pytest.fixture
def plugin_secret_registry():
    """Isolate the dynamic plugin registry around tests that register paths."""
    from secret_store import registry

    saved = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    yield registry
    registry.reset_plugin_secret_paths(saved)


@pytest.fixture(autouse=True)
def _clean_decrypt_failures():
    """Keep the per-path decrypt failure set clean around every test."""
    from secret_store.migration import reset_decrypt_failures

    reset_decrypt_failures()
    yield
    reset_decrypt_failures()


@pytest.fixture
def real_master_key(monkeypatch):
    """The settings-API tests below write through the real write_config, so
    they must encrypt with the live master key rather than the file-wide test
    key: a config written under a different key would poison every later test
    that reloads settings in the same process."""
    from app.config import settings

    monkeypatch.setattr(
        "secret_store.crypto.get_master_key",
        lambda settings_obj=None: settings.general.secrets_encryption_key,
    )


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


def _fake_database_module():
    """The save's follow-up marks the install configured in the database;
    in the test environment the database is not initialized, so the
    established fake from the config-save tests replaces the module."""
    from types import SimpleNamespace

    return SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None),
        update=lambda _model: _FakeUpdate(),
        System=object,
    )


def _remove_settings_section(provider_id):
    """Drop a test's section from the live settings and from the config on
    disk, so no later test in the same process sees it."""
    from app.config import remove_settings_section, write_config

    remove_settings_section(provider_id)
    write_config()


def _boot_with_failed_secret(provider_id):
    """Put a credential that will not decrypt into the live config and boot
    it the way a real process would: the recoverable ciphertext reaches the
    file, the reload and in-place decrypt withhold the value, and the path
    joins the failure set. Returns the ciphertext."""
    from app.config import restore_persisted_settings, settings, write_config
    from secret_store.crypto import SECRET_MARKER_PREFIX
    from secret_store.migration import decrypt_failure_paths

    ciphertext = SECRET_MARKER_PREFIX + "tampered-by-a-restore"
    settings.set(provider_id, {"api_token": ciphertext, "region": "eu"})
    write_config()
    restore_persisted_settings()
    assert settings[provider_id]["api_token"] == ""
    assert f"{provider_id}.api_token" in decrypt_failure_paths()
    return ciphertext


def _prepare_save(monkeypatch):
    """The two gates a settings save runs before its follow-ups."""
    from app import config
    import sys

    monkeypatch.setitem(sys.modules, "app.database", _fake_database_module())
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    return config


def test_reentering_a_failed_secret_clears_the_descriptor_and_the_health_issue(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A save that carries a new value ends the decrypt failure: the API
    stops reporting decrypt_failed, the health issue goes away, and the
    credential decrypts after a restart instead of resurrecting the old
    ciphertext."""
    from secret_store import migration

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        _boot_with_failed_secret(provider_id)
        descriptor = config.get_settings()[provider_id]["__secret_fields"]["api_token"]
        assert descriptor["decrypt_failed"] is True

        config.save_settings(
            [(f"settings-{provider_id}-api_token", ["unit-reentered-token-0123456789"])])
        assert config.settings[provider_id]["api_token"] == "unit-reentered-token-0123456789"
        assert migration.decrypt_failure_paths() == frozenset()
        assert migration.decrypt_failure_issues() == []
        descriptor = config.get_settings()[provider_id]["__secret_fields"]["api_token"]
        assert descriptor["configured"] is True
        assert descriptor["last_four"] == "6789"
        assert "decrypt_failed" not in descriptor

        # A restart boots the re-entered credential, not the old ciphertext.
        config.restore_persisted_settings()
        assert config.settings[provider_id]["api_token"] == "unit-reentered-token-0123456789"
        assert migration.decrypt_failure_paths() == frozenset()
    finally:
        _remove_settings_section(provider_id)


def test_clearing_a_failed_secret_clears_it_on_disk_and_across_a_restart(
        monkeypatch, plugin_secret_registry, real_master_key):
    """Clear on a failed credential is a real clear: the save carries the
    explicit empty value, the recoverable ciphertext is not preserved over
    it, and nothing comes back after a restart."""
    from secret_store import migration

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        ciphertext = _boot_with_failed_secret(provider_id)
        config.save_settings([(f"settings-{provider_id}-api_token", [""])])
        assert config.settings[provider_id]["api_token"] == ""
        assert migration.decrypt_failure_paths() == frozenset()
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert ciphertext not in on_disk
        # No descriptor: the secret is gone, not hidden.
        assert "__secret_fields" not in config.get_settings()[provider_id]

        # A restart does not resurrect the credential.
        config.restore_persisted_settings()
        assert config.settings[provider_id]["api_token"] == ""
        assert migration.decrypt_failure_paths() == frozenset()
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            assert ciphertext not in handle.read()
    finally:
        _remove_settings_section(provider_id)


def test_reentering_then_clearing_in_one_process_does_not_resurrect_the_key(
        monkeypatch, plugin_secret_registry, real_master_key):
    """The re-enter-then-clear sequence in one process ends with the
    credential gone: the failure was ended by the first save, so the
    second save's clear is not overwritten with the old ciphertext."""
    from secret_store import migration

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        ciphertext = _boot_with_failed_secret(provider_id)
        config.save_settings(
            [(f"settings-{provider_id}-api_token", ["unit-reentered-token-0123456789"])])
        config.save_settings([(f"settings-{provider_id}-api_token", [""])])
        config.restore_persisted_settings()
        assert config.settings[provider_id]["api_token"] == ""
        assert migration.decrypt_failure_paths() == frozenset()
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert ciphertext not in on_disk
        assert "unit-reentered-token-0123456789" not in on_disk
    finally:
        _remove_settings_section(provider_id)


def test_an_unrelated_save_keeps_a_failed_credentials_ciphertext(
        monkeypatch, plugin_secret_registry, real_master_key):
    """The preserve pass runs through write_config, not just when a test
    calls it directly: a save that does not touch the failed credential
    keeps its recoverable ciphertext on disk, and a restart still withholds
    the value and names the setting."""
    from secret_store import migration

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        ciphertext = _boot_with_failed_secret(provider_id)
        config.save_settings([(f"settings-{provider_id}-region", ["us"])])
        assert config.settings[provider_id]["region"] == "us"
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            assert ciphertext in handle.read()

        # A restart re-withholds the value and reports the failure again.
        config.restore_persisted_settings()
        assert config.settings[provider_id]["api_token"] == ""
        assert f"{provider_id}.api_token" in migration.decrypt_failure_paths()
    finally:
        _remove_settings_section(provider_id)


def test_numeric_and_boolean_looking_tokens_stay_text_and_mask(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A credential that looks like a number or a boolean is stored as the
    text that was typed: the numeric and boolean casts do not turn it into a
    value that neither encrypts at rest nor masks in the API."""
    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        token = "001234567890123456"
        config.save_settings([(f"settings-{provider_id}-api_token", [token])])
        assert config.settings[provider_id]["api_token"] == token
        assert isinstance(config.settings[provider_id]["api_token"], str)
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert token not in on_disk
        assert "enc:v1:" in on_disk
        descriptor = config.get_settings()[provider_id]["__secret_fields"]["api_token"]
        assert descriptor == {"configured": True, "last_four": "3456"}

        # A boolean-looking token is text too, never a stored True.
        config.save_settings([(f"settings-{provider_id}-api_token", ["true"])])
        assert config.settings[provider_id]["api_token"] == "true"
        assert isinstance(config.settings[provider_id]["api_token"], str)
        # It is encrypted at rest and survives a restart as the text.
        config.restore_persisted_settings()
        assert config.settings[provider_id]["api_token"] == "true"
        # Four characters are too large a fraction of a secret to show: no
        # last four in the descriptor, only that one is configured.
        descriptor = config.get_settings()[provider_id]["__secret_fields"]["api_token"]
        assert descriptor == {"configured": True}

        # An unsupported type is refused rather than stored in the clear.
        from app.config import ValidationError

        with pytest.raises(ValidationError):
            config.save_settings([(f"settings-{provider_id}-api_token", [123456])])
    finally:
        _remove_settings_section(provider_id)


def test_save_settings_drops_the_secret_fields_descriptor_key(
        monkeypatch, plugin_secret_registry, real_master_key):
    """The descriptor key the settings API adds is an answer, never a
    setting: echoing it back does not store it, so _get_settings cannot
    hand back whatever was written under that name."""
    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        config.save_settings([
            (f"settings-{provider_id}-__secret_fields",
             [{"api_token": {"configured": True, "last_four": "nope"}}]),
            (f"settings-{provider_id}-region", ["eu"]),
        ])
        assert config.settings[provider_id]["region"] == "eu"
        assert "__secret_fields" not in config.settings[provider_id]
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            assert "__secret_fields" not in handle.read()
        # With no secret configured, the API section carries no descriptor.
        assert "__secret_fields" not in config.get_settings()[provider_id]
    finally:
        _remove_settings_section(provider_id)


def test_settings_api_hides_plugin_secret_and_returns_descriptor(plugin_secret_registry, real_master_key):
    """The settings API never returns a plugin secret in plaintext. It returns
    an empty value plus a per-section descriptor that says the secret is
    configured and, for a long one, how it ends."""
    import json

    from app.config import get_settings, settings

    plugin_secret_registry.reset_plugin_secret_paths({"e2ehub.api_token"})
    token = "unit-e2e-token-0123456789abcdef"  # pragma: allowlist secret
    settings.set("e2ehub", {"api_token": token, "region": "eu"})
    try:
        result = get_settings()
        assert result["e2ehub"]["api_token"] == ""
        assert result["e2ehub"]["region"] == "eu"
        descriptor = result["e2ehub"]["__secret_fields"]["api_token"]
        assert descriptor["configured"] is True
        assert descriptor["last_four"] == "cdef"
        # The plaintext never reaches the response, in the value or the
        # descriptor.
        assert token not in json.dumps(result)
    finally:
        _remove_settings_section("e2ehub")


def test_settings_api_reads_empty_collections_as_not_configured(plugin_secret_registry, real_master_key):
    """An empty list or dict an earlier write left at a registered secret
    path reads exactly like an empty string: the payload carries an empty
    value, never the raw collection, and no descriptor entry claims a
    credential is configured, so the wizard cannot treat a required field
    as present."""
    from app.config import get_settings, settings

    plugin_secret_registry.reset_plugin_secret_paths({"e2ehub.api_token", "e2ehub.endpoints"})
    settings.set("e2ehub", {"api_token": [], "endpoints": {}, "region": "eu"})
    try:
        result = get_settings()
        assert result["e2ehub"]["api_token"] == ""
        assert result["e2ehub"]["endpoints"] == ""
        assert result["e2ehub"]["region"] == "eu"
        # Neither empty collection is a configured credential, so the
        # section carries no descriptor entry for either field.
        assert "__secret_fields" not in result["e2ehub"]
    finally:
        _remove_settings_section("e2ehub")


def test_settings_api_descriptor_flags_decrypt_failure(plugin_secret_registry, real_master_key):
    """A plugin secret whose decryption failed reports configured plus
    decrypt_failed, so the form asks for the key again instead of showing
    the credential as absent. The live value is the withheld empty string,
    exactly what the boot pass leaves: never a live credential paired
    with the failure flag."""
    from app.config import get_settings, settings
    from secret_store import migration

    plugin_secret_registry.reset_plugin_secret_paths({"e2ehub.api_token"})
    # The post-boot state: the value is withheld as empty and its path is
    # in the failure set, while the ciphertext stays recoverable on disk.
    settings.set("e2ehub", {"api_token": "", "region": "eu"})
    migration._FAILED_DECRYPT_PATHS.add("e2ehub.api_token")
    try:
        result = get_settings()
        assert result["e2ehub"]["api_token"] == ""
        descriptor = result["e2ehub"]["__secret_fields"]["api_token"]
        assert descriptor == {"configured": True, "decrypt_failed": True}
        assert "last_four" not in descriptor
        # The health issue names the setting, never the value.
        issues = migration.decrypt_failure_issues()
        assert {issue["object"] for issue in issues} == {"e2ehub.api_token"}
    finally:
        _remove_settings_section("e2ehub")


def test_settings_api_masks_a_short_secret_without_last_four(plugin_secret_registry, real_master_key):
    """A secret shorter than 16 characters is configured but its last four
    characters are not exposed (four characters of a short secret would be a
    large fraction of it)."""
    from app.config import get_settings, settings

    plugin_secret_registry.reset_plugin_secret_paths({"e2ehub.api_token"})
    settings.set("e2ehub", {"api_token": "unit-short"})  # pragma: allowlist secret
    try:
        result = get_settings()
        descriptor = result["e2ehub"]["__secret_fields"]["api_token"]
        assert descriptor == {"configured": True}
    finally:
        _remove_settings_section("e2ehub")


def test_settings_save_semantics_for_plugin_secrets(
        monkeypatch, plugin_secret_registry, real_master_key):
    """Absent keeps the stored value, an explicit clear clears it, and a new
    value replaces it and lands on disk encrypted at rest."""
    from app import config

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})

    import sys

    monkeypatch.setitem(sys.modules, "app.database", _fake_database_module())
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)

    try:
        # Replace: a new value lands in the live settings, encrypted at rest.
        config.save_settings([(f"settings-{provider_id}-api_token", ["unit-first-value"])])
        assert config.settings[provider_id]["api_token"] == "unit-first-value"
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert "unit-first-value" not in on_disk
        assert "enc:v1:" in on_disk

        # Absent: a save that does not carry the field keeps the value.
        config.save_settings([(f"settings-{provider_id}-region", ["eu"])])
        assert config.settings[provider_id]["api_token"] == "unit-first-value"
        assert config.settings[provider_id]["region"] == "eu"

        # Clear: an explicit empty value clears the stored secret.
        config.save_settings([(f"settings-{provider_id}-api_token", [""])])
        assert config.settings[provider_id]["api_token"] == ""
    finally:
        _remove_settings_section(provider_id)


def test_settings_save_keeps_a_boolean_toggle_a_boolean(
        monkeypatch, tmp_path, plugin_secret_registry, real_master_key):
    """A boolean field a version once marked secret stays a toggle after a
    later version drops the flag: the path is still a registered credential,
    so the submitted text would otherwise sit stored as the truthy string
    "false", which the worker reads as on. The save converts it to the
    boolean the active manifest's declared type says it is."""
    import json as json_module

    config = _prepare_save(monkeypatch)

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    # The path persisted when version 1 marked the field secret; version 2
    # is active now and declares the same field boolean without the flag.
    state_file.write_text(json_module.dumps({
        "plugin_secret_paths": ["flaghub.use_solver"],
        "installations": {
            "flaghub": {
                "provider_id": "flaghub",
                "state": "active",
                "active_version": "2.0.0",
                "manifest": {
                    "provider_id": "flaghub",
                    "version": "2.0.0",
                    "config_schema": {"properties": {
                        "use_solver": {"type": "boolean"}}},
                },
            },
        },
    }), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    plugin_secret_registry.reset_plugin_secret_paths({"flaghub.use_solver"})

    try:
        config.save_settings([("settings-flaghub-use_solver", ["false"])])
        assert config.settings.flaghub.use_solver is False
        config.save_settings([("settings-flaghub-use_solver", ["true"])])
        assert config.settings.flaghub.use_solver is True
    finally:
        _remove_settings_section("flaghub")


def test_settings_save_refused_when_a_secret_cannot_be_encrypted(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A credential that cannot be encrypted is not stored: the save is
    refused and the live settings return to what is on disk."""
    from app import config

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})

    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)

    def broken_encrypt(value, master_key=None):
        raise RuntimeError("key unavailable")

    # The config write path and the Hub service resolve the encrypter from
    # different bindings; break both so the write cannot encrypt anything.
    monkeypatch.setattr("secret_store.encrypt_secret", broken_encrypt)
    monkeypatch.setattr("secret_store.migration.encrypt_secret", broken_encrypt)
    import sys

    monkeypatch.setitem(sys.modules, "app.database", _fake_database_module())

    from dynaconf.validator import ValidationError as _SaveRefused

    try:
        with pytest.raises(_SaveRefused) as refused:
            config.save_settings([(f"settings-{provider_id}-api_token", ["unit-refused-value"])])
        # The refusal carries the actual cause (the swallowed encryption
        # failure), not just the fact that no save happened.
        assert isinstance(refused.value.__cause__, RuntimeError)
        assert "key unavailable" in str(refused.value.__cause__)
        # The refused save stored nothing, on disk or in the live settings.
        assert config.settings.get(provider_id, {}).get("api_token", "") == ""
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            assert "unit-refused-value" not in handle.read()
    finally:
        _remove_settings_section(provider_id)


def test_first_boot_encrypts_leftover_plugin_secret_and_api_hides_it(
        plugin_secret_registry, real_master_key):
    """A plaintext token left in the section of a plugin that has no
    installation row is registered at boot, encrypted by the forced rewrite,
    restored to the live settings on the next boot, and absent from the
    settings API."""
    import json as json_module

    from app.config import get_settings, settings

    plugin_secret_registry.reset_plugin_secret_paths({"ghostplugin.api_token"})
    token = "unit-ghost-token-0123456789abcd"  # pragma: allowlist secret

    # The plugin is gone: only its settings section is left behind.
    settings.set("ghostplugin", {"api_token": token, "region": "eu"})
    try:
        assert plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")

        # write_config snapshots the live plaintext and encrypts it at rest.
        snapshot = {"general": {"secrets_encryption_key": "unit-master"},
                    "ghostplugin": dict(settings.ghostplugin)}
        on_disk = encrypt_settings_dict(snapshot)
        assert is_encrypted(on_disk["ghostplugin"]["api_token"])

        # Reboot: ciphertext on disk, decrypt back to plaintext in memory.
        rebooted = _DynamicSettings({"ghostplugin": dict(on_disk["ghostplugin"])})
        decrypt_settings_in_place(rebooted)
        assert rebooted.ghostplugin.api_token == token

        # The settings API shows the descriptor, never the value.
        result = get_settings()
        assert result["ghostplugin"]["api_token"] == ""
        assert result["ghostplugin"]["__secret_fields"]["api_token"]["configured"] is True
        assert token not in json_module.dumps(result)
    finally:
        _remove_settings_section("ghostplugin")


def test_a_fresh_process_boot_loads_the_registry_before_the_secret_pass(tmp_path):
    """The import-time boot order, in a real subprocess against a fresh
    config directory: state.json is read, the plugin paths register, and
    only then do the plaintext detection and the in-place decrypt run, so
    an encrypted plugin secret is decrypted (not left live in the settings)
    and a plaintext one is detected for the forced first-save rewrite.
    Nothing in-process proves this ordering; every other test runs after
    app.config has finished importing."""
    import json as json_module
    import os
    import subprocess
    import sys as system_path

    from secret_store.crypto import encrypt_secret

    master_key = "unit-boot-master-key-for-the-subprocess"
    config_dir = tmp_path / "boot-config"
    for subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
        (config_dir / subdirectory).mkdir(parents=True, exist_ok=True)
    (config_dir / "provider_hub").mkdir()
    stored = encrypt_secret("unit-boot-stored-token", master_key=master_key)
    (config_dir / "config" / "config.yaml").write_text(
        f"general:\n"
        f"  secrets_encryption_key: '{master_key}'\n"
        f"bootvault:\n"
        f"  api_token: '{stored}'\n"
        f"bootplugin:\n"
        f"  api_token: unit-boot-plaintext-token\n",
        encoding="utf-8",
    )
    state_file = config_dir / "provider_hub" / "state.json"
    state_file.write_text(json_module.dumps({
        "installations": {
            "bootvault": {
                "provider_id": "bootvault",
                "state": "active",
                "manifest": {
                    "provider_id": "bootvault",
                    "config_schema": {"properties": {
                        "api_token": {"type": "string", "secret": True}}},
                },
            },
        },
    }), encoding="utf-8")

    worktree_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    child_code = (
        "import json\n"
        "import app.config  # noqa: F401 - the boot this test is about\n"
        "from app.config import settings\n"
        "from provider_hub.state import state_file\n"
        "from secret_store import registry\n"
        "from secret_store.migration import (decrypt_failure_paths,\n"
        "                                     has_plaintext_secrets_on_disk)\n"
        "state = json.loads(state_file().read_text(encoding='utf-8'))\n"
        "print(json.dumps({\n"
        "    'registered': sorted(registry.plugin_secret_paths()),\n"
        "    'vault': settings.bootvault.api_token,\n"
        "    'plugin': settings.bootplugin.api_token,\n"
        "    'failures': sorted(decrypt_failure_paths()),\n"
        "    'plaintext_detected': has_plaintext_secrets_on_disk(settings),\n"
        "    'persisted': state.get('plugin_secret_paths'),\n"
        "}))\n"
    )
    environment = dict(os.environ)
    environment.update({
        "BAZARR_CONFIG_DIR": str(config_dir),
        # NO_CLI makes get_args parse the config directory without the
        # command line, and --no-update is passed by that branch itself.
        "NO_CLI": "true",
        "PYTHONPATH": (os.path.join(worktree_root, "bazarr")
                       + os.pathsep + os.path.join(worktree_root, "custom_libs")),
    })
    completed = subprocess.run(
        [system_path.executable, "-c", child_code],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    result = json_module.loads(completed.stdout.strip().splitlines()[-1])

    # The registry registered both paths, from the installation row and
    # from the leftover scan, before the secret pass ran.
    assert "bootvault.api_token" in result["registered"]
    assert "bootplugin.api_token" in result["registered"]
    assert set(result["registered"]) <= set(result["persisted"] or [])
    # The encrypted plugin secret was decrypted, not left live as the
    # worker's credential.
    assert result["vault"] == "unit-boot-stored-token"
    assert result["failures"] == []
    # The plaintext leftover is detected for the forced first-save rewrite.
    assert result["plaintext_detected"] is True
    # The live settings keep the plaintext until that rewrite; the
    # encrypted one is plaintext in memory only.
    assert result["plugin"] == "unit-boot-plaintext-token"

    # The rewrite actually reached the file. Read config.yaml back with a
    # fresh disk reader, untouched by anything the child kept in memory:
    # the leftover plugin token is ciphertext on disk, the plaintext and
    # the marker-less form are gone, and the already-encrypted one stayed
    # encrypted. Without the forced rewrite the child's boot write would
    # see memory equal to disk and skip the file entirely.
    from dynaconf import Dynaconf

    on_disk = {k.lower(): v for k, v in Dynaconf(
        settings_file=str(config_dir / "config" / "config.yaml"),
        core_loaders=["YAML"]).as_dict().items()}
    disk_plugin = on_disk["bootplugin"]["api_token"]
    disk_vault = on_disk["bootvault"]["api_token"]
    assert isinstance(disk_plugin, str) and is_encrypted(disk_plugin)
    assert isinstance(disk_vault, str) and is_encrypted(disk_vault)
    assert "unit-boot-plaintext-token" not in json_module.dumps(on_disk)


def test_a_numeric_plugin_token_is_rewritten_encrypted_on_disk(tmp_path):
    """An installed plugin whose token an older save coerced into a number.
    The first boot writes the file out fully; the second starts from that
    file with only the coerced token put back as plaintext, so memory and
    disk agree everywhere else and nothing but the forced rewrite stands
    between the number and staying in the clear. The detection treats a
    non-string scalar at a registered path as plaintext, so the rewrite
    runs and the token lands on disk as ciphertext, stored as the text it
    was typed as."""
    import json as json_module
    import os
    import subprocess
    import sys as system_path

    import yaml

    config_dir = tmp_path / "boot-config"
    for subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
        (config_dir / subdirectory).mkdir(parents=True, exist_ok=True)
    (config_dir / "provider_hub").mkdir()
    config_file = config_dir / "config" / "config.yaml"
    config_file.write_text(
        "general:\n"
        "  secrets_encryption_key: 'unit-numeric-boot-master-key'\n"
        "bootnum:\n"
        "  api_token: 12345678901234567\n",
        encoding="utf-8",
    )
    (config_dir / "provider_hub" / "state.json").write_text(json_module.dumps({
        "installations": {
            "bootnum": {
                "provider_id": "bootnum",
                "state": "active",
                "manifest": {
                    "provider_id": "bootnum",
                    "config_schema": {"properties": {
                        "api_token": {"type": "string", "secret": True}}},
                },
            },
        },
    }), encoding="utf-8")

    worktree_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    environment = dict(os.environ)
    environment.update({
        "BAZARR_CONFIG_DIR": str(config_dir),
        "NO_CLI": "true",
        "PYTHONPATH": (os.path.join(worktree_root, "bazarr")
                       + os.pathsep + os.path.join(worktree_root, "custom_libs")),
    })

    # First boot: the file is written out fully, the token encrypted.
    completed = subprocess.run(
        [system_path.executable, "-c", "import app.config  # noqa: F401"],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    # Put the coerced token back as plaintext, changing nothing else: the
    # next boot sees memory equal to disk everywhere but this one value.
    stored = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    stored["bootnum"]["api_token"] = 12345678901234567
    config_file.write_text(yaml.safe_dump(stored, default_flow_style=False),
                           encoding="utf-8")

    # Second boot: only the plaintext detection forcing the rewrite can
    # cover the number now.
    completed = subprocess.run(
        [system_path.executable, "-c", "import app.config  # noqa: F401"],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    from dynaconf import Dynaconf

    on_disk = {k.lower(): v for k, v in Dynaconf(
        settings_file=str(config_file),
        core_loaders=["YAML"]).as_dict().items()}
    token = on_disk["bootnum"]["api_token"]
    assert isinstance(token, str) and is_encrypted(token)
    assert "12345678901234567" not in config_file.read_text(encoding="utf-8")


def test_a_boot_that_cannot_encrypt_the_disk_credentials_refuses_to_complete(tmp_path):
    """The boot write is what carries the encryption migration to disk. A
    registered plugin credential that cannot be encoded (a YAML-escaped lone
    surrogate) fails the encrypting write, and the boot does not complete
    with the plaintext credential still readable on disk: the startup
    write's failure raises out of the boot, the file keeps exactly what it
    had, and the pending migration survives so the next boot retries the
    rewrite once the write works."""
    import json as json_module
    import os
    import subprocess
    import sys as system_path

    config_dir = tmp_path / "boot-config"
    for subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
        (config_dir / subdirectory).mkdir(parents=True, exist_ok=True)
    (config_dir / "provider_hub").mkdir()
    config_file = config_dir / "config" / "config.yaml"
    config_file.write_text(
        "general:\n"
        "  secrets_encryption_key: 'unit-refusal-boot-master-key'\n"
        "bootvault:\n"
        "  api_token: \"\\ud800\"\n",
        encoding="utf-8",
    )
    (config_dir / "provider_hub" / "state.json").write_text(json_module.dumps({
        "installations": {
            "bootvault": {
                "provider_id": "bootvault",
                "state": "active",
                "manifest": {
                    "provider_id": "bootvault",
                    "config_schema": {"properties": {
                        "api_token": {"type": "string", "secret": True}}},
                },
            },
        },
    }), encoding="utf-8")

    worktree_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    environment = dict(os.environ)
    environment.update({
        "BAZARR_CONFIG_DIR": str(config_dir),
        "NO_CLI": "true",
        "PYTHONPATH": (os.path.join(worktree_root, "bazarr")
                       + os.pathsep + os.path.join(worktree_root, "custom_libs")),
    })
    completed = subprocess.run(
        [system_path.executable, "-c", "import app.config  # noqa: F401"],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode != 0, (
        "the boot completed with the plaintext credential on disk\n"
        + completed.stdout + completed.stderr)
    assert "could not be encrypted" in completed.stderr

    # Nothing was rewritten: the plaintext credential stays on disk for the
    # next boot's retry, never ciphertext under a key that never reached
    # the file.
    on_disk = config_file.read_text(encoding="utf-8")
    assert "\\ud800" in on_disk
    assert "enc:v1:" not in on_disk


def test_a_boot_that_cannot_persist_a_replacement_api_key_refuses_to_complete(tmp_path):
    """An undecryptable stored API key is the one failure the boot repairs
    itself: the boot mints a fresh key and the boot write persists it
    encrypted. That write failing is not survivable with no plaintext
    migration pending anywhere: unwritten, this boot would serve a key
    nothing on disk holds, and a restart would mint another while the
    stored one stays undecryptable. The boot write's failure raises out of
    the boot, and the file keeps the undecryptable ciphertext for the next
    boot's retry."""
    import json as json_module
    import os
    import subprocess
    import sys as system_path

    config_dir = tmp_path / "boot-config"
    for subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
        (config_dir / subdirectory).mkdir(parents=True, exist_ok=True)
    (config_dir / "provider_hub").mkdir()
    # The stored API key is proper ciphertext under a master key this boot
    # does not hold, and it is the only secret on disk: no plaintext
    # migration is pending anywhere.
    stored_key = encrypt_secret("unit-replacement-real-key",
                                master_key="unit-replacement-other-master-key")
    config_file = config_dir / "config" / "config.yaml"
    config_file.write_text(
        "general:\n"
        "  secrets_encryption_key: 'unit-replacement-boot-master-key'\n"
        "auth:\n"
        f"  apikey: '{stored_key}'\n",
        encoding="utf-8",
    )
    (config_dir / "provider_hub" / "state.json").write_text(json_module.dumps({
        "installations": {},
    }), encoding="utf-8")

    worktree_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    environment = dict(os.environ)
    environment.update({
        "BAZARR_CONFIG_DIR": str(config_dir),
        "NO_CLI": "true",
        "PYTHONPATH": (os.path.join(worktree_root, "bazarr")
                       + os.pathsep + os.path.join(worktree_root, "custom_libs")),
    })
    # The config write path fails after the replacement key was minted.
    # The writer is broken before the boot, so the from-import inside
    # app.config binds the broken one.
    child_code = (
        "import dynaconf.loaders.yaml_loader as _yaml_loader\n"
        "def _broken_write(*args, **kwargs):\n"
        "    raise RuntimeError('simulated settings write failure')\n"
        "_yaml_loader.write = _broken_write\n"
        "import app.config  # noqa: F401 - the boot this test is about\n"
    )
    completed = subprocess.run(
        [system_path.executable, "-c", child_code],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode != 0, (
        "the boot completed serving a replacement API key nothing on disk holds\n"
        + completed.stdout + completed.stderr)
    assert "replacement API key minted for the stored one that could not be " \
           "decrypted was not persisted" in completed.stderr
    # The refusal stands on the replacement key alone, not on the pending
    # migration wording: no plaintext migration was pending.
    assert "could not be encrypted" not in completed.stderr

    # Nothing was rewritten: the stored key keeps its undecryptable
    # ciphertext for the next boot's retry, never a fresh key this boot
    # never saved.
    on_disk = config_file.read_text(encoding="utf-8")
    assert stored_key in on_disk


def test_an_empty_api_key_never_authenticates_after_a_decrypt_failure(monkeypatch):
    """When a decrypt failure withholds auth.apikey as an empty string, an
    empty query key and an empty header must both be refused. The rule
    matters on its own: compare_digest answers that two empty strings
    match, so without the guards an empty stored key would authenticate an
    empty presented key."""
    from types import SimpleNamespace

    from flask import Flask

    import api.utils as api_utils

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    @flask_app.route("/probe")
    @api_utils.authenticate
    def probe():  # pragma: no cover - only reached if the gate wrongly passes
        return "reached"

    # The post-boot state of a failed decrypt: the stored key is withheld
    # as an empty string.
    monkeypatch.setattr(
        api_utils, "settings",
        SimpleNamespace(auth=SimpleNamespace(apikey="")))
    client = flask_app.test_client()
    # Two empty strings would satisfy compare_digest; the guards refuse
    # both the empty presented key and the empty stored one by rule.
    assert client.get("/probe?apikey=").status_code == 401
    assert client.get("/probe", headers={"X-API-KEY": ""}).status_code == 401
    # A non-empty presented value does not get in either.
    assert client.get("/probe?apikey=anything").status_code == 401
    assert client.get("/probe", headers={"X-API-KEY": "anything"}).status_code == 401

    # The gate still opens for a real key: the refusal above is the empty
    # stored secret's, not a broken probe.
    monkeypatch.setattr(
        api_utils, "settings",
        SimpleNamespace(auth=SimpleNamespace(apikey="unit-real-key-123456789")))
    client = flask_app.test_client()
    assert client.get(
        "/probe", headers={"X-API-KEY": "unit-real-key-123456789"}).status_code == 200


def test_has_plaintext_secrets_on_disk_detects_a_coerced_number():
    """The force-migration trigger covers the shape an older save left
    behind: a registered path holding a number or a boolean (a typed
    token the old casts coerced) is plaintext needing the rewrite, not a
    value the rewrite may leave in the clear. An empty string is still not
    flagged, and neither is an unregistered field."""
    from secret_store import registry
    from secret_store.migration import (
        encrypt_settings_dict,
        has_plaintext_secrets_on_disk,
    )

    settings = _DynamicSettings({
        "coercedhub": {"api_token": 12345678901234567, "flag": True},
    })
    # Without the registration these are just plugin fields nobody covers.
    assert has_plaintext_secrets_on_disk(settings) is False

    saved = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths({"coercedhub.api_token", "coercedhub.flag"})
    try:
        assert has_plaintext_secrets_on_disk(settings) is True
        # Each shape alone is enough, so a rewrite is never left waiting on
        # a second one.
        for key in ("api_token", "flag"):
            alone = _DynamicSettings({"coercedhub": {key: settings.coercedhub[key]}})
            assert has_plaintext_secrets_on_disk(alone) is True
        # An empty string at a registered path is not plaintext.
        empty = _DynamicSettings({"coercedhub": {"api_token": ""}})
        assert has_plaintext_secrets_on_disk(empty) is False
        # The rewrite stores the coerced value as the text it was typed as.
        snapshot = {"coercedhub": dict(settings.coercedhub)}
        encrypted = encrypt_settings_dict(snapshot)
        assert is_encrypted(encrypted["coercedhub"]["api_token"])
        assert is_encrypted(encrypted["coercedhub"]["flag"])
    finally:
        registry.reset_plugin_secret_paths(saved)


def test_a_sentinel_string_typed_into_a_credential_is_stored_as_the_literal_text(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A registered credential is identified before the generic
    normalizations, so a literal 'null' typed into a secret is the secret:
    it never becomes the None that means 'clear this setting'."""
    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        config.save_settings([(f"settings-{provider_id}-api_token", ["null"])])
        assert config.settings[provider_id]["api_token"] == "null"
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert "enc:v1:" in on_disk
        # The typed text survives a restart.
        config.restore_persisted_settings()
        assert config.settings[provider_id]["api_token"] == "null"
    finally:
        _remove_settings_section(provider_id)


def test_a_secret_field_named_like_a_dict_setting_stays_text(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A plugin secret whose field name collides with a dict-typed setting
    ('tiers') is stored as the typed text: the JSON conversion exists for
    ordinary settings, and letting it parse the credential would store a
    dict that neither encrypts at rest nor masks in the API."""
    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.tiers"})
    config = _prepare_save(monkeypatch)

    try:
        typed = '{"free": 1, "vip": 2}'
        config.save_settings([(f"settings-{provider_id}-tiers", [typed])])
        assert config.settings[provider_id]["tiers"] == typed
        assert isinstance(config.settings[provider_id]["tiers"], str)
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert "enc:v1:" in on_disk
        config.restore_persisted_settings()
        assert config.settings[provider_id]["tiers"] == typed
    finally:
        _remove_settings_section(provider_id)


def test_a_repeated_credential_key_refusal_leaves_nothing_applied(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A repeated form key for a credential arrives as a list and is
    refused. The refusal runs before anything is applied, so the items
    earlier in the same request are not left applied in memory, waiting
    for the next unrelated save to write them."""
    from app.config import ValidationError

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.api_token"})
    config = _prepare_save(monkeypatch)

    try:
        before_region = config.settings.get(provider_id, {}).get("region")
        with pytest.raises(ValidationError):
            config.save_settings([
                (f"settings-{provider_id}-region", ["us"]),
                (f"settings-{provider_id}-api_token", ["first", "second"]),
            ])
        assert config.settings.get(provider_id, {}).get("region") == before_region
        assert config.settings.get(provider_id, {}).get("api_token", "") == ""
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            assert "first" not in handle.read()
    finally:
        _remove_settings_section(provider_id)


def test_a_removed_plugins_numeric_token_is_hidden_by_the_settings_api(
        plugin_secret_registry, real_master_key):
    """A removed plugin's numeric token, once the scan has registered its
    path, is a stored credential like any other: the settings API returns
    an empty value and a descriptor, never the number."""
    import json as json_module

    from app.config import get_settings, settings

    plugin_secret_registry.reset_plugin_secret_paths({"gonehub.numeric_token"})
    token = 12345678901234567
    settings.set("gonehub", {"numeric_token": token, "region": "eu"})
    try:
        result = get_settings()
        assert result["gonehub"]["numeric_token"] == ""
        assert result["gonehub"]["region"] == "eu"
        descriptor = result["gonehub"]["__secret_fields"]["numeric_token"]
        assert descriptor["configured"] is True
        assert "last_four" not in descriptor
        assert str(token) not in json_module.dumps(result)
    finally:
        _remove_settings_section("gonehub")


def test_plex_sign_out_clears_a_failed_tokens_ciphertext_from_disk(
        monkeypatch, real_master_key):
    """Signing out is a purposeful clear: with plex.token failing to
    decrypt at boot, the logout ends the failure itself, so no recoverable
    ciphertext is preserved on disk and no health issue keeps asking for
    an account the user just signed out of."""
    from api.plex import oauth
    from app.config import (config_yaml_file, restore_persisted_settings,
                            settings, write_config)
    from secret_store import migration
    from secret_store.crypto import SECRET_MARKER_PREFIX

    monkeypatch.setattr(oauth, "sync_plex_account", lambda **_kwargs: None)
    original_token = settings.plex.token

    ciphertext = SECRET_MARKER_PREFIX + "tampered-by-a-signout-test"
    settings.plex.token = ciphertext
    write_config()
    restore_persisted_settings()
    assert settings.plex.token == ""
    assert "plex.token" in migration.decrypt_failure_paths()

    try:
        from flask import Flask

        # The endpoint's own gate runs when post() is called directly, so
        # the request carries the live key the same browser session would.
        with Flask(__name__).test_request_context(
                "/", method="POST",
                headers={"X-API-KEY": settings.auth.apikey}):
            response = oauth.PlexLogout().post()
        assert response == {"success": True}
        # The failure is over and the ciphertext is not preserved.
        assert "plex.token" not in migration.decrypt_failure_paths()
        with open(config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert ciphertext not in on_disk
        assert migration.decrypt_failure_issues() == []
    finally:
        # The sign-out cleared the account fields (they were the test
        # config's defaults); put the token back the way it was.
        settings.plex.token = original_token
        write_config()


def test_ensure_secrets_over_a_failed_token_clears_the_health_issue(
        monkeypatch, real_master_key):
    """A compat token that failed to decrypt at boot is withheld as empty,
    which the generator reads as missing: the regenerated token replaces
    it, the write lands, and the failure (and its health issue) is over
    instead of asking for a re-entry that already happened."""
    from api.system import compat_admin
    from app.config import settings
    from secret_store import migration

    original = settings.compat_endpoint.token
    migration._FAILED_DECRYPT_PATHS.add("compat_endpoint.token")
    try:
        settings.compat_endpoint.token = ""
        compat_admin.ensure_secrets()
        assert settings.compat_endpoint.token
        assert len(settings.compat_endpoint.token) >= 32
        assert "compat_endpoint.token" not in migration.decrypt_failure_paths()
        assert migration.decrypt_failure_issues() == []
    finally:
        settings.compat_endpoint.token = original


def _boot_child(tmp_path, config_text, child_code):
    """Run a fresh process against a fresh config directory and return its
    printed JSON, the same harness the boot-order test uses."""
    import os
    import subprocess
    import sys as system_path

    config_dir = tmp_path / "boot-config"
    for subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
        (config_dir / subdirectory).mkdir(parents=True, exist_ok=True)
    (config_dir / "provider_hub").mkdir()
    (config_dir / "config" / "config.yaml").write_text(config_text, encoding="utf-8")

    worktree_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    environment = dict(os.environ)
    environment.update({
        "BAZARR_CONFIG_DIR": str(config_dir),
        "NO_CLI": "true",
        "PYTHONPATH": (os.path.join(worktree_root, "bazarr")
                       + os.pathsep + os.path.join(worktree_root, "custom_libs")),
    })
    completed = subprocess.run(
        [system_path.executable, "-c", child_code],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    import json as json_module
    return config_dir, json_module.loads(completed.stdout.strip().splitlines()[-1])


def test_an_undecryptable_api_key_is_replaced_and_keeps_the_ui_working(tmp_path):
    """A real process boot with an auth.apikey that will not decrypt. The
    key is Bazarr's own and the SPA sends it on every request, so
    withholding it as empty would lock the UI and the API out entirely:
    the boot mints a fresh key with the validator's generator, clears the
    failure, raises a health notice saying the key was replaced, and the
    page injection and the API both accept the new key. An empty presented
    key is still refused."""
    import json as json_module

    from secret_store.crypto import SECRET_MARKER_PREFIX, decrypt_secret, is_encrypted

    ciphertext = SECRET_MARKER_PREFIX + "tampered-by-an-apikey-boot"
    master_key = "unit-apikey-boot-master-key"
    config_dir, result = _boot_child(
        tmp_path,
        f"general:\n"
        f"  secrets_encryption_key: '{master_key}'\n"
        f"auth:\n"
        f"  apikey: '{ciphertext}'\n",
        "import json\n"
        "import app.config  # noqa: F401 - the boot this test is about\n"
        "from app.config import settings\n"
        "from flask import Flask\n"
        "import api.utils as api_utils\n"
        "from secret_store.migration import (decrypt_failure_paths,\n"
        "                                    replaced_secret_issues)\n"
        "\n"
        "probe = Flask('probe')\n"
        "probe.config['SECRET_KEY'] = 'unit'\n"
        "\n"
        "@probe.route('/probe')\n"
        "@api_utils.authenticate\n"
        "def probe_route():\n"
        "    return 'reached'\n"
        "\n"
        "client = probe.test_client()\n"
        "inject = None\n"
        "page_error = None\n"
        "try:\n"
        "    import app.ui as ui\n"
        "    captured = {}\n"
        "    ui.render_template = lambda template, **kwargs: (\n"
        "        captured.update(kwargs) or '')\n"
        "    page = Flask('page')\n"
        "    page.config['SECRET_KEY'] = 'unit'\n"
        "    page.register_blueprint(ui.ui_bp)\n"
        "    page.test_client().get('/')\n"
        "    inject = (captured.get('BAZARR_SERVER_INJECT') or {}).get('apiKey')\n"
        "except Exception as error:\n"
        "    page_error = type(error).__name__\n"
        "\n"
        "print(json.dumps({\n"
        "    'live': settings.auth.apikey,\n"
        "    'failures': sorted(decrypt_failure_paths()),\n"
        "    'notices': replaced_secret_issues(),\n"
        "    'accepted': client.get('/probe',\n"
        "                            headers={'X-API-KEY': settings.auth.apikey}).status_code,\n"
        "    'query_accepted': client.get(\n"
        "        '/probe?apikey=' + settings.auth.apikey).status_code,\n"
        "    'empty_query': client.get('/probe?apikey=').status_code,\n"
        "    'empty_header': client.get('/probe',\n"
        "                                headers={'X-API-KEY': ''}).status_code,\n"
        "    'inject': inject,\n"
        "    'page_error': page_error,\n"
        "}))\n",
    )

    live = result["live"]
    # The live key is a fresh one, not the withheld empty string and never
    # the ciphertext, and the failure it replaced is over.
    assert isinstance(live, str) and live
    assert live != ciphertext
    assert result["failures"] == []
    # The health notice says the key was replaced and what to do about it.
    assert result["notices"] == [{
        "object": "auth.apikey",
        "issue": "The stored API key could not be decrypted and was replaced. Update the "
                 "tools and scripts that use the API key.",
    }]
    # The API accepts the new key, by header and by query, and an empty
    # presented key is still refused.
    assert result["accepted"] == 200
    assert result["query_accepted"] == 200
    assert result["empty_query"] == 401
    assert result["empty_header"] == 401
    # The SPA's page injection carries the new key.
    assert result["page_error"] is None, result["page_error"]
    assert result["inject"] == live

    # The replacement reached the file: a fresh disk reader sees ciphertext
    # of the minted key (not the tampered bytes), under the same master key.
    from dynaconf import Dynaconf

    on_disk = {k.lower(): v for k, v in Dynaconf(
        settings_file=str(config_dir / "config" / "config.yaml"),
        core_loaders=["YAML"]).as_dict().items()}
    stored = on_disk["auth"]["apikey"]
    assert isinstance(stored, str) and is_encrypted(stored)
    assert stored != ciphertext
    assert decrypt_secret(stored, master_key=master_key) == live
    assert "tampered-by-an-apikey-boot" not in json_module.dumps(on_disk)


def test_an_empty_api_key_is_not_regenerated(tmp_path):
    """An empty stored API key is not a decrypt failure and is not
    regenerated: an install that stores an empty key keeps it, exactly as
    before the secret store."""
    _config_dir, result = _boot_child(
        tmp_path,
        "general:\n"
        "  secrets_encryption_key: 'unit-empty-key-boot-master-key'\n"
        "auth:\n"
        "  apikey: ''\n",
        "import json\n"
        "import app.config  # noqa: F401 - the boot this test is about\n"
        "from app.config import settings\n"
        "from secret_store.migration import (decrypt_failure_paths,\n"
        "                                    replaced_secret_issues)\n"
        "print(json.dumps({\n"
        "    'live': settings.auth.apikey,\n"
        "    'failures': sorted(decrypt_failure_paths()),\n"
        "    'notices': replaced_secret_issues(),\n"
        "}))\n",
    )
    assert result["live"] == ""
    assert result["failures"] == []
    assert result["notices"] == []


class _ScanOnlySettings:
    """as_dict-only stand-in for the settings root, so a loader call in an
    in-process test stays scoped to the sections the test hands it."""

    def __init__(self, sections):
        self._sections = {str(name).lower(): values for name, values in sections.items()}

    def as_dict(self):
        return self._sections


def test_a_failed_scan_withholds_a_new_plaintext_leftover_from_the_api(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A removed plugin's plaintext token first found on a boot whose
    leftover scan cannot run (here the manifest module is broken) is never
    handed out: the fallback cannot register it safely without the reserved
    list, so the settings API returns an empty value for it until a working
    boot covers it for real. A Bazarr section keeps its normal handling."""
    import json as json_module
    import sys

    from app.config import get_settings, settings
    from secret_store import registry

    token = "unit-ghost-token-0123456789abcd"  # pragma: allowlist secret
    settings.set("ghostplugin", {"api_token": token, "region": "unit-region"})
    try:
        # The manifest module cannot be imported, which is what stops the
        # scan and takes the reserved-section list with it.
        monkeypatch.setitem(sys.modules, "provider_hub.manifest", None)
        registry.load_plugin_secret_paths(_ScanOnlySettings({
            "ghostplugin": {"api_token": token, "region": "unit-region"},
            "sonarr": {"apikey": "unit-sonarr-key"},
        }))

        assert not registry.is_plugin_secret("ghostplugin.api_token")
        assert registry.hidden_leftover_paths() == frozenset({
            "ghostplugin.api_token", "ghostplugin.region"})
        result = get_settings()
        assert result["ghostplugin"]["api_token"] == ""
        assert result["ghostplugin"]["region"] == ""
        # A Bazarr section keeps its normal handling: nothing of its own is
        # withheld on the scan's say-so.
        assert "sonarr.apikey" not in registry.hidden_leftover_paths()
        assert token not in json_module.dumps(result)
    finally:
        _remove_settings_section("ghostplugin")


def test_a_failed_scan_then_a_failed_enumeration_refuses_the_boot(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A scan that cannot run hands its leftovers to the fallback, and a
    fallback that cannot read the settings sections has no candidate it
    can vouch for and none to hide either: a loader that returned quietly
    would leave the plaintext leftover first found on that boot neither
    registered nor hidden, and the next successful settings read would
    serve it in the clear. The loader refuses to complete the boot, so
    the settings API never serves the value."""
    from app.config import settings
    from secret_store import registry
    from secret_store.registry import HubSecretRegistrationError

    class _UnreadableSections:
        """as_dict-only stand-in whose enumeration is transiently broken:
        the one read the scan, the fallback and the backstop each make of
        the sections fails, while the settings themselves stay readable."""

        def as_dict(self):
            raise RuntimeError("the settings enumeration is transiently broken")

    token = "unit-ghost-token-0123456789abcd"  # pragma: allowlist secret
    settings.set("ghostplugin", {"api_token": token})
    try:
        with pytest.raises(HubSecretRegistrationError):
            registry.load_plugin_secret_paths(_UnreadableSections())
    finally:
        _remove_settings_section("ghostplugin")


def test_a_malformed_persisted_path_never_breaks_settings_saves(
        tmp_path, monkeypatch, plugin_secret_registry, real_master_key):
    """A malformed persisted set in state.json (a bare string where the
    list belongs) is validated away at load: its characters are not
    registered one at a time (a path with no dot would make every settings
    save fail on the split), the durable registrations still happen, and
    an unrelated settings save keeps working."""
    from app import config
    from secret_store import registry

    import json as json_module

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json_module.dumps({
        "installations": {},
        "plugin_secret_paths": "bogus",
    }), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    registry.load_plugin_secret_paths(_ScanOnlySettings({}))

    assert registry.plugin_secret_paths() == set()
    assert not any(len(path) <= 2 for path in registry.at_rest_secret_paths())

    # A settings save still lands: the at-rest loops never see a path they
    # cannot split.
    assert config.write_config() is True


def test_a_registered_secret_named_like_an_array_setting_saves_as_text(
        monkeypatch, plugin_secret_registry, real_master_key):
    """A registered credential whose field name collides with an array-typed
    setting is still a scalar credential: the single-element form unwraps
    independently of the array-keys list, none of the array conversions
    touch it, and the refusal a malformed credential causes is decided
    before anything in the request is applied."""
    from app.config import ValidationError

    provider_id = "e2ehub"
    plugin_secret_registry.reset_plugin_secret_paths({f"{provider_id}.path_mappings"})
    config = _prepare_save(monkeypatch)

    try:
        # The repeated form shape unwraps and stores as the typed text, and
        # an ordinary field in the same request is applied with it.
        config.save_settings([
            (f"settings-{provider_id}-region", ["us"]),
            (f"settings-{provider_id}-path_mappings", ["unit-path-token"]),
        ])
        assert config.settings[provider_id]["region"] == "us"
        assert config.settings[provider_id]["path_mappings"] == "unit-path-token"
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert "unit-path-token" not in on_disk

        # A direct scalar goes through none of the array conversions either.
        config.save_settings([(f"settings-{provider_id}-path_mappings", "unit-direct-text")])
        assert config.settings[provider_id]["path_mappings"] == "unit-direct-text"

        # A list with more than one element is refused, and the refusal is
        # atomic: the ordinary field earlier in the request is not applied.
        with pytest.raises(ValidationError):
            config.save_settings([
                (f"settings-{provider_id}-region", ["eu"]),
                (f"settings-{provider_id}-path_mappings", ["first", "second"]),
            ])
        assert config.settings[provider_id]["region"] == "us"
    finally:
        _remove_settings_section(provider_id)


def test_plex_sign_in_clears_a_failed_credentials_records_like_sign_out(
        monkeypatch, real_master_key):
    """Signing in replaces the account's credentials, so a boot that
    withheld them stops asking: the sign-in branch clears the decrypt
    failures of the paths it sets, exactly as the sign-out branch does, and
    the old ciphertext is not preserved on disk over the fresh values."""
    from types import SimpleNamespace

    from api.plex import oauth
    from app.config import (config_yaml_file, restore_persisted_settings,
                            settings, write_config)
    from secret_store import migration
    from secret_store.crypto import SECRET_MARKER_PREFIX

    original = dict(settings.plex)
    original_use_plex = settings.general.use_plex
    monkeypatch.setattr(oauth, "sync_plex_account", lambda **_kwargs: None)
    monkeypatch.setattr(oauth, "validate_plex_token", lambda _token: {
        "id": 1, "username": "unit-user", "email": "unit-user@example.com"})
    monkeypatch.setattr(oauth, "requests", SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"authToken": "unit-new-auth-token"})))
    monkeypatch.setattr(oauth, "pin_cache", SimpleNamespace(
        get=lambda _pin_id: {"client_id": "unit-client-id", "state_token": None},
        delete=lambda _pin_id: None))

    apikey_ciphertext = SECRET_MARKER_PREFIX + "tampered-by-a-signin-test"
    token_ciphertext = SECRET_MARKER_PREFIX + "tampered-token-by-a-signin-test"
    settings.plex.apikey = apikey_ciphertext
    settings.plex.token = token_ciphertext
    write_config()
    restore_persisted_settings()
    assert settings.plex.apikey == ""
    assert settings.plex.token == ""
    assert "plex.apikey" in migration.decrypt_failure_paths()
    assert "plex.token" in migration.decrypt_failure_paths()

    try:
        from flask import Flask

        # The endpoint's own gate runs when get() is called directly, so the
        # request carries the live key the same browser session would.
        with Flask(__name__).test_request_context(
                "/", method="GET",
                headers={"X-API-KEY": settings.auth.apikey}):
            response = oauth.PlexPinCheck().get("unit-pin-id")
        assert response["data"]["authenticated"] is True

        # The failures are over: no health issue keeps asking for an API key
        # the user replaced with the sign-in.
        assert migration.decrypt_failure_paths() == frozenset()
        assert migration.decrypt_failure_issues() == []
        with open(config_yaml_file, encoding="utf-8") as handle:
            on_disk = handle.read()
        assert apikey_ciphertext not in on_disk
        assert token_ciphertext not in on_disk
        assert "unit-new-auth-token" not in on_disk
    finally:
        settings.plex.token = original.get("token")
        settings.plex.apikey = original.get("apikey")
        settings.plex.username = original.get("username")
        settings.plex.email = original.get("email")
        settings.general.use_plex = original_use_plex
        write_config()
