import io
import json
import os
import struct
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from provider_hub.runtime import frames, worker_channel
from provider_hub.runtime import session as session_module
from provider_hub.runtime.errors import (
    HandshakeError,
    HeartbeatTimeout,
    HostCallError,
    ProtocolViolation,
    RequestCancelled,
    RequestFailed,
    RequestTimeout,
    ResultTooLarge,
    SessionBusy,
    SessionClosed,
    SessionError,
    WorkerExited,
)
from provider_hub.runtime.session import SessionLimits, WorkerSession
from provider_hub.runtime.testing.fake_worker import fake_worker_command, fake_worker_env

REPO = Path(__file__).parents[2]


def fast_limits(**overrides):
    values = dict(
        handshake_timeout=10.0,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.8,
        cancel_grace=0.4,
        kill_grace=3.0,
        tick=0.02,
    )
    values.update(overrides)
    return SessionLimits(**values)


@pytest.fixture(autouse=True)
def no_protocol_threads_left_behind():
    """Every thread a test starts must be gone once the test and its fixtures end."""
    before = set(threading.enumerate())
    yield
    leaked = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        leaked = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not leaked:
            break
        time.sleep(0.05)
    assert not leaked, "threads still alive after the test: %s" % [t.name for t in leaked]


@pytest.fixture
def make_session():
    sessions = []

    def make(config=None, **kwargs):
        kwargs.setdefault("kind", "translator")
        kwargs.setdefault("limits", fast_limits())
        session = WorkerSession(fake_worker_command(), env=fake_worker_env(**(config or {})), **kwargs)
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        session.close(grace=1.0)


@pytest.fixture
def session(make_session):
    started = make_session()
    started.start()
    return started


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; the session reaps its own child, a
    # forked grandchild is reparented and reaped by init.
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------- framing


def roundtrip(*writes):
    stream = io.BytesIO()
    writer = frames.FrameWriter(stream)
    for write in writes:
        write(writer)
    stream.seek(0)
    return frames.FrameReader(stream)


def test_control_and_data_frames_round_trip():
    reader = roundtrip(
        lambda w: w.write_control({"t": "hb", "seq": 1}),
        lambda w: w.write_data(7, b"abcdef", chunk_size=4),
    )
    ftype, body = reader.read_frame()
    assert ftype == frames.FRAME_CONTROL
    assert frames.decode_control(body, "worker") == {"t": "hb", "seq": 1}
    chunks = []
    for _ in range(2):
        ftype, body = reader.read_frame()
        assert ftype == frames.FRAME_DATA
        chunks.append(frames.split_data(body))
    assert chunks == [(7, b"abcd"), (7, b"ef")]
    assert reader.read_frame() is None


def test_oversized_length_prefix_is_refused_before_the_body_is_read():
    class Stream:
        def __init__(self):
            self.data = frames.HEADER.pack(frames.FRAME_CONTROL, 0x7FFFFFFF)
            self.asked = []

        def read(self, size):
            self.asked.append(size)
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

    stream = Stream()
    with pytest.raises(frames.FrameTooLarge):
        frames.FrameReader(stream).read_frame()
    # Header only: never a read anywhere near the claimed length.
    assert max(stream.asked) <= frames.HEADER.size


def test_caps_are_per_type_and_handshake_cap_is_small():
    big = b"x" * (frames.HANDSHAKE_CONTROL_BYTES + 1)
    header = frames.HEADER.pack(frames.FRAME_CONTROL, len(big))
    reader = frames.FrameReader(io.BytesIO(header + big), {frames.FRAME_CONTROL: frames.HANDSHAKE_CONTROL_BYTES})
    with pytest.raises(frames.FrameTooLarge):
        reader.read_frame()
    data = frames.HEADER.pack(frames.FRAME_DATA, frames.MAX_DATA_BYTES + 1)
    with pytest.raises(frames.FrameTooLarge):
        frames.FrameReader(io.BytesIO(data)).read_frame()


def test_unknown_type_truncation_and_legacy_first_byte():
    with pytest.raises(frames.MalformedFrame) as unknown:
        frames.FrameReader(io.BytesIO(b"\x09\x00\x00\x00\x00")).read_frame()
    assert unknown.value.code == frames.Codes.PROTOCOL_ERROR
    with pytest.raises(frames.MalformedFrame) as legacy:
        frames.FrameReader(io.BytesIO(b'{"abi":"bazarr.provider-worker.v1"}\n')).read_frame()
    assert legacy.value.code == frames.Codes.LEGACY_PROTOCOL
    with pytest.raises(frames.MalformedFrame):
        frames.FrameReader(io.BytesIO(frames.HEADER.pack(frames.FRAME_CONTROL, 10) + b"abc")).read_frame()


def test_writer_refuses_a_message_over_the_cap():
    writer = frames.FrameWriter(io.BytesIO(), {frames.FRAME_CONTROL: 32, frames.FRAME_DATA: 64})
    with pytest.raises(frames.FrameTooLarge):
        writer.write_control({"t": "hb", "seq": 1, "pad": "x" * 100})


@pytest.mark.parametrize(
    "message, sender",
    [
        ({"t": "req", "id": 1, "op": "x", "params": {}}, "worker"),  # wrong direction
        ({"t": "res", "id": True, "ok": True}, "worker"),  # bool is not an id
        ({"t": "res", "id": 1, "ok": False}, "worker"),  # failure without error
        ({"t": "hb"}, "worker"),
        ({"t": "nope"}, "worker"),
        ({"t": "call", "cid": 1, "name": "reserve", "params": []}, "worker"),
        ({"t": "ack", "cid": 1, "ok": False}, "host"),
        ([], "host"),
    ],
)
def test_validate_message_rejects_schema_breaks(message, sender):
    with pytest.raises(frames.MalformedFrame):
        frames.validate_message(message, sender)


def test_validate_message_allows_unknown_extra_keys():
    message = {"t": "hb", "seq": 3, "added_in_a_later_minor": 1}
    assert frames.validate_message(message, "worker") is message


def test_decode_control_rejects_bad_json():
    with pytest.raises(frames.MalformedFrame):
        frames.decode_control(b"{nope", "worker")


def test_protocol_negotiation_picks_highest_shared_major_and_lower_minor():
    assert frames.negotiate_protocol([{"major": 2, "minor": 5}]) == (2, 0)
    assert frames.negotiate_protocol([{"major": 3, "minor": 0}, {"major": 2, "minor": 0}]) == (2, 0)
    assert frames.negotiate_protocol([{"major": 3, "minor": 0}]) is None
    assert frames.negotiate_protocol([{"major": 3, "minor": 1}, {"major": 2, "minor": 4}], {2: 9, 3: 0}) == (3, 0)
    assert frames.negotiate_protocol([{"major": "2"}, "junk", {"major": True}]) is None


def test_kind_negotiation_serves_an_older_major():
    offered = [{"kind": "translator", "major": 1, "minor": 3}, {"kind": "ocr", "major": 2, "minor": 0}]
    assert frames.negotiate_kind(offered, "translator", [(2, 0), (1, 1)]) == {
        "kind": "translator", "major": 1, "minor": 1}
    assert frames.negotiate_kind(offered, "translator", [(2, 0)]) is None
    both = offered + [{"kind": "translator", "major": 2, "minor": 0}]
    assert frames.negotiate_kind(both, "translator", [(2, 0), (1, 1)])["major"] == 2


def test_wire_version_defaults_to_v1():
    assert frames.wire_version(None) == 1
    assert frames.wire_version(frames.V1_ABI) == 1
    assert frames.wire_version(frames.V2_ABI) == 2
    with pytest.raises(ValueError):
        frames.wire_version("bazarr.worker-protocol.v9")


# ------------------------------------------------------------------- handshake


def test_handshake_settles_protocol_kind_and_limits(make_session):
    session = make_session()
    welcome = session.start()
    assert welcome.protocol == (2, 0)
    assert welcome.kind == {"kind": "translator", "major": 1, "minor": 0}
    assert welcome.worker["name"] == "fake-worker"
    assert welcome.pid == session.process.pid
    assert session.alive


def test_old_kind_major_is_served(make_session):
    session = make_session(
        {"kinds": [{"kind": "translator", "major": 1, "minor": 2}]},
        kind_versions=((2, 0), (1, 5)),
    )
    assert session.start().kind == {"kind": "translator", "major": 1, "minor": 2}


def test_newer_worker_minor_negotiates_down(make_session):
    session = make_session({"protocols": [{"major": 2, "minor": 7}]})
    assert session.start().protocol == (2, 0)


def test_protocol_major_mismatch_is_refused_and_the_worker_ends(make_session):
    session = make_session({"protocols": [{"major": 3, "minor": 0}]})
    with pytest.raises(HandshakeError) as caught:
        session.start()
    assert caught.value.code == frames.Codes.VERSION_MISMATCH
    assert wait_until(lambda: session.process.poll() is not None)


def test_kind_mismatch_is_refused(make_session):
    session = make_session({"kinds": [{"kind": "translator", "major": 3, "minor": 0}]})
    with pytest.raises(HandshakeError) as caught:
        session.start()
    assert caught.value.code == frames.Codes.VERSION_MISMATCH


def test_worker_that_never_speaks_fails_the_handshake_quickly(make_session):
    session = make_session({"no_hello": True}, limits=fast_limits(handshake_timeout=0.4))
    started = time.monotonic()
    with pytest.raises(HandshakeError):
        session.start()
    assert time.monotonic() - started < 5
    assert wait_until(lambda: session.process.poll() is not None)


# ------------------------------------------------------------ v1 compatibility


@pytest.fixture
def v1_worker(tmp_path):
    from provider_hub.worker import ProviderWorkerClient, worker_command

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "provider.py").write_text(
        textwrap.dedent(
            """
            class Provider:
                def search(self, video, languages, config):
                    return [{"id": "c1", "video": video}]
            """
        ),
        encoding="utf-8",
    )
    runner = REPO / "bazarr/provider_hub/worker_runner.py"
    env = {
        "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle),
        "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps({"entry_module": "provider", "entry_class": "Provider"}),
    }
    client = ProviderWorkerClient(worker_command(sys.executable, runner), cwd=bundle, env=env)
    yield client, runner, env
    client.stop()


def test_v1_provider_worker_still_speaks_the_line_protocol(v1_worker):
    client, _runner, _env = v1_worker
    assert client.request("health", {}).payload == {"initialized": True}
    result = client.request("search", {"video": {"name": "a"}, "languages": [], "config": {}})
    assert result.payload["candidates"][0]["id"] == "c1"


def test_v1_runner_under_a_v2_session_fails_the_handshake_cleanly(v1_worker):
    _client, runner, env = v1_worker
    session = WorkerSession(
        [sys.executable, "-I", "-B", str(runner)], env=env, limits=fast_limits(handshake_timeout=0.5))
    try:
        with pytest.raises(HandshakeError):
            session.start()
    finally:
        session.close(grace=1.0)


def test_a_worker_replying_in_v1_lines_is_named_as_legacy(tmp_path):
    script = tmp_path / "legacy.py"
    script.write_text(
        'import sys, time\nsys.stdout.write(\'{"abi":"bazarr.provider-worker.v1"}\\n\')\n'
        "sys.stdout.flush()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    session = WorkerSession([sys.executable, "-B", str(script)], limits=fast_limits())
    try:
        with pytest.raises(HandshakeError) as caught:
            session.start()
        assert caught.value.code == frames.Codes.LEGACY_PROTOCOL
    finally:
        session.close(grace=1.0)


def test_v1_module_surface_is_unchanged():
    from provider_hub import WORKER_ABI_VERSION

    assert WORKER_ABI_VERSION == frames.V1_ABI == "bazarr.provider-worker.v1"
    runner = (REPO / "bazarr/provider_hub/worker_runner.py").read_text(encoding="utf-8")
    assert "runtime" not in runner


# ------------------------------------------------------ requests and routing


def test_request_returns_the_result(session):
    outcome = session.request("echo", {"a": 1}, timeout=10)
    assert outcome.result["echo"] == {"a": 1}
    assert outcome.data == b""
    assert session.in_flight == 0


def test_replies_route_to_their_own_request_id(session):
    slow = session.submit("sleep", {"seconds": 0.6})
    mid = session.submit("echo", {"which": "mid"})
    fast = session.submit("echo", {"which": "fast"})
    assert len({slow.id, mid.id, fast.id}) == 3
    assert fast.wait(5).result["echo"] == {"which": "fast"}
    assert mid.wait(5).result["echo"] == {"which": "mid"}
    assert not slow.done()
    assert slow.wait(5).result == {"slept": 0.6}


def test_unknown_operation_is_a_coded_failure(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("nonsense", timeout=5)
    assert caught.value.code == "unknown_op"
    assert session.alive


def test_plugin_exception_carries_class_and_traceback(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("raise", {"message": "kaboom"}, timeout=5)
    assert caught.value.code == frames.Codes.PLUGIN_ERROR
    assert caught.value.class_name == "ValueError"
    assert "kaboom" in str(caught.value)
    assert "ValueError" in caught.value.traceback


def test_progress_messages_stream_to_the_callback(session):
    seen = []
    handle = session.submit("progress", {"steps": 4}, on_progress=seen.append)
    handle.wait(5)
    assert [m["message"] for m in seen] == ["step 1", "step 2", "step 3", "step 4"]
    assert seen[-1]["fraction"] == 1.0


def test_chunked_result_is_reassembled_and_checked(session):
    outcome = session.request("chunks", {"size": 700_000}, timeout=10)
    assert len(outcome.data) == 700_000
    assert outcome.data[:16] == b"0123456789abcdef"
    assert outcome.result == {"size": 700_000}


def test_honest_worker_refuses_a_result_over_the_request_bound(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("chunks", {"size": 500_000}, max_result_bytes=100_000, timeout=10)
    assert caught.value.code == frames.Codes.RESULT_TOO_LARGE
    assert session.request("echo", timeout=5).result["echo"] == {}


def test_result_bound_by_the_request_is_enforced_and_the_session_survives(session):
    with pytest.raises(ResultTooLarge):
        session.request("chunks", {"size": 500_000, "overshoot": True}, max_result_bytes=100_000, timeout=10)
    assert session.request("echo", {"after": True}, timeout=5).result["echo"] == {"after": True}


def test_in_flight_limit_refuses_without_sending(make_session):
    session = make_session({"max_in_flight": 2})
    session.start()
    first = session.submit("sleep", {"seconds": 0.5})
    second = session.submit("sleep", {"seconds": 0.5})
    with pytest.raises(SessionBusy):
        session.submit("echo")
    first.wait(5)
    second.wait(5)
    assert session.request("echo", timeout=5).result["echo"] == {}


def test_secrets_reach_the_worker_and_are_scrubbed_from_stderr(make_session):
    lines = []
    session = make_session(on_stderr=lines.append)
    session.start()
    secret = "s3cr3t-token-value"
    outcome = session.request("leak", secrets={"token": secret}, timeout=5)
    assert outcome.result == {"leaked": 1}
    assert wait_until(lambda: any("leaked" in line for line in lines))
    assert not any(secret in line for line in lines)


def test_stdout_noise_cannot_corrupt_frames(make_session):
    lines = []
    session = make_session(on_stderr=lines.append)
    session.start()
    assert session.request("spam", {"lines": 300}, timeout=10).result == {"spammed": True}
    assert session.request("echo", timeout=5).result["echo"] == {}
    assert session.alive
    # print() and a raw write to fd 1 land on stderr, rate limited.
    assert wait_until(lambda: any("stdout noise" in line for line in lines))


# ------------------------------------------------------------------ host calls


def test_worker_calls_are_answered_in_order_before_the_worker_continues(make_session):
    log = []

    def reserve(call):
        log.append(("reserve", call.params, call.request_id))
        return {"reservation": "r1"}

    def checkpoint(call):
        log.append(("checkpoint", call.params["done"]))
        return {}

    def usage(call):
        log.append(("usage", call.params["units"]))
        return None

    session = make_session(host_calls={"reserve": reserve, "checkpoint": checkpoint, "usage": usage})
    session.start()
    handle = session.submit("translate", {"lines": ["a", "b", "c"], "target": "hu", "batch": 2}, timeout=10)
    outcome = handle.wait(10)
    assert outcome.result["lines"] == ["[hu] a", "[hu] b", "[hu] c"]
    assert log == [
        ("reserve", {"units": 3, "target": "hu"}, handle.id),
        ("checkpoint", 2),
        ("checkpoint", 3),
        ("usage", 3),
    ]


def test_host_call_handler_error_becomes_a_coded_failure(make_session):
    def reserve(call):
        raise HostCallError("budget_exhausted", "month cap reached")

    session = make_session(host_calls={"reserve": reserve})
    session.start()
    with pytest.raises(RequestFailed) as caught:
        session.request("translate", {"lines": ["a"]}, timeout=10)
    assert caught.value.code == "budget_exhausted"
    assert session.alive


def test_unknown_host_call_and_crashing_handler_are_acked_as_errors(make_session):
    def boom(call):
        raise RuntimeError("db is down")

    session = make_session(host_calls={"boom": boom})
    session.start()
    with pytest.raises(RequestFailed) as unknown:
        session.request("call", {"name": "missing"}, timeout=5)
    assert unknown.value.code == frames.Codes.UNKNOWN_CALL
    with pytest.raises(RequestFailed) as crashed:
        session.request("call", {"name": "boom"}, timeout=5)
    assert crashed.value.code == frames.Codes.HOST_CALL_FAILED
    assert session.alive


def test_host_call_registered_after_start_is_used(session):
    session.register_host_call("lookup", lambda call: {"echo": call.params})
    outcome = session.request("call", {"name": "lookup", "params": {"k": 1}}, timeout=5)
    assert outcome.result["ack"] == {"echo": {"k": 1}}


def test_failed_translation_reports_retry_hints(make_session):
    session = make_session(host_calls={"reserve": lambda c: {}, "checkpoint": lambda c: {}, "usage": lambda c: {}})
    session.start()
    fail = {"code": "rate_limited", "message": "slow down", "retryable": True, "retry_after": 12}
    with pytest.raises(RequestFailed) as caught:
        session.request("translate", {"lines": ["a", "b"], "fail": fail}, timeout=10)
    assert caught.value.code == "rate_limited"
    assert caught.value.retryable is True
    assert caught.value.retry_after == 12


# ---------------------------------------------------------------------- cancel


def test_mid_request_cancel_stops_the_operation_and_keeps_the_worker(session):
    handle = session.submit("sleep", {"seconds": 30})
    time.sleep(0.2)
    started = time.monotonic()
    handle.cancel("user")
    with pytest.raises(RequestCancelled):
        handle.wait(5)
    assert time.monotonic() - started < 2
    assert session.alive
    assert session.request("echo", timeout=5).result["echo"] == {}


def test_cancel_reaches_a_request_while_another_runs(session):
    keep = session.submit("sleep", {"seconds": 0.8})
    drop = session.submit("sleep", {"seconds": 30})
    drop.cancel()
    with pytest.raises(RequestCancelled):
        drop.wait(5)
    assert keep.wait(5).result == {"slept": 0.8}


def test_deadline_expiry_cancels_and_reports_a_timeout(session):
    started = time.monotonic()
    with pytest.raises(RequestTimeout):
        session.request("sleep", {"seconds": 30}, timeout=0.3)
    assert time.monotonic() - started < 3
    assert session.alive


def test_a_worker_that_ignores_cancel_is_killed_after_the_grace(session):
    handle = session.submit("sleep", {"seconds": 30, "ignore_cancel": True})
    time.sleep(0.2)
    process = session.process
    handle.cancel()
    with pytest.raises(RequestCancelled):
        handle.wait(5)
    assert wait_until(lambda: process.poll() is not None)
    assert not session.alive
    with pytest.raises(SessionClosed):
        session.submit("echo")


def test_a_reply_landing_before_the_kill_spares_the_session(make_session):
    session = make_session()
    session.start()
    neighbour = session.submit("sleep", {"seconds": 1.5})
    stuck = session.submit("sleep", {"seconds": 30, "ignore_cancel": True})
    time.sleep(0.2)
    process = session.process
    real_fail = session._fail
    interleaved = []

    def fail_after_the_reader_finishes(error, kill, only=None, **kwargs):
        # The watchdog already saw the request unfinished; the worker's reply
        # is handled in the gap before the session is ended.
        if only is not None and not interleaved:
            interleaved.append(only.id)
            session._on_result({"t": frames.RES, "id": only.id, "ok": True, "result": {"late": True}})
        return real_fail(error, kill, only=only, **kwargs)

    session._fail = fail_after_the_reader_finishes
    stuck.cancel()
    assert wait_until(lambda: interleaved)
    assert stuck.wait(5).result == {"late": True}
    time.sleep(0.3)
    assert session.alive
    assert process.poll() is None
    assert neighbour.wait(5).result == {"slept": 1.5}


def test_cancelling_a_finished_request_is_a_no_op(session):
    handle = session.submit("echo")
    handle.wait(5)
    handle.cancel()
    assert session.alive


# ------------------------------------------------------------------- heartbeat


def test_heartbeats_arrive_and_carry_memory(session):
    assert wait_until(lambda: session.last_heartbeat is not None)
    assert session.last_heartbeat["seq"] >= 1
    assert session.rss_kb is None or session.rss_kb > 0


def test_a_silent_worker_is_killed_when_the_heartbeat_deadline_passes(make_session):
    session = make_session({"silent": True}, limits=fast_limits(heartbeat_timeout=0.5))
    session.start()
    handle = session.submit("sleep", {"seconds": 30})
    process = session.process
    with pytest.raises(HeartbeatTimeout):
        handle.wait(5)
    assert wait_until(lambda: process.poll() is not None)


def test_a_busy_but_beating_worker_is_not_killed(session):
    assert session.request("sleep", {"seconds": 1.5}, timeout=10).result == {"slept": 1.5}
    assert session.alive


# ---------------------------------------------------- failures and tear-down


def test_oversized_frame_from_the_worker_kills_it(session):
    process = session.process
    with pytest.raises(ProtocolViolation) as caught:
        session.request("oversize_frame", timeout=10)
    assert caught.value.code == frames.Codes.FRAME_TOO_LARGE
    assert wait_until(lambda: process.poll() is not None)


def test_malformed_frame_from_the_worker_kills_it(session):
    process = session.process
    with pytest.raises(ProtocolViolation):
        session.request("bad_frame", timeout=10)
    assert wait_until(lambda: process.poll() is not None)


def test_worker_exit_mid_request_fails_every_request_in_flight(session):
    other = session.submit("sleep", {"seconds": 30})
    with pytest.raises(WorkerExited) as caught:
        session.request("exit", {"code": 7}, timeout=10)
    assert caught.value.exit_code == 7
    with pytest.raises(WorkerExited):
        other.wait(5)


def test_sandbox_blocked_is_reported_as_an_event_and_an_error(make_session):
    events = []
    session = make_session(on_event=lambda name, payload: events.append((name, payload)))
    session.start()
    with pytest.raises(RequestFailed) as caught:
        session.request("blocked", {"target": "198.51.100.4:25"}, timeout=5)
    assert caught.value.code == frames.Codes.SANDBOX_BLOCKED
    assert wait_until(lambda: events)
    name, payload = events[0]
    assert name == "sandbox_blocked"
    assert payload["target"] == "198.51.100.4:25"
    assert payload["errno"] == 13


def test_close_stops_the_worker_cleanly_and_ends_its_process_group(session):
    child = session.request("fork", timeout=5).result["child"]
    assert pid_alive(child)
    process = session.process
    session.close(grace=3.0)
    assert process.poll() == 0
    assert wait_until(lambda: not pid_alive(child))
    session.close()  # idempotent


def test_fatal_failure_kills_forked_children_in_the_same_group(session):
    child = session.request("fork", timeout=5).result["child"]
    with pytest.raises(WorkerExited):
        session.request("exit", timeout=5)
    assert wait_until(lambda: not pid_alive(child))


def test_worker_exits_when_the_host_pipe_closes(session):
    process = session.process
    process.stdin.close()
    assert wait_until(lambda: process.poll() is not None, timeout=8)


def test_close_while_a_request_runs_fails_it(session):
    handle = session.submit("sleep", {"seconds": 30})
    time.sleep(0.2)
    threading.Thread(target=session.close, kwargs={"grace": 2.0}, daemon=True).start()
    with pytest.raises((SessionClosed, RequestCancelled)):
        handle.wait(8)


def test_fake_worker_settings_are_validated():
    with pytest.raises(ValueError):
        fake_worker_env(nonsense=1)
    assert struct.calcsize(">BI") == frames.HEADER.size


# ------------------------------------------------- hostile and stuck workers

RUNTIME_DIR = REPO / "bazarr/provider_hub/runtime"

HOSTILE_PRELUDE = """
import os, signal, sys, time
sys.path.insert(0, %r)
import frames
out = frames.FrameWriter(sys.stdout.buffer)
inp = frames.FrameReader(sys.stdin.buffer)
out.write_control({"t": "hello", "protocols": [{"major": 2, "minor": 0}], "kinds": [], "max_in_flight": 16})
inp.read_frame()
""" % str(RUNTIME_DIR)


def hostile_session(tmp_path, body, **limits):
    script = tmp_path / "hostile.py"
    script.write_text(HOSTILE_PRELUDE + textwrap.dedent(body), encoding="utf-8")
    values = dict(max_in_flight=16, heartbeat_timeout=30.0)
    values.update(limits)
    return WorkerSession([sys.executable, "-B", str(script)], limits=fast_limits(**values))


STOPS_READING = """
while True:
    out.write_control({"t": "hb", "seq": 1})
    time.sleep(0.05)
"""


def test_a_worker_that_stops_reading_is_killed_at_the_write_deadline(tmp_path):
    session = hostile_session(tmp_path, STOPS_READING, write_timeout=0.5)
    try:
        session.start()
        process = session.process
        pad = "y" * 900_000
        handles = [session.submit("x", {"pad": pad}) for _ in range(4)]
        started = time.monotonic()
        for handle in handles:
            with pytest.raises(Exception) as caught:
                handle.wait(8)
            assert getattr(caught.value, "code", None) == frames.Codes.WRITE_TIMEOUT
        assert time.monotonic() - started < 6
        assert wait_until(lambda: process.poll() is not None)
    finally:
        session.close(grace=0.5)


def test_submit_and_cancel_never_wait_on_a_worker_that_stopped_reading(tmp_path):
    session = hostile_session(tmp_path, STOPS_READING, write_timeout=30.0)
    try:
        session.start()
        pad = "y" * 900_000
        started = time.monotonic()
        handles = [session.submit("x", {"pad": pad}) for _ in range(6)]
        for handle in handles:
            handle.cancel()
        assert time.monotonic() - started < 2
    finally:
        session.close(grace=0.3)


def test_close_ends_a_worker_that_will_not_read_its_shutdown(tmp_path):
    session = hostile_session(tmp_path, STOPS_READING, write_timeout=30.0)
    session.start()
    process = session.process
    session.submit("x", {"pad": "y" * 900_000})
    session.submit("x", {"pad": "y" * 900_000})
    started = time.monotonic()
    session.close(grace=0.5)
    assert time.monotonic() - started < 6
    assert process.poll() is not None


def test_close_sends_sigterm_before_sigkill(tmp_path):
    marker = tmp_path / "terminated"
    body = """
    def on_term(signum, frame):
        open(%r, "w").write("term")
    signal.signal(signal.SIGTERM, on_term)
    while True:
        out.write_control({"t": "hb", "seq": 1})
        time.sleep(0.05)
    """ % str(marker)
    session = hostile_session(tmp_path, body)
    session.start()
    process = session.process
    time.sleep(0.3)
    session.close(grace=0.3)
    assert marker.exists()
    assert process.poll() is not None


def test_deeply_nested_json_fails_the_session_instead_of_the_reader_thread(tmp_path):
    body = """
    time.sleep(0.2)
    payload = b"[" * 500000
    sys.stdout.buffer.write(frames.HEADER.pack(frames.FRAME_CONTROL, len(payload)) + payload)
    sys.stdout.buffer.flush()
    time.sleep(30)
    """
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        handle = session.submit("x")
        started = time.monotonic()
        with pytest.raises(ProtocolViolation):
            handle.wait(10)
        # Well inside the heartbeat deadline: the failure came from the reader.
        assert time.monotonic() - started < 5
    finally:
        session.close(grace=0.5)


def test_deeply_nested_json_before_the_handshake_ends_start_quickly(tmp_path):
    script = tmp_path / "early.py"
    script.write_text(
        "import sys, time\n"
        "payload = b'[' * 50000\n"
        "sys.stdout.buffer.write(bytes([1]) + len(payload).to_bytes(4, 'big') + payload)\n"
        "sys.stdout.buffer.flush()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    session = WorkerSession(
        [sys.executable, "-B", str(script)], limits=fast_limits(handshake_timeout=20.0))
    try:
        started = time.monotonic()
        with pytest.raises(HandshakeError):
            session.start()
        assert time.monotonic() - started < 5
    finally:
        session.close(grace=0.5)


def test_flooding_host_calls_kills_the_worker(tmp_path):
    body = """
    for cid in range(1, 200):
        out.write_control({"t": "call", "cid": cid, "name": "slow", "params": {}})
    time.sleep(30)
    """
    release = threading.Event()
    session = hostile_session(tmp_path, body, max_pending_calls=10)
    session.register_host_call("slow", lambda call: release.wait(5) and {})
    try:
        session.start()
        process = session.process
        assert wait_until(lambda: process.poll() is not None, timeout=8)
        assert not session.alive
    finally:
        release.set()
        session.close(grace=0.3)


def test_a_slow_host_call_does_not_stall_another_requests_calls(make_session):
    release = threading.Event()

    def slow(call):
        release.wait(5)
        return {"slow": True}

    session = make_session(host_calls={"slow": slow, "quick": lambda call: {"quick": True}})
    session.start()
    slow_handle = session.submit("call", {"name": "slow"})
    time.sleep(0.2)
    started = time.monotonic()
    assert session.request("call", {"name": "quick"}, timeout=5).result["ack"] == {"quick": True}
    assert time.monotonic() - started < 1.5
    assert not slow_handle.done()
    release.set()
    assert slow_handle.wait(5).result["ack"] == {"slow": True}


def test_calls_of_one_request_are_serialized_even_when_sent_back_to_back(make_session):
    release = threading.Event()
    log = []

    def first(call):
        log.append("first start")
        release.wait(5)
        log.append("first end")
        return {}

    def second(call):
        log.append("second start")
        return {}

    session = make_session(host_calls={"first": first, "second": second})
    session.start()
    handle = session.submit("parallel_calls", {"names": ["first", "second"]}, timeout=10)
    assert wait_until(lambda: "first start" in log)
    time.sleep(0.5)
    # The second call reached the host while the first handler still ran, and waited.
    assert log == ["first start"]
    release.set()
    handle.wait(5)
    assert log == ["first start", "first end", "second start"]


def test_calls_of_one_request_run_in_the_order_sent(make_session):
    order = []

    def checkpoint(call):
        order.append(call.params["done"])
        return {}

    session = make_session(host_calls={"reserve": lambda c: {}, "checkpoint": checkpoint, "usage": lambda c: {}})
    session.start()
    session.request("translate", {"lines": list("abcdef"), "batch": 1}, timeout=10)
    assert order == [1, 2, 3, 4, 5, 6]


def test_call_claiming_another_request_is_refused(session):
    session.register_host_call("lookup", lambda call: {})
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "lookup", "as_request": 999}, timeout=5)
    assert caught.value.code == frames.Codes.INVALID_REQUEST


# ------------------------------------------------------------------ scrubbing

SECRET = "sk-test-0123456789abcdef"


def test_host_scrubs_secrets_from_error_text_and_traceback(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("raw_error", secrets={"key": SECRET}, timeout=5)
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.traceback
    assert "***" in str(caught.value)


def test_host_scrubs_secrets_from_progress(session):
    seen = []
    session.request("progress_secret", secrets={"key": SECRET}, on_progress=seen.append, timeout=5)
    assert seen
    assert SECRET not in json.dumps(seen)


def test_worker_scrubs_secrets_before_cutting_the_traceback(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("leak_error", {"tail": 7990}, secrets={"key": SECRET}, timeout=5)
    traceback_text = caught.value.traceback
    assert len(traceback_text) <= 8000
    # A cut-then-scrub order would leave the end of the secret behind.
    assert SECRET[-9:] not in traceback_text
    assert SECRET[-9:] not in str(caught.value)


def test_a_secret_straddling_the_stderr_display_limit_does_not_leak_a_prefix(make_session):
    lines = []
    session = make_session(on_stderr=lines.append)
    session.start()
    session.request("leak", {"pad": 1985}, secrets={"key": SECRET}, timeout=5)
    assert wait_until(lambda: lines)
    assert all(SECRET[:8] not in line and SECRET[-8:] not in line for line in lines)
    assert all(len(line) <= 2000 for line in lines)


def test_an_error_from_a_later_request_cannot_leak_an_earlier_requests_secret(session):
    session.request("echo", secrets={"key": SECRET}, timeout=5)
    # Request B has no secrets of its own. Its message puts the first ten
    # characters of request A's secret right at the 4096 character cut.
    with pytest.raises(RequestFailed) as caught:
        session.request("leak_seen_error", {"pad": 4096 - 10, "tail": 50}, timeout=5)
    for text in (str(caught.value), caught.value.traceback):
        for start in range(len(SECRET) - 5):
            assert SECRET[start:start + 6] not in text


def test_chunked_result_without_a_checksum_is_a_protocol_error(session):
    with pytest.raises(ProtocolViolation):
        session.request("chunks", {"size": 1000, "no_sha": True}, timeout=5)
    assert session.alive


def test_chunked_result_without_a_chunk_count_is_a_protocol_error(session):
    with pytest.raises(ProtocolViolation):
        session.request("chunks", {"size": 1000, "no_chunks": True}, timeout=5)
    assert session.alive


# -------------------------------------------------------- fake translator knobs


def recording_calls():
    log = []

    def record(name):
        def handler(call):
            log.append((name, dict(call.params)))
            return {}
        return handler

    return log, {name: record(name) for name in ("reserve", "checkpoint", "usage")}


def names(log):
    return [entry[0] for entry in log]


def test_fake_fails_at_the_requested_batch(make_session):
    log, calls = recording_calls()
    session = make_session(host_calls=calls)
    session.start()
    fail = {"code": "provider_down", "message": "no", "retryable": True}
    with pytest.raises(RequestFailed) as caught:
        session.request("translate", {"lines": list("abcdef"), "batch": 2, "fail": fail, "fail_at_batch": 2}, timeout=10)
    assert caught.value.code == "provider_down"
    assert names(log) == ["reserve", "checkpoint", "checkpoint"]


def test_fake_dies_after_reserve_before_any_usage(make_session):
    log, calls = recording_calls()
    session = make_session(host_calls=calls)
    session.start()
    with pytest.raises(WorkerExited) as caught:
        session.request("translate", {"lines": list("abc"), "die_after": "reserve"}, timeout=10)
    assert caught.value.exit_code == 9
    assert names(log) == ["reserve"]


def test_fake_dies_after_the_work_and_before_usage(make_session):
    log, calls = recording_calls()
    session = make_session(host_calls=calls)
    session.start()
    with pytest.raises(WorkerExited):
        session.request("translate", {"lines": list("abcd"), "batch": 2, "die_after": "work"}, timeout=10)
    assert names(log) == ["reserve", "checkpoint", "checkpoint"]


def test_fake_reports_partial_usage_on_failure(make_session):
    log, calls = recording_calls()
    session = make_session(host_calls=calls)
    session.start()
    with pytest.raises(RequestFailed):
        session.request(
            "translate",
            {"lines": list("abcd"), "batch": 2, "fail": {"code": "x"}, "usage_on_failure": True},
            timeout=10,
        )
    assert log[-1] == ("usage", {"units": 2, "target": "xx", "partial": True})


def test_fake_reports_partial_usage_on_cancel(make_session):
    log, calls = recording_calls()
    session = make_session(host_calls=calls)
    session.start()
    handle = session.submit(
        "translate", {"lines": list("abcdefgh"), "batch": 2, "delay": 0.3, "usage_on_failure": True})
    assert wait_until(lambda: "checkpoint" in names(log))
    handle.cancel("user")
    with pytest.raises(RequestCancelled):
        handle.wait(5)
    assert wait_until(lambda: names(log)[-1] == "usage")
    assert log[-1][1]["partial"] is True
    assert 0 < log[-1][1]["units"] < 8


def test_fake_reports_partial_usage_when_a_checkpoint_is_refused(make_session):
    log, calls = recording_calls()

    def refuse(call):
        log.append(("checkpoint", dict(call.params)))
        raise HostCallError("store_full", "no room for the checkpoint")

    calls["checkpoint"] = refuse
    session = make_session(host_calls=calls)
    session.start()
    with pytest.raises(RequestFailed):
        session.request("translate", {"lines": list("abcd"), "batch": 2, "usage_on_failure": True}, timeout=10)
    assert names(log) == ["reserve", "checkpoint", "usage"]
    assert log[-1] == ("usage", {"units": 2, "target": "xx", "partial": True})


def test_fake_script_gives_each_attempt_its_own_outcome(make_session):
    log, calls = recording_calls()
    script = [
        {"fail": {"code": "rate_limited", "retryable": True, "retry_after": 3}},
        {"die_after": "batch:1"},
        {},
    ]
    session = make_session(
        {"script": script}, host_calls=calls, limits=fast_limits(max_in_flight=1))
    session.start()
    with pytest.raises(RequestFailed) as first:
        session.request("translate", {"lines": ["a", "b"]}, timeout=10)
    assert first.value.retry_after == 3
    with pytest.raises(WorkerExited):
        session.request("translate", {"lines": ["a", "b"]}, timeout=10)


# ------------------------------------------- limits, secrets and the reply path

READS_REQUESTS = """
def next_request():
    while True:
        frame = inp.read_frame()
        if frame is None:
            sys.exit(0)
        if frame[0] == frames.FRAME_CONTROL:
            message = frames.decode_control(frame[1], "host")
            if message["t"] == "req":
                return message["id"]
"""


def with_reader(body):
    return READS_REQUESTS + textwrap.dedent(body)


def test_empty_data_chunk_is_a_protocol_violation(tmp_path):
    body = with_reader("""
    rid = next_request()
    sys.stdout.buffer.write(frames.HEADER.pack(frames.FRAME_DATA, 4) + frames.DATA_PREFIX.pack(rid))
    sys.stdout.buffer.flush()
    time.sleep(30)
    """)
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(ProtocolViolation):
            session.submit("x").wait(8)
    finally:
        session.close(grace=0.3)


def test_a_result_arriving_in_too_many_chunks_is_refused(tmp_path):
    body = with_reader("""
    rid = next_request()
    for _ in range(200):
        out.write_data(rid, b"x", chunk_size=1)
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {}, "chunks": 200, "bytes": 200})
    time.sleep(30)
    """)
    session = hostile_session(tmp_path, body, max_result_chunks=50)
    try:
        session.start()
        with pytest.raises(ResultTooLarge):
            session.submit("x").wait(8)
    finally:
        session.close(grace=0.3)


def test_data_for_nothing_in_flight_is_not_proof_of_life(tmp_path):
    body = """
    while True:
        out.write_data(999, b"junk")
        time.sleep(0.05)
    """
    session = hostile_session(tmp_path, body, heartbeat_timeout=0.6)
    try:
        session.start()
        handle = session.submit("x")
        with pytest.raises(HeartbeatTimeout):
            handle.wait(8)
    finally:
        session.close(grace=0.3)


def test_call_then_reply_cannot_stack_blocked_handlers_past_the_cap(tmp_path):
    body = """
    while True:
        rid = next_request()
        out.write_control({"t": "call", "cid": rid, "id": rid, "name": "stuck", "params": {}})
        out.write_control({"t": "res", "id": rid, "ok": True, "result": {}})
    """
    release = threading.Event()
    session = hostile_session(tmp_path, with_reader(body), max_pending_calls=4)
    session.register_host_call("stuck", lambda call: release.wait(10) and {})
    try:
        session.start()
        process = session.process
        for _ in range(12):
            if not session.alive:
                break
            try:
                session.request("x", timeout=3)
            except Exception:
                break
        assert wait_until(lambda: process.poll() is not None, timeout=8)
        assert not session.alive
        live = [t for t in threading.enumerate() if "calls" in t.name and t.is_alive()]
        assert len(live) <= 4
    finally:
        release.set()
        session.close(grace=0.3)


def test_worker_that_closes_stdout_but_keeps_running_is_ended(tmp_path):
    body = """
    out.write_control({"t": "bye"})
    os.close(1)
    time.sleep(60)
    """
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        process = session.process
        assert wait_until(lambda: process.poll() is not None, timeout=8)
    finally:
        session.close(grace=0.3)


def test_secret_rotation_does_not_forget_a_secret_of_an_active_request(make_session):
    lines = []
    session = make_session(on_stderr=lines.append)
    session.start()
    first = "sk-first-0123456789abcdef"
    active = session.submit("leak", {"after": 1.0}, secrets={"key": first}, timeout=10)
    for index in range(80):
        session.request("echo", secrets={"key": "sk-other-%04d-padding" % index}, timeout=5)
    active.wait(8)
    assert wait_until(lambda: any("leaked" in line for line in lines))
    assert not any(first in line for line in lines)


def test_session_asks_to_be_replaced_at_the_secret_cap(make_session):
    session = make_session(limits=fast_limits(max_secret_values=3))
    session.start()
    for index in range(3):
        session.request("echo", secrets={"key": "secret-value-%d" % index}, timeout=5)
    from provider_hub.runtime.errors import SessionError

    with pytest.raises(SessionError) as caught:
        session.submit("echo", secrets={"key": "one-more-secret"})
    assert caught.value.code == frames.Codes.RECYCLE_REQUIRED
    assert session.recycle_needed
    assert session.request("echo", secrets={"key": "secret-value-1"}, timeout=5).result["echo"] == {}


def test_host_call_failure_is_logged_without_the_raw_exception(make_session, caplog):
    def boom(call):
        raise RuntimeError("upstream said key " + SECRET)

    session = make_session(host_calls={"boom": boom})
    session.start()
    caplog.set_level("WARNING", logger="provider_hub.runtime.session")
    with pytest.raises(RequestFailed):
        session.request("call", {"name": "boom"}, secrets={"key": SECRET}, timeout=5)
    records = [r for r in caplog.records if "boom" in r.getMessage()]
    assert records
    for record in records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
        assert SECRET not in (record.exc_text or "")


def test_error_text_is_bounded_after_scrubbing(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("raise", {"message": "m" * 1_000_000}, timeout=5)
    assert len(str(caught.value)) <= 4096
    assert len(caught.value.traceback) <= 8000


def test_a_host_handler_error_that_cannot_be_formatted_still_gets_an_ack(make_session):
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("no text for this one")

    def broken(call):
        raise Unprintable()

    session = make_session(host_calls={"broken": broken, "good": lambda call: {"ok": 1}})
    session.start()
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "broken"}, timeout=5)
    assert caught.value.code == frames.Codes.HOST_CALL_FAILED
    assert "host handler failed" in str(caught.value)
    assert session.request("call", {"name": "good"}, timeout=5).result["ack"] == {"ok": 1}
    assert wait_until(lambda: not session._call_queues and session._call_pending == 0)


def test_an_unknown_result_id_is_not_written_to_the_log(tmp_path, caplog):
    secret_id = 4815162342
    body = with_reader("""
    rid = next_request()
    out.write_control({"t": "res", "id": %d, "ok": True, "result": {}})
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {"done": True}})
    time.sleep(30)
    """ % secret_id)
    session = hostile_session(tmp_path, body)
    caplog.set_level("DEBUG", logger="provider_hub.runtime.session")
    try:
        session.start()
        assert session.submit("x").wait(8).result == {"done": True}
    finally:
        session.close(grace=0.3)
    messages = [r.getMessage() for r in caplog.records]
    assert any("unknown request" in m for m in messages)
    assert not any(str(secret_id) in m for m in messages)


def test_an_unprintable_error_still_gets_a_terminal_reply(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("bad_str", timeout=5)
    assert caught.value.code == frames.Codes.PLUGIN_ERROR
    assert session.alive


def test_cancel_interrupts_the_wait_for_a_host_call_ack(make_session):
    release = threading.Event()
    session = make_session(host_calls={"slow": lambda call: release.wait(10) and {}})
    session.start()
    try:
        handle = session.submit("call", {"name": "slow"})
        time.sleep(0.2)
        started = time.monotonic()
        handle.cancel("user")
        with pytest.raises(RequestCancelled):
            handle.wait(5)
        assert time.monotonic() - started < 0.35
        # The worker answered the cancel itself: it was not killed for ignoring it.
        assert session.alive
    finally:
        release.set()


def test_one_slot_worker_accepts_the_next_request_at_once(make_session):
    session = make_session({"max_in_flight": 1})
    session.start()
    for index in range(60):
        assert session.request("echo", {"n": index}, timeout=5).result["echo"] == {"n": index}


# ------------------------------------------- worker text, encoding and shutdown


LONG_SECRET = "sk-" + "abcdefghij" * 20


def test_unknown_message_name_is_never_echoed_in_the_violation(tmp_path):
    body = with_reader(
        """
        next_request()
        out.write_control({"t": %r})
        time.sleep(30)
        """
        % LONG_SECRET
    )
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(ProtocolViolation) as caught:
            session.submit("x", secrets={"key": LONG_SECRET}).wait(8)
        text = str(caught.value)
        assert "unknown message type" in text
        # Not the whole value and not a piece of it, whatever the cut.
        assert LONG_SECRET[:10] not in text and LONG_SECRET[-10:] not in text
    finally:
        session.close(grace=0.3)


def test_error_code_echoing_a_secret_is_not_passed_on(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("raw_error", {"code": SECRET}, secrets={"key": SECRET}, timeout=5)
    assert SECRET not in caught.value.code
    assert caught.value.code == frames.Codes.PLUGIN_ERROR
    with pytest.raises(RequestFailed) as odd:
        session.request("raw_error", {"code": "not a code!" * 20}, secrets={"key": SECRET}, timeout=5)
    assert odd.value.code == frames.Codes.PLUGIN_ERROR


@pytest.mark.parametrize(
    "result",
    [
        {"nan": float("nan")},
        {"big": "x" * 2_000_000},
        {"object": object()},
    ],
    ids=["nan", "oversized", "not-json"],
)
def test_unsendable_host_call_result_becomes_a_failure_ack(make_session, result):
    session = make_session(host_calls={"bad": lambda call: result, "good": lambda call: {"ok": 1}})
    session.start()
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "bad"}, timeout=5)
    assert caught.value.code == frames.Codes.HOST_CALL_FAILED
    assert session.request("call", {"name": "good"}, timeout=5).result["ack"] == {"ok": 1}
    assert wait_until(lambda: not session._call_queues and session._call_pending == 0)


def test_a_result_that_cannot_be_encoded_gets_an_error_reply_and_the_channel_lives(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("nan_result", timeout=5)
    assert caught.value.code == frames.Codes.PLUGIN_ERROR
    assert session.request("echo", {"after": 1}, timeout=5).result["echo"] == {"after": 1}


def test_params_that_cannot_be_encoded_do_not_leak_a_slot(make_session):
    session = make_session({"max_in_flight": 1})
    session.start()
    with pytest.raises(TypeError):
        session.submit("echo", {"o": object()})
    with pytest.raises(ValueError):
        session.submit("echo", {"n": float("nan")})
    assert session.in_flight == 0
    assert session.request("echo", {"fine": True}, timeout=5).result["echo"] == {"fine": True}


def test_shutdown_still_delivers_acks_to_cleanup_calls(make_session):
    log, calls = recording_calls()
    session = make_session(host_calls=calls)
    session.start()
    process = session.process
    handle = session.submit(
        "translate", {"lines": list("abcdefgh"), "batch": 2, "delay": 0.3, "usage_on_failure": True})
    assert wait_until(lambda: "checkpoint" in names(log))
    started = time.monotonic()
    session.close(grace=4.0)
    assert time.monotonic() - started < 3.5
    assert process.returncode == 0
    assert log[-1][0] == "usage" and log[-1][1]["partial"] is True
    assert handle.done()


def test_requests_during_shutdown_are_refused(tmp_path):
    from provider_hub.runtime.worker_channel import WorkerChannel

    host_to_worker_r, host_to_worker_w = os.pipe()
    worker_to_host_r, worker_to_host_w = os.pipe()
    gate = threading.Event()

    def handler(operation):
        gate.wait(5)
        return {}

    channel = WorkerChannel(
        handler,
        kinds=[],
        stdin=os.fdopen(host_to_worker_r, "rb", buffering=0),
        stdout=os.fdopen(worker_to_host_w, "wb"),
        redirect_stdout=False,
        heartbeat_interval=30,
        wait_grace=3.0,
    )
    status = []
    thread = threading.Thread(target=lambda: status.append(channel.run()), daemon=True)
    thread.start()
    host_in = frames.FrameReader(os.fdopen(worker_to_host_r, "rb"))
    host_out = frames.FrameWriter(os.fdopen(host_to_worker_w, "wb"))
    assert frames.decode_control(host_in.read_frame()[1], "worker")["t"] == "hello"
    host_out.write_control({"t": "welcome", "protocol": {"major": 2, "minor": 0}, "limits": {}, "session": "t"})
    host_out.write_control({"t": "req", "id": 1, "op": "hold", "params": {}})
    host_out.write_control({"t": "shutdown"})
    host_out.write_control({"t": "req", "id": 2, "op": "late", "params": {}})
    refused = frames.decode_control(host_in.read_frame()[1], "worker")
    assert refused["id"] == 2
    assert refused["error"]["code"] == frames.Codes.SHUTTING_DOWN
    gate.set()
    seen = [frames.decode_control(host_in.read_frame()[1], "worker")["t"] for _ in range(2)]
    assert seen == ["res", "bye"]
    thread.join(5)
    assert status == [0]


PEM_SECRET = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
    "q1w2e3r4t5y6u7i8o9p0aSdFgHjKlZxCvBnMqWeRtYuIoPaSdFgH\n"
    "-----END PRIVATE KEY-----"
)


def test_each_line_of_a_multiline_secret_is_redacted_from_stderr(make_session):
    lines = []
    session = make_session(on_stderr=lines.append)
    session.start()
    session.request("leak", secrets={"key": PEM_SECRET}, timeout=5)
    assert wait_until(lambda: len(lines) >= 4)
    for part in PEM_SECRET.splitlines():
        assert not any(part in line for line in lines)


def test_multiline_secret_is_redacted_from_error_text_too(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("leak_error", {"tail": 5}, secrets={"key": PEM_SECRET}, timeout=5)
    for part in PEM_SECRET.splitlines():
        assert part not in str(caught.value)
        assert part not in caught.value.traceback


SHORT_LINES_SECRET = "abc123\nxyz456"


def test_short_lines_of_a_multiline_secret_are_redacted_from_stderr(make_session):
    lines = []
    session = make_session(on_stderr=lines.append)
    session.start()
    session.request("leak", secrets={"key": SHORT_LINES_SECRET}, timeout=5)
    assert wait_until(lambda: len(lines) >= 2)
    assert not any("abc123" in line or "xyz456" in line for line in lines)


@pytest.mark.parametrize("part", [0, 1])
def test_a_single_short_line_of_a_multiline_secret_is_scrubbed_from_error_text(session, part):
    line = SHORT_LINES_SECRET.splitlines()[part]
    with pytest.raises(RequestFailed) as caught:
        session.request("leak_error", {"tail": 5, "part": part}, secrets={"key": SHORT_LINES_SECRET}, timeout=5)
    assert line not in str(caught.value)
    assert line not in caught.value.traceback


def test_a_request_is_always_queued_before_its_cancel(make_session, monkeypatch):
    session = make_session(limits=fast_limits(tick=0.005))
    session.start()
    queued = []
    real_put = session._outbox.put_nowait

    def record(data):
        message = json.loads(data[frames.HEADER.size:])
        if message["t"] in (frames.REQ, frames.CANCEL):
            queued.append(message["t"])
        real_put(data)

    monkeypatch.setattr(session._outbox, "put_nowait", record)
    real_pack = frames.pack_control

    def slow_pack(message, *args, **kwargs):
        # The window between a request becoming known and its frame being
        # queued. The pre-check pack uses id 0 and is left alone.
        if message.get("t") == frames.REQ and message.get("id"):
            time.sleep(0.2)
        return real_pack(message, *args, **kwargs)

    monkeypatch.setattr(frames, "pack_control", slow_pack)
    # A deadline that has already passed: the watchdog cancels at its first look.
    handle = session.submit("sleep", {"seconds": 5}, timeout=0)
    with pytest.raises(RequestTimeout):
        handle.wait(8)
    assert queued == [frames.REQ, frames.CANCEL]


def test_start_after_close_is_refused_and_spawns_nothing():
    spawned = []
    session = WorkerSession(spawn=lambda: spawned.append(1), limits=fast_limits())
    session.close()
    with pytest.raises(SessionClosed):
        session.start()
    assert spawned == []


def test_close_racing_start_ends_the_worker_that_was_spawned():
    import subprocess

    started = threading.Event()
    processes = []

    def slow_spawn():
        started.set()
        time.sleep(0.4)
        process = subprocess.Popen(
            fake_worker_command(), env={**os.environ, **fake_worker_env()},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        processes.append(process)
        return process

    session = WorkerSession(spawn=slow_spawn, kind="translator", limits=fast_limits())
    outcome = []

    def run_start():
        try:
            session.start()
            outcome.append("started")
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=run_start, daemon=True)
    thread.start()
    assert started.wait(5)
    session.close(grace=0.5)
    thread.join(10)
    assert processes
    assert wait_until(lambda: processes[0].poll() is not None)
    assert not session.alive


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_unusable_timeout_is_refused_before_a_slot_is_taken(make_session, bad):
    session = make_session({"max_in_flight": 1})
    session.start()
    with pytest.raises(ValueError):
        session.submit("echo", timeout=bad)
    assert session.in_flight == 0
    assert session.request("echo", timeout=5).result["echo"] == {}


def test_a_handler_returning_a_list_fails_only_that_request(session):
    with pytest.raises(RequestFailed) as caught:
        session.request("list_result", timeout=5)
    assert caught.value.code == frames.Codes.PLUGIN_ERROR
    assert "not an object" in str(caught.value)
    assert session.alive
    assert session.request("echo", timeout=5).result["echo"] == {}


# ------------------------------------- redaction, acceptance and wake paths

NUMERIC_SECRET = "4815162342"


@pytest.mark.parametrize("field", ["bytes", "chunks"])
def test_a_count_mismatch_does_not_echo_a_numeric_secret(tmp_path, field):
    body = with_reader("""
    rid = next_request()
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {}, "%s": 4815162342})
    time.sleep(30)
    """ % field)
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(ProtocolViolation) as caught:
            session.submit("x", secrets={"pin": NUMERIC_SECRET}).wait(8)
    finally:
        session.close(grace=0.3)
    assert NUMERIC_SECRET not in str(caught.value)
    assert "***" in str(caught.value)


def test_numbers_that_spell_a_secret_are_dropped_from_every_worker_field(tmp_path):
    body = with_reader("""
    rid = next_request()
    out.write_control({"t": "hb", "seq": 1, "rss_kb": 4815162342, "note": "n4815162342"})
    out.write_control({"t": "progress", "id": rid, "fraction": 4815162342,
                       "data": {"4815162342": [4815162342, 7]}})
    out.write_control({"t": "res", "id": rid, "ok": False,
                       "error": {"code": "plugin_error", "message": "m",
                                 "retry_after": 4815162342, "errno": 4815162342}})
    time.sleep(30)
    """)
    session = hostile_session(tmp_path, body)
    seen = []
    try:
        session.start()
        with pytest.raises(RequestFailed) as caught:
            session.submit("x", secrets={"pin": NUMERIC_SECRET}, on_progress=seen.append).wait(8)
        heartbeat = session.last_heartbeat
        rss = session.rss_kb
    finally:
        session.close(grace=0.3)
    assert caught.value.errno is None
    assert caught.value.retry_after is None
    assert seen
    assert NUMERIC_SECRET not in json.dumps(seen)
    assert seen[0]["data"] == {"***": [None, 7]}
    assert NUMERIC_SECRET not in json.dumps(heartbeat)
    assert rss is None


def test_callback_failures_are_logged_without_the_raw_exception(make_session, caplog):
    def bad_progress(message):
        raise ValueError("progress callback saw " + SECRET)

    def bad_event(name, payload):
        raise ValueError("event callback saw " + SECRET)

    session = make_session(on_event=bad_event)
    session.start()
    caplog.set_level("DEBUG", logger="provider_hub.runtime.session")
    session.request("progress_secret", secrets={"key": SECRET}, on_progress=bad_progress, timeout=5)
    with pytest.raises(RequestFailed):
        session.request("blocked", secrets={"key": SECRET}, timeout=5)
    records = [r for r in caplog.records if "callback failed" in r.getMessage()]
    assert {"progress", "event"} <= {r.getMessage().split(" callback failed")[0].split()[-1] for r in records}
    for record in records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
        assert not record.exc_text


def test_a_success_after_the_deadline_is_a_timeout_before_the_watchdog_ticks(make_session):
    session = make_session(limits=fast_limits(tick=30.0, heartbeat_timeout=60.0))
    session.start()
    with pytest.raises(RequestTimeout):
        session.request("sleep", {"seconds": 0.7}, timeout=0.2)
    assert session.alive


def test_a_cancelled_reply_after_the_deadline_is_a_timeout_before_the_watchdog_ticks(tmp_path):
    body = with_reader("""
    rid = next_request()
    time.sleep(0.6)
    out.write_control({"t": "res", "id": rid, "ok": False, "error": {"code": "cancelled", "message": "c"}})
    time.sleep(30)
    """)
    session = hostile_session(tmp_path, body, tick=30.0)
    try:
        session.start()
        with pytest.raises(RequestTimeout):
            session.submit("x", timeout=0.2).wait(8)
    finally:
        session.close(grace=0.3)


def test_a_reply_that_breaks_the_reader_does_not_strand_its_request(make_session, monkeypatch):
    session = make_session()
    session.start()

    def broken(*args, **kwargs):
        raise RuntimeError("cannot build the error")

    monkeypatch.setattr(session, "_failed", broken)
    with pytest.raises(ProtocolViolation):
        session.submit("raise").wait(8)


def test_a_watchdog_that_crashes_ends_the_session_instead_of_dying_quietly(make_session, monkeypatch):
    session = make_session()
    session.start()

    def broken(*args, **kwargs):
        raise RuntimeError("tick broke")

    monkeypatch.setattr(session, "_cancel", broken)
    handle = session.submit("sleep", {"seconds": 30}, timeout=0.1)
    with pytest.raises(SessionError):
        handle.wait(8)
    assert not session.alive


class _BreaksOnSecondWrite:
    """A worker stdin that takes the welcome and then fails with something other than an OSError."""

    def __init__(self, stream):
        self._stream = stream
        self._writes = 0

    def write(self, data):
        self._writes += 1
        if self._writes > 1:
            raise RuntimeError("write broke")
        return self._stream.write(data)

    def flush(self):
        self._stream.flush()

    def close(self):
        self._stream.close()


def test_a_writer_that_breaks_ends_the_session_instead_of_dying_quietly(tmp_path):
    script = tmp_path / "quiet.py"
    script.write_text(HOSTILE_PRELUDE + "time.sleep(30)\n", encoding="utf-8")

    def spawn():
        process = subprocess.Popen([sys.executable, "-B", str(script)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        process.stdin = _BreaksOnSecondWrite(process.stdin)
        return process

    session = WorkerSession(spawn=spawn, limits=fast_limits(max_in_flight=16, heartbeat_timeout=30.0))
    try:
        session.start()
        with pytest.raises(SessionError):
            session.submit("x").wait(8)
        assert not session.alive
    finally:
        session.close(grace=0.3)


def test_requests_are_finished_even_when_the_kill_itself_fails(make_session, monkeypatch):
    session = make_session()
    session.start()
    handle = session.submit("sleep", {"seconds": 30})

    def broken(process):
        raise RuntimeError("kill broke")

    monkeypatch.setattr(session, "_kill_tree", broken)
    with pytest.raises(RuntimeError):
        session._fail(ProtocolViolation("worker broke the contract"), kill=True)
    with pytest.raises(ProtocolViolation):
        handle.wait(2)


def test_start_reports_a_session_that_ended_during_the_handshake(tmp_path):
    session = hostile_session(tmp_path, "time.sleep(30)\n")

    class EndsAfterHello(threading.Event):
        def wait(self, timeout=None):
            result = super().wait(timeout)
            session._fail(SessionClosed("closed meanwhile"), kill=True)
            return result

    session._hello = EndsAfterHello()
    try:
        with pytest.raises(HandshakeError):
            session.start()
    finally:
        session.close(grace=0.3)


def test_a_hello_landing_as_the_handshake_times_out_is_not_killed(tmp_path):
    session = hostile_session(tmp_path, "time.sleep(30)\n")

    class TimesOutAfterHello(threading.Event):
        def wait(self, timeout=None):
            super().wait(timeout)
            return False

    session._hello = TimesOutAfterHello()
    try:
        assert session.start() is session.welcome
        assert session.alive
    finally:
        session.close(grace=0.3)


def test_an_outbox_that_drained_after_it_was_judged_full_does_not_end_the_session(make_session, monkeypatch):
    session = make_session()
    session.start()
    real_put = session._put
    judged = []

    def full_once(data):
        if not judged:
            judged.append(True)
            return session_module._FULL
        return real_put(data)

    monkeypatch.setattr(session, "_put", full_once)
    assert session._send_quietly({"t": frames.CANCEL, "id": 999}) is True
    assert session.alive
    judged.clear()
    with pytest.raises(SessionBusy):
        session.submit("echo")
    assert session.alive
    assert session.request("echo", timeout=5).result["echo"] == {}


def test_a_queued_host_call_does_not_start_once_the_session_is_closed(make_session):
    ran = []
    session = make_session(host_calls={"h": lambda call: ran.append(call.name) or {}})
    session.start()
    session._call_queues[7] = deque([{"t": "call", "cid": 1, "name": "h", "params": {}, "id": 7}])
    session._call_pending = 1
    session._closed = True
    try:
        session._call_worker(7)
    finally:
        # Put it back so the fixture's close() runs the real shutdown path.
        session._closed = False
    assert ran == []


# --------------------------------------------------- worker channel wake paths


class ChannelHarness:
    """A WorkerChannel on real pipes, with the test acting as the host."""

    def __init__(self, handler, **kwargs):
        to_worker_r, to_worker_w = os.pipe()
        from_worker_r, from_worker_w = os.pipe()
        self.channel = worker_channel.WorkerChannel(
            handler, kinds=[{"kind": "translator", "major": 1, "minor": 0}],
            stdin=os.fdopen(to_worker_r, "rb", buffering=0), stdout=os.fdopen(from_worker_w, "wb"),
            redirect_stdout=False, **kwargs)
        self.to_worker = os.fdopen(to_worker_w, "wb")
        self.host_out = frames.FrameWriter(self.to_worker)
        self.from_worker = os.fdopen(from_worker_r, "rb")
        self.messages = []
        self.status = []
        self.thread = threading.Thread(target=lambda: self.status.append(self.channel.run()), daemon=True)

    def start(self):
        self.host_out.write_control({
            "t": "welcome", "protocol": {"major": 2, "minor": 0}, "kind": None,
            "limits": {"heartbeat_interval_ms": 50}, "session": "test"})
        self.thread.start()
        collector = threading.Thread(target=self._collect, daemon=True)
        collector.start()
        assert wait_until(lambda: any(m["t"] == "hello" for m in self.messages))

    def _collect(self):
        reader = frames.FrameReader(self.from_worker)
        try:
            while True:
                frame = reader.read_frame()
                if frame is None:
                    return
                if frame[0] == frames.FRAME_CONTROL:
                    self.messages.append(frames.decode_control(frame[1], "worker"))
        except (OSError, ValueError, frames.FrameError):
            return

    def hang_up_input(self):
        self.to_worker.close()

    def stop_reading_output(self):
        self.from_worker.close()

    def finish(self):
        for closer in (self.to_worker.close,):
            try:
                closer()
            except OSError:
                pass
        self.thread.join(10)
        try:
            self.channel._writer._stream.close()
        except OSError:
            pass


def test_run_returns_when_the_output_fails_while_the_host_still_holds_the_input():
    harness = ChannelHarness(lambda operation: {}, heartbeat_interval=0.05)
    harness.start()
    try:
        harness.stop_reading_output()
        harness.thread.join(5)
        assert not harness.thread.is_alive()
    finally:
        harness.finish()


def test_a_call_made_after_the_host_hung_up_fails_at_once():
    gate = threading.Event()
    outcome = {}

    def handler(operation):
        gate.wait(5)
        started = time.monotonic()
        try:
            operation.call("usage", {}, timeout=30, cancellable=False)
        except worker_channel.ChannelClosed:
            outcome["waited"] = time.monotonic() - started
        return {}

    harness = ChannelHarness(handler, wait_grace=4.0)
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}})
        assert wait_until(lambda: 1 in harness.channel._operations)
        harness.hang_up_input()
        assert wait_until(lambda: harness.channel._ended.is_set())
        time.sleep(0.3)
        gate.set()
        assert wait_until(lambda: "waited" in outcome, timeout=8)
        assert outcome["waited"] < 1.5
    finally:
        gate.set()
        harness.finish()


def test_an_operation_that_cannot_get_a_thread_is_answered_with_an_error(monkeypatch):
    harness = ChannelHarness(lambda operation: {})
    harness.start()
    real_start = threading.Thread.start

    def start(self):
        if getattr(self, "_target", None) == harness.channel._run_operation:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}})
        assert wait_until(lambda: any(m["t"] == "res" for m in harness.messages))
        reply = next(m for m in harness.messages if m["t"] == "res")
        assert reply["id"] == 1 and reply["ok"] is False
        assert reply["error"]["code"] == frames.Codes.PLUGIN_ERROR
        assert 1 not in harness.channel._operations
    finally:
        monkeypatch.undo()
        harness.finish()


# ------------------------------------------ handshake order and operation lifetime


def recording_outbox(session):
    """The frame types the session queues for its worker, in order."""
    queued = []
    real_put = session._outbox.put_nowait

    def record(data):
        queued.append(json.loads(data[frames.HEADER.size:])["t"])
        real_put(data)

    session._outbox.put_nowait = record
    return queued


def test_a_request_cannot_be_queued_ahead_of_the_welcome(make_session):
    session = make_session()
    queued = recording_outbox(session)
    real_put = session._put
    in_welcome = threading.Event()

    def slow_welcome(data):
        if json.loads(data[frames.HEADER.size:])["t"] == frames.WELCOME:
            in_welcome.set()
            time.sleep(0.4)
        return real_put(data)

    session._put = slow_welcome
    starter = threading.Thread(target=session.start, daemon=True)
    starter.start()
    submitted = []

    def submit_when_possible():
        assert in_welcome.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                submitted.append(session.submit("echo"))
                return
            except SessionError:
                time.sleep(0.01)

    submitter = threading.Thread(target=submit_when_possible, daemon=True)
    submitter.start()
    submitter.join(10)
    starter.join(10)
    assert submitted
    assert submitted[0].wait(5).result["echo"] == {}
    assert queued[:2] == [frames.WELCOME, frames.REQ]


def test_nothing_is_published_when_the_welcome_could_not_be_queued(make_session):
    session = make_session()
    real_put = session._put

    def refuse_welcome(data):
        if json.loads(data[frames.HEADER.size:])["t"] == frames.WELCOME:
            return session_module._FULL
        return real_put(data)

    session._put = refuse_welcome
    with pytest.raises(HandshakeError):
        session.start()
    assert session.welcome is None
    assert not session.alive


def test_close_during_the_handshake_queues_no_frame_ahead_of_the_welcome(tmp_path):
    script = tmp_path / "silent.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    session = WorkerSession([sys.executable, "-B", str(script)], limits=fast_limits(handshake_timeout=20.0))
    queued = recording_outbox(session)
    outcome = []

    def run_start():
        try:
            session.start()
        except Exception as error:
            outcome.append(error)

    starter = threading.Thread(target=run_start, daemon=True)
    starter.start()
    assert wait_until(lambda: session.process is not None)
    began = time.monotonic()
    session.close(grace=5.0)
    starter.join(10)
    assert time.monotonic() - began < 4.0
    assert queued == []
    assert outcome and isinstance(outcome[0], HandshakeError)


class _ChunkedWorker:
    """A WorkerChannel whose big result takes a while to stream, one chunk at a time."""

    def __init__(self, **kwargs):
        self.written = []
        self.first_chunk = threading.Event()
        data = b"x" * (frames.DEFAULT_CHUNK_BYTES * 6)

        def handler(operation):
            if operation.op == "big":
                return worker_channel.OperationResult({"size": len(data)}, data)
            return {}

        self.harness = ChannelHarness(handler, **kwargs)
        writer = self.harness.channel._writer
        real_write = writer._write

        def slow_write(ftype, body):
            if ftype == frames.FRAME_DATA:
                self.written.append(len(body))
                self.first_chunk.set()
                time.sleep(0.3)
            return real_write(ftype, body)

        writer._write = slow_write


def test_a_cancel_stops_a_result_that_is_being_streamed():
    worker = _ChunkedWorker()
    harness = worker.harness
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "big", "params": {}})
        assert worker.first_chunk.wait(5)
        # The handler is done: the slot is free, but a cancel must still find the operation.
        assert 1 not in harness.channel._operations
        assert 1 in harness.channel._transmitting
        harness.host_out.write_control({"t": "cancel", "id": 1, "reason": "enough"})
        assert wait_until(lambda: any(m["t"] == "res" for m in harness.messages), timeout=8)
        reply = next(m for m in harness.messages if m["t"] == "res")
        assert reply["ok"] is False and reply["error"]["code"] == frames.Codes.CANCELLED
        assert len(worker.written) < 6
        assert wait_until(lambda: not harness.channel._transmitting)
        assert not harness.channel._operations
    finally:
        harness.finish()


def test_a_slot_is_free_for_the_next_request_while_a_result_streams():
    worker = _ChunkedWorker(max_in_flight=1)
    harness = worker.harness
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "big", "params": {}})
        assert worker.first_chunk.wait(5)
        harness.host_out.write_control({"t": "req", "id": 2, "op": "quick", "params": {}})
        assert wait_until(lambda: any(m["t"] == "res" and m["id"] == 2 for m in harness.messages), timeout=8)
        second = next(m for m in harness.messages if m["t"] == "res" and m["id"] == 2)
        assert second["ok"] is True
        # A reused id is still refused while the first one is on the wire.
        harness.host_out.write_control({"t": "req", "id": 1, "op": "quick", "params": {}})
        assert wait_until(
            lambda: any(m["t"] == "res" and m["id"] == 1 and not m["ok"] for m in harness.messages), timeout=8)
        refused = next(m for m in harness.messages if m["t"] == "res" and m["id"] == 1 and not m["ok"])
        assert refused["error"]["code"] == frames.Codes.INVALID_REQUEST
    finally:
        harness.finish()


def test_a_session_cancels_a_streaming_result_inside_the_grace():
    session = WorkerSession(
        fake_worker_command(), env=fake_worker_env(), kind="translator",
        limits=fast_limits(cancel_grace=1.0))
    session.start()
    try:
        handle = session.submit("chunks", {"size": frames.DEFAULT_CHUNK_BYTES * 3}, timeout=0)
        with pytest.raises(RequestTimeout):
            handle.wait(8)
        assert session.alive
        assert session.request("echo", timeout=5).result["echo"] == {}
    finally:
        session.close(grace=1.0)


# ---------------------------------------------------- partial start and teardown


@pytest.mark.parametrize("label", ["reader", "stderr", "writer", "watchdog"])
def test_a_thread_that_cannot_start_leaves_nothing_behind(make_session, monkeypatch, label):
    session = make_session()
    real_start = threading.Thread.start

    def start(self):
        if self.name == "worker-%s" % label:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError):
        session.start()
    monkeypatch.undo()
    process = session.process
    assert process.poll() is not None
    assert not pid_alive(process.pid)
    assert not session.alive
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    assert wait_until(lambda: not [t for t in threading.enumerate() if t.name.startswith("worker-")])


def test_a_reader_that_cannot_be_built_leaves_nothing_behind(make_session, monkeypatch):
    session = make_session()

    def broken(*args, **kwargs):
        raise RuntimeError("reader broke")

    monkeypatch.setattr(frames, "FrameReader", broken)
    with pytest.raises(RuntimeError):
        session.start()
    monkeypatch.undo()
    process = session.process
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    assert not session.alive


def test_a_host_call_whose_thread_cannot_start_is_answered_and_gives_its_slot_back(make_session, monkeypatch):
    session = make_session(host_calls={"h": lambda call: {}})
    session.start()
    real_start = threading.Thread.start

    def start(self):
        if "-calls-" in self.name:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "h"}, timeout=5)
    monkeypatch.undo()
    assert caught.value.code == frames.Codes.HOST_CALL_FAILED
    assert session.alive
    assert session._call_pending == 0 and not session._call_queues
    assert session.request("call", {"name": "h"}, timeout=5).result["ack"] == {}


def test_a_channel_whose_reader_thread_cannot_start_stops_its_heartbeat(monkeypatch):
    harness = ChannelHarness(lambda operation: {}, heartbeat_interval=0.05)
    real_start = threading.Thread.start

    def start(self):
        if getattr(self, "_target", None) == harness.channel._read_loop:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    harness.host_out.write_control({
        "t": "welcome", "protocol": {"major": 2, "minor": 0}, "kind": None,
        "limits": {"heartbeat_interval_ms": 50}, "session": "test"})
    harness.thread.start()
    try:
        harness.thread.join(5)
        assert harness.status == [3]
        assert harness.channel._closed.is_set()
    finally:
        monkeypatch.undo()
        harness.finish()
