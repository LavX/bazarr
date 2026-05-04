from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture(autouse=True)
def _secrets():
    from app.config import settings
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["file_id_ttl_seconds"] = 3600
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 300
    yield


def test_fetch_subtitle_bytes_returns_empty_on_missing_content():
    """P0: plugin's blocklist signal is 200 + empty body. Previous
    behavior raised FileNotFoundError -> route returned 404 -> plugin
    treated as transient and retried forever."""
    from compat import service
    sub = MagicMock()
    sub.download_link = "https://safe.example.com/sub.srt"
    sub.url = None
    sub.provider_name = "os"
    sub.content = None  # provider returned nothing
    with patch("compat.service.resolve_safe_url"), \
         patch("compat.service.assert_safe_outbound"), \
         patch("compat.service._get_compat_pool") as pool:
        pool.return_value.download_subtitle = MagicMock(return_value=None)
        out = service._fetch_subtitle_bytes(sub)
    assert out == b""


def test_fetch_subtitle_bytes_still_raises_for_none_sub():
    """sub=None is a genuine 404 (file_id unknown). Keep that path."""
    from compat import service
    with pytest.raises(FileNotFoundError):
        service._fetch_subtitle_bytes(None)
