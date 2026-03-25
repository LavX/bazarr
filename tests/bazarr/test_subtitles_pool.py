from unittest.mock import patch, MagicMock

from bazarr.subtitles import pool


def test_init_pool():
    with patch("bazarr.subtitles.pool.provider_pool") as mock_pool:
        mock_pool.return_value = MagicMock()
        assert pool._init_pool("movie")


def test_pool_update():
    with patch("bazarr.subtitles.pool.provider_pool") as mock_pool:
        mock_pool.return_value = MagicMock()
        pool_ = pool._init_pool("movie")
        assert pool._pool_update(pool_, "movie")
