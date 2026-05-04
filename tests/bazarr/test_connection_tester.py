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
    resolved_ip, hostname, parsed = _resolve_and_validate_constrained(
        "http://example.test:8989/"
    )
    assert resolved_ip == ip
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
    """Multicast first, public second: validator skips multicast and picks public."""
    from app.ui import _resolve_and_validate_constrained
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: [_addr("224.0.0.1"), _addr("8.8.8.8")],
    )
    resolved_ip, _, _ = _resolve_and_validate_constrained("http://example.test/")
    assert resolved_ip == "8.8.8.8"


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


def test_proxy_service_pins_to_resolved_ip_with_host_header(monkeypatch):
    """DNS-rebinding mitigation preserved: request goes to the resolved IP
    with Host: header set to the original hostname."""
    monkeypatch.setattr("app.config.settings.auth.type", None)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **kw: [_addr("203.0.113.42")])
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"version": "4.0.0.0"}
    with patch("app.ui.requests.get", return_value=resp) as fake_get:
        app = _build_app()
        client = app.test_client()
        _login(client)
        client.get("/test/sonarr?url=https://sonarr.example.com&apikey=k")
        called_url = fake_get.call_args_list[0].args[0]
        called_headers = fake_get.call_args_list[0].kwargs["headers"]
        assert "203.0.113.42" in called_url
        assert called_headers.get("Host") == "sonarr.example.com"
