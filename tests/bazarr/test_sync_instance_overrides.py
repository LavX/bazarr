# coding=utf-8
# Per-instance subsync resolution (#227).
# _resolve_subsync_overrides resolves the audio-sync settings against the owning
# Sonarr/Radarr instance, while honouring any explicit caller-supplied values.
import pytest

from app.config import settings
from arr_instances import resolution
from subtitles.sync import _resolve_subsync_overrides


@pytest.fixture
def seed_instance_settings():
    def _seed(blob, instance_id=8888):
        resolution._subtitle_settings_cache[instance_id] = blob
        return instance_id

    yield _seed
    resolution.clear_subtitle_settings_cache()


def test_global_when_no_instance(monkeypatch):
    monkeypatch.setattr(settings.subsync, "use_subsync", True)
    monkeypatch.setattr(settings.subsync, "use_subsync_threshold", True)
    monkeypatch.setattr(settings.subsync, "subsync_threshold", 96)
    monkeypatch.setattr(settings.subsync, "max_offset_seconds", 60)
    use_subsync, use_th, th, engines, max_off = _resolve_subsync_overrides(None, True, None, None)
    assert use_subsync is True and use_th is True and th == 96
    assert engines is None          # None => downstream global fallback preserved
    assert max_off == "60"          # coerced to string like the legacy default


def test_instance_use_subsync_override(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.subsync, "use_subsync", True)
    iid = seed_instance_settings({"subsync": {"use_subsync": False}})
    use_subsync, *_ = _resolve_subsync_overrides(iid, True, None, None)
    assert use_subsync is False


def test_series_vs_movie_threshold(seed_instance_settings):
    iid = seed_instance_settings({"subsync": {
        "use_subsync_threshold": True, "subsync_threshold": 70,
        "use_subsync_movie_threshold": True, "subsync_movie_threshold": 40,
    }})
    _, use_th_s, th_s, _, _ = _resolve_subsync_overrides(iid, True, None, None)
    _, use_th_m, th_m, _, _ = _resolve_subsync_overrides(iid, False, None, None)
    assert (use_th_s, th_s) == (True, 70)
    assert (use_th_m, th_m) == (True, 40)


def test_explicit_engines_win_over_instance(seed_instance_settings):
    iid = seed_instance_settings({"subsync": {"enabled_engines": ["alass"]}})
    _, _, _, engines, _ = _resolve_subsync_overrides(iid, True, ["ffsubsync"], None)
    assert engines == ["ffsubsync"]


def test_instance_engines_used_when_caller_passes_none(seed_instance_settings):
    iid = seed_instance_settings({"subsync": {"enabled_engines": ["alass", "ffsubsync"]}})
    _, _, _, engines, _ = _resolve_subsync_overrides(iid, True, None, None)
    assert engines == ["alass", "ffsubsync"]


def test_explicit_max_offset_wins(seed_instance_settings):
    iid = seed_instance_settings({"subsync": {"max_offset_seconds": 600}})
    _, _, _, _, max_off = _resolve_subsync_overrides(iid, True, None, "120")
    assert max_off == "120"


def test_instance_max_offset_used_when_caller_passes_none(seed_instance_settings):
    iid = seed_instance_settings({"subsync": {"max_offset_seconds": 300}})
    _, _, _, _, max_off = _resolve_subsync_overrides(iid, True, None, None)
    assert max_off == "300"
