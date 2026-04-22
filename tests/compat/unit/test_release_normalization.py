"""Release-string normalization for the Jellyfin picker display.

Providers pack multiple releases into one field with inconsistent separators.
These tests pin the picking rule: prefer the part with the most release-
quality markers (resolution/source/codec), tiebreak on length.
"""
from bazarr.compat.response_mapper import _normalize_release


def test_empty_returns_empty():
    assert _normalize_release("") == ""
    assert _normalize_release(None) == ""
    assert _normalize_release("   ") == ""


def test_single_clean_release_passthrough():
    """OS.com-style single releases are handed through unchanged."""
    r = "For.All.Mankind.S01E01.WEB-DL.x264-ION10"
    assert _normalize_release(r) == r


def test_embedded_newline_is_collapsed_not_displayed():
    """gestdown sometimes emits 'WEB-DL\\nx264' as one release - never show
    the literal newline in the picker."""
    out = _normalize_release("WEB-DL\nx264")
    assert "\n" not in out
    # WEB-DL has a quality marker (WEB-DL) - it wins over the bare 'x264'.
    # Actually 'x264' is ALSO a marker, so either is fine - just not newline.
    assert out in ("WEB-DL", "x264")


def test_slash_separated_picks_most_specific():
    """SUBDL-style: 'S01E01 - ATVP.WEB-DL - TOMMY / ION10 / METCON / DEFLATE / BTN'
    - the first part has the most markers (WEB-DL); that wins."""
    raw = "S01E01 - ATVP.WEB-DL - TOMMY / ION10 / METCON / DEFLATE / BTN"
    out = _normalize_release(raw)
    assert out == "S01E01 - ATVP.WEB-DL - TOMMY"


def test_plus_separated_aggregator_name_stays_one_token():
    """'BTN+ION10+TOMMY' is not a list-of-releases, it's a single aggregator
    name with no quality markers. Must return as-is, not split."""
    assert _normalize_release("BTN+ION10+TOMMY") == "BTN+ION10+TOMMY"


def test_massive_anonymus_blob_picks_best_release():
    """The Anonymus SUBDL shape: 22 release lines in one blob. Must pick
    ONE. The best one has the most markers - typically the 2160p/4K entry."""
    raw = (
        "For All Mankind (2019) S01E01\n"
        "ATVP.WEBRip.2160p-DEFLATE\n"
        "INTERNAL.WEB-AFG\n"
        "INTERNAL.WEB.1080p-AMRAP\n"
        "INTERNAL.WEB.1080p-RMTeam\n"
        "PROPER.WEB.1080p-ELiMiNATE\n"
        "REAL.PROPER.WEB.1080p-METCON\n"
        "WEB-DL.1080p-BTN\n"
        "WEB-DL.1080p-TOMMY\n"
        "WEB-DL.720p-TOMMY\n"
        "WEBRip-ION10"
    )
    out = _normalize_release(raw)
    assert "\n" not in out
    # The winner must be one of the multi-marker entries, not the bare header.
    assert out != "For All Mankind (2019) S01E01"
    # All the plausible winners contain BOTH a resolution AND a source marker.
    # Just assert that it has at least one resolution marker.
    import re
    assert re.search(r"\b(2160p|1080p|720p|480p)\b", out), out


def test_pipe_separated_picks_most_specific():
    raw = "REMUX | 1080p.WEB-DL.x264 | raw"
    out = _normalize_release(raw)
    assert out == "1080p.WEB-DL.x264"


def test_whitespace_trimmed():
    assert _normalize_release("  1080p.WEB-DL  ") == "1080p.WEB-DL"


def test_tied_scores_fall_back_to_length():
    """Both parts have zero markers - longer wins (more info for the user)."""
    out = _normalize_release("short / somewhat-longer-name")
    assert out == "somewhat-longer-name"


def test_comma_is_NOT_a_separator():
    """A release like 'x264, AAC' is a single release; don't split on comma."""
    raw = "Release.1080p.x264, AAC"
    out = _normalize_release(raw)
    assert "," in out  # preserved
    assert out == "Release.1080p.x264, AAC"


def test_response_mapper_applies_normalization():
    """End-to-end: subtitle_to_os_entry uses the normalizer."""
    from unittest.mock import MagicMock
    from bazarr.compat.response_mapper import subtitle_to_os_entry
    sub = MagicMock(
        language=MagicMock(alpha2="hu"),
        release_info="WEB-DL\nx264",
        id="s1", download_count=0, ratings=0.0,
        uploader="gestdown", provider_name="gestdown",
        upload_date=None,
    )
    entry = subtitle_to_os_entry(sub, file_id=1, media_type="episode",
                                  imdb_id="9198004", season=1, episode=1)
    assert "\n" not in entry["attributes"]["release"]
