from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture(autouse=True)
def _set_secrets():
    """Set compat secrets via the dict-assignment path. DynaBox setattr
    doesn't reliably restore on teardown (monkeypatch's revert mutates
    the wrapper in a way Dynaconf's layered storage doesn't always see),
    causing flakes when tests run in different orders across the suite."""
    from app.config import settings
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["file_id_ttl_seconds"] = 3600
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 300
    yield


def test_download_returns_relative_link_by_default():
    """Relative link is the default: Bazarr+ can't know its public URL behind
    the supervisor proxy, so the client prepends the host it connected to."""
    from compat import service, auth
    from compat.file_id_store import reset_store
    reset_store()

    fid = auth.mint_file_id("opensubtitlescom", "123", "eng", "")
    assert isinstance(fid, int)
    resp = service.download(fid, remaining=999,
                             reset_iso="2099-01-01T00:00:00Z")
    assert resp["link"].startswith("/api/v1/download/stream/")
    assert resp["remaining_downloads"] == 999


def test_download_honors_base_host_when_provided():
    """Backwards-compat: callers that pass an explicit base_host get an
    absolute URL."""
    from compat import service, auth
    from compat.file_id_store import reset_store
    reset_store()
    fid = auth.mint_file_id("opensubtitlescom", "123", "eng", "")
    resp = service.download(fid, base_host="http://bazarr.local",
                             remaining=0, reset_iso="2099-01-01T00:00:00Z")
    assert resp["link"].startswith("http://bazarr.local/api/v1/download/stream/")


def test_download_rejects_invalid_file_id():
    from compat import service
    with pytest.raises(FileNotFoundError):
        service.download(999999999)
    with pytest.raises(FileNotFoundError):
        service.download("not-a-real-token")


def test_serve_subtitle_content_runs_ssrf_guard():
    """serve_subtitle_content resolves file_id -> subtitle -> _fetch_subtitle_bytes,
    which runs the SSRF guard against the subtitle's download URL before the
    provider dereferences it."""
    from compat import service, auth
    from compat.file_id_store import reset_store
    from utilities.url_guard import UnsafeURLError
    reset_store()
    fake_sub = MagicMock()
    fake_sub.download_link = "http://127.0.0.1/sub.srt"
    fake_sub.url = None
    fake_sub.provider_name = "opensubtitlescom"
    fid = auth.mint_file_id("opensubtitlescom", "123", "eng", "", subtitle=fake_sub)
    tok = auth.mint_file_stream_token(fid)
    with patch("compat.service.assert_safe_outbound") as guard:
        guard.side_effect = UnsafeURLError("loopback")
        with pytest.raises(UnsafeURLError):
            service.serve_subtitle_content(tok)


def test_serve_subtitle_content_rejects_invalid_token():
    from compat import service
    with pytest.raises(ValueError):
        service.serve_subtitle_content("not-a-valid-token")


def test_fetch_subtitle_bytes_invokes_guard_before_download():
    """_fetch_subtitle_bytes runs the SSRF guard (resolve_safe_url, which
    walks the redirect chain through assert_safe_outbound on every hop),
    calls pool.download_subtitle(sub), then returns sub.content bytes."""
    from compat import service
    fake_sub = MagicMock()
    fake_sub.download_link = "https://safe.example.com/sub.srt"
    fake_sub.url = None
    fake_sub.provider_name = "opensubtitlescom"
    fake_sub.content = b"1\n00:00:00,000 --> 00:00:01,000\nhi"

    with patch("compat.service.resolve_safe_url") as guard, \
         patch("compat.service.assert_safe_outbound"), \
         patch("compat.service._get_compat_pool") as pool:
        pool.return_value.download_subtitle = MagicMock(return_value=None)
        out = service._fetch_subtitle_bytes(fake_sub)
        assert guard.called, "resolve_safe_url MUST be called before fetch"
        assert out == fake_sub.content
        pool.return_value.download_subtitle.assert_called_once_with(fake_sub)
