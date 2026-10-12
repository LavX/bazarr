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


@pytest.fixture(autouse=True)
def _clean_decrypt_failures():
    """The fail-closed decrypt pass tracks per-path failures in module state.
    Keep it clean around every test in this file so one test's failure never
    leaks into another's assertions."""
    from secret_store.migration import reset_decrypt_failures

    reset_decrypt_failures()
    yield
    reset_decrypt_failures()


@pytest.fixture
def plaintext_dict():
    return {
        # Mirrors the post-bootstrap shape: get_master_key has already
        # generated and persisted general.secrets_encryption_key. The
        # encrypt-settings-dict path expects the master key to be in
        # the snapshot so on-disk ciphertext stays paired with the key
        # that decrypts it (a first-write race).
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


def test_tmdb_is_encrypted_and_roundtrips_without_double_encryption(plaintext_dict):
    plaintext_dict["discover"] = {"tmdb_access_token": "synthetic-tmdb-token"}
    encrypted = encrypt_settings_dict(plaintext_dict)
    assert encrypted["discover"]["tmdb_access_token"].startswith(SECRET_MARKER_PREFIX)
    assert encrypt_settings_dict(encrypted) == encrypted
    assert decrypt_settings_dict(encrypted) == plaintext_dict


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
    """A single corrupt cipher must not stop bazarr from booting, and it must
    never become the credential: the live value is withheld (empty) and the
    path is tracked so a later save preserves the on-disk ciphertext. The
    operator rotates that one credential via the Settings page."""
    import json

    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    good = encrypt_secret("good-key")
    settings = _FakeSettings({
        "sonarr": {"apikey": bad},
        "radarr": {"apikey": good},
        "general": {}, "auth": {}, "compat_endpoint": {}, "plex": {},
        "jellyfin": {}, "translator": {}, "proxy": {}, "postgresql": {},
        "opensubtitles": {}, "opensubtitlescom": {}, "addic7ed": {},
        "legendasdivx": {}, "legendasnet": {},
        "deathbycaptcha": {}, "napisy24": {}, "titlovi": {}, "titulky": {},
        "karagarga": {}, "assrt": {}, "betaseries": {}, "jimaku": {},
        "subdl": {}, "subsource": {}, "subx": {}, "subsro": {}, "omdb": {},
    })
    with caplog.at_level("ERROR"):
        decrypt_settings_in_place(settings)
    assert settings.radarr.apikey == "good-key"
    # The undecryptable value is withheld from every consumer, so no code path
    # can send the ciphertext anywhere as if it were the credential.
    assert settings.sonarr.apikey == ""
    assert any("Failed to decrypt secret" in r.message for r in caplog.records)
    # The log stays nameless, but the failure is tracked per path so the
    # health issue can name the setting (never the value) and write_config
    # can preserve the recoverable ciphertext.
    from secret_store.migration import decrypt_failure_issues, decrypt_failure_paths

    assert decrypt_failure_paths() == {"sonarr.apikey"}
    issues = decrypt_failure_issues()
    assert issues == [{
        "object": "sonarr.apikey",
        "issue": "The stored credential could not be decrypted (the master key may have "
                 "changed). Re-enter this credential in Settings and save.",
    }]
    assert "tampered-payload" not in json.dumps(issues)
    # The error log must NOT include the value either, and not the section
    # either: the failure is nameless in the log so nothing about it can be
    # inferred to a specific provider in a public bug report.
    for r in caplog.records:
        assert "tampered-payload" not in r.message
        assert "sonarr" not in r.message.lower()


def test_failed_decrypt_ciphertext_survives_a_save_that_keeps_it_empty():
    """write_config must not overwrite recoverable ciphertext with the
    withheld empty value: a save that leaves the failed credential alone
    keeps the on-disk cipher, and only a re-entered value replaces it."""
    from secret_store.migration import preserve_recoverable_ciphertexts

    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    good = encrypt_secret("good-key")
    settings = _FakeSettings({
        "sonarr": {"apikey": bad},
        "radarr": {"apikey": good},
        "general": {}, "auth": {}, "compat_endpoint": {}, "plex": {},
        "jellyfin": {}, "translator": {}, "proxy": {}, "postgresql": {},
        "opensubtitles": {}, "opensubtitlescom": {}, "addic7ed": {},
        "legendasdivx": {}, "legendasnet": {},
        "deathbycaptcha": {}, "napisy24": {}, "titlovi": {}, "titulky": {},
        "karagarga": {}, "assrt": {}, "betaseries": {}, "jimaku": {},
        "subdl": {}, "subsource": {}, "subx": {}, "subsro": {}, "omdb": {},
    })
    decrypt_settings_in_place(settings)

    on_disk = {"sonarr": {"apikey": bad}, "radarr": {"apikey": good}, "general": {}}
    live = {"sonarr": {"apikey": ""}, "radarr": {"apikey": "good-key"}, "general": {}}

    payload = encrypt_settings_dict(live)
    preserve_recoverable_ciphertexts(payload, live, on_disk)
    assert payload["sonarr"]["apikey"] == bad
    assert payload["radarr"]["apikey"].startswith(SECRET_MARKER_PREFIX)

    # A re-entered value replaces the broken cipher.
    reentered = {"sonarr": {"apikey": "unit-reentered-key"}, "general": {}}
    payload = encrypt_settings_dict(reentered)
    preserve_recoverable_ciphertexts(payload, reentered, on_disk)
    assert payload["sonarr"]["apikey"] != bad
    assert payload["sonarr"]["apikey"].startswith(SECRET_MARKER_PREFIX)


@pytest.fixture
def plugin_secret_paths():
    from secret_store import registry

    saved = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    yield registry
    registry.reset_plugin_secret_paths(saved)


def test_plugin_secret_path_is_encrypted_on_write_and_on_first_boot(plugin_secret_paths):
    """A registered plugin secret is encrypted at rest like a built-in
    credential, detected as plaintext before the first rewrite, and
    decrypted back into the live settings on boot."""
    from secret_store.migration import has_plaintext_secrets_on_disk

    plaintext_dict = {
        "general": {"secrets_encryption_key": "test-master-key-not-secret-but-stable-for-tests"},
        "smokehub": {"api_token": "unit-plugin-token", "region": "eu"},
        "subsource": {"api_key": "unit-plugin-key"},
    }
    # With no plugin path registered, nothing in this dict is a plaintext
    # credential the tiers know, so the check proves the plugin rule rather
    # than riding on a static tier member.
    assert has_plaintext_secrets_on_disk(_FakeSettings(plaintext_dict)) is False

    plugin_secret_paths.reset_plugin_secret_paths({"smokehub.api_token", "subsource.api_key"})
    # Plaintext plugin secrets are seen before any rewrite happens.
    assert has_plaintext_secrets_on_disk(_FakeSettings(plaintext_dict)) is True

    out = encrypt_settings_dict(plaintext_dict)
    assert out["smokehub"]["api_token"].startswith(SECRET_MARKER_PREFIX)
    assert out["subsource"]["api_key"].startswith(SECRET_MARKER_PREFIX)
    # A declared non-secret field keeps its plain value; only the registered
    # secret paths are touched.
    assert out["smokehub"]["region"] == "eu"

    # Reboot: ciphertext on disk decrypts back to plaintext in memory.
    settings = _FakeSettings({
        "smokehub": dict(out["smokehub"]),
        "subsource": dict(out["subsource"]),
    })
    decrypt_settings_in_place(settings)
    assert settings.smokehub.api_token == "unit-plugin-token"
    assert settings.subsource.api_key == "unit-plugin-key"

    # The comparison form decrypts plugin paths too, so write_config's
    # equality check does not rewrite the file on every save.
    assert decrypt_settings_dict(out) == plaintext_dict


def test_a_collection_at_a_registered_secret_path_refuses_the_rewrite(plugin_secret_paths):
    """A list or a dictionary an earlier write left at a registered secret
    path in the settings can be neither encrypted at rest nor covered, so
    the plaintext detection flags it and the encryption refuses rather than
    return it in the payload as plaintext: the forced rewrite fails, and
    the boot does not complete while the value may remain readable in the
    clear. An empty collection is not a credential, the string rule's empty
    stays empty, so it changes nothing."""
    from secret_store.migration import has_plaintext_secrets_on_disk

    plugin_secret_paths.reset_plugin_secret_paths({"smokehub.api_token"})
    settings = _FakeSettings({
        "smokehub": {"api_token": ["unit-legacy-token-a", "unit-legacy-token-b"],
                     "region": "eu"},
    })

    # The collection is seen as a credential the rewrite cannot converge on,
    # not skipped as a shape the tiers do not know.
    assert has_plaintext_secrets_on_disk(settings) is True

    # The rewrite refuses rather than write the credential in the clear.
    with pytest.raises(ValueError):
        encrypt_settings_dict({
            "smokehub": dict(settings.smokehub),
            "general": {"secrets_encryption_key": "test-master-key-not-secret-but-stable-for-tests"},
        })

    # An empty collection is not a credential: empty stays empty, and the
    # rewrite carries it through untouched.
    empty = _FakeSettings({"smokehub": {"api_token": []}})
    assert has_plaintext_secrets_on_disk(empty) is False
    out = encrypt_settings_dict({"smokehub": {"api_token": []}, "general": {}})
    assert out["smokehub"]["api_token"] == []


def test_plugin_secret_decrypt_failure_withholds_the_value(plugin_secret_paths):
    """A plugin secret that will not decrypt is withheld and tracked, exactly
    like a built-in credential."""
    plugin_secret_paths.reset_plugin_secret_paths({"smokehub.api_token"})

    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    settings = _FakeSettings({
        "smokehub": {"api_token": bad, "region": "eu"},
    })
    decrypt_settings_in_place(settings)
    assert settings.smokehub.api_token == ""
    assert settings.smokehub.region == "eu"

    from secret_store.migration import decrypt_failure_paths

    assert "smokehub.api_token" in decrypt_failure_paths()


def test_decrypt_failure_paths_are_reset_between_boots(plugin_secret_paths):
    """The failure set describes one boot pass. A fresh boot with no failures
    reports none, and a value that decrypts again clears its path."""
    from secret_store.migration import decrypt_failure_paths, reset_decrypt_failures

    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    settings = _FakeSettings({"sonarr": {"apikey": bad}})
    decrypt_settings_in_place(settings)
    assert decrypt_failure_paths() == {"sonarr.apikey"}

    reset_decrypt_failures()
    assert decrypt_failure_paths() == frozenset()

    good = encrypt_secret("unit-key")
    settings = _FakeSettings({"sonarr": {"apikey": good}})
    decrypt_settings_in_place(settings)
    assert settings.sonarr.apikey == "unit-key"
    assert decrypt_failure_paths() == frozenset()


def test_clear_decrypt_failure_path_ends_the_failure(plugin_secret_paths):
    """A save that carries a path (a new value or an explicit clear) ends
    its decrypt failure: the path stops reporting, and the preserve pass
    no longer holds the old ciphertext over what the save wrote."""
    from secret_store.migration import (
        clear_decrypt_failure_path,
        decrypt_failure_paths,
        preserve_recoverable_ciphertexts,
    )

    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    settings = _FakeSettings({"sonarr": {"apikey": bad}})
    decrypt_settings_in_place(settings)
    assert decrypt_failure_paths() == {"sonarr.apikey"}

    # The explicit clear: nothing left on disk for this path, and the
    # failure is over.
    cleared = {"sonarr": {"apikey": ""}, "general": {}}
    payload = encrypt_settings_dict(cleared)
    clear_decrypt_failure_path("sonarr.apikey")
    preserve_recoverable_ciphertexts(payload, cleared, {"sonarr": {"apikey": bad}})
    assert payload["sonarr"]["apikey"] == ""
    assert decrypt_failure_paths() == frozenset()


def test_partial_list_failure_keeps_the_failed_element_ciphertext():
    """One element of a list-shaped secret that fails to decrypt is
    withheld, but its ciphertext survives an unrelated save: it would
    decrypt again under the right master key, and losing it is exactly
    what the failure set exists to prevent. A save that carries the list
    (an explicit replacement) replaces the whole thing."""
    from secret_store.migration import preserve_recoverable_ciphertexts

    good = encrypt_secret("gemini-1")
    bad = SECRET_MARKER_PREFIX + "tampered-payload"
    settings = _FakeSettings({"translator": {"gemini_keys": [good, bad]}})
    decrypt_settings_in_place(settings)
    # The good element decrypts; the failed one is withheld, never handed
    # out as the credential.
    assert settings.translator.gemini_keys == ["gemini-1"]

    # An unrelated save re-encrypts the live (withheld) list; the failed
    # element's ciphertext is re-appended, so nothing recoverable is lost.
    live = {"translator": {"gemini_keys": ["gemini-1"]}, "general": {}}
    payload = encrypt_settings_dict(live)
    on_disk = {"translator": {"gemini_keys": [good, bad]}}
    preserve_recoverable_ciphertexts(payload, live, on_disk)
    assert payload["translator"]["gemini_keys"][0].startswith(SECRET_MARKER_PREFIX)
    assert payload["translator"]["gemini_keys"][1] == bad

    # An explicit replacement of the list drops the failed element with
    # it: the save carried the path, so the failure is over.
    from secret_store.migration import clear_decrypt_failure_path

    replaced = {"translator": {"gemini_keys": ["fresh-key"]}, "general": {}}
    payload = encrypt_settings_dict(replaced)
    clear_decrypt_failure_path("translator.gemini_keys")
    preserve_recoverable_ciphertexts(payload, replaced, on_disk)
    assert payload["translator"]["gemini_keys"] == [
        payload["translator"]["gemini_keys"][0]]

    # Every element failing is the empty-list case: the whole on-disk list
    # is kept for the same reason.
    from secret_store.migration import reset_decrypt_failures

    reset_decrypt_failures()
    settings = _FakeSettings({"translator": {"gemini_keys": [bad]}})
    decrypt_settings_in_place(settings)
    assert settings.translator.gemini_keys == []
    live = {"translator": {"gemini_keys": []}, "general": {}}
    payload = encrypt_settings_dict(live)
    preserve_recoverable_ciphertexts(payload, live, {"translator": {"gemini_keys": [bad]}})
    assert payload["translator"]["gemini_keys"] == [bad]


# --- Coerced scalars: the live value, the on-disk text and the manifest ---


def decrypt_secret_value(value):
    from secret_store.crypto import decrypt_secret
    return decrypt_secret(value)


def test_a_coerced_boolean_is_stored_as_the_lowercase_text_it_was_typed(
        plugin_secret_paths):
    """A boolean left at a registered path by an older save is the text the
    user typed, not Python's capitalized repr: "true" and "false" round-trip
    through the settings write path, while "True" would decrypt as something
    nobody typed and "False" would count as truthy. Numbers keep their
    digits, as before."""
    from secret_store.migration import has_plaintext_secrets_on_disk

    plugin_secret_paths.reset_plugin_secret_paths(
        {"coercedhub.flag", "coercedhub.off", "coercedhub.token"})
    live = _FakeSettings({"coercedhub": {"flag": True, "off": False, "token": 50}})

    assert has_plaintext_secrets_on_disk(live) is True
    snapshot = {"coercedhub": dict(live.coercedhub)}
    encrypted = encrypt_settings_dict(snapshot)
    assert decrypt_secret_value(encrypted["coercedhub"]["flag"]) == "true"
    assert decrypt_secret_value(encrypted["coercedhub"]["off"]) == "false"
    assert decrypt_secret_value(encrypted["coercedhub"]["token"]) == "50"
    assert encrypted["coercedhub"]["flag"].startswith(SECRET_MARKER_PREFIX)

    # The live value is normalized in the same pass, before any worker
    # configuration is built from it: the settings overlay would otherwise
    # keep forwarding the coerced boolean and number to the worker while
    # the disk copy is ciphertext.
    decrypt_settings_in_place(live)
    assert live.coercedhub.flag == "true"
    assert live.coercedhub.off == "false"
    assert live.coercedhub.token == "50"


def test_a_manifest_declared_boolean_secret_field_keeps_its_toggle(
        tmp_path, monkeypatch, plugin_secret_paths):
    """A manifest may mark a boolean property secret. The stored False is
    the toggle's value, not a typed token: it is not flagged as plaintext,
    not stringified (a truthy "False" is not what anybody stored), not
    encrypted, and the live settings keep the boolean the plugin reads."""
    import json

    from secret_store.migration import has_plaintext_secrets_on_disk

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {
        "flaghub": {
            "provider_id": "flaghub",
            "state": "active",
            "active_version": "1.0.0",
            "manifest": {
                "provider_id": "flaghub",
                "config_schema": {"properties": {
                    "use_solver": {"type": "boolean", "secret": True}}},
            },
        },
    }}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    plugin_secret_paths.reset_plugin_secret_paths({"flaghub.use_solver"})
    live = _FakeSettings({"flaghub": {"use_solver": False, "region": "eu"}})

    # The manifest says the field is boolean, so the toggle is left alone at
    # rest and in memory; nothing forces a rewrite for it.
    assert has_plaintext_secrets_on_disk(live) is False
    snapshot = {"flaghub": dict(live.flaghub)}
    encrypted = encrypt_settings_dict(snapshot)
    assert encrypted["flaghub"]["use_solver"] is False
    decrypt_settings_in_place(live)
    assert live.flaghub.use_solver is False

    # Without the manifest behind the path, the same stored boolean is a
    # coerced token again and keeps the usual covering.
    state_file.write_text(json.dumps({"installations": {}}), encoding="utf-8")
    from secret_store import registry
    registry._BOOLEAN_SECRET_CACHE["key"] = None
    assert has_plaintext_secrets_on_disk(live) is True


def test_a_boolean_field_keeps_its_toggle_after_the_secret_flag_is_dropped(
        tmp_path, monkeypatch, plugin_secret_paths):
    """A version may mark a boolean field secret and a later one drop the
    flag while keeping the field boolean. The path the earlier version
    registered never leaves the persisted registry, and the toggle rule
    follows the active manifest's declared type alone: the stored False is
    still a toggle, not a typed token, so it is not flagged as plaintext,
    not stringified to the truthy text the worker would read as on, and
    not encrypted."""
    import json

    from secret_store.migration import has_plaintext_secrets_on_disk

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    # The path persisted when version 1 marked the field secret; version 2
    # is active now and declares the same field boolean without the flag.
    state_file.write_text(json.dumps({
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
                        "use_solver": {"type": "boolean"},
                        "region": {"type": "string"}}},
                },
            },
        },
    }), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    plugin_secret_paths.reset_plugin_secret_paths({"flaghub.use_solver"})
    live = _FakeSettings({"flaghub": {"use_solver": False, "region": "eu"}})

    assert has_plaintext_secrets_on_disk(live) is False
    snapshot = {"flaghub": dict(live.flaghub)}
    encrypted = encrypt_settings_dict(snapshot)
    assert encrypted["flaghub"]["use_solver"] is False
    decrypt_settings_in_place(live)
    assert live.flaghub.use_solver is False


def test_an_encryption_failure_never_returns_the_credential_plaintext(
        plugin_secret_paths):
    """The at-rest loops tolerate a malformed registry path, but never an
    encryption failure. A lone surrogate in a credential raises
    UnicodeEncodeError (a ValueError subclass) inside the encryption
    itself; swallowing it would hand the credential back in the returned
    payload as written, plaintext, and the save would put it on disk. The
    failure propagates instead, so the save refuses, for a scalar
    credential and for a list element alike."""
    # A malformed registry path cannot be walked and is still skipped: the
    # tolerance belongs to the lookup, and it keeps costing nothing.
    plugin_secret_paths.reset_plugin_secret_paths({"nodotpath"})
    snapshot = {
        "general": {"secrets_encryption_key": "test-master-key-not-secret-but-stable-for-tests"},
        "sonarr": {"apikey": "tt-sonarr"},
        "translator": {"gemini_keys": ["g1"]},
    }
    out = encrypt_settings_dict(snapshot)
    assert out["sonarr"]["apikey"].startswith(SECRET_MARKER_PREFIX)
    assert out["translator"]["gemini_keys"][0].startswith(SECRET_MARKER_PREFIX)

    # A scalar credential that cannot be encoded never comes back in the
    # clear: the whole encrypt pass raises, so write_config refuses the
    # save instead of persisting the plaintext.
    snapshot["sonarr"]["apikey"] = "tt-sonarr\ud800"
    with pytest.raises(UnicodeEncodeError):
        encrypt_settings_dict(snapshot)

    # The list loop refuses its plaintext element the same way.
    snapshot["sonarr"]["apikey"] = "tt-sonarr"
    snapshot["translator"]["gemini_keys"] = ["g1", "g2\ud800"]
    with pytest.raises(UnicodeEncodeError):
        encrypt_settings_dict(snapshot)
