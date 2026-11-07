# coding=utf-8
"""Host half of worker protocol v2: one session per worker process.

A ``WorkerSession`` owns one worker process. It starts it, runs the handshake,
multiplexes requests by id, answers the worker's calls back into the host
(reserve, usage, checkpoint and so on), cancels, watches the heartbeat and
tears the whole process group down when anything goes wrong.

Threads: one reader (frames from the worker), one watchdog (heartbeat,
deadlines, cancel grace), one host call dispatcher, one stderr drain. Public
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
_STDERR_LINE_LIMIT = 2000
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
        self._writer: frames.FrameWriter | None = None
        self._reader: frames.FrameReader | None = None
        self._threads: list[threading.Thread] = []
        self._calls: "queue.Queue[Any]" = queue.Queue()
        self._stop = threading.Event()
        self._stderr_window = (0.0, 0)
        self._bye = threading.Event()
        # Secret values seen in requests, kept after the request ends: its last
        # stderr lines can reach the drain thread after the reply reached the caller.
        self._secret_values: list[str] = []

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
        self._writer = frames.FrameWriter(process.stdin)
        self.last_frame_at = time.monotonic()
        for target, label in (
            (self._read_loop, "reader"),
            (self._drain_stderr, "stderr"),
            (self._call_loop, "calls"),
        ):
            thread = threading.Thread(target=target, name="%s-%s" % (self.name, label), daemon=True)
            thread.start()
            self._threads.append(thread)

        if not self._hello.wait(self.limits.handshake_timeout):
            self._fail(HandshakeError("worker sent no hello within %.1fs" % self.limits.handshake_timeout), kill=True)
        if self._handshake_error is not None:
            self._fail(self._handshake_error, kill=True)
            raise self._handshake_error
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
            self._next_id += 1
            deadline = time.monotonic() + float(timeout) if timeout is not None else None
            request = _Request(req_id, op, deadline, cap, on_progress, secrets)
            self._requests[req_id] = request
            for value in request.secrets:
                if value not in self._secret_values:
                    self._secret_values.append(value)
            del self._secret_values[:-64]
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
            self._writer.write_control(message)
        except FrameError:
            with self._lock:
                self._requests.pop(req_id, None)
            raise
        except (OSError, ValueError) as error:
            self._fail(WorkerExited("worker pipe closed: %s" % error, exit_code=self._poll()), kill=True)
            raise self._failure from error
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
        reader = self._reader
        welcomed = False
        try:
            while not self._stop.is_set():
                frame = reader.read_frame()
                if frame is None:
                    break
                self.last_frame_at = time.monotonic()
                ftype, body = frame
                if ftype == frames.FRAME_DATA:
                    if not welcomed:
                        raise FrameError("data frame before the handshake finished")
                    self._on_data(*frames.split_data(body))
                    continue
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
            failure = ProtocolViolation(str(error), code=error.code)
            if not welcomed:
                self._handshake_error = failure
                self._hello.set()
                return
            self._fail(failure, kill=True)
            return
        except (OSError, ValueError):
            pass
        # End of stream, or the pipe broke.
        exit_code = self._reap_exit_code()
        if not welcomed:
            self._handshake_error = HandshakeError("worker exited before hello (exit code %s)" % exit_code)
            self._hello.set()
            return
        if self._bye.is_set() or self._closed:
            self._fail(SessionClosed("worker exited"), kill=False)
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
        self._writer.write_control(welcome)
        self._hello.set()

    def _reject(self, code, message):
        try:
            self._writer.write_control({"t": frames.REJECT, "code": code, "message": message})
        except (OSError, FrameError):
            pass
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
                    request.on_progress(message)
                except Exception:
                    logger.debug("progress callback failed", exc_info=True)
        elif kind == frames.RES:
            self._on_result(message)
        elif kind == frames.CALL:
            self._calls.put(message)
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
            return
        request.received += len(chunk)
        if request.received > request.max_result_bytes:
            request.overflow = True
            request.chunks = []
            self._cancel(request, Codes.RESULT_TOO_LARGE)
            return
        request.chunks.append(chunk)

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
        if problem:
            self._finish(request, error=ProtocolViolation(problem))
            return
        if request.timed_out:
            self._finish(request, error=RequestTimeout("request exceeded its deadline"))
            return
        self._finish(request, outcome=RequestResult(message.get("result") or {}, data))

    def _failed(self, error, code, request):
        def clean(value):
            return scrub_text(value) if isinstance(value, str) else None

        retry_after = error.get("retry_after")
        if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
            retry_after = None
        return RequestFailed(
            clean(error.get("message")) or code,
            code=code,
            retryable=error.get("retryable") is True,
            retry_after=retry_after,
            class_name=error.get("class_name") if isinstance(error.get("class_name"), str) else None,
            traceback=clean(error.get("traceback")),
            errno=error.get("errno") if isinstance(error.get("errno"), int) else None,
        )

    def _finish(self, request, outcome=None, error=None):
        request.outcome = outcome
        request.error = error
        request.chunks = []
        request.done.set()

    # ------------------------------------------------------------ host calls

    def _call_loop(self):
        while True:
            message = self._calls.get()
            if message is None:
                return
            name = message["name"]
            with self._lock:
                handler = self._host_calls.get(name)
            if handler is None:
                ack = {"t": frames.ACK, "cid": message["cid"], "ok": False,
                       "error": frames.error_object(Codes.UNKNOWN_CALL, "no host call named %s" % name)}
            else:
                try:
                    result = handler(HostCall(name, message["params"], message.get("id"), self))
                    if result is None:
                        result = {}
                    if not isinstance(result, dict):
                        raise TypeError("host call %s returned %s, not a dict" % (name, type(result).__name__))
                    ack = {"t": frames.ACK, "cid": message["cid"], "ok": True, "result": result}
                except HostCallError as error:
                    ack = {"t": frames.ACK, "cid": message["cid"], "ok": False,
                           "error": frames.error_object(error.code, scrub_text(error.message),
                                                        retryable=error.retryable)}
                except Exception as error:
                    logger.warning("%s: host call %s failed", self.name, name, exc_info=True)
                    ack = {"t": frames.ACK, "cid": message["cid"], "ok": False,
                           "error": frames.error_object(Codes.HOST_CALL_FAILED, scrub_text(str(error)))}
            if not self._send_quietly(ack):
                return

    # --------------------------------------------------------------- watchdog

    def _watchdog(self):
        limits = self.limits
        while not self._stop.wait(limits.tick):
            now = time.monotonic()
            if now - self.last_frame_at > limits.heartbeat_timeout:
                self._fail(HeartbeatTimeout(
                    "no frame from the worker for %.1fs" % (now - self.last_frame_at)), kill=True)
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

    def _send_quietly(self, message):
        writer = self._writer
        if writer is None or self._closed:
            return False
        try:
            writer.write_control(message)
            return True
        except (OSError, ValueError, FrameError):
            return False

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
        self._calls.put(None)
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
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def close(self, grace=5.0):
        """Ask the worker to stop, then end its whole group. Idempotent."""
        process = self.process
        with self._lock:
            already = self._closed
        if not already and process is not None and process.poll() is None:
            self._send_quietly({"t": frames.SHUTDOWN, "grace_ms": int(grace * 1000)})
            try:
                process.wait(timeout=grace)
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
                raw = stream.readline(_STDERR_LINE_LIMIT + 1)
                if not raw:
                    return
                if len(raw) > _STDERR_LINE_LIMIT and not raw.endswith(b"\n"):
                    # Skip the rest of an overlong line.
                    while True:
                        more = stream.readline(65536)
                        if not more or more.endswith(b"\n"):
                            break
                if not self._stderr_allowed():
                    continue
                line = self._scrub(raw[:_STDERR_LINE_LIMIT].decode("utf-8", "replace").rstrip("\r\n"))
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

    def _scrub(self, line):
        with self._lock:
            secrets = list(self._secret_values)
        for secret in secrets:
            line = line.replace(secret, "***")
        return scrub_text(line)


def _as_session_error(error):
    """The error the requests that were not the cause see."""
    return SessionClosed("worker was ended while this request ran")
