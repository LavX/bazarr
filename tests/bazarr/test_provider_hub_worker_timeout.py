# -*- coding: utf-8 -*-
from types import SimpleNamespace

import pytest

import provider_hub.registry as registry
from provider_hub.registry import HubProxyProvider


WHISPER_CONFIG = {
    "endpoint": "http://127.0.0.1:9000",
    "ffmpeg_path": "ffmpeg",
    "pass_video_name": False,
    "response_timeout_seconds": 600,
    "transcription_timeout_seconds": 3600,
}


def _pin_global(monkeypatch, value):
    monkeypatch.setattr(registry, "_global_worker_timeout", lambda: float(value))


def test_worker_timeout_validator_default():
    from app.config import settings

    assert int(settings.general.provider_hub_worker_timeout) == 120


def test_declared_transcription_timeout_drives_deadline(monkeypatch):
    _pin_global(monkeypatch, 120)
    provider = HubProxyProvider(**WHISPER_CONFIG)
    # 3600 declared + 30 margin, floored by 120, under the 86400 cap.
    assert provider._request_timeout() == 3630.0


def test_no_declared_timeout_uses_global_default(monkeypatch):
    _pin_global(monkeypatch, 120)
    provider = HubProxyProvider(endpoint="http://x", api_key="secret")
    assert provider._request_timeout() == 120.0


def test_global_default_is_a_floor(monkeypatch):
    _pin_global(monkeypatch, 5000)
    provider = HubProxyProvider(**WHISPER_CONFIG)
    # derived 3630 < global default 5000 -> floor wins.
    assert provider._request_timeout() == 5000.0


def test_declared_timeout_clamped_to_cap(monkeypatch):
    _pin_global(monkeypatch, 120)
    # A plugin timeout above the cap is clamped to _MAX_WORKER_REQUEST_TIMEOUT
    # BEFORE the margin is added, so the margin survives (host wall outlives the
    # worker's own timeout rather than racing it).
    provider = HubProxyProvider(endpoint="http://x", transcription_timeout_seconds=90000)
    assert provider._request_timeout() == registry._MAX_WORKER_REQUEST_TIMEOUT + 30.0


def test_margin_preserved_at_the_cap(monkeypatch):
    _pin_global(monkeypatch, 120)
    # Exactly at the manifest maximum, the +30 host margin must not be swallowed
    # by the cap (regression guard: host deadline > worker's own timeout).
    provider = HubProxyProvider(endpoint="http://x", transcription_timeout_seconds=86400)
    assert provider._request_timeout() == 86430.0


def test_explicit_constructor_timeout_override(monkeypatch):
    _pin_global(monkeypatch, 120)
    # An explicit timeout= above the floor drives the deadline (this is the only
    # path exercising the self.timeout contribution to `declared`).
    provider = HubProxyProvider(endpoint="http://x", timeout=5000)
    assert provider._request_timeout() == 5030.0


def test_legacy_worker_timeout_key_still_honored(monkeypatch):
    _pin_global(monkeypatch, 120)
    provider = HubProxyProvider(endpoint="http://x", worker_timeout=900)
    assert provider._request_timeout() == 930.0


def test_legacy_and_suffix_keys_take_the_larger(monkeypatch):
    _pin_global(monkeypatch, 120)
    # Both timeout categories present with the legacy key larger, so a refactor
    # that let the *_timeout_seconds loop overwrite (rather than extend) the
    # declared list would drop to 630 and fail here.
    provider = HubProxyProvider(
        endpoint="http://x", worker_timeout=3600, transcription_timeout_seconds=600
    )
    assert provider._request_timeout() == 3630.0


def test_registry_cap_covers_the_validator_maximum():
    # The host cap must accommodate the largest value the setting/manifest allow
    # (validator lte and whisper manifest maximum are both 86400); otherwise a
    # user's configured timeout would be silently clipped below what they set.
    assert registry._MAX_WORKER_REQUEST_TIMEOUT >= 86400


def test_global_worker_timeout_reads_setting(monkeypatch):
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(general=SimpleNamespace(provider_hub_worker_timeout=500)),
        raising=False,
    )
    assert registry._global_worker_timeout() == 500.0


@pytest.mark.parametrize("bad_value", [None, 0, -5, "oops"])
def test_global_worker_timeout_falls_back(monkeypatch, bad_value):
    # Missing, zero, negative, and non-numeric all fall back to the default via
    # _coerce_timeout; the docstring promises this for "missing/invalid".
    general = SimpleNamespace() if bad_value is None else SimpleNamespace(
        provider_hub_worker_timeout=bad_value
    )
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(general=general),
        raising=False,
    )
    assert registry._global_worker_timeout() == 120.0
