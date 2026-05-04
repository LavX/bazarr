"""Tests for the constrained Sonarr/Radarr connection tester
introduced for LavX/bazarr#92.

Covers:
- _resolve_and_validate_constrained: loopback OK, link-local OK, multicast
  rejected, unspecified rejected, multi-IP DNS picks first usable address,
  empty getaddrinfo handled, missing host handled.
- _validate_test_base_url: scheme allowlist, missing host, query/fragment
  rejection, path traversal rejection.
- proxy_service: service whitelist, missing url/apikey, end-to-end status
  probe with mocked requests.get, both legacy (/api/system/status) and
  v3 (/api/v3/system/status) paths attempted.
"""
import socket
from unittest.mock import patch, MagicMock

import pytest


def _addr(ip):
    """Build a getaddrinfo tuple for `ip`."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, 0, 0, "", (ip, 0))


# === _resolve_and_validate_constrained ===

@pytest.mark.parametrize("ip", [
    "127.0.0.1",       # IPv4 loopback
    "::1",             # IPv6 loopback
    "169.254.1.1",     # IPv4 link-local
    "fe80::1",         # IPv6 link-local
    "10.0.0.5",        # private LAN
    "192.168.1.10",    # private LAN
    "8.8.8.8",         # public
])
def test_relaxed_validator_accepts(monkeypatch, ip):
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [_addr(ip)])
    resolved_ips, hostname, parsed = _resolve_and_validate_constrained(
        "http://example.test:8989/"
    )
    assert resolved_ips == [ip]
    assert hostname == "example.test"


@pytest.mark.parametrize("ip", [
    "224.0.0.1",        # IPv4 multicast
    "ff02::1",          # IPv6 multicast (link-local all-nodes)
    "0.0.0.0",          # IPv4 unspecified
    "::",               # IPv6 unspecified
])
def test_relaxed_validator_rejects(monkeypatch, ip):
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [_addr(ip)])
    with pytest.raises(ValueError):
        _resolve_and_validate_constrained("http://example.test/")


def test_relaxed_validator_picks_first_usable_in_dual_stack(monkeypatch):
    """Multicast first, public second: validator skips multicast and keeps public."""
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [_addr("224.0.0.1"), _addr("8.8.8.8")],
    )
    resolved_ips, _, _ = _resolve_and_validate_constrained("http://example.test/")
    assert resolved_ips == ["8.8.8.8"]


def test_relaxed_validator_returns_all_safe_addresses(monkeypatch):
    """Dual-stack hostname: both addresses returned in DNS order so the
    caller can fall back if the first one refuses connection."""
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [_addr("::1"), _addr("127.0.0.1")],
    )
    resolved_ips, _, _ = _resolve_and_validate_constrained("http://localhost/")
    assert resolved_ips == ["::1", "127.0.0.1"]


def test_relaxed_validator_dedupes_repeated_addresses(monkeypatch):
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [
            _addr("127.0.0.1"),
            _addr("127.0.0.1"),  # SOCK_STREAM + SOCK_DGRAM both return the same IP
            _addr("::1"),
        ],
    )
    resolved_ips, _, _ = _resolve_and_validate_constrained("http://localhost/")
    assert resolved_ips == ["127.0.0.1", "::1"]


def test_relaxed_validator_no_addrs(monkeypatch):
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
    with pytest.raises(ValueError):
        _resolve_and_validate_constrained("http://example.test/")


def test_relaxed_validator_missing_host():
    from app.ui import _resolve_and_validate_constrained
    with pytest.raises(ValueError):
        _resolve_and_validate_constrained("http:///somepath")


# === _validate_test_base_url ===

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8989",
    "https://radarr.example.com",
    "https://example.com:7878/radarr",
    "http://[::1]:8989",
    "http://[fe80::1]:8989",
])
def test_base_url_validator_accepts(url):
    from app.ui import _validate_test_base_url
    parsed = _validate_test_base_url(url)
    assert parsed.scheme in ("http", "https")


@pytest.mark.parametrize("url,reason", [
    ("ftp://nope/", "protocol"),
    ("file:///etc/passwd", "protocol"),
    ("http:///nopath", "host"),
    ("http://x.test/?evil=1", "query"),
    ("http://x.test/#frag", "fragment"),
    ("http://x.test/sonarr/../admin", "relative"),
])
def test_base_url_validator_rejects(url, reason):
    from app.ui import _validate_test_base_url
    with pytest.raises(ValueError, match=reason):
        _validate_test_base_url(url)


# === proxy_service end-to-end ===


def _build_app():
    """Stand up a minimal Flask app with the ui blueprint mounted, with
    @check_login bypassed via session injection. Returns the test client."""
    from flask import Flask
    from app.ui import ui_bp
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(ui_bp)
    return app


def _login(client):
    """Pre-populate the session so @check_login passes."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True


def test_proxy_service_rejects_unknown_service(monkeypatch):
    monkeypatch.setattr("app.config.settings.auth.type", None)
    app = _build_app()
    client = app.test_client()
    _login(client)
    r = client.get("/test/whatever?url=http://x.test&apikey=k")
    body = r.get_json()
    assert body["status"] is False
    assert "unsupported service" in body["error"]


def test_proxy_service_rejects_missing_url(monkeypatch):
    monkeypatch.setattr("app.config.settings.auth.type", None)
    app = _build_app()
    client = app.test_client()
    _login(client)
    r = client.get("/test/sonarr?apikey=k")
    body = r.get_json()
    assert body["status"] is False
    assert "missing url" in body["error"]


def test_proxy_service_rejects_missing_apikey(monkeypatch):
    monkeypatch.setattr("app.config.settings.auth.type", None)
    app = _build_app()
    client = app.test_client()
    _login(client)
    r = client.get("/test/sonarr?url=http://127.0.0.1:8989")
    body = r.get_json()
    assert body["status"] is False
    assert "missing apikey" in body["error"]


def test_proxy_service_succeeds_against_localhost(monkeypatch):
    """The headline reproduction from issue #92: localhost connection
    test must succeed after the fix."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("127.0.0.1")])
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=fake_response) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        r = client.get("/test/sonarr?url=http://127.0.0.1:8989&apikey=secret")
        body = r.get_json()
        assert body["status"] is True
        assert body["version"] == "4.0.0.0"
        # First call attempts /api/system/status (legacy path)
        called_url = fake_get.call_args_list[0].args[0]
        assert called_url.endswith("/api/system/status")
        # X-Api-Key header passed through
        called_headers = fake_get.call_args_list[0].kwargs["headers"]
        assert called_headers.get("X-Api-Key") == "secret"


def test_proxy_service_falls_back_to_v3(monkeypatch):
    """Legacy path 404 -> v3 path tried -> success."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("10.0.0.5")])
    legacy_resp = MagicMock(status_code=404)
    v3_resp = MagicMock(status_code=200)
    v3_resp.json.return_value = {"version": "5.1.0"}
    with patch("app.ui.requests.get", side_effect=[legacy_resp, v3_resp]) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        r = client.get("/test/radarr?url=https://radarr.example.com&apikey=k")
        body = r.get_json()
        assert body["status"] is True
        assert body["version"] == "5.1.0"
        # Both attempts made
        assert fake_get.call_count == 2
        urls = [c.args[0] for c in fake_get.call_args_list]
        assert urls[0].endswith("/api/system/status")
        assert urls[1].endswith("/api/v3/system/status")


def test_proxy_service_returns_401_without_falling_back(monkeypatch):
    """401 is a final answer (bad apikey), do NOT try v3."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("127.0.0.1")])
    resp = MagicMock(status_code=401)
    with patch("app.ui.requests.get", return_value=resp) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        r = client.get("/test/sonarr?url=http://127.0.0.1:8989&apikey=bad")
        body = r.get_json()
        assert body["status"] is False
        assert "Access Denied" in body["error"]
        assert fake_get.call_count == 1


def test_proxy_service_rejects_url_with_query(monkeypatch):
    monkeypatch.setattr("app.config.settings.auth.type", None)
    app = _build_app()
    client = app.test_client()
    _login(client)
    r = client.get(
        "/test/sonarr?url=http://x.test/sonarr%3Fevil%3D1&apikey=k"
    )
    body = r.get_json()
    assert body["status"] is False
    assert "Request blocked" in body["error"]


def test_proxy_service_honors_verify_ssl_setting(monkeypatch):
    """verify=False is no longer hardcoded; the per-service verify_ssl
    setting flows through via get_ssl_verify."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("8.8.8.8")])
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=resp) as fake_get, \
         patch("app.ui.get_ssl_verify", return_value=False) as fake_verify:
        app = _build_app()
        client = app.test_client()
        _login(client)
        client.get("/test/sonarr?url=https://sonarr.example.com&apikey=k")
        assert fake_get.call_args_list[0].kwargs["verify"] is False
        fake_verify.assert_called_with("sonarr")


def test_proxy_service_falls_back_to_next_resolved_ip_on_connection_refused(monkeypatch):
    """Dual-stack hostname where the first address refuses connection.
    The second address must be tried automatically. This is the
    'localhost resolves to ::1 first, only 127.0.0.1 listens' case
    seen on every dual-stack Linux box."""
    import requests
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [_addr("::1"), _addr("127.0.0.1")],
    )
    refused = requests.ConnectionError("ECONNREFUSED on ::1")
    success = MagicMock(status_code=200)
    success.json.return_value = {"version": "4.0.0.7000"}
    # Two ConnectionError on ::1 (legacy + v3 path), then 200 on 127.0.0.1
    # legacy (because the inner break short-circuits to next IP after
    # first ConnectionError, so 127.0.0.1 starts at legacy path).
    with patch("app.ui.requests.get", side_effect=[refused, success]) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        r = client.get("/test/sonarr?url=http://localhost:8989&apikey=k")
        body = r.get_json()
        assert body["status"] is True, body
        assert body["version"] == "4.0.0.7000"
        # First call hit ::1 (refused), second call hit 127.0.0.1
        assert fake_get.call_count == 2
        urls = [c.args[0] for c in fake_get.call_args_list]
        assert "[::1]" in urls[0]
        assert "127.0.0.1" in urls[1]


def test_proxy_service_returns_connection_error_when_no_ip_reachable(monkeypatch):
    import requests
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [_addr("::1"), _addr("127.0.0.1")],
    )
    refused = requests.ConnectionError("nothing listens here")
    with patch("app.ui.requests.get", side_effect=[refused, refused]):
        app = _build_app()
        client = app.test_client()
        _login(client)
        r = client.get("/test/sonarr?url=http://localhost:8989&apikey=k")
        body = r.get_json()
        assert body["status"] is False
        assert "Cannot connect" in body["error"]


def test_format_host_header_brackets_ipv6():
    """RFC 7230 §5.4 requires IPv6 literals in the Host header to be
    bracketed. urlparse(...).hostname strips brackets, so the helper
    has to put them back. Codex P2 round 3."""
    from app.ui import _format_host_header
    # IPv6 with non-default port
    assert _format_host_header("::1", 8989, "http") == "[::1]:8989"
    # IPv6 with default HTTP port -> port omitted, brackets kept
    assert _format_host_header("::1", 80, "http") == "[::1]"
    # IPv6 link-local
    assert _format_host_header("fe80::1", 8989, "http") == "[fe80::1]:8989"
    # IPv4 unchanged
    assert _format_host_header("127.0.0.1", 8989, "http") == "127.0.0.1:8989"
    # Hostname unchanged
    assert _format_host_header("sonarr.example.com", 443, "https") == "sonarr.example.com"
    # Hostname with non-default https port
    assert _format_host_header("sonarr.example.com", 8443, "https") == "sonarr.example.com:8443"
    # original_port=None -> omitted
    assert _format_host_header("sonarr.example.com", None, "http") == "sonarr.example.com"


def test_proxy_service_brackets_ipv6_in_host_header(monkeypatch):
    """Verify the IPv6 Host header makes it through proxy_service intact.
    Without the bracketing fix, Sonarr/Radarr behind certain HTTP parsers
    return 400 because Host: ::1:8989 is ambiguous (which colon is the
    port separator?). Codex P2 round 3."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("::1")])
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=resp) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        # Note: Flask's test_client URL-encodes %5B/%5D; we use the
        # already-encoded form so the value reaches base_url unchanged.
        client.get("/test/sonarr?url=http%3A//%5B%3A%3A1%5D%3A8989&apikey=k")
        called_url = fake_get.call_args_list[0].args[0]
        called_headers = fake_get.call_args_list[0].kwargs["headers"]
        # Pinned URL still uses ::1 in bracketed form (the netloc
        # construction already brackets IPv6)
        assert "[::1]" in called_url
        # Host header brackets the IPv6 literal correctly
        assert called_headers["Host"] == "[::1]:8989"


def test_proxy_service_pins_to_resolved_ip_for_http(monkeypatch):
    """DNS-rebinding mitigation preserved on HTTP: request goes to the
    resolved IP with Host: header set to the original hostname. TLS
    hostname validation does not run for HTTP, so pinning is the only
    DNS-rebinding mitigation available."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("203.0.113.42")])
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=resp) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        client.get("/test/sonarr?url=http://sonarr.example.com&apikey=k")
        called_url = fake_get.call_args_list[0].args[0]
        called_headers = fake_get.call_args_list[0].kwargs["headers"]
        assert "203.0.113.42" in called_url
        assert called_headers.get("Host") == "sonarr.example.com"


def test_proxy_service_does_not_pin_for_https_with_verify(monkeypatch):
    """Codex P2: when HTTPS is in use with verify_ssl=True, pinning the
    URL to the resolved IP would set SNI to the IP and fail TLS
    hostname validation against a cert legitimately issued for the
    hostname. The hostname must be preserved in the URL so urllib3
    sets SNI correctly and the cert validates."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("203.0.113.42")])
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=resp) as fake_get, \
         patch("app.ui.get_ssl_verify", return_value=True):
        app = _build_app()
        client = app.test_client()
        _login(client)
        client.get("/test/sonarr?url=https://sonarr.example.com&apikey=k")
        called_url = fake_get.call_args_list[0].args[0]
        called_headers = fake_get.call_args_list[0].kwargs["headers"]
        # Hostname preserved -> TLS cert can validate
        assert "sonarr.example.com" in called_url
        assert "203.0.113.42" not in called_url
        # No Host header override needed when URL already has the hostname
        assert "Host" not in called_headers
        # verify=True flowed through
        assert fake_get.call_args_list[0].kwargs["verify"] is True


def test_proxy_service_pins_for_https_when_verify_disabled(monkeypatch):
    """Belt-and-suspenders for the verify=False case: with TLS
    validation explicitly disabled, the only DNS-rebinding mitigation
    left is IP pinning, so we DO pin in that case."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("203.0.113.42")])
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=resp) as fake_get, \
         patch("app.ui.get_ssl_verify", return_value=False):
        app = _build_app()
        client = app.test_client()
        _login(client)
        client.get("/test/sonarr?url=https://sonarr.example.com&apikey=k")
        called_url = fake_get.call_args_list[0].args[0]
        called_headers = fake_get.call_args_list[0].kwargs["headers"]
        assert "203.0.113.42" in called_url
        assert called_headers.get("Host") == "sonarr.example.com"
        assert fake_get.call_args_list[0].kwargs["verify"] is False
