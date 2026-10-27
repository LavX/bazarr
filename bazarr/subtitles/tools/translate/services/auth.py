import hashlib
import hmac

from app.config import settings

AUTH_MESSAGE = b"subtitle-translator-auth-v1"


def get_translator_auth_headers(encryption_key=None):
    """Build auth headers for requests to the AI Subtitle Translator.

    Computes HMAC-SHA256(encryption_key, "subtitle-translator-auth-v1")
    and returns it as X-Auth-Token. Returns empty dict when no key is
    configured (backwards-compatible).

    ``encryption_key=None`` means "use the saved key". An explicit empty string
    is an answer of its own, encryption off, and does not fall back to the saved
    key.
    """
    key = settings.translator.openrouter_encryption_key if encryption_key is None else encryption_key
    if not key:
        return {}
    token = hmac.new(
        bytes.fromhex(key),
        AUTH_MESSAGE,
        hashlib.sha256,
    ).hexdigest()
    return {"X-Auth-Token": token}
