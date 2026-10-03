"""
Test for Bazarr UI functionality including authentication decorators.
"""
import logging
import socket
import threading
from contextlib import contextmanager

import pytest
from types import SimpleNamespace
from unittest.mock import Mock, patch  # noqa: F401
from flask import Flask, session as flask_session

from app.ui import check_login


def test_check_login_decorator_preserves_function_signature():
    """
    Test that check_login decorator preserves the original function's signature and metadata.
    """
    def original_function(arg1, arg2, kwarg1=None):
        """Test function docstring."""
        return f"{arg1}:{arg2}:{kwarg1}"

    decorated_function = check_login(original_function)

    # Check that function metadata is preserved
    assert decorated_function.__name__ == original_function.__name__
    assert decorated_function.__doc__ == original_function.__doc__


def test_check_login_decorator_can_be_applied():
    """
    Test that check_login decorator can be successfully applied to functions.
    """
    def test_function():
        return "test_result"

    # Should not raise any exceptions when applying decorator
    decorated_function = check_login(test_function)
    assert callable(decorated_function)


def test_check_login_decorator_is_wrapper():
    """
    Test that check_login returns a wrapper function that can be called.
    """
    def original_function(value):
        return value * 2

    decorated_function = check_login(original_function)

    # Verify it's a different function (wrapped)
    assert decorated_function != original_function
    assert callable(decorated_function)
    assert decorated_function.__name__ == original_function.__name__


def test_instance_image_url_rejects_wrong_instance_kind(monkeypatch):
    """
    A movie image request must not be allowed to route through a Sonarr instance
    just because the caller supplied that instance id.
    """
    from app.ui import _instance_image_url
    import arr_instances.resolution as resolution

    class SonarrClient:
        kind = "sonarr"
        api_key = "key"
        verify_ssl = False
        _base_url_raw = "/"

        def base_url(self):
            return "http://sonarr.example:8989"

    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: SonarrClient(),
    )

    app = Flask(__name__)
    with app.test_request_context("/images/movies/MediaCover/1/poster.jpg?arr_instance_id=9"):
        assert _instance_image_url("radarr", "MediaCover/1/poster.jpg") == (None, None)


def test_instance_image_url_routes_matching_instance_kind(monkeypatch):
    from app.ui import _instance_image_url
    import arr_instances.resolution as resolution

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: RadarrClient(),
    )

    app = Flask(__name__)
    with app.test_request_context("/images/movies/radarr/MediaCover/1/poster.jpg?arr_instance_id=9"):
        assert _instance_image_url("radarr", "radarr/MediaCover/1/poster.jpg") == (
            "https://radarr.example:7878/radarr/api/v3/MediaCover/1/poster.jpg?apikey=key",
            True,
        )


def test_movie_image_route_returns_404_for_wrong_instance_kind(monkeypatch):
    from app import ui
    import arr_instances.resolution as resolution

    class SonarrClient:
        kind = "sonarr"
        api_key = "key"
        verify_ssl = False
        _base_url_raw = "/"

        def base_url(self):
            return "http://sonarr.example:8989"

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: SonarrClient(),
    )

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)

    response = app.test_client().get(
        "/images/movies/MediaCover/1/poster.jpg?arr_instance_id=9"
    )

    assert response.status_code == 404


def test_movie_image_route_fetches_matching_instance(monkeypatch):
    from app import ui
    import arr_instances.resolution as resolution

    captured = {}

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    class UpstreamResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg", "ETag": '"abc"',
                   "Last-Modified": "Mon, 01 Sep 2026 00:00:00 GMT"}

        def iter_content(self, chunk_size):
            captured["chunk_size"] = chunk_size
            yield b"image-bytes"

        def close(self):
            captured["closed"] = True

    def fake_get(url, stream, timeout, verify, headers):
        captured.update({
            "url": url,
            "stream": stream,
            "timeout": timeout,
            "verify": verify,
            "headers": headers,
        })
        return UpstreamResponse()

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(ui.requests, "get", fake_get)
    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: RadarrClient(),
    )

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)

    response = app.test_client().get(
        "/images/movies/radarr/MediaCover/1/poster.jpg?lastWrite=1&arr_instance_id=9"
    )

    assert response.status_code == 200
    assert response.data == b"image-bytes"
    assert response.content_type == "image/jpeg"
    assert captured["url"] == (
        "https://radarr.example:7878/radarr/api/v3/MediaCover/1/poster.jpg?apikey=key"
    )
    assert captured["stream"] is True
    assert captured["timeout"] == 15
    assert captured["verify"] is True
    assert captured["chunk_size"] == 2048
    # Without these the browser re-fetches every cover through Bazarr on every
    # page view, one round trip per poster to the owning arr instance.
    # A day and no more: Sonarr strips the cache-busting query from its image
    # URLs, so a replaced series poster has no new address to arrive under and
    # only this window ends it.
    assert response.headers["Cache-Control"] == "private, max-age=86400"
    assert response.headers["ETag"] == '"abc"'
    assert response.headers["Last-Modified"] == "Mon, 01 Sep 2026 00:00:00 GMT"


def test_cover_route_relays_the_readers_validators_and_a_not_modified(monkeypatch):
    """An unchanged cover is answered with an empty 304, not the image again."""
    from app import ui
    import arr_instances.resolution as resolution

    captured = {}

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    class NotModified:
        status_code = 304
        headers = {"ETag": '"abc"'}

        def iter_content(self, chunk_size):  # pragma: no cover - never streamed
            raise AssertionError("a 304 has no body to stream")

        def close(self):
            captured["closed"] = True

    def fake_get(url, stream, timeout, verify, headers):
        captured["headers"] = headers
        return NotModified()

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(ui.requests, "get", fake_get)
    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: RadarrClient(),
    )

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)

    response = app.test_client().get(
        "/images/movies/radarr/MediaCover/1/poster.jpg?arr_instance_id=9",
        headers={"If-None-Match": '"abc"'},
    )

    assert response.status_code == 304
    assert response.data == b""
    assert captured["headers"]["If-None-Match"] == '"abc"'
    assert captured["closed"] is True
    assert response.headers["ETag"] == '"abc"'


def test_cover_route_does_not_dress_an_upstream_failure_as_an_image(monkeypatch):
    """A 404 body from the arr instance is a 404 here, not a 200 of its text."""
    from app import ui
    import arr_instances.resolution as resolution

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    class Missing:
        status_code = 404
        headers = {"content-type": "text/html"}

        def iter_content(self, chunk_size):  # pragma: no cover - never streamed
            raise AssertionError("an error page is not a cover")

        def close(self):
            pass

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(ui.requests, "get",
                        lambda url, stream, timeout, verify, headers: Missing())
    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: RadarrClient(),
    )

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)

    response = app.test_client().get(
        "/images/movies/radarr/MediaCover/1/poster.jpg?arr_instance_id=9")

    assert response.status_code == 404
    assert "Cache-Control" not in response.headers


# --- a cover that breaks off partway ------------------------------------------

COVER_PATH = "/images/movies/radarr/MediaCover/1/poster.jpg?arr_instance_id=9"


class _BreakingUpstream:
    """A Radarr cover answer that sends one chunk and then loses the connection,
    the way urllib3's IncompleteRead reaches a streaming requests response."""

    status_code = 200

    def __init__(self, headers):
        from requests.structures import CaseInsensitiveDict

        self.headers = CaseInsensitiveDict({"Content-Type": "image/jpeg", **headers})
        self.closed = False

    def iter_content(self, chunk_size):
        import requests

        yield b"x" * 100
        raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead(100 bytes read)")

    def close(self):
        self.closed = True


def _cover_app(monkeypatch, upstream):
    from app import ui
    import arr_instances.resolution as resolution

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(ui.requests, "get", lambda url, stream, timeout, verify, headers: upstream)
    monkeypatch.setattr(resolution, "client_for_instance", lambda session, instance_id: RadarrClient())

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)
    return app


def test_a_cover_that_breaks_off_aborts_the_response_and_releases_the_upstream(monkeypatch, caplog):
    """Ending the body quietly would finish the response, and the browser would
    keep a truncated poster for a day. It has to fail, as the exception the
    logger knows not to print a traceback for."""
    from app.logger import CoverStreamAborted

    upstream = _BreakingUpstream({"Content-Length": "1000"})
    response = _cover_app(monkeypatch, upstream).test_client().get(COVER_PATH)

    assert response.status_code == 200
    with caplog.at_level("DEBUG"), pytest.raises(CoverStreamAborted):
        response.get_data()
    assert upstream.closed is True
    assert [record.levelname for record in caplog.records if "cover" in record.getMessage()] == ["DEBUG"]


def test_a_whole_cover_releases_the_upstream_too(monkeypatch):
    class WholeUpstream(_BreakingUpstream):
        def iter_content(self, chunk_size):
            yield b"x" * 100

    upstream = WholeUpstream({"Content-Length": "100"})
    response = _cover_app(monkeypatch, upstream).test_client().get(COVER_PATH)

    assert response.get_data() == b"x" * 100
    assert upstream.closed is True


def test_a_head_request_for_a_cover_releases_the_upstream(monkeypatch):
    """A HEAD answer has no body, so the body's generator never starts and cannot
    be what closes the upstream response. Left open, it holds a pooled connection
    to the arr until it is garbage collected."""
    upstream = _BreakingUpstream({"Content-Length": "1000"})
    response = _cover_app(monkeypatch, upstream).test_client().head(COVER_PATH)

    assert response.status_code == 200
    assert response.get_data() == b""
    response.close()
    assert upstream.closed is True


@pytest.mark.parametrize("upstream_headers,relayed", [
    ({"Content-Length": "1000"}, "1000"),
    ({"Content-Length": "1000", "Content-Encoding": "identity"}, "1000"),
    # requests decodes a compressed body, so the upstream length is not this one's.
    ({"Content-Length": "400", "Content-Encoding": "gzip"}, None),
    # Chunking wins over a length sent beside it, so that length need not match.
    ({"Content-Length": "400", "Transfer-Encoding": "chunked"}, None),
    ({"Content-Length": "a lot"}, None),
    ({}, None),
])
def test_a_cover_carries_the_upstream_length_when_it_is_the_length_sent(monkeypatch, upstream_headers, relayed):
    """Without a length, a reverse proxy speaking HTTP/1.0 to Bazarr, as nginx does
    by default, reads a dropped connection as the end of a complete image."""
    upstream = _BreakingUpstream(upstream_headers)
    response = _cover_app(monkeypatch, upstream).test_client().get(COVER_PATH)

    assert response.headers.get("Content-Length") == relayed
    response.close()


@contextmanager
def _waitress(app):
    """Serve the app with the WSGI server Bazarr runs, on a loopback port of its own."""
    from waitress import wasyncore
    from waitress.server import create_server

    server = create_server(app, host="127.0.0.1", port=0, threads=1)
    stop = threading.Event()

    def serve():
        # The loop's own thread closes its sockets once it has stopped: closed
        # from the test's thread, they would vanish under a select() in progress.
        try:
            while not stop.is_set():
                wasyncore.loop(timeout=0.1, map=server._map, count=1)
        finally:
            server.task_dispatcher.shutdown()
            wasyncore.close_all(server._map)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.effective_port
    finally:
        stop.set()
        thread.join(timeout=5)


def _raw_get(port, path, version):
    """One request as a proxy or browser sends it, read to the end of the connection."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(f"GET {path} HTTP/{version}\r\nHost: 127.0.0.1\r\n\r\n".encode())
        received = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    status, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {name.lower(): value.strip() for name, _, value in (line.partition(":") for line in header_lines)}
    return status, headers, body


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_a_cover_that_breaks_off_is_visibly_incomplete_on_the_wire(monkeypatch, version):
    """What a browser or a reverse proxy actually receives from waitress.

    The response declares the upstream length and the connection closes short
    of it, so neither can take the partial image for a whole one.
    """
    from app.logger import CoverStreamAborted, UnwantedWaitressMessageFilter

    waitress_logger = logging.getLogger("waitress")
    monkeypatch.setattr(waitress_logger, "filters", [])
    records = []
    capture = logging.Handler()
    capture.emit = records.append
    waitress_logger.addHandler(capture)
    upstream = _BreakingUpstream({"Content-Length": "1000"})
    try:
        with _waitress(_cover_app(monkeypatch, upstream)) as port:
            status, headers, body = _raw_get(port, COVER_PATH, version)
    finally:
        waitress_logger.removeHandler(capture)

    assert status.split()[1] == "200"
    assert headers["content-length"] == "1000"
    assert body == b"x" * 100
    assert upstream.closed is True
    # waitress reports the abort, and the filter configure_logging installs
    # recognises that exact record, so a normal install logs no traceback.
    served = [record for record in records if record.getMessage().startswith("Exception while serving")]
    assert len(served) == 1
    assert isinstance(served[0].exc_info[1], CoverStreamAborted)
    assert not UnwantedWaitressMessageFilter(debug=False).filter(served[0])


def test_a_chunked_cover_that_breaks_off_never_gets_its_last_chunk(monkeypatch):
    """With no upstream length, HTTP/1.1 marks the end with an empty chunk, and
    that is the one thing an aborted cover must not send."""
    upstream = _BreakingUpstream({})
    monkeypatch.setattr(logging.getLogger("waitress"), "disabled", True)
    with _waitress(_cover_app(monkeypatch, upstream)) as port:
        status, headers, body = _raw_get(port, COVER_PATH, "1.1")

    assert status.split()[1] == "200"
    assert headers["transfer-encoding"] == "chunked"
    assert b"x" * 100 in body
    assert not body.endswith(b"0\r\n\r\n")


def test_check_login_no_authentication():
    """
    Test check_login decorator when no authentication is configured.
    """
    def test_function():
        return "success_response"

    # Mock settings for no authentication
    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        decorated_function = check_login(test_function)
        result = decorated_function()

        assert result == "success_response"


def test_check_login_basic_auth_success():
    """
    Test check_login decorator with valid basic authentication.
    """
    def test_function():
        return "authenticated_response"

    # Mock Flask request context with basic auth
    app = Flask(__name__)
    with app.test_request_context(headers={'Authorization': 'Basic dGVzdDp0ZXN0'}):
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.check_credentials', return_value=True):

            mock_settings.auth.type = 'basic'

            decorated_function = check_login(test_function)
            result = decorated_function()

            assert result == "authenticated_response"


def test_check_login_basic_auth_failure():
    """
    Test check_login decorator with invalid basic authentication.
    """
    def test_function():
        return "should_not_reach"

    # Mock Flask request context with invalid basic auth
    app = Flask(__name__)
    with app.test_request_context(headers={'Authorization': 'Basic aW52YWxpZA=='}):
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.check_credentials', return_value=False):

            mock_settings.auth.type = 'basic'

            decorated_function = check_login(test_function)
            result = decorated_function()

            # Should return 401 tuple
            assert isinstance(result, tuple)
            assert result[1] == 401
            assert result[0] == 'Unauthorized'


def test_check_login_basic_auth_missing():
    """
    Test check_login decorator when basic auth is required but not provided.
    """
    def test_function():
        return "should_not_reach"

    # Mock Flask request context without authorization header
    app = Flask(__name__)
    with app.test_request_context():
        with patch('app.ui.settings') as mock_settings:
            mock_settings.auth.type = 'basic'

            decorated_function = check_login(test_function)
            result = decorated_function()

            # Should return 401 tuple
            assert isinstance(result, tuple)
            assert result[1] == 401
            assert result[0] == 'Unauthorized'


def test_check_login_form_auth_success():
    """
    Test check_login decorator with valid form authentication session.
    """
    def test_function():
        return "form_authenticated_response"

    # Mock Flask request context with valid session
    app = Flask(__name__)
    app.secret_key = 'test_secret'

    with app.test_request_context():
        with patch('app.ui.settings') as mock_settings:
            mock_settings.auth.type = 'form'
            flask_session['logged_in'] = True

            decorated_function = check_login(test_function)
            result = decorated_function()

            assert result == "form_authenticated_response"


def test_check_login_form_auth_failure():
    """
    Test check_login decorator when form auth session is invalid.
    """
    def test_function():
        return "should_not_reach"

    app = Flask(__name__)
    app.secret_key = 'test_secret'
    with app.test_request_context():
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.abort') as mock_abort:

            mock_settings.auth.type = 'form'
            mock_abort.return_value = ('Unauthorized', 401)

            decorated_function = check_login(test_function)
            result = decorated_function()  # noqa: F841

            # Should call abort
            mock_abort.assert_called_once_with(401)


def test_check_login_preserves_function_arguments():
    """
    Test that check_login decorator properly passes through function arguments.
    """
    def test_function(arg1, arg2, kwarg1=None):
        return f"args:{arg1},{arg2} kwargs:{kwarg1}"

    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        decorated_function = check_login(test_function)
        result = decorated_function("test1", "test2", kwarg1="test_kw")

        assert result == "args:test1,test2 kwargs:test_kw"


def test_check_login_preserves_function_exceptions():
    """
    Test that check_login decorator allows function exceptions to propagate.
    """
    def test_function():
        raise ValueError("Test exception")

    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        decorated_function = check_login(test_function)

        with pytest.raises(ValueError, match="Test exception"):
            decorated_function()


def test_check_login_with_different_return_types():
    """
    Test that check_login decorator properly handles different return types.
    """
    test_cases = [
        ("string_result", str),
        (42, int),
        ([1, 2, 3], list),
        ({"key": "value"}, dict),
        (None, type(None)),
    ]

    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        for expected_value, expected_type in test_cases:
            def test_function():
                return expected_value

            decorated_function = check_login(test_function)
            result = decorated_function()

            assert result == expected_value
            assert type(result) == expected_type  # noqa: E721


# --- connection-test proxy API-key gate (SSRF hardening) ---------------------

def test_proxy_route_requires_api_key(monkeypatch):
    """The /test connection proxy must reject requests without the API key,
    even when UI auth is disabled (the default), so it is never an
    unauthenticated SSRF surface."""
    from app import ui

    monkeypatch.setattr(ui, "settings", SimpleNamespace(
        auth=SimpleNamespace(type=None, apikey="secret-key")))
    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)
    client = app.test_client()

    # No key -> blocked before any outbound request happens.
    assert client.get("/test/ftp/example.test/").status_code == 401

    # Correct key -> passes the gate (ftp is then rejected downstream, no DNS).
    resp = client.get("/test/ftp/example.test/", headers={"X-API-KEY": "secret-key"})
    assert resp.status_code == 200
    assert resp.get_json()["error"] == "Unsupported protocol"


def test_proxy_route_rejects_wrong_api_key(monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "settings", SimpleNamespace(
        auth=SimpleNamespace(type=None, apikey="secret-key")))
    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)
    assert app.test_client().get(
        "/test/ftp/x/", headers={"X-API-KEY": "wrong"}).status_code == 401


# --- backup download containment (path-traversal hardening) ------------------

@pytest.fixture
def backup_app(monkeypatch, tmp_path):
    """A backup folder holding one real backup, with a config file beside it and
    a sibling directory that shares the folder's name as a prefix."""
    from app import ui

    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "bazarr_backup.zip").write_text("zip-bytes")
    (tmp_path / "config.yaml").write_text("apikey: top-secret")
    sibling = tmp_path / "backup-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("top-secret")

    monkeypatch.setattr(ui, "settings", SimpleNamespace(
        auth=SimpleNamespace(type=None),
        backup=SimpleNamespace(folder=str(backup_dir))))
    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)
    return app, tmp_path


@pytest.mark.parametrize("filename", [
    ".",
    "../config.yaml",
    "ABSOLUTE",
    "../backup-evil/secret.txt",
])
def test_backup_download_refuses_anything_outside_the_backup_folder(backup_app, filename):
    """The folder itself, a parent-relative path, an absolute path and a sibling
    directory sharing the folder's name prefix are all a plain 404."""
    from app import ui

    app, root = backup_app
    if filename == "ABSOLUTE":
        filename = str(root / "config.yaml")
    with app.test_request_context():
        assert ui.backup_download(filename) == ('', 404)


def test_backup_download_serves_a_real_backup(backup_app):
    from app import ui

    app, _ = backup_app
    with app.test_request_context():
        response = ui.backup_download("bazarr_backup.zip")
        response.direct_passthrough = False
        assert response.status_code == 200
        assert response.get_data() == b"zip-bytes"
        assert "attachment" in response.headers["Content-Disposition"]
