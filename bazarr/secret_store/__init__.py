# coding=utf-8
"""Central secret-storage module.

Named `secret_store` (not `secrets`) because the stdlib module `secrets`
already owns that name - shadowing it would break `import secrets` in
this package's own modules.

Owns:
- The master encryption key (`general.secrets_encryption_key`), generated
  on first boot and stored alongside `flask_secret_key`.
- A self-describing ciphertext format with a versioned marker prefix
  (`enc:v1:`) so the same field at rest can be plaintext during the
  migration window OR ciphertext post-migration without ambiguity.
- A registry that classifies every sensitive setting as either a
  user-visible credential (encrypted at rest, decrypted before the API
  serializes the settings tree) or a system secret (never leaves the
  backend regardless of encryption state).

Commits 2-4 wire this into the config read/write paths and the
`/api/system/settings` serializer. This commit is just the building
blocks plus their tests.
"""

from .crypto import (
    SECRET_MARKER_PREFIX,
    decrypt_secret,
    encrypt_secret,
    get_master_key,
    is_encrypted,
)
from .migration import (
    decrypt_settings_dict,
    decrypt_settings_in_place,
    encrypt_settings_dict,
    has_plaintext_secrets_on_disk,
    migrate_legacy_plex_encryption,
)
from .registry import (
    SYSTEM_SECRETS,
    USER_VISIBLE_SECRET_LISTS,
    USER_VISIBLE_SECRETS,
    is_system_secret,
    is_user_visible_secret,
    is_user_visible_secret_list,
)

__all__ = [
    "SECRET_MARKER_PREFIX",
    "SYSTEM_SECRETS",
    "USER_VISIBLE_SECRET_LISTS",
    "USER_VISIBLE_SECRETS",
    "decrypt_secret",
    "decrypt_settings_dict",
    "decrypt_settings_in_place",
    "encrypt_secret",
    "encrypt_settings_dict",
    "get_master_key",
    "has_plaintext_secrets_on_disk",
    "is_encrypted",
    "is_system_secret",
    "is_user_visible_secret",
    "is_user_visible_secret_list",
    "migrate_legacy_plex_encryption",
]
