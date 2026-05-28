# coding=utf-8

import os
import pysubs2

from subtitles.tools.combine.aligner import (
    AlignmentMode,
    detect_mode,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "combine")


def load(name):
    return pysubs2.load(os.path.join(FIXTURES, name), encoding="utf-8")


class TestDetectMode:
    def test_sibling_identical_timing(self):
        primary = load("en_hu_sibling_en.srt").events
        secondary = load("en_hu_sibling_hu.srt").events
        mode = detect_mode(primary, secondary)
        assert mode.kind == "sibling"
        assert mode.offset_ms == 0

    def test_offset_4_seconds(self):
        primary = load("en_hu_offset_en.srt").events
        secondary = load("en_hu_offset_hu.srt").events
        mode = detect_mode(primary, secondary)
        assert mode.kind == "offset"
        assert 3500 <= mode.offset_ms <= 4500

    def test_overlap_fallback(self):
        primary = load("en_hu_indep_en.srt").events
        secondary = load("en_hu_indep_hu.srt").events
        mode = detect_mode(primary, secondary)
        assert mode.kind == "overlap"

    def test_overlap_when_no_match(self):
        primary = load("en_hu_no_overlap_en.srt").events
        secondary = load("en_hu_no_overlap_hu.srt").events
        mode = detect_mode(primary, secondary)
        assert mode.kind == "overlap"
