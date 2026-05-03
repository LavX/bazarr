from collections import defaultdict  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest

from subliminal_patch.core_persistent import download_best_subtitles


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.list_subtitles_prioritized.return_value = []
    pool.list_subtitles.return_value = []
    pool.download_best_subtitles.return_value = []
    return pool


@pytest.fixture
def mock_video():
    video = MagicMock()
    video.subtitle_languages = set()
    return video


def test_uses_prioritized_listing_by_default(mock_pool, mock_video):
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
        )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    mock_pool.list_subtitles.assert_not_called()


def test_uses_prioritized_listing_when_enabled(mock_pool, mock_video):
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
            use_provider_priority=True,
        )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    mock_pool.list_subtitles.assert_not_called()


def test_uses_regular_listing_when_disabled(mock_pool, mock_video):
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
            use_provider_priority=False,
        )

    mock_pool.list_subtitles.assert_called_once()
    mock_pool.list_subtitles_prioritized.assert_not_called()
