"""Framed SSE parsing, repair, coalescing and explicitly owned lifecycle."""

import json
import threading
import time
from types import SimpleNamespace

import pytest



@pytest.fixture
def sportarr_enabled(monkeypatch):
    """The manager now refuses to start a stream while the master toggle is
    off, which is the point of the gate. These tests exercise the per-instance
    lifecycle underneath it, so they state the precondition explicitly."""
    from app.config import settings

    monkeypatch.setattr(settings.general, 'use_sportarr', True)

def test_sse_complete_frames_and_no_id_resync():
    from sportarr.sse_client import parse_sse

    lines = [
        ": keepalive",
        "id: 12",
        "event: file.imported",
        'data: {"eventId":1,',
        'data: "resourceType":"file"}',
        "",
        "event: stream.resync",
        'data: {"lastId": 20}',
        "",
        "id: 30",
        "data: unfinished",
    ]
    frames = list(parse_sse(lines))
    assert len(frames) == 2
    assert frames[0].id == "12" and frames[0].event == "file.imported"
    assert json.loads(frames[0].data)["eventId"] == 1
    assert frames[1].id is None and frames[1].event == "stream.resync"
    assert json.loads(frames[1].data) == {"lastId": 20}


class Transport:
    def __init__(self, lines=()):
        self.lines = list(lines)
        self.opened, self.closed = threading.Event(), threading.Event()
        self.calls = []

    def stream(self, client, cursor, on_open):
        self.calls.append(cursor)
        self.opened.set()
        on_open()
        yield from self.lines
        self.closed.wait(5)

    def close(self):
        self.closed.set()


def test_reader_enqueues_complete_frames_repairs_after_connect_and_stops():
    from sportarr.sse_client import SportarrSSEClient

    transport = Transport(
        [
            "id: 12",
            "event: file.imported",
            'data: {"eventId":8,"resourceType":"file","action":"imported"}',
            "",
            "event: stream.resync",
            'data: {"lastId":20}',
            "",
        ]
    )
    work = []
    delivered = threading.Event()

    def reconcile(owner, batch, **kwargs):
        work.append((owner, batch))
        delivered.set()

    client = SportarrSSEClient(
        1,
        ("connection",),
        SimpleNamespace(),
        transport_factory=lambda: transport,
        reconcile=reconcile,
        debounce=0.01,
    )
    client.start()
    assert delivered.wait(2)
    assert work[0][0] == 1 and work[0][1].full
    assert client.last_event_id == "20"
    assert client.stop(timeout=2) is True
    assert transport.closed.is_set() and not client.is_alive()


def test_bursts_coalesce_and_work_received_during_sync_is_not_lost():
    from sportarr.sse_client import SportarrSSEClient, SSEFrame

    transport = Transport()
    started, release, second = threading.Event(), threading.Event(), threading.Event()
    batches = []

    def reconcile(owner, batch, **kwargs):
        batches.append(batch)
        if len(batches) == 1:
            started.set()
            release.wait(2)
        else:
            second.set()

    client = SportarrSSEClient(
        1,
        (),
        SimpleNamespace(),
        transport_factory=lambda: transport,
        reconcile=reconcile,
        debounce=0.03,
    )
    client.start()
    try:
        assert started.wait(2)
        for i in range(100):
            client.dispatch(
                SSEFrame(str(i), "file.imported", '{"eventId":8,"resourceType":"file"}')
            )
        release.set()
        assert second.wait(2)
        assert len(batches) == 2 and batches[1].event_ids == {8}
    finally:
        release.set()
        assert client.stop(timeout=2)


def test_failed_work_retries_full_repair_without_advancing_unqueued_cursor():
    from sportarr.sse_client import SportarrSSEClient, SSEFrame

    transport = Transport()
    repaired = threading.Event()
    attempts = []

    def reconcile(owner, batch, **kwargs):
        attempts.append(batch)
        if len(attempts) == 1:
            raise ValueError("remote failure")
        repaired.set()

    client = SportarrSSEClient(
        1,
        (),
        SimpleNamespace(),
        transport_factory=lambda: transport,
        reconcile=reconcile,
        debounce=0.01,
        retry_min=0.01,
    )
    client.start()
    assert repaired.wait(2)
    assert all(batch.full for batch in attempts)
    assert client.stop(timeout=2)
    cursor = client.last_event_id
    assert not client.dispatch(SSEFrame("50", "file.imported", '{"eventId":8}'))
    assert client.last_event_id == cursor


def test_reconnect_uses_cursor_and_bounded_backoff():
    from sportarr.sse_client import SportarrSSEClient

    connected = threading.Event()
    transports = []

    class Dropped(Transport):
        def stream(self, client, cursor, on_open):
            self.calls.append(cursor)
            on_open()
            if len(transports) == 1:
                yield from [
                    "id: 9",
                    "event: file.imported",
                    'data: {"eventId":8,"resourceType":"file"}',
                    "",
                ]
                return
            connected.set()
            self.closed.wait(3)

    def factory():
        t = Dropped()
        transports.append(t)
        return t

    client = SportarrSSEClient(
        1,
        (),
        SimpleNamespace(),
        transport_factory=factory,
        reconcile=lambda *a, **k: None,
        debounce=0.01,
        retry_min=0.01,
        retry_max=0.02,
    )
    client.start()
    assert connected.wait(2)
    assert transports[1].calls == ["9"]
    assert client.stop(timeout=2)
    assert all(t.closed.is_set() for t in transports)


def test_manager_only_enabled_owners_replaces_changed_connection_and_joins(schema_session, sportarr_enabled):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.sse_client import SportarrClientManager

    repo = ArrInstanceRepository(schema_session)
    created = []

    class Managed:
        def __init__(self, owner, identity, client):
            self.owner, self.identity = owner, identity
            self.running = False
            created.append(self)

        def start(self):
            self.running = True

        def stop(self, timeout=6):
            self.running = False
            return True

        def is_alive(self):
            return self.running

    manager = SportarrClientManager(client_factory=Managed)
    manager.refresh(schema_session)
    assert created == []
    repo.create("sonarr", "TV")
    a, b = repo.create("sportarr", "A"), repo.create("sportarr", "B")
    repo.create("sportarr", "Off", enabled=False)
    manager.refresh(schema_session)
    assert len(created) == 2
    manager.refresh(schema_session)
    assert len(created) == 2
    repo.update(a.id, ip="changed")
    manager.refresh(schema_session)
    assert not created[0].running and len(created) == 3
    repo.update(a.id, enabled=False)
    repo.delete(b.id)
    manager.refresh(schema_session)
    assert manager.clients == {} and not any(c.running for c in created)
    manager.stop()


def test_manager_never_starts_replacement_before_old_workers_stop(schema_session, sportarr_enabled):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.sse_client import SportarrClientManager

    repo = ArrInstanceRepository(schema_session)
    owner = repo.create("sportarr", "A")
    created = []

    class Stuck:
        def __init__(self, owner, identity, client):
            self.identity, self.done = identity, False
            created.append(self)

        def start(self):
            pass

        def stop(self, timeout=6):
            return self.done

        def is_alive(self):
            return not self.done

    manager = SportarrClientManager(client_factory=Stuck)
    manager.refresh(schema_session)
    repo.update(owner.id, ip="changed")
    manager.refresh(schema_session)
    assert len(created) == 1
    created[0].done = True
    manager.refresh(schema_session)
    assert len(created) == 2
    created[1].done = True
    manager.stop()


def test_real_idle_stream_authentication_and_active_socket_cancellation():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from arr_instances.client import ArrClient
    from sportarr.sse_client import SportarrSSEClient

    request_seen, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("X-Api-Key")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            request_seen.set()
            release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = SportarrSSEClient(
        1,
        (),
        ArrClient(
            kind="sportarr",
            ip="127.0.0.1",
            port=server.server_port,
            api_key="fixture-key",
            base_url="/proxy",
        ),
        reconcile=lambda *a, **k: None,
        debounce=0.01,
    )
    try:
        client.start()
        assert request_seen.wait(2)
        start = time.monotonic()
        assert client.stop(timeout=2)
        assert time.monotonic() - start < 2
        assert requests == [("/proxy/api/stream", "fixture-key")]
    finally:
        release.set()
        client.stop(timeout=2)
        server.shutdown()
        serving.join(2)
        server.server_close()


def test_stream_routing_uses_owned_existing_events_before_remote_lookup(
    schema_session, monkeypatch
):
    import sqlalchemy as sa
    from app.database import TableSportsLeagues, TableSportsEvents
    from arr_instances.repository import ArrInstanceRepository
    from sportarr import sse_client as sse
    from sportarr.connection import connection_identity
    from sportarr.sync import events, leagues

    repo = ArrInstanceRepository(schema_session)
    a, b = repo.create("sportarr", "A"), repo.create("sportarr", "B")
    schema_session.execute(
        sa.insert(TableSportsLeagues),
        [
            dict(id=51, arr_instance_id=a.id, sportarrLeagueId=7, title="A"),
            dict(id=52, arr_instance_id=b.id, sportarrLeagueId=7, title="B"),
        ],
    )
    schema_session.execute(
        sa.insert(TableSportsEvents),
        [
            dict(
                id=61,
                league_id=51,
                arr_instance_id=a.id,
                sportarrEventId=8,
                file_id=9,
                title="A",
                path="/sports/a.mkv",
            ),
            dict(
                id=62,
                league_id=52,
                arr_instance_id=b.id,
                sportarrEventId=8,
                file_id=9,
                title="B",
                path="/sports/a.mkv",
            ),
        ],
    )
    monkeypatch.setattr(sse, "database", schema_session)
    synced, full = [], []
    monkeypatch.setattr(
        events, "sync_events", lambda local, owner, **kw: synced.append((local, owner))
    )
    monkeypatch.setattr(
        leagues, "update_sports_for_instance", lambda owner, **kw: full.append(owner)
    )
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {"id": 10, "leagueId": 7})

    kwargs = dict(
        cancel=threading.Event(),
        expected_connection=connection_identity(a),
        http_get=get,
    )
    sse.reconcile_work(a.id, sse.PendingWork(event_ids={8}), **kwargs)
    assert requests == [] and synced == [(51, a.id)] and full == []
    sse.reconcile_work(a.id, sse.PendingWork(event_ids={10}), **kwargs)
    assert requests == ["http://127.0.0.1:1867/api/events/10"] and synced[-1] == (
        51,
        a.id,
    )
    sse.reconcile_work(a.id, sse.PendingWork(league_ids={99}), **kwargs)
    assert full == [a.id]
    repo.update(a.id, enabled=False)
    with pytest.raises(ValueError):
        sse.reconcile_work(a.id, sse.PendingWork(full=True), **kwargs)


def test_frame_cursor_acknowledgment_requires_safe_enqueue():
    from sportarr.sse_client import SportarrSSEClient, SSEFrame

    client = SportarrSSEClient(1, (), SimpleNamespace())
    assert client.dispatch(SSEFrame("12", "file.imported", '{"eventId":8}'))
    assert client.last_event_id == "12"
    assert client.dispatch(SSEFrame(None, "stream.resync", '{"lastId":20}'))
    assert client.last_event_id == "20"
    assert client.dispatch(SSEFrame("1", "file.imported", '{"eventId":8}'))
    assert client.last_event_id == "1" and client._pending.full
    client.stop()
    assert not client.dispatch(SSEFrame("99", "file.imported", '{"eventId":8}'))
    assert client.last_event_id == "1"


def test_manager_disables_and_deletes_actual_readers(schema_session, sportarr_enabled):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.sse_client import SportarrClientManager, SportarrSSEClient

    repo = ArrInstanceRepository(schema_session)
    a, b = repo.create("sportarr", "A"), repo.create("sportarr", "B")
    transports, clients = [], []

    def factory(owner, identity, http):
        transport = Transport()
        transports.append(transport)
        client = SportarrSSEClient(
            owner,
            identity,
            http,
            transport_factory=lambda: transport,
            reconcile=lambda *a, **k: None,
            debounce=0.01,
        )
        clients.append(client)
        return client

    manager = SportarrClientManager(factory)
    try:
        manager.refresh(schema_session)
        assert all(transport.opened.wait(2) for transport in transports)
        manager.refresh(schema_session)
        assert len(clients) == 2
        repo.update(a.id, enabled=False)
        repo.delete(b.id)
        manager.refresh(schema_session)
        assert manager.clients == {}
        assert all(transport.closed.is_set() for transport in transports)
        assert not any(client.is_alive() for client in clients)
    finally:
        manager.stop()


@pytest.fixture
def slow_headers_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    headers_started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nX-Slow: "
            )
            headers_started.set()
            try:
                while not release.wait(0.05):
                    self.connection.sendall(b"x")
                self.connection.sendall(b"\r\n\r\n")
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, headers_started
    finally:
        release.set()
        server.shutdown()
        thread.join(2)
        server.server_close()


@pytest.mark.parametrize("phase", ["stream", "worker"])
def test_incomplete_headers_are_actively_cancelled_and_joined(
    slow_headers_server, phase
):
    from arr_instances.client import ArrClient
    from sportarr.sse_client import OwnedHTTPTransport, SportarrSSEClient

    port, headers_started = slow_headers_server
    http = ArrClient(kind="sportarr", ip="127.0.0.1", port=port)
    transport = OwnedHTTPTransport()

    def reconcile(owner, batch, **kwargs):
        kwargs["http_get"](
            http.base_url() + "/api/leagues",
            headers=http._headers(),
            timeout=30,
            verify=http.verify_ssl,
            allow_redirects=False,
        )

    if phase == "stream":
        client = SportarrSSEClient(
            1,
            (),
            http,
            transport_factory=lambda: transport,
            reconcile=lambda *a, **k: None,
            debounce=0.01,
        )
    else:
        client = SportarrSSEClient(
            1, (), http, transport_factory=Transport, reconcile=reconcile, debounce=0.01
        )
    try:
        client.start()
        client.enqueue(full=True)
        assert headers_started.wait(3)
        if phase == "worker":
            transport = client._work_transport
        process = getattr(transport, "_process", None)
        stopped = client.stop(timeout=1)
        assert stopped and not client.is_alive()
        if process is not None:
            assert process.poll() is not None
    finally:
        client.stop(timeout=2)
        transport.close()


def test_startup_repair_does_not_wait_for_stream_success():
    from sportarr.sse_client import SportarrSSEClient

    repaired = threading.Event()

    class Unavailable(Transport):
        def stream(self, client, cursor, on_open):
            self.opened.set()
            raise ValueError("stream unavailable")

    batches = []

    def reconcile(owner, batch, **kwargs):
        batches.append(batch)
        repaired.set()

    client = SportarrSSEClient(
        1,
        (),
        SimpleNamespace(),
        transport_factory=Unavailable,
        reconcile=reconcile,
        debounce=0.01,
        retry_min=0.01,
    )
    try:
        client.start()
        assert repaired.wait(1)
        assert batches[0].full
    finally:
        assert client.stop(timeout=2)


def test_subscription_after_startup_snapshot_queues_another_repair():
    from sportarr.sse_client import SportarrSSEClient

    startup_done, allow_open, subscription_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class Delayed(Transport):
        def stream(self, client, cursor, on_open):
            assert allow_open.wait(2)
            on_open()
            self.closed.wait(3)
            return iter(())

    transport = Delayed()
    batches = []

    def reconcile(owner, batch, **kwargs):
        batches.append(batch)
        (startup_done if len(batches) == 1 else subscription_done).set()

    client = SportarrSSEClient(
        1,
        (),
        SimpleNamespace(),
        transport_factory=lambda: transport,
        reconcile=reconcile,
        debounce=0.01,
    )
    try:
        client.start()
        assert startup_done.wait(1)
        allow_open.set()
        assert subscription_done.wait(1)
        assert len(batches) == 2 and all(batch.full for batch in batches)
    finally:
        allow_open.set()
        assert client.stop(timeout=2)


def _start_http_request(transport, url, phase="worker"):
    from arr_instances.client import ArrClient
    from urllib.parse import urlsplit

    errors = []

    def request():
        try:
            if phase == "worker":
                transport.get(url, timeout=30, verify=False)
            else:
                parsed = urlsplit(url)
                client = ArrClient(
                    kind="sportarr",
                    ip=parsed.hostname,
                    port=parsed.port,
                    ssl=parsed.scheme == "https",
                    verify_ssl=False,
                )
                list(transport.stream(client, None, lambda: None))
        except ValueError as error:
            errors.append(str(error))

    thread = threading.Thread(target=request)
    thread.start()
    return thread, errors


@pytest.mark.parametrize("phase", ["stream", "worker"])
def test_slow_headers_have_an_absolute_deadline(slow_headers_server, phase):
    from sportarr.transport import OwnedHTTPTransport

    port, started = slow_headers_server
    transport = OwnedHTTPTransport(setup_timeout=1)
    thread, errors = _start_http_request(transport, f"http://127.0.0.1:{port}", phase)
    try:
        assert started.wait(2)
        thread.join(2)
        assert not thread.is_alive() and errors
        assert transport._process.poll() is not None
    finally:
        transport.close()
        thread.join(2)


@pytest.mark.parametrize("cancel", [True, False])
def test_dns_setup_can_be_cancelled_and_has_an_absolute_deadline(
    tmp_path, monkeypatch, cancel
):
    import sportarr.transport as module

    marker = tmp_path / "dns-entered"
    wrapper = tmp_path / "blocked_dns.py"
    wrapper.write_text(
        "import socket, time, runpy\n"
        "from pathlib import Path\n"
        "def blocked(*args, **kwargs):\n"
        f"    Path({str(marker)!r}).touch()\n"
        "    time.sleep(60)\n"
        "socket.getaddrinfo = blocked\n"
        f'runpy.run_path({str(module.WORKER_PATH)!r}, run_name="__main__")\n'
    )
    monkeypatch.setattr(module, "WORKER_PATH", wrapper)
    transport = module.OwnedHTTPTransport(setup_timeout=2)
    thread, errors = _start_http_request(transport, "http://fixture.invalid:1234")
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        if cancel:
            transport.close()
        thread.join(3)
        assert not thread.is_alive() and errors
        assert transport._process.poll() is not None
    finally:
        transport.close()
        thread.join(2)


@pytest.mark.parametrize("phase", ["stream", "worker"])
def test_tls_setup_is_actively_cancelled(phase):
    import socketserver
    from sportarr.transport import OwnedHTTPTransport

    started, release = threading.Event(), threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            started.set()
            release.wait(5)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    transport = OwnedHTTPTransport()
    thread, errors = _start_http_request(
        transport, f"https://127.0.0.1:{server.server_address[1]}", phase
    )
    try:
        assert started.wait(2)
        transport.close()
        thread.join(1)
        assert not thread.is_alive() and errors
        assert transport._process.poll() is not None
    finally:
        transport.close()
        thread.join(2)
        release.set()
        server.shutdown()
        serving.join(2)
        server.server_close()


@pytest.fixture
def ordinary_http_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            seen.append(
                (
                    self.path,
                    self.headers.get("X-Api-Key"),
                    self.headers.get("Authorization"),
                    self.client_address,
                )
            )
            body = b"x" * 2048 if self.path == "/large" else b'{"id": 7}'
            self.send_response(302 if self.path == "/redirect" else 200)
            if self.path == "/redirect":
                self.send_header("Location", "/followed")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        serving.join(2)
        server.server_close()


def test_repair_reuses_helper_session_and_preserves_private_request_policy(
    ordinary_http_server, tmp_path, monkeypatch
):
    from sportarr.transport import OwnedHTTPTransport

    url, seen = ordinary_http_server
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login fixture password fixture\n")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    transport = OwnedHTTPTransport()
    try:
        assert transport.get(
            url + "/proxy/api/league", headers={"X-Api-Key": "fixture-key"}
        ).json() == {"id": 7}
        process = transport._process
        assert process.poll() is None
        assert (
            transport.get(
                url + "/redirect",
                headers={"X-Api-Key": "fixture-key"},
                allow_redirects=True,
            ).status_code
            == 302
        )
        assert process is transport._process and process.poll() is None
        assert [item[:3] for item in seen] == [
            ("/proxy/api/league", "fixture-key", None),
            ("/redirect", "fixture-key", None),
        ]
        assert seen[0][3] == seen[1][3]
    finally:
        transport.close()
        assert transport._process.poll() is not None


def test_worker_setup_failure_is_private_and_reaped():
    from sportarr.transport import OwnedHTTPTransport

    transport = OwnedHTTPTransport()
    with pytest.raises(ValueError) as error:
        transport.get(
            "unsupported://fixture-secret", headers={"X-Api-Key": "fixture-key"}
        )
    assert "fixture" not in str(error.value)
    assert transport._process.poll() is not None


def test_oversized_response_is_rejected_and_reaped(ordinary_http_server, monkeypatch):
    import sportarr.transport as module

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 1024)
    transport = module.OwnedHTTPTransport()
    with pytest.raises(ValueError):
        transport.get(ordinary_http_server[0] + "/large")
    assert transport._process.poll() is not None


def test_oversized_worker_message_is_rejected_and_reaped(tmp_path, monkeypatch):
    import sportarr.transport as module

    wrapper = tmp_path / "oversized.py"
    wrapper.write_text(
        f'import sys\nsys.stdin.buffer.readline()\nprint("x" * {module.MAX_MESSAGE_BYTES + 1})\n'
    )
    monkeypatch.setattr(module, "WORKER_PATH", wrapper)
    transport = module.OwnedHTTPTransport()
    with pytest.raises(ValueError):
        transport.get("http://fixture.invalid")
    assert transport._process.poll() is not None


def test_manager_replacement_and_shutdown_reap_actual_setup_helpers(schema_session, slow_headers_server, sportarr_enabled):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.sse_client import (
        OwnedHTTPTransport,
        SportarrClientManager,
        SportarrSSEClient,
    )

    port, started = slow_headers_server
    repo = ArrInstanceRepository(schema_session)
    owner = repo.create("sportarr", "A", ip="127.0.0.1", port=port)
    transports, clients = [], []

    def factory(owner_id, identity, http):
        assert all(transport._process.poll() is not None for transport in transports)
        transport = OwnedHTTPTransport()
        transports.append(transport)
        client = SportarrSSEClient(
            owner_id,
            identity,
            http,
            transport_factory=lambda: transport,
            reconcile=lambda *a, **k: None,
            debounce=0.01,
        )
        clients.append(client)
        return client

    manager = SportarrClientManager(factory)
    try:
        manager.refresh(schema_session)
        assert started.wait(2)
        started.clear()
        repo.update(owner.id, base_url="/changed")
        manager.refresh(schema_session)
        assert started.wait(2)
        assert len(clients) == 2 and not clients[0].is_alive()
        assert transports[0]._process.poll() is not None
        assert transports[1]._process.poll() is None
    finally:
        manager.stop()
        assert not any(client.is_alive() for client in clients)
        assert all(transport._process.poll() is not None for transport in transports)


def test_actual_helpers_reconnect_with_cursor_and_reap_predecessor():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from arr_instances.client import ArrClient
    from sportarr.sse_client import OwnedHTTPTransport, SportarrSSEClient

    seen, transports = [], []
    reconnected, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Last-Event-ID"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if len(seen) == 1:
                self.wfile.write(
                    b'id: 17\nevent: file.imported\ndata: {"eventId": 8}\n\n'
                )
                self.wfile.flush()
            else:
                reconnected.set()
                release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    def factory():
        assert all(transport._process.poll() is not None for transport in transports)
        transport = OwnedHTTPTransport()
        transports.append(transport)
        return transport

    client = SportarrSSEClient(
        1,
        (),
        ArrClient(kind="sportarr", ip="127.0.0.1", port=server.server_port),
        transport_factory=factory,
        reconcile=lambda *a, **k: None,
        debounce=0.01,
        retry_min=0.01,
    )
    try:
        client.start()
        assert reconnected.wait(3)
        assert seen == [None, "17"]
    finally:
        assert client.stop(timeout=2)
        release.set()
        server.shutdown()
        serving.join(2)
        server.server_close()
        assert not client.is_alive()
        assert all(transport._process.poll() is not None for transport in transports)


def test_startup_repairs_over_http_when_actual_stream_is_unavailable(
    ordinary_http_server,
):
    from arr_instances.client import ArrClient
    from urllib.parse import urlsplit
    from sportarr.sse_client import OwnedHTTPTransport, SportarrSSEClient

    url, seen = ordinary_http_server
    parsed = urlsplit(url)
    repaired = threading.Event()
    transports = []

    def factory():
        transport = OwnedHTTPTransport()
        transports.append(transport)
        return transport

    def reconcile(owner, batch, **kwargs):
        assert batch.full
        assert kwargs["http_get"](url + "/api/league").json() == {"id": 7}
        repaired.set()

    client = SportarrSSEClient(
        1,
        (),
        ArrClient(kind="sportarr", ip=parsed.hostname, port=parsed.port),
        transport_factory=factory,
        reconcile=reconcile,
        debounce=0.01,
    )
    try:
        client.start()
        assert repaired.wait(2)
        assert any(item[0] == "/api/league" for item in seen)
    finally:
        assert client.stop(timeout=2)
        assert all(transport._process.poll() is not None for transport in transports)


def test_no_enabled_owner_starts_no_http_helper(schema_session, monkeypatch):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.sse_client import SportarrClientManager
    import sportarr.transport as module

    def unexpected(*args, **kwargs):
        pytest.fail("Disabled sports runtime launched an HTTP helper")

    monkeypatch.setattr(module.subprocess, "Popen", unexpected)
    manager = SportarrClientManager()
    try:
        manager.refresh(schema_session)
        ArrInstanceRepository(schema_session).create("sportarr", "Off", enabled=False)
        manager.refresh(schema_session)
        assert manager.clients == {}
    finally:
        manager.stop()


def test_lost_parent_control_pipe_exits_blocked_helper(slow_headers_server):
    from sportarr.transport import OwnedHTTPTransport

    port, started = slow_headers_server
    transport = OwnedHTTPTransport()
    thread, errors = _start_http_request(transport, f"http://127.0.0.1:{port}")
    try:
        assert started.wait(2)
        # Parent exit closes this descriptor even when Requests has no response yet.
        transport._process.stdin.close()
        thread.join(1)
        assert not thread.is_alive() and errors
        assert transport._process.poll() is not None
    finally:
        transport.close()
        thread.join(2)


def test_the_master_toggle_stops_every_stream(schema_session, monkeypatch):
    """Selecting on the per-instance flag alone left every enabled instance's
    stream running after the operator turned Sportarr off, and each client
    enqueues a full reconciliation on start, which can reach search_after_sync.
    So the feature kept syncing and downloading after being switched off."""
    from app.config import settings
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.sse_client import SportarrClientManager

    created = []

    class Managed:
        def __init__(self, owner, identity, client):
            self.owner, self.identity = owner, identity
            self.running = False
            created.append(self)

        def start(self):
            self.running = True

        def stop(self, timeout=6):
            self.running = False
            return True

        def is_alive(self):
            return self.running

    repo = ArrInstanceRepository(schema_session)
    repo.create("sportarr", "A")
    repo.create("sportarr", "B")

    manager = SportarrClientManager(client_factory=Managed)
    monkeypatch.setattr(settings.general, 'use_sportarr', True)
    manager.refresh(schema_session)
    assert len(manager.clients) == 2
    assert all(client.running for client in created)

    # Turning the toggle off drops them, without anything else changing.
    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    manager.refresh(schema_session)
    assert manager.clients == {}
    assert not any(client.running for client in created)

    # And back on brings them up again, so the gate is not one-way.
    monkeypatch.setattr(settings.general, 'use_sportarr', True)
    manager.refresh(schema_session)
    assert len(manager.clients) == 2


def test_saving_the_master_toggle_refreshes_the_sports_runtime():
    """Gating the manager is only half of it: nothing re-ran the manager when
    the toggle was saved, so the streams and the scheduled sports jobs both
    kept running until a restart or an unrelated instance edit happened to
    refresh them. Sonarr and Radarr each restart their SignalR client from the
    same block for the same reason."""
    import inspect

    from app import config

    source = inspect.getsource(config._save_settings)
    assert "sportarr_changed = True" in source
    assert "settings-general-use_sportarr" in source
    hook = source[source.index("if sportarr_changed:"):]
    # Streams and jobs together: configure_sports_jobs is gated on the same
    # toggle, so refreshing only the streams would leave the jobs registered.
    assert "refresh_sports_runtime()" in hook.split("if update_path_map")[0]
