# coding=utf-8

import pytest

from subtitles.language_profiles import (
    get_combine_rule,
    validate_combine_rule,
    CombineRuleError,
)


class TestValidateCombineRule:
    def test_accepts_two_language_rule(self):
        items = [{"language": "en"}, {"language": "hu"}]
        validate_combine_rule({"languages": ["en", "hu"], "format": "srt"}, items)

    def test_accepts_three_language_rule(self):
        items = [{"language": "en"}, {"language": "hu"}, {"language": "zh"}]
        validate_combine_rule(
            {"languages": ["en", "hu", "zh"], "format": "ass"}, items,
        )

    def test_rejects_single_language(self):
        items = [{"language": "en"}]
        with pytest.raises(CombineRuleError, match="2 to 3"):
            validate_combine_rule({"languages": ["en"], "format": "srt"}, items)

    def test_rejects_four_languages(self):
        items = [{"language": x} for x in ["en", "hu", "zh", "de"]]
        with pytest.raises(CombineRuleError, match="2 to 3"):
            validate_combine_rule(
                {"languages": ["en", "hu", "zh", "de"], "format": "srt"}, items,
            )

    def test_rejects_unknown_language(self):
        items = [{"language": "en"}, {"language": "hu"}]
        with pytest.raises(CombineRuleError, match="not in profile items"):
            validate_combine_rule(
                {"languages": ["en", "de"], "format": "srt"}, items,
            )

    def test_rejects_unknown_format(self):
        items = [{"language": "en"}, {"language": "hu"}]
        with pytest.raises(CombineRuleError, match="format"):
            validate_combine_rule(
                {"languages": ["en", "hu"], "format": "vtt"}, items,
            )


class TestGetCombineRule:
    def test_returns_none_when_absent(self):
        assert get_combine_rule({"items": []}) is None

    def test_returns_rule_when_present(self):
        rule = get_combine_rule({
            "items": [{"language": "en"}, {"language": "hu"}],
            "combine": {"languages": ["en", "hu"], "format": "srt"},
        })
        assert rule == {"languages": ["en", "hu"], "format": "srt"}

    def test_skips_invalid_rule_silently(self):
        rule = get_combine_rule({
            "items": [{"language": "en"}],
            "combine": {"languages": ["en", "hu"], "format": "srt"},
        })
        assert rule is None


class TestGetProfileCutoffWithCombineColumn:
    def test_cutoff_does_not_crash_with_combine_key(self):
        # Regression: update_profile_id_list now yields a 'combine' key, so the
        # old positional .values() unpack in get_profile_cutoff raised
        # ValueError for every profile during indexing. Key access must tolerate
        # the extra key.
        from unittest.mock import patch
        from app.database import get_profile_cutoff

        profile = {
            "profileId": 1, "name": "p", "cutoff": 65535,
            "items": [{"id": 1, "language": "en"}],
            "mustContain": [], "mustNotContain": [], "originalFormat": None,
            "tag": None, "combine": {"languages": ["en", "hu"], "format": "srt"},
        }
        with patch("app.database.update_profile_id_list", return_value=[profile]):
            result = get_profile_cutoff(1)
        assert result == [{"id": 1, "language": "en"}]
