# coding=utf-8
"""The Test of a FlareSolverr address the user typed in Settings.

A real HTTP server on the loopback interface plays FlareSolverr, so the probe,
the address rules and the failure classes all run end to end without the network.
"""

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from provider_hub import endpoints

INDEX = {"msg": "FlareSolverr is ready!", "version": "3.3.21", "userAgent": "Mozilla/5.0"}
SESSIONS = {"status": "ok", "message": "", "sessions": [], "version": "3.3.21"}
EM_DASH = chr(0x2014)


class FakeFlareSolverr:
    """A tiny server whose answers each test sets. ``requests`` records every hit."""

    def __init__(self):
        self.root = (200, INDEX)
        self.v1 = (200, SESSIONS)
        self.root_delay = 0.0
        self.root_raw = None
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, raw=None, location=None):
                body = raw if raw is not None else json.dumps(payload).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if outer.root_delay:
                    time.sleep(outer.root_delay)
                status, payload = outer.root
                self._send(status, payload, raw=outer.root_raw,
                           location="/elsewhere" if status == 302 else None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(("POST", self.path, json.loads(self.rfile.read(length) or b"null")))
                status, payload = outer.v1
                self._send(status, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@pytest.fixture
def fake():
    server = FakeFlareSolverr()
    yield server
    server.stop()


def _closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestAnswers:
    @pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
    def test_root_and_v1_spellings_are_the_same_service(self, fake, suffix):
        result = endpoints.check_flaresolverr_address(fake.address + suffix)

        assert result["ok"] is True
        assert result["code"] == "ok"
        assert result["version"] == "3.3.21"
        assert "3.3.21" in result["message"]
        assert [(method, path) for method, path, _ in fake.requests] == [("GET", "/"), ("POST", "/v1")]

    def test_the_v1_probe_only_lists_sessions(self, fake):
        endpoints.check_flaresolverr_address(fake.address)

        assert fake.requests[1][2] == {"cmd": "sessions.list"}

    def test_a_reverse_proxy_path_is_kept(self, fake):
        result = endpoints.check_flaresolverr_address(fake.address + "/flare/v1")

        assert result["code"] == "ok"
        assert [path for _, path, _ in fake.requests] == ["/flare", "/flare/v1"]

    def test_the_address_is_trimmed(self, fake):
        assert endpoints.check_flaresolverr_address(f"  {fake.address}/v1 ")["code"] == "ok"

    def test_a_missing_version_still_passes(self, fake):
        fake.root = (200, {"msg": "FlareSolverr is ready!"})
        fake.v1 = (200, {"status": "ok", "sessions": []})

        result = endpoints.check_flaresolverr_address(fake.address)

        assert result["code"] == "ok"
        assert result["version"] is None
        assert result["message"] == "FlareSolverr answered."

    def test_a_version_that_is_not_a_version_is_not_echoed(self, fake):
        fake.root = (200, {"msg": "FlareSolverr is ready!", "version": "<script>alert(1)</script>"})
        fake.v1 = (200, {"status": "ok", "sessions": []})

        result = endpoints.check_flaresolverr_address(fake.address)

        assert result["code"] == "ok"
        assert result["version"] is None
        assert "script" not in result["message"]


class TestNotFlareSolverr:
    @pytest.mark.parametrize("root", [
        (200, {"msg": "hello"}),
        (200, ["a", "list"]),
        (404, {"msg": "FlareSolverr"}),
        (500, {"msg": "FlareSolverr"}),
    ])
    def test_a_root_that_does_not_answer_like_it(self, fake, root):
        fake.root = root

        result = endpoints.check_flaresolverr_address(fake.address)

        assert (result["ok"], result["code"]) == (False, "not_flaresolverr")
        assert "not FlareSolverr" in result["message"]
        assert [method for method, _, _ in fake.requests] == ["GET"]

    def test_html_at_the_root(self, fake):
        fake.root_raw = b"<html>captive portal</html>"

        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "not_flaresolverr"

    def test_an_endless_root_body_is_refused(self, fake):
        fake.root_raw = b'{"msg": "FlareSolverr", "pad": "' + b"x" * endpoints.SIBLING_PROBE_MAX_BYTES + b'"}'

        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "not_flaresolverr"

    @pytest.mark.parametrize("v1", [(404, {"status": "ok"}), (200, {"status": "error"}), (200, {"nope": 1})])
    def test_a_root_without_a_working_v1(self, fake, v1):
        fake.v1 = v1

        result = endpoints.check_flaresolverr_address(fake.address)

        assert (result["ok"], result["code"]) == (False, "not_flaresolverr")
        assert "/v1" in result["message"]

    def test_a_redirect_is_not_followed(self, fake):
        fake.root = (302, {"msg": "FlareSolverr"})

        result = endpoints.check_flaresolverr_address(fake.address)

        assert result["code"] == "not_flaresolverr"
        assert [path for _, path, _ in fake.requests] == ["/"]


class TestFailures:
    def test_nothing_listening(self):
        result = endpoints.check_flaresolverr_address(f"http://127.0.0.1:{_closed_port()}")

        assert (result["ok"], result["code"]) == (False, "unreachable")
        assert "Could not connect" in result["message"]

    def test_a_server_that_does_not_answer_in_time(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "ADDRESS_TEST_REQUEST_TIMEOUT_SECONDS", 0.3)
        fake.root_delay = 1.5

        result = endpoints.check_flaresolverr_address(fake.address)

        assert (result["ok"], result["code"]) == (False, "timeout")
        assert "in time" in result["message"]

    def test_the_whole_test_has_a_deadline(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "ADDRESS_TEST_DEADLINE_SECONDS", -1)

        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "timeout"
        assert fake.requests == []

    def test_a_tls_failure(self, fake):
        # https spoken to a plain http server: the handshake cannot complete.
        result = endpoints.check_flaresolverr_address(fake.address.replace("http://", "https://"))

        assert (result["ok"], result["code"]) == (False, "tls")
        assert "secure connection" in result["message"]

    def test_a_name_that_does_not_resolve(self, monkeypatch):
        def no_such_host(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", no_such_host)

        result = endpoints.check_flaresolverr_address("http://flaresolverr.invalid:8191")

        assert (result["ok"], result["code"]) == (False, "unreachable")
        assert "could not be resolved" in result["message"]

    def test_the_next_address_is_tried_when_the_first_refuses(self, fake, monkeypatch):
        port = fake.server.server_address[1]
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, p, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ])

        result = endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")

        assert result["code"] == "ok"

    def test_the_request_goes_to_the_checked_address_with_the_typed_host(self, fake, monkeypatch):
        port = fake.server.server_address[1]
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, p, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
        seen = []
        original = endpoints._fetch_index

        def spy(session, url, timeout, headers=None, verify=True):
            seen.append((url, (headers or {}).get("Host")))
            return original(session, url, timeout, headers=headers, verify=verify)

        monkeypatch.setattr(endpoints, "_fetch_index", spy)

        assert endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")["code"] == "ok"
        assert seen == [(f"http://127.0.0.1:{port}", f"solver.lan:{port}")]


class TestInvalidAddress:
    @pytest.mark.parametrize("address", [
        None,
        5,
        "",
        "   ",
        "flaresolverr:8191",
        "localhost:8191",
        "ftp://solver.lan:8191",
        "file:///etc/passwd",
        "http://",
        "http://:8191",
        "http://user:secret@solver.lan:8191",
        "http://user@solver.lan:8191",
        "http://solver.lan:8191/?a=b",
        "http://solver.lan:8191/#frag",
        "http://solver.lan:8191/../v1",
        "http://solver.lan:99999",
        "http://solver.lan:notaport",
        "http://solver .lan:8191",
        "http://solver.lan:8191\\v1",
        "http://224.0.0.1:8191",
        "http://0.0.0.0:8191",
        "http://[ff02::1]:8191",
        "http://" + "a" * 3000,
    ])
    def test_refused_before_any_request(self, monkeypatch, address):
        import requests

        monkeypatch.setattr(requests.Session, "get", lambda *a, **k: pytest.fail("must not connect"))
        monkeypatch.setattr(requests.Session, "post", lambda *a, **k: pytest.fail("must not connect"))

        result = endpoints.check_flaresolverr_address(address)

        assert (result["ok"], result["code"]) == (False, "invalid_address")
        assert result["version"] is None
        assert "secret" not in result["message"]

    def test_loopback_and_lan_hosts_are_allowed(self, fake):
        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "ok"

    def test_the_empty_address_has_its_own_sentence(self):
        assert endpoints.check_flaresolverr_address("")["message"] == "Type an address to test."


class TestResultContract:
    def test_every_code_is_a_documented_one_with_a_sentence(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "ADDRESS_TEST_REQUEST_TIMEOUT_SECONDS", 0.3)
        results = [
            endpoints.check_flaresolverr_address(fake.address),
            endpoints.check_flaresolverr_address(f"http://127.0.0.1:{_closed_port()}"),
            endpoints.check_flaresolverr_address(""),
            endpoints.check_flaresolverr_address(fake.address.replace("http://", "https://")),
        ]
        fake.root = (200, {"msg": "nope"})
        results.append(endpoints.check_flaresolverr_address(fake.address))
        fake.root = (200, INDEX)
        fake.root_delay = 1.5
        results.append(endpoints.check_flaresolverr_address(fake.address))

        assert {result["code"] for result in results} == set(endpoints.ADDRESS_TEST_CODES)
        for result in results:
            assert set(result) == {"ok", "code", "message", "version"}
            assert result["message"].endswith(".")
            assert EM_DASH not in result["message"]

    def test_nothing_about_the_address_is_logged(self, fake, caplog):
        caplog.set_level(logging.DEBUG)
        port = _closed_port()

        endpoints.check_flaresolverr_address(f"http://127.0.0.1:{port}/v1")
        endpoints.check_flaresolverr_address("http://user:hunter2@solver.lan:8191")
        endpoints.check_flaresolverr_address(fake.address.replace("http://", "https://"))

        # urllib3 logs its own connection lines; what this code logs is checked.
        ours = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("provider_hub"))
        assert ours
        assert str(port) not in ours
        assert "127.0.0.1" not in ours
        assert "hunter2" not in ours
        assert "solver.lan" not in ours

    def test_the_sibling_probe_is_unchanged_by_the_shared_reader(self, fake):
        assert endpoints._probe_flaresolverr(fake.address) is True
        fake.root = (200, {"msg": "hello"})
        assert endpoints._probe_flaresolverr(fake.address) is False


class TestApiRoute:
    @staticmethod
    def _client():
        from flask import Flask
        from flask_restx import Api

        from api.providers import flaresolverr as api_module

        app = Flask(__name__)
        api = Api(app)
        api.add_namespace(api_module.api_ns_providers_flaresolverr, path="/")
        return app.test_client()

    @staticmethod
    def _headers():
        from app.config import settings

        return {"X-API-KEY": settings.auth.apikey}

    def test_it_needs_authentication_and_probes_nothing(self, fake):
        client = self._client()

        assert client.post("/providers/flaresolverr/test", json={"url": fake.address}).status_code == 401
        assert client.post("/providers/flaresolverr/test", json={"url": fake.address},
                           headers={"X-API-KEY": "wrong"}).status_code == 401
        assert fake.requests == []

    def test_it_tests_the_typed_address_and_saves_nothing(self, fake, monkeypatch):
        from app.config import settings

        before = settings.get("endpoints", {}).get(endpoints.FLARESOLVERR_SETTING_KEY)
        saved = []
        monkeypatch.setattr("app.config.save_settings", lambda *a, **k: saved.append(a))

        response = self._client().post("/providers/flaresolverr/test",
                                       json={"url": fake.address + "/v1"}, headers=self._headers())

        assert response.status_code == 200, response.data
        data = response.get_json()["data"]
        assert data["ok"] is True and data["code"] == "ok" and data["version"] == "3.3.21"
        assert settings.get("endpoints", {}).get(endpoints.FLARESOLVERR_SETTING_KEY) == before
        assert saved == []

    def test_the_settings_page_sends_a_form_field(self, fake):
        response = self._client().post("/providers/flaresolverr/test", data={"url": fake.address},
                                       headers=self._headers())

        assert response.status_code == 200, response.data
        assert response.get_json()["data"]["code"] == "ok"

    @pytest.mark.parametrize("kwargs", [
        {"data": "not json", "content_type": "text/plain"},
        {"json": ["list"]},
        {"json": {}},
        {"json": {"url": 7}},
        {"json": {"url": ""}},
    ])
    def test_a_bad_body_is_an_invalid_address_not_an_error(self, kwargs):
        response = self._client().post("/providers/flaresolverr/test", headers=self._headers(), **kwargs)

        assert response.status_code == 200, response.data
        assert response.get_json()["data"]["code"] == "invalid_address"
