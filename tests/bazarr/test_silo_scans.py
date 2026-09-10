"""Real HTTP/WebSocket protocol tests for causal native file scans."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import ipaddress
import socket
import ssl
import threading
import time

import pytest
from aiohttp import ClientConnectionError, web


PATH = "/media/Movie.mkv"
STAMP = "automatic fixture timestamp"
CLEAN_RESULT = {"new": 0, "updated": 0, "unchanged": 0, "missing": 0, "missing_skipped_protected": 0,
                "files_deleted": 0, "memberships_removed": 0, "items_deleted": 0, "matched_files": 0,
                "retried_items": 1, "still_unmatched_warnings": 1, "skipped": 0, "errors": 0}


def run_row(run_id="fresh", *, library=7, mode="file", path=PATH, status="accepted", result=None):
    row = {"id": run_id, "library_id": library, "mode": mode, "trigger": "path", "status": status}
    if mode != "library":
        row["path"] = path
    if status != "accepted":
        row["started_at"] = STAMP
    if status in {"completed", "failed", "cancelled"}:
        row["completed_at"] = STAMP
        row["result"] = copy.deepcopy(CLEAN_RESULT if result is None else result)
    return row


def scan_event(row, *, event_id=None, event=None, timestamp=STAMP):
    suffix = "started" if row["status"] == "running" else row["status"]
    return {"type": "event", "channel": "scans", "event": event or "scan." + suffix,
            "event_id": event_id or f"event-{row['id']}-{suffix}", "timestamp": timestamp, "data": row}


class NativeService:
    """A threaded local service with explicit event gates, never real credentials."""

    def __init__(self, *, snapshot=(), handshake=None, after_snapshot=None, on_post=None,
                 ws_status=None, post_reply=None, prefix="", tls_context=None, before_ws=None, listen_host="127.0.0.1"):
        self.snapshot = list(snapshot)
        self.handshake = handshake
        self.after_snapshot = after_snapshot
        self.on_post = on_post
        self.ws_status = ws_status
        self.post_reply = post_reply
        self.prefix = prefix
        self.tls_context = tls_context
        self.before_ws = before_ws
        self.listen_host = listen_host
        self.clock = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.run_starts = {}
        self.records = []
        self.sequence = []
        self.posts = []
        self.ws_closed = threading.Event()
        self.ready = threading.Event()
        self.started = threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.serve()), daemon=True)
        self.thread.start()
        assert self.ready.wait(3), "native fixture did not start"
        if hasattr(self, "startup_error"):
            raise self.startup_error

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, self.listen_host, 0, ssl_context=self.tls_context)
        try:
            await site.start()
        except Exception as error:
            self.startup_error = error
            self.ready.set()
            await runner.cleanup()
            return
        port = site._server.sockets[0].getsockname()[1]
        host = f"[{self.listen_host}]" if ":" in self.listen_host else self.listen_host
        self.url = f"{'https' if self.tls_context else 'http'}://{host}:{port}{self.prefix}"
        self.ready.set()
        try:
            await self.stop.wait()
        finally:
            self.release.set()
            await runner.cleanup()

    def close(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop.set)
            self.thread.join(3)
        assert not self.thread.is_alive(), "native fixture leaked its server thread"

    async def send(self, payload):
        # The native hub drops events when the observer disconnects. Scan HTTP
        # acceptance still succeeds independently of that subscriber's socket.
        payload = copy.deepcopy(payload)
        rows = payload.get("data") if payload.get("type") == "snapshot" else [payload.get("data")]
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("started_at") == STAMP:
                    row["started_at"] = self.run_starts.setdefault(row["id"], self.tick())
                if row.get("completed_at") == STAMP:
                    row["completed_at"] = self.tick()
                for key in ("started_at", "completed_at"):
                    self.observe_time(row.get(key))
        if payload.get("timestamp") == STAMP:
            payload["timestamp"] = self.tick()
        else:
            self.observe_time(payload.get("timestamp"))
        try:
            await self.ws.send_json(payload)
        except ClientConnectionError:
            pass

    def observe_time(self, value):
        if isinstance(value, str):
            try:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo:
                    self.clock = max(self.clock, stamp)
            except ValueError:
                pass

    def tick(self):
        self.clock += timedelta(microseconds=100)
        return self.clock.isoformat().replace("+00:00", "Z")

    async def complete(self, run_id="fresh", **kwargs):
        self.sequence.append(f"completed:{run_id}")
        await self.send(scan_event(run_row(run_id, status="completed", **kwargs)))

    async def handle(self, request):
        record = {"method": request.method, "path": request.path_qs, "headers": dict(request.headers)}
        self.records.append(record)
        path = request.path.removeprefix(self.prefix)
        if path == "/capture":
            return web.Response(status=500)
        if request.headers.get("Authorization") != "Bearer synthetic-key":
            return web.Response(status=401)
        if path == "/api/v1/events/ws":
            if self.before_ws:
                self.started.set()
                await self.before_ws(self)
            if self.ws_status:
                return web.Response(status=self.ws_status, headers={"Location": self.url + "/capture"})
            self.ws = web.WebSocketResponse(autoping=False)
            await self.ws.prepare(request)
            frames = self.handshake if self.handshake is not None else [
                {"type": "hello", "schema_version": 1, "connection_id": "synthetic-connection",
                 "available_channels": ["scans"], "required_action": "none"},
                {"type": "subscribed", "channels": ["scans"]},
                {"type": "snapshot", "channel": "scans", "timestamp": STAMP, "data": self.snapshot}]
            try:
                for frame in frames:
                    await self.send(frame)
                self.sequence.append("snapshot")
                if self.after_snapshot:
                    await self.after_snapshot(self)
                async for message in self.ws:
                    if message.type.name == "PONG":
                        self.sequence.append("pong")
                        self.release.set()
            finally:
                self.ws_closed.set()
            return self.ws
        if path == "/api/v1/scan":
            record["json"] = await request.json()
            self.posts.append(record["json"])
            self.sequence.append("post")
            self.started.set()
            if self.on_post:
                await self.on_post(self)
            elif not self.post_reply:
                await self.send(scan_event(run_row()))
                await self.complete()
            if self.post_reply:
                if callable(self.post_reply):
                    return await self.post_reply(request, self)
                status, body = self.post_reply
                if status in {301, 302, 307, 308}:
                    return web.Response(status=status, headers={"Location": self.url + "/capture"})
                if isinstance(body, bytes):
                    return web.Response(status=status, body=body)
                return web.json_response(body, status=status)
            return web.json_response({"status": "accepted", "mode": "file", "library_id": 7}, status=202)
        return web.Response(status=404)


@pytest.fixture
def silo_fixture():
    servers = []

    def start(**kwargs):
        server = NativeService(**kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def refresh(server, *, timeout=0.5, library="0007", path=PATH, verify_ssl=True):
    from silo.client import SiloClient
    with SiloClient(server.url, "synthetic-key", verify_ssl) as client:
        return client.refresh_file(library, path, timeout=timeout)


@pytest.mark.parametrize('phase', ['connection', 'initial_barrier', 'trailing_barrier', 'completion'])
def test_revision_invalidation_prevents_initial_or_trailing_scan(silo_fixture, phase):
    from silo.client import SiloClient
    from media_servers.http import MediaServerError
    valid = phase != 'connection'

    def guard():
        if not valid:
            raise MediaServerError('configuration_changed')

    async def initial(server):
        nonlocal valid
        valid = False
        await server.complete('old')

    async def on_post(server):
        nonlocal valid
        if phase == 'trailing_barrier':
            # The initial request reused an observed run. Its completion only
            # clears a barrier and must not authorize another POST after a save.
            await server.send(scan_event(run_row('reused', status='running')))
            valid = False
            await server.complete('reused')
        else:
            await server.send(scan_event(run_row()))
            valid = False
            await server.complete()

    server = silo_fixture(snapshot=[run_row('old', status='running')] if phase == 'initial_barrier' else (),
                          after_snapshot=initial if phase == 'initial_barrier' else None, on_post=on_post)
    with SiloClient(server.url, 'synthetic-key') as client:
        with pytest.raises(MediaServerError, match='^configuration_changed$'):
            client.refresh_file('7', PATH, timeout=1, ensure_current=guard)
    assert len(server.posts) == (1 if phase in {'trailing_barrier', 'completion'} else 0)
    if phase == 'connection':
        assert server.records == []
    else:
        assert server.ws_closed.wait(1)


def test_fresh_clean_scan_confirms_even_with_zero_metadata_updates(silo_fixture, monkeypatch):
    server = silo_fixture(prefix="/reverse/silo")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert refresh(server) == {"status": "confirmed"}
    assert server.posts == [{"library_id": 7, "path": "/media/Movie.mkv"}]
    assert [r["path"] for r in server.records] == [
        "/reverse/silo/api/v1/events/ws?channels=scans", "/reverse/silo/api/v1/scan"]
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize(("mode", "path"), [("file", PATH), ("subtree", "/media"), ("library", None)])
def test_preexisting_overlapping_run_must_finish_before_trailing_scan(silo_fixture, mode, path):
    async def finish_old(server):
        await server.complete("old", mode=mode, path=path, result={**CLEAN_RESULT, "skipped": 1})

    server = silo_fixture(snapshot=[run_row("old", mode=mode, path=path, status="running")],
                          after_snapshot=finish_old)
    assert refresh(server) == {"status": "confirmed"}
    assert server.sequence.index("completed:old") < server.sequence.index("post")
    assert len(server.posts) == 1


@pytest.mark.parametrize(("library", "mode", "path"), [(8, "library", None), (7, "file", "/media/Other.mkv"),
                                                       (7, "subtree", "/media2")])
def test_unrelated_active_runs_do_not_block_target(silo_fixture, library, mode, path):
    server = silo_fixture(snapshot=[run_row("unrelated", library=library, mode=mode, path=path)])
    assert refresh(server) == {"status": "confirmed"}


@pytest.mark.parametrize("counter", ["skipped", "errors", "missing", "missing_skipped_protected"])
def test_completed_with_unclean_work_never_confirms(silo_fixture, counter):
    from media_servers.http import MediaServerError

    async def unclean(server):
        await server.send(scan_event(run_row()))
        await server.complete(result={**CLEAN_RESULT, counter: 1})

    server = silo_fixture(on_post=unclean)
    with pytest.raises(MediaServerError, match="^scan_incomplete$"):
        refresh(server)
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_cancelled_run_never_confirms_or_exposes_error_text(silo_fixture, status):
    from media_servers.http import MediaServerError

    async def fail(server):
        await server.send(scan_event(run_row()))
        row = run_row(status=status)
        row["error_message"] = "synthetic-private-server-detail"
        await server.send(scan_event(row))

    server = silo_fixture(on_post=fail)
    with pytest.raises(MediaServerError) as error:
        refresh(server)
    assert str(error.value) == f"scan_{status}"
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize("result", [{}, {**CLEAN_RESULT, "errors": False}, {**CLEAN_RESULT, "missing": -1}])
def test_absent_or_invalid_clean_counters_are_not_completion_proof(silo_fixture, result):
    from media_servers.http import MediaServerError

    async def malformed(server):
        await server.send(scan_event(run_row()))
        await server.complete(result=result)

    server = silo_fixture(on_post=malformed)
    with pytest.raises(MediaServerError, match="^invalid_response$"):
        refresh(server)


def test_reused_run_without_fresh_acceptance_requires_another_post(silo_fixture):
    async def reuse_then_fresh(server):
        if len(server.posts) == 1:
            await server.send(scan_event(run_row("reused", status="running")))
            await server.complete("reused")
        else:
            await server.send(scan_event(run_row("trailing")))
            await server.complete("trailing")

    server = silo_fixture(on_post=reuse_then_fresh)
    assert refresh(server) == {"status": "confirmed"}
    assert len(server.posts) == 2
    assert server.sequence.index("completed:reused") < len(server.sequence) - 1


def test_new_overlapping_full_scan_requires_trailing_scan(silo_fixture):
    async def conflict_then_fresh(server):
        if len(server.posts) == 1:
            await server.send(scan_event(run_row()))
            await server.send(scan_event(run_row("full", mode="library")))
            await server.complete()
            await server.complete("full", mode="library")
        else:
            await server.send(scan_event(run_row("trailing")))
            await server.complete("trailing")

    server = silo_fixture(on_post=conflict_then_fresh)
    assert refresh(server) == {"status": "confirmed"}
    assert len(server.posts) == 2
    assert server.sequence.index("completed:full") < len(server.sequence) - 1


def test_event_envelope_ids_cannot_substitute_for_matching_run_ids(silo_fixture):
    from media_servers.http import MediaServerError

    async def unrelated(server):
        await server.send(scan_event(run_row("target"), event_id="same-envelope"))
        await server.send(scan_event(run_row("other", status="completed", path="/media/Other.mkv"),
                                     event_id="same-envelope"))

    server = silo_fixture(on_post=unrelated)
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)


@pytest.mark.parametrize("fault", ["disconnect", "oversized", "invalid_json"])
def test_stream_loss_or_invalid_frames_never_confirms_and_releases_socket(silo_fixture, fault):
    from media_servers.http import MediaServerError

    async def broken(server):
        if fault == "disconnect":
            await server.ws.close()
        elif fault == "oversized":
            await server.ws.send_str("x" * (2 * 1024 * 1024 + 1))
        else:
            await server.ws.send_str("not-json")

    server = silo_fixture(after_snapshot=broken)
    with pytest.raises(MediaServerError) as error:
        refresh(server)
    assert error.value.code == {"disconnect": "stream_disconnected", "oversized": "response_too_large",
                                "invalid_json": "invalid_response"}[fault]
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize("handshake", [[], [{"type": "subscribed", "channels": []}],
                                      [{"type": "snapshot", "channel": "scans", "data": []}]])
def test_missing_handshake_never_submits_a_scan(silo_fixture, handshake):
    from media_servers.http import MediaServerError
    server = silo_fixture(handshake=handshake)
    with pytest.raises(MediaServerError) as error:
        refresh(server, timeout=0.08)
    assert error.value.code in {"timeout", "observation_incomplete"}
    assert server.posts == []
    assert server.ws_closed.wait(1)


def test_capped_500_row_snapshot_is_unknown_even_when_rows_are_unrelated(silo_fixture):
    from media_servers.http import MediaServerError
    server = silo_fixture(snapshot=[run_row(f"other-{i}", library=8) for i in range(500)])
    with pytest.raises(MediaServerError, match="^observation_incomplete$"):
        refresh(server)
    assert server.posts == []


@pytest.mark.parametrize(("status", "body"), [(200, {"status": "accepted", "mode": "file", "library_id": 7}),
                                            (202, {"status": "accepted", "mode": "library", "library_id": 7}),
                                            (202, {"status": "accepted", "mode": "file", "library_id": 8}),
                                            (202, {"status": "accepted", "mode": "file", "library_id": "7"}),
                                            (202, b"not-json")])
def test_invalid_http_acceptance_cannot_confirm_even_with_clean_events(silo_fixture, status, body):
    from media_servers.http import MediaServerError

    async def clean(server):
        await server.send(scan_event(run_row()))
        await server.complete()

    server = silo_fixture(on_post=clean, post_reply=(status, body))
    with pytest.raises(MediaServerError) as error:
        refresh(server)
    assert error.value.code in {"invalid_response", "request_rejected"}


def test_the_documented_refusal_of_a_path_is_a_miss_not_a_failure(silo_fixture):
    """Silo answers 400 for a path it cannot place and for a container its
    scanner does not read. Retrying that scan cannot change the answer, so the
    file rung has nothing left to say and the ladder climbs on."""
    server = silo_fixture(post_reply=(400, {"error": "bad_request"}))
    assert refresh(server) is None


@pytest.mark.parametrize("status", [409, 422, 429])
def test_a_rejected_scan_request_is_a_failure_not_a_path_silo_cannot_place(silo_fixture, status):
    """A conflict, an unprocessable body or a rate limit are all the server
    refusing this request, not the server saying it holds no such file. Reading
    them as a miss escalates a working destination to whole-library scans and
    reports every one of them as a success."""
    from media_servers.http import MediaServerError
    server = silo_fixture(post_reply=(status, {}))
    with pytest.raises(MediaServerError) as error:
        refresh(server)
    assert str(error.value) == "request_rejected"


def test_a_refused_event_stream_handshake_is_never_a_missing_file(silo_fixture):
    """The file scan is observed over /api/v1/events/ws. A handshake the server
    refuses breaks every per-file refresh, and it has to surface as one."""
    from media_servers.http import MediaServerError
    server = silo_fixture(ws_status=400)
    with pytest.raises(MediaServerError) as error:
        refresh(server)
    assert str(error.value) == "request_rejected"
    assert all("/api/v1/scan" not in record["path"] for record in server.records)


@pytest.mark.parametrize("endpoint", ["ws", "post"])
@pytest.mark.parametrize(("status", "code"), [(302, "redirect_denied"), (307, "redirect_denied"),
                                            (401, "unauthorized"), (403, "forbidden"), (500, "server_error")])
def test_http_and_ws_failures_are_sanitized_and_redirects_never_reach_target(silo_fixture, endpoint, status, code):
    from media_servers.http import MediaServerError
    server = silo_fixture(**({"ws_status": status} if endpoint == "ws" else {"post_reply": (status, {})}))
    with pytest.raises(MediaServerError) as error:
        refresh(server)
    assert str(error.value) == code
    assert all("/capture" not in record["path"] for record in server.records)


def test_socket_is_read_during_post_to_answer_pings(silo_fixture):
    async def ping_during_post(server):
        await server.ws.ping(b"synthetic-ping")
        await server.release.wait()
        await server.send(scan_event(run_row()))
        await server.complete()

    server = silo_fixture(on_post=ping_during_post)
    assert refresh(server) == {"status": "confirmed"}
    assert "pong" in server.sequence


@pytest.mark.parametrize("phase", ["barrier", "post", "terminal"])
def test_total_deadline_covers_active_runs_requests_and_terminal_wait(silo_fixture, phase):
    from media_servers.http import MediaServerError

    async def held_post(server):
        if phase == "post":
            await server.release.wait()
        else:
            await server.send(scan_event(run_row()))

    server = silo_fixture(snapshot=[run_row("old")] if phase == "barrier" else [], on_post=held_post)
    started = time.monotonic()
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)
    assert time.monotonic() - started < 0.4
    assert server.ws_closed.wait(1)
    assert len(server.posts) == (0 if phase == "barrier" else 1)


@pytest.mark.parametrize("library", [7, True, "", "0", "-7", "7.0", " 7", "٧", "9" * 100])
def test_invalid_native_library_id_sends_nothing(silo_fixture, library):
    from media_servers.http import MediaServerError
    server = silo_fixture()
    with pytest.raises(MediaServerError, match="^library_invalid$"):
        refresh(server, library=library)
    assert server.records == []


@pytest.mark.parametrize("path", ["relative.mkv", "/media/../Movie.mkv", "", "/media/\nMovie.mkv"])
def test_invalid_video_path_sends_nothing(silo_fixture, path):
    from media_servers.http import MediaServerError
    server = silo_fixture()
    with pytest.raises(MediaServerError, match="^path_invalid$"):
        refresh(server, path=path)
    assert server.records == []


@pytest.mark.parametrize("field", ["status", "mode", "event"])
def test_non_string_event_discriminators_fail_closed_as_invalid_response(silo_fixture, field):
    from media_servers.http import MediaServerError

    async def malformed(server):
        payload = scan_event(run_row())
        if field == "event":
            payload["event"] = []
        else:
            payload["data"][field] = []
        await server.send(payload)

    server = silo_fixture(on_post=malformed)
    with pytest.raises(MediaServerError, match="^invalid_response$"):
        refresh(server, timeout=0.08)


def test_same_run_id_with_changed_target_cannot_confirm(silo_fixture):
    from media_servers.http import MediaServerError

    async def substituted(server):
        await server.send(scan_event(run_row()))
        await server.complete(path="/media/Other.mkv")

    server = silo_fixture(on_post=substituted)
    with pytest.raises(MediaServerError, match="^invalid_response$"):
        refresh(server)


def test_repeated_reuse_is_bounded_to_three_posts(silo_fixture):
    from media_servers.http import MediaServerError

    async def always_reuse(server):
        run_id = f"reused-{len(server.posts)}"
        await server.send(scan_event(run_row(run_id, status="running")))
        await server.complete(run_id)

    server = silo_fixture(on_post=always_reuse)
    with pytest.raises(MediaServerError, match="^observation_incomplete$"):
        refresh(server)
    assert len(server.posts) == 3


@pytest.mark.parametrize("missing", ["subscribed", "snapshot"])
def test_missing_subscription_or_snapshot_after_hello_never_posts(silo_fixture, missing):
    from media_servers.http import MediaServerError
    frames = [{"type": "hello", "schema_version": 1, "connection_id": "synthetic-connection",
               "available_channels": ["scans"], "required_action": "none"}]
    if missing == "snapshot":
        frames.append({"type": "subscribed", "channels": ["scans"]})
    server = silo_fixture(handshake=frames)
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)
    assert server.posts == []
    assert server.ws_closed.wait(1)


def test_upgrade_http_request_is_inside_total_deadline(silo_fixture):
    from media_servers.http import MediaServerError

    async def held_upgrade(server):
        await server.release.wait()

    server = silo_fixture(before_ws=held_upgrade)
    started = time.monotonic()
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)
    assert time.monotonic() - started < 0.4
    assert server.posts == []


def test_http_response_body_wait_is_inside_total_deadline(silo_fixture):
    from media_servers.http import MediaServerError

    async def held_body(request, server):
        response = web.StreamResponse(status=202)
        await response.prepare(request)
        await response.write(b'{"status":')
        await server.release.wait()
        return response

    server = silo_fixture(post_reply=held_body)
    started = time.monotonic()
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)
    assert time.monotonic() - started < 0.4
    assert server.ws_closed.wait(1)


def test_http_stream_without_content_length_is_size_bounded(silo_fixture):
    from media_servers.http import MediaServerError

    async def oversized_body(request, _server):
        response = web.StreamResponse(status=202)
        await response.prepare(request)
        try:
            for _chunk in range(257):
                await response.write(b" " * 8192)
        except ClientConnectionError:
            pass
        return response

    server = silo_fixture(post_reply=oversized_body)
    with pytest.raises(MediaServerError, match="^response_too_large$"):
        refresh(server)


@pytest.fixture
def native_tls_context(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "certificate.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def test_tls_verification_applies_to_ws_and_post(silo_fixture, native_tls_context):
    from media_servers.http import MediaServerError
    server = silo_fixture(tls_context=native_tls_context)
    with pytest.raises(MediaServerError, match="^tls_error$"):
        refresh(server)
    assert server.records == []
    assert refresh(server, verify_ssl=False) == {"status": "confirmed"}
    assert server.posts == [{"library_id": 7, "path": "/media/Movie.mkv"}]


@pytest.mark.parametrize("timeout", [True, None, "90", 0, -1, float("nan"), float("inf"), 10**500],
                         ids=["boolean", "null", "string", "zero", "negative", "nan", "infinite", "huge"])
def test_invalid_deadline_values_send_nothing(silo_fixture, timeout):
    from media_servers.http import MediaServerError
    server = silo_fixture()
    with pytest.raises(MediaServerError, match="^invalid_timeout$"):
        refresh(server, timeout=timeout)
    assert server.records == []


def test_observer_cannot_retain_unbounded_target_text(silo_fixture):
    from media_servers.http import MediaServerError

    async def many_large_targets(server):
        for index in range(25):
            await server.send(scan_event(run_row(f"other-{index}", library=8, path="/other/" + "x" * 100000)))
        await server.send(scan_event(run_row()))
        await server.complete()

    server = silo_fixture(on_post=many_large_targets)
    with pytest.raises(MediaServerError, match="^observation_incomplete$"):
        refresh(server)


@pytest.fixture
def native_dns(monkeypatch, tmp_path):
    import dns.asyncresolver
    import dns.flags
    import dns.message
    import dns.rrset

    transports = []

    def configure(server, *, stalled=False):
        queries, peers = [], []

        class Responder(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, packet, peer):
                query = dns.message.from_wire(packet)
                name, record_type = query.question[0].name.to_text(), query.question[0].rdtype
                queries.append((name, record_type))
                peers.append(peer)
                if stalled:
                    return
                response = dns.message.make_response(query)
                response.flags |= dns.flags.AA
                if record_type == 1 and name == "native-silo.fixture.test.":
                    response.answer.append(dns.rrset.from_text(name, 30, "IN", "A", "127.0.0.1"))
                self.transport.sendto(response.to_wire(), peer)

        async def start():
            transport, _protocol = await server.loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
            return transport

        transport = asyncio.run_coroutine_threadsafe(start(), server.loop).result(1)
        transports.append((server, transport))
        resolver_file = tmp_path / "resolv.conf"
        resolver_file.write_text("nameserver 127.0.0.1\nsearch fixture.test\noptions ndots:1\n")
        resolver_type = dns.asyncresolver.Resolver

        def configured_resolver():
            resolver = resolver_type(filename=str(resolver_file))
            resolver.nameserver_ports["127.0.0.1"] = transport.get_extra_info("sockname")[1]
            return resolver

        monkeypatch.setattr(dns.asyncresolver, "Resolver", configured_resolver)

        def forbid_threaded_lookup(*_args, **_kwargs):
            raise AssertionError("blocking resolver must not run")

        monkeypatch.setattr(socket, "getaddrinfo", forbid_threaded_lookup)
        return queries, peers

    yield configure
    for server, transport in transports:
        if server.thread.is_alive():
            server.loop.call_soon_threadsafe(transport.close)


def test_native_hostname_uses_configured_async_dns_and_search_domains(silo_fixture, native_dns):
    server = silo_fixture()
    queries, _peers = native_dns(server)
    server.url = server.url.replace("127.0.0.1", "native-silo")
    assert refresh(server) == {"status": "confirmed"}
    assert ("native-silo.fixture.test.", 1) in queries
    assert server.posts == [{"library_id": 7, "path": "/media/Movie.mkv"}]


def test_stalled_async_dns_obeys_deadline_and_closes_its_socket(silo_fixture, native_dns):
    from media_servers.http import MediaServerError
    server = silo_fixture()
    queries, peers = native_dns(server, stalled=True)
    server.url = server.url.replace("127.0.0.1", "native-silo")
    started = time.monotonic()
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)
    assert time.monotonic() - started < 0.4
    assert queries
    assert server.records == []
    for peer in set(peers):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(peer)


def test_hosts_file_name_does_not_require_dns_or_an_executor(silo_fixture, monkeypatch):
    server = silo_fixture()

    def forbid_threaded_lookup(*_args, **_kwargs):
        raise AssertionError("blocking resolver must not run")

    monkeypatch.setattr(socket, "getaddrinfo", forbid_threaded_lookup)
    server.url = server.url.replace("127.0.0.1", "localhost")
    assert refresh(server) == {"status": "confirmed"}


def test_ipv6_literal_connects_without_dns_or_an_executor(silo_fixture, monkeypatch):
    if not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable on this host")
    server = silo_fixture(listen_host="::1")

    def forbid_threaded_lookup(*_args, **_kwargs):
        raise AssertionError("blocking resolver must not run")

    monkeypatch.setattr(socket, "getaddrinfo", forbid_threaded_lookup)
    assert refresh(server) == {"status": "confirmed"}
    assert server.posts == [{"library_id": 7, "path": "/media/Movie.mkv"}]


def test_barrier_time_is_not_reset_when_the_http_post_starts(silo_fixture):
    from media_servers.http import MediaServerError

    async def release_barrier(server):
        gate = asyncio.Event()
        server.loop.call_later(0.2, gate.set)
        await gate.wait()
        await server.complete("old")

    async def hold_post(server):
        await server.release.wait()

    server = silo_fixture(snapshot=[run_row("old")], after_snapshot=release_barrier, on_post=hold_post)
    started = time.monotonic()
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.3)
    assert time.monotonic() - started < 0.45
    assert server.posts == [{"library_id": 7, "path": "/media/Movie.mkv"}]
    assert server.ws_closed.wait(1)


def buffered_handshake(*frames, timestamp="2026-09-08T00:00:01Z"):
    return [{"type": "hello", "schema_version": 1, "connection_id": "synthetic-connection",
             "available_channels": ["scans"], "required_action": "none"},
            {"type": "subscribed", "channels": ["scans"]},
            {"type": "snapshot", "channel": "scans", "timestamp": timestamp, "data": []}, *frames]


@pytest.mark.parametrize("fault", ["older_acceptance", "equal_acceptance", "missing_acceptance_time",
                                  "old_start", "equal_start", "missing_start", "missing_completion",
                                  "missing_terminal_time"])
def test_snapshot_buffered_events_require_a_trailing_fresh_completion(silo_fixture, fault):
    accepted_time = "2026-09-08T00:00:02Z"
    completed = run_row("buffered", status="completed")
    completed.update(started_at="2026-09-08T00:00:03Z", completed_at="2026-09-08T00:00:04Z")
    terminal_time = "2026-09-08T00:00:05Z"
    if fault == "older_acceptance":
        accepted_time = "2026-09-08T00:00:00Z"
    elif fault == "equal_acceptance":
        accepted_time = "2026-09-08T00:00:01Z"
    elif fault == "missing_acceptance_time":
        accepted_time = None
    elif fault == "old_start":
        completed["started_at"] = "2026-09-08T00:00:00Z"
    elif fault == "equal_start":
        completed["started_at"] = "2026-09-08T00:00:01Z"
    elif fault == "missing_start":
        completed.pop("started_at")
    elif fault == "missing_completion":
        completed.pop("completed_at")
    else:
        terminal_time = None
    frames = buffered_handshake(scan_event(run_row("buffered"), timestamp=accepted_time),
                                scan_event(completed, timestamp=terminal_time))

    async def accept_then_fresh(server):
        if len(server.posts) == 2:
            await server.send(scan_event(run_row("trailing")))
            await server.complete("trailing")

    server = silo_fixture(handshake=frames, on_post=accept_then_fresh)
    assert refresh(server) == {"status": "confirmed"}
    assert len(server.posts) == 2
    assert "completed:trailing" in server.sequence
    assert server.ws_closed.wait(1)


def test_old_buffered_completion_never_confirms_an_unobserved_new_run(silo_fixture):
    from media_servers.http import MediaServerError
    old = run_row("old", status="completed")
    old.update(started_at="2026-09-08T00:00:00Z", completed_at="2026-09-08T00:00:00.2Z")
    frames = buffered_handshake(scan_event(run_row("old"), timestamp="2026-09-08T00:00:00Z"),
                                scan_event(old, timestamp="2026-09-08T00:00:00.3Z"))
    server = silo_fixture(handshake=frames, post_reply=(202, {"status": "accepted", "mode": "file", "library_id": 7}))
    with pytest.raises(MediaServerError, match="^timeout$"):
        refresh(server, timeout=0.08)
    assert len(server.posts) == 2
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize("timestamp", [None, "", "not a timestamp", "2026-09-08", "2026-09-08T00:00:00"])
def test_missing_or_invalid_snapshot_time_never_submits(silo_fixture, timestamp):
    from media_servers.http import MediaServerError
    server = silo_fixture(handshake=buffered_handshake(timestamp=timestamp))
    with pytest.raises(MediaServerError, match="^observation_incomplete$"):
        refresh(server)
    assert server.posts == []
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize("value", ["x" * 131072, ["x" * 131072], {"detail": "x" * 131072}, True, 2**256],
                         ids=["string", "list", "object", "boolean", "oversized-integer"])
@pytest.mark.parametrize("counter", ["skipped", "errors", "missing", "missing_skipped_protected"])
def test_unrelated_malformed_counters_fail_before_retention(silo_fixture, value, counter):
    from media_servers.http import MediaServerError

    async def unrelated(server):
        await server.send(scan_event(run_row("other", library=8, status="completed",
                                            result={**CLEAN_RESULT, counter: value})))
        await server.send(scan_event(run_row()))
        await server.complete()

    server = silo_fixture(on_post=unrelated)
    with pytest.raises(MediaServerError, match="^invalid_response$"):
        refresh(server)
    assert server.ws_closed.wait(1)


def test_equivalent_replacement_paths_are_charged_to_the_retention_budget(silo_fixture):
    from media_servers.http import MediaServerError

    async def replacements(server):
        for index in range(10):
            await server.send(scan_event(run_row(f"other-{index}", library=8, path="/other/file.mkv")))
            await server.send(scan_event(run_row(f"other-{index}", library=8, status="running",
                                                path="/other/" + "./" * 32768 + "file.mkv")))
        await server.send(scan_event(run_row()))
        await server.complete()

    server = silo_fixture(on_post=replacements)
    with pytest.raises(MediaServerError, match="^observation_incomplete$"):
        refresh(server, timeout=2)
    assert server.ws_closed.wait(1)


def test_shorter_replacements_release_the_previous_path_charge(silo_fixture):
    async def replace_repeatedly(server):
        for _index in range(20):
            await server.send(scan_event(run_row("other", library=8, status="running",
                                                path="/other/" + "./" * 16384 + "file.mkv")))
            await server.send(scan_event(run_row("other", library=8, status="running", path="/other/file.mkv")))
        await server.send(scan_event(run_row()))
        await server.complete()

    server = silo_fixture(on_post=replace_repeatedly)
    assert refresh(server, timeout=2) == {"status": "confirmed"}
    assert server.ws_closed.wait(1)


@pytest.mark.parametrize("path", [1, True, [], {}, None, "relative/path"])
def test_malformed_library_snapshot_is_categorized_and_closes_resources(silo_fixture, monkeypatch, path):
    import aiohttp
    from media_servers.http import MediaServerError
    sessions = []
    original = aiohttp.ClientSession.__init__

    def record_session(session, *args, **kwargs):
        original(session, *args, **kwargs)
        sessions.append(session)

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", record_session)
    row = run_row("library", mode="library")
    row["path"] = path
    server = silo_fixture(snapshot=[row])
    with pytest.raises(MediaServerError, match="^invalid_response$"):
        refresh(server, timeout=0.08)
    assert sessions and all(session.closed for session in sessions)
    assert server.ws_closed.wait(1)
    assert server.posts == []


@pytest.mark.parametrize("fault", ["counter", "library_id"])
def test_unrelated_snapshot_scalars_are_validated_before_storage(silo_fixture, fault):
    from media_servers.http import MediaServerError
    row = run_row("other", library=8, status="running")
    if fault == "counter":
        row["result"] = {**CLEAN_RESULT, "missing": ["x" * 131072]}
    else:
        row["library_id"] = 2**256
    server = silo_fixture(snapshot=[row])
    with pytest.raises(MediaServerError, match="^invalid_response$"):
        refresh(server)
    assert server.posts == []
    assert server.ws_closed.wait(1)


def test_completed_barrier_advances_the_timestamp_boundary_for_the_next_post(silo_fixture):
    async def stale_then_fresh(server):
        if len(server.posts) == 1:
            row = run_row("reused", status="completed")
            row.update(started_at="2026-09-08T00:00:01Z", completed_at="2026-09-08T00:00:02Z")
            await server.send(scan_event(row, timestamp="2026-09-08T00:00:03Z"))
        elif len(server.posts) == 2:
            row = run_row("delayed", status="completed")
            row.update(started_at="2026-09-08T00:00:01.5Z", completed_at="2026-09-08T00:00:02.5Z")
            await server.send(scan_event(run_row("delayed"), timestamp="2026-09-08T00:00:01.1Z"))
            await server.send(scan_event(row, timestamp="2026-09-08T00:00:02.9Z"))
        else:
            await server.send(scan_event(run_row("trailing")))
            await server.complete("trailing")

    server = silo_fixture(on_post=stale_then_fresh,
                          handshake=buffered_handshake(timestamp="2026-09-08T00:00:00Z"))
    assert refresh(server) == {"status": "confirmed"}
    assert len(server.posts) == 3
    assert "completed:trailing" in server.sequence


def test_native_nanosecond_times_preserve_a_strict_freshness_boundary(silo_fixture):
    async def fresh(server):
        await server.send(scan_event(run_row(), timestamp="2026-09-08T00:00:00.123456781Z"))
        row = run_row(status="completed")
        row.update(started_at="2026-09-08T00:00:00.123456782Z", completed_at="2026-09-08T00:00:00.123456783Z")
        await server.send(scan_event(row, timestamp="2026-09-08T00:00:00.123456784Z"))

    server = silo_fixture(handshake=buffered_handshake(timestamp="2026-09-08T00:00:00.123456780Z"), on_post=fresh)
    assert refresh(server) == {"status": "confirmed"}
    assert len(server.posts) == 1


def test_equal_times_in_different_offsets_are_ambiguous(silo_fixture):
    row = run_row("old", status="completed")
    row.update(started_at="2026-09-08T00:00:01Z", completed_at="2026-09-08T00:00:02Z")
    frames = buffered_handshake(scan_event(run_row("old"), timestamp="2026-09-08T00:00:01Z"),
                                scan_event(row, timestamp="2026-09-08T00:00:03Z"),
                                timestamp="2026-09-08T01:00:01+01:00")

    async def trailing(server):
        if len(server.posts) == 2:
            await server.send(scan_event(run_row("trailing")))
            await server.complete("trailing")

    server = silo_fixture(handshake=frames, on_post=trailing)
    assert refresh(server) == {"status": "confirmed"}
    assert len(server.posts) == 2
