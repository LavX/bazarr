# coding=utf-8
"""The authenticating front: token checks, the admin path, caps, and what it never does.

The server under test is the real ``front.FrontServer`` on a loopback port, with a stub upstream standing in for the
daemon. No Docker, no container: the container half is in ``test_plugin_front_runtime``.
"""
import ast
import hashlib
import http.client
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from pathlib import Path

import pytest

from provider_hub.runtime import front

ADMIN_SECRET = "admin-secret-" + "a" * 40
TOKEN = "T" * 43
OTHER_TOKEN = "O" * 43
UNAUTHORIZED = b'{"error": "unauthorized"}'


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Upstream:
    """A stand-in for the daemon: records what it is sent and answers as told."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.body = b'{"status": "ok", "solution": {}}'
        self.content_type = "application/json"
        self.send_length = True
        self.gate = None
        self.arrived = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                return None

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length)
                outer.requests.append({"path": self.path, "body": data, "headers": dict(self.headers)})
                outer.arrived.set()
                if outer.gate is not None:
                    outer.gate.wait(10)
                self.send_response(outer.status)
                self.send_header("Content-Type", outer.content_type)
                if outer.send_length:
                    self.send_header("Content-Length", str(len(outer.body)))
                self.send_header("Set-Cookie", "session=upstream")
                self.end_headers()
                self.wfile.write(outer.body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        if self.gate is not None:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    stub = Upstream()
    yield stub
    stub.close()


class Running:
    """The proxy listener and the admin listener of one front, sharing one lease set."""

    def __init__(self, upstream, **config):
        values = dict(admin_sha256=digest(ADMIN_SECRET), upstream_host="127.0.0.1", upstream_port=upstream.port,
                      port=0, admin_port=0, upstream_timeout=5)
        values.update(config)
        self.config = front.FrontConfig(**values)
        self.server = front.FrontServer(("127.0.0.1", 0), self.config, role=front.ROLE_PROXY)
        self.admin = front.FrontServer(("127.0.0.1", 0), self.config, self.server.leases, role=front.ROLE_ADMIN)
        self.port = self.server.server_address[1]
        self.admin_port = self.admin.server_address[1]
        for server in (self.server, self.admin):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        for server in (self.server, self.admin):
            server.shutdown()
            server.server_close()

    def call(self, method, path, body=None, headers=None, timeout=10, port=None):
        """Admin calls go to the admin listener, everything else to the proxy listener, unless ``port`` says."""
        port = port or (self.admin_port if path == front.ADMIN_PATH else self.port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response
        finally:
            connection.close()

    def push(self, *tokens, secret=ADMIN_SECRET):
        payload = json.dumps({"tokens": [digest(token) for token in tokens]}).encode()
        return self.call("PUT", front.ADMIN_PATH, payload, {"Authorization": "Bearer " + secret,
                                                           "Content-Length": str(len(payload))})

    def post(self, token, body=b'{"cmd": "request.get"}', path=None, headers=None):
        headers = {"Content-Type": "application/json", **(headers or {})}
        return self.call("POST", path or f"/l/{token}/v1", body, headers)


@pytest.fixture
def running(upstream):
    instance = Running(upstream)
    yield instance
    instance.close()


# ------------------------------------------------------------------ the token


def test_the_front_starts_empty_and_refuses_everything(running, upstream):
    status, body, _ = running.post(TOKEN)

    assert (status, body) == (401, UNAUTHORIZED)
    assert upstream.requests == []


def test_a_pushed_token_is_forwarded_to_v1_with_only_the_content_type(running, upstream):
    assert running.push(TOKEN)[0] == 200

    status, body, response = running.post(TOKEN, b'{"cmd": "request.get", "url": "http://x"}',
                                          headers={"Authorization": "Bearer abc", "Cookie": "a=b",
                                                   "X-Forwarded-For": "1.2.3.4", "Content-Type": "application/json"})

    assert status == 200 and json.loads(body)["status"] == "ok"
    assert response.getheader("Content-Type") == "application/json"
    assert response.getheader("Set-Cookie") is None
    assert len(upstream.requests) == 1
    sent = upstream.requests[0]
    assert sent["path"] == "/v1" and sent["body"] == b'{"cmd": "request.get", "url": "http://x"}'
    lowered = {key.lower() for key in sent["headers"]}
    assert not lowered & {"authorization", "cookie", "x-forwarded-for"}
    assert sent["headers"]["Content-Type"] == "application/json"
    assert TOKEN not in json.dumps(sent, default=str)


def test_an_upstream_status_is_passed_on(running, upstream):
    running.push(TOKEN)
    upstream.status = 500
    upstream.body = b'{"status": "error"}'

    status, body, _ = running.post(TOKEN)

    assert status == 500 and body == b'{"status": "error"}'


def test_a_token_missing_from_the_latest_push_is_stale_at_once(running, upstream):
    running.push(TOKEN, OTHER_TOKEN)
    assert running.post(TOKEN)[0] == 200

    running.push(OTHER_TOKEN)

    assert running.post(TOKEN)[:2] == (401, UNAUTHORIZED)
    assert running.post(OTHER_TOKEN)[0] == 200
    assert len(upstream.requests) == 2

    running.push()
    assert running.post(OTHER_TOKEN)[0] == 401


@pytest.mark.parametrize("path", [
    "/",
    "/v1",
    "/healthz",
    "/l/v1",
    "/l//v1",
    "/l/short/v1",
    "/l/" + "T" * 43,
    "/l/" + "T" * 43 + "/v1/extra",
    "/l/" + "T" * 43 + "/v2",
    "/l/" + "T" * 43 + "/v1?x=1",
    "/l/" + "T" * 43 + "/v1#frag",
    "/L/" + "T" * 43 + "/v1",
    "/l/" + "T" * 129 + "/v1",
    "/l/" + "T" * 42 + "!/v1",
    "/l/%54" + "T" * 42 + "/v1",
    "/_admin",
    "/_admin/leases/",
])
def test_every_other_path_is_401_with_the_same_body(running, upstream, path):
    running.push(TOKEN)

    for method in ("GET", "POST"):
        status, body, response = running.call(method, path, b"{}" if method == "POST" else None,
                                              {"Content-Type": "application/json"} if method == "POST" else {})
        assert status == 401 and body == UNAUTHORIZED, (method, path)
        assert response.getheader("Server") == "front"
    assert upstream.requests == []


def test_the_admin_secret_is_not_a_lease_token(running, upstream):
    running.push(TOKEN)

    assert running.post(ADMIN_SECRET)[0] == 401


def test_a_valid_token_on_the_wrong_method_is_refused_after_the_check(running, upstream):
    running.push(TOKEN)

    status, _, _ = running.call("GET", f"/l/{TOKEN}/v1")

    assert status == 405 and upstream.requests == []
    assert running.call("GET", f"/l/{'X' * 43}/v1")[0] == 401


def test_the_token_check_compares_every_digest_whatever_the_first_says(monkeypatch):
    leases = front.LeaseSet()
    tokens = ["%043d" % index for index in range(7)]
    leases.replace(front.token_digest(token) for token in tokens)
    calls = []
    real = front.hmac.compare_digest
    monkeypatch.setattr(front.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))

    assert leases.allows(tokens[0]) and len(calls) == 7
    calls.clear()
    assert leases.allows(tokens[6]) and len(calls) == 7
    calls.clear()
    assert not leases.allows("x" * 43) and len(calls) == 7


def test_the_lease_set_holds_digests_and_never_a_token():
    leases = front.LeaseSet()
    leases.replace([front.token_digest(TOKEN)])

    assert TOKEN.encode() not in b"".join(leases._digests)
    assert len(leases) == 1
    assert leases.allows(TOKEN) and not leases.allows(OTHER_TOKEN)
    assert not leases.allows("é" * 40)


# ------------------------------------------------------------------ the admin path


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": "Basic " + ADMIN_SECRET},
    {"Authorization": ADMIN_SECRET},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer " + ADMIN_SECRET[:-1]},
    {"Authorization": "Bearer " + ADMIN_SECRET + "x"},
])
def test_the_admin_path_needs_the_secret(running, headers):
    payload = json.dumps({"tokens": [digest(TOKEN)]}).encode()

    status, body, _ = running.call("PUT", front.ADMIN_PATH, payload, {**headers, "Content-Length": str(len(payload))})
    getter = running.call("GET", front.ADMIN_PATH, None, headers)

    assert (status, body) == (401, UNAUTHORIZED) and getter[:2] == (401, UNAUTHORIZED)
    assert running.post(TOKEN)[0] == 401


def test_the_admin_path_replaces_the_set_and_reports_the_count(running):
    secret = {"Authorization": "Bearer " + ADMIN_SECRET}
    status, body, _ = running.push(TOKEN, OTHER_TOKEN)
    assert status == 200 and json.loads(body) == {"count": 2}
    state = json.loads(running.call("GET", front.ADMIN_PATH, None, secret)[1])
    assert state["count"] == 2 and state["fingerprint"] == running.server.leases.fingerprint()

    assert json.loads(running.push(TOKEN)[1]) == {"count": 1}
    assert json.loads(running.push()[1]) == {"count": 0}
    assert running.call("DELETE", front.ADMIN_PATH, None, secret)[0] == 405


@pytest.mark.parametrize("payload", [
    b"not json",
    b"[]",
    b"{}",
    b'{"tokens": "x"}',
    b'{"tokens": ["short"]}',
    b'{"tokens": [5]}',
    b'{"tokens": ["' + b"A" * 64 + b'"]}',
    json.dumps({"tokens": ["%064x" % index for index in range(front.MAX_LEASES + 1)]}).encode(),
])
def test_a_bad_lease_set_is_400_and_changes_nothing(running, payload):
    running.push(TOKEN)

    status, _, _ = running.call("PUT", front.ADMIN_PATH, payload, {"Authorization": "Bearer " + ADMIN_SECRET,
                                                                   "Content-Length": str(len(payload))})

    assert status == 400
    assert running.post(TOKEN)[0] == 200
    assert json.loads(running.call("GET", front.ADMIN_PATH, None,
                                   {"Authorization": "Bearer " + ADMIN_SECRET})[1])["count"] == 1


def test_the_admin_body_has_its_own_small_cap(running):
    payload = b"x" * (front.MAX_ADMIN_BODY + 1)

    status, _, _ = running.call("PUT", front.ADMIN_PATH, payload, {"Authorization": "Bearer " + ADMIN_SECRET,
                                                                   "Content-Length": str(len(payload))})

    assert status == 413


# ------------------------------------------------------------------ caps


def test_a_request_over_the_body_cap_is_413_and_never_reaches_the_daemon(upstream):
    instance = Running(upstream, max_body=100)
    try:
        instance.push(TOKEN)

        assert instance.post(TOKEN, b"x" * 101)[0] == 413
        assert upstream.requests == []
        assert instance.post(TOKEN, b"x" * 100)[0] == 200
    finally:
        instance.close()


def test_a_body_needs_a_content_length_and_no_transfer_encoding(running, upstream):
    running.push(TOKEN)
    for extra in (b"Transfer-Encoding: chunked\r\n", b""):
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
            sock.sendall(b"POST /l/" + TOKEN.encode() + b"/v1 HTTP/1.0\r\n" + extra + b"\r\n0\r\n\r\n")
            assert sock.recv(64).startswith(b"HTTP/1.0 411")
    assert upstream.requests == []


def test_a_reply_over_the_cap_or_without_a_length_is_502(upstream):
    instance = Running(upstream, max_response=10)
    try:
        instance.push(TOKEN)
        assert instance.post(TOKEN)[0] == 502

        upstream.body = b"0123456789"
        assert instance.post(TOKEN)[:2] == (200, b"0123456789")

        upstream.send_length = False
        assert instance.post(TOKEN)[0] == 502
    finally:
        instance.close()


def test_an_unreachable_daemon_is_502(running, upstream):
    running.push(TOKEN)
    upstream.close()

    status, body, _ = running.post(TOKEN)

    assert status == 502 and b"unavailable" in body


def test_a_connection_beyond_the_limit_is_503(upstream, monkeypatch):
    monkeypatch.setattr(front, "MAX_CONCURRENT", 1)
    instance = Running(upstream)
    try:
        instance.push(TOKEN)
        upstream.gate = threading.Event()
        holder = threading.Thread(target=lambda: instance.post(TOKEN), daemon=True)
        holder.start()
        assert upstream.arrived.wait(5)

        status = refused(lambda: instance.post(TOKEN))

        assert status == 503
        upstream.gate.set()
        holder.join(5)
        assert instance.post(TOKEN)[0] == 200
    finally:
        instance.close()


# ------------------------------------------------------------------ the network check


def test_a_connection_outside_the_allowed_network_is_dropped_even_with_a_token(upstream):
    instance = Running(upstream, allow_net="192.0.2.0/24")
    try:
        # The admin path is the way in for the token, so allow it through a second front to prepare the set.
        instance.server.leases.replace([front.token_digest(TOKEN)])

        with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
            instance.post(TOKEN)
        assert upstream.requests == []
    finally:
        instance.close()


def test_a_connection_inside_the_allowed_network_is_served(upstream):
    instance = Running(upstream, allow_net="127.0.0.0/8")
    try:
        instance.server.leases.replace([front.token_digest(TOKEN)])

        assert instance.post(TOKEN)[0] == 200
    finally:
        instance.close()


# ------------------------------------------------------------------ nothing leaks


def test_no_token_secret_or_path_is_ever_written_out(running, upstream, capsys, caplog):
    running.push(TOKEN)
    caplog.set_level(0)

    running.post(TOKEN)
    running.post(OTHER_TOKEN)
    running.call("GET", f"/l/{OTHER_TOKEN}/v1")
    running.push(TOKEN, secret="wrong-secret")

    out = capsys.readouterr()
    everything = out.out + out.err + caplog.text
    for secret in (TOKEN, OTHER_TOKEN, ADMIN_SECRET, "wrong-secret", "/l/", "/_admin"):
        assert secret not in everything
    assert "unauthorized request" in out.err


def test_refusals_are_summarised_at_most_once_a_minute(upstream, capsys):
    now = [1000.0]
    config = front.FrontConfig(admin_sha256=digest(ADMIN_SECRET), upstream_host="127.0.0.1",
                               upstream_port=upstream.port)
    server = front.FrontServer(("127.0.0.1", 0), config, clock=lambda: now[0])
    try:
        for _ in range(5):
            server.note_refusal("198.51.100.9")
        now[0] += 61
        server.note_refusal("198.51.100.9")
    finally:
        server.server_close()

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2 and "1 unauthorized" in lines[0] and "5 unauthorized" in lines[1]


# ------------------------------------------------------------------ the configuration


def good_env(**overrides):
    env = {front.ENV_ADMIN_SHA256: digest(ADMIN_SECRET), front.ENV_UPSTREAM: "daemon:8191"}
    env.update(overrides)
    return env


def test_the_front_reads_its_configuration_from_the_environment():
    config = front.FrontConfig.from_env(good_env(**{front.ENV_ALLOW_NET: "198.51.100.0/24",
                                                    front.ENV_MAX_BODY: "5000", front.ENV_PORT: "9000",
                                                    front.ENV_ADMIN_PORT: "9001"}))

    assert (config.upstream_host, config.upstream_port, config.port) == ("daemon", 8191, 9000)
    assert config.admin_port == 9001 and front.FrontConfig.from_env(good_env()).admin_port == front.ADMIN_PORT
    assert str(config.allow_net) == "198.51.100.0/24" and config.max_body == 5000
    assert config.admin_digest == bytes.fromhex(digest(ADMIN_SECRET))
    assert front.FrontConfig.from_env(good_env()).port == front.FRONT_PORT


def test_the_two_listeners_cannot_share_a_port():
    with pytest.raises(ValueError, match="differ"):
        front.FrontConfig.from_env(good_env(**{front.ENV_PORT: "9000", front.ENV_ADMIN_PORT: "9000"}))
    assert front.FrontConfig(admin_sha256=digest(ADMIN_SECRET), upstream_host="x", upstream_port=1, port=0,
                             admin_port=0).port == 0


def test_the_header_limits_the_front_tightens_still_exist_in_the_standard_library():
    assert isinstance(http.client._MAXLINE, int) and isinstance(http.client._MAXHEADERS, int)


def test_an_admin_head_that_is_trickled_is_cut_off_much_sooner_than_a_proxy_head(upstream, monkeypatch):
    monkeypatch.setattr(front, "ADMIN_HEAD_DEADLINE", 0.3)
    instance = Running(upstream)
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", instance.admin_port), timeout=5) as sock:
            sock.sendall(b"PUT /_admin/leases HTTP/1.0\r\n")
            assert sock.recv(64) == b"" and time.monotonic() - started < 3
    finally:
        instance.close()


@pytest.mark.parametrize("overrides", [
    {front.ENV_ADMIN_SHA256: ""},
    {front.ENV_ADMIN_SHA256: "A" * 64},
    {front.ENV_ADMIN_SHA256: "a" * 63},
    {front.ENV_UPSTREAM: "daemon"},
    {front.ENV_UPSTREAM: "daemon:0"},
    {front.ENV_UPSTREAM: "daemon:70000"},
    {front.ENV_UPSTREAM: "http://daemon:8191"},
    {front.ENV_UPSTREAM: "da emon:8191"},
    {front.ENV_PORT: "x"},
    {front.ENV_PORT: "70000"},
    {front.ENV_ADMIN_PORT: "70000"},
    {front.ENV_ALLOW_NET: "not-a-network"},
    {front.ENV_MAX_BODY: "0"},
    {front.ENV_MAX_RESPONSE: "999999999999"},
    {front.ENV_UPSTREAM_TIMEOUT: "0"},
])
def test_a_bad_configuration_is_refused(overrides):
    with pytest.raises(ValueError):
        front.FrontConfig.from_env(good_env(**overrides))


def test_main_exits_with_2_on_a_bad_configuration_and_says_nothing_secret(capsys):
    env = good_env(**{front.ENV_ADMIN_SHA256: "nope"})

    assert front.main(env) == 2
    assert "nope" not in capsys.readouterr().err


def test_the_front_imports_only_the_standard_library():
    source = Path(front.__file__).read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import cannot run as a script"
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}


def test_the_launch_command_names_the_file_the_image_carries():
    from provider_hub.runtime.front_runtime import FRONT_ENTRYPOINT, FRONT_SCRIPT

    suffix = "bazarr/provider_hub/runtime/front.py"
    assert Path(front.__file__).as_posix().endswith(suffix)
    assert FRONT_SCRIPT == "/app/bazarr/" + suffix
    assert FRONT_ENTRYPOINT == ("python3", "-I", FRONT_SCRIPT)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_program_runs_as_a_script_under_isolated_mode_and_stops_on_sigterm(upstream):
    port, admin_port = free_port(), free_port()
    env = {"PATH": os.environ.get("PATH", ""), **good_env(**{
        front.ENV_UPSTREAM: "127.0.0.1:%d" % upstream.port, front.ENV_PORT: str(port),
        front.ENV_ADMIN_PORT: str(admin_port)})}
    process = subprocess.Popen([sys.executable, "-I", front.__file__], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                socket.create_connection(("127.0.0.1", admin_port), timeout=1).close()
                break
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", f"/l/{TOKEN}/v1", b"{}", {"Content-Type": "application/json"})
        assert connection.getresponse().status == 401
        payload = json.dumps({"tokens": [digest(TOKEN)]}).encode()
        connection = http.client.HTTPConnection("127.0.0.1", admin_port, timeout=5)
        connection.request("PUT", front.ADMIN_PATH, payload, {"Authorization": "Bearer " + ADMIN_SECRET})
        assert connection.getresponse().status == 200
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", f"/l/{TOKEN}/v1", b"{}", {"Content-Type": "application/json"})
        assert connection.getresponse().status == 200
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        log = process.stderr.read()
        assert TOKEN not in log and ADMIN_SECRET not in log
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stderr.close()


# ------------------------------------------------------------------ the admin path has capacity of its own


def refused(call):
    """The status of ``call()``, or 503 when the front hung up on a body it had refused to take."""
    try:
        return call()[0]
    except (BrokenPipeError, ConnectionResetError):
        return 503


def hold_proxy_slots(instance, upstream, count):
    """Park ``count`` proxied requests at the daemon, so every proxy slot is taken."""
    upstream.gate = threading.Event()
    threads = [threading.Thread(target=lambda: instance.post(TOKEN), daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while len(upstream.requests) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(upstream.requests) == count
    return threads


def test_a_saturated_proxy_never_blocks_a_push_so_a_revoke_lands(running, upstream):
    running.push(TOKEN, OTHER_TOKEN)
    threads = hold_proxy_slots(running, upstream, front.MAX_CONCURRENT)
    try:
        assert refused(lambda: running.post(TOKEN)) == 503

        status, body, _ = running.push(OTHER_TOKEN)
        state = running.call("GET", front.ADMIN_PATH, None, {"Authorization": "Bearer " + ADMIN_SECRET})

        assert status == 200 and json.loads(body) == {"count": 1} and state[0] == 200
        assert refused(lambda: running.post(TOKEN)) == 503
    finally:
        upstream.gate.set()
        for thread in threads:
            thread.join(5)
    # The slots are free again, and the revoked token is refused where the other still works.
    assert running.post(TOKEN)[0] == 401
    assert running.post(OTHER_TOKEN)[0] == 200


def test_the_admin_listener_has_its_own_small_limit(upstream, monkeypatch):
    monkeypatch.setattr(front, "MAX_ADMIN_CONCURRENT", 1)
    instance = Running(upstream)
    try:
        with socket.create_connection(("127.0.0.1", instance.admin_port), timeout=5) as holder:
            holder.sendall(b"PUT /_admin/leases HTTP/1.0\r\n")
            time.sleep(0.2)
            assert refused(lambda: instance.push(TOKEN)) == 503
        assert wait_for(lambda: refused(lambda: instance.push(TOKEN)) == 200)
    finally:
        instance.close()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_each_listener_answers_only_its_own_routes(running, upstream):
    running.push(TOKEN)
    secret = {"Authorization": "Bearer " + ADMIN_SECRET}
    payload = json.dumps({"tokens": []}).encode()

    on_proxy = running.call("PUT", front.ADMIN_PATH, payload, {**secret, "Content-Length": str(len(payload))},
                            port=running.port)
    on_admin = running.call("POST", f"/l/{TOKEN}/v1", b"{}", {"Content-Type": "application/json"},
                            port=running.admin_port)

    assert on_proxy[:2] == (401, UNAUTHORIZED) and on_admin[:2] == (401, UNAUTHORIZED)
    assert upstream.requests == [] and len(running.server.leases) == 1


# ------------------------------------------------------------------ time is bounded by the wall clock


def test_a_request_head_that_is_trickled_is_cut_off_at_the_deadline(upstream, monkeypatch):
    monkeypatch.setattr(front, "HEAD_DEADLINE", 0.4)
    instance = Running(upstream)
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", instance.port), timeout=5) as sock:
            closed = False
            for _ in range(40):
                try:
                    sock.sendall(b"x")
                except OSError:
                    closed = True
                    break
                time.sleep(0.1)
        assert closed and time.monotonic() - started < 3
    finally:
        instance.close()


def test_a_body_that_is_trickled_is_cut_off_at_the_deadline(upstream, monkeypatch):
    monkeypatch.setattr(front, "BODY_DEADLINE", 0.4)
    instance = Running(upstream)
    try:
        instance.push(TOKEN)
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", instance.port), timeout=5) as sock:
            sock.sendall(b"POST /l/" + TOKEN.encode() + b"/v1 HTTP/1.0\r\nContent-Length: 100\r\n\r\nab")
            assert sock.recv(64) == b"" and time.monotonic() - started < 3
        assert upstream.requests == []
    finally:
        instance.close()


def test_a_proxied_exchange_is_bounded_in_total_and_frees_its_slot(upstream):
    instance = Running(upstream, upstream_timeout=1)
    try:
        instance.push(TOKEN)
        upstream.gate = threading.Event()
        started = time.monotonic()

        with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
            instance.post(TOKEN)

        assert time.monotonic() - started < 4
        upstream.gate.set()
        assert wait_for(lambda: refused(lambda: instance.post(TOKEN)) == 200)
    finally:
        instance.close()


def test_a_connection_from_outside_the_allowed_network_is_closed_before_it_is_read(upstream):
    instance = Running(upstream, allow_net="192.0.2.0/24")
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", instance.port), timeout=5) as sock:
            # Nothing is sent: a front that waited for a request head would hold this open until its deadline.
            assert sock.recv(64) == b"" and time.monotonic() - started < 2
        with socket.create_connection(("127.0.0.1", instance.admin_port), timeout=5) as sock:
            assert sock.recv(64) == b""
    finally:
        instance.close()


@pytest.mark.parametrize("length", [b"\xb2", b"1\xb2", b"-1", b"+5", b"1_0", b" "])
def test_a_content_length_that_is_not_plain_ascii_digits_is_refused(running, upstream, length):
    running.push(TOKEN)
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
        sock.sendall(b"POST /l/" + TOKEN.encode() + b"/v1 HTTP/1.0\r\nContent-Length: " + length + b"\r\n\r\n")
        assert sock.recv(64).startswith(b"HTTP/1.0 411")
    assert upstream.requests == []
