# coding=utf-8
"""Host half of worker protocol v2: one session per worker process.

A ``WorkerSession`` owns one worker process. It starts it, runs the handshake,
multiplexes requests by id, answers the worker's calls back into the host
(reserve, usage, checkpoint and so on), cancels, watches the heartbeat and
tears the whole process group down when anything goes wrong.

Threads: one reader (frames from the worker), one writer (the only thread
that writes to the worker's stdin; everything else queues), one watchdog
(heartbeat, write deadline, request deadlines, cancel grace), one thread per
request with host calls pending, one stderr drain. Public
methods are safe to call from any thread. Callbacks (``on_progress``,
``on_event``, ``on_stderr``) run on the reader or drain thread and must not
block.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from secret_store.scrub import scrub_text

from . import frames
from .errors import (
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
from .frames import Codes, FrameError

logger = logging.getLogger(__name__)

_SIGKILL = getattr(signal, "SIGKILL", None)
_SIGTERM = getattr(signal, "SIGTERM", None)
_STDERR_LINE_LIMIT = 2000
# Read this much of a line before scrubbing, so a secret that straddles the
# 2000 character display cut is replaced whole first.
_STDERR_READ_LIMIT = 64 * 1024
_STDERR_LINES_PER_SECOND = 100


@dataclass(frozen=True)
class SessionLimits:
    """Tunables for one session. The defaults are the production values."""

    handshake_timeout: float = 10.0
    heartbeat_interval: float = frames.DEFAULT_HEARTBEAT_INTERVAL
    # The worker is dead if no frame arrives within this long.
    heartbeat_timeout: float = frames.DEFAULT_HEARTBEAT_INTERVAL * frames.DEFAULT_HEARTBEAT_MISSES
    cancel_grace: float = 5.0
    kill_grace: float = 5.0
    # close() asks with a shutdown frame for the caller's grace, then SIGTERM,
    # waits this long, then SIGKILL.
    term_grace: float = 1.0
    # The worker is killed if one write to its stdin takes longer than this
    # (it stopped reading), or if this much is queued for it.
    write_timeout: float = 10.0
    max_queued_frames: int = 256
    max_queued_bytes: int = 8 * 1024 * 1024
    # Worker calls waiting for a host handler, over the whole session.
    max_pending_calls: int = 64
    # Chunks one result may arrive in; honest senders use big chunks.
    max_result_chunks: int = 4096
    # Distinct secret values one worker may be sent over its life. Every one is
    # kept for redaction (a late stderr line can name any of them); past this the
    # session asks to be replaced rather than forget one.
    max_secret_values: int = 512
    max_in_flight: int = frames.DEFAULT_MAX_IN_FLIGHT
    max_result_bytes: int = frames.DEFAULT_MAX_RESULT_BYTES
    # Watchdog resolution. Tests lower it.
    tick: float = 0.25


@dataclass
class Welcome:
    """What the handshake settled."""

    protocol: tuple
    kind: dict | None
    worker: dict
    max_in_flight: int
    pid: int | None


@dataclass
class RequestResult:
    result: dict
    data: bytes = b""


@dataclass
class HostCall:
    """A worker-to-host call, as a host call handler sees it."""

    name: str
    params: dict
    request_id: int | None
    session: "WorkerSession"


class _Request:
    def __init__(self, req_id, op, deadline, max_result_bytes, on_progress, secrets):
        self.id = req_id
        self.op = op
        self.deadline = deadline
        self.max_result_bytes = max_result_bytes
        self.on_progress = on_progress
        self.secrets = [str(v) for v in (secrets or {}).values() if isinstance(v, str) and v]
        self.chunks: list[bytes] = []
        self.received = 0
        self.overflow = False
        self.cancel_sent_at: float | None = None
        self.cancel_reason: str | None = None
        self.timed_out = False
        self.done = threading.Event()
        self.outcome: RequestResult | None = None
        self.error: BaseException | None = None


class RequestHandle:
    """A request in flight. ``wait`` returns the result or raises."""

    def __init__(self, session, request):
        self._session = session
        self._request = request

    @property
    def id(self):
        return self._request.id

    def done(self):
        return self._request.done.is_set()

    def cancel(self, reason="cancelled"):
        self._session._cancel(self._request, reason)

    def wait(self, timeout=None):
        if not self._request.done.wait(timeout):
            raise TimeoutError("request %d is still running" % self._request.id)
        if self._request.error is not None:
            raise self._request.error
        return self._request.outcome


def _default_env():
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _signal_group(process, sig):
    killpg = getattr(os, "killpg", None)
    if killpg is None or sig is None:
        return False
    try:
        killpg(process.pid, sig)
    except OSError:
        return False
    return True


class WorkerSession:
    """One v2 worker process and the conversation with it.

    ``spawn`` is a callable returning a ``subprocess.Popen`` with binary pipes
    on stdin, stdout and stderr. The default starts ``command`` in its own
    session. The launcher in the sandbox slice plugs in here.
    """

    def __init__(
        self,
        command=None,
        *,
        spawn: Callable[[], subprocess.Popen] | None = None,
        cwd=None,
        env: dict | None = None,
        kind: str | None = None,
        kind_versions=((1, 0),),
        limits: SessionLimits | None = None,
        host_calls: dict | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        name: str = "worker",
    ):
        if spawn is None and not command:
            raise ValueError("a command or a spawn callable is required")
        self._command = command
        self._spawn = spawn
        self._cwd = str(cwd) if cwd else None
        self._env = env
        self.kind = kind
        self._kind_versions = tuple(kind_versions)
        self.limits = limits or SessionLimits()
        self._host_calls = dict(host_calls or {})
        self._on_event = on_event
        self._on_stderr = on_stderr
        self.name = name

        self.process: subprocess.Popen | None = None
        self.welcome: Welcome | None = None
        self.exit_code: int | None = None
        self.last_heartbeat: dict | None = None
        self.last_frame_at = time.monotonic()

        self._lock = threading.RLock()
        self._requests: dict[int, _Request] = {}
        self._next_id = 1
        self._closed = False
        self._failure: BaseException | None = None
        self._hello = threading.Event()
        self._handshake_error: BaseException | None = None
        self._reader: frames.FrameReader | None = None
        self._threads: list[threading.Thread] = []
        # Every host write goes through the outbox and one writer thread, so no
        # caller (the watchdog and close() least of all) ever waits on a worker
        # that has stopped reading.
        self._outbox: "queue.Queue[bytes]" = queue.Queue()
        self._outbox_bytes = 0
        self._write_started: float | None = None
        self._call_queues: dict[Any, deque] = {}
        self._call_pending = 0
        self._stop = threading.Event()
        self._stderr_window = (0.0, 0)
        self._bye = threading.Event()
        # Secret values seen in requests, kept after the request ends: its last
        # stderr lines can reach the drain thread after the reply reached the caller.
        self._secret_values: set[str] = set()
        self._secrets_by_length: tuple = ()
        self.recycle_needed = False

    # ------------------------------------------------------------------ setup

    def register_host_call(self, name, handler):
        """Register ``handler(call: HostCall) -> dict`` for a worker call name."""
        with self._lock:
            self._host_calls[name] = handler

    @property
    def alive(self):
        return self.process is not None and not self._closed and self.process.poll() is None

    @property
    def in_flight(self):
        with self._lock:
            return len(self._requests)

    @property
    def rss_kb(self):
        heartbeat = self.last_heartbeat or {}
        return heartbeat.get("rss_kb")

    def start(self) -> Welcome:
        """Spawn the worker and run the handshake. Raises ``HandshakeError``."""
        with self._lock:
            if self.process is not None:
                raise RuntimeError("session already started")
            if self._spawn is not None:
                process = self._spawn()
            else:
                env = _default_env()
                if self._env:
                    env.update(self._env)
                process = subprocess.Popen(
                    self._command,
                    cwd=self._cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            self.process = process
        handshake_caps = {frames.FRAME_CONTROL: frames.HANDSHAKE_CONTROL_BYTES}
        self._reader = frames.FrameReader(process.stdout, handshake_caps)
        self.last_frame_at = time.monotonic()
        for target, label in (
            (self._read_loop, "reader"),
            (self._drain_stderr, "stderr"),
            (self._write_loop, "writer"),
        ):
            thread = threading.Thread(target=target, name="%s-%s" % (self.name, label), daemon=True)
            thread.start()
            self._threads.append(thread)

        if self._hello.wait(self.limits.handshake_timeout):
            error = self._handshake_error
        else:
            error = HandshakeError("worker sent no hello within %.1fs" % self.limits.handshake_timeout)
        if error is not None:
            self._fail(error, kill=True)
            raise error
        watchdog = threading.Thread(target=self._watchdog, name="%s-watchdog" % self.name, daemon=True)
        watchdog.start()
        self._threads.append(watchdog)
        return self.welcome

    # --------------------------------------------------------------- requests

    def submit(self, op, params=None, *, timeout=None, max_result_bytes=None, secrets=None, on_progress=None):
        """Send a request and return its handle. Raises ``SessionBusy`` at the limit."""
        limit = self.limits.max_in_flight
        if self.welcome is not None:
            limit = min(limit, self.welcome.max_in_flight)
        cap = self.limits.max_result_bytes if max_result_bytes is None else min(
            int(max_result_bytes), self.limits.max_result_bytes)
        with self._lock:
            if self._closed or self.welcome is None:
                raise self._failure or SessionClosed("session is not running")
            if len(self._requests) >= limit:
                raise SessionBusy("worker has %d requests in flight" % len(self._requests))
            req_id = self._next_id
            deadline = time.monotonic() + float(timeout) if timeout is not None else None
            request = _Request(req_id, op, deadline, cap, on_progress, secrets)
            fresh = [value for value in request.secrets if value not in self._secret_values]
            if len(self._secret_values) + len(set(fresh)) > self.limits.max_secret_values:
                self.recycle_needed = True
                raise SessionError(
                    "worker has been sent %d distinct secrets; start a new one" % len(self._secret_values),
                    code=Codes.RECYCLE_REQUIRED)
            if fresh:
                self._secret_values.update(fresh)
                self._secrets_by_length = tuple(sorted(self._secret_values, key=len, reverse=True))
            self._next_id += 1
            self._requests[req_id] = request
        message = {
            "t": frames.REQ,
            "id": req_id,
            "op": op,
            "params": params or {},
            "max_result_bytes": cap,
        }
        if timeout is not None:
            message["deadline_ms"] = int(float(timeout) * 1000)
        if secrets:
            message["secrets"] = secrets
        try:
            queued = self._enqueue(message)
        except (FrameError, ValueError, RecursionError):
            with self._lock:
                self._requests.pop(req_id, None)
            raise
        if not queued:
            raise self._failure or SessionClosed("session is not running")
        return RequestHandle(self, request)

    def request(self, op, params=None, *, timeout=None, **kwargs):
        """Submit and wait. ``timeout`` is the deadline for the whole request."""
        return self.submit(op, params, timeout=timeout, **kwargs).wait()

    def _cancel(self, request, reason):
        with self._lock:
            if request.done.is_set() or request.cancel_sent_at is not None:
                return
            request.cancel_sent_at = time.monotonic()
            request.cancel_reason = reason
        self._send_quietly({"t": frames.CANCEL, "id": request.id, "reason": reason})

    # ----------------------------------------------------------------- reading

    def _read_loop(self):
        try:
            self._read_frames()
        finally:
            self._close_stream(self.process.stdout)

    @staticmethod
    def _close_stream(stream):
        # Closed by the thread that uses the stream: closing a pipe another
        # thread is blocked on would wait on that thread's lock.
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass

    def _read_frames(self):
        reader = self._reader
        welcomed = False
        try:
            while not self._stop.is_set():
                frame = reader.read_frame()
                if frame is None:
                    break
                ftype, body = frame
                if ftype == frames.FRAME_DATA:
                    if not welcomed:
                        raise FrameError("data frame before the handshake finished")
                    op_id, chunk = frames.split_data(body)
                    if not chunk:
                        raise FrameError("empty data chunk")
                    # Only data a running request is waiting for counts as proof of life.
                    if self._on_data(op_id, chunk):
                        self.last_frame_at = time.monotonic()
                    continue
                self.last_frame_at = time.monotonic()
                message = frames.decode_control(body, "worker")
                if not welcomed:
                    if message["t"] != frames.HELLO:
                        raise FrameError("first message was %s, not hello" % message["t"])
                    self._on_hello(message)
                    welcomed = True
                    reader.caps.update(frames.default_caps())
                    continue
                if self._on_message(message):
                    return
        except FrameError as error:
            if not welcomed and getattr(error, "type_byte", None) is not None:
                code = error.code
                failure = HandshakeError(
                    "worker spoke an unknown protocol (%s)" % error, code=code)
                self._handshake_error = failure
                self._hello.set()
                return
            if not welcomed:
                self._handshake_error = HandshakeError(str(error), code=error.code)
                self._hello.set()
                return
            self._fail(ProtocolViolation(str(error), code=error.code), kill=True)
            return
        except (OSError, ValueError):
            pass
        except Exception as error:
            # Whatever else a hostile frame provokes. The reader must never die silently.
            detail = "worker frame could not be processed: %s" % type(error).__name__
            if not welcomed:
                self._handshake_error = HandshakeError(detail)
                self._hello.set()
                return
            self._fail(ProtocolViolation(detail), kill=True)
            return
        # End of stream, or the pipe broke.
        exit_code = self._reap_exit_code()
        if not welcomed:
            self._handshake_error = HandshakeError("worker exited before hello (exit code %s)" % exit_code)
            self._hello.set()
            return
        if self._bye.is_set() or self._closed:
            # Always end and reap the group: the leader may still be alive after
            # the stream closed, and anything it forked has to go with it.
            self._fail(SessionClosed("worker exited"), kill=True)
        else:
            self._fail(WorkerExited("worker exited (exit code %s)" % exit_code, exit_code=exit_code), kill=True)

    def _on_hello(self, hello):
        protocol = frames.negotiate_protocol(hello["protocols"])
        if protocol is None:
            self._reject(Codes.VERSION_MISMATCH, "no protocol major in common")
            return
        kind = None
        if self.kind is not None:
            kind = frames.negotiate_kind(hello["kinds"], self.kind, self._kind_versions)
            if kind is None:
                self._reject(Codes.VERSION_MISMATCH, "worker does not serve a usable %s API" % self.kind)
                return
        limits = {
            "heartbeat_interval_ms": int(self.limits.heartbeat_interval * 1000),
            "max_in_flight": self.limits.max_in_flight,
            "max_result_bytes": self.limits.max_result_bytes,
            "max_control_bytes": frames.MAX_CONTROL_BYTES,
            "max_data_bytes": frames.MAX_DATA_BYTES,
        }
        welcome = {
            "t": frames.WELCOME,
            "protocol": {"major": protocol[0], "minor": protocol[1]},
            "kind": kind,
            "limits": limits,
            "session": "%s-%d" % (self.name, os.getpid()),
        }
        self.welcome = Welcome(
            protocol=protocol,
            kind=kind,
            worker=hello.get("worker") or {},
            max_in_flight=hello.get("max_in_flight") or self.limits.max_in_flight,
            pid=hello.get("pid"),
        )
        self._enqueue(welcome)
        self._hello.set()

    def _reject(self, code, message):
        self._send_quietly({"t": frames.REJECT, "code": code, "message": message})
        self._handshake_error = HandshakeError(message, code=code)
        self._hello.set()

    def _on_message(self, message):
        """Handle one control message. True means the reader should stop."""
        kind = message["t"]
        if kind == frames.HB:
            self.last_heartbeat = message
        elif kind == frames.PROGRESS:
            request = self._get(message["id"])
            if request is not None and request.on_progress is not None:
                try:
                    request.on_progress(self._redact_obj(message))
                except Exception:
                    logger.debug("progress callback failed", exc_info=True)
        elif kind == frames.RES:
            self._on_result(message)
        elif kind == frames.CALL:
            self._on_call(message)
        elif kind == frames.SANDBOX_BLOCKED:
            self._emit("sandbox_blocked", message)
        elif kind == frames.BYE:
            self._bye.set()
        elif kind == frames.HELLO:
            raise FrameError("hello sent twice")
        return False

    def _get(self, req_id):
        with self._lock:
            return self._requests.get(req_id)

    def _on_data(self, op_id, chunk):
        with self._lock:
            request = self._requests.get(op_id)
        if request is None or request.overflow:
            return False
        request.received += len(chunk)
        if request.received > request.max_result_bytes or len(request.chunks) >= self.limits.max_result_chunks:
            request.overflow = True
            request.chunks = []
            self._cancel(request, Codes.RESULT_TOO_LARGE)
            return False
        request.chunks.append(chunk)
        return True

    def _on_result(self, message):
        with self._lock:
            request = self._requests.pop(message["id"], None)
        if request is None:
            logger.debug("%s: result for unknown request %s ignored", self.name, message["id"])
            return
        if request.overflow:
            self._finish(request, error=ResultTooLarge(
                "result exceeded %d bytes" % request.max_result_bytes))
            return
        if not message["ok"]:
            error = message["error"]
            code = error.get("code") if isinstance(error.get("code"), str) else Codes.PLUGIN_ERROR
            if code == Codes.CANCELLED and request.timed_out:
                self._finish(request, error=RequestTimeout("request exceeded its deadline"))
            elif code == Codes.CANCELLED:
                self._finish(request, error=RequestCancelled(request.cancel_reason or "cancelled"))
            else:
                self._finish(request, error=self._failed(error, code, request))
            return
        data = b"".join(request.chunks)
        declared = message.get("bytes")
        problem = None
        if declared is not None and declared != len(data):
            problem = "result declared %d bytes and sent %d" % (declared, len(data))
        elif message.get("chunks") is not None and message["chunks"] != len(request.chunks):
            problem = "result declared %d chunks and sent %d" % (message["chunks"], len(request.chunks))
        elif message.get("sha256") is not None and message["sha256"] != hashlib.sha256(data).hexdigest():
            problem = "result checksum does not match its data"
        elif declared is None and data:
            problem = "data frames arrived without a byte count"
        elif data and message.get("sha256") is None:
            problem = "chunked result without a checksum"
        if problem:
            self._finish(request, error=ProtocolViolation(problem))
            return
        if request.timed_out:
            self._finish(request, error=RequestTimeout("request exceeded its deadline"))
            return
        self._finish(request, outcome=RequestResult(message.get("result") or {}, data))

    def _failed(self, error, code, request):
        def clean(value):
            return self._redact(value) if isinstance(value, str) else None

        retry_after = error.get("retry_after")
        if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
            retry_after = None
        return RequestFailed(
            clean(error.get("message")) or code,
            code=code,
            retryable=error.get("retryable") is True,
            retry_after=retry_after,
            class_name=clean(error.get("class_name")),
            traceback=clean(error.get("traceback")),
            errno=error.get("errno") if isinstance(error.get("errno"), int) else None,
        )

    def _finish(self, request, outcome=None, error=None):
        request.outcome = outcome
        request.error = error
        request.chunks = []
        request.done.set()

    # ------------------------------------------------------------ host calls
    #
    # Ordering guarantee: calls that belong to one request are handled one at a
    # time, in the order the worker sent them. Calls of different requests run
    # concurrently, each request on its own daemon thread, so a slow handler
    # for one request never delays another request's calls. Handlers therefore
    # must be thread safe. At most ``max_pending_calls`` calls may wait in the
    # whole session; a worker that exceeds that is flooding and is killed.

    def _on_call(self, message):
        key = message.get("id")
        refused = None
        start = False
        with self._lock:
            if key is not None and key not in self._requests:
                refused = frames.error_object(Codes.INVALID_REQUEST, "call for a request that is not running")
            elif self._call_pending >= self.limits.max_pending_calls:
                refused = False
            else:
                self._call_pending += 1
                pending = self._call_queues.get(key)
                if pending is None:
                    pending = self._call_queues[key] = deque()
                    start = True
                pending.append(message)
        if refused is False:
            self._fail(ProtocolViolation("worker has %d host calls pending" % self.limits.max_pending_calls), kill=True)
        elif refused is not None:
            self._send_quietly({"t": frames.ACK, "cid": message["cid"], "ok": False, "error": refused})
        elif start:
            threading.Thread(target=self._call_worker, args=(key,), daemon=True,
                             name="%s-calls-%s" % (self.name, key)).start()

    def _call_worker(self, key):
        while not self._stop.is_set():
            with self._lock:
                pending = self._call_queues.get(key)
                if not pending:
                    self._call_queues.pop(key, None)
                    return
                message = pending.popleft()
            answer = self._answer_call(message)
            # Counted until the handler is done: a handler that blocks keeps its
            # slot, so a worker cannot stack blocked handler threads past the cap.
            with self._lock:
                if not self._stop.is_set():
                    self._call_pending = max(0, self._call_pending - 1)
            self._send_quietly(answer)

    def _answer_call(self, message):
        name = message["name"]
        cid = message["cid"]
        with self._lock:
            handler = self._host_calls.get(name)
        if handler is None:
            return {"t": frames.ACK, "cid": cid, "ok": False,
                    "error": frames.error_object(Codes.UNKNOWN_CALL, "no host call named %s" % name)}
        try:
            result = handler(HostCall(name, message["params"], message.get("id"), self))
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise TypeError("host call %s returned %s, not a dict" % (name, type(result).__name__))
            return {"t": frames.ACK, "cid": cid, "ok": True, "result": result}
        except HostCallError as error:
            return {"t": frames.ACK, "cid": cid, "ok": False,
                    "error": frames.error_object(error.code, self._redact(error.message),
                                                 retryable=error.retryable)}
        except Exception as error:
            # Logged redacted and without the raw exception, which may carry a secret.
            logger.warning("%s: host call %s failed: %s", self.name, name,
                           self._redact("%s: %s" % (type(error).__name__, error)))
            return {"t": frames.ACK, "cid": cid, "ok": False,
                    "error": frames.error_object(Codes.HOST_CALL_FAILED, self._redact(str(error)))}

    # --------------------------------------------------------------- watchdog

    def _watchdog(self):
        limits = self.limits
        while not self._stop.wait(limits.tick):
            now = time.monotonic()
            if now - self.last_frame_at > limits.heartbeat_timeout:
                self._fail(HeartbeatTimeout(
                    "no frame from the worker for %.1fs" % (now - self.last_frame_at)), kill=True)
                return
            started = self._write_started
            if started is not None and now - started > limits.write_timeout:
                self._fail(SessionError(
                    "worker has not read its input for %.1fs" % (now - started),
                    code=Codes.WRITE_TIMEOUT), kill=True)
                return
            with self._lock:
                pending = list(self._requests.values())
            for request in pending:
                if request.done.is_set():
                    continue
                if request.cancel_sent_at is not None:
                    if now - request.cancel_sent_at > limits.cancel_grace:
                        # The worker ignored the cancel. Only ending it ends the work.
                        if request.overflow:
                            error = ResultTooLarge("result exceeded %d bytes" % request.max_result_bytes)
                        elif request.timed_out:
                            error = RequestTimeout("request exceeded its deadline")
                        else:
                            error = RequestCancelled(request.cancel_reason or "cancelled")
                        self._fail(error, kill=True, only=request)
                        return
                elif request.deadline is not None and now >= request.deadline:
                    request.timed_out = True
                    self._cancel(request, Codes.TIMEOUT)

    # ------------------------------------------------------------- lifecycle

    def _poll(self):
        return self.process.poll() if self.process is not None else None

    def _reap_exit_code(self):
        process = self.process
        if process is None:
            return None
        try:
            self.exit_code = process.wait(timeout=1.0)
        except Exception:
            self.exit_code = None
        return self.exit_code

    def _enqueue(self, message):
        """Queue a control frame for the writer thread. Never blocks.

        Returns False if the session is already closed. Raises
        ``FrameTooLarge`` for a message over the control cap. A full outbox
        means the worker stopped reading: the group is killed.
        """
        data = frames.pack_control(message)
        full = False
        with self._lock:
            if self._closed:
                return False
            if (self._outbox.qsize() >= self.limits.max_queued_frames
                    or self._outbox_bytes + len(data) > self.limits.max_queued_bytes):
                full = True
            else:
                self._outbox_bytes += len(data)
                self._outbox.put_nowait(data)
        if full:
            self._fail(SessionError("worker is not reading its input", code=Codes.WRITE_TIMEOUT), kill=True)
            return False
        return True

    def _send_quietly(self, message):
        try:
            return self._enqueue(message)
        except (FrameError, ValueError, RecursionError):
            return False

    def _write_loop(self):
        stream = self.process.stdin
        try:
            while not self._stop.is_set():
                try:
                    data = self._outbox.get(timeout=0.2)
                except queue.Empty:
                    continue
                with self._lock:
                    self._outbox_bytes -= len(data)
                self._write_started = time.monotonic()
                try:
                    stream.write(data)
                    stream.flush()
                except (OSError, ValueError) as error:
                    self._write_started = None
                    self._fail(WorkerExited("worker pipe closed: %s" % error, exit_code=self._poll()), kill=True)
                    return
                self._write_started = None
        finally:
            self._close_stream(stream)

    def _emit(self, name, payload):
        callback = self._on_event
        if callback is None:
            return
        try:
            callback(name, payload)
        except Exception:
            logger.debug("event callback failed", exc_info=True)

    def _fail(self, error, kill, only=None):
        """End the session (or just ``only``'s request) with ``error``.

        Killing the worker is how every unrecoverable case ends: a worker that
        broke the contract, went silent or ignored a cancel cannot be trusted
        to finish the work it holds.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._failure = error if isinstance(error, SessionError) else _as_session_error(error)
            pending = list(self._requests.values())
            self._requests.clear()
        self._stop.set()
        with self._lock:
            self._call_queues.clear()
            self._call_pending = 0
        process = self.process
        if kill and process is not None:
            self._kill_tree(process)
        for request in pending:
            if request.done.is_set():
                continue
            if only is not None and request is not only:
                self._finish(request, error=_as_session_error(error))
            else:
                self._finish(request, error=error)

    def _kill_tree(self, process):
        if process.poll() is None and not _signal_group(process, _SIGKILL):
            try:
                process.kill()
            except OSError:
                pass
        try:
            self.exit_code = process.wait(timeout=self.limits.kill_grace)
        except Exception:
            logger.warning("%s: worker survived a kill", self.name)
        else:
            # What the worker forked dies with its group.
            _signal_group(process, _SIGKILL)

    def close(self, grace=5.0):
        """Ask the worker to stop, then end its whole group. Idempotent.

        The shutdown frame is only a request: it is queued, never awaited. If
        the worker has not exited after ``grace`` it gets SIGTERM, then after
        ``term_grace`` SIGKILL for the group, whether or not it was reading.
        """
        process = self.process
        with self._lock:
            already = self._closed
        if not already and process is not None and process.poll() is None:
            self._send_quietly({"t": frames.SHUTDOWN, "grace_ms": int(grace * 1000)})
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                if not _signal_group(process, _SIGTERM):
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=self.limits.term_grace)
                except subprocess.TimeoutExpired:
                    pass
        self._fail(SessionClosed("session closed"), kill=process is not None)
        if process is not None:
            # The reader may have closed the session first on a clean exit;
            # anything the worker forked still has to go.
            self._kill_tree(process)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)

    # ----------------------------------------------------------------- stderr

    def _drain_stderr(self):
        stream = self.process.stderr
        if stream is None:
            return
        try:
            while True:
                raw = stream.readline(_STDERR_READ_LIMIT + 1)
                if not raw:
                    return
                cut = len(raw) > _STDERR_READ_LIMIT and not raw.endswith(b"\n")
                if cut:
                    # Skip the rest of an overlong line.
                    while True:
                        more = stream.readline(65536)
                        if not more or more.endswith(b"\n"):
                            break
                if not self._stderr_allowed():
                    continue
                text = raw[:_STDERR_READ_LIMIT].decode("utf-8", "replace").rstrip("\r\n")
                if cut:
                    # The cut may have split a secret; drop a secret's length of tail.
                    text = text[:-max([len(v) for v in self._secrets_snapshot()] or [0])]
                # Scrub first, truncate second: a secret that straddles the
                # display limit must be replaced whole, not cut into a prefix.
                line = self._redact(text)[:_STDERR_LINE_LIMIT]
                if not line:
                    continue
                callback = self._on_stderr
                if callback is not None:
                    try:
                        callback(line)
                    except Exception:
                        pass
                else:
                    logger.debug("%s stderr: %s", self.name, line)
        except (OSError, ValueError):
            return
        finally:
            self._close_stream(stream)

    def _stderr_allowed(self):
        second = int(time.monotonic())
        window, count = self._stderr_window
        if second != window:
            self._stderr_window = (second, 1)
            return True
        if count >= _STDERR_LINES_PER_SECOND:
            return False
        self._stderr_window = (window, count + 1)
        return True

    def _secrets_snapshot(self):
        with self._lock:
            return self._secrets_by_length

    def _redact(self, text):
        """Replace every secret value this session has carried, then the generic patterns."""
        for secret in self._secrets_snapshot():
            text = text.replace(secret, "***")
        return scrub_text(text)

    def _redact_obj(self, value):
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._redact_obj(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_obj(item) for key, item in value.items()}
        return value


def _as_session_error(error):
    """The error the requests that were not the cause see."""
    return SessionClosed("worker was ended while this request ran")
