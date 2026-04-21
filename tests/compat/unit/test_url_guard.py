import pytest
from bazarr.utilities.url_guard import assert_safe_outbound, UnsafeURLError

@pytest.mark.parametrize("url", [
    "https://api.opensubtitles.com/api/v1/subtitles",
    "https://subdl.com/api/v1/search",
    "http://addic7ed.com/search.php",
])
def test_safe_urls_pass(url):
    assert_safe_outbound(url)  # no exception

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/secret",
    "http://localhost/admin",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/router",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/v6-loop",
    "http://metadata.internal/foo",
    "http://bar.local/baz",
    "file:///etc/passwd",
    "ftp://fileserver/x",
    "gopher://evil",
    "ssh://root@host",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_outbound(url)

def test_null_bytes_rejected():
    with pytest.raises(UnsafeURLError):
        assert_safe_outbound("http://a.com/\x00/b")


def test_dns_rebinding_multiple_ips_all_checked(monkeypatch):
    """If DNS returns multiple IPs, ALL must pass validation. Any unsafe → reject."""
    import socket
    fake_results = [
        (0, 0, 0, "", ("8.8.8.8", 0)),       # public
        (0, 0, 0, "", ("127.0.0.1", 0)),     # loopback — should trip
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: fake_results)
    with pytest.raises(UnsafeURLError):
        assert_safe_outbound("http://rebind.example/")


def test_dns_rebinding_multiple_public_ips_pass(monkeypatch):
    """Multiple public IPs → all pass."""
    import socket
    fake_results = [
        (0, 0, 0, "", ("8.8.8.8", 0)),
        (0, 0, 0, "", ("1.1.1.1", 0)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: fake_results)
    assert_safe_outbound("http://multi-public.example/")  # no exception


def test_multicast_rejected():
    with pytest.raises(UnsafeURLError):
        assert_safe_outbound("http://224.0.0.1/multicast")
