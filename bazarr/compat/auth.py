from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
import uuid
from functools import wraps
from typing import Tuple

import jwt as pyjwt
from flask import jsonify, make_response, request

from app.config import settings


_XREASON_ALLOWED = frozenset({
    "auth", "not_found", "throttled", "compat-disabled",
    "bad-request", "upstream", "internal",
})


class CompatBootError(RuntimeError):
    """Raised when compat secrets fail the boot-time self-test."""


def validate_compat_token(supplied: str | None) -> bool:
    """Constant-time compare against settings.compat_endpoint.token. Never `==`."""
    if not supplied:
        return False
    expected = settings.compat_endpoint.token or ""
    if not expected:
        return False
    return hmac.compare_digest(str(supplied), str(expected))


def _get_jwt_secret() -> str:
    return settings.compat_endpoint.jwt_secret or ""


def mint_jwt(claims_extra: dict | None = None) -> str:
    secret = _get_jwt_secret()
    if len(secret) < 32:
        raise CompatBootError("jwt_secret missing or too short")
    now = int(time.time())
    claims = {
        "iat": now,
        "exp": now + int(settings.compat_endpoint.jwt_ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    if claims_extra:
        claims.update(claims_extra)
    return pyjwt.encode(claims, secret, algorithm="HS256")


def validate_jwt(token: str | None) -> Tuple[bool, dict]:
    if not token:
        return False, {}
    secret = _get_jwt_secret()
    if len(secret) < 32:
        return False, {}
    try:
        claims = pyjwt.decode(token, secret, algorithms=["HS256"])
    except pyjwt.PyJWTError:
        return False, {}
    from . import jwt_denylist
    jti = claims.get("jti")
    if jti and jwt_denylist.is_revoked(jti):
        return False, {}
    return True, claims


def revoke_jwt(jti: str, exp: int) -> None:
    """Add a jti to the server-side revocation denylist. Called by /logout."""
    from . import jwt_denylist
    jwt_denylist.revoke(jti, exp)


def boot_hmac_selftest() -> None:
    """Assert all three secrets exist, are >=32 chars, and can produce HMAC output.

    Called during blueprint registration when enabled=True. Aborts fail-closed.
    """
    for name in ("token", "jwt_secret", "file_id_secret"):
        val = getattr(settings.compat_endpoint, name, "") or ""
        if len(val) < 32:
            raise CompatBootError(f"compat_endpoint.{name} missing or <32 chars")
    # Known-plaintext HMAC: must produce non-empty bytes.
    for secret_name in ("jwt_secret", "file_id_secret"):
        secret = getattr(settings.compat_endpoint, secret_name).encode()
        tag = hmac.new(secret, b"selftest", hashlib.sha256).hexdigest()
        if not tag or len(tag) != 64:
            raise CompatBootError(f"HMAC self-test failed for {secret_name}")


def _hmac_sign(secret: bytes, payload_bytes: bytes) -> bytes:
    return hmac.new(secret, payload_bytes, hashlib.sha256).digest()


def mint_file_id(provider: str, native_id: str, language: str, release_info: str,
                 subtitle=None) -> int:
    """Allocate a server-side-mapped integer file_id.

    OS.com-compat clients require `files[].file_id` to be an int, so we can't
    return an HMAC token string. The payload (provider, native_id, language,
    and the actual Subtitle instance) is stashed in a TTL-bound in-memory
    store keyed by a monotonic counter; the int is what we hand to clients.
    Process restart flushes the store and 404s stale ids, which is fine
    because OS-compat clients always search immediately before downloading.

    The full Subtitle object is retained so that `/download/stream` can hand
    it straight to the pool's `download_subtitle(sub)`, which is the only
    reliable way to fetch content across providers.
    """
    from .file_id_store import get_store
    ttl = int(settings.compat_endpoint.file_id_ttl_seconds)
    release_hash = hashlib.sha1((release_info or "").encode()).hexdigest()[:10]
    payload = {"p": provider, "i": str(native_id), "l": str(language),
               "r": release_hash, "sub": subtitle}
    return get_store().put(payload, ttl)


def mint_local_file_id(*, path: str, lang: str, modifier: str | None,
                       fmt: str, media_type: str, media_id: int,
                       media_dir: str,
                       allowed_roots: list[str] | None = None) -> int:
    """Allocate a server-side-mapped int file_id for a locally-stored subtitle.

    Stash the resolved path + format + allowed-roots list so /download/stream
    can validate and serve without re-resolving from the DB. `allowed_roots`
    is the union of the media file's directory and any configured target
    folder (general.subfolder=='absolute'); serve_local rejects any path
    outside this list at stream time. Falls back to [media_dir] if the
    caller doesn't pre-compute the list (back-compat).
    """
    from .file_id_store import get_store
    ttl = int(settings.compat_endpoint.file_id_ttl_seconds)
    roots = [str(r) for r in (allowed_roots or [media_dir])]
    payload = {
        "kind": "local",
        "path": str(path),
        "lang": str(lang),
        "modifier": modifier,
        "fmt": str(fmt),
        "media_type": str(media_type),
        "media_id": int(media_id),
        "media_dir": str(media_dir),
        "allowed_roots": roots,
    }
    return get_store().put(payload, ttl)


def parse_file_id(fid) -> Tuple[bool, dict]:
    """Resolve an int (or digit string) file_id to its stored payload."""
    from .file_id_store import get_store
    return get_store().get(fid)


def mint_file_stream_token(file_id: int) -> str:
    """HMAC-sign a file_id into a short-lived stream token.

    The file_id points at a server-side payload including the Subtitle object;
    the HMAC ensures only tokens we minted can hit /download/stream, even
    though the file_id itself is a predictable monotonic int.
    """
    secret = (settings.compat_endpoint.file_id_secret or "").encode()
    exp = int(time.time()) + int(settings.compat_endpoint.stream_token_ttl_seconds)
    payload = {"fid": int(file_id), "exp": exp, "t": "s"}
    p_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = _hmac_sign(secret, p_bytes)
    return base64.urlsafe_b64encode(p_bytes + b"." + sig).decode().rstrip("=")


def parse_file_stream_token(token: str) -> Tuple[bool, dict]:
    """HMAC-verify a file-id-based stream token; returns payload with 'fid'."""
    try:
        padded = token + ("=" * (-len(token) % 4))
        raw = base64.urlsafe_b64decode(padded.encode())
        # Signature is always 32 bytes (SHA-256). Splitting on the last
        # b"." would silently corrupt the parse when the signature itself
        # happens to contain a 0x2e byte as its last byte - a ~0.4% chance
        # per mint that manifested as intermittent "stream token invalid"
        # failures under load.
        if len(raw) < 33 or raw[-33:-32] != b".":
            return False, {}
        p_bytes = raw[:-33]
        sig = raw[-32:]
    except Exception:
        return False, {}
    secret = (settings.compat_endpoint.file_id_secret or "").encode()
    if len(secret) < 32:
        return False, {}
    expected_sig = _hmac_sign(secret, p_bytes)
    if not hmac.compare_digest(sig, expected_sig):
        return False, {}
    try:
        payload = json.loads(p_bytes)
    except ValueError:
        return False, {}
    if int(payload.get("exp", 0)) < int(time.time()):
        return False, {}
    return True, payload


def mint_stream_token(provider: str, native_id: str) -> str:
    """Short-lived HMAC token used in the /download/stream/<token> URL. 5-min default.

    Kept as a signed stateless token (rather than store-backed like file_id)
    because the stream URL is opaque to the client and doesn't need to be an
    int; signing means /stream is valid across restarts for the lifetime of
    the TTL."""
    secret = (settings.compat_endpoint.file_id_secret or "").encode()
    exp = int(time.time()) + int(settings.compat_endpoint.stream_token_ttl_seconds)
    payload = {"p": provider, "i": str(native_id), "exp": exp, "t": "s"}
    p_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = _hmac_sign(secret, p_bytes)
    return base64.urlsafe_b64encode(p_bytes + b"." + sig).decode().rstrip("=")


def parse_stream_token(token: str) -> Tuple[bool, dict]:
    """HMAC-verify a stream token and check its exp claim."""
    try:
        padded = token + ("=" * (-len(token) % 4))
        raw = base64.urlsafe_b64decode(padded.encode())
        # Fixed-length split: signature is always 32 bytes (SHA-256).
        # rsplit(b".", 1) breaks when the signature ends in 0x2e.
        if len(raw) < 33 or raw[-33:-32] != b".":
            return False, {}
        p_bytes = raw[:-33]
        sig = raw[-32:]
    except Exception:
        return False, {}
    secret = (settings.compat_endpoint.file_id_secret or "").encode()
    if len(secret) < 32:
        return False, {}
    expected_sig = _hmac_sign(secret, p_bytes)
    if not hmac.compare_digest(sig, expected_sig):
        return False, {}
    try:
        payload = json.loads(p_bytes)
    except ValueError:
        return False, {}
    if int(payload.get("exp", 0)) < int(time.time()):
        return False, {}
    return True, payload


def compat_error(message: str, status: int, x_reason: str):
    """Uniform error response: {message} body + x-reason header + application/json (B13)."""
    if x_reason not in _XREASON_ALLOWED:
        raise ValueError(f"x-reason {x_reason!r} not whitelisted")
    resp = make_response(jsonify({"message": message}), status)
    resp.headers["x-reason"] = x_reason
    resp.headers["Content-Type"] = "application/json"
    return resp


def compat_auth(require_jwt: bool = False):
    """Standalone decorator. MUST NOT call bazarr/api/utils.py::authenticate.

    Status-code contract (matters for plugin retry loops):
      - Api-Key missing / invalid: 403 Forbidden. The plugin MUST NOT
        interpret this as a JWT-expiry signal; otherwise it'll clear its
        JWT and retry in a loop that can't recover.
      - JWT missing / invalid / expired: 401 Unauthorized. This IS the
        signal the plugin uses to re-login (clear token + POST /login).
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            api_key = request.headers.get("Api-Key") or request.headers.get("X-Api-Key")
            if not api_key:
                return compat_error("Missing API key", 403, "auth")
            if not validate_compat_token(api_key):
                return compat_error("Invalid API key", 403, "auth")
            if require_jwt:
                bearer = (request.headers.get("Authorization") or "")
                if not bearer.startswith("Bearer "):
                    return compat_error("Authorization header required", 401, "auth")
                ok, _ = validate_jwt(bearer[7:])
                if not ok:
                    return compat_error("Token invalid or expired", 401, "auth")
            return fn(*args, **kwargs)
        return wrapper
    return decorator
