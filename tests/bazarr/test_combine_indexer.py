# coding=utf-8


from subtitles.indexer.utils import (
    add_combined_outputs,
    combined_modifier_from_subtitle_name,
    normalize_subtitle_language_variant,
    subtitle_language_with_combined_modifier,
)


class TestCombinedModifierFromSubtitleName:
    def test_two_language(self):
        assert combined_modifier_from_subtitle_name(
            "Movie.en.combined-hu.srt"
        ) == "combined-hu"

    def test_three_language_ass(self):
        assert combined_modifier_from_subtitle_name(
            "Movie.de.combined-es-zh.ass"
        ) == "combined-es-zh"

    def test_regular_file(self):
        assert combined_modifier_from_subtitle_name("Movie.en.srt") is None

    def test_sync_engine_file(self):
        assert combined_modifier_from_subtitle_name("Movie.en.ffsubsync.srt") is None


class TestAddCombinedOutputs:
    def test_picks_up_combined_file(self, tmp_path):
        (tmp_path / "Movie.en.combined-hu.srt").write_text("")
        subtitles = {}
        result = add_combined_outputs(str(tmp_path), subtitles)
        assert "Movie.en.combined-hu.srt" in result

    def test_ignores_regular_file(self, tmp_path):
        (tmp_path / "Movie.en.srt").write_text("")
        subtitles = {}
        result = add_combined_outputs(str(tmp_path), subtitles)
        assert "Movie.en.srt" not in result

    def test_ignores_already_known(self, tmp_path):
        (tmp_path / "Movie.en.combined-hu.srt").write_text("")
        subtitles = {"Movie.en.combined-hu.srt": "something"}
        result = add_combined_outputs(str(tmp_path), subtitles)
        # Must not overwrite the existing entry.
        assert result["Movie.en.combined-hu.srt"] == "something"


class TestSubtitleLanguageWithCombinedModifier:
    def test_stamps_modifier(self):
        result = subtitle_language_with_combined_modifier("en", "Movie.en.combined-hu.srt")
        assert result == "en:combined-hu"

    def test_preserves_hi_then_appends(self):
        result = subtitle_language_with_combined_modifier(
            "en:hi", "Movie.en.combined-hu.srt"
        )
        assert result == "en:hi:combined-hu"

    def test_noop_for_non_combined(self):
        result = subtitle_language_with_combined_modifier("en", "Movie.en.srt")
        assert result == "en"


class TestNormalizeSubtitleLanguageVariantCombined:
    def test_preserves_combined_modifier(self):
        assert normalize_subtitle_language_variant("en:combined-hu") == "en:combined-hu"

    def test_preserves_hi_plus_combined_in_order(self):
        # hi/forced come before combined.
        assert normalize_subtitle_language_variant("en:combined-hu:hi") == "en:hi:combined-hu"

    def test_drops_unknown_modifier(self):
        # Unknown modifiers must still be dropped (PR 158 behavior preserved).
        assert normalize_subtitle_language_variant("en:bogus") == "en"
