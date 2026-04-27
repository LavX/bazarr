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
1. After dynaconf finishes loading config.yaml: walk USER_VISIBLE_SECRETS
   and decrypt anything carrying the marker prefix. After this runs, the
   live settings object holds plaintext for every credential.
2. Inside `write_config()`: snapshot settings as a dict, encrypt every
   USER_VISIBLE_SECRETS in the snapshot, persist that. Only the
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
from .registry import USER_VISIBLE_SECRET_LISTS, USER_VISIBLE_SECRETS


# Plex legacy encryption fields. Pre-this-package, plex.apikey / plex.token
# were stored encrypted under plex.encryption_key (URLSafeSerializer with
# no marker prefix), and a sibling boolean plex.apikey_encrypted flagged
# the apikey as encrypted. The unified pipeline doesn't need any of that
# - encryption metadata is self-describing via the marker prefix - so on
# first boot we decrypt them with the legacy key and let write_config
# re-encrypt under the unified master key.
_PLEX_LEGACY_FIELDS = ("apikey", "token")

logger = logging.getLogger(__name__)


def _split_path(path: str) -> tuple[str, str]:
    """Split 'section.key' into ('section', 'key'). Always yields exactly
    two parts because the registry keys are flat dotted paths by contract."""
    section, _, key = path.partition(".")
    if not key:
        raise ValueError(f"Registry path {path!r} must be 'section.key'")
    return section, key


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
    URLSafeSerializer + plex.encryption_key scheme, and clear the
    accompanying apikey_encrypted boolean. Runs ONCE at boot, BEFORE
    decrypt_settings_in_place - the legacy ciphertext does NOT carry the
    enc:v1: marker, so without this conversion the unified pipeline
    would treat the legacy bytes as plaintext, the next write would
    re-encrypt them as if they were a credential, and the user's plex
    creds would be unrecoverable.

    The legacy plex.encryption_key stays in config.yaml (it's still a
    SYSTEM_SECRET masked by the API serializer) so the migration is
    one-way safe: a downgrade post-this-version can't read enc:v1:
    ciphertext anyway, so leaving the legacy key around is harmless and
    avoids a destructive deletion in the migration path.

    No-op on installs that never used the legacy scheme (apikey_encrypted
    absent or False).
    """
    plex = getattr(settings_obj, "plex", None)
    if plex is None:
        return

    apikey_encrypted = bool(plex.get("apikey_encrypted", False)) \
        if hasattr(plex, "get") else bool(getattr(plex, "apikey_encrypted", False))
    if not apikey_encrypted:
        return

    legacy_key = plex.get("encryption_key", "") if hasattr(plex, "get") \
        else getattr(plex, "encryption_key", "")
    if not legacy_key:
        # The flag is set but the key is missing - the install is in an
        # inconsistent state. Clear the flag so the unified pipeline
        # doesn't re-trigger this branch on every boot, and leave the
        # values in place for the user to recover from the Settings page.
        plex.apikey_encrypted = False
        logger.warning(
            "plex.apikey_encrypted=True but plex.encryption_key is empty; "
            "skipping legacy decryption. Reconfigure Plex from Settings."
        )
        return

    # Lazy import - keeps the secret_store package usable without a full
    # bazarr environment for the simpler tests.
    try:
        from api.plex.security import TokenManager  # noqa: PLC0415
    except Exception as e:  # pragma: no cover
        logger.error(
            f"Cannot load legacy Plex TokenManager for migration: "
            f"{type(e).__name__}; leaving legacy values in place."
        )
        return

    token_manager = TokenManager(legacy_key)
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
            plaintext = token_manager.decrypt(ciphertext)
        except Exception as e:
            # A genuine corruption / wrong-key situation. Don't crash
            # the boot; the user can re-paste the credential.
            logger.error(
                f"Legacy Plex decryption failed for plex.{field}: "
                f"{type(e).__name__}. Reconfigure Plex from Settings."
            )
            continue
        plex[field] = plaintext
        migrated_any = True

    # Always clear the legacy flag once we've handled the fields - leaving
    # it True would mean the next boot tries to legacy-decrypt plaintext.
    plex.apikey_encrypted = False
    if migrated_any:
        logger.info(
            "Migrated legacy-encrypted Plex credentials to the unified "
            "secret_store format. They will be re-persisted under "
            "general.secrets_encryption_key on next config save."
        )


def has_plaintext_secrets_on_disk(settings_obj) -> bool:
    """Return True iff at least one USER_VISIBLE_SECRETS path holds a
    non-empty string without the enc:v1: marker - i.e. somebody (manual
    edit, fresh upgrade, registry-expansion deploy) wrote a credential
    in clear text and write_config has not yet rewritten the file.

    Lets the bootstrap force a one-time write_config call so the
    encryption guarantee is deterministic - the operator does not have
    to remember to touch a Settings field to trigger migration.

    Reads from the LIVE settings object (which after dynaconf load
    matches what's on disk for fields with no marker), so this is the
    same source of truth that decrypt_settings_in_place / write_config
    operate on.
    """
    for path in USER_VISIBLE_SECRETS:
        try:
            section, key = _split_path(path)
            section_obj = getattr(settings_obj, section, None)
            if section_obj is None:
                continue
            current = section_obj.get(key) if hasattr(section_obj, "get") \
                else getattr(section_obj, key, None)
            if isinstance(current, str) and current and not is_encrypted(current):
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
    """Walk every USER_VISIBLE_SECRETS / USER_VISIBLE_SECRET_LISTS path in
    the live settings object and decrypt any value carrying the marker
    prefix. Plaintext values are left untouched (decrypt_secret is a
    passthrough on plaintext - that's the auto-migration story).

    Failures are logged but non-fatal: a corrupt single ciphertext should
    not stop bazarr from starting. The operator can rotate that credential
    via the Settings page; everything else continues to work.

    Runs exactly once at bootstrap, after settings.validators.validate_all
    finishes. Re-running is harmless (decrypt_secret is idempotent on
    already-plaintext values).
    """
    for path in USER_VISIBLE_SECRETS:
        try:
            section, key = _split_path(path)
            section_obj = getattr(settings_obj, section, None)
            if section_obj is None:
                continue
            current = section_obj.get(key) if hasattr(section_obj, "get") \
                else getattr(section_obj, key, None)
            if isinstance(current, str) and is_encrypted(current):
                decrypted = decrypt_secret(current)
                section_obj[key] = decrypted
        except Exception as e:
            # Don't print the credential value or section/key hint that could
            # be inferred to a specific provider in a public bug report.
            logger.error(
                f"Failed to decrypt secret at rest: {type(e).__name__}; "
                f"continuing with the value unchanged so the rest of bazarr "
                f"can boot. Rotate the credential to recover."
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
            new_items = []
            for item in current:
                if isinstance(item, str) and is_encrypted(item):
                    new_items.append(decrypt_secret(item))
                else:
                    new_items.append(item)
            section_obj[key] = new_items
        except Exception as e:
            logger.error(
                f"Failed to decrypt list-shaped secret at rest: "
                f"{type(e).__name__}; continuing unchanged."
            )


def encrypt_settings_dict(plaintext_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Return a new dict identical to `plaintext_dict` except that every
    USER_VISIBLE_SECRETS / USER_VISIBLE_SECRET_LISTS path is encrypted.

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
    the credential. (Codex P1 finding.)
    """
    out = deepcopy(plaintext_dict)

    # Resolve the master key once and pass it explicitly to every
    # encrypt_secret call so the snapshot's general.secrets_encryption_key
    # ends up consistent with the ciphertext we generate from it.
    master_key = _crypto.get_master_key()
    out.setdefault("general", {})
    if not out["general"].get("secrets_encryption_key"):
        out["general"]["secrets_encryption_key"] = master_key

    for path in USER_VISIBLE_SECRETS:
        try:
            section_key, attr, value = _read_section_key(out, path)
            if isinstance(value, str) and value:
                out[section_key][attr] = encrypt_secret(value, master_key=master_key)
        except KeyError:
            continue

    for path in USER_VISIBLE_SECRET_LISTS:
        try:
            section_key, attr, value = _read_section_key(out, path)
            if isinstance(value, list):
                out[section_key][attr] = [
                    encrypt_secret(v, master_key=master_key) if isinstance(v, str) and v else v
                    for v in value
                ]
        except KeyError:
            continue
    return out


def decrypt_settings_dict(encrypted_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Symmetric counterpart of encrypt_settings_dict. Returns a new dict
    where every USER_VISIBLE_SECRETS / USER_VISIBLE_SECRET_LISTS path is
    decrypted to plaintext.

    Used by `write_config()` to compare the in-memory plaintext settings
    against the on-disk dict in plaintext form. Without this normalization
    the comparison would always differ (encrypt_secret is non-deterministic
    by design), and write_config would rewrite the file on every save
    even when nothing changed.

    Decrypt failures (corrupt cipher, wrong master key) leave the value
    untouched so the comparison fails safely - the rewrite proceeds and
    overwrites the bad cipher with a fresh one.
    """
    out = deepcopy(encrypted_dict)
    for path in USER_VISIBLE_SECRETS:
        try:
            section_key, attr, value = _read_section_key(out, path)
            if isinstance(value, str) and is_encrypted(value):
                try:
                    out[section_key][attr] = decrypt_secret(value)
                except ValueError:
                    pass  # leave the bad cipher in place; compare will diff
        except KeyError:
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
        except KeyError:
            continue
    return out
