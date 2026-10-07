# coding=utf-8
"""Tests for bazarr.secrets.registry classification."""

from secret_store.registry import (
    SYSTEM_SECRETS,
    USER_VISIBLE_SECRET_LISTS,
    USER_VISIBLE_SECRETS,
    is_system_secret,
    is_user_visible_secret,
    is_user_visible_secret_list,
)


def test_user_visible_includes_compat_token():
    """Per the user clarification - the compat endpoint TOKEN (the API
    token that gets pasted into VLSub / Jellyfin OS plugin) is visible
    on the frontend; only the compat endpoint's signing keys
    (jwt_secret, file_id_secret) stay backend-only."""
    assert is_user_visible_secret("compat_endpoint.token")
    assert is_system_secret("compat_endpoint.jwt_secret")
    assert is_system_secret("compat_endpoint.file_id_secret")


def test_user_visible_includes_arr_apikeys():
    for key in ("sonarr.apikey", "radarr.apikey", "jellyfin.apikey",
                "plex.apikey", "plex.token"):
        assert is_user_visible_secret(key), f"{key} should be user-visible"


def test_user_visible_includes_provider_passwords_and_keys():
    """Spot-check the provider list - if validators get added without
    updating the registry, those credentials would ship plaintext."""
    for key in ("opensubtitles.password", "addic7ed.password",
                "subdl.api_key", "subsource.apikey",
                "translator.openrouter_api_key", "translator.lingarr_token",
                "anticaptcha.anti_captcha_key", "captchaai.captchaai_key"):
        assert is_user_visible_secret(key), f"{key} should be user-visible"


def test_system_secrets_never_user_visible():
    """Disjoint sets - a secret cannot be both backend-only and visible."""
    overlap = USER_VISIBLE_SECRETS & SYSTEM_SECRETS
    assert not overlap, f"keys must not be in both tiers: {overlap}"
    overlap_lists = USER_VISIBLE_SECRET_LISTS & SYSTEM_SECRETS
    assert not overlap_lists, f"list keys must not be in SYSTEM: {overlap_lists}"


def test_tmdb_credential_has_a_disjoint_encrypted_write_only_tier():
    from secret_store import registry
    tier = getattr(registry, "WRITE_ONLY_SECRETS", frozenset())
    assert "discover.tmdb_access_token" in tier
    assert not tier & (USER_VISIBLE_SECRETS | SYSTEM_SECRETS | USER_VISIBLE_SECRET_LISTS)


def test_system_includes_master_and_signing_keys():
    """Every cryptographic primitive (anything used to sign / encrypt
    OTHER secrets) must be SYSTEM, never visible to the frontend."""
    for key in ("general.flask_secret_key",
                "general.secrets_encryption_key",
                "compat_endpoint.jwt_secret",
                "compat_endpoint.file_id_secret",
                "plex.encryption_key"):
        assert is_system_secret(key), f"{key} should be system-only"


def test_translator_openrouter_encryption_key_is_user_visible():
    """The Translator settings page exposes a 'Encryption Key (optional)'
    Password field for translator.openrouter_encryption_key. It's a
    user-managed credential, NOT a master key - so it's user-visible at
    rest and decrypted before the API returns it."""
    assert is_user_visible_secret("translator.openrouter_encryption_key")
    assert not is_system_secret("translator.openrouter_encryption_key")


def test_translator_gemini_keys_classified_as_list():
    """List-shaped credentials need a different code path (encrypt each
    element). Confirm they live in their own tier."""
    assert is_user_visible_secret_list("translator.gemini_keys")
    assert not is_user_visible_secret("translator.gemini_keys")


def test_provider_login_pairs_are_both_classified():
    """Every provider with a password / passkey / cookies field also has
    its username / email companion in the registry, so a leaked
    config.yaml does not expose which accounts the operator owns even
    when the password is the harder bit."""
    pairs = [
        ("opensubtitles.username", "opensubtitles.password"),
        ("opensubtitlescom.username", "opensubtitlescom.password"),
        ("addic7ed.username", "addic7ed.password"),
        ("legendasdivx.username", "legendasdivx.password"),
        ("legendasnet.username", "legendasnet.password"),
        ("deathbycaptcha.username", "deathbycaptcha.password"),
        ("napisy24.username", "napisy24.password"),
        ("titlovi.username", "titlovi.password"),
        ("titulky.username", "titulky.password"),
        ("karagarga.username", "karagarga.password"),
        ("ktuvit.email", "ktuvit.hashed_password"),
        ("hdbits.username", "hdbits.passkey"),
        ("proxy.username", "proxy.password"),
        ("postgresql.username", "postgresql.password"),
        ("auth.username", "auth.password"),
    ]
    for username_key, password_key in pairs:
        assert is_user_visible_secret(username_key), \
            f"{username_key} missing from USER_VISIBLE_SECRETS"
        assert is_user_visible_secret(password_key), \
            f"{password_key} missing from USER_VISIBLE_SECRETS"


def test_session_cookies_are_classified():
    """Session cookies are functionally long-lived auth tokens after the
    initial login - encrypt them at rest like passwords."""
    for key in ("addic7ed.cookies", "avistaz.cookies", "cinemaz.cookies",
                "turkcealtyaziorg.cookies"):
        assert is_user_visible_secret(key), f"{key} missing"


def test_karagarga_forum_password_is_classified():
    """karagarga has TWO passwords (main + forum). Both must be encrypted."""
    assert is_user_visible_secret("karagarga.password")
    assert is_user_visible_secret("karagarga.f_password")


def test_plex_oauth_pii_is_classified():
    """Plex OAuth populates plex.username + plex.email from the linked
    Plex account. Treat as PII and encrypt at rest."""
    assert is_user_visible_secret("plex.username")
    assert is_user_visible_secret("plex.email")


def test_unknown_keys_not_classified():
    """Defaults are 'no, this isn't a secret' - a brand-new setting must
    explicitly opt in or it stays plaintext (and the unit test for the
    next migration commit will catch that)."""
    assert not is_user_visible_secret("general.debug")
    assert not is_user_visible_secret("sonarr.url")
    assert not is_system_secret("general.port")
    assert not is_user_visible_secret_list("general.path_mappings")


# --- Provider Hub plugin secrets: dynamic registry membership -----------------
#
# A catalog plugin's secret fields are registered from the plugin's own
# schema, not from a hand-kept list in Bazarr, so a new plugin secret is
# covered without a code change. The paths live in state.json (persisted
# across removals) plus the installation rows, and the boot pass scans the
# settings sections key by key for leftovers of plugins that no longer have
# a row. Every value below is a synthetic test fixture, never a real token.

import json  # noqa: E402

import pytest  # noqa: E402


class _ScanSettings:
    """Stand-in for the live settings root. The boot loader only needs the
    section tree, shaped like dynaconf's as_dict()."""

    def __init__(self, sections=None):
        self._sections = {
            str(name).lower(): values for name, values in (sections or {}).items()
        }

    def as_dict(self):
        return self._sections


def _hub_manifest(provider_id="smokehub", secret_fields=(), secret_true_fields=(),
                  plain_fields=(), version="1.0.0"):
    properties = {}
    for field in secret_true_fields:
        properties[field] = {"type": "string", "secret": True}
    for field in plain_fields:
        properties.setdefault(field, {"type": "string"})
    return {
        "provider_id": provider_id,
        "name": provider_id,
        "version": version,
        "config_schema": {"type": "object", "properties": properties},
        "secret_fields": list(secret_fields),
    }


def _write_hub_state(tmp_path, state):
    path = tmp_path / "provider_hub" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _write_bundle_manifest(tmp_path, provider_id, manifest, commit="c" * 40):
    """A provider.json left under provider_hub/bundles/<id>/, the way a real
    install writes it: version and commit directories under the id. Returns
    the bundle directory, the path an installation row's active_path names."""
    root = (
        tmp_path / "provider_hub" / "bundles" / provider_id
        / str(manifest.get("version") or "1.0.0") / commit
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "provider.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


@pytest.fixture
def plugin_secret_registry():
    """Isolate the module-level dynamic set around each plugin-registry test."""
    from secret_store import registry

    saved = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    yield registry
    registry.reset_plugin_secret_paths(saved)


def test_installation_rows_register_secret_fields_and_secret_true(
        tmp_path, monkeypatch, plugin_secret_registry):
    """Every installation row registers its schema's secret fields, whatever
    the row's state, and a field marked secret: true alone counts too."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "staged",
                "pending_restart": True,
                "manifest": _hub_manifest(
                    "smokehub",
                    secret_fields=("api_token",),
                    secret_true_fields=("fallback_token",),
                    plain_fields=("profile_name",),
                ),
            },
            "ghostplugin": {
                "provider_id": "ghostplugin",
                "state": "removed",
                "pending_restart": True,
                "staged_manifest": _hub_manifest(
                    "ghostplugin", secret_fields=("session_token",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("smokehub.api_token")
    assert plugin_secret_registry.is_plugin_secret("smokehub.fallback_token")
    assert plugin_secret_registry.is_plugin_secret("ghostplugin.session_token")
    # A declared non-secret field of the same plugin is not a secret.
    assert not plugin_secret_registry.is_plugin_secret("smokehub.profile_name")


def test_persisted_paths_cover_plugins_without_installation_rows(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A removed plugin keeps its registrations: the persisted set survives
    the row deletion that removal performs at the next restart."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {},
        "plugin_secret_paths": ["ghostplugin.api_token"],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")


def test_leftover_plugin_keys_are_registered_key_by_key_beside_built_ins(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A catalog plugin that replaces a built-in shares that built-in's
    settings section, so one section holds both. The scan judges each
    section.key on its own: the plugin's keys are registered while the
    built-in's keys keep their built-in handling.

    The bundles declare no plain username, password or apikey field, so
    the only thing keeping the built-in's own keys clear is the settings
    validator guard: with that check deleted, every one of them would be
    registered by the last-resort rule and the test would fail."""
    _write_bundle_manifest(
        tmp_path, "legendasdivx",
        _hub_manifest("legendasdivx",
                      secret_fields=("captcha_response", "captcha_solver_token"),
                      plain_fields=("skip_wrong_fps",)))
    _write_bundle_manifest(
        tmp_path, "subsource",
        _hub_manifest("subsource", secret_fields=("api_key",)))
    state_path = _write_hub_state(tmp_path, {"installations": {}})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    settings = _ScanSettings({
        "legendasdivx": {
            "username": "unit-user",
            "password": "unit-pass",
            "skip_wrong_fps": False,
            "captcha_response": "unit-captcha-value",
            "captcha_solver_token": "unit-solver-value",
        },
        "subsource": {"apikey": "unit-builtin-key", "api_key": "unit-plugin-key"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    # The plugin's keys in the shared sections are covered.
    assert plugin_secret_registry.is_plugin_secret("legendasdivx.captcha_response")
    assert plugin_secret_registry.is_plugin_secret("legendasdivx.captcha_solver_token")
    assert plugin_secret_registry.is_plugin_secret("subsource.api_key")
    # The built-in's own keys never become write-only plugin secrets, by the
    # validator guard alone.
    assert not plugin_secret_registry.is_plugin_secret("legendasdivx.username")
    assert not plugin_secret_registry.is_plugin_secret("legendasdivx.password")
    assert not plugin_secret_registry.is_plugin_secret("legendasdivx.skip_wrong_fps")
    assert not plugin_secret_registry.is_plugin_secret("subsource.apikey")
    # The discovered paths join the persisted set in state.json.
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legendasdivx.captcha_response" in persisted
    assert "legendasdivx.captcha_solver_token" in persisted
    assert "subsource.api_key" in persisted
    assert "legendasdivx.username" not in persisted
    assert "subsource.apikey" not in persisted


def test_leftover_key_rules_per_key(tmp_path, monkeypatch, plugin_secret_registry):
    """Secret status of a leftover key comes from the manifests held for its
    id. Any held manifest declaring it secret registers it; only the bundle
    on disk of the version the installation row records as active, code
    this install actually ran, keeps it in the clear by declaring it a
    plain field; every other leftover key holding a non-empty string is
    registered by the last-resort rule, whatever manifests exist."""
    # A bundle that declares `region` as a plain field, for an id with no
    # installation row at all: no version can be shown to have run, so its
    # declaration is not evidence and the value stays covered.
    _write_bundle_manifest(
        tmp_path, "withfields", _hub_manifest("withfields", plain_fields=("region",)))
    # The earlier version's bundle declared old_token secret; the installed
    # version renamed it, so the installation schema no longer knows the key.
    _write_bundle_manifest(
        tmp_path, "renamedplugin",
        _hub_manifest("renamedplugin", secret_fields=("old_token",), version="1.0.0"))
    # A bundle on disk declares `solver_token` a plain field, while a held
    # catalog entry for the same id declares it secret. The held entry can
    # add coverage but never clear a key, so the value is registered.
    _write_bundle_manifest(
        tmp_path, "divergent", _hub_manifest("divergent", plain_fields=("solver_token",)))
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "renamedplugin": {
                "provider_id": "renamedplugin",
                "state": "active",
                "pending_restart": False,
                "manifest": _hub_manifest(
                    "renamedplugin", secret_fields=("new_token",), version="2.0.0"),
            },
        },
        "catalog_entries": {
            "held-entry": {
                "source": "official",
                "provider_id": "heldentry",
                "version": "1.0.0",
                "manifest": _hub_manifest("heldentry", plain_fields=("only_a_field",)),
            },
            "divergent-entry": {
                "source": "official",
                "provider_id": "divergent",
                "version": "1.0.0",
                "manifest": _hub_manifest("divergent", secret_fields=("solver_token",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    settings = _ScanSettings({
        # No manifest anywhere: every non-empty string key is covered,
        # including the captcha response and captcha solver token fields
        # an anti-captcha plugin would leave in this section.
        "ghostplugin": {"api_token": "unit-legacy-plaintext",
                        "captcha_response": "unit-legacy-captcha"},
        # Declared plain by a bundle no row vouches ever ran: covered.
        "withfields": {"region": "unit-region"},
        # Declared secret by the earlier version's bundle, renamed since.
        "renamedplugin": {"old_token": "unit-old-value", "new_token": "unit-new-value"},
        # A held catalog entry declaring it a plain field never clears a key.
        "heldentry": {"only_a_field": "unit-held-value"},
        # The bundle says plain, the held catalog entry says secret: secret
        # wins, because a bundle can only keep clear what it declares plain
        # while a held entry can always add coverage.
        "divergent": {"solver_token": "unit-divergent-value"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")
    assert plugin_secret_registry.is_plugin_secret("ghostplugin.captcha_response")
    assert plugin_secret_registry.is_plugin_secret("withfields.region")
    assert plugin_secret_registry.is_plugin_secret("renamedplugin.old_token")
    assert plugin_secret_registry.is_plugin_secret("renamedplugin.new_token")
    assert plugin_secret_registry.is_plugin_secret("heldentry.only_a_field")
    assert plugin_secret_registry.is_plugin_secret("divergent.solver_token")


def test_the_scan_covers_numeric_leftovers_and_knows_a_bool_is_not_one(
        tmp_path, monkeypatch, plugin_secret_registry):
    """An older save coerced a digits-only typed token into a number, so a
    leftover number is registered like a leftover string, with or without a
    declaring catalog entry. A boolean is not: isinstance(True, int) is
    True, so the number rule alone would take it, but a leftover boolean is
    a toggle until a held manifest declares that exact field secret."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {},
        "catalog_entries": {
            "declared-entry": {
                "source": "official",
                "provider_id": "declaredhub",
                "version": "1.0.0",
                "manifest": _hub_manifest("declaredhub", secret_fields=("numeric_token",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    settings = _ScanSettings({
        # Removed with a declaring catalog entry: a numeric token is
        # registered and covered exactly like a string one.
        "declaredhub": {"numeric_token": 12345678901234567},
        # No manifest anywhere: the last-resort rule covers numbers like
        # strings, but never a boolean.
        "ghosthub": {"numeric_token": 12345678901234567,
                     "float_token": 1.25,
                     "use_solver": True},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("declaredhub.numeric_token")
    assert plugin_secret_registry.is_plugin_secret("ghosthub.numeric_token")
    assert plugin_secret_registry.is_plugin_secret("ghosthub.float_token")
    assert not plugin_secret_registry.is_plugin_secret("ghosthub.use_solver")

    # The boolean is registered when a held manifest declares that exact
    # field secret: the manifest is authoritative about its own fields.
    declared_state = _write_hub_state(tmp_path, {
        "installations": {},
        "catalog_entries": {
            "declared-entry": {
                "source": "official",
                "provider_id": "flaghub",
                "version": "1.0.0",
                "manifest": _hub_manifest("flaghub", secret_fields=("use_solver",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(declared_state))
    plugin_secret_registry.load_plugin_secret_paths(
        _ScanSettings({"flaghub": {"use_solver": True}}))
    assert plugin_secret_registry.is_plugin_secret("flaghub.use_solver")


def test_a_malformed_installations_map_does_not_abort_registration(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A corrupt installations value (a list where the map belongs) is read
    as empty rather than raised out of the boot loader: the persisted paths
    of removed plugins, the leftover scan and the backstop still register,
    because an abort would leave every plugin ciphertext live."""
    state_path = _write_hub_state(tmp_path, {
        "installations": ["not", "a", "mapping"],
        "plugin_secret_paths": ["ghostplugin.api_token"],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    settings = _ScanSettings({"ghostplugin": {"api_token": "unit-leftover-token"}})

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")


def test_the_backstop_survives_a_broken_manifest_module(
        tmp_path, monkeypatch, plugin_secret_registry):
    """The backstop's reserved-section import sits inside its try and its
    call is guarded: the import fails in exactly the situations the
    backstop exists for (the same broken module that stopped the scan),
    and raising there would drop the durable registrations the loader
    already made. With the manifest module unimportable, every marked key
    in a non-reserved section is still covered."""
    import sys

    from secret_store.crypto import encrypt_secret

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "active",
                "manifest": _hub_manifest("smokehub", secret_fields=("api_token",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    # The manifest module cannot be imported: both the scan and the
    # backstop have to live without it.
    monkeypatch.setitem(sys.modules, "provider_hub.manifest", None)

    settings = _ScanSettings({
        "bootvault": {"api_token": encrypt_secret("unit-stored-token")},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("smokehub.api_token")
    assert plugin_secret_registry.is_plugin_secret("bootvault.api_token")


def test_a_broken_manifest_module_logs_the_scan_skip_not_a_name_error(
        tmp_path, monkeypatch, plugin_secret_registry, caplog):
    """The scan's reserved-section import failing is its own refusal to
    run, and the intended warning names what broke. When the import line
    itself failed, the except tuple used to name a manifest exception
    unbound in that scope, so the failure surfaced as an UnboundLocalError
    the loader caught with a misleading log line instead."""
    import logging
    import sys

    state_path = _write_hub_state(tmp_path, {"installations": {}})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    monkeypatch.setitem(sys.modules, "provider_hub.manifest", None)

    with caplog.at_level(logging.WARNING, logger="secret_store.registry"):
        plugin_secret_registry.load_plugin_secret_paths(
            _ScanSettings({"ghostplugin": {"api_token": "unit-leftover-token"}}))

    assert "Skipping the leftover plugin secret scan" in caplog.text
    assert "UnboundLocalError" not in caplog.text


def test_historical_config_fields_are_discovered_and_persisted(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A field a schema dropped before this upgrade, whose value survives
    in the installation config on an install that never had the registry,
    is discovered from the config itself: registered at boot, persisted in
    state.json, and encrypted by the migration. Without the discovery
    neither the current manifest (v2, region only) nor the empty persisted
    set names the credential, and the installation API would hand it out.

    The declared plain field is left alone: coverage runs on what no
    manifest knows, not on the whole config."""
    from secret_store.crypto import is_encrypted

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub", plain_fields=("region",)),
                "config": {"legacy_token": "unit-legacy-secret", "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("legacyhub.legacy_token")
    assert not plugin_secret_registry.is_plugin_secret("legacyhub.region")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.legacy_token" in persisted
    assert "legacyhub.region" not in persisted

    plugin_secret_registry.migrate_installation_config_secrets()
    config = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert is_encrypted(config["legacy_token"])
    assert config["region"] == "eu"


def test_bazarr_owned_sections_are_never_scanned(
        tmp_path, monkeypatch, plugin_secret_registry):
    """Keys of Bazarr's own settings sections are never treated as plugin
    secrets, so the scan cannot make a Bazarr credential write-only. The
    guard is exercised by a key no validator defines: with the reserved
    check deleted, the last-resort rule would register it."""
    state_path = _write_hub_state(tmp_path, {"installations": {}})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    settings = _ScanSettings({
        "sonarr": {"apikey": "unit-sonarr-key", "ip": "unit-host",
                   "an_undefined_extra": "unit-not-a-validator-key"},
        "auth": {"password": "unit-admin-pass"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert not plugin_secret_registry.is_plugin_secret("sonarr.apikey")
    assert not plugin_secret_registry.is_plugin_secret("sonarr.ip")
    assert not plugin_secret_registry.is_plugin_secret("auth.password")
    assert not plugin_secret_registry.is_plugin_secret("sonarr.an_undefined_extra")


def test_a_scan_failure_does_not_discard_the_durable_registrations(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A boot loader failure must fail toward coverage, never toward the
    clear: an exception in the leftover scan (an unreadable bundle, an
    import cycle) leaves the persisted paths and the installation rows
    registered, so the boot decrypt pass still walks them and no plugin
    ciphertext is left live in the settings."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "active",
                "manifest": _hub_manifest("smokehub", secret_fields=("api_token",)),
            },
        },
        "plugin_secret_paths": ["ghostplugin.session_token"],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    def broken_scan(*_args, **_kwargs):
        raise OSError("the scan broke")

    monkeypatch.setattr(plugin_secret_registry, "_scan_leftover_plugin_keys", broken_scan)

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("ghostplugin.session_token")
    assert plugin_secret_registry.is_plugin_secret("smokehub.api_token")


def test_an_unreadable_state_still_registers_encrypted_leftovers(
        tmp_path, monkeypatch, plugin_secret_registry):
    """The no-state backstop: when Provider Hub's state cannot be read at
    all, every key holding the encryption marker in a non-reserved section
    is still registered, so the boot decrypt pass decrypts it instead of
    leaving the ciphertext live in the settings. A Bazarr validator's key
    is never registered, whatever it holds."""
    import sys

    from secret_store.crypto import encrypt_secret

    # The loader's state import fails: state.json is unreadable.
    monkeypatch.setitem(sys.modules, "provider_hub.state", None)

    settings = _ScanSettings({
        "bootvault": {"api_token": encrypt_secret("unit-stored-token"),
                      "region": "unit-plain-region"},
        "sonarr": {"apikey": "unit-sonarr-key"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("bootvault.api_token")
    # A plaintext non-secret field in the same section is not the backstop's
    # business: it covers what is encrypted, nothing else. The value is
    # covered anyway, by the fallback that now runs without the Hub
    # directory too, conservatively, the same rule it applies when the
    # scan broke with the directory in place.
    assert plugin_secret_registry.is_plugin_secret("bootvault.region")
    # A validator-defined key stays out even if it somehow carries a marker.
    assert not plugin_secret_registry.is_plugin_secret("sonarr.apikey")


def test_boot_migration_encrypts_plaintext_installation_credentials(
        tmp_path, monkeypatch, plugin_secret_registry):
    """Plugin credentials an earlier version stored in the clear inside
    Provider Hub's state.json are encrypted at boot, after the boot settings
    write put the master key on disk: the registry loader registers the
    paths first and app.config's boot calls the encryption step, so an
    atomic boot migration covers them the same way the config.yaml rewrite
    covers the settings, keeping every other value as it is."""
    from secret_store.crypto import is_encrypted

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub",
                                          secret_fields=("api_token",),
                                          secret_true_fields=("fallback_token",),
                                          plain_fields=("region",)),
                "config": {"api_token": "unit-legacy-token", "fallback_token": "unit-fallback",
                           "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    plugin_secret_registry.migrate_installation_config_secrets()

    config = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert is_encrypted(config["api_token"])
    assert is_encrypted(config["fallback_token"])
    # The plain field keeps its value.
    assert config["region"] == "eu"


def test_boot_migration_covers_a_legacy_numeric_credential(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A typed token an older save coerced into a number inside an
    installation config is stored as the text it was: encrypted at rest
    and restored as text, not left as a bare number that neither encrypts
    nor masks."""
    from secret_store.crypto import decrypt_secret, is_encrypted

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub",
                                          secret_fields=("api_token",),
                                          plain_fields=("region",)),
                "config": {"api_token": 12345678901234567, "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    plugin_secret_registry.migrate_installation_config_secrets()

    config = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert is_encrypted(config["api_token"])
    assert decrypt_secret(config["api_token"]) == "12345678901234567"
    assert config["region"] == "eu"


def test_boot_migration_covers_a_legacy_boolean_credential(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A legacy Hub credential an older save coerced into a boolean
    (api_key true under a string-secret schema) is migrated like the
    coerced numbers are: normalized to the lowercase text and encrypted,
    so it stops sitting in the clear in state.json and in the config the
    installation API hands out. A boolean the manifest declares for the
    secret field is a stored toggle, not a credential, and keeps its
    value."""
    from secret_store.crypto import decrypt_secret, is_encrypted

    manifest = _hub_manifest("legacyhub", secret_fields=("api_key",),
                             plain_fields=("region",))
    manifest["config_schema"]["properties"]["use_solver"] = {
        "type": "boolean", "secret": True}
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": manifest,
                "config": {"api_key": True, "use_solver": True, "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    plugin_secret_registry.migrate_installation_config_secrets()

    config = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert is_encrypted(config["api_key"])
    assert decrypt_secret(config["api_key"]) == "true"
    # The manifest-declared boolean secret field keeps its toggle, and the
    # plain field keeps its value.
    assert config["use_solver"] is True
    assert config["region"] == "eu"


def test_boot_migration_canonicalizes_a_mixed_case_credential(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A credential an older version persisted under a mixed-case alias of
    a declared secret field (API_KEY for api_key) is invisible to a
    migration that reads canonical names only: it would survive the boot
    in the clear, ride the installation API out as it stood and outlive
    even a canonical replacement. The migration canonicalizes it instead:
    the alias moves under the canonical name and encrypts, an alias beside
    a canonical value drops without costing the canonical writer's value,
    whatever order the stored config holds them in."""
    from secret_store.crypto import decrypt_secret, encrypt_secret, is_encrypted

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub", secret_fields=("api_key",)),
                "config": {"API_KEY": "unit-legacy-token"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    plugin_secret_registry.migrate_installation_config_secrets()

    stored = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert "API_KEY" not in stored
    assert is_encrypted(stored["api_key"])
    assert decrypt_secret(stored["api_key"]) == "unit-legacy-token"

    # A collision resolves to the canonical writer's value, never the
    # alias's, in either stored order.
    for stored_config in ({"API_KEY": "unit-alias-left", "api_key": encrypt_secret("unit-canonical")},
                         {"api_key": encrypt_secret("unit-canonical"), "API_KEY": "unit-alias-left"}):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["installations"]["legacyhub"]["config"] = dict(stored_config)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
        plugin_secret_registry.migrate_installation_config_secrets()
        stored = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
        assert set(stored) == {"api_key"}
        assert decrypt_secret(stored["api_key"]) == "unit-canonical"


def test_boot_migration_fails_closed_on_encryption_errors(
        tmp_path, monkeypatch, plugin_secret_registry):
    """An encryption failure leaves state.json exactly as it was (no
    partial rewrite, the values stay covered, to be rewritten the moment
    the key works) and stops the boot: startup does not complete while
    plaintext Hub credentials remain readable in the file."""
    from secret_store.registry import HubSecretMigrationError

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub", secret_fields=("api_token",)),
                "config": {"api_token": "unit-legacy-token", "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    def broken_encrypt(value, master_key=None):
        raise RuntimeError("key unavailable")

    monkeypatch.setattr("secret_store.crypto.encrypt_secret", broken_encrypt)
    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    with pytest.raises(HubSecretMigrationError):
        plugin_secret_registry.migrate_installation_config_secrets()

    stored = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert stored == {"api_token": "unit-legacy-token", "region": "eu"}


def test_boot_migration_waits_for_a_durable_master_key(
        tmp_path, monkeypatch, plugin_secret_registry):
    """The Hub credentials encrypt only under a master key that is durably
    saved. A settings write that fails leaves the key in memory only, and
    a restart would mint a different one: encrypting then would store
    ciphertext no boot can ever open. The migration refuses, nothing is
    encrypted, and the boot does not complete while the plaintext values
    remain."""
    from secret_store import crypto
    from secret_store.registry import HubSecretMigrationError

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub", secret_fields=("api_token",),
                                          plain_fields=("region",)),
                "config": {"api_token": "unit-legacy-token", "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    # A master key that exists only in this process's memory: the boot has
    # not written it to disk, and the settings write that would keeps
    # failing.
    monkeypatch.setattr(crypto, "_unpersisted_master_keys", set())
    original_get_master_key = crypto.get_master_key

    def memory_only_key(settings_obj=None):
        key = original_get_master_key(settings_obj)
        crypto._unpersisted_master_keys.add(key)
        return key

    monkeypatch.setattr(crypto, "get_master_key", memory_only_key)
    monkeypatch.setattr("app.config.write_config", lambda: False)

    with pytest.raises(HubSecretMigrationError):
        plugin_secret_registry.migrate_installation_config_secrets()

    # Nothing was encrypted and nothing was rewritten: the plaintext values
    # stand exactly as they were, not ciphertext under a lost key.
    stored = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert stored == {"api_token": "unit-legacy-token", "region": "eu"}

    # The control: with the settings write landing, the key is durable and
    # the same migration encrypts.
    monkeypatch.setattr("app.config.write_config", lambda: True)
    plugin_secret_registry.migrate_installation_config_secrets()
    from secret_store.crypto import is_encrypted
    stored = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert is_encrypted(stored["api_token"])
    assert stored["region"] == "eu"


def test_boot_migration_refuses_when_the_state_module_cannot_be_imported(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A boot whose Provider Hub state module cannot be imported cannot
    enumerate the installation credentials either, so it cannot show them
    to be encrypted. Returning there would let startup complete while
    plaintext credentials remain readable in state.json, so the migration
    raises instead and the boot refuses to complete, the refused call
    rewriting nothing."""
    import sys

    from secret_store.registry import HubSecretMigrationError

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub", secret_fields=("api_token",),
                                          plain_fields=("region",)),
                "config": {"api_token": "unit-legacy-token", "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    # Whatever the boot pass already covered while the state module still
    # imported is beside the point here; the refused call below must be
    # the only thing that could rewrite the file.
    before = state_path.read_text(encoding="utf-8")

    # The state module cannot be imported: nothing can enumerate or
    # migrate the installation credentials on this boot.
    monkeypatch.setitem(sys.modules, "provider_hub.state", None)

    with pytest.raises(HubSecretMigrationError):
        plugin_secret_registry.migrate_installation_config_secrets()

    # Nothing was rewritten: the file keeps exactly what it had.
    assert state_path.read_text(encoding="utf-8") == before


def test_boot_migration_refuses_a_collection_left_at_a_secret_path(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A list or a dictionary an earlier Hub write left at a registered
    secret path can be neither encrypted at rest nor covered while the
    worker config is built, and the runtime decryption hands a non-string
    back unchanged, so the boot migration refuses instead of completing and
    passing the collection to the provider as plaintext. The state keeps
    exactly what it had; an empty collection is not a credential, the
    string rule's empty stays empty, so it is not a refusal."""
    from secret_store.registry import HubSecretMigrationError

    # The loader's leftover scan reads Bazarr's own settings validators,
    # whose module import runs the boot secret pass against whatever state
    # the environment names. Prime the import before the poisoned state
    # exists, so the loader below judges that state itself instead of the
    # import's bootstrap refusing on it first.
    import app.config  # noqa: F401

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "manifest": _hub_manifest("legacyhub",
                                          secret_true_fields=("api_token",),
                                          plain_fields=("region",)),
                "config": {"api_token": ["unit-legacy-token-a", "unit-legacy-token-b"],
                           "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    with pytest.raises(HubSecretMigrationError):
        plugin_secret_registry.migrate_installation_config_secrets()

    # No partial rewrite: the state keeps exactly what it had, and the
    # collection is still what the worker config builder would hand out,
    # which is what the boot refusal has to stand between.
    row = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]
    config = row["config"]
    assert config["api_token"] == ["unit-legacy-token-a", "unit-legacy-token-b"]
    assert config["region"] == "eu"
    from provider_hub.service import _effective_installation_config
    effective, _ = _effective_installation_config(
        {"provider_id": "legacyhub", "manifest": row["manifest"], "config": config},
        {"installations": {}})
    assert effective["api_token"] == ["unit-legacy-token-a", "unit-legacy-token-b"]

    # An empty collection is not a credential: the migration passes over it
    # and rewrites nothing.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["installations"]["legacyhub"]["config"] = {"api_token": [], "region": "eu"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    plugin_secret_registry.migrate_installation_config_secrets()
    stored = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert stored["api_token"] == []
    assert stored["region"] == "eu"


def test_at_rest_paths_union_static_tiers_with_plugin_paths(plugin_secret_registry):
    """Every at-rest consumer (boot decrypt, encrypt-on-write, plaintext
    detection) walks the static tiers plus the dynamic plugin set."""
    plugin_secret_registry.reset_plugin_secret_paths({"smokehub.api_token"})

    paths = plugin_secret_registry.at_rest_secret_paths()

    assert "sonarr.apikey" in paths          # static user-visible tier
    assert "discover.tmdb_access_token" in paths  # static write-only tier
    assert "smokehub.api_token" in paths     # dynamic plugin tier
    assert "legendasdivx.captcha_response" not in paths


def test_persisted_paths_are_validated_before_registration(
        tmp_path, monkeypatch, plugin_secret_registry):
    """Only a list of `provider_id.field` strings matching the id and
    config-key shapes is durable. A non-list value is skipped whole (reading
    a bare string would register its characters one at a time, and a
    character is not a `section.key` any at-rest loop can split), and the
    malformed entries of a list are logged and skipped without costing the
    valid ones."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "active",
                "manifest": _hub_manifest("smokehub", secret_fields=("api_token",)),
            },
        },
        "plugin_secret_paths": ["smokehub.fallback_token", "bogus", "nodot",
                                42, ["nested"], "UPPERCASE.API_TOKEN"],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("smokehub.fallback_token")
    # The installation row's secrets still register behind the junk.
    assert plugin_secret_registry.is_plugin_secret("smokehub.api_token")
    # A malformed entry never reaches the registry, in any shape.
    assert not plugin_secret_registry.is_plugin_secret("bogus")
    assert not plugin_secret_registry.is_plugin_secret("nodot")
    assert not plugin_secret_registry.is_plugin_secret("42")
    assert not any(path in ("b", "o", "g", "u", "s")
                   for path in plugin_secret_registry.plugin_secret_paths())
    # The valid entries keep their normalized form; the rest is not kept.
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "smokehub.fallback_token" in persisted
    assert "bogus" not in persisted and "nodot" not in persisted


def test_an_un_enumerable_persisted_value_still_registers_the_rows(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A persisted set that cannot be iterated (a number where the list
    belongs) must not cost the registration of the installation rows' own
    secrets, nor the loader's own run: the loader validates the shape
    before it iterates."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "active",
                "manifest": _hub_manifest("smokehub", secret_fields=("api_token",)),
            },
        },
        "plugin_secret_paths": 42,
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("smokehub.api_token")
    assert plugin_secret_registry.plugin_secret_paths() == {"smokehub.api_token"}


def test_migration_fails_closed_when_it_cannot_enumerate(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A migration that cannot safely read the installation credentials
    raises instead of returning: the boot does not complete while plaintext
    credentials may remain readable in state.json. A persisted set it
    cannot iterate is validated away first, so a malformed value alone
    never reaches the failure; the enumeration itself has to break."""
    from secret_store.registry import HubSecretMigrationError

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "active",
                "manifest": _hub_manifest("smokehub", secret_fields=("api_token",)),
                "config": {"api_token": "unit-legacy-token"},
            },
        },
        # A value that cannot be iterated: skipped, not a failure.
        "plugin_secret_paths": "bogus",
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())
    plugin_secret_registry.migrate_installation_config_secrets()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    from secret_store.crypto import is_encrypted
    assert is_encrypted(stored["installations"]["smokehub"]["config"]["api_token"])

    # The enumeration itself breaking is the failure: the migration raises,
    # the file keeps exactly what it had, and the boot does not complete.
    class _Unenumerable(dict):
        def items(self):
            raise RuntimeError("cannot enumerate")

    with pytest.raises(HubSecretMigrationError):
        plugin_secret_registry.migrate_installation_config_secrets({"installations": _Unenumerable()})


def test_a_failed_scan_conservatively_registers_plaintext_leftovers(
        tmp_path, monkeypatch, plugin_secret_registry):
    """The scan broke, so its finds would stay in the clear for the whole
    boot. The fallback applies the scan's own rule without the manifests:
    credential-shaped values in non-reserved sections at keys no validator
    defines are registered and persisted, so the boot pass encrypts them
    and the settings API redacts them exactly as the scan would have."""
    state_path = _write_hub_state(tmp_path, {"installations": {}})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    def broken_scan(*_args, **_kwargs):
        raise OSError("the scan broke")

    monkeypatch.setattr(plugin_secret_registry, "_scan_leftover_plugin_keys", broken_scan)

    settings = _ScanSettings({
        "ghostplugin": {"api_token": "unit-leftover-token", "region": "unit-region"},
        # A Bazarr section is never a leftover, even when the scan is gone:
        # the reserved list and the validators still vouch for it.
        "sonarr": {"apikey": "unit-sonarr-key"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")
    # The last-resort over-match is the same as the scan's: a non-empty
    # plain value in a plugin section is covered too.
    assert plugin_secret_registry.is_plugin_secret("ghostplugin.region")
    assert not plugin_secret_registry.is_plugin_secret("sonarr.apikey")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "ghostplugin.api_token" in persisted
    assert "sonarr.apikey" not in persisted
    assert plugin_secret_registry.hidden_leftover_paths() == frozenset()


def test_a_failed_scan_preserves_a_declared_plain_field_and_still_registers_the_leftover(
        tmp_path, monkeypatch, plugin_secret_registry):
    """The fallback cannot read the bundle manifests, but the state it is
    handed still holds the running installation's declarations: a field
    the active manifest declares, like region, keeps its plain handling
    when the scan fails, instead of being persisted as a secret path and
    encrypted into a permanently write-only field. The leftover credential
    beside it in the same section is still registered, so the failed scan
    leaves no plaintext find in the clear."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "active_version": "1.0.0",
                "manifest": _hub_manifest("legacyhub", plain_fields=("region",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    def broken_scan(*_args, **_kwargs):
        raise OSError("the scan broke")

    monkeypatch.setattr(plugin_secret_registry, "_scan_leftover_plugin_keys", broken_scan)

    settings = _ScanSettings({
        "legacyhub": {"api_token": "unit-leftover-token", "region": "eu"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    # The leftover credential is covered; the declared plain field is not.
    assert plugin_secret_registry.is_plugin_secret("legacyhub.api_token")
    assert not plugin_secret_registry.is_plugin_secret("legacyhub.region")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.api_token" in persisted
    assert "legacyhub.region" not in persisted


def test_a_failed_scan_without_the_reserved_list_withholds_the_values(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A broken manifest module takes the reserved list with it, so the
    fallback cannot register safely and withholds the candidate values from
    the settings API for this boot instead: a plaintext leftover first found
    now is never handed out, and the next boot with the module working
    covers it for real. A section the validators vouch for keeps its normal
    handling, and an encrypted value is the backstop's business, not the
    hide set's."""
    import sys

    from secret_store.crypto import encrypt_secret

    state_path = _write_hub_state(tmp_path, {"installations": {}})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    monkeypatch.setitem(sys.modules, "provider_hub.manifest", None)

    settings = _ScanSettings({
        "ghostplugin": {"api_token": "unit-leftover-token", "region": "unit-region"},
        "encryptedvault": {"api_token": encrypt_secret("unit-stored-token")},
        "sonarr": {"apikey": "unit-sonarr-key"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    # Nothing is registered on a guess, and nothing plaintext in a section
    # the validators do not vouch for is handed out.
    assert not plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")
    assert plugin_secret_registry.hidden_leftover_paths() == frozenset({
        "ghostplugin.api_token", "ghostplugin.region"})
    # An already-encrypted value is the backstop's business, so the hide set
    # leaves it to the registration the backstop still makes.
    assert plugin_secret_registry.is_plugin_secret("encryptedvault.api_token")
    assert "encryptedvault.api_token" not in plugin_secret_registry.hidden_leftover_paths()
    # A Bazarr section keeps its normal handling either way.
    assert "sonarr.apikey" not in plugin_secret_registry.hidden_leftover_paths()


def test_a_staged_manifest_cannot_suppress_historical_discovery(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A pre-registry upgrade with an active version: the active manifest
    omits the legacy field and a staged version redeclares it as plain, so
    neither manifest declares it secret. The value in the installation
    config is a credential the active code still holds, so the discovery
    compares against the active manifest alone, registers it, persists it
    and the migration encrypts it; the staged schema does not get a say
    until the restart makes it the active one."""
    from secret_store.crypto import is_encrypted

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "active_version": "2.0.0",
                "manifest": _hub_manifest("legacyhub", plain_fields=("region",), version="2.0.0"),
                "staged_manifest": _hub_manifest(
                    "legacyhub", plain_fields=("region", "legacy_token"), version="3.0.0"),
                "config": {"legacy_token": "unit-legacy-secret", "region": "eu"},
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    plugin_secret_registry.load_plugin_secret_paths(_ScanSettings())

    assert plugin_secret_registry.is_plugin_secret("legacyhub.legacy_token")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.legacy_token" in persisted

    plugin_secret_registry.migrate_installation_config_secrets()
    config = json.loads(state_path.read_text(encoding="utf-8"))["installations"]["legacyhub"]["config"]
    assert is_encrypted(config["legacy_token"])
    assert config["region"] == "eu"


def test_a_staged_schema_cannot_keep_a_retained_leftover_key_clear(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A running installation's active schema omits the key a retained
    credential sits at in the settings section, and the staged version
    redeclares it plain, on the row and in its bundle on disk. The staged
    code never ran, so neither its declarations nor its bundle keep the
    key out of the leftover scan: the credential is registered, persisted
    and covered at rest by the boot pass, instead of left plaintext in
    the section and readable through the settings API."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "active_version": "2.0.0",
                "staged_version": "3.0.0",
                "manifest": _hub_manifest("legacyhub", plain_fields=("region",), version="2.0.0"),
                "staged_manifest": _hub_manifest(
                    "legacyhub", plain_fields=("region", "legacy_token"), version="3.0.0"),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    # The staged version's bundle on disk, declaring the retained key plain.
    _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub", plain_fields=("legacy_token",), version="3.0.0"))

    settings = _ScanSettings({
        "legacyhub": {"legacy_token": "unit-retained-secret", "region": "eu"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("legacyhub.legacy_token")
    assert "legacyhub.legacy_token" in plugin_secret_registry.at_rest_secret_paths()
    assert not plugin_secret_registry.is_plugin_secret("legacyhub.region")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.legacy_token" in persisted
    assert "legacyhub.region" not in persisted


def test_a_failed_updates_bundle_cannot_keep_a_retained_key_clear(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A failed update clears the staged version but leaves the bundle it
    staged on disk, so the row no longer names the version whose manifest
    declares the retained key plain. That declaration is not evidence the
    key is a plain field, because the version's code never ran: the
    credential in the settings section is registered, persisted and
    covered at rest by the boot pass, instead of left plaintext and
    readable through the settings API. The active bundle the row's
    active_path names keeps its plain fields clear: that one did run, and
    the row says so by naming its directory."""
    # The running version's bundle, declaring `region` a plain field.
    active_bundle = _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub", plain_fields=("region",), version="2.0.0"))
    # The failed update's bundle: staged on disk before the dependency
    # installation failed, its code never ran, and the failure cleared the
    # staged version so nothing on the row names it. It renames the
    # credential and declares the retained key plain.
    _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub",
                      plain_fields=("region", "legacy_token"), version="3.0.0"))

    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "active_version": "2.0.0",
                "active_path": str(active_bundle),
                "manifest": _hub_manifest("legacyhub", version="2.0.0"),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    # The running version's bundle, declaring `region` a plain field.
    _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub", plain_fields=("region",), version="2.0.0"))
    # The failed update's bundle: staged on disk before the dependency
    # installation failed, its code never ran, and the failure cleared the
    # staged version so nothing on the row names it. It renames the
    # credential and declares the retained key plain.
    _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub",
                      plain_fields=("region", "legacy_token"), version="3.0.0"))

    settings = _ScanSettings({
        "legacyhub": {"legacy_token": "unit-retained-secret", "region": "eu"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("legacyhub.legacy_token")
    assert "legacyhub.legacy_token" in plugin_secret_registry.at_rest_secret_paths()
    # The active version's own plain declaration still counts: the key it
    # names in the section stays clear.
    assert not plugin_secret_registry.is_plugin_secret("legacyhub.region")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.legacy_token" in persisted
    assert "legacyhub.region" not in persisted


def test_a_same_version_reinstall_bundle_cannot_keep_a_key_clear(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A failed reinstall of the same version from another commit leaves a
    never-run bundle whose provider.json declares the very version the row
    records as active, so a version match alone is no evidence a bundle's
    code ran. The plain-field exception belongs to the one bundle whose
    directory the row's active_path names, where the running code was
    loaded from; a row without an active_path grants no exception, which
    errs toward registering, and a provider.json nested inside a shipped
    bundle never matches either, whatever it declares."""
    active_bundle = _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub", plain_fields=("region",), version="2.0.0"),
        commit="a" * 40)
    # The failed same-version reinstall's bundle: same version, another
    # commit, and its code never ran. It declares the retained key plain.
    _write_bundle_manifest(
        tmp_path, "legacyhub",
        _hub_manifest("legacyhub", plain_fields=("region", "legacy_token"),
                      version="2.0.0"),
        commit="b" * 40)
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "active",
                "active_version": "2.0.0",
                "active_path": str(active_bundle),
                "staged_version": None,
                "last_error": "dependency install failed",
                "manifest": _hub_manifest(
                    "legacyhub", plain_fields=("region",), version="2.0.0"),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    # A provider.json shipped nested inside the active bundle's own tree:
    # rglob finds it, and the active_path rule never grants it the
    # exception, so a shipped manifest cannot keep a key clear either.
    nested = active_bundle / "shipped" / "provider.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text(json.dumps(
        _hub_manifest("legacyhub", plain_fields=("region", "legacy_token"),
                      version="2.0.0")), encoding="utf-8")

    settings = _ScanSettings({
        "legacyhub": {"legacy_token": "unit-retained-secret", "region": "eu"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("legacyhub.legacy_token")
    assert "legacyhub.legacy_token" in plugin_secret_registry.at_rest_secret_paths()
    # The active bundle's own plain declaration still counts: the key it
    # names in the section stays clear.
    assert not plugin_secret_registry.is_plugin_secret("legacyhub.region")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.legacy_token" in persisted
    assert "legacyhub.region" not in persisted


def test_a_failed_fresh_install_row_declares_nothing(
        tmp_path, monkeypatch, plugin_secret_registry):
    """A failed fresh install leaves a row whose manifest is the validated
    manifest of a version whose code never ran: the install failed before
    any of it executed. Its declarations are no evidence about the section,
    so they suppress nothing: a row without an active version declares only
    while a staged install is pending, and a failed row declares nothing.
    The keys its never-run schema calls plain fields fall to the scan's own
    rules, so the plaintext credential beside them is registered, persisted
    and covered at rest, and the plain field itself joins the deliberate
    over-match rather than keeping the failed schema's word."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "legacyhub": {
                "provider_id": "legacyhub",
                "state": "failed",
                "active_version": None,
                "staged_version": None,
                "active_path": None,
                "staged_path": None,
                "pending_restart": False,
                "last_error": "dependency install failed",
                "manifest": _hub_manifest(
                    "legacyhub", plain_fields=("region", "legacy_token"),
                    version="3.0.0"),
            },
            "stagedhub": {
                "provider_id": "stagedhub",
                "state": "staged",
                "active_version": None,
                "staged_version": "1.0.0",
                "pending_restart": True,
                "manifest": _hub_manifest(
                    "stagedhub", plain_fields=("profile",), version="1.0.0"),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    settings = _ScanSettings({
        "legacyhub": {"legacy_token": "unit-retained-secret", "region": "eu"},
        "stagedhub": {"profile": "unit-profile"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    # The failed row's plain declaration is no evidence: the retained key
    # registers, and so does the field the never-run schema called plain.
    assert plugin_secret_registry.is_plugin_secret("legacyhub.legacy_token")
    assert plugin_secret_registry.is_plugin_secret("legacyhub.region")
    # A staged install still pending declares its fields: the restart that
    # will run them is already scheduled, so its plain field stays clear.
    assert not plugin_secret_registry.is_plugin_secret("stagedhub.profile")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "legacyhub.legacy_token" in persisted


def test_a_failed_scan_without_the_reserved_list_judges_keys_and_refuses_when_blind(
        tmp_path, monkeypatch, plugin_secret_registry):
    """Without the reserved-section list the fallback judges each key
    against the settings validators instead of skipping whole sections: a
    shared section's built-in keys stay the validators' business while
    the plugin's leftover credential beside them is withheld from the
    settings API this boot, never skipped with the whole section. With
    the validators gone too there is no covering left that can be vouched
    for, and the boot refuses to complete rather than return empty
    protection for the plaintext values."""
    import sys

    state_path = _write_hub_state(tmp_path, {"installations": {}})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    monkeypatch.setitem(sys.modules, "provider_hub.manifest", None)

    settings = _ScanSettings({
        # A shared section: the built-in's keys sit beside the plugin's
        # leftover credential, and only the built-in's keys have
        # validators.
        "legendasdivx": {"username": "unit-user", "password": "unit-pass",
                         "captcha_response": "unit-captcha-value"},
        "ghostplugin": {"api_token": "unit-leftover-token"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.hidden_leftover_paths() == frozenset({
        "legendasdivx.captcha_response", "ghostplugin.api_token"})
    # The built-in's own keys are withheld with nothing and registered by
    # nobody: the validators answer for them.
    assert "legendasdivx.username" not in plugin_secret_registry.hidden_leftover_paths()
    assert "legendasdivx.password" not in plugin_secret_registry.hidden_leftover_paths()
    assert not plugin_secret_registry.is_plugin_secret("legendasdivx.username")
    # Registering stays unsafe without the reserved list.
    assert not plugin_secret_registry.is_plugin_secret("legendasdivx.captcha_response")

    # With neither the reserved list nor the validators readable, no
    # covering can be vouched for: the loader refuses to complete the
    # boot instead of returning empty protection sets.
    monkeypatch.setitem(sys.modules, "app.config", None)
    from secret_store.registry import HubSecretRegistrationError
    with pytest.raises(HubSecretRegistrationError):
        plugin_secret_registry.load_plugin_secret_paths(settings)


def test_a_failed_scan_with_an_unresolvable_hub_directory_still_runs_the_fallback(
        tmp_path, monkeypatch, plugin_secret_registry):
    """The leftover scan needs the bundle manifests on disk, but the
    fallback does not: when the Hub directory cannot be resolved the
    fallback still runs, so the plaintext leftovers it can vouch for are
    registered conservatively and persisted, instead of being left to a
    backstop that only knows what is already encrypted."""
    state_path = _write_hub_state(tmp_path, {
        "installations": {
            "smokehub": {
                "provider_id": "smokehub",
                "state": "active",
                "manifest": _hub_manifest("smokehub", secret_fields=("api_token",)),
            },
        },
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    def broken_dir():
        raise RuntimeError("the Hub directory cannot be resolved")

    monkeypatch.setattr("provider_hub.state.provider_hub_dir", broken_dir)

    settings = _ScanSettings({
        "ghostplugin": {"api_token": "unit-leftover-token"},
        "sonarr": {"apikey": "unit-sonarr-key"},
    })

    plugin_secret_registry.load_plugin_secret_paths(settings)

    assert plugin_secret_registry.is_plugin_secret("smokehub.api_token")
    assert plugin_secret_registry.is_plugin_secret("ghostplugin.api_token")
    assert not plugin_secret_registry.is_plugin_secret("sonarr.apikey")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["plugin_secret_paths"]
    assert "ghostplugin.api_token" in persisted
    assert "sonarr.apikey" not in persisted
