# coding=utf-8
"""Bidirectional secret crossing between in-memory plaintext and on-disk
ciphertext.

The model:
- In memory (`settings.sonarr.apikey`, etc.): always plaintext. Application
  code reads creds the same way it always has.
- On disk (`config.yaml`): always ciphertext, post-migration. The first-
  boot-after-upgrade path is plaintext; the bootstrap decrypter is a
  passthrough on plaintext (`decrypt_secret` returns plaintext unchanged
  when the marker prefix is missing) and the next `write_config()`
  rewrites the file in encrypted form. Auto-migration is implicit -
  there is no separate one-shot script.

Two boundaries:
1. After dynaconf finishes loading config.yaml: walk USER_VISIBLE_SECRETS and
   WRITE_ONLY_SECRETS and decrypt anything carrying the marker prefix. After this runs, the
   live settings object holds plaintext for every credential.
2. Inside `write_config()`: snapshot settings as a dict, encrypt every
   USER_VISIBLE_SECRETS and WRITE_ONLY_SECRETS in the snapshot, persist that. Only the
   snapshot is encrypted; the live settings object stays plaintext.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Dict

from . import crypto as _crypto  # module reference so test patches on
                                  # _crypto.get_master_key apply uniformly
                                  # across this module's call sites
from .crypto import decrypt_secret, encrypt_secret, is_encrypted
from .registry import (
    USER_VISIBLE_SECRET_LISTS,
    at_rest_secret_paths,
    manifest_boolean_secret_paths,
)


# Plex legacy encryption fields. Pre-this-package, plex.apikey / plex.token
# were stored encrypted under plex.encryption_key (URLSafeSerializer with
# no marker prefix), and a sibling boolean plex.apikey_encrypted flagged
# the apikey as encrypted. The unified pipeline doesn't need any of that
# - encryption metadata is self-describing via the marker prefix - so on
# first boot we decrypt them with the legacy key and let write_config
# re-encrypt under the unified master key.
_PLEX_LEGACY_FIELDS = ("apikey", "token")

logger = logging.getLogger(__name__)

# Setting paths whose value failed to decrypt at boot. The live value is
# withheld from every consumer (empty string, never the ciphertext), and
# write_config keeps the on-disk ciphertext for these paths so a later save
# does not overwrite recoverable ciphertext with the withheld empty value.
# A save that carries the path (a new value or an explicit clear) removes it:
# the failure is over, and neither the descriptor nor the preserve pass may
# hold on to the old ciphertext past what the user just did.
_FAILED_DECRYPT_PATHS: set[str] = set()

# Failed elements of list-shaped secrets, per path: the ciphertext exactly as
# it sits on disk. The preserve pass re-appends them to a later save's list,
# so one element that fails to decrypt does not cost the elements that would
# decrypt again under the right master key. A save that carries the path
# replaces the whole list and drops the records with it.
_FAILED_DECRYPT_ELEMENTS: dict[str, list[str]] = {}

# Health notices for credentials this boot replaced because their stored
# value would not decrypt. The replacement is already done, so the notice
# tells the operator what to do next (update whatever held the old value)
# rather than asking them to re-enter anything.
_REPLACED_SECRET_ISSUES: list[dict[str, str]] = []

# A secret shorter than this never has its last four characters exposed:
# four characters of a short secret would be a large fraction of it.
# The settings API and the Provider Hub installation API build their
# write-only descriptors by this one rule.
LAST_FOUR_MIN_LENGTH = 16


def decrypt_failure_paths() -> frozenset[str]:
    """The setting paths whose stored value would not decrypt this boot."""
    return frozenset(_FAILED_DECRYPT_PATHS)


def reset_decrypt_failures() -> None:
    """Clear the per-path decrypt failure set. A fresh boot starts empty;
    values that decrypt again no longer report a failure, and the
    replacement notices of the previous boot do not carry over."""
    _FAILED_DECRYPT_PATHS.clear()
    _FAILED_DECRYPT_ELEMENTS.clear()
    _REPLACED_SECRET_ISSUES.clear()


def clear_decrypt_failure_path(path: str) -> None:
    """End the decrypt failure for one path.

    Called for every path a settings save carries, before the config is
    written: a new value replaces the credential and an explicit clear
    removes it, and neither may keep reporting decrypt_failed, hold the
    old recoverable ciphertext over the fresh value, or resurrect the
    credential after the next restart."""
    _FAILED_DECRYPT_PATHS.discard(path)
    _FAILED_DECRYPT_ELEMENTS.pop(path, None)


def note_replaced_secret(path: str, issue: str) -> None:
    """Record the health notice for a credential this boot replaced because
    its stored value would not decrypt. The path leaves the failure set
    (nothing needs re-entering), and the notice says what the operator still
    has to do."""
    _REPLACED_SECRET_ISSUES.append({"object": path, "issue": issue})


def replaced_secret_issues() -> list[dict[str, str]]:
    """Health issues for credentials this boot replaced, never their
    values."""
    return [dict(item) for item in _REPLACED_SECRET_ISSUES]


def clear_replaced_secret_paths(live_snapshot: Dict[str, Any]) -> None:
    """After a successful settings write: end the failure of every scalar
    path whose live value is non-empty.

    The boot pass withholds a failed value as an empty string, so a live
    value that is not empty means some writer replaced the credential and
    the fresh ciphertext of that value is now on disk: the failure is over.
    Without this, only the settings API's save path ended a failure, and a
    writer that set a credential directly (an OAuth sign-in, a generated
    token) kept the health issue asking for a re-entry that had already
    happened. List-shaped paths are left alone: their per-element records
    survive until a save carries the path, and an element that reappears
    non-empty in a list is not a replacement of the failed ones."""
    for path in sorted(_FAILED_DECRYPT_PATHS):
        if path in _FAILED_DECRYPT_ELEMENTS:
            continue
        try:
            _section_key, _attr, live = _read_section_key(live_snapshot, path)
        except (KeyError, ValueError):
            continue
        if isinstance(live, list):
            continue
        if live not in (None, ""):
            clear_decrypt_failure_path(path)


def decrypt_failure_issues() -> list[dict[str, str]]:
    """Health issues for every credential that failed to decrypt.

    Each issue names the setting path so the operator knows exactly which
    credential to re-enter, and never the value."""
    return [
        {
            "object": path,
            "issue": "The stored credential could not be decrypted (the master key may have "
                     "changed). Re-enter this credential in Settings and save.",
        }
        for path in sorted(_FAILED_DECRYPT_PATHS)
    ]


def _split_path(path: str) -> tuple[str, str]:
    """Split 'section.key' into ('section', 'key'). Always yields exactly
    two parts because the registry keys are flat dotted paths by contract."""
    section, _, key = path.partition(".")
    if not key:
        raise ValueError(f"Registry path {path!r} must be 'section.key'")
    return section, key


def _coerced_secret_text(value) -> str:
    """The typed text a non-string scalar at a registered secret path
    stands for. A boolean is the lowercase text the settings write path
    round-trips ("true" and "false", never Python's capitalized repr, which
    is not what anybody typed and would count as truthy for a False); a
    number is its digits."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _read_section_key(d: Dict[str, Any], path: str) -> tuple[str, str, Any]:
    """Look up `section.key` in a dict whose top-level keys may be either
    case. Returns (section_lookup_key, attr_key, value) or raises KeyError.
    """
    section, key = _split_path(path)
    # Settings dicts have section keys in mixed case depending on origin;
    # match either casing without rewriting the dict.
    for candidate in (section, section.lower(), section.upper()):
        if candidate in d and isinstance(d[candidate], dict) and key in d[candidate]:
            return candidate, key, d[candidate][key]
    raise KeyError(path)


def migrate_legacy_plex_encryption(settings_obj) -> None:
    """Decrypt any plex.apikey / plex.token still stored under the legacy
    URLSafeSerializer + plex.encryption_key scheme. Runs ONCE at boot,
    BEFORE decrypt_settings_in_place - the legacy ciphertext does NOT
    carry the enc:v1: marker, so without this conversion the unified
    pipeline would treat the legacy bytes as plaintext, the next write
    would re-encrypt them as if they were a credential, and the user's
    plex creds would be unrecoverable.

    The legacy plex.encryption_key stays in config.yaml (it's still a
    SYSTEM_SECRET masked by the API serializer) so the migration is
    one-way safe: a downgrade post-this-version can't read enc:v1:
    ciphertext anyway, so leaving the legacy key around is harmless and
    avoids a destructive deletion in the migration path.

    Trigger semantics: detection is value-shaped, NOT flag-shaped. The
    legacy apikey path set `plex.apikey_encrypted = True`, but the OAuth
    flow stored `settings.plex.token = encrypt_token(...)` and never
    touched that flag. Gating on `apikey_encrypted` left OAuth tokens
    untouched, the unified pipeline then encrypted the legacy URLSafe
    payload as if it were plaintext, and Plex got the unwrapped JSON
    blob as the auth token after decrypt - breaking OAuth users on
    upgrade.

    Approach: when plex.encryption_key is non-empty, attempt a
    URLSafeSerializer load (the legacy TokenManager format) on every
    legacy field. URLSafeSerializer raises on plaintext / empty / already-unified-marker values, so
    the same code path covers both the apikey-encrypted flag case AND
    the OAuth-no-flag case without false positives.
    """
    plex = getattr(settings_obj, "plex", None)
    if plex is None:
        return

    legacy_key = plex.get("encryption_key", "") if hasattr(plex, "get") \
        else getattr(plex, "encryption_key", "")

    apikey_encrypted = bool(plex.get("apikey_encrypted", False)) \
        if hasattr(plex, "get") else bool(getattr(plex, "apikey_encrypted", False))

    if not legacy_key:
        # Without the key we can't decrypt anything. Clear the flag if it
        # was set so the migration check doesn't trip on every boot, and
        # leave the values for the operator to fix from Settings.
        if apikey_encrypted:
            plex.apikey_encrypted = False
            logger.warning(
                "plex.apikey_encrypted=True but plex.encryption_key is empty; "
                "skipping legacy decryption. Reconfigure Plex from Settings."
            )
        return

    # Decode the legacy payload with itsdangerous directly. Importing
    # api.plex.security here would load the whole api package, which
    # imports app.config while app.config is still running this migration,
    # so the import always failed at startup.
    from itsdangerous import URLSafeSerializer

    serializer = URLSafeSerializer(legacy_key)
    migrated_any = False
    for field in _PLEX_LEGACY_FIELDS:
        ciphertext = plex.get(field, "") if hasattr(plex, "get") \
            else getattr(plex, field, "")
        if not ciphertext:
            continue
        if is_encrypted(ciphertext):
            # Already on the unified format (someone re-saved between
            # commit 2 deploy and this migration running). Nothing to do.
            continue
        try:
            payload = serializer.loads(ciphertext)
            plaintext = payload["token"]
            if not isinstance(plaintext, str) or not plaintext:
                continue
        except Exception:
            # URLSafeSerializer rejects this payload. Two legitimate
            # causes: the value was already plaintext (fresh manual
            # edit, never went through encrypt_token), OR the legacy key
            # was rotated. Either way the unified pipeline downstream
            # treats no-marker non-empty strings as plaintext and
            # re-encrypts them on next save. Silent passthrough is the
            # right choice; an error log here would fire on every fresh
            # install with a non-empty plex.encryption_key, which is
            # noisy and misleading.
            continue
        plex[field] = plaintext
        migrated_any = True

    # Clear the legacy flag if it was set; on plaintext-only installs we
    # leave it absent (don't materialize a False where there was nothing).
    if apikey_encrypted:
        plex.apikey_encrypted = False
    if migrated_any:
        logger.info(
            "Migrated legacy-encrypted Plex credentials to the unified "
            "secret_store format. They will be re-persisted under "
            "general.secrets_encryption_key on next config save."
        )


def has_plaintext_secrets_on_disk(settings_obj) -> bool:
    """Return True iff at least one at-rest secret path holds a value
    without the enc:v1: marker that is still a credential - a non-empty
    string, or any non-string, non-None scalar (int, float, bool). The
    scalar case is the mark of an older save that coerced a typed token
    into a number or a boolean: the typed text is the credential, and
    leaving the coerced value unencrypted would keep it in the clear on
    disk, unable to mask in the API like every other credential. A
    non-empty list or dictionary at a registered path is flagged too: no
    at-rest pass can encrypt it, so the rewrite must refuse rather than
    converge on leaving it in the clear. An empty value is not flagged:
    empty stays empty, no migration.

    Covers the static tiers plus the dynamically registered Provider Hub
    plugin secrets, so a plaintext plugin secret forces the same one-time
    rewrite as a built-in one.

    Lets the bootstrap force a one-time write_config call so the
    encryption guarantee is deterministic - the operator does not have
    to remember to touch a Settings field to trigger migration.

    Reads from the LIVE settings object (which after dynaconf load
    matches what's on disk for fields with no marker), so this is the
    same source of truth that decrypt_settings_in_place / write_config
    operate on.
    """
    for path in at_rest_secret_paths():
        try:
            section, key = _split_path(path)
            section_obj = getattr(settings_obj, section, None)
            if section_obj is None:
                continue
            current = section_obj.get(key) if hasattr(section_obj, "get") \
                else getattr(section_obj, key, None)
            if isinstance(current, str) and current and not is_encrypted(current):
                return True
            # isinstance(True, int) is True, so the bool check has to come
            # first: a boolean at a registered path is flagged exactly like
            # a number, because a save that coerced "true" left it there.
            # One exception: a field the active manifest declares boolean
            # holds a real toggle, and coercing its False to text would
            # store the truthy string, so it is not plaintext to fix.
            if isinstance(current, (bool, int, float)):
                if isinstance(current, bool) and path in manifest_boolean_secret_paths():
                    continue
                return True
            if isinstance(current, (list, dict)):
                # A collection an earlier write left at a registered
                # secret path can be neither encrypted at rest nor
                # covered, and this pass is what forces the rewrite whose
                # encryption refusal then stops the boot. An empty
                # collection is not a credential, the same empty the
                # string rule leaves alone.
                if current:
                    return True
        except Exception:
            continue

    for path in USER_VISIBLE_SECRET_LISTS:
        try:
            section, key = _split_path(path)
            section_obj = getattr(settings_obj, section, None)
            if section_obj is None:
                continue
            current = section_obj.get(key) if hasattr(section_obj, "get") \
                else getattr(section_obj, key, None)
            if isinstance(current, list):
                for item in current:
                    if isinstance(item, str) and item and not is_encrypted(item):
                        return True
        except Exception:
            continue
    return False


def decrypt_settings_in_place(settings_obj) -> None:
    """Walk every at-rest secret path in the live settings object and
    decrypt any value carrying the marker prefix. Plaintext values are
    left untouched (decrypt_secret is a passthrough on plaintext - that's
    the auto-migration story).

    Fails closed: a value that will not decrypt is withheld from every
    consumer (the live value becomes an empty string, never the
    ciphertext, so nothing can send the stored bytes anywhere as the
    credential), its path is tracked so write_config preserves the
    on-disk ciphertext, and a health issue names the setting path. The
    operator re-enters the credential; everything else keeps working.

    Runs exactly once at bootstrap, after settings.validators.validate_all
    finishes. Re-running is harmless (decrypt_secret is idempotent on
    already-plaintext values), and re-runs after reload() re-withhold a
    path whose ciphertext still will not decrypt.

    A non-string scalar at a registered path is normalized to the typed
    text it stands for in the same pass, before any worker configuration
    is built from the live settings: the rewrite stores that text on disk,
    and without the matching live value the settings overlay would keep
    forwarding the coerced number to the worker while the disk copy is
    ciphertext. A boolean at a field the active manifest declares boolean
    is a stored toggle and is left alone, whether or not it is a secret."""
    for path in at_rest_secret_paths():
        try:
            section, key = _split_path(path)
            section_obj = getattr(settings_obj, section, None)
            if section_obj is None:
                continue
            current = section_obj.get(key) if hasattr(section_obj, "get") \
                else getattr(section_obj, key, None)
            if isinstance(current, str) and is_encrypted(current):
                try:
                    decrypted = decrypt_secret(current)
                except Exception as e:
                    # Don't print the credential value or section/key hint that
                    # could be inferred to a specific provider in a public bug
                    # report. The health issue names the path; the log stays
                    # nameless.
                    logger.error(
                        f"Failed to decrypt secret at rest: {type(e).__name__}; "  # noqa: G004
                        f"the value is withheld and must be re-entered."
                    )
                    _FAILED_DECRYPT_PATHS.add(path)
                    section_obj[key] = ""
                    continue
                section_obj[key] = decrypted
            elif isinstance(current, (bool, int, float)):
                # A typed token an older save coerced into a number or a
                # boolean: the live value becomes the text it was, so the
                # worker configuration, the API and the on-disk rewrite all
                # carry the same credential. A field the active manifest
                # declares boolean is a toggle and keeps its value, whether
                # or not it is a secret.
                if isinstance(current, bool) and path in manifest_boolean_secret_paths():
                    continue
                section_obj[key] = _coerced_secret_text(current)
        except Exception as e:
            logger.error(
                f"Failed to decrypt secret at rest: {type(e).__name__}; "  # noqa: G004
                f"continuing so the rest of bazarr can boot."
            )

    for path in USER_VISIBLE_SECRET_LISTS:
        try:
            section, key = _split_path(path)
            section_obj = getattr(settings_obj, section, None)
            if section_obj is None:
                continue
            current = section_obj.get(key) if hasattr(section_obj, "get") \
                else getattr(section_obj, key, None)
            if not isinstance(current, list):
                continue
            # Re-walked on every pass: what this pass fails to decrypt is the
            # record, and what decrypts again leaves none behind.
            _FAILED_DECRYPT_ELEMENTS.pop(path, None)
            new_items = []
            for item in current:
                if isinstance(item, str) and is_encrypted(item):
                    try:
                        new_items.append(decrypt_secret(item))
                    except Exception as e:
                        # Same fail-closed rule as the scalar loop: an
                        # element that will not decrypt is withheld rather
                        # than handed to a consumer as if it were the key.
                        # The ciphertext itself is recorded, because the
                        # next save has to be able to keep it: one failed
                        # element must not cost the elements that would
                        # decrypt again under the right master key.
                        logger.error(
                            f"Failed to decrypt list-shaped secret at rest: "  # noqa: G004
                            f"{type(e).__name__}; the element is withheld and must "
                            f"be re-entered."
                        )
                        _FAILED_DECRYPT_PATHS.add(path)
                        _FAILED_DECRYPT_ELEMENTS.setdefault(path, []).append(item)
                        continue
                else:
                    new_items.append(item)
            section_obj[key] = new_items
        except Exception as e:
            logger.error(
                f"Failed to decrypt list-shaped secret at rest: "  # noqa: G004
                f"{type(e).__name__}; continuing unchanged."
            )


def encrypt_settings_dict(plaintext_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict identical to `plaintext_dict` except that every
    at-rest secret path (static tiers plus registered plugin secrets) and
    every secret list is encrypted.

    Used by `write_config()` to produce the on-disk form. The dict is
    deep-copied so the in-memory settings (which is what the caller
    snapshots) is never mutated to ciphertext.

    Already-encrypted values are returned unchanged (encrypt_secret is
    idempotent on its own ciphertext) - that handles the case where a
    config save races with a fresh load and the live settings already
    carry marker prefixes.

    First-boot ordering: get_master_key() lazily generates the master key
    when general.secrets_encryption_key is empty. We must call it ONCE up
    front and write the result into the snapshot we're about to persist;
    otherwise the lazy generator only mutates the live settings object,
    while the disk file gets `enc:v1:` ciphertext alongside an empty
    secrets_encryption_key - on next boot, decrypt_settings_in_place
    would generate a DIFFERENT master key, decryption would silently
    fail, and the application would start using the bad ciphertext as
    the credential.
    """
    out = deepcopy(plaintext_dict)

    # Resolve the master key once and pass it explicitly to every
    # encrypt_secret call so the snapshot's general.secrets_encryption_key
    # ends up consistent with the ciphertext we generate from it.
    master_key = _crypto.get_master_key()
    out.setdefault("general", {})
    if not out["general"].get("secrets_encryption_key"):
        out["general"]["secrets_encryption_key"] = master_key

    for path in at_rest_secret_paths():
        try:
            section_key, attr, value = _read_section_key(out, path)
        except (KeyError, ValueError):
            # ValueError is a malformed registry path (no section.key shape
            # to split): skipped rather than raised, because the value it
            # names cannot be walked anyway and the failure must not cost
            # the save. The tolerance ends at the lookup: an encryption
            # failure raises instead, so the save refuses rather than
            # return the credential in the payload as plaintext.
            continue
        if isinstance(value, str) and value:
            out[section_key][attr] = encrypt_secret(value, master_key=master_key)
        elif isinstance(value, bool):
            if path in manifest_boolean_secret_paths():
                # A field the active manifest declares boolean holds a
                # stored toggle, whether or not it is a secret:
                # stringifying its False would hand the plugin the truthy
                # text, so the value is left exactly as it is.
                continue
            # A credential an older save coerced into a boolean ("true"
            # that became True): stored as the typed text from now on,
            # in the lowercase form the settings write path round-trips,
            # so it encrypts at rest and masks in the API like every
            # other credential. The detection pass flags exactly these
            # values, so the rewrite must cover them too or it would
            # never converge.
            out[section_key][attr] = encrypt_secret(
                _coerced_secret_text(value), master_key=master_key)
        elif value is not None and not isinstance(value, (str, list, dict)):
            # A typed token an older save coerced into a number (a token
            # like 001234567890123456): stored as the typed text from
            # now on, so it encrypts at rest and masks in the API like
            # every other credential.
            out[section_key][attr] = encrypt_secret(str(value), master_key=master_key)
        elif isinstance(value, (list, dict)):
            # A collection an earlier write left at a registered secret
            # path can be neither encrypted at rest nor covered, and the
            # detection pass flags it as exactly that: carrying it into
            # the payload would write the credential in the clear, so
            # the save refuses instead. An empty collection is not a
            # credential, the same empty the string rule leaves alone.
            if value:
                raise ValueError(
                    f"The setting {path} holds a list or a dictionary "
                    f"where a registered secret path lives, which the "
                    f"secret store cannot encrypt; the save refuses "
                    f"rather than return the credential in the payload "
                    f"as plaintext"
                )

    for path in USER_VISIBLE_SECRET_LISTS:
        try:
            section_key, attr, value = _read_section_key(out, path)
        except (KeyError, ValueError):
            continue
        if isinstance(value, list):
            out[section_key][attr] = [
                encrypt_secret(v, master_key=master_key) if isinstance(v, str) and v else v
                for v in value
            ]
    return out


def preserve_recoverable_ciphertexts(encrypted_payload: Dict[str, Any],
                                     plaintext_snapshot: Dict[str, Any],
                                     on_disk_dict: Dict[str, Any]) -> None:
    """Keep the recoverable ciphertext of every path that failed to decrypt
    at boot, unless a new value replaces it.

    The boot pass withholds a failed value as an empty string, so without
    this step the next save would overwrite recoverable ciphertext with
    that empty value. A live value that is not empty means the operator
    re-entered the credential, and the fresh encryption of that value is
    kept instead. A list-shaped path keeps the ciphertext of each element
    that failed to decrypt, re-appended to the fresh list: an unrelated
    save must not cost the element that would decrypt again once the right
    master key is back.

    A path a save carried (new value or explicit clear) is removed from
    the failure set before the write, so what the user just did is what
    lands on disk.

    `encrypted_payload` is the dict about to be persisted; `on_disk_dict`
    is the file's current content, both keyed as `_write_config` builds
    them.
    """
    for path in sorted(_FAILED_DECRYPT_PATHS):
        try:
            live_key, live_attr, live = _read_section_key(plaintext_snapshot, path)
        except (KeyError, ValueError):
            continue
        if isinstance(live, list):
            # The recorded failed elements join the fresh list. The on-disk
            # comparison below is the source for paths recorded before this
            # process started keeping per-element records.
            recorded = list(_FAILED_DECRYPT_ELEMENTS.get(path) or [])
            if not recorded:
                continue
        elif isinstance(live, str) and live:
            # Re-entered credential: the fresh cipher wins.
            continue
        elif live not in (None, ""):
            continue
        try:
            _section_key, _attr, stored = _read_section_key(on_disk_dict, path)
        except (KeyError, ValueError):
            continue
        if isinstance(live, list):
            # The payload's freshly encrypted list, plus the withheld
            # ciphertext elements.
            try:
                payload_key, payload_attr, payload_value = _read_section_key(encrypted_payload, path)
            except (KeyError, ValueError):
                continue
            if not isinstance(payload_value, list):
                continue
            encrypted_payload[payload_key][payload_attr] = payload_value + recorded
            continue
        if isinstance(stored, str) and not stored:
            continue
        if isinstance(stored, list) and not stored:
            continue
        if not isinstance(stored, (str, list)):
            continue
        # _read_section_key matched the payload's casing for us.
        try:
            payload_key, payload_attr, _ = _read_section_key(encrypted_payload, path)
        except (KeyError, ValueError):
            continue
        encrypted_payload[payload_key][payload_attr] = stored


def decrypt_settings_dict(encrypted_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Symmetric counterpart of encrypt_settings_dict. Returns a new dict
    where every at-rest secret path (static tiers plus registered plugin
    secrets) and every secret list is decrypted to plaintext.

    Used by `write_config()` to compare the in-memory plaintext settings
    against the on-disk dict in plaintext form. Without this normalization
    the comparison would always differ (encrypt_secret is non-deterministic
    by design), and write_config would rewrite the file on every save
    even when nothing changed.

    Decrypt failures (corrupt cipher, wrong master key) leave the value
    untouched so the comparison fails safely - the rewrite proceeds, and
    preserve_recoverable_ciphertexts decides whether the on-disk cipher
    survives it.
    """
    out = deepcopy(encrypted_dict)
    for path in at_rest_secret_paths():
        try:
            section_key, attr, value = _read_section_key(out, path)
            if isinstance(value, str) and is_encrypted(value):
                try:
                    out[section_key][attr] = decrypt_secret(value)
                except ValueError:
                    pass  # leave the bad cipher in place; compare will diff
        except (KeyError, ValueError):
            # ValueError is a malformed registry path (no section.key shape
            # to split): skipped, so an unsplittable path never costs the
            # comparison the whole save depends on.
            continue

    for path in USER_VISIBLE_SECRET_LISTS:
        try:
            section_key, attr, value = _read_section_key(out, path)
            if isinstance(value, list):
                new_items = []
                for v in value:
                    if isinstance(v, str) and is_encrypted(v):
                        try:
                            new_items.append(decrypt_secret(v))
                        except ValueError:
                            new_items.append(v)
                    else:
                        new_items.append(v)
                out[section_key][attr] = new_items
        except (KeyError, ValueError):
            continue
    return out
