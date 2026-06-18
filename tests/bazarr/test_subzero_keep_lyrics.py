# coding=utf-8
# Helper coverage for the preserve-song-lyrics option.
# See https://github.com/LavX/bazarr/issues/225
import pytest

from app.config import settings
from arr_instances import resolution
from subtitles.tools.mods import get_subzero_mods, has_remove_hi, with_keep_lyrics


def test_has_remove_hi_plain():
    assert has_remove_hi(["remove_HI", "OCR_fixes"])


def test_has_remove_hi_parameterized():
    assert has_remove_hi(["remove_HI(keep_lyrics=1)", "common"])


def test_has_remove_hi_absent():
    assert not has_remove_hi(["OCR_fixes", "common"])
    assert not has_remove_hi([])
    assert not has_remove_hi(None)


def test_with_keep_lyrics_off_is_noop(monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", False)
    assert with_keep_lyrics(["remove_HI", "common"]) == ["remove_HI", "common"]


def test_with_keep_lyrics_on_rewrites_remove_hi(monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    assert with_keep_lyrics(["remove_HI", "common"]) == [
        "remove_HI(keep_lyrics=1)",
        "common",
    ]


def test_with_keep_lyrics_on_without_remove_hi_is_noop(monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    assert with_keep_lyrics(["common", "OCR_fixes"]) == ["common", "OCR_fixes"]


def test_with_keep_lyrics_empty(monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    assert with_keep_lyrics([]) == []


# --- Per-instance overrides (#227) -----------------------------------------
# resolve_subtitle_setting checks the in-memory cache before any DB read, so a
# seeded cache entry exercises the real per-instance branch without a session.
@pytest.fixture
def seed_instance_settings():
    seeded = []

    def _seed(blob, instance_id=4242):
        resolution._subtitle_settings_cache[instance_id] = blob
        seeded.append(instance_id)
        return instance_id

    yield _seed
    resolution.clear_subtitle_settings_cache()


def test_with_keep_lyrics_instance_override_on_beats_global_off(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", False)
    iid = seed_instance_settings({"general": {"subzero_mods_keep_lyrics": True}})
    assert with_keep_lyrics(["remove_HI", "common"], iid) == [
        "remove_HI(keep_lyrics=1)",
        "common",
    ]


def test_with_keep_lyrics_instance_override_off_beats_global_on(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    iid = seed_instance_settings({"general": {"subzero_mods_keep_lyrics": False}})
    assert with_keep_lyrics(["remove_HI", "common"], iid) == ["remove_HI", "common"]


def test_with_keep_lyrics_instance_without_key_falls_back_to_global(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    iid = seed_instance_settings({"general": {}})
    assert with_keep_lyrics(["remove_HI"], iid) == ["remove_HI(keep_lyrics=1)"]


def test_get_subzero_mods_uses_instance_list(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", False)
    iid = seed_instance_settings({"general": {"subzero_mods": ["remove_HI", "OCR_fixes"]}})
    assert get_subzero_mods(iid) == ["remove_HI", "OCR_fixes"]


def test_get_subzero_mods_instance_list_applies_instance_keep_lyrics(seed_instance_settings):
    iid = seed_instance_settings({"general": {
        "subzero_mods": ["remove_HI", "common"],
        "subzero_mods_keep_lyrics": True,
    }})
    assert get_subzero_mods(iid) == ["remove_HI(keep_lyrics=1)", "common"]


def test_get_subzero_mods_instance_without_override_uses_global(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods", "remove_HI,common")
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", False)
    iid = seed_instance_settings({"subsync": {"subsync_threshold": 90}})
    assert get_subzero_mods(iid) == ["remove_HI", "common"]


def test_get_subzero_mods_none_instance_uses_global(monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods", "remove_HI,common")
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", False)
    assert get_subzero_mods(None) == ["remove_HI", "common"]


# --- subtitles_apply_mods threads the owning instance (#227) ----------------
# The manual "apply mods" action lets the user pick the mod list, so only the
# keep-lyrics preference is instance-relevant. subtitles_apply_mods must resolve
# it against the media's owning instance instead of the global setting.
class _RecordingSubtitle:
    """Captures the mods Subtitle is built with, then short-circuits the write
    path by reporting no modified content (so no real file I/O happens)."""

    last_mods = None

    def __init__(self, lang_obj, mods=None, original_format=True):
        self.language = lang_obj
        self.mods = mods
        self.format = "srt"
        self.content = b""
        _RecordingSubtitle.last_mods = mods

    def is_valid(self):
        return True

    def get_modified_content(self, format=None):
        return None


@pytest.fixture
def captured_apply_mods(monkeypatch, tmp_path):
    from subtitles.tools import mods as mods_module

    _RecordingSubtitle.last_mods = None
    monkeypatch.setattr(mods_module, "Subtitle", _RecordingSubtitle)
    # alpha3_from_alpha2 needs the app-loaded languages_dict; stub it since the
    # language-code conversion is not the behaviour under test here.
    monkeypatch.setattr(mods_module, "alpha3_from_alpha2", lambda lang: "eng")
    srt = tmp_path / "movie.en.srt"
    srt.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")

    def _apply(arr_instance_id):
        mods_module.subtitles_apply_mods(
            language="en",
            subtitle_path=str(srt),
            mods=["remove_HI"],
            video_path=str(video),
            arr_instance_id=arr_instance_id,
        )
        return _RecordingSubtitle.last_mods

    return _apply


def test_subtitles_apply_mods_instance_override_on_beats_global_off(
    captured_apply_mods, seed_instance_settings, monkeypatch
):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", False)
    iid = seed_instance_settings({"general": {"subzero_mods_keep_lyrics": True}})
    assert captured_apply_mods(iid) == ["remove_HI(keep_lyrics=1)"]


def test_subtitles_apply_mods_instance_override_off_beats_global_on(
    captured_apply_mods, seed_instance_settings, monkeypatch
):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    iid = seed_instance_settings({"general": {"subzero_mods_keep_lyrics": False}})
    assert captured_apply_mods(iid) == ["remove_HI"]


def test_subtitles_apply_mods_none_instance_uses_global(captured_apply_mods, monkeypatch):
    monkeypatch.setattr(settings.general, "subzero_mods_keep_lyrics", True)
    assert captured_apply_mods(None) == ["remove_HI(keep_lyrics=1)"]
