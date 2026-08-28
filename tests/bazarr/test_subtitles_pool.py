from unittest.mock import patch, MagicMock

from subtitles import pool


def test_init_pool():
    with patch("subtitles.pool.provider_pool") as mock_pool:
        mock_pool.return_value = MagicMock()
        assert pool._init_pool("movie")


def test_pool_update():
    with patch("subtitles.pool.provider_pool") as mock_pool:
        mock_pool.return_value = MagicMock()
        pool_ = pool._init_pool("movie")
        assert pool._pool_update(pool_, "movie")


def test_init_pool_wires_the_adoption_gate():
    # The gate keeps a stale cached search result from resurrecting a
    # disabled or throttled provider via SZProviderPool.__getitem__ adoption.
    from app.get_providers import provider_is_usable

    with patch("subtitles.pool.provider_pool") as mock_pool:
        mock_pool.return_value = MagicMock()
        pool._init_pool("movie")
        kwargs = mock_pool.return_value.call_args.kwargs
        assert kwargs["adoption_gate"] is provider_is_usable
