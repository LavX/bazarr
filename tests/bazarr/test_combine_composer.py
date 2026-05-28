# coding=utf-8

import os

from subtitles.tools.combine.composer import compose

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "combine")


def fixture(name):
    return os.path.join(FIXTURES, name)


def test_srt_sibling_two_languages():
    out = compose(
        primary_path=fixture("en_hu_sibling_en.srt"),
        secondary_paths=[fixture("en_hu_sibling_hu.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    assert "Hello there." in text
    assert "Szia." in text
    assert text.count("-->") == 3
    cue1 = text.split("\n\n")[0]
    lines = [ln for ln in cue1.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert lines == ["Hello there.", "Szia."]


def test_srt_offset_alignment():
    out = compose(
        primary_path=fixture("en_hu_offset_en.srt"),
        secondary_paths=[fixture("en_hu_offset_hu.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    assert "Hello there." in text
    assert "Szia." in text
    cue1 = text.split("\n\n")[0]
    lines = [ln for ln in cue1.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert "Hello there." in lines
    assert "Szia." in lines


def test_srt_overlap_keeps_primary_only_when_no_match():
    out = compose(
        primary_path=fixture("en_hu_no_overlap_en.srt"),
        secondary_paths=[fixture("en_hu_no_overlap_hu.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    assert "Hello there." in text
    assert "How are you?" in text
    assert "Szia." not in text


def test_srt_trio_sibling():
    out = compose(
        primary_path=fixture("trio_en.srt"),
        secondary_paths=[fixture("trio_hu.srt"), fixture("trio_zh.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    cue1 = text.split("\n\n")[0]
    lines = [ln for ln in cue1.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert lines == ["Hello there.", "Szia.", "你好。"]
