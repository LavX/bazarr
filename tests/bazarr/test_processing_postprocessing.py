# coding=utf-8
# Per-instance post-processing resolution (#227).
# The helper resolves the six post-processing settings against the owning
# Sonarr/Radarr instance's overrides, falling back to the global config.
import pytest

from app.config import settings
from arr_instances import resolution
from subtitles.processing import _postprocessing_config


@pytest.fixture
def seed_instance_settings():
    def _seed(blob, instance_id=7777):
        resolution._subtitle_settings_cache[instance_id] = blob
        return instance_id

    yield _seed
    resolution.clear_subtitle_settings_cache()


def test_global_when_no_instance(monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing", True)
    monkeypatch.setattr(settings.general, "postprocessing_cmd", "/global.sh")
    monkeypatch.setattr(settings.general, "use_postprocessing_threshold", True)
    monkeypatch.setattr(settings.general, "postprocessing_threshold", 80)
    assert _postprocessing_config("series", None) == (True, "/global.sh", True, 80)


def test_instance_override_series(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing", False)
    monkeypatch.setattr(settings.general, "postprocessing_cmd", "/global.sh")
    iid = seed_instance_settings({"general": {
        "use_postprocessing": True,
        "postprocessing_cmd": "/instance.sh",
        "use_postprocessing_threshold": True,
        "postprocessing_threshold": 60,
    }})
    assert _postprocessing_config("series", iid) == (True, "/instance.sh", True, 60)


def test_instance_movie_uses_movie_threshold(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing", True)
    monkeypatch.setattr(settings.general, "postprocessing_cmd", "/global.sh")
    iid = seed_instance_settings({"general": {
        "use_postprocessing_threshold_movie": True,
        "postprocessing_threshold_movie": 45,
    }})
    use_pp, cmd, use_th, th = _postprocessing_config("movie", iid)
    assert use_th is True and th == 45


def test_instance_without_threshold_override_falls_back_to_global(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing_threshold", True)
    monkeypatch.setattr(settings.general, "postprocessing_threshold", 75)
    iid = seed_instance_settings({"general": {"use_postprocessing": True}})
    use_pp, cmd, use_th, th = _postprocessing_config("series", iid)
    assert use_pp is True and use_th is True and th == 75


def test_threshold_coerced_to_int(seed_instance_settings, monkeypatch):
    # the global value may be stored as a string; the helper must return an int
    monkeypatch.setattr(settings.general, "postprocessing_threshold", "90")
    iid = seed_instance_settings({"general": {}})
    _, _, _, th = _postprocessing_config("series", iid)
    assert th == 90 and isinstance(th, int)
