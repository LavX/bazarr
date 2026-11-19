# coding=utf-8
"""The Test of a FlareSolverr address the user typed in Settings.

A real HTTP server on the loopback interface plays FlareSolverr, so the probe,
the address rules and the failure classes all run end to end without the network.
"""

import json
import logging
import os
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
        # Seconds between the bytes of an answer that is sent one byte at a time.
        self.root_drip = 0.0
        self.v1_drip = 0.0
        # The same, for the status line and the headers.
        self.root_header_drip = 0.0
        self.v1_header_drip = 0.0
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

            def _drip(self, payload, pause):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for index in range(len(body)):
                        self.wfile.write(body[index:index + 1])
                        self.wfile.flush()
                        time.sleep(pause)
                except OSError:
                    pass

            def _drip_headers(self, pause):
                raw = b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
                try:
                    for index in range(len(raw)):
                        self.wfile.write(raw[index:index + 1])
                        self.wfile.flush()
                        time.sleep(pause)
                except OSError:
                    pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if outer.root_header_drip:
                    return self._drip_headers(outer.root_header_drip)
                if outer.root_drip:
                    return self._drip(outer.root[1], outer.root_drip)
                if outer.root_delay:
                    time.sleep(outer.root_delay)
                status, payload = outer.root
                self._send(status, payload, raw=outer.root_raw,
                           location="/elsewhere" if status == 302 else None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(("POST", self.path, json.loads(self.rfile.read(length) or b"null")))
                status, payload = outer.v1
                if outer.v1_header_drip:
                    return self._drip_headers(outer.v1_header_drip)
                if outer.v1_drip:
                    return self._drip(payload, outer.v1_drip)
                self._send(status, payload)

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # Clients that give up mid-answer, or speak TLS to it, are the point of some tests.
                pass

        self.server = QuietServer(("127.0.0.1", 0), Handler)
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


def _resolve_host(monkeypatch, host, ips, port):
    """Make one host name resolve to the given addresses and leave every other lookup alone."""
    real = socket.getaddrinfo

    def lookup(name, *args, **kwargs):
        if name == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
        return real(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)


def _spy_fetch_index(monkeypatch, failures=None):
    """Record the address of every root request; ``failures`` maps an address to an exception to raise."""
    seen = []
    original = endpoints._fetch_index

    def spy(session, url, timeout, headers=None, verify=True, deadline=None):
        seen.append((url, (headers or {}).get("Host"), timeout))
        for ip, error in (failures or {}).items():
            if ip in url:
                raise error
        return original(session, url, timeout, headers=headers, verify=verify, deadline=deadline)

    monkeypatch.setattr(endpoints, "_fetch_index", spy)
    return seen


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
        _resolve_host(monkeypatch, "solver.lan", ["127.0.0.2", "127.0.0.1"], port)
        seen = _spy_fetch_index(monkeypatch)

        result = endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")

        assert result["code"] == "ok"
        # Both candidates, in the order the name resolved to them.
        assert [url for url, _, _ in seen] == [f"http://127.0.0.2:{port}", f"http://127.0.0.1:{port}"]

    def test_a_connect_timeout_moves_on_to_the_next_address(self, fake, monkeypatch):
        import requests

        port = fake.server.server_address[1]
        _resolve_host(monkeypatch, "solver.lan", ["127.0.0.2", "127.0.0.1"], port)
        seen = _spy_fetch_index(monkeypatch, {"127.0.0.2": requests.exceptions.ConnectTimeout("slow")})

        result = endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")

        assert result["code"] == "ok"
        assert [url for url, _, _ in seen] == [f"http://127.0.0.2:{port}", f"http://127.0.0.1:{port}"]

    def test_connect_timeouts_on_every_address_report_a_timeout(self, fake, monkeypatch):
        import requests

        port = fake.server.server_address[1]
        _resolve_host(monkeypatch, "solver.lan", ["127.0.0.2", "127.0.0.1"], port)
        seen = _spy_fetch_index(monkeypatch, {"127.0.0.": requests.exceptions.ConnectTimeout("slow")})

        result = endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")

        assert result["code"] == "timeout"
        assert len(seen) == 2

    def test_a_read_timeout_does_not_move_on(self, fake, monkeypatch):
        import requests

        port = fake.server.server_address[1]
        _resolve_host(monkeypatch, "solver.lan", ["127.0.0.2", "127.0.0.1"], port)
        seen = _spy_fetch_index(monkeypatch, {"127.0.0.2": requests.exceptions.ReadTimeout("stalled")})

        result = endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")

        assert result["code"] == "timeout"
        assert len(seen) == 1

    def test_each_request_timeout_is_capped_by_the_time_left(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "ADDRESS_TEST_DEADLINE_SECONDS", 2.0)
        seen = _spy_fetch_index(monkeypatch)

        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "ok"

        connect, read = seen[0][2]
        assert connect == read
        assert 0 < connect <= 2.0

    def test_the_request_goes_to_the_checked_address_with_the_typed_host(self, fake, monkeypatch):
        port = fake.server.server_address[1]
        _resolve_host(monkeypatch, "solver.lan", ["127.0.0.1"], port)
        seen = _spy_fetch_index(monkeypatch)

        assert endpoints.check_flaresolverr_address(f"http://solver.lan:{port}")["code"] == "ok"
        assert [(url, host) for url, host, _ in seen] == [(f"http://127.0.0.1:{port}", f"solver.lan:{port}")]

    def test_an_unexpected_failure_ends_in_a_sentence(self, monkeypatch):
        # Resolvable, but the host name cannot go into an http header.
        host = "\u4f8b\u3048.test"
        _resolve_host(monkeypatch, host, ["127.0.0.1"], 8191)

        result = endpoints.check_flaresolverr_address(f"http://{host}:8191")

        assert (result["ok"], result["code"]) == (False, "unreachable")
        assert result["message"] == "The test could not be completed."


class TestDeadline:
    DEADLINE = 1.5
    MARGIN = 1.5

    @pytest.fixture(autouse=True)
    def _short_deadline(self, monkeypatch):
        monkeypatch.setattr(endpoints, "ADDRESS_TEST_DEADLINE_SECONDS", self.DEADLINE)

    def _assert_times_out(self, address):
        started = time.monotonic()
        result = endpoints.check_flaresolverr_address(address)
        elapsed = time.monotonic() - started

        assert (result["ok"], result["code"]) == (False, "timeout")
        assert elapsed < self.DEADLINE + self.MARGIN, elapsed

    def test_a_root_answer_dripped_one_byte_at_a_time(self, fake):
        # Each byte arrives well inside the socket read timeout, so only the
        # deadline can end this.
        fake.root_drip = 0.5

        self._assert_times_out(fake.address)

    def test_a_v1_answer_dripped_one_byte_at_a_time(self, fake):
        fake.v1_drip = 0.5

        self._assert_times_out(fake.address)

    @staticmethod
    def _workers():
        return [thread for thread in threading.enumerate() if thread.name == "flaresolverr-address-test"]

    def _assert_worker_ends(self, within=3.0):
        stop = time.monotonic() + within
        while self._workers() and time.monotonic() < stop:
            time.sleep(0.05)
        assert not self._workers(), "the worker outlived the answer"

    @pytest.mark.parametrize("which", ["root_header_drip", "v1_header_drip"])
    def test_headers_dripped_one_byte_at_a_time(self, fake, which):
        # Every byte restarts the socket read timeout and getresponse() has not
        # returned yet, so neither the timeout nor the body check can end this.
        setattr(fake, which, 0.5)
        started = time.monotonic()

        result = endpoints.check_flaresolverr_address(fake.address)
        elapsed = time.monotonic() - started

        assert (result["ok"], result["code"]) == (False, "timeout")
        assert elapsed < self.DEADLINE + endpoints.ADDRESS_TEST_ANSWER_MARGIN_SECONDS + 1.0, elapsed
        # The worker's connection was cut from this side, so it ends soon after
        # instead of reading the rest of a forty second answer.
        self._assert_worker_ends()

    def test_the_route_answers_timeout_for_dripped_headers(self, fake):
        from flask import Flask
        from flask_restx import Api

        from api.providers import flaresolverr as api_module
        from app.config import settings

        fake.root_header_drip = 0.5
        app = Flask(__name__)
        Api(app).add_namespace(api_module.api_ns_providers_flaresolverr, path="/")
        started = time.monotonic()

        response = app.test_client().post("/providers/flaresolverr/test", json={"url": fake.address},
                                          headers={"X-API-KEY": settings.auth.apikey})
        elapsed = time.monotonic() - started

        assert response.status_code == 200, response.data
        assert response.get_json()["data"]["code"] == "timeout"
        assert elapsed < self.DEADLINE + endpoints.ADDRESS_TEST_ANSWER_MARGIN_SECONDS + 1.0, elapsed
        self._assert_worker_ends()

    def test_a_finished_test_leaves_no_worker_and_no_sockets(self, fake):
        result = endpoints.check_flaresolverr_address(fake.address)

        assert result["code"] == "ok"
        assert self._workers() == []


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

    def test_this_module_never_logs_the_address(self, fake, caplog):
        caplog.set_level(logging.DEBUG)
        port = _closed_port()

        endpoints.check_flaresolverr_address(f"http://127.0.0.1:{port}/v1")
        endpoints.check_flaresolverr_address("http://user:hunter2@solver.lan:8191")
        endpoints.check_flaresolverr_address(fake.address.replace("http://", "https://"))

        # urllib3 logs its own connection lines and may name the host; what this module logs is checked.
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
    @pytest.fixture(autouse=True)
    def _no_environment_address(self, monkeypatch):
        monkeypatch.delenv(endpoints.FLARESOLVERR_ENV, raising=False)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: ("", "none"))

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

    def test_an_empty_field_tests_the_environment_address_and_says_so(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (fake.address, "env"))

        data = self._client().post("/providers/flaresolverr/test", data={"url": "  "},
                                   headers=self._headers()).get_json()["data"]

        assert (data["code"], data["source"]) == ("ok", "env")
        assert [path for _, path, _ in fake.requests] == ["/", "/v1"]

    def test_an_empty_field_tests_the_saved_address_and_says_so(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (fake.address, "setting"))

        data = self._client().post("/providers/flaresolverr/test", json={},
                                   headers=self._headers()).get_json()["data"]

        assert (data["code"], data["source"]) == ("ok", "setting")

    def test_a_typed_address_wins_over_the_one_in_use(self, fake, monkeypatch):
        monkeypatch.setattr(endpoints, "flaresolverr_url_source",
                            lambda settings=None: ("http://elsewhere.invalid:1", "env"))

        data = self._client().post("/providers/flaresolverr/test", json={"url": fake.address},
                                   headers=self._headers()).get_json()["data"]

        assert (data["code"], data["source"]) == ("ok", "typed")

    def test_nothing_typed_and_nothing_in_use_has_nothing_to_test(self):
        data = self._client().post("/providers/flaresolverr/test", json={"url": ""},
                                   headers=self._headers()).get_json()["data"]

        assert (data["code"], data["source"]) == ("invalid_address", "none")
        assert data["message"] == "Type an address to test."


class TestSocketTracker:
    """The duplicates the tracker holds are always closed, and cutting shuts the connection."""

    @pytest.fixture
    def pair(self):
        left, right = socket.socketpair()
        right.settimeout(2)
        yield left, right
        left.close()
        right.close()

    def test_release_closes_the_duplicates_and_leaves_the_connection(self, pair):
        left, right = pair
        tracker = endpoints._SocketTracker()
        tracker.add(left)
        (held,) = tracker._sockets
        assert held.fileno() != -1

        tracker.release()

        assert tracker._sockets == []
        assert held.fileno() == -1
        # The connection itself is still open and usable.
        left.sendall(b"x")
        assert right.recv(1) == b"x"

    def test_cut_closes_the_duplicates_and_shuts_the_connection_down(self, pair):
        left, right = pair
        tracker = endpoints._SocketTracker()
        tracker.add(left)
        (held,) = tracker._sockets

        tracker.cut()

        assert tracker._sockets == []
        assert held.fileno() == -1
        assert right.recv(1) == b""

    def test_a_socket_added_after_the_cut_is_shut_down_and_not_kept(self, pair):
        left, right = pair
        tracker = endpoints._SocketTracker()
        tracker.cut()

        tracker.add(left)

        assert tracker._sockets == []
        assert right.recv(1) == b""

    def test_release_after_a_cut_stays_cut(self, pair):
        left, right = pair
        tracker = endpoints._SocketTracker()
        tracker.cut()
        tracker.release()

        tracker.add(left)

        assert tracker._sockets == []
        assert right.recv(1) == b""

    @pytest.fixture
    def trackers(self, monkeypatch):
        """Every tracker a Test creates, with every duplicate it ever held."""
        made = []

        class Recording(endpoints._SocketTracker):
            def __init__(self):
                super().__init__()
                self.ever_held = []
                made.append(self)

            def add(self, sock):
                super().add(sock)
                self.ever_held = [*self.ever_held, *[held for held in self._sockets if held not in self.ever_held]]

        monkeypatch.setattr(endpoints, "_SocketTracker", Recording)
        return made

    def test_a_finished_test_closes_every_duplicate(self, fake, trackers):
        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "ok"

        (tracker,) = trackers
        assert tracker.ever_held, "the Test opened a connection that was never tracked"
        assert tracker._sockets == []
        assert all(held.fileno() == -1 for held in tracker.ever_held)

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts descriptors through /proc")
    def test_a_finished_test_leaves_no_descriptor_open(self, fake):
        endpoints.check_flaresolverr_address(fake.address)  # warm up anything lazily opened
        before = len(os.listdir("/proc/self/fd"))

        for _ in range(3):
            assert endpoints.check_flaresolverr_address(fake.address)["code"] == "ok"

        # The fake server may hold a connection a moment longer; the Test must not add one each run.
        assert len(os.listdir("/proc/self/fd")) <= before + 1

    def test_a_cut_test_closes_every_duplicate(self, fake, trackers, monkeypatch):
        monkeypatch.setattr(endpoints, "ADDRESS_TEST_DEADLINE_SECONDS", 1.0)
        fake.root_header_drip = 0.5

        assert endpoints.check_flaresolverr_address(fake.address)["code"] == "timeout"

        (tracker,) = trackers
        stop = time.monotonic() + 3
        while any(t.name == "flaresolverr-address-test" for t in threading.enumerate()) and time.monotonic() < stop:
            time.sleep(0.05)
        assert tracker.ever_held, "the Test opened a connection that was never tracked"
        assert tracker._sockets == []
        assert all(held.fileno() == -1 for held in tracker.ever_held)
