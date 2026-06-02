# coding=utf-8


from subtitles.indexer.utils import (
    _language_code_from_sync_engine_output,
    add_combined_outputs,
    add_sync_engine_outputs,
    combined_modifier_from_subtitle_name,
    normalize_subtitle_language_variant,
    subtitle_language_with_combined_modifier,
)


class TestSyncedCombinedOrphan:
    def test_sync_parser_cleanly_skips_combined_chain(self):
        # Movie.en.combined-hu.ffsubsync.srt must NOT be mis-parsed as language
        # "combined-hu"; it is cleanly skipped (returns None).
        assert _language_code_from_sync_engine_output(
            "Movie.en.combined-hu.ffsubsync.srt"
        ) is None
        # A plain synced output still parses normally.
        assert _language_code_from_sync_engine_output(
            "Movie.en.ffsubsync.srt"
        ) == "en"

    def test_add_sync_engine_outputs_skips_combined_chain_without_error(self, tmp_path):
        (tmp_path / "Movie.en.combined-hu.ffsubsync.srt").write_text("")
        result = add_sync_engine_outputs(str(tmp_path), {})
        assert "Movie.en.combined-hu.ffsubsync.srt" not in result

    def test_add_combined_outputs_skips_synced_combined_chain(self, tmp_path):
        (tmp_path / "Movie.en.combined-hu.ffsubsync.srt").write_text("")
        result = add_combined_outputs(
            str(tmp_path), {}, video_filename="Movie.mkv"
        )
        assert "Movie.en.combined-hu.ffsubsync.srt" not in result


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

    def test_filters_to_current_video(self, tmp_path):
        # A multi-episode folder: only the current episode's combined file
        # should be picked up.
        (tmp_path / "Show.S01E01.en.combined-hu.srt").write_text("")
        (tmp_path / "Show.S01E02.en.combined-hu.srt").write_text("")
        result = add_combined_outputs(
            str(tmp_path), {}, video_filename="Show.S01E01.mkv"
        )
        assert "Show.S01E01.en.combined-hu.srt" in result
        assert "Show.S01E02.en.combined-hu.srt" not in result

    def test_does_not_cross_attach_prefix_sibling(self, tmp_path):
        # "Movie (2020)" must NOT pick up the Extended edition's combined file
        # even though its stem is a dotted prefix.
        (tmp_path / "Movie (2020).en.combined-hu.srt").write_text("")
        (tmp_path / "Movie (2020).Extended.en.combined-hu.srt").write_text("")
        result = add_combined_outputs(
            str(tmp_path), {}, video_filename="Movie (2020).mkv"
        )
        assert "Movie (2020).en.combined-hu.srt" in result
        assert "Movie (2020).Extended.en.combined-hu.srt" not in result


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
