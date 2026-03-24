import base64
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_HEX_64_RE = re.compile(r'^[0-9a-fA-F]{64}$')


def validate_encryption_key(key_hex: str) -> bool:
    """Check if key_hex is a valid 64-character hex string."""
    return bool(_HEX_64_RE.match(key_hex))


def encrypt_api_key(api_key: str, encryption_key_hex: str) -> str:
    """Encrypt an API key using AES-256-GCM with the shared key.

    Args:
        api_key: The plaintext OpenRouter API key.
        encryption_key_hex: 64-char hex string (the shared key).

    Returns:
        Encrypted string in format "enc:base64data".

    Raises:
        ValueError: If encryption_key_hex is not a valid 64-char hex string.
    """
    if not validate_encryption_key(encryption_key_hex):
        raise ValueError("Encryption key must be exactly 64 hexadecimal characters")

    key = bytes.fromhex(encryption_key_hex)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, api_key.encode("utf-8"), None)
    encoded = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"enc:{encoded}"
