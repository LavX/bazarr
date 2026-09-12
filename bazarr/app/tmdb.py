# coding=utf-8
"""The built-in TMDB credential, shared by everything in the app that calls TMDB.

Bazarr+ ships its own read-only TMDB v3 API key so global metadata works with no
reader configuration at all: the login screen's backdrops and the whole Discover
feature both authenticate with it. Only one copy of the key and one resolution
rule exist, here, so the two surfaces can never drift apart.

The key stays server side. It travels as a v3 `api_key` query parameter on the
outbound request and never reaches a browser, a DTO, a log line or a cache key.
A reader who wants their own key can set one in Subtitle Hub, and the
environment override exists for a deployment that wants a dedicated key without
touching settings at all.
"""
import os
import re

# Built-in read-only TMDB v3 API key. This is the shared public key used across
# the Overseerr/Jellyseerr ecosystem, the same app-shipped service-key pattern
# Bazarr already uses for TVDB v4. Override at runtime with BAZARR_TMDB_API_KEY
# to point at a dedicated key.
BUILTIN_API_KEY = '431a8708161bcd1f1fbe7536137e61ed'

# A v3 API key is 32 hexadecimal characters. A v4 read access token is a JWT,
# which is longer, dotted and not hexadecimal, so the two are never confusable.
_V3_KEY = re.compile(r'[0-9a-fA-F]{32}')


def builtin_api_key():
    """Resolve the app's own key: environment override first, then the default."""
    return os.environ.get('BAZARR_TMDB_API_KEY', '').strip() or BUILTIN_API_KEY


def is_v3_key(value):
    """Whether a value can be used as a TMDB v3 API key at all."""
    return isinstance(value, str) and bool(_V3_KEY.fullmatch(value.strip()))


def api_key(override=None):
    """The key to authenticate with: a usable reader override, else the built-in.

    An override that is not usable as a v3 key is ignored rather than sent. A
    token stored during the v4 era is exactly that case, and a reader whose
    stored value predates this change should get working feeds rather than a
    failing request they did not ask for. The settings connection check is where
    they are told their entry will not be used.
    """
    candidate = override.strip() if isinstance(override, str) else ''
    return candidate if is_v3_key(candidate) else builtin_api_key()
