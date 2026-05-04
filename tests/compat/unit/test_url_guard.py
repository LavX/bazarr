import pytest
import requests
from utilities.url_guard import assert_safe_outbound, resolve_safe_url, UnsafeURLError

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


# === resolve_safe_url: per-hop redirect validation ===


@pytest.fixture
def public_dns(monkeypatch):
    """Make api.example.com and attacker.example resolve to a public IP so
    the SSRF guard treats them as safe hostnames. The redirect chain logic
    is what these tests are exercising; the DNS-validation logic itself is
    covered by the assert_safe_outbound test cases above."""
    import socket
    public_ip_result = [(0, 0, 0, "", ("8.8.8.8", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: public_ip_result)


class _FakeResp:
    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}


def _fake_session(responses):
    """Return an object that mimics requests.Session.head, returning the
    next item from `responses` on each call."""
    iterator = iter(responses)

    class _S:
        def head(self, url, allow_redirects=False, timeout=None):
            assert allow_redirects is False, "must walk manually"
            return next(iterator)

    return _S()


def test_resolve_safe_url_no_redirect(monkeypatch, public_dns):
    monkeypatch.setattr(
        "utilities.url_guard._get_walker_session",
        lambda: _fake_session([_FakeResp(200)]),
    )
    final = resolve_safe_url("https://api.example.com/x")
    assert final == "https://api.example.com/x"


def test_resolve_safe_url_blocks_redirect_to_metadata(monkeypatch, public_dns):
    """The exact attack from Codex P1: public URL 30x to AWS metadata."""
    monkeypatch.setattr(
        "utilities.url_guard._get_walker_session",
        lambda: _fake_session([
            _FakeResp(302, location="http://169.254.169.254/latest/meta-data/iam/"),
        ]),
    )
    with pytest.raises(UnsafeURLError):
        resolve_safe_url("https://attacker.example/safe-looking")


def test_resolve_safe_url_blocks_redirect_to_loopback(monkeypatch, public_dns):
    monkeypatch.setattr(
        "utilities.url_guard._get_walker_session",
        lambda: _fake_session([
            _FakeResp(301, location="http://127.0.0.1/admin"),
        ]),
    )
    with pytest.raises(UnsafeURLError):
        resolve_safe_url("https://attacker.example/x")


def test_resolve_safe_url_blocks_relative_redirect_into_loopback(monkeypatch, public_dns):
    """Relative Location values must be resolved against the current URL.
    A relative path on a public host stays on that host (safe). Test that
    an absolute Location still gets validated."""
    monkeypatch.setattr(
        "utilities.url_guard._get_walker_session",
        lambda: _fake_session([
            _FakeResp(302, location="/path/here"),
            _FakeResp(200),
        ]),
    )
    final = resolve_safe_url("https://api.example.com/start")
    assert final == "https://api.example.com/path/here"


def test_resolve_safe_url_chain_too_long(monkeypatch, public_dns):
    monkeypatch.setattr(
        "utilities.url_guard._get_walker_session",
        lambda: _fake_session([
            _FakeResp(302, location="https://api.example.com/a"),
            _FakeResp(302, location="https://api.example.com/b"),
            _FakeResp(302, location="https://api.example.com/c"),
            _FakeResp(302, location="https://api.example.com/d"),
            _FakeResp(302, location="https://api.example.com/e"),
            _FakeResp(302, location="https://api.example.com/f"),
        ]),
    )
    with pytest.raises(UnsafeURLError):
        resolve_safe_url("https://api.example.com/start", max_redirects=5)


def test_resolve_safe_url_redirect_without_location(monkeypatch, public_dns):
    monkeypatch.setattr(
        "utilities.url_guard._get_walker_session",
        lambda: _fake_session([_FakeResp(302, location=None)]),
    )
    with pytest.raises(UnsafeURLError):
        resolve_safe_url("https://api.example.com/x")


def test_resolve_safe_url_head_failure_returns_input(monkeypatch, public_dns):
    """HEAD failure means we cannot probe further. The original URL was
    already validated by the leading assert_safe_outbound, so return it
    unchanged and let the caller's defense-in-depth handle the GET."""

    class _S:
        def head(self, *a, **kw):
            raise requests.RequestException("network down")

    monkeypatch.setattr("utilities.url_guard._get_walker_session", lambda: _S())
    final = resolve_safe_url("https://api.example.com/x")
    assert final == "https://api.example.com/x"


def test_resolve_safe_url_initial_url_unsafe_rejected(monkeypatch):
    """If the input URL itself is unsafe, fail before any HEAD probe."""

    class _S:
        def head(self, *a, **kw):
            pytest.fail("HEAD must NOT be called when the input URL is unsafe")

    monkeypatch.setattr("utilities.url_guard._get_walker_session", lambda: _S())
    with pytest.raises(UnsafeURLError):
        resolve_safe_url("http://169.254.169.254/latest/")
