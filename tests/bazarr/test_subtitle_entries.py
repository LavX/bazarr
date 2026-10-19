# coding=utf-8
"""The stored shape of one subtitles entry, and the codec an embedded one carries."""
import ast

import pytest


@pytest.mark.parametrize("raw, expected", [
    ("PGS", "pgs"),
    ("VobSub", "vobsub"),
    ("SubRip", "subrip"),
    ("MOV Text", "mov text"),
    ("  ASS ", "ass"),
    ("", None),
    ("   ", None),
    (None, None),
    (144, None),
])
def test_normalize_codec(raw, expected):
    from subtitles.indexer.entries import normalize_codec

    assert normalize_codec(raw) == expected


def test_embedded_entry_is_four_elements_and_round_trips_through_the_column():
    from subtitles.indexer.entries import embedded_entry, entry_codec

    stored = str([embedded_entry("en:hi", "PGS"), ["en", "/m/Movie.en.srt", 12]])
    parsed = ast.literal_eval(stored)

    assert parsed[0] == ["en:hi", None, None, "pgs"]
    assert entry_codec(parsed[0]) == "pgs"
    assert entry_codec(parsed[1]) is None


def test_unknown_codec_is_stored_as_none():
    from subtitles.indexer.entries import embedded_entry, entry_codec

    entry = embedded_entry("fr", None)

    assert entry == ["fr", None, None, None]
    assert entry_codec(entry) is None


@pytest.mark.parametrize("entry", [
    ["en", None, None],
    ["en", None],
    ["en"],
    [],
    ("en", None, None),
    "en",
    None,
    ["en", "/m/Movie.en.srt", 12, "subrip"],
])
def test_entry_codec_tolerates_legacy_and_malformed_entries(entry):
    from subtitles.indexer.entries import entry_codec

    assert entry_codec(entry) is None
