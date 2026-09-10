"""Local protocol fixtures use synthetic keys only."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests


@pytest.fixture
def http_fixture():
    servers = []

    def start(replies, *, body_delay=0):
        records = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_request(self):
                records.append({"method": self.command, "path": self.path,
                                "headers": dict(self.headers),
                                "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
                status, body, headers = replies.pop(0)
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body_delay:
                    time.sleep(body_delay)
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = handle_request
            do_POST = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}", records

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_base_prefix_proxy_bypass_and_authenticated_request(http_fixture, monkeypatch):
    from media_servers.http import MediaServerHTTP
    base, records = http_fixture([(200, {"ok": True}, {})])
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    client = MediaServerHTTP(base + "/proxy/emby/", headers={"X-Emby-Token": "synthetic-key"})
    assert client.request_json("GET", "/System/Info") == {"ok": True}
    assert records[0]["path"] == "/proxy/emby/System/Info"
    assert records[0]["headers"]["X-Emby-Token"] == "synthetic-key"
    assert records[0]["body"] == b""
    client.close()


def test_redirect_fails_closed_without_contacting_target(http_fixture):
    from media_servers.http import MediaServerError, MediaServerHTTP
    target, target_records = http_fixture([])
    base, records = http_fixture([(302, b"", {"Location": target + "/stolen"})])
    with MediaServerHTTP(base, headers={"X-Emby-Token": "synthetic-key"}) as client:
        with pytest.raises(MediaServerError, match="redirect_denied"):
            client.request_json("GET", "/System/Info")
    assert len(records) == 1
    assert target_records == []


@pytest.mark.parametrize("url", ["ftp://server", "http://user:synthetic-key@server", "http://server?x=1",
                                 "http://server#fragment", "http://", "http://server:invalid", "http://server/\npath"])
def test_unsafe_base_urls_are_rejected(url):
    from media_servers.http import MediaServerError, MediaServerHTTP
    with pytest.raises(MediaServerError, match="invalid_url"):
        MediaServerHTTP(url)


@pytest.mark.parametrize(("status", "body", "code"), [
    (401, b"sensitive server response", "unauthorized"), (403, b"", "forbidden"),
    (500, b"", "server_error"), (200, b"not json", "invalid_response"),
    (200, b"x" * (2 * 1024 * 1024 + 1), "response_too_large"),
], ids=["unauthorized", "forbidden", "server-error", "malformed", "oversized"])
def test_response_classification(http_fixture, status, body, code):
    from media_servers.http import MediaServerError, MediaServerHTTP
    base, _records = http_fixture([(status, body, {})])
    with MediaServerHTTP(base) as client, pytest.raises(MediaServerError) as error:
        client.request_json("GET", "/System/Info")
    assert str(error.value) == code


@pytest.mark.parametrize("verify_ssl", [True, False])
def test_tls_timeouts_and_streaming_are_propagated(monkeypatch, verify_ssl):
    from media_servers.http import MediaServerError, MediaServerHTTP
    observed = []

    def request(_session, method, url, **kwargs):
        observed.append(kwargs)
        raise requests.Timeout("synthetic-secret-must-not-escape")

    monkeypatch.setattr(requests.Session, "request", request)
    with MediaServerHTTP("https://server", verify_ssl=verify_ssl) as client:
        with pytest.raises(MediaServerError) as error:
            client.request_json("GET", "/System/Info")
    assert str(error.value) == "timeout"
    assert observed[0]["verify"] is verify_ssl
    assert observed[0]["timeout"] == (3, 10)
    assert observed[0]["stream"] is True
    assert observed[0]["allow_redirects"] is False


def test_stream_limit_applies_without_content_length(monkeypatch):
    from media_servers.http import MediaServerError, MediaServerHTTP
    response = requests.Response()
    response.status_code = 200
    closed = []
    response.close = lambda: closed.append(True)
    response.iter_content = lambda chunk_size: iter([b"x" * chunk_size] * 400)
    monkeypatch.setattr(requests.Session, "request", lambda *_args, **_kwargs: response)
    with MediaServerHTTP("http://server") as client, pytest.raises(MediaServerError, match="response_too_large"):
        client.request_json("GET", "/System/Info")
    assert closed == [True]


def test_real_stream_read_timeout_is_categorized(http_fixture):
    from media_servers.http import MediaServerError, MediaServerHTTP
    base, records = http_fixture([(200, {"ok": True}, {})], body_delay=11)
    started = time.monotonic()
    with MediaServerHTTP(base) as client, pytest.raises(MediaServerError) as error:
        client.request_json("GET", "/System/Info")
    assert str(error.value) == "timeout"
    assert len(records) == 1
    assert time.monotonic() - started < 13
