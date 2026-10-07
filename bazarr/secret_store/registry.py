# coding=utf-8
"""Registry of every sensitive setting in config.yaml.

Three credential tiers:

USER_VISIBLE_SECRETS - credentials the user types in, copies, or rotates
    via the Settings page. They are encrypted at rest, but the
    `/api/system/settings` serializer decrypts before returning so the
    UI can show / let the user copy them. Examples: provider API keys,
    auth.apikey, compat_endpoint.token (which the user copies into
    Jellyfin OS plugin / VLSub).

USER_VISIBLE_SECRET_LISTS - same tier as above, but the value is a list
    of strings (e.g. translator.gemini_keys), each item handled
    individually.

WRITE_ONLY_SECRETS - credentials encrypted at rest and omitted from settings
    responses. Only their configured status is public.

SYSTEM_SECRETS - cryptographic primitives the user MUST NOT see and the
    backend MUST NOT leak. These are scoped to backend-internal use:
    flask_secret_key for session signing, jwt_secret / file_id_secret
    for compat token signing, the encryption keys themselves. The API
    serializer masks these with `***`.

The boundary matters because the user clarification was explicit:
"we should able to see the compat on the frontend" applies to
compat_endpoint.token (the API token they copy). The compat_endpoint's
internal jwt_secret / file_id_secret are NOT visible - they're signing
keys, never shown to the user.

Membership tests use exact-match dotted paths (e.g. "sonarr.apikey").
The dynaconf API serializer flattens nested settings to that form
before checking, so this stays simple.
"""

# Sections whose connection settings are now instance rows. They remain
# encrypted for the one-time database import, but their values are never
# exposed or edited through the general settings API, because a save would
# write a value nothing reads. Plex is not here: its section still holds the
# account, the recently-added dates and the webhook and Autopulse
# configuration, none of which is a refresh.
IMPORT_ONLY_SECTIONS = frozenset({'emby', 'jellyfin', 'silo'})

USER_VISIBLE_SECRETS = frozenset({
    # Bazarr's own admin login (username + password) and the API key that
    # the SPA uses on every authenticated request. Username is NOT
    # universally treated as secret, but encrypting it alongside the
    # password preserves account privacy in support bundles / leaked
    # config.yaml uploads.
    "auth.apikey",
    "auth.username",
    "auth.password",
    # Compat endpoint token (user copies this into VLSub / Jellyfin plugin)
    "compat_endpoint.token",
    # External webhook
    "general.external_webhook_password",
    # *arr API keys
    "sonarr.apikey",
    "radarr.apikey",
    # Plex (apikey + token; encryption_key stays in SYSTEM_SECRETS).
    # plex.username / plex.email come from the OAuth flow and are user
    # PII, encrypted at rest alongside the token.
    "plex.apikey",
    "plex.token",
    "plex.username",
    "plex.email",
    # Media servers
    "jellyfin.apikey",
    "emby.apikey",
    "silo.apikey",
    # Seerr: the key is admin access to Seerr, encrypted at rest and shown
    # only in the Connections form.
    "seerr.apikey",
    # Network proxy (full login pair).
    "proxy.username",
    "proxy.password",
    # External Postgres (full login pair).
    "postgresql.username",
    "postgresql.password",
    # AI Subtitle Translator. The "encryption_key" here is a USER-managed
    # secret - the Translator settings page exposes it as a Password field
    # ("Encryption Key (optional)") that the user enters and rotates. It
    # is NOT a master key that signs other secrets, so it's user-visible
    # at rest and decrypted before the API returns it.
    "translator.openrouter_api_key",
    "translator.openrouter_encryption_key",
    "translator.lingarr_token",
    # Subtitle providers - full login pairs (username + password) so a
    # leaked config.yaml doesn't expose which provider accounts the
    # operator owns even when the password is the only crackable bit.
    "opensubtitles.username",
    "opensubtitles.password",
    "opensubtitlescom.username",
    "opensubtitlescom.password",
    "addic7ed.username",
    "addic7ed.password",
    "legendasdivx.username",
    "legendasdivx.password",
    "legendasnet.username",
    "legendasnet.password",
    "pipocas.username",
    "pipocas.password",
    "deathbycaptcha.username",
    "deathbycaptcha.password",
    # Anti-captcha vendors - bare API keys.
    "anticaptcha.anti_captcha_key",
    "captchaai.captchaai_key",
    # Subtitle providers - full login pairs, continued.
    "napisy24.username",
    "napisy24.password",
    "titlovi.username",
    "titlovi.password",
    "titulky.username",
    "titulky.password",
    "karagarga.username",
    "karagarga.password",
    # karagarga forum login is a separate credential pair (no f_username
    # validator exists - the same `username` is reused).
    "karagarga.f_password",
    # ktuvit: separate field names but same pattern (login pair).
    "ktuvit.email",
    "ktuvit.hashed_password",
    # Private-tracker passkey (single credential, no separate password).
    "hdbits.username",
    "hdbits.passkey",
    # Session cookies - functionally equivalent to a long-lived auth
    # token once the provider login has happened.
    "addic7ed.cookies",
    "avistaz.cookies",
    "cinemaz.cookies",
    "turkcealtyaziorg.cookies",
    # Subtitle providers - tokens / keys
    "assrt.token",
    "betaseries.token",
    "jimaku.api_key",
    "subdl.api_key",
    "subsource.apikey",
    "subx.api_key",
    "subsro.api_key",
    "omdb.apikey",
})


USER_VISIBLE_SECRET_LISTS = frozenset({
    # AI Subtitle Translator's pool of Gemini API keys
    "translator.gemini_keys",
})


SYSTEM_SECRETS = frozenset({
    # Flask session signing
    "general.flask_secret_key",
    # Master encryption key - never round-trips through the API
    "general.secrets_encryption_key",
    # Compat endpoint signing keys (NOT the user-facing token)
    "compat_endpoint.jwt_secret",
    "compat_endpoint.file_id_secret",
    # Plex legacy per-namespace key (commit 4 unifies under
    # general.secrets_encryption_key; left here so the legacy read
    # path still has it masked in API responses during the migration
    # window)
    "plex.encryption_key",
})


def is_user_visible_secret(key: str) -> bool:
    """True iff `key` is a scalar credential that should be encrypted at
    rest and decrypted before the API returns it."""
    return key in USER_VISIBLE_SECRETS


def is_user_visible_secret_list(key: str) -> bool:
    """True iff `key` is a list of credentials, each item handled like a
    USER_VISIBLE_SECRET (encrypt at rest per-element, decrypt for API)."""
    return key in USER_VISIBLE_SECRET_LISTS


def is_system_secret(key: str) -> bool:
    """True iff `key` is a backend-only cryptographic primitive that
    must be masked by the API serializer."""
    return key in SYSTEM_SECRETS


# Credentials encrypted at rest and omitted from all settings responses.
WRITE_ONLY_SECRETS = frozenset({"discover.tmdb_access_token"})


def is_write_only_secret(path: str) -> bool:
    return path.lower() in WRITE_ONLY_SECRETS


# --- Provider Hub plugin secrets --------------------------------------------
#
# A catalog plugin's secret fields are registered from the plugin's own
# schema, not from a hand-kept list here: every field listed in the
# manifest's `secret_fields` or carrying `secret: true` in its
# config_schema, for every installation row in Provider Hub's state.json
# whatever its state. The set also keeps the paths of plugins that were
# removed (the row goes at the next restart, the persisted path never
# leaves), and a boot scan adds keys left in config.yaml sections by
# plugins that no longer have a row at all. Membership is therefore
# dynamic: a new plugin secret is covered without a code change.
#
# Plugin secrets join the write-only tier: encrypted at rest like every
# other credential, and never returned by the settings API, which reports
# only that one is configured and how it ends.

import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from .crypto import is_encrypted  # noqa: E402

_DYNAMIC_PLUGIN_SECRET_PATHS: set[str] = set()

# Setting paths whose plaintext values are withheld from the settings API
# for this boot: the leftover scan could not run and the fallback could not
# register them safely either, so hiding what may be a credential is what
# remains. The next boot with the scan working covers them for real.
_HIDDEN_LEFTOVER_PATHS: set[str] = set()

# The id and config-key shapes a persisted plugin secret path must match,
# mirroring the manifest validation's own patterns. They are kept here
# rather than imported, because this module has to keep working when the
# manifest module is the thing that broke.
_PERSISTED_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PERSISTED_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

logger = logging.getLogger(__name__)


class HubSecretMigrationError(RuntimeError):
    """Provider Hub installation credentials are still stored in the clear
    and could not be encrypted. Raised out of the boot so startup does not
    complete while plaintext credentials remain readable in state.json."""


class HubSecretRegistrationError(RuntimeError):
    """Provider Hub plugin leftovers in the settings could be neither
    registered nor withheld: neither the reserved-section list nor the
    settings validators could be read, so no covering can be vouched for.
    Raised out of the boot so startup does not complete while the
    plaintext values remain readable in the settings and through the
    settings API."""


def plugin_secret_paths() -> frozenset[str]:
    """Every dynamically registered plugin secret path."""
    return frozenset(_DYNAMIC_PLUGIN_SECRET_PATHS)


def is_plugin_secret(path: str) -> bool:
    """True iff `path` is a Provider Hub plugin secret, registered from a
    manifest or the leftover-key boot scan."""
    return path.lower() in _DYNAMIC_PLUGIN_SECRET_PATHS


def hidden_leftover_paths() -> frozenset[str]:
    """Setting paths whose values are withheld from the settings API for
    this boot.

    Registration is the normal covering: a registered path is encrypted at
    rest and redacted by the API. Hiding is what remains when the leftover
    scan cannot run and the values cannot be registered safely either, so
    a plaintext leftover first found on that boot is never handed out."""
    return frozenset(_HIDDEN_LEFTOVER_PATHS)


def _valid_persisted_paths(value) -> set[str]:
    """The well-formed `provider_id.field` strings in a persisted
    plugin_secret_paths value.

    Only a list of such strings is durable. Anything else is logged and
    skipped rather than iterated: reading a non-list value would either
    raise before the installation rows register or walk a bare string one
    character at a time, and a character is not a `section.key` any at-rest
    loop can split."""
    if value is None:
        return set()
    if not isinstance(value, list):
        logger.warning("The persisted plugin secret paths are malformed; "
                       "they are skipped until the state is repaired")
        return set()
    paths = set()
    for item in value:
        text = item.strip().lower() if isinstance(item, str) else None
        provider_id, separator, field = (text or "").partition(".")
        if text is None or not separator \
                or not _PERSISTED_ID_RE.match(provider_id) \
                or not _PERSISTED_KEY_RE.match(field):
            logger.warning("Skipping a malformed persisted plugin secret path: %r", item)
            continue
        paths.add(f"{provider_id}.{field}")
    return paths


def register_plugin_secret_paths(paths) -> None:
    """Grow the runtime set with `paths` ('provider_id.field' strings).

    Called by the boot loader, and by install and activation so a plugin's
    secrets are covered in the running process without a restart."""
    _DYNAMIC_PLUGIN_SECRET_PATHS.update(
        str(path).strip().lower() for path in paths if str(path).strip()
    )


def reset_plugin_secret_paths(paths=()) -> None:
    """Replace the runtime set. Test seam, and the loader's way of proving
    the set is rebuilt from state.json on every boot."""
    _DYNAMIC_PLUGIN_SECRET_PATHS.clear()
    register_plugin_secret_paths(paths)


def at_rest_secret_paths() -> frozenset[str]:
    """Every scalar path the at-rest pipeline must handle: the static
    user-visible and write-only tiers plus the dynamic plugin set.

    This is what the boot decrypt, the encrypt-on-write snapshot and the
    plaintext detection all walk, so a registered plugin secret is encrypted
    at rest, decrypted at boot and detected as plaintext exactly like a
    built-in credential."""
    return (USER_VISIBLE_SECRETS | WRITE_ONLY_SECRETS
            | frozenset(_DYNAMIC_PLUGIN_SECRET_PATHS))


# The per-section key holding a write-only secret's descriptor in a settings
# or installation response: the value never comes back, only whether one is
# configured and how a long one ends. A manifest config schema can never
# declare a field with a leading underscore, so this cannot collide with a
# real field.
SECRET_DESCRIPTOR_KEY = "__secret_fields"


def manifest_secret_fields(manifest) -> set[str]:
    """The secret field names a manifest declares, through `secret_fields`
    or a `secret: true` config_schema property (the same rule the frontend
    renders a password input by).

    The one shared definition of a manifest's secret fields: the settings
    registry, the Provider Hub service and the frontend all answer this
    question the same way, so a malformed manifest is handled the same way
    everywhere too."""
    if not isinstance(manifest, dict):
        return set()
    secret_fields = manifest.get("secret_fields")
    if not isinstance(secret_fields, list):
        secret_fields = []
    names = {item for item in secret_fields if isinstance(item, str)}
    schema = manifest.get("config_schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(properties, dict):
        for key, field in properties.items():
            if isinstance(field, dict) and field.get("secret") is True and isinstance(key, str):
                names.add(key)
    return names


_manifest_secret_fields = manifest_secret_fields


def _manifest_field_names(manifest) -> set[str]:
    """Every field name a manifest declares in its config_schema."""
    if not isinstance(manifest, dict):
        return set()
    schema = manifest.get("config_schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return set()
    return {key for key in properties if isinstance(key, str)}


def _installation_secret_paths(state) -> set[str]:
    """`provider_id.field` paths for every installation row, whatever its
    state: a staged row's credentials need the same protection as an active
    one's, and a removed-but-not-yet-restarted row still owns its section.

    A malformed installations map is read as empty rather than raised out
    of: the rest of the boot (persisted paths, the leftover scan, the
    backstop) still has to register what it can."""
    paths = set()
    installations = state.get("installations") if isinstance(state, dict) else None
    if not isinstance(installations, dict):
        if installations not in (None, {}, []):
            logger.warning("The Provider Hub installations are malformed; "
                           "installation rows register no secret paths")
        return paths
    for provider_id, installation in installations.items():
        if not isinstance(installation, dict):
            continue
        for manifest in (installation.get("manifest"), installation.get("staged_manifest")):
            for field in _manifest_secret_fields(manifest):
                paths.add(f"{str(provider_id).lower()}.{field.lower()}")
    return paths


_BOOLEAN_SECRET_CACHE: dict[str, Any] = {"key": None, "paths": frozenset()}


def manifest_boolean_secret_paths() -> frozenset[str]:
    """Plugin paths whose active manifest declares the field boolean,
    whether or not it is a secret.

    A manifest may mark a boolean property secret, and a False toggle is a
    stored value, not a typed credential: rewriting it as text (the way an
    older save's coerced token is handled) would store the truthy string
    "False" for a toggle the user turned off. The rule follows the active
    manifest's declared type alone: a later version may drop the secret
    flag while keeping the field boolean, and the path the earlier version
    registered never leaves the persisted registry, so a stored boolean the
    active manifest still declares keeps its toggle treatment. A boolean
    at any other registered path is a typed token an older save coerced
    and keeps the usual covering.

    Cached by state.json identity (the file's stat plus the write version),
    because the at-rest pipeline asks per scalar it walks and only a
    boolean ever reaches the question."""
    try:
        from provider_hub.state import load_state, state_file, state_version

        path = state_file()
        try:
            stat = path.stat()
            disk_key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            disk_key = None
        cache_key = (str(path), disk_key, state_version())
    except Exception:
        return frozenset()
    if _BOOLEAN_SECRET_CACHE["key"] == cache_key:
        return _BOOLEAN_SECRET_CACHE["paths"]
    paths = set()
    try:
        installations = load_state().get("installations")
        if not isinstance(installations, dict):
            installations = {}
        for provider_id, installation in installations.items():
            if not isinstance(installation, dict):
                continue
            manifest = installation.get("manifest")
            if not isinstance(manifest, dict):
                manifest = installation.get("staged_manifest")
            schema = manifest.get("config_schema") if isinstance(manifest, dict) else None
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if not isinstance(properties, dict):
                continue
            for field, definition in properties.items():
                if (isinstance(field, str) and isinstance(definition, dict)
                        and definition.get("type") == "boolean"):
                    paths.add(f"{str(provider_id).lower()}.{field.lower()}")
    except Exception:
        return frozenset()
    result = frozenset(paths)
    _BOOLEAN_SECRET_CACHE["key"] = cache_key
    _BOOLEAN_SECRET_CACHE["paths"] = result
    return result


def _bundle_manifests(provider_hub_dir, provider_id):
    """Every provider.json left under bundles/<id>/ on disk, with the
    directory it was read from.

    Removal deletes the installation row but not the bundle, so the manifest
    of each version once installed is normally still on disk; a corrupt file
    is skipped rather than trusted."""
    bundles = provider_hub_dir / "bundles" / provider_id
    if not bundles.is_dir():
        return []
    manifests = []
    for path in sorted(bundles.rglob("provider.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Skipping unreadable provider manifest %s", path)
            continue
        if isinstance(manifest, dict):
            manifests.append((path, manifest))
    return manifests


def _credential_shaped(value) -> bool:
    """A value that can be a stored credential: text, or a number an older
    save coerced a typed token into. A boolean is excluded on purpose:
    isinstance(True, int) is True, so the number test alone would take it,
    but a boolean at a plugin path is a toggle until a manifest declares
    that exact field secret."""
    if isinstance(value, str):
        return bool(value)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _case_variants(config, field) -> list[str]:
    """The stored case variants of a canonical field name: keys an older
    version persisted under a mixed-case spelling of `field` (API_KEY for
    api_key), sorted so a resolution never depends on iteration order."""
    return sorted(
        key for key in config
        if isinstance(key, str) and key != field and key.lower() == field
    )


def _held_manifests(state, provider_hub_dir, provider_id, installation):
    """The manifests held for a provider id: every held catalog entry, every
    bundle manifest on disk, and the installation's own manifests.

    A held catalog entry can add secrets but never clear a key, because an
    unrelated catalog can publish the same id; only the bundle the
    installation row's active_path names can keep a key clear, so every
    other held manifest adds coverage at most."""
    held = []
    entries = state.get("catalog_entries") if isinstance(state, dict) else None
    held.extend(
        entry["manifest"]
        for entry in (entries if isinstance(entries, dict) else {}).values()
        if (isinstance(entry, dict)
            and str(entry.get("provider_id") or "").lower() == provider_id
            and isinstance(entry.get("manifest"), dict))
    )
    if isinstance(installation, dict):
        held.extend(
            manifest
            for manifest in (installation.get("manifest"), installation.get("staged_manifest"))
            if isinstance(manifest, dict)
        )
    held.extend(manifest for _path, manifest in _bundle_manifests(provider_hub_dir, provider_id))
    return held


def _builtin_validator_paths():
    """The exact dotted paths Bazarr's own settings validators define, or
    None when they cannot be read. The boot scan treats a key a validator
    defines as Bazarr's own and never touches it, so an unreadable validator
    list cancels the whole scan (fail toward leaving values alone) rather
    than risking a built-in credential becoming write-only."""
    try:
        from app.config import validators
    except Exception as error:
        logger.warning("Cannot read the settings validators for the plugin secret scan: %s",
                       type(error).__name__)
        return None
    paths = set()
    for validator in validators or ():
        for name in getattr(validator, "names", None) or ():
            text = str(name).strip().lower()
            if "." in text:
                paths.add(text)
    return paths or None


def _declared_fields(installation):
    """The field names a section's running installation declares in its
    schema. An installation with an active version runs the active
    manifest's code, so the active manifest alone decides which keys its
    section declares: a staged version redeclaring a retained key as
    plain must not suppress the leftover rule, or the credential the
    active code no longer names stays plaintext in the section and
    readable through the settings API until the restart swaps the code
    anyway. A row without an active version declares only while a staged
    install is pending: a failed row's manifest is the validated manifest
    of a version whose code never ran (the install failed before any of
    it executed), so it is no evidence about the section either and its
    keys fall to the caller's own rules."""
    if not isinstance(installation, dict):
        return set()
    if installation.get("active_version"):
        manifests = (installation.get("manifest"),)
    elif (installation.get("staged_version")
            and installation.get("pending_restart")):
        manifests = (installation.get("manifest"),
                     installation.get("staged_manifest"))
    else:
        return set()
    declared = set()
    for manifest in manifests:
        declared |= {field.lower() for field in _manifest_field_names(manifest)}
    return declared


def _scan_leftover_plugin_keys(settings_obj, state, provider_hub_dir):
    """Find plugin secret keys left in settings sections, key by key.

    Returns None when the scan cannot run at all (the reserved-section list
    or the settings validators are unavailable, the sections cannot be
    read): the loader then falls back to a conservative discovery instead
    of mistaking a give-up for a clean empty result.

    A catalog plugin that replaces a built-in shares that built-in's
    settings section, so a whole-section test would call the section
    built-in and leave the plugin's keys in the clear. Each `section.key`
    is judged on its own: it is a leftover plugin key when its section is
    not one of Bazarr's own, no validator defines that exact path, and
    the schema of the installation that owns the section, if one does,
    does not declare it (a running installation is judged by its active
    manifest alone; the staged version's code has not run, and a failed
    row's never-run manifest declares nothing). A leftover
    key is registered when any manifest held for the id declares it
    secret, and stays clear only when the bundle the installation row's
    active_path names, the directory the code this install actually runs
    from, declares it as a plain field: a bundle whose version matches but
    whose code never ran (a failed same-version reinstall from another
    commit) is no evidence at all, a provider.json nested inside a shipped
    bundle never matches, and a row without an active_path grants no
    exception, which errs toward registering; every other leftover key
    holding a credential-shaped value (non-empty
    text, or a number an older save coerced a typed token into) is
    registered by the last-resort rule, so a field a later version
    renamed, or that no held manifest ever knew, is still covered. A
    boolean is registered only when a held manifest declares that exact
    field secret: a leftover boolean is far more plausibly a toggle than
    a credential, but a manifest that says secret is authoritative about
    its own field."""
    try:
        from provider_hub.manifest import reserved_settings_sections

        reserved = reserved_settings_sections()
    except Exception as error:
        logger.warning("Skipping the leftover plugin secret scan: %s", error)
        return None
    validator_paths = _builtin_validator_paths()
    if validator_paths is None:
        logger.warning("Skipping the leftover plugin secret scan: the settings "
                       "validators are unavailable")
        return None

    try:
        sections = {
            str(name).lower(): values
            for name, values in settings_obj.as_dict().items()
            if isinstance(values, dict)
        }
    except Exception as error:
        logger.warning("Skipping the leftover plugin secret scan: %s", type(error).__name__)
        return None

    installations = state.get("installations") if isinstance(state, dict) else None
    if not isinstance(installations, dict):
        installations = {}
    discovered = set()
    for section, values in sections.items():
        if not section or section in reserved:
            continue
        installation = installations.get(section)
        declared = _declared_fields(installation)
        for key, value in values.items():
            if not isinstance(key, str) or key.startswith("__"):
                continue
            path = f"{section}.{key.lower()}"
            if path in validator_paths or key.lower() in declared:
                continue
            is_boolean = isinstance(value, bool)
            if not _credential_shaped(value) and not is_boolean:
                # Not a value a credential can hide behind: an empty string,
                # a list, a dict, or nothing at all.
                continue
            held = _held_manifests(state, provider_hub_dir, section, installation)
            if any(key.lower() in {f.lower() for f in _manifest_secret_fields(manifest)}
                   for manifest in held):
                # A held manifest declaring the field secret is authoritative
                # whatever the value's shape: text, a coerced number, or a
                # boolean this install once stored under a secret flag.
                discovered.add(path)
                continue
            if is_boolean:
                # No manifest knows this boolean as a secret: it stays a
                # plain field rather than becoming write-only on a guess.
                continue
            active_path = (str(installation.get("active_path") or "").strip()
                           if isinstance(installation, dict) else "")
            ran_bundles = [
                manifest for path, manifest in _bundle_manifests(provider_hub_dir, section)
                if active_path
                and path.parent == Path(active_path)
            ]
            if any(key.lower() in {f.lower() for f in _manifest_field_names(manifest)}
                   for manifest in ran_bundles):
                # The bundle whose directory the row's active_path names is
                # the one whose code this install actually runs, the only
                # bundle whose plain declaration can keep a key clear. A
                # same-version reinstall that failed before its code ran
                # leaves a bundle whose version matches, so the version
                # alone is no evidence, and a provider.json nested inside
                # the shipped bundle never matches either. No active_path
                # means no exception, which errs toward registering.
                continue
            # Last resort: over-matching is deliberate. The path stays
            # registered, so the value always decrypts; the only cost is that
            # a non-secret field becomes write-only if the plugin comes back.
            discovered.add(path)
    return discovered


def _fallback_leftover_discovery(settings_obj, state):
    """What the loader does about plaintext leftovers when the scan cannot
    run: (paths to register, paths to hide from the settings API).

    Discovery is the scan's own rule with the bundle manifests left out:
    credential-shaped values in non-reserved sections at keys no validator
    defines and the installation row does not declare. The state's own
    declarations still count, so a plain field the running manifest
    declares keeps its plain handling instead of being persisted as a
    secret path and encrypted into a permanently write-only field; the
    bundle declarations the scan reads from disk are what the fallback
    does without, because it runs without the Hub directory too. It
    needs both halves of that rule, the reserved-section list and the
    settings validators, because without the reserved list a Bazarr key
    the validators happen to miss would go write-only and without the
    validators a shared section's built-in credential would.
    When one half is missing, registering is not safe, so the candidate
    values are hidden from the settings API for this boot instead: a
    withheld credential cannot leak, and the next boot with both halves
    working covers it for real. An already-encrypted value is never a
    candidate; the encrypted-leftover backstop registers it.

    A section the reserved list does not name is judged key by key: a
    shared section keeps the built-in's keys out by their exact validator
    match, so a plugin's leftover credential beside them is still covered,
    where a whole-section skip would leave it neither registered nor
    hidden. When neither half can be read, or the settings sections
    themselves cannot be, no covering can be vouched for at all, so the
    boot refuses to complete rather than return empty protection for the
    plaintext values."""
    validator_paths = _builtin_validator_paths()
    reserved = None
    try:
        from provider_hub.manifest import reserved_settings_sections

        reserved = reserved_settings_sections()
    except Exception:
        reserved = None
    try:
        sections = {
            str(name).lower(): values
            for name, values in settings_obj.as_dict().items()
            if isinstance(values, dict)
        }
    except Exception as error:
        logger.error("Cannot read the settings sections to cover plaintext "
                     "plugin leftovers after a failed scan: %s", type(error).__name__)
        raise HubSecretRegistrationError(
            "The leftover plugin secret scan could not run and the settings "
            "sections could not be read to cover the plaintext plugin "
            "leftovers, so startup does not complete while they may remain "
            "readable in the settings and through the settings API"
        ) from error

    if validator_paths is None and reserved is None:
        logger.error("The leftover plugin secret scan could not run and neither "
                     "the reserved sections nor the settings validators could "
                     "be read; the boot cannot vouch for any covering of the "
                     "plaintext plugin leftovers")
        raise HubSecretRegistrationError(
            "The leftover plugin secret scan could not run and neither the "
            "reserved sections nor the settings validators could be read, "
            "so startup does not complete while plaintext plugin leftovers "
            "may remain readable in the settings"
        )

    installations = state.get("installations") if isinstance(state, dict) else None
    if not isinstance(installations, dict):
        installations = {}
    candidates = set()
    for section, values in sections.items():
        if not section or (reserved is not None and section in reserved):
            continue
        declared = _declared_fields(installations.get(section))
        for key, value in values.items():
            if not isinstance(key, str) or key.startswith("__"):
                continue
            if isinstance(value, str) and is_encrypted(value):
                continue
            if not _credential_shaped(value):
                continue
            path = f"{section}.{key.lower()}"
            if validator_paths is not None and path in validator_paths:
                continue
            if key.lower() in declared:
                continue
            candidates.add(path)

    if validator_paths is not None and reserved is not None:
        if candidates:
            logger.warning("The leftover plugin secret scan could not run; "
                           "%d plaintext leftover key(s) are registered "
                           "conservatively", len(candidates))
        return candidates, set()
    if candidates:
        logger.warning("The leftover plugin secret scan could not run and its "
                       "finds cannot be registered safely; %d candidate "
                       "value(s) are withheld from the settings API this boot",
                       len(candidates))
    return set(), candidates


def _historical_config_secret_paths(state) -> set[str]:
    """`provider_id.field` paths for credentials held in Provider Hub
    installation configs that no current manifest declares.

    A version before the secret store wrote a plugin's config values in the
    clear, and a later version could drop a field from its schema while the
    stored value survived in state.json. On the first boot after such an
    upgrade neither the current manifests nor the persisted registry
    (empty until this boot) names those values, so without this discovery
    the installation API would hand the stored credential out and the
    worker would carry it as ordinary config. Every undeclared key holding
    a credential-shaped value is registered; the conservative cost is the
    same as the leftover scan's last-resort rule, that a plain undeclared
    field becomes write-only if the plugin ever reads it again."""
    paths = set()
    installations = state.get("installations") if isinstance(state, dict) else None
    if not isinstance(installations, dict):
        return paths
    for provider_id, installation in installations.items():
        if not isinstance(installation, dict):
            continue
        config = installation.get("config")
        if not isinstance(config, dict):
            continue
        declared = set()
        if installation.get("active_version"):
            # An installation with an active version runs the active
            # manifest's code, so the active manifest alone decides what a
            # held field is. A staged version redeclaring a dropped legacy
            # field as plain must not suppress the discovery, or the
            # credential the value still is stays plaintext in the
            # installation API and in the worker config until the restart
            # that replaces the code anyway.
            manifest = installation.get("manifest")
            if isinstance(manifest, dict):
                declared = {field.lower() for field in _manifest_field_names(manifest)}
        else:
            for manifest in (installation.get("manifest"), installation.get("staged_manifest")):
                if isinstance(manifest, dict):
                    declared |= {field.lower() for field in _manifest_field_names(manifest)}
        for field, value in config.items():
            if not isinstance(field, str) or not _credential_shaped(value):
                continue
            name = field.lower()
            if name in declared:
                continue
            paths.add(f"{str(provider_id).lower()}.{name}")
    return paths


def _persist_plugin_secret_paths(paths) -> None:
    """Add `paths` to the persisted set in state.json so they survive the
    removal of the plugin that owns them."""
    if not paths:
        return
    from provider_hub.state import mutate_state

    def add_paths(state):
        known = _valid_persisted_paths(state.get("plugin_secret_paths"))
        if set(paths) - known:
            state["plugin_secret_paths"] = sorted(known | set(paths))

    mutate_state(add_paths)


def _encrypted_leftover_keys(settings_obj) -> set[str]:
    """Every key holding the encryption marker in a section the reserved
    list does not cover, needing no state at all.

    The backstop of the boot registry: if the leftover scan cannot run, or
    Provider Hub's state is unreadable, an encrypted plugin credential is
    still registered, so the boot decrypt pass decrypts it instead of
    leaving the ciphertext live in the settings (the worker would send it
    as the credential, which is exactly the fail-open this registry
    exists to remove). The marker only ever sits on a secret, and a key a
    Bazarr validator defines is skipped so a built-in credential does not
    become write-only. The reserved-section import sits inside the try with
    an empty fallback: the import fails in exactly the situations the
    backstop exists for (the same broken module that stopped the scan),
    and raising there would drop the durable registrations this loader has
    already made."""
    try:
        from provider_hub.manifest import reserved_settings_sections

        reserved = reserved_settings_sections()
    except Exception as error:
        logger.warning("The reserved section list is unavailable (%s); the "
                       "encrypted-leftover backstop registers every marked key",
                       type(error).__name__)
        reserved = set()
    validator_paths = _builtin_validator_paths() or set()
    paths = set()
    try:
        sections = {
            str(name).lower(): values
            for name, values in settings_obj.as_dict().items()
            if isinstance(values, dict)
        }
    except Exception as error:
        logger.warning("The encrypted-leftover backstop cannot read the "
                       "settings sections: %s", type(error).__name__)
        return set()
    for section, values in sections.items():
        if not section or section in reserved:
            continue
        for key, value in values.items():
            if not isinstance(key, str) or key.startswith("__"):
                continue
            if not (isinstance(value, str) and is_encrypted(value)):
                continue
            path = f"{section}.{key.lower()}"
            if path in validator_paths:
                continue
            paths.add(path)
    return paths


def migrate_installation_config_secrets(state=None) -> None:
    """Encrypt every plaintext plugin credential held in Provider Hub's
    state.json, atomically, or leave the file untouched.

    A version before the secret store wrote installation config values in
    the clear, so an upgraded install carries plaintext secrets in
    state.json the registry covers but nothing rewrites. Runs from
    app.config's boot, AFTER the boot settings write, so the master key it
    encrypts under is already durable on disk: encrypting earlier, under a
    key that exists only in memory, would commit ciphertext a failed
    settings write could leave with no key that ever opens it.

    A credential an older version persisted under a mixed-case alias of
    its field name (API_KEY for api_key) is canonicalized in the same
    write: the alias moves under the canonical name when the config holds
    no canonical value, and drops beside one it holds, so the value the
    canonical writer exposes is never lost and no alias survives to ride
    the installation API out in the clear.

    Fails closed: the master key is made durable before anything is
    encrypted, any encryption error writes nothing (the file keeps exactly
    what it had, never a partial rewrite), and a failure raises
    HubSecretMigrationError so the boot does not complete while plaintext
    credentials remain readable in the file. The values stay covered and
    the migration reruns the moment the key works.

    A list or a dictionary an earlier Hub write left at a registered
    secret path raises the same way: no at-rest pass can encrypt it, and
    the runtime decryption hands a non-string back unchanged, so passing
    it through would hand the plugin the collection in the clear. The
    refusal ends only when the state holds text at the path again.
    `state` is the state as the boot loader just read it, used to decide
    whether there is anything to migrate before the locked write; None
    loads it fresh."""
    try:
        from provider_hub.state import load_state, mutate_state
    except Exception as error:
        logger.error("Cannot import the Provider Hub state module to "
                     "enumerate the installation credentials: %s",
                     type(error).__name__)
        raise HubSecretMigrationError(
            "The Provider Hub installation credentials could not be "
            "enumerated because the state module could not be imported; "
            "they stay covered and startup does not complete while they "
            "may remain readable in the clear"
        ) from error
    if state is None:
        state = load_state()
    if not isinstance(state, dict):
        state = {}

    def _fields(provider_id, installation):
        """The credential fields of one installation: everything the current
        or staged schema declares secret, everything the persisted registry
        names for this provider, and everything the runtime registry just
        discovered (the loader runs first, so a historical field found in
        this same boot is covered by the same write)."""
        fields = set()
        for manifest in (installation.get("manifest"), installation.get("staged_manifest")):
            if isinstance(manifest, dict):
                fields |= manifest_secret_fields(manifest)
        prefix = f"{str(provider_id).lower()}."
        for path in plugin_secret_paths():
            text = str(path).strip().lower()
            if text.startswith(prefix) and "." not in text[len(prefix):]:
                fields.add(text[len(prefix):])
        for path in _valid_persisted_paths(state.get("plugin_secret_paths")):
            if path.startswith(prefix) and "." not in path[len(prefix):]:
                fields.add(path[len(prefix):])
        return fields

    try:
        plaintext = []
        canonicalize = []
        uncovered_collections = []
        installations = state.get("installations")
        if isinstance(installations, dict):
            for provider_id, installation in installations.items():
                if not isinstance(installation, dict):
                    continue
                config = installation.get("config")
                if not isinstance(config, dict):
                    continue
                for field in _fields(provider_id, installation):
                    variants = _case_variants(config, field)
                    if variants:
                        canonicalize.append((provider_id, field))
                    # The value the canonical writer exposes: the one at
                    # the canonical name when the config holds it, else the
                    # one a mixed-case alias of the field still holds.
                    if field not in config and variants:
                        value = config.get(variants[0])
                    else:
                        value = config.get(field)
                    if isinstance(value, str) and value and not is_encrypted(value):
                        plaintext.append((provider_id, field))
                        continue
                    if value is None or isinstance(value, str):
                        continue
                    if isinstance(value, (list, dict)):
                        # A collection an earlier Hub write left at a
                        # registered secret path can be neither encrypted
                        # at rest nor covered while the worker config is
                        # built: the runtime decryption hands a non-string
                        # back unchanged, so it would travel to the plugin
                        # as the credential in the clear. An empty one is
                        # not a credential, the same empty the string rule
                        # leaves alone.
                        if value:
                            uncovered_collections.append((provider_id, field))
                        continue
                    if (isinstance(value, bool)
                            and f"{str(provider_id).lower()}.{str(field).lower()}"
                            in manifest_boolean_secret_paths()):
                        # A boolean the active manifest declares is a stored
                        # toggle, not a credential, whether or not the
                        # field is a secret: stringifying its False would
                        # hand the plugin the truthy text, so it keeps its
                        # value.
                        continue
                    # A typed token an older save coerced into a number, or
                    # a legacy credential it coerced into a boolean ("true"
                    # that became True under a string-secret schema): stored
                    # as the typed text from now on, so it encrypts at rest
                    # like every other credential instead of staying a bare
                    # scalar in the clear.
                    plaintext.append((provider_id, field))
    except Exception as error:
        raise HubSecretMigrationError(
            "The Provider Hub installation credentials could not be read "
            "for encryption; they stay covered and startup does not "
            "complete while they may remain readable in the clear"
        ) from error
    if uncovered_collections:
        # Fail closed before any key work or rewrite, exactly like the
        # enumeration refusal above: the file keeps what it had, and the
        # boot does not complete while the collection may remain readable
        # in the clear.
        raise HubSecretMigrationError(
            "The Provider Hub installation credentials include a list or "
            "a dictionary where a registered secret path lives, which "
            "the secret store cannot encrypt; they stay covered and "
            "startup does not complete while they may remain readable "
            "in the clear"
        )
    if not plaintext and not canonicalize:
        return

    if plaintext:
        # Defer the encryption until the master key is durably saved: the key
        # this process may have just generated exists only in memory until a
        # settings write lands, and a restart before that would mint a
        # different one, leaving these credentials permanently unopenable.
        # A canonicalization alone rewrites what is stored, never a value
        # into ciphertext, so it needs no key to be durable first.
        try:
            from .crypto import persist_master_key
            persist_master_key()
        except Exception as error:
            raise HubSecretMigrationError(
                "The Provider Hub installation credentials were left as stored: "
                "the master key could not be saved to disk, so encrypting them "
                "would store ciphertext no restart can open"
            ) from error

    def encrypt_values(current_state):
        # The stored case variants of a credential key resolve first: the
        # value at the canonical name is the one the canonical writer
        # exposes, so an alias beside it drops as a stale duplicate, while
        # an alias with no canonical name beside it moves under the
        # canonical name, whatever order the config holds them in.
        for provider_id, field in canonicalize:
            installation = (current_state.get("installations") or {}).get(provider_id)
            if not isinstance(installation, dict):
                continue
            config = installation.get("config")
            if not isinstance(config, dict):
                continue
            variants = _case_variants(config, field)
            if not variants:
                continue
            if field not in config:
                config[field] = config[variants[0]]
            for variant in variants:
                config.pop(variant, None)
        for provider_id, field in plaintext:
            installation = (current_state.get("installations") or {}).get(provider_id)
            if not isinstance(installation, dict):
                continue
            config = installation.get("config")
            if not isinstance(config, dict):
                continue
            value = config.get(field)
            if isinstance(value, str) and value and not is_encrypted(value):
                config[field] = encrypt_secret(value)
            elif value is not None and not isinstance(value, (str, list, dict)):
                # A coerced scalar (a number, or a boolean a legacy save
                # typed as text): stored as the typed text, the same
                # normalization the live-scalar pass applies ("true" and
                # "false" in the lowercase form, never Python's capitalized
                # repr, which would count as truthy for a False). The state
                # may have changed between the detection and this locked
                # write, so the toggle exception is checked here too: a
                # boolean the active manifest declares keeps its value,
                # whether or not the field is a secret.
                if (isinstance(value, bool)
                        and f"{str(provider_id).lower()}.{str(field).lower()}"
                        in manifest_boolean_secret_paths()):
                    continue
                config[field] = encrypt_secret(_coerced_secret_text(value))

    try:
        from .crypto import encrypt_secret
        from .migration import _coerced_secret_text
        mutate_state(encrypt_values)
    except Exception as error:
        raise HubSecretMigrationError(
            "The Provider Hub installation credentials were left as stored "
            "(encryption failed); they stay covered and are rewritten when "
            "the key works"
        ) from error
    logger.info("Encrypted %d Provider Hub plugin credential(s) left "
                "in the clear by an earlier version", len(plaintext))


def load_plugin_secret_paths(settings_obj) -> None:
    """Boot loader for the dynamic part of the registry.

    Reads Provider Hub's state.json directly (never through the service
    layer, which is not up while settings load): the persisted paths of
    removed plugins, every installation row's schema secrets, and the
    historical fields held in installation configs that no current
    manifest declares, then the per-key scan of the live settings
    sections for leftovers. Registers everything it finds and persists the
    newly discovered paths, so the boot secret pass that runs right after
    encrypts plaintext plugin secrets on the first boot after the upgrade.
    The state.json encryption itself is invoked from app.config after the
    boot settings write, never from here.

    Fails toward coverage, never toward the clear: the durable sources
    (persisted paths, installation rows, historical config fields)
    register first, a malformed installations map is read as empty rather
    than aborting the registration, a malformed persisted set is validated
    entry by entry rather than iterated, the leftover scan runs in its own
    try so its failure cannot discard them, and the no-state backstop
    registers every key holding the encryption marker in a non-reserved
    section (its own call guarded too), so an unreadable state.json or a
    broken manifest module still leaves the boot pass able to decrypt what
    is encrypted. A scan that cannot run at all does not leave its finds in
    the clear either: the conservative fallback registers the plaintext
    leftovers it can vouch for, hides the ones it cannot from the
    settings API for this boot, and runs without the Hub directory too,
    because it needs no bundle manifests; when neither the reserved
    sections nor the settings validators can be read, or the settings
    sections themselves cannot be read at all, it raises instead, so
    the boot does not complete while the plaintext values stay
    uncovered."""
    try:
        from provider_hub.state import load_state, provider_hub_dir
        state = load_state()
    except Exception as error:
        logger.warning("Cannot load the Provider Hub state for the secret registry: %s",
                       type(error).__name__)
        state = {}
    if not isinstance(state, dict):
        state = {}
    try:
        hub_dir = provider_hub_dir()
    except Exception as error:
        logger.warning("Cannot locate the Provider Hub directory for the secret registry: %s",
                       type(error).__name__)
        hub_dir = None

    persisted = _valid_persisted_paths(state.get("plugin_secret_paths"))
    installation_paths = _installation_secret_paths(state)
    historical_paths = _historical_config_secret_paths(state)
    # The durable sources register first: a failure in the leftover scan
    # below must not discard them, or every plugin ciphertext stays live.
    reset_plugin_secret_paths(persisted | installation_paths | historical_paths)

    scanned = None
    if hub_dir is not None:
        try:
            scanned = _scan_leftover_plugin_keys(settings_obj, state, hub_dir)
        except Exception as error:
            logger.warning("The leftover plugin secret scan failed (%s); the "
                           "encrypted-leftover backstop still covers what it "
                           "would have found", type(error).__name__)
    # The scan gave up, broke, or never ran because the Hub directory could
    # not be resolved: the fallback needs no bundle manifests, so the
    # directory does not spare it either. A scan that cannot decide leaves
    # its finds in the clear otherwise: a plaintext leftover first found
    # on that boot would stay readable on disk and through the settings
    # API. The conservative fallback registers what it can vouch for and
    # hides the rest from the settings API, so nothing the scan would
    # have covered is handed out.
    fallback_discovered, fallback_hidden = (set(), set())
    if scanned is None:
        fallback_discovered, fallback_hidden = _fallback_leftover_discovery(settings_obj, state)
    _HIDDEN_LEFTOVER_PATHS.clear()
    _HIDDEN_LEFTOVER_PATHS.update(fallback_hidden)
    # The backstop needs no state: whatever holds the encryption marker in
    # a non-reserved section is a secret this install once covered. Its
    # own failure is contained here too: the durable registrations above
    # have already landed, and the loader must not abort because its
    # second-chance scan broke in the same way its first one did.
    backstop = set()
    try:
        backstop = _encrypted_leftover_keys(settings_obj)
    except Exception as error:
        logger.warning("The encrypted-leftover backstop failed (%s); the "
                       "durable registrations stand", type(error).__name__)
    register_plugin_secret_paths((scanned or set()) | fallback_discovered | backstop)
    # The scan's finds, the fallback's and the installation rows' paths join
    # the persisted set: an install from before this upgrade gets its paths
    # persisted at the first boot after it, and nothing but an explicit
    # state wipe removes them again. Best-effort: an unreadable state cannot
    # be written either, and the runtime registration above already covers
    # this boot.
    try:
        _persist_plugin_secret_paths((scanned or set()) | fallback_discovered
                                     | installation_paths | backstop | historical_paths)
    except Exception as error:
        logger.warning("Cannot persist the discovered plugin secret paths: %s",
                       type(error).__name__)
