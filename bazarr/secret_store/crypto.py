# coding=utf-8
"""Symmetric AEAD crypto for secrets at rest.

Uses `cryptography.fernet.Fernet` - AES-128-CBC + HMAC-SHA256 in a single
authenticated primitive. Replaces an earlier signed-only design (Codex
flagged URLSafeSerializer.dumps as encoding-not-encryption: anyone with
config.yaml could base64-decode the JSON payload and recover the secret
without the master key).

Format:
- Stored value: `enc:v1:<urlsafe-base64 Fernet token>`
- Marker prefix is intentionally NOT cryptographic. An attacker with
  write access to config.yaml can drop a value without the marker; the
  read path treats anything without `enc:v1:` as plaintext (passthrough).
  We are protecting against accidental disclosure (logs, screenshots,
  support bundles), not a local attacker who already owns the host.
- Each encrypt produces a fresh ciphertext (Fernet bakes in a random IV
  + timestamp), so two encryptions of the same plaintext will not equal
  byte-for-byte. Defends against equality inference if config.yaml leaks.

Master key derivation:
- The master key on disk (`general.secrets_encryption_key`) can be any
  string - we generate it as `secrets.token_urlsafe(32)` (a base64-ish
  string), but a hand-edited master can be any length.
- Fernet itself requires a 32-byte key, urlsafe-b64-encoded. We derive
  it deterministically with SHA-256: `b64(sha256(master_key.utf8))`. This
  KDF is intentionally simple - the master IS already high-entropy
  random; we are not stretching a low-entropy passphrase.

Migration window:
- Predecessor format (also marker-prefixed `enc:v1:`) used
  URLSafeSerializer with a JSON payload (`{"secret": "...", "salt": ...,
  "ts": ...}`). Anything written before this commit reads as that shape.
- decrypt_secret first attempts Fernet decryption; on InvalidToken it
  falls back to the legacy URLSafeSerializer reader. Either way the
  caller gets plaintext. The next write_config rewrites all secrets in
  the new Fernet format.
"""

import base64
import hashlib
import logging
import secrets as _secrets

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadPayload, BadSignature, URLSafeSerializer

logger = logging.getLogger(__name__)

# Versioned marker. Bumping the suffix lets a future change rotate the
# format and still detect legacy ciphertext via the byte shape after the
# prefix (Fernet tokens vs URLSafeSerializer dumps look different).
SECRET_MARKER_PREFIX = "enc:v1:"

# Generated key length in bytes (token_urlsafe doubles to ~43 chars).
_MASTER_KEY_BYTES = 32


def _generate_master_key() -> str:
    return _secrets.token_urlsafe(_MASTER_KEY_BYTES)


def _fernet_key_from_master(master_key: str) -> bytes:
    """Derive a Fernet-shaped 32-byte key (urlsafe-b64 encoded) from a
    free-form master key. Deterministic; same master always maps to the
    same Fernet key, so encrypt/decrypt agree across boots.

    The master is already high-entropy random (`token_urlsafe(32)`), so
    SHA-256 is sufficient as a KDF here - we are NOT stretching a
    low-entropy passphrase. If a future version migrates to user-typed
    passphrases, swap this for HKDF / Argon2.
    """
    if not isinstance(master_key, str):
        raise TypeError("master_key must be a string")
    digest = hashlib.sha256(master_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_master_key(settings_obj=None) -> str:
    """Return the master encryption key, creating one if missing.

    Reads/writes `settings.general.secrets_encryption_key`. The key is
    generated on first call when the value is absent or empty - mirrors
    the `flask_secret_key` lifecycle, no extra config step for the user.

    `settings_obj` is the dynaconf settings root; defaults to
    `app.config.settings`. Injectable for tests so we don't need a real
    bazarr config to be loaded.
    """
    if settings_obj is None:
        from app.config import settings as settings_obj  # noqa: PLC0415, RUF100

    general = settings_obj.general
    key = getattr(general, "secrets_encryption_key", None)
    if not key or (isinstance(key, str) and not key.strip()):
        key = _generate_master_key()
        general.secrets_encryption_key = key
        logger.info("Generated new master secrets_encryption_key on first boot")
    return key


def is_encrypted(value) -> bool:
    """True iff `value` is a string carrying the SECRET_MARKER_PREFIX.

    Used by the read path to decide "decrypt this" vs "leave alone /
    auto-migrate". Non-string and empty values are NOT encrypted (an
    empty credential is just an empty credential, no migration needed).
    """
    return isinstance(value, str) and value.startswith(SECRET_MARKER_PREFIX)


def encrypt_secret(plaintext, master_key: str = None) -> str:
    """Encrypt `plaintext` and return a marker-prefixed Fernet token.

    Empty / falsy plaintext is returned unchanged - we don't waste bytes
    encrypting an empty string, and the read path treats empty as
    "credential not configured" anyway.
    """
    if not plaintext:
        return plaintext
    if not isinstance(plaintext, str):
        raise TypeError(f"encrypt_secret expects str, got {type(plaintext).__name__}")
    if is_encrypted(plaintext):
        # Caller passed an already-encrypted value through. Return as-is
        # so callers can be idempotent (re-encrypting on every save would
        # rotate ciphertext on every write for no reason).
        return plaintext

    if master_key is None:
        master_key = get_master_key()

    fernet = Fernet(_fernet_key_from_master(master_key))
    token = fernet.encrypt(plaintext.encode("utf-8"))
    return SECRET_MARKER_PREFIX + token.decode("ascii")


def decrypt_secret(value, master_key: str = None) -> str:
    """Decrypt `value` if it carries the marker, else return it as-is.

    Tolerant by design: the read path may encounter a freshly-installed
    value (plaintext, no marker) or an encrypted one. Both must work, so
    a value without the marker is returned unchanged. The auto-migrator
    is responsible for rewriting plaintext to ciphertext.

    Tries the current Fernet format first, then falls back to the legacy
    URLSafeSerializer payload shape (pre-AEAD migration) so installs
    written before this change keep working until the next write_config
    rewrites the file.

    Raises `ValueError` only on tampered / mis-keyed ciphertext (marker
    present, but neither Fernet nor the legacy reader accepts the
    payload). Callers that boot with the wrong master key see this and
    can surface a clear error instead of silently serving garbage.
    """
    if not isinstance(value, str) or not value:
        return value
    if not is_encrypted(value):
        return value

    if master_key is None:
        master_key = get_master_key()

    payload_text = value[len(SECRET_MARKER_PREFIX):]

    # Current format: Fernet (AES-128-CBC + HMAC-SHA256, authenticated).
    try:
        fernet = Fernet(_fernet_key_from_master(master_key))
        return fernet.decrypt(payload_text.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # ValueError catches malformed base64; fall through to legacy.
        pass

    # Legacy format: URLSafeSerializer.dumps({"secret": ..., "salt": ..., "ts": ...})
    # Pre-AEAD shipped ciphertext under the same marker prefix; decoding
    # both lets a writer-then-reader cycle migrate without manual steps.
    try:
        payload = URLSafeSerializer(master_key).loads(payload_text)
    except (BadSignature, BadPayload, ValueError) as e:
        raise ValueError(
            "Failed to decrypt secret: ciphertext was tampered with or the "
            "master key has changed. Inspect general.secrets_encryption_key "
            "in config.yaml."
        ) from e

    if not isinstance(payload, dict) or "secret" not in payload:
        raise ValueError("Decrypted payload has unexpected shape")
    return payload["secret"]
