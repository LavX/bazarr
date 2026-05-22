from collections import defaultdict  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest

from subliminal_patch.core_persistent import (
    download_best_subtitles,
    list_all_subtitles,
)


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


def test_list_all_subtitles_requests_exhaustive(mock_pool, mock_video):
    """Why: Manual search must query every provider; the exhaustive flag is the
    only thing that disables the early-exit in list_subtitles_prioritized.
    What: list_all_subtitles calls list_subtitles_prioritized with exhaustive=True.
    Test: Inspect the call kwargs and assert exhaustive=True.
    """
    languages = {MagicMock()}

    list_all_subtitles(
        videos={mock_video},
        languages=languages,
        pool_instance=mock_pool,
        min_score=80,
    )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    _, kwargs = mock_pool.list_subtitles_prioritized.call_args
    assert kwargs.get("exhaustive") is True
    assert kwargs.get("min_score") == 80


def test_download_best_subtitles_does_not_request_exhaustive(mock_pool, mock_video):
    """Why: Auto/scheduled downloads must keep the early-exit so we stop after
    the first provider that satisfies all languages above min_score.
    What: download_best_subtitles never passes exhaustive=True.
    Test: Inspect call_args and assert exhaustive is False (and definitely not True).
    """
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
        )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    _, kwargs = mock_pool.list_subtitles_prioritized.call_args
    assert kwargs.get("exhaustive", False) is False
