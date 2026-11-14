import errno
import io
import json
import os
import signal
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
    if os.name != "posix":
        # os.kill(pid, 0) terminates the process on Windows.
        pytest.skip("probing a pid with signal 0 is POSIX only")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not sys.platform.startswith("linux"):
        # No /proc here: signal 0 is all there is.
        return True
    # A zombie still answers signal 0; the session reaps its own child, a
    # forked grandchild is reparented and reaped by init.
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        # /proc is hidden or unreadable (hidepid): signal 0 said it exists.
        return True


def submit_running(session, params, **kwargs):
    """Submit a ``sleep`` request and return its handle once the worker says it is running."""
    started = threading.Event()

    def on_progress(message):
        if message.get("message") == "started":
            started.set()

    handle = session.submit("sleep", {**params, "announce": True}, on_progress=on_progress, **kwargs)
    assert started.wait(8), "the worker never started the request"
    return handle


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


_RES_HEAD = b'{"t":"res","id":1,"ok":true,'


@pytest.mark.parametrize("sender, head", [
    ("worker", _RES_HEAD),
    ("host", b'{"t":"ack","cid":1,"ok":true,'),
])
@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"])
@pytest.mark.parametrize("shape", ["flat", "nested", "in-list"])
def test_non_finite_numbers_are_refused_on_both_sides(sender, head, constant, shape):
    value = {
        "flat": constant,
        "nested": b'{"a":{"b":' + constant + b"}}",
        "in-list": b"[1,[" + constant + b"]]",
    }[shape]
    body = head + b'"result":{"x":' + value + b"}}"
    with pytest.raises(frames.MalformedFrame):
        frames.decode_control(body, sender)
    # The same frame with a finite number is fine.
    frames.decode_control(head + b'"result":{"x":1.5}}', sender)


def test_duplicate_keys_are_refused_at_any_depth():
    for body in (
        b'{"t":"hb","seq":1,"seq":2}',
        b'{"t":"hb","seq":1,"extra":{"a":1,"a":2}}',
        b'{"t":"hb","seq":1,"extra":[{"a":1,"a":1}]}',
    ):
        with pytest.raises(frames.MalformedFrame):
            frames.decode_control(body, "worker")
    frames.decode_control(b'{"t":"hb","seq":1,"extra":{"a":1},"other":{"a":2}}', "worker")


def test_nesting_is_bounded_and_deep_input_is_a_protocol_error():
    def nested(levels):
        return b'{"t":"hb","seq":1,"extra":' + b"[" * levels + b"]" * levels + b"}"

    frames.decode_control(nested(frames.MAX_JSON_DEPTH - 2), "worker")
    for levels in (frames.MAX_JSON_DEPTH + 1, 5000, 200_000):
        with pytest.raises(frames.MalformedFrame):
            frames.decode_control(nested(levels), "worker")
    # Unterminated, so the parser itself runs out of stack.
    with pytest.raises(frames.MalformedFrame):
        frames.decode_control(b'{"t":"hb","seq":1,"extra":' + b"[" * 500_000, "worker")


def test_text_that_cannot_be_encoded_is_refused():
    with pytest.raises(frames.MalformedFrame):
        frames.decode_control(b'{"t":"hb","seq":1,"note":"\\ud800"}', "worker")
    with pytest.raises(frames.MalformedFrame):
        frames.decode_control(b'{"t":"hb","seq":1,"\\udc00":1}', "worker")
    with pytest.raises(frames.MalformedFrame):
        frames.decode_control(b'{"t":"hb","seq":1,"note":"\xff"}', "worker")


def test_a_worker_reply_with_a_non_finite_result_fails_the_session(tmp_path):
    body = with_reader("""
    rid = next_request()
    payload = b'{"t":"res","id":%d,"ok":true,"result":{"x":[NaN]}}' % rid
    sys.stdout.buffer.write(frames.HEADER.pack(frames.FRAME_CONTROL, len(payload)) + payload)
    sys.stdout.buffer.flush()
    time.sleep(30)
    """)
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(ProtocolViolation):
            session.submit("x").wait(8)
        assert not session.alive
    finally:
        session.close(grace=0.3)


def _deep(levels):
    value = "bottom"
    for _ in range(levels):
        value = [value]
    return value


@pytest.mark.parametrize("value, error", [
    (_deep(frames.MAX_JSON_DEPTH + 1), frames.UnencodableFrame),
    ({1: "a", "1": "b"}, TypeError),
    ({1: "a"}, TypeError),
    ({None: 1}, TypeError),
    ({(1, 2): 1}, TypeError),
    ({"x": float("nan")}, ValueError),
    ({"x": [float("inf")]}, ValueError),
    ({"x": float("-inf")}, ValueError),
    ({"x": "\ud800"}, frames.UnencodableFrame),
    ({"\udc00": 1}, frames.UnencodableFrame),
    ({"x": object()}, TypeError),
    ({"x": {1, 2}}, TypeError),
    ({"x": b"bytes"}, TypeError),
])
def test_the_encoder_refuses_what_the_decoder_refuses(value, error):
    with pytest.raises(error):
        frames.encode_control({"t": "hb", "seq": 1, "extra": value})
    with pytest.raises(error):
        frames.pack_control({"t": "hb", "seq": 1, "extra": value})


def test_a_circular_message_is_refused_not_looped_on():
    loop = {}
    loop["self"] = loop
    with pytest.raises(frames.UnencodableFrame):
        frames.encode_control({"t": "hb", "seq": 1, "extra": loop})


def _random_value(rng, depth):
    kind = rng.randrange(14 if depth < 110 else 8)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randrange(-10**6, 10**6)
    if kind == 3:
        return rng.choice([0.5, -2.25, 1e300, float("nan"), float("inf"), float("-inf"), -0.0])
    if kind in (4, 5):
        return rng.choice(["", "a", "caf\u00e9", "\U0001f600", "\ud800", "\udfff", "line\nbreak", "\x00"])
    if kind == 6:
        return rng.choice([b"x", object(), {1, 2}])
    if kind == 7:
        return rng.randrange(10)
    if kind in (8, 9):
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
    if kind == 10:
        return tuple(_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 3)))
    if kind == 11:
        # A long chain, so nesting runs past the limit now and then.
        value = _random_value(rng, depth + 1)
        for _ in range(rng.choice([0, 0, 0, 40, 99, 100, 101, 120])):
            value = [value]
        return value
    keys = [rng.choice(["a", "b", "1", "x\u00e9", 1, 2, True, None, 1.5, "\ud800", ("t",)])
            for _ in range(rng.randrange(0, 4))]
    return {key: _random_value(rng, depth + 1) for key in keys}


def _as_json_value(value):
    if isinstance(value, (list, tuple)):
        return [_as_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_json_value(item) for key, item in value.items()}
    return value


def test_anything_the_encoder_accepts_the_decoder_accepts_and_reads_back_equal():
    import random

    rng = random.Random(20260507)
    accepted = refused = 0
    for _ in range(6000):
        extra = _random_value(rng, 0)
        message = {"t": "hb", "seq": 1, "extra": extra}
        try:
            body = frames.encode_control(message)
        except (ValueError, TypeError):
            refused += 1
            continue
        accepted += 1
        decoded = frames.decode_control(body, "worker")
        assert decoded == _as_json_value(message)
        # And what the decoder accepts, the encoder accepts again.
        assert frames.encode_control(decoded) == body
    # The generator reaches both outcomes in volume.
    assert accepted > 500 and refused > 500


def test_a_plugin_result_the_peer_would_refuse_fails_that_request_only(session):
    for kind in ("deep", "duplicate_keys", "surrogate"):
        with pytest.raises(RequestFailed) as caught:
            session.request("unencodable_result", {"kind": kind}, timeout=5)
        assert caught.value.code == frames.Codes.PLUGIN_ERROR
        assert session.alive
    assert session.request("echo", {"after": True}, timeout=5).result["echo"] == {"after": True}


@pytest.mark.parametrize("kind", ["deep", "duplicate_keys", "surrogate"])
def test_a_host_call_result_the_worker_would_refuse_fails_that_call_only(make_session, kind):
    results = {
        "deep": {"deep": _deep(frames.MAX_JSON_DEPTH + 1)},
        "duplicate_keys": {"x": {1: "a", "1": "b"}},
        "surrogate": {"x": "\ud800"},
    }
    session = make_session(host_calls={"bad": lambda call: results[kind], "good": lambda call: {"ok": 1}})
    session.start()
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "bad"}, timeout=5)
    assert caught.value.code == frames.Codes.HOST_CALL_FAILED
    assert session.alive
    assert session.request("call", {"name": "good"}, timeout=5).result["ack"] == {"ok": 1}


@pytest.mark.parametrize("params", [
    {"deep": _deep(frames.MAX_JSON_DEPTH + 1)}, {1: "a", "1": "b"}, {"x": "\ud800"}])
def test_params_the_worker_would_refuse_are_refused_before_sending(make_session, params):
    session = make_session({"max_in_flight": 1})
    session.start()
    with pytest.raises((ValueError, TypeError)):
        session.submit("echo", params)
    assert session.in_flight == 0
    assert session.alive
    assert session.request("echo", {"fine": True}, timeout=5).result["echo"] == {"fine": True}


@pytest.mark.parametrize("kind", ["progress", "progress_data", "event", "call_params", "call_name"])
def test_a_plugin_sending_a_schema_breaking_message_fails_only_its_own_request(make_session, kind):
    session = make_session(host_calls={"lookup": lambda call: {}})
    session.start()
    process = session.process
    other = session.submit("sleep", {"seconds": 1.0})
    with pytest.raises(RequestFailed) as caught:
        session.request("bad_send", {"kind": kind}, timeout=5)
    assert caught.value.code == frames.Codes.PLUGIN_ERROR
    # The session, the worker and an unrelated request in flight are untouched.
    assert session.alive and session.process is process and process.poll() is None
    assert other.wait(8).result == {"slept": 1.0}
    assert session.request("echo", {"after": True}, timeout=5).result["echo"] == {"after": True}


@pytest.mark.parametrize("op, params, kwargs", [
    ("echo", [1], {}),
    ("echo", "text", {}),
    ("echo", {"ok": 1}, {"secrets": {"k": 5}}),
    (5, {}, {}),
    (None, {}, {}),
])
def test_a_request_the_worker_would_reject_is_refused_locally_and_others_survive(make_session, op, params, kwargs):
    session = make_session({"max_in_flight": 2})
    session.start()
    process = session.process
    other = session.submit("sleep", {"seconds": 0.5})
    with pytest.raises((ValueError, TypeError)):
        session.submit(op, params, **kwargs)
    assert session.in_flight == 1
    assert session.alive and process.poll() is None
    assert other.wait(8).result == {"slept": 0.5}
    assert session.request("echo", {"after": True}, timeout=5).result["echo"] == {"after": True}


def test_a_cancel_with_a_reason_that_is_not_text_still_cancels(session):
    handle = submit_running(session, {"seconds": 30})
    process = session.process
    handle.cancel(reason=5)
    with pytest.raises(RequestCancelled):
        handle.wait(8)
    assert session.alive and session.process is process and process.poll() is None
    assert session.request("echo", timeout=5).result["echo"] == {}


def test_a_huge_shutdown_grace_is_clamped_to_a_frame_the_worker_accepts(make_session):
    session = make_session()
    session.start()
    process = session.process
    session.close(grace=10 ** 7)
    # A shutdown frame the worker rejected would have ended it with status 3.
    assert process.returncode == 0


@pytest.mark.parametrize("reason", ["x" * 100_000, "bad \ud800 text", "\udfff", 5, None, ""])
def test_a_cancel_reason_that_cannot_be_sent_as_given_still_cancels(make_session, reason):
    # A long grace: if the cancel frame were dropped, only the watchdog's kill
    # (after 20 seconds) would end the request, and it would end the session.
    session = make_session(limits=fast_limits(cancel_grace=20.0))
    session.start()
    process = session.process
    handle = submit_running(session, {"seconds": 30})
    started = time.monotonic()
    handle.cancel(reason)
    with pytest.raises(RequestCancelled):
        handle.wait(8)
    assert time.monotonic() - started < 5
    assert session.alive and session.process is process and process.poll() is None
    assert session.request("echo", timeout=5).result["echo"] == {}


@pytest.mark.parametrize("pad", [150, 170, 185, 190, 195, 199, 200])
def test_a_secret_crossing_the_cancel_reason_limit_leaks_no_prefix(make_session, pad):
    session = make_session()
    session.start()
    sent = []
    real = session._enqueue_packed
    session._enqueue_packed = lambda data: sent.append(data) or real(data)
    handle = submit_running(session, {"seconds": 30}, secrets={"key": SECRET})
    # The secret starts `pad` characters in, so the 200 character cut lands inside it.
    handle.cancel("p" * pad + SECRET + " and a tail")
    with pytest.raises(RequestCancelled) as caught:
        handle.wait(8)
    frame = next(d for d in sent if b'"cancel"' in d)
    reason = frames.decode_control(frame[frames.HEADER.size:], "host")["reason"]
    assert len(reason) <= 200 and reason.startswith("p" * min(pad, 200))
    for text in (str(caught.value), reason, frame[frames.HEADER.size:].decode("utf-8")):
        for start in range(len(SECRET) - 5):
            assert SECRET[start:start + 6] not in text


def test_a_host_error_text_that_cannot_be_sent_still_answers_the_call(make_session):
    def broken(call):
        raise ValueError("bad \ud800 text")

    session = make_session(host_calls={"broken": broken})
    session.start()
    started = time.monotonic()
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "broken"}, timeout=8)
    # Answered at once, not after the worker's own 60 second call timeout.
    assert time.monotonic() - started < 5
    assert caught.value.code == frames.Codes.HOST_CALL_FAILED
    assert session.alive


_SCHEMA_SAMPLES = [None, True, 0, -1, 7, 1.5, float("nan"), "", "text", [], [1], {}, {"a": 1}, 10 ** 9, "\ud800"]


def _frame_fields(sender):
    names = (frames.FROM_WORKER if sender == "worker" else frames.FROM_HOST)
    for name in sorted(names):
        keys = set(frames._REQUIRED[name]) | set(frames._OPTIONAL.get(name, {}))
        yield name, sorted(keys | {"extra"})


@pytest.mark.parametrize("sender", ["host", "worker"])
def test_every_message_a_side_can_send_passes_the_other_sides_checks(sender):
    import random

    rng = random.Random(7 if sender == "host" else 11)
    accepted = refused = 0
    for name, keys in _frame_fields(sender):
        for _ in range(400):
            message = {"t": name}
            for key in keys:
                if rng.random() < 0.7:
                    message[key] = rng.choice(_SCHEMA_SAMPLES)
            try:
                body = frames.encode_control(message, sender)
            except (ValueError, TypeError):
                refused += 1
                continue
            accepted += 1
            # What leaves the sender is always something the receiver takes.
            assert frames.decode_control(body, sender) == message
    assert accepted > 100 and refused > 1000


def test_the_sender_check_names_the_problem_and_is_a_value_error():
    with pytest.raises(frames.UnencodableFrame) as caught:
        frames.encode_control({"t": "progress", "id": 1, "fraction": "x"}, "worker")
    assert isinstance(caught.value, ValueError)
    assert "fraction" in str(caught.value)
    with pytest.raises(frames.UnencodableFrame):
        frames.pack_control({"t": "req", "id": 1, "op": "x", "params": [1]}, sender="host")
    # A message that belongs to the other side is not this side's to send.
    with pytest.raises(frames.UnencodableFrame):
        frames.encode_control({"t": "req", "id": 1, "op": "x", "params": {}}, "worker")


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
    handle = submit_running(session, {"seconds": 30})
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
    handle = submit_running(session, {"seconds": 30, "ignore_cancel": True})
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
    neighbour = submit_running(session, {"seconds": 1.5})
    stuck = submit_running(session, {"seconds": 30, "ignore_cancel": True})
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
    handle = submit_running(session, {"seconds": 30})
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
    ready = tmp_path / "ready"
    body = """
    def on_term(signum, frame):
        open(%r, "w").write("term")
    signal.signal(signal.SIGTERM, on_term)
    open(%r, "w").write("ready")
    while True:
        out.write_control({"t": "hb", "seq": 1})
        time.sleep(0.05)
    """ % (str(marker), str(ready))
    session = hostile_session(tmp_path, body)
    session.start()
    process = session.process
    # The handler is installed once the script says so.
    assert wait_until(ready.exists, timeout=8)
    session.close(grace=0.3)
    assert marker.exists()
    assert process.poll() is not None


def test_deeply_nested_json_fails_the_session_instead_of_the_reader_thread(tmp_path):
    body = with_reader("""
    next_request()
    payload = b"[" * 500000
    sys.stdout.buffer.write(frames.HEADER.pack(frames.FRAME_CONTROL, len(payload)) + payload)
    sys.stdout.buffer.flush()
    time.sleep(30)
    """)
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

    entered = threading.Event()

    def slow(call):
        entered.set()
        release.wait(5)
        return {"slow": True}

    session = make_session(host_calls={"slow": slow, "quick": lambda call: {"quick": True}})
    session.start()
    slow_handle = session.submit("call", {"name": "slow"})
    assert entered.wait(5)
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
    received = []
    real_on_call = session._on_call

    def counting(message):
        real_on_call(message)
        received.append(message["name"])

    session._on_call = counting
    handle = session.submit("parallel_calls", {"names": ["first", "second"]}, timeout=10)
    # The host has taken in both calls (the worker sends the second only after the
    # first is on the wire), and the first handler is still running.
    assert wait_until(lambda: received == ["first", "second"])
    assert wait_until(lambda: "first start" in log)
    # The second call is queued behind the first, not run.
    assert len(session._call_queues[handle._request.id]) == 1
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


def test_chunked_result_with_the_right_counts_but_a_wrong_checksum_is_a_protocol_error(session):
    with pytest.raises(ProtocolViolation) as caught:
        session.request("chunks", {"size": 1000, "bad_sha": True}, timeout=5)
    assert "checksum" in str(caught.value)


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('"bytes": 4, "chunks": 1', "declared 4 bytes"),
        ('"bytes": 3, "chunks": 2', "declared 2 chunks"),
        ('"chunks": 1', "without a byte count"),
    ],
)
def test_result_data_that_disagrees_with_its_reply_is_a_protocol_error(tmp_path, reply, expected):
    body = with_reader("""
    rid = next_request()
    out.write_data(rid, b"abc")
    sha = __import__("hashlib").sha256(b"abc").hexdigest()
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {}, "sha256": sha, %s})
    time.sleep(30)
    """ % reply)
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(ProtocolViolation) as caught:
            session.submit("x").wait(8)
    finally:
        session.close(grace=0.3)
    assert expected in str(caught.value)


def test_a_data_frame_shorter_than_its_operation_id_is_malformed():
    with pytest.raises(frames.MalformedFrame):
        frames.split_data(b"\x00" * (frames.DATA_PREFIX.size - 1))
    assert frames.split_data(frames.DATA_PREFIX.pack(7) + b"ab") == (7, b"ab")


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
    # Each reply waits until the host's handler for that request's call has
    # started, so the call is running (and counted) before its request ends.
    body = """
    while True:
        rid = next_request()
        out.write_control({"t": "call", "cid": rid, "id": rid, "name": "stuck", "params": {}})
        flag = os.path.join(%r, "started-%%d" %% rid)
        waited = 0.0
        while not os.path.exists(flag) and waited < 20:
            time.sleep(0.01)
            waited += 0.01
        out.write_control({"t": "res", "id": rid, "ok": True, "result": {}})
    """ % str(tmp_path)
    release = threading.Event()
    session = hostile_session(tmp_path, with_reader(body), max_pending_calls=4)

    def stuck(call):
        (tmp_path / ("started-%d" % call.request_id)).write_text("1")
        return release.wait(10) and {}

    session.register_host_call("stuck", stuck)
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
    entered = threading.Event()

    def slow(call):
        entered.set()
        return release.wait(10) and {}

    session = make_session(host_calls={"slow": slow})
    session.start()
    try:
        handle = session.submit("call", {"name": "slow"})
        assert entered.wait(5)
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


def test_a_call_queued_behind_a_slow_one_is_refused_once_its_request_has_finished(tmp_path):
    marker = tmp_path / "acks.json"
    started_file = tmp_path / "slow-started"
    body = with_reader("""
    rid = next_request()
    out.write_control({"t": "call", "cid": 1, "id": rid, "name": "slow", "params": {}})
    # Nothing else is sent until the host says its handler is running.
    while not os.path.exists(%r):
        time.sleep(0.01)
    out.write_control({"t": "call", "cid": 2, "id": rid, "name": "checkpoint", "params": {}})
    out.write_control({"t": "call", "cid": 3, "id": rid, "name": "checkpoint", "params": {}})
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {}})
    acks = {}
    while len(acks) < 3:
        frame = inp.read_frame()
        if frame is None:
            sys.exit(0)
        if frame[0] == frames.FRAME_CONTROL:
            message = frames.decode_control(frame[1], "host")
            if message["t"] == "ack":
                acks[message["cid"]] = message
    open(%r, "w").write(__import__("json").dumps(acks))
    time.sleep(30)
    """ % (str(started_file), str(marker)))
    release = threading.Event()
    started = threading.Event()
    ran = []

    def slow(call):
        started_file.write_text("1")
        started.set()
        release.wait(10)
        return {"slow": True}

    session = hostile_session(tmp_path, body)
    session.register_host_call("slow", slow)
    session.register_host_call("checkpoint", lambda call: ran.append(call.request_id) or {})
    try:
        session.start()
        handle = session.submit("x")
        assert started.wait(8)
        # The terminal result arrives while the slow handler still runs.
        assert handle.wait(8).result == {}
        assert not ran
        release.set()
        assert wait_until(marker.exists, timeout=8)
        acks = json.loads(marker.read_text())
        assert acks["1"]["ok"] is True
        for cid in ("2", "3"):
            assert acks[cid]["ok"] is False
            assert acks[cid]["error"]["code"] == frames.Codes.INVALID_REQUEST
        assert ran == []
        # Nothing stays counted against the cap and no queue is left behind.
        assert wait_until(lambda: session._call_pending == 0 and not session._call_queues)
        assert session.alive
    finally:
        release.set()
        session.close(grace=0.3)


def test_a_call_pipelined_behind_a_rejected_hello_never_runs(tmp_path):
    script = tmp_path / "pipelined.py"
    script.write_text(textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        import frames
        out = frames.FrameWriter(sys.stdout.buffer)
        # An incompatible hello and a host call with no request, written back to back.
        out.write_control({"t": "hello", "protocols": [{"major": 99, "minor": 0}], "kinds": []})
        out.write_control({"t": "call", "cid": 1, "name": "poke", "params": {}})
        time.sleep(30)
    """) % str(RUNTIME_DIR), encoding="utf-8")
    ran = []
    session = WorkerSession([sys.executable, "-B", str(script)], limits=fast_limits(heartbeat_timeout=30.0))
    session.register_host_call("poke", lambda call: ran.append(call.name) or {})
    try:
        with pytest.raises(HandshakeError) as caught:
            session.start()
        assert caught.value.code == frames.Codes.VERSION_MISMATCH
        # The reader is finished (start() joined it): any handler thread would exist by now.
        assert wait_until(lambda: not [t for t in threading.enumerate() if "-calls-" in t.name])
        assert ran == []
        assert session._call_pending == 0 and not session._call_queues
    finally:
        session.close(grace=0.3)


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
        assert wait_until(lambda: harness.channel._acks_closed)
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


def test_the_goodbye_is_bounded_when_the_output_is_stuck():
    harness = ChannelHarness(lambda operation: {}, heartbeat_interval=30)
    harness.start()
    release = threading.Event()
    real = harness.channel._writer.write_packed

    def stuck(data):
        release.wait(20)
        return real(data)

    try:
        harness.channel._writer.write_packed = stuck
        harness.host_out.write_control({"t": "shutdown", "grace_ms": 500})
        # The goodbye cannot be written; the drain does not wait on it forever.
        harness.thread.join(4)
        assert not harness.thread.is_alive()
    finally:
        release.set()
        harness.finish()


def test_run_releases_its_streams_and_reader_while_the_peer_keeps_its_end_open():
    harness = ChannelHarness(lambda operation: {}, heartbeat_interval=0.05)
    harness.start()
    channel = harness.channel
    assert wait_until(lambda: channel._reader_thread is not None and channel._reader_thread.is_alive())
    reader = channel._reader_thread
    try:
        harness.host_out.write_control({"t": "shutdown"})
        harness.thread.join(5)
        assert not harness.thread.is_alive()
        # harness.to_worker is still open here: the host has not hung up.
        assert not harness.to_worker.closed
        assert harness.status == [0]
        assert not reader.is_alive()
        assert channel._input._stream.closed
        assert channel._writer._stream.closed
        assert wait_until(lambda: not channel._heartbeat_thread.is_alive())
    finally:
        harness.finish()


class _NoSelect:
    """Stands in for the ``select`` module on a platform where it cannot wait on pipes."""

    calls = 0

    EVENT_READ = 1

    @classmethod
    def DefaultSelector(cls, *args, **kwargs):
        cls.calls += 1
        raise OSError("selectors only work on sockets here")


@pytest.fixture
def no_pipe_select(monkeypatch):
    _NoSelect.calls = 0
    monkeypatch.setattr(worker_channel, "_select_on_pipes", lambda: False)
    monkeypatch.setattr(worker_channel, "selectors", _NoSelect)
    return _NoSelect


def test_the_channel_serves_a_request_and_shuts_down_where_select_cannot_wait_on_pipes(no_pipe_select):
    harness = ChannelHarness(lambda operation: {"echo": operation.params}, heartbeat_interval=0.05)
    harness.start()
    channel = harness.channel
    try:
        assert channel._input.interruptible is False
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {"a": 1}})
        assert wait_until(lambda: any(m["t"] == "res" for m in harness.messages))
        reply = next(m for m in harness.messages if m["t"] == "res")
        assert reply["ok"] is True and reply["result"] == {"echo": {"a": 1}}
        harness.host_out.write_control({"t": "shutdown"})
        # The host keeps its end open and the reader cannot be woken: run() still returns.
        harness.thread.join(5)
        assert not harness.thread.is_alive()
        assert harness.status == [0]
        assert any(m["t"] == "bye" for m in harness.messages)
        assert channel._writer._stream.closed
        assert wait_until(lambda: channel._input._stream.closed)
        assert no_pipe_select.calls == 0
    finally:
        harness.finish()


def test_a_failed_handshake_returns_where_select_cannot_wait_on_pipes(no_pipe_select):
    harness = ChannelHarness(lambda operation: {})
    harness.host_out.write_control({"t": "reject", "code": "version_mismatch", "message": "no"})
    harness.thread.start()
    try:
        harness.thread.join(5)
        assert harness.status == [3]
        assert no_pipe_select.calls == 0
    finally:
        harness.finish()


def test_the_plain_input_reads_without_ever_calling_select(no_pipe_select):
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    wake = worker_channel._WakeableInput(stream)
    try:
        assert wake.interruptible is False
        os.write(write_fd, b"abc")
        assert wake.read(10) == b"abc"
        wake.interrupt()  # nothing to wake, and no error
        os.close(write_fd)
        write_fd = None
        assert wake.read(10) == b""
        assert no_pipe_select.calls == 0
    finally:
        wake.close()
        if write_fd is not None:
            os.close(write_fd)


def _high_fd(fd, floor=1100):
    """A copy of ``fd`` numbered at or above ``floor`` (select.select refuses 1024 and up)."""
    import fcntl

    high = fcntl.fcntl(fd, fcntl.F_DUPFD, floor)
    os.close(fd)
    return high


@pytest.mark.skipif(os.name != "posix", reason="descriptor numbers and rlimits are POSIX")
def test_the_wakeable_input_works_with_descriptors_past_the_select_limit(monkeypatch):
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1300
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip("cannot raise the descriptor limit far enough")
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    try:
        read_fd, write_fd = os.pipe()
        stream = io.FileIO(_high_fd(read_fd), "r", closefd=True)
        real_pipe = os.pipe

        def high_pipe():
            r, w = real_pipe()
            return _high_fd(r), _high_fd(w, 1200)

        with monkeypatch.context() as patched:
            patched.setattr(os, "pipe", high_pipe)
            wake = worker_channel._WakeableInput(stream)
        assert wake.interruptible
        assert stream.fileno() > 1024 and wake._wake_r > 1024 and wake._wake_w > 1024
        try:
            os.write(write_fd, b"abc")
            assert wake.read(10) == b"abc"
            got = []
            reader = threading.Thread(target=lambda: got.append(wake.read(10)), daemon=True)
            reader.start()
            time.sleep(0.2)
            assert reader.is_alive()
            wake.interrupt()
            reader.join(3)
            assert got == [b""]
        finally:
            wake.close()
            os.close(write_fd)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_a_group_is_signalled_once_and_only_while_its_leader_is_unreaped(make_session, monkeypatch):
    session = make_session()
    session.start()
    process = session.process
    calls = []
    real = os.killpg

    def recording(pgid, sig):
        # The leader's status must not have been collected yet, or its number is free to reuse.
        calls.append((pgid, sig, process.returncode))
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording)
    session.close(grace=2.0)
    assert process.returncode is not None
    assert len(calls) >= 1 and all(returncode is None for _, _, returncode in calls)
    first = len(calls)
    # The leader is reaped and its group number may belong to someone else now.
    session.close(grace=2.0)
    session._kill_tree(process)
    session._fail(SessionClosed("again"), kill=True)
    assert session._signal_leader_group(process, signal.SIGTERM) is True
    assert len(calls) == first


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_concurrent_closes_signal_the_group_only_once(make_session, monkeypatch):
    session = make_session()
    session.start()
    calls = []
    real = os.killpg

    def recording(pgid, sig):
        calls.append(sig)
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording)
    threads = [threading.Thread(target=session.close, kwargs={"grace": 2.0}) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    assert session.process.returncode is not None
    assert calls.count(signal.SIGKILL) == 1


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
@pytest.mark.parametrize("reaper", ["popen", "waitpid"])
def test_a_worker_reaped_by_someone_else_is_never_signalled(make_session, monkeypatch, reaper):
    session = make_session()
    session.start()
    process = session.process
    # Hold the session's own reader back, so the outside reaper is the first to collect the worker.
    go = threading.Event()
    real_reap = session._reap_exit_code
    session._reap_exit_code = lambda: go.wait(10) and real_reap()
    os.killpg(process.pid, signal.SIGKILL)
    if reaper == "popen":
        process.wait()
    else:
        # A bare waitpid: Popen is not told, so returncode stays None.
        os.waitpid(process.pid, 0)
        assert process.returncode is None
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    go.set()
    session.close(grace=0.5)
    session._kill_tree(process)
    session._fail(SessionClosed("again"), kill=True)
    assert session._signal_leader_group(process, signal.SIGTERM) is True
    assert calls == []
    assert session._group_lost or process.returncode is not None


class _ReapingProcess:
    """A worker handle whose ``poll()`` collects the child, as ``Popen.poll`` does."""

    args = ["worker"]

    def __init__(self):
        # A pid that exists (this process), so the fallback probe sees "maybe alive".
        self.pid = os.getpid()
        self.returncode = None
        self.polled = 0

    def poll(self):
        self.polled += 1
        self.returncode = 0
        return 0

    def wait(self, timeout=None):
        return self.poll()


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
@pytest.mark.parametrize("how", ["no-waitid", "waitid-fails"])
def test_the_group_is_signalled_before_any_poll_that_could_reap_the_leader(monkeypatch, how):
    session = WorkerSession(["worker"])
    process = _ReapingProcess()
    session._process_mark = session_module._start_mark(process.pid)  # what a spawn records
    if how == "no-waitid":
        monkeypatch.delattr(os, "waitid", raising=False)
    else:
        def failing(*args, **kwargs):
            raise OSError(22, "invalid argument")

        monkeypatch.setattr(os, "waitid", failing, raising=False)
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig, process.returncode)))
    session._end_group(process)
    # Signalled while the leader was still unreaped, and nothing polled first.
    assert calls == [(process.pid, signal.SIGKILL, None)]
    assert process.polled == 0
    # Once reaped, the group is never signalled again.
    process.poll()
    session._kill_tree(process)
    assert len(calls) == 1


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
@pytest.mark.parametrize("ending", ["close", "worker-exits"])
@pytest.mark.parametrize("how", ["no-waitid", "waitid-fails"])
def test_a_surviving_descendant_is_killed_when_the_exit_is_found_without_waitid(make_session, monkeypatch, how, ending):
    if how == "no-waitid":
        monkeypatch.delattr(os, "waitid", raising=False)
    else:
        def failing(*args, **kwargs):
            raise OSError(22, "invalid argument")

        monkeypatch.setattr(os, "waitid", failing, raising=False)
    session = make_session()
    session.start()
    process = session.process
    child = session.request("fork", {"inherit": True}, timeout=5).result["child"]
    assert pid_alive(child)
    if ending == "close":
        session.close(grace=2.0)
    else:
        # The worker dies by itself; its descendant outlives it.
        with pytest.raises(WorkerExited):
            session.request("exit", timeout=8)
        session.close(grace=1.0)
    # The leader stayed a zombie until the group was ended, so the group was ours to kill.
    assert wait_until(lambda: not pid_alive(child), timeout=5)
    assert process.returncode is not None
    assert not session._group_lost
    # Nothing is left blocked on the pipes the descendant held.
    ours = list(session._threads)
    assert ours
    assert wait_until(lambda: not [t.name for t in ours if t.is_alive()]), [t.name for t in ours if t.is_alive()]


def test_a_healthy_worker_is_not_probed_with_signal_zero_where_that_would_kill_it(make_session, monkeypatch):
    session = make_session()
    session.start()
    process = session.process
    # What Windows looks like to the session: no waitid, and os.kill is TerminateProcess.
    monkeypatch.setattr(session_module, "_is_posix", lambda: False)
    monkeypatch.delattr(os, "waitid", raising=False)
    kills = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)) if sig == 0 else real_kill(pid, sig))
    assert session.alive
    assert session._exited(process) is False
    assert session._wait_exit(process, 0.1) is False
    with session._group_lock:
        session._ours(process)
    assert session.alive
    assert kills == []
    assert process.returncode is None
    monkeypatch.undo()
    assert pid_alive(process.pid)
    assert session.request("echo", {"still": True}, timeout=5).result["echo"] == {"still": True}


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_close_reaps_the_worker_where_a_zombie_cannot_be_told_from_a_live_process(make_session, monkeypatch):
    # No waitid, and no /proc: kill(pid, 0) answers yes for a zombie, and for a stranger that took
    # the pid, so nothing can vouch that the pid is still the child's.
    monkeypatch.delattr(os, "waitid", raising=False)
    monkeypatch.setattr(session_module, "_has_proc", lambda: False)
    session = make_session(limits=fast_limits(kill_grace=3.0, term_grace=0.3))
    session.start()
    process = session.process
    child = session.request("fork", {"inherit": True}, timeout=5).result["child"]
    try:
        started = time.monotonic()
        session.close(grace=0.5)
        assert time.monotonic() - started < 8
        # The worker itself is ended and reaped, the way Popen does it...
        assert process.returncode is not None
        # ...but its group is not signalled on a pid nobody can vouch for, so what it forked
        # is not known to be gone: the teardown reads as unconfirmed.
        assert session.teardown_confirmed is False
        assert session._group_lost is True
    finally:
        monkeypatch.undo()
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
    assert wait_until(lambda: not pid_alive(child), timeout=5)
    ours = list(session._threads)
    assert wait_until(lambda: not [t.name for t in ours if t.is_alive()])


def test_run_returns_and_its_threads_leave_while_a_write_is_blocked_on_a_full_pipe():
    to_worker_r, to_worker_w = os.pipe()
    from_worker_r, from_worker_w = os.pipe()
    channel = worker_channel.WorkerChannel(
        lambda operation: worker_channel.OperationResult({}, b"x" * (4 * 1024 * 1024)),
        kinds=[{"kind": "translator", "major": 1, "minor": 0}],
        stdin=os.fdopen(to_worker_r, "rb", buffering=0), stdout=os.fdopen(from_worker_w, "wb"),
        redirect_stdout=False, heartbeat_interval=30)
    host_in = os.fdopen(to_worker_w, "wb")
    host_out = frames.FrameWriter(host_in)
    from_worker = os.fdopen(from_worker_r, "rb")
    status = []
    runner = threading.Thread(target=lambda: status.append(channel.run()), daemon=True)
    try:
        host_out.write_control({"t": "welcome", "protocol": {"major": 2, "minor": 0}, "kind": None,
                                "limits": {"heartbeat_interval_ms": 30000}, "session": "t"})
        runner.start()
        # Read the hello and then never read again: the pipe fills up.
        hello = frames.FrameReader(from_worker).read_frame()
        assert frames.decode_control(hello[1], "worker")["t"] == "hello"
        host_out.write_control({"t": "req", "id": 1, "op": "big", "params": {}})
        assert wait_until(lambda: 1 in channel._transmitting)
        # The result streams until the pipe is full, then the writer blocks holding its lock.
        assert wait_until(lambda: channel._writer._lock.locked())
        time.sleep(0.5)
        assert channel._writer._lock.locked()
        workers = [t for t in list(channel._threads) if t.is_alive()]
        assert workers
        host_out.write_control({"t": "shutdown", "grace_ms": 200})
        started = time.monotonic()
        runner.join(8)
        assert not runner.is_alive()
        assert time.monotonic() - started < 6
        # The blocked write was ended, so the thread that was in it is gone, and so is the stream.
        assert wait_until(lambda: not any(t.is_alive() for t in workers), timeout=4)
        assert channel._output.closed
        assert not channel._writer._lock.locked()
    finally:
        for stream in (host_in, from_worker):
            try:
                stream.close()
            except OSError:
                pass


def test_the_wakeable_input_is_used_on_posix():
    assert worker_channel._select_on_pipes() is (os.name == "posix")


def test_a_failed_handshake_also_releases_the_streams():
    harness = ChannelHarness(lambda operation: {})
    harness.host_out.write_control({"t": "reject", "code": "version_mismatch", "message": "no"})
    harness.thread.start()
    try:
        harness.thread.join(5)
        assert not harness.thread.is_alive()
        assert harness.status == [3]
        assert harness.channel._input._stream.closed
        assert harness.channel._writer._stream.closed
        assert not harness.to_worker.closed
    finally:
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

    def __init__(self, hold=False, **kwargs):
        self.written = []
        self.first_chunk = threading.Event()
        # With ``hold`` the stream stops after its first chunk until ``release`` is set.
        self.release = threading.Event()
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
                if hold and len(self.written) == 1:
                    self.release.wait(20)
                else:
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
    worker = _ChunkedWorker(hold=True, max_in_flight=1)
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
        # The first stream was held the whole time: it is still on the wire.
        assert 1 in harness.channel._transmitting
        assert not any(m["t"] == "res" and m["id"] == 1 and m["ok"] for m in harness.messages)
    finally:
        worker.release.set()
        harness.finish()


def test_a_session_cancels_a_streaming_result_inside_the_grace(tmp_path):
    session = WorkerSession(
        fake_worker_command(), env=fake_worker_env(), kind="translator",
        limits=fast_limits(cancel_grace=5.0))
    session.start()
    received = []
    real_on_data = session._on_data

    def counting(op_id, chunk):
        received.append(len(chunk))
        return real_on_data(op_id, chunk)

    session._on_data = counting
    paused = tmp_path / "first-chunk"
    try:
        process = session.process
        handle = session.submit(
            "chunks", {"size": frames.DEFAULT_CHUNK_BYTES * 3, "pause_file": str(paused)}, timeout=30)
        # The worker has sent one chunk and waits for a cancel before the next.
        assert wait_until(paused.exists, timeout=8)
        handle.cancel("enough")
        with pytest.raises(RequestCancelled):
            handle.wait(8)
        # The transfer stopped early, by a cancelled reply and not by ending the worker.
        assert len(received) < 3
        assert session.alive and session.process is process and process.poll() is None
        assert session.request("echo", timeout=5).result["echo"] == {}
    finally:
        session.close(grace=1.0)


def test_shutdown_drains_for_the_graced_time_the_host_asked_for():
    # The host asked for ten seconds; the channel's own default is five.
    usage = []

    def handler(operation):
        operation.cancelled.wait(10)
        time.sleep(6)
        usage.append(operation.call("usage", {}, timeout=10, cancellable=False))
        return {}

    harness = ChannelHarness(handler)
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}})
        assert wait_until(lambda: 1 in harness.channel._operations)
        harness.host_out.write_control({"t": "shutdown", "grace_ms": 10_000})
        assert wait_until(lambda: any(m["t"] == "call" for m in harness.messages), timeout=12)
        call = next(m for m in harness.messages if m["t"] == "call")
        harness.host_out.write_control({"t": "ack", "cid": call["cid"], "ok": True, "result": {"settled": True}})
        assert wait_until(lambda: any(m["t"] == "bye" for m in harness.messages), timeout=8)
        assert usage == [{"settled": True}]
        reply = next(m for m in harness.messages if m["t"] == "res")
        assert reply["id"] == 1 and reply["ok"] is True
        assert harness.messages.index(reply) < next(
            i for i, m in enumerate(harness.messages) if m["t"] == "bye")
    finally:
        harness.finish()


def test_shutdown_without_a_grace_uses_the_channels_own_budget():
    gate = threading.Event()
    harness = ChannelHarness(lambda operation: gate.wait(10) and {}, wait_grace=0.3)
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}})
        assert wait_until(lambda: 1 in harness.channel._operations)
        harness.host_out.write_control({"t": "shutdown"})
        # The handler never finishes: the drain gives up after its own 0.3 seconds
        # and, with the budget spent, does not write a goodbye.
        harness.thread.join(3)
        assert not harness.thread.is_alive()
        assert not any(m["t"] == "bye" for m in harness.messages)
    finally:
        gate.set()
        harness.finish()


@pytest.mark.parametrize("grace_ms", [-1, True, "5000", 1.5, frames.MAX_SHUTDOWN_GRACE_MS + 1])
def test_a_shutdown_grace_outside_its_bounds_is_malformed(grace_ms):
    with pytest.raises(frames.MalformedFrame):
        frames.validate_message({"t": "shutdown", "grace_ms": grace_ms}, "host")
    for fine in (0, 1, frames.MAX_SHUTDOWN_GRACE_MS):
        frames.validate_message({"t": "shutdown", "grace_ms": fine}, "host")
    frames.validate_message({"t": "shutdown"}, "host")


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


# --------------------------------------- secret contract and bounded worker fields

BAD_SECRETS = [
    pytest.param({"pin": 123456}, id="number"),
    pytest.param({"pin": True}, id="bool"),
    pytest.param({"pin": {"inner": "sk-nested"}}, id="nested-object"),
    pytest.param({"pin": ["sk-listed"]}, id="list"),
    pytest.param({"pin": ""}, id="empty"),
    pytest.param({"pin": None}, id="null"),
    pytest.param({"pin": b"sk-bytes"}, id="bytes"),
    pytest.param({1: "sk-numbered-name"}, id="non-text-name"),
    pytest.param(["sk-in-a-list"], id="not-a-mapping"),
    pytest.param("sk-bare-text", id="bare-text"),
]


@pytest.mark.parametrize("secrets", BAD_SECRETS)
def test_submit_refuses_secrets_that_are_not_a_flat_text_mapping(session, secrets):
    with pytest.raises(ValueError, match="secrets must be"):
        session.submit("echo", secrets=secrets)
    # Nothing was sent or registered: no slot is held and the worker is fine.
    assert not session._requests and not session._secret_values
    assert session.request("echo", {"a": 1}, timeout=5).result


@pytest.mark.parametrize("secrets", BAD_SECRETS)
def test_frame_validation_refuses_secrets_that_are_not_a_flat_text_mapping(secrets):
    message = {"t": frames.REQ, "id": 1, "op": "x", "params": {}, "secrets": secrets}
    # The worker validates what it reads from the host with this same call.
    with pytest.raises(frames.MalformedFrame):
        frames.validate_message(message, "host")


def test_frame_validation_accepts_flat_text_secrets_and_none():
    for secrets in ({}, {"a": "x", "b": "line1\nline2"}, None):
        message = {"t": frames.REQ, "id": 1, "op": "x", "params": {}, "secrets": secrets}
        assert frames.decode_control(json.dumps(message).encode(), "host")["secrets"] == secrets


def test_a_secret_that_is_not_text_never_reaches_the_worker_unredacted(session):
    with pytest.raises(ValueError):
        session.submit("raw_error", secrets={"pin": 123456})
    with pytest.raises(RequestFailed) as caught:
        session.request("raw_error", secrets={"pin": "123456"}, timeout=5)
    assert "123456" not in str(caught.value) and "123456" not in caught.value.traceback


OVERSIZED_ERROR = """
rid = next_request()
big = "m" * 100000
out.write_control({"t": "res", "id": rid, "ok": False, "error": {
    "code": "plugin_error",
    "message": "m" * (%(message)d - 8) + SECRET + big,
    "class_name": "C" * 100000,
    "traceback": big + SECRET + "t" * 100000,
}})
time.sleep(30)
"""


def test_a_raw_oversized_error_reply_is_cut_after_redaction(tmp_path):
    secret = "sk-oversize-0123456789"
    body = with_reader("SECRET = %r" % secret + OVERSIZED_ERROR % {"message": session_module._MESSAGE_LIMIT})
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(RequestFailed) as caught:
            session.submit("x", secrets={"key": secret}).wait(8)
        error = caught.value
        assert len(str(error)) <= session_module._MESSAGE_LIMIT
        assert len(error.class_name) <= session_module._CLASS_NAME_LIMIT
        assert len(error.traceback) <= session_module._TRACEBACK_LIMIT
        for text in (str(error), error.class_name, error.traceback):
            assert secret not in text and secret[:8] not in text and secret[-8:] not in text
    finally:
        session.close(grace=0.3)


def test_a_secret_straddling_the_cut_is_replaced_whole(tmp_path):
    secret = "sk-straddle-0123456789"
    cut = session_module._MESSAGE_LIMIT
    body = with_reader("""
    SECRET = %r
    rid = next_request()
    out.write_control({"t": "res", "id": rid, "ok": False, "error": {
        "code": "plugin_error", "message": "m" * (%d - 5) + SECRET + "tail"}})
    time.sleep(30)
    """ % (secret, cut))
    session = hostile_session(tmp_path, body)
    try:
        session.start()
        with pytest.raises(RequestFailed) as caught:
            session.submit("x", secrets={"key": secret}).wait(8)
        text = str(caught.value)
        assert len(text) <= cut
        assert "sk-str" not in text and "0123456789" not in text
    finally:
        session.close(grace=0.3)


def test_progress_heartbeat_and_sandbox_fields_are_bounded(tmp_path):
    body = with_reader("""
    rid = next_request()
    big = "p" * 200000
    deep = "bottom"
    for _ in range(40):
        deep = [deep]
    out.write_control({"t": "hb", "seq": 1, "extra": big, big[:50000]: "k",
                       "many": list(range(5000))})
    out.write_control({"t": "sandbox_blocked", "target": big, "id": rid})
    out.write_control({"t": "progress", "id": rid, "message": big, "data": {"deep": deep,
                       "items": {str(i): i for i in range(5000)}}})
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {}})
    time.sleep(30)
    """)
    events, seen = [], []
    session = hostile_session(tmp_path, body)
    session._on_event = lambda name, payload: events.append((name, payload))
    try:
        session.start()
        session.submit("x", on_progress=seen.append).wait(8)
        assert wait_until(lambda: session.last_heartbeat is not None, timeout=5)
        beat = session.last_heartbeat
        assert len(beat["extra"]) <= session_module._FIELD_LIMIT
        assert len(beat["many"]) <= session_module._ITEM_LIMIT
        assert all(len(key) <= session_module._FIELD_LIMIT for key in beat)
        blocked = [payload for name, payload in events if name == "sandbox_blocked"]
        assert blocked and len(blocked[0]["target"]) <= session_module._FIELD_LIMIT
        progress = seen[0]
        assert len(progress["message"]) <= session_module._FIELD_LIMIT
        assert len(progress["data"]["items"]) <= session_module._ITEM_LIMIT
        node, levels = progress["data"]["deep"], 0
        while isinstance(node, list):
            node, levels = node[0], levels + 1
        assert node is None and levels < 40
    finally:
        session.close(grace=0.3)


def test_the_hello_description_is_bounded(tmp_path):
    script = tmp_path / "bighello.py"
    script.write_text(textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        import frames
        out = frames.FrameWriter(sys.stdout.buffer)
        inp = frames.FrameReader(sys.stdin.buffer)
        out.write_control({"t": "hello", "protocols": [{"major": 2, "minor": 0}], "kinds": [],
                           "worker": {"name": "w" * 20000, "list": list(range(2000))}})
        inp.read_frame()
        time.sleep(30)
    """) % str(RUNTIME_DIR), encoding="utf-8")
    session = WorkerSession([sys.executable, "-B", str(script)], limits=fast_limits(heartbeat_timeout=30.0))
    try:
        welcome = session.start()
        assert len(welcome.worker["name"]) <= session_module._FIELD_LIMIT
        assert len(welcome.worker["list"]) <= session_module._ITEM_LIMIT
    finally:
        session.close(grace=0.3)


# ------------------------------- escaped secrets and exits that skip an exception clause

ESCAPED_SECRETS = [
    pytest.param("pa\\ss\\\\wd-9", id="backslash"),
    pytest.param("pa\tss\twd-9", id="tab"),
    pytest.param("pass'word\"-9", id="quotes"),
    pytest.param("pässwörd✓-9", id="non-ascii"),
    pytest.param("p/a?s=s&w+d#-9 %x", id="url-reserved"),
    pytest.param("first-line-9\nsecond-line-9", id="newline"),
]


def spellings(secret):
    """How an error text may spell a secret, worked out here and not by the code under test."""
    import urllib.parse

    return {
        secret,
        repr(secret)[1:-1],
        ascii(secret)[1:-1],
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret),
    }


def spelled_in(secret, texts):
    return sorted(form for form in spellings(secret) if form and any(form in text for text in texts))


def raisers(secret):
    import urllib.parse

    return {
        "keyerror": lambda: KeyError(secret),
        "repr": lambda: ValueError(repr(secret)),
        "fstring": lambda: ValueError(f"cannot use {secret!r} here"),
        "ascii": lambda: ValueError(ascii(secret)),
        "json": lambda: ValueError(json.dumps({"key": secret})),
        "url": lambda: ValueError("GET /x?k=" + urllib.parse.quote(secret, safe="") + " failed"),
        "url-plus": lambda: ValueError("GET /x?k=" + urllib.parse.quote_plus(secret) + " failed"),
    }


@pytest.mark.parametrize("secret", ESCAPED_SECRETS)
@pytest.mark.parametrize("how", sorted(raisers("x")))
def test_a_worker_error_reply_hides_every_escaped_spelling_of_a_secret(secret, how):
    def handler(operation):
        raise raisers(operation.secrets["key"])[how]()

    harness = ChannelHarness(handler)
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}, "secrets": {"key": secret}})
        assert wait_until(lambda: any(m["t"] == "res" for m in harness.messages))
        error = next(m for m in harness.messages if m["t"] == "res")["error"]
        assert error["code"] == frames.Codes.PLUGIN_ERROR
        texts = [error["message"], error.get("traceback", ""), error.get("class_name", "")]
        assert spelled_in(secret, texts) == []
    finally:
        harness.finish()


@pytest.mark.parametrize("secret", ESCAPED_SECRETS)
def test_a_secret_spelled_out_in_a_worker_error_reply_is_cut_before_the_limit(secret):
    def handler(operation):
        raise KeyError(operation.secrets["key"] + "B" * 5000)

    harness = ChannelHarness(handler)
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}, "secrets": {"key": secret}})
        assert wait_until(lambda: any(m["t"] == "res" for m in harness.messages))
        error = next(m for m in harness.messages if m["t"] == "res")["error"]
        assert len(error["message"]) <= 4096
        assert spelled_in(secret, [error["message"], error["traceback"]]) == []
    finally:
        harness.finish()


def escaped_error_script(secret):
    return with_reader("""
    import json, urllib.parse
    SECRET = %r
    rid = next_request()
    spelled = [str(KeyError(SECRET)), repr(SECRET), ascii(SECRET), json.dumps({"k": SECRET}),
               urllib.parse.quote(SECRET, safe=""), urllib.parse.quote_plus(SECRET)]
    out.write_control({"t": "res", "id": rid, "ok": False, "error": {
        "code": "plugin_error", "message": " | ".join(spelled), "class_name": spelled[0],
        "traceback": "\\n".join(spelled)}})
    time.sleep(30)
    """ % secret)


@pytest.mark.parametrize("secret", ESCAPED_SECRETS)
def test_the_host_hides_every_escaped_spelling_of_a_secret_in_a_worker_error(tmp_path, secret):
    session = hostile_session(tmp_path, escaped_error_script(secret))
    try:
        session.start()
        with pytest.raises(RequestFailed) as caught:
            session.submit("x", secrets={"key": secret}).wait(8)
        error = caught.value
        assert spelled_in(secret, [str(error), error.class_name, error.traceback]) == []
    finally:
        session.close(grace=0.3)


def test_the_host_hides_an_escaped_secret_in_progress_and_stderr(tmp_path):
    secret = "pa\\ss\twd'9"
    body = with_reader("""
    SECRET = %r
    rid = next_request()
    print(repr(SECRET), file=sys.stderr, flush=True)
    out.write_control({"t": "progress", "id": rid, "message": repr(SECRET), "data": {"k": [ascii(SECRET)]}})
    out.write_control({"t": "res", "id": rid, "ok": True, "result": {}})
    time.sleep(30)
    """ % secret)
    lines, seen = [], []
    session = hostile_session(tmp_path, body)
    session._on_stderr = lines.append
    try:
        session.start()
        session.submit("x", secrets={"key": secret}, on_progress=seen.append).wait(8)
        assert wait_until(lambda: lines)
        assert spelled_in(secret, lines + [json.dumps(seen)]) == []
        # The JSON dump above escapes the backslashes once more; check the raw strings too.
        assert spelled_in(secret, [seen[0]["message"], seen[0]["data"]["k"][0]]) == []
    finally:
        session.close(grace=0.3)


def test_secret_forms_keep_a_plain_secret_to_one_entry():
    assert frames.secret_forms("sk-plain_token.9~") == ["sk-plain_token.9~"]


def test_secret_spellings_do_not_count_against_the_distinct_secret_limit(make_session):
    session = make_session(limits=fast_limits(max_secret_values=3))
    session.start()
    session.request("echo", secrets={"a": "pa\\ss\twd'9é/&"}, timeout=5)
    assert len(session._secret_values) == 1 and len(session._secret_forms) > 1
    session.request("echo", secrets={"b": "second", "c": "third"}, timeout=5)


# A handler that exits the interpreter's way

def op_message(op_id, op="x", **extra):
    return dict({"t": "req", "id": op_id, "op": op, "params": {}}, **extra)


def reply_for(harness, op_id):
    assert wait_until(lambda: any(m["t"] == "res" and m["id"] == op_id for m in harness.messages))
    return next(m for m in harness.messages if m["t"] == "res" and m["id"] == op_id)


def test_a_handler_that_calls_sys_exit_gets_an_error_reply_and_frees_its_slot():
    def handler(operation):
        if operation.id == 1:
            sys.exit(7)
        return {"fine": operation.id}

    harness = ChannelHarness(handler, max_in_flight=1)
    harness.start()
    try:
        harness.host_out.write_control(op_message(1))
        reply = reply_for(harness, 1)
        assert reply["ok"] is False
        assert reply["error"]["code"] == frames.Codes.PLUGIN_ERROR
        assert reply["error"]["class_name"] == "SystemExit"
        assert wait_until(lambda: not harness.channel._operations and not harness.channel._transmitting)
        # One slot, and the channel is still serving: the exit ended that request only.
        harness.host_out.write_control(op_message(2))
        assert reply_for(harness, 2)["result"] == {"fine": 2}
        assert harness.thread.is_alive()
    finally:
        harness.finish()


def test_a_handler_that_raises_a_base_exception_subclass_is_answered():
    class Stop(BaseException):
        pass

    def handler(operation):
        raise Stop("stopping")

    harness = ChannelHarness(handler)
    harness.start()
    try:
        harness.host_out.write_control(op_message(1))
        reply = reply_for(harness, 1)
        assert reply["ok"] is False and reply["error"]["class_name"] == "Stop"
        assert wait_until(lambda: not harness.channel._operations and not harness.channel._transmitting)
    finally:
        harness.finish()


def test_a_sys_exit_inside_a_streamed_reply_is_answered_and_forgotten(monkeypatch):
    harness = ChannelHarness(lambda operation: worker_channel.OperationResult({}, b"payload"))
    monkeypatch.setattr(harness.channel._writer, "write_data", lambda *a, **k: sys.exit(2))
    harness.start()
    try:
        harness.host_out.write_control(op_message(1))
        reply = reply_for(harness, 1)
        assert reply["ok"] is False and reply["error"]["class_name"] == "SystemExit"
        assert wait_until(lambda: not harness.channel._operations and not harness.channel._transmitting)
    finally:
        harness.finish()


@pytest.mark.parametrize("exit_type", [KeyboardInterrupt, GeneratorExit])
def test_an_interrupt_in_a_handler_is_answered_then_raised_again_after_cleanup(exit_type):
    def handler(operation):
        raise exit_type()

    harness = ChannelHarness(handler)
    harness.start()
    try:
        operation = worker_channel.Operation(harness.channel, op_message(1))
        harness.channel._operations[1] = operation
        with pytest.raises(exit_type):
            harness.channel._run_operation(operation)
        reply = reply_for(harness, 1)
        assert reply["ok"] is False and reply["error"]["class_name"] == exit_type.__name__
        assert not harness.channel._operations and not harness.channel._transmitting
    finally:
        harness.finish()


@pytest.mark.parametrize("exit_type", [KeyboardInterrupt, GeneratorExit])
@pytest.mark.parametrize("where", ["error-text", "result"])
def test_an_interrupt_while_building_the_reply_still_ends_in_a_terminal_reply(exit_type, where):
    class BadText(Exception):
        def __str__(self):
            raise exit_type()

    class BadData:
        def __len__(self):
            raise exit_type()

        def __bool__(self):
            raise exit_type()

    def handler(operation):
        if where == "error-text":
            raise BadText()
        return worker_channel.OperationResult({}, BadData())

    harness = ChannelHarness(handler)
    harness.start()
    try:
        operation = worker_channel.Operation(harness.channel, op_message(1))
        harness.channel._operations[1] = operation
        with pytest.raises(exit_type):
            harness.channel._run_operation(operation)
        # The host is not left waiting: a fixed failure went out before the re-raise.
        reply = reply_for(harness, 1)
        assert reply["ok"] is False
        assert reply["error"]["code"] == frames.Codes.PLUGIN_ERROR
        assert not harness.channel._operations and not harness.channel._transmitting
        assert 1 not in harness.channel._replied
    finally:
        harness.finish()


def test_an_operation_that_cannot_even_get_its_fixed_reply_closes_the_channel(monkeypatch):
    class BadText(Exception):
        def __str__(self):
            raise KeyboardInterrupt()

    def handler(operation):
        raise BadText()

    harness = ChannelHarness(handler)
    harness.start()
    try:
        operation = worker_channel.Operation(harness.channel, op_message(1))
        harness.channel._operations[1] = operation
        # Every write fails: the only honest thing left is to end the channel.
        monkeypatch.setattr(harness.channel, "_send", lambda message: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            harness.channel._run_operation(operation)
        assert harness.channel._closed.is_set()
        assert harness.channel.exit_status == 3
    finally:
        monkeypatch.undo()
        harness.finish()


def test_a_heartbeat_thread_that_dies_ends_the_channel(monkeypatch):
    harness = ChannelHarness(lambda operation: {}, heartbeat_interval=0.05)
    monkeypatch.setattr(worker_channel, "_rss_kb", lambda: sys.exit(1))
    harness.start()
    try:
        harness.thread.join(5)
        assert not harness.thread.is_alive()
        assert harness.status == [3]
    finally:
        harness.finish()


def test_a_reader_thread_that_hits_a_system_exit_ends_the_channel_with_a_failure(monkeypatch):
    harness = ChannelHarness(lambda operation: {})
    harness.start()
    monkeypatch.setattr(harness.channel, "_dispatch", lambda message: sys.exit(1))
    try:
        harness.host_out.write_control(op_message(1))
        harness.thread.join(5)
        assert not harness.thread.is_alive()
        assert harness.status == [3]
    finally:
        harness.finish()


def test_a_host_call_handler_that_calls_sys_exit_fails_that_call_only(make_session):
    session = make_session(host_calls={"quit": lambda call: sys.exit(1), "good": lambda call: {"ok": 1}})
    session.start()
    with pytest.raises(RequestFailed) as caught:
        session.request("call", {"name": "quit"}, timeout=5)
    assert caught.value.code == frames.Codes.HOST_CALL_FAILED
    assert session.request("call", {"name": "good"}, timeout=5).result["ack"] == {"ok": 1}
    assert wait_until(lambda: not session._call_queues and session._call_pending == 0)


def test_callbacks_that_call_sys_exit_do_not_stop_the_session(make_session):
    lines, events = [], []

    def stderr_exit(line):
        lines.append(line)
        sys.exit(1)

    def event_exit(name, payload):
        events.append(name)
        sys.exit(1)

    session = make_session(on_stderr=stderr_exit, on_event=event_exit)
    session.start()
    assert session.request("progress_secret", secrets={"key": "sk-abc-123"},
                           on_progress=lambda message: sys.exit(1), timeout=5)
    with pytest.raises(RequestFailed):
        session.request("blocked", timeout=5)
    session.request("leak", secrets={"key": PEM_SECRET}, timeout=5)
    # The stderr drain kept reading after the first line raised: later lines arrive.
    assert wait_until(lambda: len(lines) >= 4)
    assert events and session.alive


def test_a_writer_that_exits_the_thread_ends_the_session(tmp_path):
    script = tmp_path / "quiet.py"
    script.write_text(HOSTILE_PRELUDE + "time.sleep(30)\n", encoding="utf-8")

    class ExitsOnSecondWrite(_BreaksOnSecondWrite):
        def write(self, data):
            self._writes += 1
            if self._writes > 1:
                sys.exit(1)
            return self._stream.write(data)

    def spawn():
        process = subprocess.Popen([sys.executable, "-B", str(script)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        process.stdin = ExitsOnSecondWrite(process.stdin)
        return process

    session = WorkerSession(spawn=spawn, limits=fast_limits(max_in_flight=16, heartbeat_timeout=30.0))
    try:
        session.start()
        with pytest.raises(SessionError):
            session.submit("x").wait(8)
        assert not session.alive
    finally:
        session.close(grace=0.3)


def test_a_watchdog_that_exits_the_thread_ends_the_session(make_session, monkeypatch):
    session = make_session()
    session.start()
    monkeypatch.setattr(session, "_cancel", lambda *a, **k: sys.exit(1))
    handle = session.submit("sleep", {"seconds": 30}, timeout=0.1)
    with pytest.raises(SessionError):
        handle.wait(8)
    assert not session.alive


# A rejection reaches the worker before the worker is ended

class _SlowStdin:
    """A worker stdin that is slow to take a frame and notes each frame it took whole."""

    def __init__(self, stream, delivered):
        self._stream = stream
        self._delivered = delivered
        self._pending = b""

    def write(self, data):
        time.sleep(0.4)
        self._pending = data
        return self._stream.write(data)

    def flush(self):
        # Raises if the worker is already gone, like a real pipe to a dead reader.
        self._stream.flush()
        self._delivered.append(self._pending)

    def close(self):
        self._stream.close()


def test_a_rejected_worker_receives_the_rejection_before_it_is_ended(tmp_path):
    script = tmp_path / "rejected.py"
    script.write_text(textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        import frames
        out = frames.FrameWriter(sys.stdout.buffer)
        out.write_control({"t": "hello", "protocols": [{"major": 2, "minor": 0}], "kinds": []})
        time.sleep(30)
    """) % str(RUNTIME_DIR), encoding="utf-8")
    delivered = []

    def spawn():
        process = subprocess.Popen([sys.executable, "-B", str(script)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        process.stdin = _SlowStdin(process.stdin, delivered)
        return process

    session = WorkerSession(spawn=spawn, kind="translator", limits=fast_limits(heartbeat_timeout=30.0))
    try:
        with pytest.raises(HandshakeError) as caught:
            session.start()
        assert caught.value.code == frames.Codes.VERSION_MISMATCH
        # The rejection went onto the worker's pipe whole before the worker was ended.
        assert len(delivered) == 1
        ftype, body = frames.FrameReader(io.BytesIO(delivered[0])).read_frame()
        message = frames.decode_control(body, "host")
        assert message["t"] == frames.REJECT and message["code"] == frames.Codes.VERSION_MISMATCH
    finally:
        session.close(grace=0.3)


def test_a_stray_thread_cannot_act_for_an_operation_that_has_returned():
    leaked = []

    def handler(operation):
        leaked.append(operation)
        return {}

    harness = ChannelHarness(handler)
    harness.start()
    try:
        harness.host_out.write_control({"t": "req", "id": 1, "op": "x", "params": {}})
        assert wait_until(lambda: any(m["t"] == "res" for m in harness.messages))
        operation = leaked[0]
        operation.progress(message="late")
        operation.sandbox_blocked("203.0.113.9:25")
        with pytest.raises(worker_channel.HostCallFailed) as caught:
            operation.call("checkpoint", {})
        assert caught.value.code == frames.Codes.INVALID_REQUEST
        time.sleep(0.3)
        assert [m["t"] for m in harness.messages if m["t"] in ("progress", "call", "sandbox_blocked")] == []
    finally:
        harness.finish()


def test_a_group_found_gone_after_the_session_ended_it_is_not_reported_lost(monkeypatch):
    """Our own reap makes the leader vanish; only a reap by somebody else loses the group."""
    session = WorkerSession(["worker"])
    process = _ReapingProcess()
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: None)
    state = {"now": session_module._RUNNING}
    monkeypatch.setattr(session_module, "_probe", lambda proc, mark=None: state["now"])
    session._end_group(process)
    assert session._group_ended
    # The session's own reap: the leader is not there any more.
    state["now"] = session_module._GONE
    session._exited(process)
    assert not session._group_lost
    fresh = WorkerSession(["worker"])
    fresh._exited(process)
    assert fresh._group_lost


# --------------------------------------------- processes that end their own tree


def _launcher_session(tmp_path, config=None, **kwargs):
    """A session whose worker is the real sandbox launcher around the fake worker."""
    from provider_hub.runtime import testing
    from provider_hub.sandbox import Limits, RuntimeSpec, SandboxPolicy, SandboxProbe, clean_env, spawn_worker

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    runtime_dir = Path(testing.__file__).resolve().parents[1]
    command = fake_worker_command()
    declared = SandboxProbe(abi=0, verified=False, reason="test: declared mode")

    def spawn():
        policy = SandboxPolicy.for_interpreter(sys.executable, read_only=[runtime_dir], read_write=[data])
        spec = RuntimeSpec(
            interpreter=command[0], args=tuple(command[1:]), cwd=str(data),
            env=clean_env(home=data, tmpdir=data, extra=fake_worker_env(**(config or {}))),
            policy=policy, limits=Limits(), term_grace=1.0)
        return spawn_worker(spec, probe=declared)

    return WorkerSession(spawn=spawn, kind="translator", limits=fast_limits(**kwargs), name="launcher-session")


linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the launcher is Linux only")


@linux_only
@pytest.mark.parametrize("ending", ["close", "ignored-cancel"])
def test_a_launcher_worker_is_ended_by_the_launcher_not_by_a_group_kill(tmp_path, monkeypatch, ending):
    """The launcher's own cleanup must finish: it reaches a child that left the group with setsid."""
    session = _launcher_session(tmp_path, {"stubborn": True})
    session.start()
    launcher = session.process
    child = session.request("fork", {"detached": True}, timeout=5).result["child"]
    assert pid_alive(child)
    kills = []
    real = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (kills.append((pgid, sig)), real(pgid, sig))[1])
    if ending == "close":
        session.close(grace=0.3)
    else:
        handle = session.submit("sleep", {"seconds": 30, "ignore_cancel": True}, timeout=0.3)
        with pytest.raises(RequestTimeout):
            handle.wait()
        session.close(grace=0.3)
    assert wait_until(lambda: not pid_alive(child), timeout=10), "the setsid child outlived the session"
    # The launcher left on its own after its cleanup; nobody killed it first.
    assert launcher.returncode is not None and launcher.returncode != -signal.SIGKILL
    assert (launcher.pid, signal.SIGKILL) not in kills


class _OwnTreeProcess:
    """A handle with the launcher contract: a lifeline to cut and a stop() that ends everything."""

    args = ["worker"]

    def __init__(self):
        self.pid = os.getpid()
        self.returncode = None
        self.events = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def close_lifeline(self):
        self.events.append("lifeline")

    def stop(self, grace=3.0):
        self.events.append("stop")
        self.returncode = 0


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_a_process_that_ends_its_own_tree_is_handed_the_teardown(monkeypatch):
    session = WorkerSession(["worker"])
    process = _OwnTreeProcess()
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    session._kill_tree(process)
    session._end_group(process)
    session._kill_tree(process)
    assert process.events == ["lifeline", "stop"]
    assert calls == []
    assert session.exit_code == 0


class _GroupSignallingProcess(_OwnTreeProcess):
    """A handle that checks ownership and signals under its own lock."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.signals = []

    def signal_group(self, sig):
        self.signals.append(sig)
        return self.answer


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
@pytest.mark.parametrize("answer,expected", [(True, True), (None, True), (False, False)])
def test_a_process_with_its_own_group_signal_is_asked_to_send_it(monkeypatch, answer, expected):
    session = WorkerSession(["worker"])
    process = _GroupSignallingProcess(answer)
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    assert session._signal_leader_group(process, signal.SIGTERM) is expected
    assert process.signals == [signal.SIGTERM]
    assert calls == []
    # Once the group was ended nothing is asked of the process any more.
    session._group_ended = True
    assert session._signal_leader_group(process, signal.SIGTERM) is True
    assert process.signals == [signal.SIGTERM]


def test_a_done_callback_runs_once_whether_it_was_added_before_or_after_the_finish(session):
    handle = session.submit("sleep", {"seconds": 0.3})
    early, late, broken = [], [], []
    handle.add_done_callback(lambda: early.append(handle.done()))
    handle.add_done_callback(lambda: broken.append(1) or (_ for _ in ()).throw(RuntimeError("boom")))
    assert not handle.done()
    handle.wait(10)
    assert wait_until(lambda: early == [True] and broken == [1])
    handle.add_done_callback(lambda: late.append(handle.done()))
    assert late == [True]
    assert handle.error is None
    failing = session.submit("raise", {})
    seen = []
    failing.add_done_callback(lambda: seen.append(failing.error))
    with pytest.raises(RequestFailed):
        failing.wait(10)
    assert wait_until(lambda: len(seen) == 1 and isinstance(seen[0], RequestFailed))


class _SlowStopProcess(_OwnTreeProcess):
    """A contract process whose ``stop()`` takes a while; records the order of reap and cleanup."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.started = threading.Event()
        self.order = []

    def stop(self, grace=3.0):
        self.order.append("stop-start")
        self.started.set()
        self.release.wait(10)
        self.order.append("stop-done")
        self.returncode = None

    def wait(self, timeout=None):
        self.order.append("wait")
        self.returncode = 0
        return 0


def test_nothing_reaps_the_leader_while_its_teardown_is_still_running():
    session = WorkerSession(["worker"], limits=fast_limits(kill_grace=5.0))
    process = _SlowStopProcess()
    tearer = threading.Thread(target=session._kill_tree, args=(process,))
    tearer.start()
    assert process.started.wait(5)
    reaped = []
    reaper = threading.Thread(target=lambda: reaped.append(session._reap(process, 8.0)))
    reaper.start()
    time.sleep(0.4)
    # The reaper is held back: reaping now would make stop() skip the group cleanup.
    assert "wait" not in process.order
    process.release.set()
    tearer.join(10)
    reaper.join(10)
    assert process.order.index("stop-done") < process.order.index("wait")
    assert reaped == [0]


def test_a_reap_that_cannot_wait_out_the_teardown_gives_up_instead_of_reaping():
    session = WorkerSession(["worker"], limits=fast_limits(kill_grace=5.0))
    process = _SlowStopProcess()
    tearer = threading.Thread(target=session._kill_tree, args=(process,))
    tearer.start()
    assert process.started.wait(5)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            session._reap(process, 0.3)
        assert "wait" not in process.order
    finally:
        process.release.set()
        tearer.join(10)


@pytest.mark.skipif(not hasattr(os, "waitid") or os.name != "posix", reason="needs waitid")
def test_a_probe_overtaken_by_the_process_objects_own_reap_does_not_lose_the_group(monkeypatch):
    from provider_hub.sandbox import WorkerProcess

    process = WorkerProcess([sys.executable, "-c", "pass"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        time.sleep(0.01)
    session = WorkerSession(["worker"])
    session.process = process
    real_waitid = os.waitid
    inside = threading.Event()
    first = []

    def pausing(*args, **kwargs):
        # The probe has read returncode (None) and is about to ask the kernel; the
        # pool's own stop() reaps the child in this window.
        if not first:
            first.append(True)
            inside.set()
            time.sleep(0.4)
        return real_waitid(*args, **kwargs)

    monkeypatch.setattr(os, "waitid", pausing)
    results = []
    prober = threading.Thread(target=lambda: results.append(session._exited(process)))
    prober.start()
    assert inside.wait(5)
    stopper = threading.Thread(target=process.stop, args=(0.5,))
    stopper.start()
    prober.join(10)
    stopper.join(10)
    monkeypatch.undo()
    assert results == [True]
    # The process object did the reap itself: that is not somebody else's.
    assert session._group_lost is False
    assert process.returncode is not None


class _OrderedProcess:
    """A plain child handle that records the order of group signals and of its reap."""

    args = ["worker"]

    def __init__(self, order):
        self.pid = os.getpid()
        self.returncode = None
        self.order = order

    def wait(self, timeout=None):
        self.order.append("reap")
        self.returncode = 0
        return 0


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_the_leader_is_not_reaped_between_the_flag_and_the_group_signal(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    order = []
    session = WorkerSession(["worker"])
    process = _OrderedProcess(order)
    session._process_mark = session_module._start_mark(process.pid)  # what a spawn records
    in_step = threading.Event()

    def pausing_signal(target, sig):
        # The flag is set; the signal is about to go. A reader that sees the flag must not reap yet.
        order.append("signal-start")
        in_step.set()
        time.sleep(0.4)
        order.append("signal-sent")
        return True

    monkeypatch.setattr(session_module, "_signal_group", pausing_signal)
    ender = threading.Thread(target=session._end_group, args=(process,))
    ender.start()
    assert in_step.wait(5)
    assert session._group_ended is True
    reaper = threading.Thread(target=lambda: session._reap(process, 5.0))
    reaper.start()
    ender.join(10)
    reaper.join(10)
    assert order == ["signal-start", "signal-sent", "reap"], order


# ---- A bare session child with no waitid: ownership comes from /proc and the start time -------


def _bare_child():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = WorkerSession(["worker"])
    session.process = process
    session._process_mark = session_module._start_mark(process.pid)
    return session, process


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_bare_child_reaped_by_somebody_else_is_not_ours_without_waitid(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    session, process = _bare_child()
    try:
        assert session._process_mark is not None
        with session._group_lock:
            assert session._ours(process) is True
        os.kill(process.pid, signal.SIGKILL)
        os.waitpid(process.pid, 0)
        assert process.returncode is None  # Popen was not told
        with session._group_lock:
            assert session._ours(process) is False
        assert session._group_lost is True
    finally:
        monkeypatch.undo()
        try:
            process.kill()
        except OSError:
            pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_process_that_took_the_childs_pid_is_not_ours_without_waitid(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    session, process = _bare_child()
    try:
        # The pid is alive and a perfectly good process, but it started later than the child did.
        monkeypatch.setattr(session_module, "_proc_fields", lambda pid: (b"S", b"999999999999"))
        with session._group_lock:
            assert session._ours(process) is False
        assert session._group_lost is True
    finally:
        monkeypatch.undo()
        process.kill()
        process.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_bare_child_with_no_saved_identity_is_unknown_when_a_stranger_has_its_pid(monkeypatch):
    # The start time could not be read at spawn; the child is then reaped by somebody else and a
    # stranger takes the pid. Nothing can tell them apart, so no group signal and no confirmed teardown.
    monkeypatch.delattr(os, "waitid", raising=False)
    session, process = _bare_child()
    session._process_mark = None
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    try:
        os.kill(process.pid, signal.SIGKILL)
        os.waitpid(process.pid, 0)
        monkeypatch.setattr(session_module, "_proc_fields", lambda pid: (b"S", b"999999999999"))
        assert session_module._probe(process, None) == session_module._UNKNOWN
        with session._group_lock:
            assert session._ours(process) is False
        assert session._group_lost is True and session._ownership_unknown is True
        session.close(grace=0.3)
        assert [sig for pgid, sig in sent if pgid == process.pid] == [], sent
        assert session.teardown_confirmed is False
    finally:
        monkeypatch.undo()
        try:
            process.kill()
        except OSError:
            pass


def test_ownership_is_unknown_and_refused_with_neither_waitid_nor_proc(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    monkeypatch.setattr(session_module, "_has_proc", lambda: False)
    session = WorkerSession(["worker"])
    process = _ReapingProcess()  # alive, a pid that answers signal 0
    with session._group_lock:
        assert session._ours(process) is False
    assert session._group_lost is True
    assert session_module._zombie_or_gone(os.getpid()) == session_module._UNKNOWN

    def gone(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "kill", gone)
    assert session_module._zombie_or_gone(424242) == session_module._GONE


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_started_session_records_its_childs_start_time():
    session = WorkerSession(fake_worker_command(), env=fake_worker_env(), kind="translator", limits=fast_limits())
    session.start()
    try:
        assert session._process_mark == session_module._start_mark(session.process.pid)
        assert session._process_mark is not None
    finally:
        session.close(grace=1.0)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_close_ends_a_stubborn_worker_where_ownership_cannot_be_established(make_session, monkeypatch):
    # Neither waitid nor /proc, and a worker that ignores both the shutdown frame and SIGTERM:
    # the group is not signalled, but close() must still end and reap the child itself.
    monkeypatch.delattr(os, "waitid", raising=False)
    monkeypatch.setattr(session_module, "_has_proc", lambda: False)
    session = make_session({"stubborn": True}, limits=fast_limits(kill_grace=3.0, term_grace=0.3))
    session.start()
    process = session.process
    child = session.request("fork", {"inherit": True}, timeout=5).result["child"]
    try:
        started = time.monotonic()
        session.close(grace=0.4)
        assert time.monotonic() - started < 10
        assert process.returncode is not None, "the stubborn worker outlived close()"
        # Marked once, whichever way the unknown answer first came: through exited() or ours().
        assert session._group_lost is True and session._ownership_unknown is True
        assert session.teardown_confirmed is False
    finally:
        monkeypatch.undo()
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
    assert wait_until(lambda: not pid_alive(child), timeout=5)
    ours = list(session._threads)
    assert wait_until(lambda: not [t.name for t in ours if t.is_alive()])


def test_exited_and_ours_both_record_an_unknown_answer_the_same_way(monkeypatch):
    for how in ("exited", "ours"):
        session = WorkerSession(["worker"])
        process = _ReapingProcess()
        monkeypatch.setattr(session_module, "_probe", lambda proc, mark=None: session_module._UNKNOWN)
        if how == "exited":
            session._exited(process)
        else:
            with session._group_lock:
                assert session._ours(process) is False
        assert session._group_lost is True and session._ownership_unknown is True, how
    for how in ("exited", "ours"):
        session = WorkerSession(["worker"])
        process = _ReapingProcess()
        monkeypatch.setattr(session_module, "_probe", lambda proc, mark=None: session_module._GONE)
        if how == "exited":
            session._exited(process)
        else:
            with session._group_lock:
                assert session._ours(process) is False
        # Gone is lost without being unknown: somebody else reaped it, and there is nothing to end.
        assert session._group_lost is True and session._ownership_unknown is False, how


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("mode", ["waitid-missing", "waitid-fails"])
def test_a_stop_during_the_proc_probe_is_not_read_as_somebody_elses_reap(monkeypatch, mode):
    import builtins

    from provider_hub.sandbox import WorkerProcess

    process = WorkerProcess([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = WorkerSession(["worker"])
    session.process = process
    session._process_mark = session_module._start_mark(process.pid)
    if mode == "waitid-missing":
        monkeypatch.delattr(os, "waitid", raising=False)
    else:
        def failing(*args, **kwargs):
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "waitid", failing)
    real_open = builtins.open
    inside = threading.Event()
    first = []

    def pausing_open(path, *args, **kwargs):
        # The probe is about to read the child's /proc entry; the pool's stop() reaps it in this window.
        if str(path) == "/proc/%d/stat" % process.pid and threading.current_thread().name == "prober":
            first.append(True)
            inside.set()
            time.sleep(0.4)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", pausing_open)
    results = []
    prober = threading.Thread(target=lambda: results.append(session._exited(process)), name="prober")
    prober.start()
    assert inside.wait(5)
    stopper = threading.Thread(target=process.stop, args=(0.5,))
    stopper.start()
    prober.join(10)
    stopper.join(15)
    monkeypatch.undo()
    assert not prober.is_alive() and not stopper.is_alive()
    # The object reaped it itself: that is not somebody else's, and the slot must not stay stuck.
    assert session._group_lost is False
    assert process.returncode is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_contract_process_that_cannot_tell_is_unknown_to_the_session_not_asked_again(monkeypatch):
    from provider_hub.sandbox import WorkerProcess, spawn as sandbox_spawn

    process = WorkerProcess([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        monkeypatch.delattr(os, "waitid", raising=False)

        def unreadable(pid):
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(sandbox_spawn, "_stat_fields", unreadable)
        assert process.probe_exit() is None
        assert session_module._probe(process) == session_module._UNKNOWN
    finally:
        monkeypatch.undo()
        process.kill()
        process.wait()
