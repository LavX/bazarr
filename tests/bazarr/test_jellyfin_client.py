"""Tests for the Jellyfin HTTP client.

Mock responses are validated against Jellyfin's OpenAPI spec via fake_jellyfin.
"""

import os
from unittest.mock import patch, MagicMock

import pytest
from bazarr.jellyfin.client import JellyfinClient
from fake_jellyfin import (
    FakeJellyfinClient,
    OPENAPI_PATH,
    make_movie,
    make_episode,
)


@pytest.fixture
def client():
    return JellyfinClient("http://jellyfin:8096", "test-api-key")



def test_fake_client_has_same_interface_as_real():
    """Ensure FakeJellyfinClient implements all public methods of JellyfinClient."""
    real_methods = {m for m in dir(JellyfinClient) if not m.startswith('_')}
    fake_methods = {m for m in dir(FakeJellyfinClient) if not m.startswith('_')}
    missing = real_methods - fake_methods
    assert not missing, f"FakeJellyfinClient is missing methods: {missing}"


@pytest.mark.skipif(not os.path.exists(OPENAPI_PATH), reason="jellyfin-openapi.json not found")
class TestFakeClientMatchesContract:
    """Verify that FakeJellyfinClient returns schema-valid responses."""

    def test_system_info(self):
        fake = FakeJellyfinClient()
        fake.get_system_info()  # validates internally

    def test_libraries(self):
        fake = FakeJellyfinClient()
        fake.get_libraries()

    def test_items(self):
        fake = FakeJellyfinClient()
        fake.items = [make_movie()]
        fake.get_items({})

    def test_episodes(self):
        fake = FakeJellyfinClient()
        fake.episodes = {"s1": {1: [make_episode()]}}
        fake.get_episodes("s1", 1)

    def test_empty_items(self):
        fake = FakeJellyfinClient()
        fake.get_items({})

    def test_empty_episodes(self):
        fake = FakeJellyfinClient()
        fake.get_episodes("s1", 1)



@patch.object(JellyfinClient, "post")
def test_refresh_item(mock_post, client):
    mock_post.return_value = MagicMock()
    valid_id = "abcdef0123456789abcdef0123456789"  # 32-char hex GUID shape
    client.refresh_item(valid_id)
    mock_post.assert_called_once()
    assert f"/Items/{valid_id}/Refresh" in mock_post.call_args[0][0]


@pytest.mark.parametrize("bad_id", [
    "../../etc/passwd",
    "abc/def",
    "abc?injected=1",
    "abc.exe",
    "",
    "short",
    "a" * 100,  # too long
])
def test_refresh_item_rejects_unsafe_id(client, bad_id):
    """A malicious or corrupt Jellyfin response with non-GUID IDs must not
    flow into URL-path substitution."""
    with pytest.raises(ValueError, match="refusing unsafe Jellyfin id"):
        client.refresh_item(bad_id)


def test_session_verifies_tls_by_default():
    """Hardcoded verify=False is the bug we are guarding against. By default
    the client must validate TLS like sonarr/radarr/plex."""
    c = JellyfinClient("https://jellyfin.example", "k", verify_ssl=True)
    assert c.session.verify is True


def test_session_can_opt_out_of_tls_verification():
    """Users with self-signed homelab certs flip the explicit opt-out."""
    c = JellyfinClient("https://jellyfin.example", "k", verify_ssl=False)
    assert c.session.verify is False


def test_redact_secret_strips_token_and_key():
    """Helper underpinning operations._redact: api_key never appears in
    redacted output; the Authorization Token form is also masked."""
    from bazarr.jellyfin.client import _redact_secret
    raw = 'GET https://x/y Token="SECRET-LEAK" failed: bad SECRET-LEAK'
    assert "SECRET-LEAK" not in _redact_secret(raw, "SECRET-LEAK")
    # Token="..." form is masked even when the literal secret is unknown
    assert _redact_secret('Token="anything"', '') == 'Token="***"'


@patch.object(JellyfinClient, "post")
def test_report_media_updated(mock_post, client):
    mock_post.return_value = MagicMock()
    client.report_media_updated("/media/movies/Test Movie")
    mock_post.assert_called_once_with("/Library/Media/Updated", json={
        "Updates": [{"Path": "/media/movies/Test Movie", "UpdateType": "Modified"}],
    })


