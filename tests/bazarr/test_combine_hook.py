# coding=utf-8

from unittest.mock import patch

from subtitles.processing import _trigger_combine


class TestTriggerCombine:
    @patch("subtitles.processing.try_combine_for_video")
    def test_calls_combine_for_movie(self, mock_combine):
        _trigger_combine(
            video_path="/movies/Movie.mkv",
            media_type="movies",
            radarr_id=42,
            series_id=None,
            episode_id=None,
        )
        mock_combine.assert_called_once_with(
            video_path="/movies/Movie.mkv",
            media_type="movies",
            radarr_id=42,
            sonarr_series_id=None,
            sonarr_episode_id=None,
        )

    @patch("subtitles.processing.try_combine_for_video")
    def test_calls_combine_for_episode(self, mock_combine):
        _trigger_combine(
            video_path="/tv/Show/S01E01.mkv",
            media_type="series",
            radarr_id=None,
            series_id=5,
            episode_id=99,
        )
        mock_combine.assert_called_once_with(
            video_path="/tv/Show/S01E01.mkv",
            media_type="series",
            radarr_id=None,
            sonarr_series_id=5,
            sonarr_episode_id=99,
        )

    @patch("subtitles.processing.try_combine_for_video")
    def test_swallows_exceptions(self, mock_combine):
        mock_combine.side_effect = RuntimeError("boom")
        # Should not raise.
        _trigger_combine(
            video_path="/movies/Movie.mkv",
            media_type="movies",
            radarr_id=42,
            series_id=None,
            episode_id=None,
        )
