# coding=utf-8
# Helper coverage for the preserve-song-lyrics option.
# See https://github.com/LavX/bazarr/issues/225
from app.config import settings
from subtitles.tools.mods import has_remove_hi, with_keep_lyrics


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
