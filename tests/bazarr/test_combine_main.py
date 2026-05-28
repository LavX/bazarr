# coding=utf-8

import os
import shutil
import pytest
from unittest.mock import patch

from subtitles.tools.combine.main import (
    CombineResult,
    try_combine_for_video,
)
from subtitles.tools.combine.rules import SourcePaths


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "combine")


@pytest.fixture
def tmp_video(tmp_path):
    video = tmp_path / "Movie.mkv"
    video.write_text("")
    return str(video)


def make_sources(tmp_path, primary_src, secondary_src):
    p = tmp_path / "primary.srt"
    s = tmp_path / "secondary.srt"
    shutil.copy(os.path.join(FIXTURES, primary_src), p)
    shutil.copy(os.path.join(FIXTURES, secondary_src), s)
    return SourcePaths(primary=str(p), secondaries=[str(s)])


class TestTryCombine:
    @patch("subtitles.tools.combine.main.get_combine_rule")
    @patch("subtitles.tools.combine.main.resolve_source_paths")
    @patch("subtitles.tools.combine.main.compose_combined_filename")
    @patch("subtitles.tools.combine.main._write_history_row")
    @patch("subtitles.tools.combine.main._post_write")
    def test_builds_when_sources_present(
        self, mock_post_write, mock_history, mock_filename, mock_resolve, mock_rule,
        tmp_video, tmp_path,
    ):
        mock_rule.return_value = {"languages": ["en", "hu"], "format": "srt"}
        mock_resolve.return_value = make_sources(
            tmp_path, "en_hu_sibling_en.srt", "en_hu_sibling_hu.srt"
        )
        out_path = str(tmp_path / "Movie.en.combined-hu.srt")
        mock_filename.return_value = out_path

        result = try_combine_for_video(
            video_path=tmp_video, media_type="movies", radarr_id=42,
            languages=["en", "hu"], format="srt",
        )
        assert isinstance(result, CombineResult)
        assert result.status == "built"
        assert result.path == out_path
        assert os.path.exists(out_path)
        mock_history.assert_called_once()

    @patch("subtitles.tools.combine.main.get_combine_rule")
    @patch("subtitles.tools.combine.main._profile_for")
    def test_skips_when_no_rule(self, mock_profile, mock_rule, tmp_video):
        mock_profile.return_value = {"items": []}
        mock_rule.return_value = None
        result = try_combine_for_video(
            video_path=tmp_video, media_type="movies", radarr_id=42,
        )
        assert result.status == "skipped"
        assert result.reason == "no rule"

    @patch("subtitles.tools.combine.main.get_combine_rule")
    @patch("subtitles.tools.combine.main._profile_for")
    @patch("subtitles.tools.combine.main.resolve_source_paths")
    def test_skips_when_source_missing(
        self, mock_resolve, mock_profile, mock_rule, tmp_video,
    ):
        mock_profile.return_value = {"items": []}
        mock_rule.return_value = {"languages": ["en", "hu"], "format": "srt"}
        mock_resolve.return_value = None
        result = try_combine_for_video(
            video_path=tmp_video, media_type="movies", radarr_id=42,
        )
        assert result.status == "skipped"
        assert "missing" in result.reason

    @patch("subtitles.tools.combine.main.get_combine_rule")
    @patch("subtitles.tools.combine.main._profile_for")
    @patch("subtitles.tools.combine.main.resolve_source_paths")
    @patch("subtitles.tools.combine.main.compose_combined_filename")
    @patch("subtitles.tools.combine.main._write_history_row")
    @patch("subtitles.tools.combine.main.compose")
    def test_failed_compose_returns_failed(
        self, mock_compose, mock_history, mock_filename, mock_resolve,
        mock_profile, mock_rule, tmp_video, tmp_path,
    ):
        mock_profile.return_value = {"items": []}
        mock_rule.return_value = {"languages": ["en", "hu"], "format": "srt"}
        mock_resolve.return_value = make_sources(
            tmp_path, "en_hu_sibling_en.srt", "en_hu_sibling_hu.srt"
        )
        mock_filename.return_value = str(tmp_path / "out.srt")
        mock_compose.side_effect = ValueError("bad SRT")

        result = try_combine_for_video(
            video_path=tmp_video, media_type="movies", radarr_id=42,
        )
        assert result.status == "failed"
        assert "bad SRT" in result.error
        mock_history.assert_called_once()
