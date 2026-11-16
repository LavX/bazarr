from unittest.mock import patch, MagicMock

import pytest

from subtitles import pool


@pytest.fixture(autouse=True)
def blacklist_database(schema_session, monkeypatch):
    """Pool construction reads the blacklist tables. Give it an empty schema of
    its own instead of the application database, which only exists once an
    earlier test or the app has created it."""
    from radarr import blacklist as radarr_blacklist
    from sonarr import blacklist as sonarr_blacklist

    monkeypatch.setattr(radarr_blacklist, "database", schema_session)
    monkeypatch.setattr(sonarr_blacklist, "database", schema_session)


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
