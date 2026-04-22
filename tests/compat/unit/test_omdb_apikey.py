"""Tests for the OMDB api-key resolver.

Upstream Bazarr's OMDB refiner uses a Python 2 `.decode('base64')` chain
that crashes on any Python 3 interpreter, which is why OMDB has been
silently dead in every Bazarr deployment for years. This module verifies
the Python-3-safe resolver honors a plain OMDB_API_KEY and can also
decode the legacy obfuscated envelope when present.
"""
import base64
import codecs
import zlib

import pytest

from subliminal_patch.refiners.omdb import _resolve_omdb_apikey


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("OMDB_API_KEY", raising=False)
    monkeypatch.delenv("U1pfT01EQl9LRVk", raising=False)


def test_plain_key_is_preferred(monkeypatch):
    monkeypatch.setenv("OMDB_API_KEY", "  abc123  ")
    assert _resolve_omdb_apikey() == "abc123"


def test_no_env_returns_none():
    assert _resolve_omdb_apikey() is None


def test_malformed_envelope_returns_none(monkeypatch):
    monkeypatch.setenv("U1pfT01EQl9LRVk", "not-valid-base16")
    assert _resolve_omdb_apikey() is None


def test_envelope_decodes_roundtrip(monkeypatch):
    # Build an envelope matching the upstream shape:
    #   plain + 'x' + discard -> base64 -> rot13 -> zlib -> base16
    inner = "deadbeefkeyxjunk"
    b64 = base64.b64encode(inner.encode("utf-8")).decode("utf-8")
    rot = codecs.encode(b64, "rot_13")
    compressed = zlib.compress(rot.encode("utf-8"))
    envelope = base64.b16encode(compressed).decode("utf-8")
    monkeypatch.setenv("U1pfT01EQl9LRVk", envelope)
    assert _resolve_omdb_apikey() == "deadbeefkey"


def test_plain_key_wins_over_envelope(monkeypatch):
    monkeypatch.setenv("OMDB_API_KEY", "plain")
    monkeypatch.setenv("U1pfT01EQl9LRVk", "whatever")
    assert _resolve_omdb_apikey() == "plain"
