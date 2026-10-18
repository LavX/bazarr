"""ChineseTraditional must match Traditional track titles, not the Brazilian
Portuguese list the base class carries.

ffprobe_found feeds the embedded-track indexer (utilities/video_analyzer.py),
and the catalog's embeddedsubtitles provider classifies the same tracks by
title, so a mismatch here means the provider offers a track the indexer cannot
name, or the indexer names one the provider never offered.
"""
import pytest

from languages.custom_lang import BrazilianPortuguese, ChineseTraditional


@pytest.fixture
def chinese_traditional():
    return ChineseTraditional()


@pytest.fixture
def brazilian_portuguese():
    return BrazilianPortuguese()


@pytest.mark.parametrize("name", [
    "Traditional Chinese",
    "繁體中文",
    "CHT",
    "Big5",
    "zh-Hant",
    "正體字幕",
])
def test_chinese_traditional_matches_traditional_titles(chinese_traditional, name):
    assert chinese_traditional.ffprobe_found({"name": name}) is True


@pytest.mark.parametrize("name", [
    "Portuguese (Brazil)",
    "Brazilian",
    "pb",
    "pob",
    "简繁中文",
    "简体中文",
    "Dutch",
    "",
])
def test_chinese_traditional_rejects_other_titles(chinese_traditional, name):
    assert chinese_traditional.ffprobe_found({"name": name}) is False


@pytest.mark.parametrize("name", ["TC", "Traditional Chinese (TC)", "Movie.TC.1080p"])
def test_the_short_tc_token_matches_only_as_a_word(chinese_traditional, name):
    # "tc" as a plain substring classified "Dutch" as Traditional Chinese;
    # it still matches the title tokens it was added for.
    assert chinese_traditional.ffprobe_found({"name": name}) is True


def test_brazilian_portuguese_still_matches_its_own_titles(brazilian_portuguese):
    assert brazilian_portuguese.ffprobe_found({"name": "Portuguese (Brazil)"}) is True
    assert brazilian_portuguese.ffprobe_found({"name": "Brazilian"}) is True
    assert brazilian_portuguese.ffprobe_found({"name": "Traditional Chinese"}) is False


def test_chinese_traditional_does_not_share_the_base_list(chinese_traditional):
    """The regression this pins: the inherited list made a "pb" title classify
    as Traditional, and Traditional titles classify as nothing."""
    assert "pb" not in ChineseTraditional._possible_matches
    assert "pob" not in ChineseTraditional._possible_matches
