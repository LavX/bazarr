# coding=utf-8

"""Stream tokens for the subtitle editor's native HLS playback.

A native player (Safari, iOS) cannot add headers to the playlist and segment
requests the browser makes itself, so the editor asks for one of these tokens
over a header-authenticated call and puts it in the playlist URL as ``st``
instead of the API key. A token binds one media item, arr instance and audio
track, and carries an expiry that covers a whole playback. It is HMAC-signed
and stateless, encoded like the compat endpoint's stream token but with a
type tag of its own.

The signing key is derived from a random 32-byte secret generated once per
process, together with the installation's API key. The secret is held in
memory, never persisted or logged, and is created at import rather than on
the first mint: two concurrent first mints after a restart could each create
a lazy secret, and a token signed with the replaced one would fail its first
playlist request. A restart or an API key change invalidates every
outstanding token; the editor recovers through its media error handler and
waiting probes.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from app.config import settings

# The type tag distinguishing editor stream tokens from the compat
# endpoint's (whose tag is "s").
TOKEN_TYPE = 'e'

# Lifetime rule: the media's full duration plus 30 minutes, at least one
# hour, at most 12 hours, and 4 hours when the duration is unknown. Slower
# playback is covered by the editor's renewal, not by a longer token.
LIFETIME_MIN_SECONDS = 3600
LIFETIME_MAX_SECONDS = 12 * 3600
LIFETIME_UNKNOWN_DURATION_SECONDS = 4 * 3600
LIFETIME_DURATION_MARGIN_SECONDS = 30 * 60

# Test-only override: when BAZARR_EDITOR_STREAM_TOKEN_MAX_SECONDS holds an
# integer of at least 180, the lifetime is the smaller of that and the rule
# above, so it can shorten a token and never lengthen one. Below 180 seconds
# a fresh token would already sit inside the expiry margin the editor's
# waiting handler reacts to, and every reset would mint and reset again, in
# a loop. It is not a setting and is not documented for users.
_OVERRIDE_ENV = 'BAZARR_EDITOR_STREAM_TOKEN_MAX_SECONDS'
_OVERRIDE_MIN_SECONDS = 180

# Created once per process at import, never persisted or logged.
_SECRET = secrets.token_bytes(32)


def _signing_key():
    """The HMAC key: the per-process secret bound to the current API key."""
    apikey = str(settings.auth.apikey or '')
    return hmac.new(_SECRET, apikey.encode(), hashlib.sha256).digest()


def _lifetime_override_seconds():
    raw = os.environ.get(_OVERRIDE_ENV)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < _OVERRIDE_MIN_SECONDS:
        return None
    return value


def stream_token_lifetime(duration_seconds=None):
    """The lifetime of a newly minted token, in seconds.

    The duration is the media's full duration from ffprobe. The test-only
    override can shorten the result, never lengthen it.
    """
    if duration_seconds is None:
        lifetime = LIFETIME_UNKNOWN_DURATION_SECONDS
    else:
        lifetime = duration_seconds + LIFETIME_DURATION_MARGIN_SECONDS
    lifetime = max(LIFETIME_MIN_SECONDS, min(LIFETIME_MAX_SECONDS, lifetime))
    override = _lifetime_override_seconds()
    if override is not None:
        lifetime = min(lifetime, override)
    return lifetime


def mint_stream_token(media_type, media_id, arr_instance_id, audio_track, lifetime_seconds):
    """Mint a token binding one media item, arr instance and audio track.

    The start time and the player parameter are not bound, so a new
    start-time session on the same track reuses the token. Nothing here
    logs; a token grants one media item's stream on one audio track for
    its lifetime and nothing else, but it is still a credential, and the
    formatter redacts only apikey values.
    """
    payload = {
        'k': TOKEN_TYPE,
        'm': media_type,
        'i': int(media_id),
        'a': None if arr_instance_id is None else int(arr_instance_id),
        't': int(audio_track),
        'e': int(time.time()) + int(lifetime_seconds),
    }
    p_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    sig = hmac.new(_signing_key(), p_bytes, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(p_bytes + b'.' + sig).decode().rstrip('=')


def parse_stream_token(token):
    """Verify a token's signature and expiry. Returns its payload or None.

    The signature is always 32 bytes (SHA-256), so the split is fixed-length:
    splitting on the last dot would corrupt the parse when the signature
    itself happens to contain a dot byte.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        padded = token + ('=' * (-len(token) % 4))
        raw = base64.urlsafe_b64decode(padded.encode())
        if len(raw) < 33 or raw[-33:-32] != b'.':
            return None
        p_bytes, sig = raw[:-33], raw[-32:]
    except Exception:
        return None
    expected = hmac.new(_signing_key(), p_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(p_bytes)
    except ValueError:
        return None
    if payload.get('k') != TOKEN_TYPE:
        return None
    if int(payload.get('e', 0)) < int(time.time()):
        return None
    return payload


def verify_stream_token(token, media_type, media_id, arr_instance_id, audio_track):
    """Whether the token was minted for exactly this media, instance and track.

    A compat stream token, a token minted before an API key change, an
    expired token and one minted for another media item, arr instance or
    audio track are all refused.
    """
    payload = parse_stream_token(token)
    if payload is None:
        return False
    try:
        minted_instance = payload.get('a')
        scope_instance = None if arr_instance_id is None else int(arr_instance_id)
        return (
            payload.get('m') == media_type
            and int(payload.get('i')) == int(media_id)
            and (None if minted_instance is None else int(minted_instance)) == scope_instance
            and int(payload.get('t')) == int(audio_track)
        )
    except (TypeError, ValueError):
        return False
