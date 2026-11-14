# coding=utf-8
"""Worker wire protocol v2: framing, message schemas and version negotiation.

This is the one module both sides of the pipe import. It uses the standard
library only and imports nothing from Bazarr+, so the plugin SDK vendors it
byte for byte and a copy check keeps the two identical. Keep it that way: no
Bazarr imports, no third party imports, nothing newer than Python 3.9 syntax.

Wire format
===========

The worker's stdout carries frames to the host and the worker's stdin carries
frames from the host. Everything else a plugin prints must go to stderr (the
worker side moves fd 1 to stderr after it has saved the real stdout).

A frame is a 5 byte header followed by a body::

    +--------+------------------+----------------+
    | type   | body length      | body           |
    | 1 byte | 4 bytes, big end | length bytes   |
    +--------+------------------+----------------+

Types:

* ``FRAME_CONTROL`` (1): the body is one UTF-8 JSON object with a string
  ``t`` field naming the message. Capped at ``MAX_CONTROL_BYTES``.
* ``FRAME_DATA`` (2): a result chunk. The body is a 4 byte big endian
  operation id followed by raw bytes. Capped at ``MAX_DATA_BYTES``.

The reader checks the length against the cap for the type before it reads the
body, so a hostile prefix costs nothing. A first byte of ``{`` (0x7b) is not a
frame type: it means the peer speaks the v1 line protocol.

Conversation
============

The worker speaks first. Ids are small positive integers: the host numbers
requests, the worker numbers its own calls (``cid``).

``hello`` (worker)   protocols, kinds, max_in_flight, worker, pid
``welcome`` (host)   protocol, kind, limits, session
``reject`` (host)    code, message; the host then closes the pipe
``req`` (host)       id, op, params, deadline_ms, max_result_bytes, secrets
``cancel`` (host)    id, reason
``progress`` (worker) id, fraction, message, data
``call`` (worker)    cid, id, name, params   (a question to the host)
``ack`` (host)       cid, ok, result or error
``res`` (worker)     id, ok, result, chunks, bytes, sha256, or error
``hb`` (worker)      seq, in_flight, rss_kb
``sandbox_blocked`` (worker) id, target, errno
``shutdown`` (host)  grace_ms
``bye`` (worker)     sent once, just before a clean exit

A result with a payload larger than a control frame sends ``DATA`` frames for
the operation id first, then ``res`` with the total ``bytes``. Any frame from
the worker counts as proof of life; ``hb`` frames cover the silent stretches.

Version negotiation
===================

``hello.protocols`` lists ``{"major": M, "minor": m}`` for every protocol major
the worker speaks. The host picks the highest major both sides know and the
lower of the two minors. A minor adds optional keys and messages only. When
nothing matches the host sends ``reject`` with ``version_mismatch``.

``hello.kinds`` lists ``{"kind", "major", "minor"}`` for each plugin kind API
the worker serves, an older major included. The host names the majors it can
use in preference order and the first one the worker offers wins, with the
lower minor. The v1 provider workers never send ``hello``; they stay on the
line protocol in ``provider_hub.worker`` and are selected by ``wire_version``.
"""

from __future__ import annotations

import json
import re
import struct
import threading
import urllib.parse

PROTOCOL_MAJOR = 2
PROTOCOL_MINOR = 0
# Wire versions a manifest or spec can name; see wire_version().
V1_ABI = "bazarr.provider-worker.v1"
V2_ABI = "bazarr.worker-protocol.v2"

FRAME_CONTROL = 1
FRAME_DATA = 2
HEADER = struct.Struct(">BI")
DATA_PREFIX = struct.Struct(">I")

MAX_CONTROL_BYTES = 1024 * 1024
# Chunk payload plus the 4 byte operation id.
MAX_DATA_BYTES = 1024 * 1024 + DATA_PREFIX.size
DEFAULT_CHUNK_BYTES = 256 * 1024
# Until the welcome the peer is unknown, so the control cap is small.
HANDSHAKE_CONTROL_BYTES = 64 * 1024

DEFAULT_HEARTBEAT_INTERVAL = 15.0
DEFAULT_HEARTBEAT_MISSES = 3
DEFAULT_MAX_IN_FLIGHT = 4
# The longest drain a host may ask for in a shutdown frame: ten minutes.
MAX_SHUTDOWN_GRACE_MS = 600_000
# Containers nested deeper than this in a control frame are refused.
MAX_JSON_DEPTH = 100
DEFAULT_MAX_RESULT_BYTES = 32 * 1024 * 1024

# The first byte of a v1 line (a JSON object).
LEGACY_V1_FIRST_BYTE = b"{"

# Message names.
HELLO = "hello"
WELCOME = "welcome"
REJECT = "reject"
REQ = "req"
CANCEL = "cancel"
PROGRESS = "progress"
CALL = "call"
ACK = "ack"
RES = "res"
HB = "hb"
SANDBOX_BLOCKED = "sandbox_blocked"
SHUTDOWN = "shutdown"
BYE = "bye"

FROM_WORKER = frozenset({HELLO, PROGRESS, CALL, RES, HB, SANDBOX_BLOCKED, BYE})
FROM_HOST = frozenset({WELCOME, REJECT, REQ, CANCEL, ACK, SHUTDOWN})


class Codes:
    """The shared error vocabulary, in ``error.code`` of a failed ``res``/``ack``.

    A plugin may use its own codes too; these are the ones the runtime itself
    reads or raises.
    """

    PLUGIN_ERROR = "plugin_error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    BUSY = "busy"
    UNKNOWN_OP = "unknown_op"
    INVALID_REQUEST = "invalid_request"
    RESULT_TOO_LARGE = "result_too_large"
    FRAME_TOO_LARGE = "frame_too_large"
    PROTOCOL_ERROR = "protocol_error"
    VERSION_MISMATCH = "version_mismatch"
    HANDSHAKE_FAILED = "handshake_failed"
    LEGACY_PROTOCOL = "legacy_protocol"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    WRITE_TIMEOUT = "write_timeout"
    RECYCLE_REQUIRED = "recycle_required"
    SHUTTING_DOWN = "shutting_down"
    WORKER_EXITED = "worker_exited"
    SESSION_CLOSED = "session_closed"
    SANDBOX_BLOCKED = "sandbox_blocked"
    UNKNOWN_CALL = "unknown_call"
    HOST_CALL_FAILED = "host_call_failed"


class FrameError(Exception):
    """A frame the reader refuses. ``code`` is one of ``Codes``."""

    code = Codes.PROTOCOL_ERROR

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class FrameTooLarge(FrameError):
    code = Codes.FRAME_TOO_LARGE


class MalformedFrame(FrameError):
    """Unknown type, truncated body, bad JSON or a message that breaks its schema.

    ``type_byte`` is set for an unknown frame type so the caller can tell a v1
    peer (a ``{``) from line noise.
    """

    def __init__(self, message, *, code=None, type_byte=None):
        super().__init__(message, code=code)
        self.type_byte = type_byte


def default_caps():
    return {FRAME_CONTROL: MAX_CONTROL_BYTES, FRAME_DATA: MAX_DATA_BYTES}


class FrameReader:
    """Reads frames from a binary stream. Not thread safe: one reader thread."""

    def __init__(self, stream, caps=None):
        self._stream = stream
        self.caps = dict(caps) if caps else default_caps()

    def _read_exact(self, size, what):
        parts = []
        remaining = size
        while remaining:
            chunk = self._stream.read(remaining)
            if not chunk:
                raise MalformedFrame("stream ended inside a frame %s" % what)
            parts.append(chunk)
            remaining -= len(chunk)
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def read_frame(self):
        """Return ``(type, body)`` or ``None`` on a clean end of stream."""
        first = self._stream.read(1)
        if not first:
            return None
        rest = self._read_exact(HEADER.size - 1, "header")
        ftype, length = HEADER.unpack(first + rest)
        cap = self.caps.get(ftype)
        if cap is None:
            raise MalformedFrame(
                "unknown frame type %d" % ftype,
                code=Codes.LEGACY_PROTOCOL if first == LEGACY_V1_FIRST_BYTE else Codes.PROTOCOL_ERROR,
                type_byte=first,
            )
        if length > cap:
            raise FrameTooLarge("frame of %d bytes exceeds the %d byte cap for type %d" % (length, cap, ftype))
        body = self._read_exact(length, "body") if length else b""
        return ftype, body


class FrameWriter:
    """Writes frames to a binary stream. Safe to share between threads.

    Each frame is written and flushed under one lock, so frames from different
    threads never interleave mid-frame. Data chunks of one operation may be
    interleaved with other frames; the operation id says which they belong to.
    """

    def __init__(self, stream, caps=None, lock=None, sender=None):
        # ``sender`` ("host" or "worker"), when given, makes every control message
        # this writer sends pass the receiving side's schema first.
        self._sender = sender
        self._stream = stream
        self.caps = dict(caps) if caps else default_caps()
        self._lock = lock or threading.Lock()

    def _write(self, ftype, body):
        header = HEADER.pack(ftype, len(body))
        with self._lock:
            self._stream.write(header + body)
            self._stream.flush()

    def write_packed(self, data):
        """Write a frame already built by ``pack_control``."""
        with self._lock:
            self._stream.write(data)
            self._stream.flush()

    def write_control(self, message):
        body = encode_control(message, self._sender)
        cap = self.caps[FRAME_CONTROL]
        if len(body) > cap:
            raise FrameTooLarge("control message of %d bytes exceeds the %d byte cap" % (len(body), cap))
        self._write(FRAME_CONTROL, body)

    def write_data(self, op_id, data, chunk_size=DEFAULT_CHUNK_BYTES, before_chunk=None):
        """Send ``data`` as chunks. Returns ``(chunk_count, total_bytes)``.

        ``before_chunk``, when given, is called ahead of every chunk and may
        raise to stop the transfer; chunks already written stay written.
        """
        cap = self.caps[FRAME_DATA] - DATA_PREFIX.size
        size = max(1, min(int(chunk_size), cap))
        view = memoryview(data)
        count = 0
        for start in range(0, len(view), size):
            if before_chunk is not None:
                before_chunk()
            self._write(FRAME_DATA, DATA_PREFIX.pack(op_id) + bytes(view[start:start + size]))
            count += 1
        return count, len(view)


def pack_control(message, cap=MAX_CONTROL_BYTES, sender=None):
    """One whole control frame (header and body) as bytes, checked against ``cap``.

    For senders that queue frames for a writer thread instead of writing under
    a lock.
    """
    body = encode_control(message, sender)
    if len(body) > cap:
        raise FrameTooLarge("control message of %d bytes exceeds the %d byte cap" % (len(body), cap))
    return HEADER.pack(FRAME_CONTROL, len(body)) + body


def encode_control(message, sender=None):
    """Serialise one control message, refusing exactly what ``decode_control`` refuses.

    With ``sender`` ("host" or "worker") the message must also pass the schema the
    receiving side checks, so a frame the peer would reject never leaves: it is
    the sender's mistake (``UnencodableFrame``, a ``ValueError``), not the peer's
    reason to end the session.
    """
    _check_tree(message, True)
    if sender is not None:
        try:
            validate_message(message, sender)
        except MalformedFrame as error:
            raise UnencodableFrame(str(error)) from None
    return json.dumps(message, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")


def split_data(body):
    """Split a ``FRAME_DATA`` body into ``(operation_id, bytes)``."""
    if len(body) < DATA_PREFIX.size:
        raise MalformedFrame("data frame shorter than its operation id")
    (op_id,) = DATA_PREFIX.unpack_from(body)
    return op_id, body[DATA_PREFIX.size:]


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_grace_ms(value):
    return _is_int(value) and 0 <= value <= MAX_SHUTDOWN_GRACE_MS


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_str(value):
    return isinstance(value, str)


def _is_dict(value):
    return isinstance(value, dict)


def _is_bool(value):
    return isinstance(value, bool)


def _is_list(value):
    return isinstance(value, list)


def is_secrets(value):
    """A flat mapping of text names to non-empty text values.

    The only shape a request may carry as ``secrets``: every value is
    registered for redaction as text, so a number, a nested object, an empty
    string or bytes would travel unredacted.
    """
    return isinstance(value, dict) and all(
        isinstance(name, str) and isinstance(item, str) and item for name, item in value.items())


def secret_forms(value):
    """Every spelling of a secret that an error message, log line or URL may carry.

    The literal value is never enough: ``KeyError(secret)`` prints through
    ``repr`` and doubles every backslash, a JSON body escapes control and
    non-ASCII characters, and a URL percent-encodes the reserved ones. Each
    spelling is a string to cut out of outgoing text, so the list is built once
    per secret when it is registered. The literal value comes first.
    """
    forms = [value]

    def add(text):
        if text and text not in forms:
            forms.append(text)

    bodies = []

    def python_forms(text):
        for spelled in (repr(text), ascii(text)):
            body = spelled[1:-1]
            plain = body.replace("\\'", "'")
            # repr picks its quote by content: a body holds \' or a bare '.
            bodies.extend((body, plain, plain.replace("'", "\\'")))
        bodies.append(repr(text.encode("utf-8", "surrogatepass"))[2:-1])

    def json_forms(text):
        for ensure_ascii in (True, False):
            body = json.dumps(text, ensure_ascii=ensure_ascii)[1:-1]
            bodies.extend((body, body.replace("/", "\\/")))

    python_forms(value)
    json_forms(value)
    for body in bodies:
        add(body)
        # An error that carries a repr or a JSON document is itself repr'd or
        # dumped once more on its way out.
        add(repr(body)[1:-1])
        add(json.dumps(body)[1:-1])
    for encoded in (urllib.parse.quote(value, safe=""), urllib.parse.quote(value),
                    urllib.parse.quote_plus(value)):
        add(encoded)
        add(re.sub("%[0-9A-F]{2}", lambda m: m.group(0).lower(), encoded))
    return forms


def _opt(check):
    def wrapped(value):
        return value is None or check(value)

    return wrapped


# message -> {key: checker} for required keys, and the same for optional keys.
_REQUIRED = {
    HELLO: {"protocols": _is_list, "kinds": _is_list},
    WELCOME: {"protocol": _is_dict, "limits": _is_dict, "session": _is_str},
    REJECT: {"code": _is_str, "message": _is_str},
    REQ: {"id": _is_int, "op": _is_str, "params": _is_dict},
    CANCEL: {"id": _is_int},
    PROGRESS: {"id": _is_int},
    CALL: {"cid": _is_int, "name": _is_str, "params": _is_dict},
    ACK: {"cid": _is_int, "ok": _is_bool},
    RES: {"id": _is_int, "ok": _is_bool},
    HB: {"seq": _is_int},
    SANDBOX_BLOCKED: {"target": _is_str},
    SHUTDOWN: {},
    BYE: {},
}
_OPTIONAL = {
    HELLO: {"max_in_flight": _is_int, "worker": _is_dict, "pid": _is_int},
    WELCOME: {"kind": _opt(_is_dict)},
    REQ: {"deadline_ms": _opt(_is_int), "max_result_bytes": _opt(_is_int), "secrets": _opt(is_secrets)},
    CANCEL: {"reason": _opt(_is_str)},
    PROGRESS: {"fraction": _opt(_is_num), "message": _opt(_is_str), "data": _opt(_is_dict)},
    CALL: {"id": _opt(_is_int)},
    ACK: {"result": _opt(_is_dict), "error": _opt(_is_dict)},
    RES: {
        "result": _opt(_is_dict),
        "error": _opt(_is_dict),
        "chunks": _opt(_is_int),
        "bytes": _opt(_is_int),
        "sha256": _opt(_is_str),
    },
    HB: {"in_flight": _opt(_is_int), "rss_kb": _opt(_is_int)},
    SANDBOX_BLOCKED: {"id": _opt(_is_int), "errno": _opt(_is_int)},
    SHUTDOWN: {"grace_ms": _opt(_is_grace_ms)},
}


def validate_message(message, sender):
    """Check a decoded control message against its schema.

    ``sender`` is ``"worker"`` or ``"host"``. Unknown extra keys are allowed
    (a later minor may add them); unknown message names are not.
    """
    if not isinstance(message, dict):
        raise MalformedFrame("control message is not an object")
    name = message.get("t")
    if not isinstance(name, str):
        raise MalformedFrame("control message has no type")
    allowed = FROM_WORKER if sender == "worker" else FROM_HOST
    if name not in allowed:
        # The value is the peer's own text and may be a secret echoed back: never repeat it.
        raise MalformedFrame("unknown message type for a %s" % sender)
    for key, check in _REQUIRED[name].items():
        if key not in message or not check(message[key]):
            raise MalformedFrame("%s message has a missing or invalid %r" % (name, key))
    for key, check in _OPTIONAL.get(name, {}).items():
        if key in message and not check(message[key]):
            raise MalformedFrame("%s message has an invalid %r" % (name, key))
    if name == RES and message["ok"] is False and not isinstance(message.get("error"), dict):
        raise MalformedFrame("a failed res needs an error object")
    if name == ACK and message["ok"] is False and not isinstance(message.get("error"), dict):
        raise MalformedFrame("a failed ack needs an error object")
    return message


def _no_constant(name):
    # NaN, Infinity and -Infinity are not JSON, and the encoder refuses them.
    raise ValueError("non-finite number")


def _finite_float(text):
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        # A literal such as 1e999 overflows to infinity.
        raise ValueError("non-finite number")
    return value


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class UnencodableFrame(ValueError):
    """A message the sender built that the receiver's decoder would refuse.

    It is the sender's own fault and fails the one request or call that built
    it; it never reaches the wire, so it cannot end a session.
    """


def _check_tree(value, outbound):
    """The one set of rules for what a control frame may hold, applied both ways.

    Nesting no deeper than ``MAX_JSON_DEPTH``, text that is valid Unicode (no
    lone surrogates), finite numbers and unique object keys. Parsed input
    (``outbound`` false) fails as ``MalformedFrame``. A message about to be
    sent also has to be plain JSON data with text keys, because the encoder
    would otherwise turn ``{1: "a", "1": "b"}`` into an object with two ``"1"``
    keys; it fails as ``UnencodableFrame`` (``TypeError`` for a type JSON has
    no form for).
    """
    problem = UnencodableFrame if outbound else MalformedFrame
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, str):
            try:
                node.encode("utf-8")
            except UnicodeEncodeError:
                # A lone surrogate (\ud800): the receiver could never read it back.
                raise problem("control frame holds text that is not valid Unicode") from None
            continue
        if node is None or isinstance(node, (bool, int)):
            continue
        if isinstance(node, float):
            if node != node or node in (float("inf"), float("-inf")):
                raise problem("control frame holds a number that is not finite")
            continue
        if isinstance(node, dict):
            if outbound and not all(isinstance(key, str) for key in node):
                raise TypeError("control message object keys must be text")
            children = list(node.keys()) + list(node.values())
        elif isinstance(node, (list, tuple)) and (outbound or isinstance(node, list)):
            children = node
        elif outbound:
            raise TypeError("%s is not JSON serializable" % type(node).__name__)
        else:
            continue
        if depth > MAX_JSON_DEPTH:
            raise problem("control frame is nested too deeply")
        stack.extend((child, depth + 1) for child in children)


def decode_control(body, sender):
    """Parse one control frame strictly: valid Unicode, finite numbers, unique keys, bounded depth."""
    try:
        message = json.loads(
            body.decode("utf-8"), parse_constant=_no_constant, parse_float=_finite_float,
            object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        # RecursionError: JSON nested deeper than the interpreter allows.
        raise MalformedFrame("control frame is not valid JSON") from error
    _check_tree(message, False)
    return validate_message(message, sender)


def error_object(code, message, *, retryable=False, retry_after=None, class_name=None, traceback=None, errno=None):
    """The ``error`` object of a failed ``res`` or ``ack``."""
    error = {"code": code, "message": message, "retryable": bool(retryable)}
    if retry_after is not None:
        error["retry_after"] = retry_after
    if class_name:
        error["class_name"] = class_name
    if traceback:
        error["traceback"] = traceback
    if errno is not None:
        error["errno"] = errno
    return error


def _version_entry(entry):
    if not isinstance(entry, dict):
        return None
    major, minor = entry.get("major"), entry.get("minor", 0)
    if not _is_int(major) or not _is_int(minor) or major < 0 or minor < 0:
        return None
    return major, minor


def negotiate_protocol(offered, supported=None):
    """Pick ``(major, minor)`` from the worker's offer, or ``None``.

    ``supported`` maps a protocol major to the highest minor this side knows;
    it defaults to the one in this module.
    """
    supported = supported or {PROTOCOL_MAJOR: PROTOCOL_MINOR}
    best = None
    for entry in offered or ():
        parsed = _version_entry(entry)
        if parsed is None or parsed[0] not in supported:
            continue
        candidate = (parsed[0], min(parsed[1], supported[parsed[0]]))
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def negotiate_kind(offered, kind, accepted):
    """Pick the kind API to serve from the worker's offer, or ``None``.

    ``accepted`` is a list of ``(major, minor)`` in the host's order of
    preference. Returns ``{"kind", "major", "minor"}`` with the lower minor.
    """
    offers = {}
    for entry in offered or ():
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            continue
        parsed = _version_entry(entry)
        if parsed is not None:
            offers[parsed[0]] = max(offers.get(parsed[0], 0), parsed[1])
    for major, minor in accepted:
        if major in offers:
            return {"kind": kind, "major": major, "minor": min(minor, offers[major])}
    return None


def wire_version(abi):
    """1 for the v1 line protocol, 2 for this one.

    ``abi`` is what a manifest or runtime spec declares. A missing value means
    v1, which keeps every existing provider worker where it is.
    """
    if abi is None or abi == V1_ABI:
        return 1
    if abi == V2_ABI:
        return 2
    raise ValueError("unknown worker protocol %r" % (abi,))
