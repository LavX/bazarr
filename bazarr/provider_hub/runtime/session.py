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
import math
import os
import queue
import re
import signal
import subprocess
import sys
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
# What the host keeps of worker-supplied text, applied after redaction (the
# same bounds the worker applies to its own error fields).
_MESSAGE_LIMIT = 4096
_TRACEBACK_LIMIT = 8000
_CLASS_NAME_LIMIT = 200
_CALL_NAME_LIMIT = 200
# Bounds for worker-supplied JSON the host hands on (progress, heartbeat,
# sandbox reports, the hello description).
_FIELD_LIMIT = 4096
_ITEM_LIMIT = 256
_DEPTH_LIMIT = 8
_CODE_SHAPE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Read this much of a line before scrubbing, so a secret that straddles the
# 2000 character display cut is replaced whole first.
_STDERR_READ_LIMIT = 64 * 1024
_STDERR_LINES_PER_SECOND = 100
# How long a queued reject may take to reach the worker before it is ended anyway.
_REJECT_FLUSH_WAIT = 2.0
# How long a second caller waits for a teardown that another thread is running, on
# top of the session's kill grace (``stop()`` bounds itself well inside this).
_HANDOVER_WAIT = 30.0


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
        self.secrets = list((secrets or {}).values())
        self.chunks: list[bytes] = []
        self.received = 0
        self.overflow = False
        self.cancel_sent_at: float | None = None
        self.cancel_reason: str | None = None
        self.timed_out = False
        self.done = threading.Event()
        self.outcome: RequestResult | None = None
        self.error: BaseException | None = None
        # Callbacks to run once the request has finished, and the lock that
        # decides, for each one, whether it runs now or at the finish.
        self.callbacks: list = []
        self.callback_lock = threading.Lock()


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

    @property
    def error(self):
        """The error the request ended with, or None (still running, or it succeeded)."""
        return self._request.error

    def add_done_callback(self, callback):
        """Call ``callback()`` once the request has finished, whether or not anybody waits.

        Runs at once, on the caller's thread, if it already finished; otherwise
        on a short-lived thread of its own after the finish, never under a session
        lock. An exception from a callback is logged and dropped.
        """
        request = self._request
        with request.callback_lock:
            if not request.done.is_set():
                request.callbacks.append(callback)
                return
        _run_callbacks([callback])

    def cancel(self, reason="cancelled"):
        self._session._cancel(self._request, reason)

    def wait(self, timeout=None):
        if not self._request.done.wait(timeout):
            raise TimeoutError("request %d is still running" % self._request.id)
        if self._request.error is not None:
            raise self._request.error
        return self._request.outcome


def _run_callbacks(callbacks):
    for callback in callbacks:
        try:
            callback()
        except Exception as error:
            # The type only: a message or traceback may carry a request's secret.
            logger.warning("a request completion callback failed (%s)", type(error).__name__)


_REASON_LIMIT = 200


_QUEUED, _CLOSED, _FULL = "queued", "closed", "full"


def _with_line_parts(values):
    """The secret values plus every nonempty line of a multiline one.

    Stderr and tracebacks are cut into lines, so a PEM key never appears whole
    in a log line; each of its lines has to be a secret of its own, at any
    length. A single-line secret is registered whole, with no minimum.
    """
    out = []
    for value in values:
        out.append(value)
        if "\n" in value or "\r" in value:
            out.extend(line.strip() for line in value.splitlines() if line.strip())
    return out


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


_RUNNING, _EXITED, _GONE = "running", "exited", "gone"


def _is_posix():
    """Where process groups, ``waitid`` and signal 0 mean what the teardown code assumes.

    On Windows ``os.kill`` terminates the process whatever the signal, there are
    no process groups to protect from a reused number, and reaping early is
    harmless, so none of the non-reaping logic below applies there.
    """
    return os.name == "posix"


def _has_proc():
    return sys.platform.startswith("linux")


def _zombie_or_gone(pid):
    """Without ``waitid``: ``running`` (maybe alive), ``exited`` (a zombie) or ``gone``, never reaping.

    ``kill(pid, 0)`` succeeds on a zombie, so on Linux the state in
    ``/proc/<pid>/stat`` says whether it is one. Elsewhere a zombie reads as
    running: the caller then ends the group anyway at its deadline.
    """
    if _has_proc():
        try:
            with open("/proc/%d/stat" % pid, "rb") as handle:
                state = handle.read().rsplit(b")", 1)[1].split()[0]
        except (OSError, IndexError):
            state = None
        if state in (b"Z", b"X"):
            return _EXITED
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return _GONE
    except OSError:
        return _RUNNING
    return _RUNNING


def _probe(process):
    """What the worker is doing, without reaping it: ``running``, ``exited`` or ``gone``.

    Nothing may collect the leader until its group has been dealt with: a
    zombie leader keeps its pid, and with it the group number, so signalling the
    group before the reap is always safe and afterwards never. So this never
    calls ``poll()``. It looks with ``waitid(WNOWAIT)``, or where that is
    unavailable or fails, with the zombie check above. ``gone`` means somebody
    else already collected the leader (``ECHILD``): the pid is no longer ours.
    """
    # A process that owns its reaps (a launcher, an anchored worker) answers under its
    # own lock: reading ``returncode`` here and asking ``waitid`` afterwards could be
    # overtaken by its own ``stop()`` and read as a reap by somebody else.
    own = getattr(process, "probe_exit", None)
    if callable(own):
        answer = own()
        if answer in (_RUNNING, _EXITED, _GONE):
            return answer
    if process.returncode is not None:
        return _EXITED
    if not _is_posix():
        # Never ``os.kill(pid, 0)`` here: on Windows that terminates the process.
        return _EXITED if process.poll() is not None else _RUNNING
    waitid = getattr(os, "waitid", None)
    flag = getattr(os, "WNOWAIT", None)
    if waitid is None or flag is None:
        return _zombie_or_gone(process.pid)
    try:
        found = waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | flag)
    except ChildProcessError:
        return _GONE
    except OSError:
        return _zombie_or_gone(process.pid)
    return _RUNNING if found is None else _EXITED


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
        # Group teardown happens once, while the leader is still unreaped; after
        # that no signal is ever sent to the group (its number may be reused).
        self._group_lock = threading.Lock()
        self._group_ended = False
        # A contract process's teardown (cut the lifeline, ``stop()``) takes time.
        # ``_group_ended`` says it started, so nobody signals the group; this says
        # it finished, and nothing reaps the leader before that: a reaped leader
        # makes ``stop()`` skip the group cleanup it was in the middle of.
        self._handover_started = False
        self._handover_done = threading.Event()
        # Set only when the session itself ended the worker's group (or its hand-over
        # returned cleanly). A leader that vanished some other way proves nothing
        # about what it left behind.
        self._teardown_ok = False
        # Set when the leader was reaped by someone else: its number is not ours.
        self._group_lost = False
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
        # Frames queued and frames the writer has finished, so a caller can wait
        # for one frame to be written (the reject) before the worker is ended.
        self._queued_total = 0
        self._written_total = 0
        self._written = threading.Condition(self._lock)
        self._write_started: float | None = None
        self._call_queues: dict[Any, deque] = {}
        self._call_pending = 0
        self._stop = threading.Event()
        self._stderr_window = (0.0, 0)
        self._bye = threading.Event()
        # Secret values seen in requests, kept after the request ends: its last
        # stderr lines can reach the drain thread after the reply reached the caller.
        self._secret_values: set[str] = set()
        # The same values and lines in every escaped spelling (repr, JSON, URL):
        # what redaction cuts out. Not counted against ``max_secret_values``.
        self._secret_forms: set[str] = set()
        self._secrets_by_length: tuple = ()
        self.recycle_needed = False

    # ------------------------------------------------------------------ setup

    def register_host_call(self, name, handler):
        """Register ``handler(call: HostCall) -> dict`` for a worker call name."""
        with self._lock:
            self._host_calls[name] = handler

    @property
    def alive(self):
        return self.process is not None and not self._closed and not self._exited(self.process)

    @property
    def in_flight(self):
        with self._lock:
            return len(self._requests)

    @property
    def rss_kb(self):
        return (self.last_heartbeat or {}).get("rss_kb")

    def start(self) -> Welcome:
        """Spawn the worker and run the handshake. Raises ``HandshakeError``."""
        with self._lock:
            if self._closed:
                raise self._failure or SessionClosed("session is closed")
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
        started = []
        try:
            return self._run_handshake(process, started)
        except BaseException as error:
            # Whatever failed after the spawn, nothing it acquired may outlive
            # this call: the process, the threads that did start, and the pipes
            # no thread was started to own.
            self._abort_start(process, started, error)
            raise

    def _run_handshake(self, process, started):
        self._reader = frames.FrameReader(process.stdout, {frames.FRAME_CONTROL: frames.HANDSHAKE_CONTROL_BYTES})
        self.last_frame_at = time.monotonic()
        for target, label in (
            (self._read_loop, "reader"),
            (self._drain_stderr, "stderr"),
            (self._write_loop, "writer"),
        ):
            self._start_thread(target, label, started)

        if self._hello.wait(self.limits.handshake_timeout):
            error = self._handshake_error
        else:
            late = HandshakeError("worker sent no hello within %.1fs" % self.limits.handshake_timeout)
            # The hello may land between the timeout and here: end the session
            # only if it still has not, under the lock.
            self._fail(late, kill=True, still=lambda: not self._hello.is_set())
            error = late if self._failure is late else self._handshake_error
        if error is None:
            with self._lock:
                if self._closed:
                    # Ended between the hello and now (the worker left, or close() ran).
                    error = HandshakeError("session ended during the handshake")
        if error is not None:
            self._fail(error, kill=True)
            raise error
        self._start_thread(self._watchdog, "watchdog", started)
        return self.welcome

    def _start_thread(self, target, label, started):
        thread = threading.Thread(target=target, name="%s-%s" % (self.name, label), daemon=True)
        thread.start()
        started.append(label)
        with self._lock:
            self._threads.append(thread)

    def _abort_start(self, process, started, error):
        """Undo a start() that failed after the spawn. Never raises."""
        try:
            self._fail(error if isinstance(error, SessionError) else SessionClosed("session could not start"),
                       kill=True)
        except Exception:
            pass
        self._stop.set()
        # Every stream is closed by the thread that reads or writes it; one whose
        # thread never started has no owner, so it is closed here.
        owners = (("reader", process.stdout), ("stderr", process.stderr), ("writer", process.stdin))
        for label, stream in owners:
            if label not in started:
                self._close_stream(stream)
        self._join_threads(timeout=2.0)
        try:
            self._kill_tree(process)
        except Exception:
            pass

    def _join_threads(self, timeout):
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=timeout)

    # --------------------------------------------------------------- requests

    def submit(self, op, params=None, *, timeout=None, max_result_bytes=None, secrets=None, on_progress=None):
        """Send a request and return its handle. Raises ``SessionBusy`` at the limit."""
        cap = self.limits.max_result_bytes if max_result_bytes is None else min(
            int(max_result_bytes), self.limits.max_result_bytes)
        # Encoded before anything is registered: params that cannot be sent
        # (not JSON, NaN, too big) must not cost an in-flight slot.
        if timeout is not None:
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("timeout must be a finite number of seconds, not negative")
        if secrets is not None and not frames.is_secrets(secrets):
            raise ValueError("secrets must be a flat mapping of text names to non-empty text values")
        frames.pack_control({"t": frames.REQ, "id": 0, "op": op, "params": params or {},
                             "max_result_bytes": cap, "secrets": secrets or {}}, sender="host")
        with self._lock:
            if self._closed or self.welcome is None:
                raise self._failure or SessionClosed("session is not running")
            limit = min(self.limits.max_in_flight, self.welcome.max_in_flight)
            if len(self._requests) >= limit:
                raise SessionBusy("worker has %d requests in flight" % len(self._requests))
            req_id = self._next_id
            deadline = time.monotonic() + timeout if timeout is not None else None
            request = _Request(req_id, op, deadline, cap, on_progress, secrets)
            fresh = [value for value in _with_line_parts(request.secrets) if value not in self._secret_values]
            if len(self._secret_values) + len(set(fresh)) > self.limits.max_secret_values:
                self.recycle_needed = True
                raise SessionError(
                    "worker has been sent %d distinct secrets; start a new one" % len(self._secret_values),
                    code=Codes.RECYCLE_REQUIRED)
            if fresh:
                self._secret_values.update(fresh)
                for value in fresh:
                    self._secret_forms.update(frames.secret_forms(value))
                self._secrets_by_length = tuple(sorted(self._secret_forms, key=len, reverse=True))
            self._next_id += 1
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
            # The request becomes visible to the watchdog and to cancel only
            # together with its frame in the outbox, so a cancel can never be
            # queued ahead of the request it cancels.
            data = frames.pack_control(message, sender="host")
            status = self._put(data)
            if status == _QUEUED:
                self._requests[req_id] = request
        if status == _FULL:
            self._end_if_still_full(len(data))
            if not self._closed:
                # The writer caught up while the full outbox was being judged.
                raise SessionBusy("worker input is backed up; nothing was sent")
        if status != _QUEUED:
            raise self._failure or SessionClosed("session is not running")
        return RequestHandle(self, request)

    def request(self, op, params=None, *, timeout=None, **kwargs):
        """Submit and wait. ``timeout`` is the deadline for the whole request."""
        return self.submit(op, params, timeout=timeout, **kwargs).wait()

    def _cancel(self, request, reason, timed_out=False):
        # The frame is built first. The cancellation (and its grace timer, which
        # ends the whole session when the worker ignores it) starts only once
        # there is a valid frame to send, so a bad reason cannot make the
        # watchdog wait on a cancel that was never sent.
        reason = self._clean_reason(reason)
        data = frames.pack_control({"t": frames.CANCEL, "id": request.id, "reason": reason}, sender="host")
        with self._lock:
            if (request.done.is_set() or request.cancel_sent_at is not None
                    or self._requests.get(request.id) is not request):
                return
            request.cancel_sent_at = time.monotonic()
            request.cancel_reason = reason
            if timed_out:
                request.timed_out = True
        try:
            self._enqueue_packed(data)
        except (FrameError, ValueError, TypeError, RecursionError):
            pass

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
                if self._closed:
                    # The session decided to end while this frame was being read
                    # (``_closed`` is set a moment before ``_stop``): dispatch nothing.
                    return
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
                        raise FrameError("first message was not hello")
                    if not self._on_hello(message):
                        # Negotiation failed: nothing the worker sent after its
                        # hello may be dispatched, only start() decides what next.
                        return
                    welcomed = True
                    reader.caps.update(frames.default_caps())
                    continue
                if self._on_message(message):
                    return
        except FrameError as error:
            if not welcomed and getattr(error, "type_byte", None) is not None:
                code = error.code
                failure = HandshakeError(
                    "worker spoke an unknown protocol (%s)" % self._public(error), code=code)
                self._handshake_error = failure
                self._hello.set()
                return
            if not welcomed:
                self._handshake_error = HandshakeError(self._public(error), code=error.code)
                self._hello.set()
                return
            self._fail(ProtocolViolation(self._public(error), code=error.code), kill=True)
            return
        except (OSError, ValueError):
            pass
        except BaseException as error:
            # Whatever else a hostile frame or a callback provokes, SystemExit
            # included. The reader must never die silently.
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
            self._handshake_error = HandshakeError(self._public("worker exited before hello (exit code %s)" % exit_code))
            self._hello.set()
            return
        if self._bye.is_set() or self._closed:
            # Always end and reap the group: the leader may still be alive after
            # the stream closed, and anything it forked has to go with it.
            self._fail(SessionClosed("worker exited"), kill=True)
        else:
            self._fail(WorkerExited(self._public("worker exited (exit code %s)" % exit_code), exit_code=exit_code),
                       kill=True)

    def _on_hello(self, hello):
        """Answer a hello. True only when the welcome was queued and the session is negotiated."""
        protocol = frames.negotiate_protocol(hello["protocols"])
        if protocol is None:
            self._reject(Codes.VERSION_MISMATCH, "no protocol major in common")
            return False
        kind = None
        if self.kind is not None:
            kind = frames.negotiate_kind(hello["kinds"], self.kind, self._kind_versions)
            if kind is None:
                self._reject(Codes.VERSION_MISMATCH, "worker does not serve a usable %s API" % self.kind)
                return False
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
        negotiated = Welcome(
            protocol=protocol,
            kind=kind,
            worker=self._redact_obj(hello.get("worker")) or {},
            max_in_flight=hello.get("max_in_flight") or self.limits.max_in_flight,
            pid=hello.get("pid"),
        )
        data = frames.pack_control(welcome, sender="host")
        # The welcome is queued and the negotiated state published in one locked
        # step, and only if the welcome was queued: submit() reads that state
        # under the same lock, so no request frame can be queued ahead of the
        # welcome, and nothing looks negotiated when the worker was never told.
        with self._lock:
            status = _CLOSED if self._closed else self._put(data)
            if status == _QUEUED:
                self.welcome = negotiated
        if status == _FULL:
            self._end_if_still_full(len(data))
        if status != _QUEUED:
            self._handshake_error = HandshakeError("session ended during the handshake")
        self._hello.set()
        return status == _QUEUED

    def _reject(self, code, message):
        if self._send_quietly({"t": frames.REJECT, "code": code, "message": message}):
            # Waking start() ends the worker; give the writer a bounded moment to
            # put the rejection on the pipe first, so the worker can read why.
            self._wait_written(min(self.limits.write_timeout, _REJECT_FLUSH_WAIT))
        self._handshake_error = HandshakeError(message, code=code)
        self._hello.set()

    def _wait_written(self, timeout):
        """Wait until every frame queued so far is written. False on timeout or when the session ends."""
        deadline = time.monotonic() + timeout
        with self._written:
            target = self._queued_total
            while self._written_total < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return False
                self._written.wait(min(remaining, 0.05))
        return True

    def _on_message(self, message):
        """Handle one control message. True means the reader should stop."""
        kind = message["t"]
        if kind == frames.HB:
            self.last_heartbeat = self._redact_obj(message)
        elif kind == frames.PROGRESS:
            request = self._get(message["id"])
            if request is not None and request.on_progress is not None:
                try:
                    request.on_progress(self._redact_obj(message))
                except BaseException as error:
                    self._log_callback_failure("progress", error)
        elif kind == frames.RES:
            self._on_result(message)
        elif kind == frames.CALL:
            self._on_call(message)
        elif kind == frames.SANDBOX_BLOCKED:
            self._emit("sandbox_blocked", self._redact_obj(message))
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
        req_id = message["id"]
        with self._lock:
            request = self._requests.get(req_id)
        if request is None:
            # The id is worker-supplied and may echo a secret: name only its type.
            logger.debug("%s: result for an unknown request ignored (id is %s)",
                         self.name, type(message["id"]).__name__)
            return
        # The reply is judged while the request is still registered, so anything
        # that goes wrong here still reaches the session's failure path, which
        # finishes every registered request. Only this thread adds data to it.
        outcome, error, cancelled = self._judge_result(request, message)
        with self._lock:
            if self._requests.get(req_id) is not request:
                # The session ended meanwhile and has finished it.
                return
            del self._requests[req_id]
            # Checked at the point of acceptance, against the clock: the
            # watchdog only sets timed_out on its next tick.
            expired = request.timed_out or (
                request.deadline is not None and time.monotonic() >= request.deadline)
            if cancelled:
                error = (RequestTimeout("request exceeded its deadline") if expired
                         else RequestCancelled(self._public(request.cancel_reason or "cancelled")))
            elif error is None and expired:
                outcome = None
                error = RequestTimeout("request exceeded its deadline")
            self._finish(request, outcome=outcome, error=error)

    def _judge_result(self, request, message):
        """What a reply means: ``(outcome, error, cancelled)``. Changes no session state."""
        if request.overflow:
            return None, ResultTooLarge("result exceeded %d bytes" % request.max_result_bytes), False
        if not message["ok"]:
            error = message["error"]
            code = self._safe_code(error.get("code"))
            if code == Codes.CANCELLED:
                return None, None, True
            return None, self._failed(error, code, request), False
        data = b"".join(request.chunks)
        declared = message.get("bytes")
        problem = None
        if declared is not None and declared != len(data):
            problem = "result declared %d bytes and sent %d" % (declared, len(data))
        elif message.get("chunks") is not None and message["chunks"] != len(request.chunks):
            problem = "result declared %d chunks and sent %d" % (message["chunks"], len(request.chunks))
        elif message.get("sha256") is not None and message["sha256"] != hashlib.sha256(data).hexdigest():
            problem = "result checksum does not match its data"
        elif request.chunks and message.get("chunks") is None:
            problem = "chunked result without a chunk count"
        elif declared is None and data:
            problem = "data frames arrived without a byte count"
        elif data and message.get("sha256") is None:
            problem = "chunked result without a checksum"
        if problem:
            # The counts are worker-supplied numbers and can spell a secret.
            return None, ProtocolViolation(self._public(problem)), False
        return RequestResult(message.get("result") or {}, data), None, False

    def _failed(self, error, code, request):
        # Scrub first, then cut: a secret that straddles the cut is replaced whole.
        def clean(value, limit, tail=False):
            if not isinstance(value, str):
                return None
            text = self._redact(value)
            return text[-limit:] if tail else text[:limit]

        retry_after = self._redact_obj(error.get("retry_after"))
        if (not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool)
                or retry_after != retry_after or retry_after in (float("inf"), float("-inf"))):
            retry_after = None
        errno = self._redact_obj(error.get("errno"))
        return RequestFailed(
            clean(error.get("message"), _MESSAGE_LIMIT) or code,
            code=code,
            retryable=error.get("retryable") is True,
            retry_after=retry_after,
            class_name=clean(error.get("class_name"), _CLASS_NAME_LIMIT),
            traceback=clean(error.get("traceback"), _TRACEBACK_LIMIT, tail=True),
            errno=errno if isinstance(errno, int) and not isinstance(errno, bool) else None,
        )

    def _finish(self, request, outcome=None, error=None):
        request.outcome = outcome
        request.error = error
        request.chunks = []
        request.done.set()
        with request.callback_lock:
            callbacks, request.callbacks = request.callbacks, []
        if callbacks:
            # Not on this thread: it may hold session locks, and a callback is
            # free to call back into whatever owns the session.
            threading.Thread(target=_run_callbacks, args=(callbacks,), name="%s-done" % self.name,
                             daemon=True).start()

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
            if self._closed:
                return
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
            try:
                threading.Thread(target=self._call_worker, args=(key,), daemon=True,
                                 name="%s-calls-%s" % (self.name, key)).start()
            except Exception:
                # No thread, so nothing will ever answer: give back the slot and
                # the queue, and tell the worker now.
                with self._lock:
                    dropped = self._call_queues.pop(key, None)
                    if not self._stop.is_set():
                        self._call_pending = max(0, self._call_pending - len(dropped or ()))
                self._send_quietly({"t": frames.ACK, "cid": message["cid"], "ok": False,
                                    "error": frames.error_object(Codes.HOST_CALL_FAILED,
                                                                 "the host could not start the call")})

    def _call_worker(self, key):
        clean = False
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._closed:
                        # Ended since the check above: a handler must not start now.
                        clean = True
                        return
                    pending = self._call_queues.get(key)
                    if not pending:
                        self._call_queues.pop(key, None)
                        clean = True
                        return
                    message = pending.popleft()
                    # Judged under the lock, right before the handler would run: a
                    # call queued behind a slow one may outlive its request, and
                    # must not touch state that a retry of the request now owns.
                    ended = key is not None and self._requests.get(key) is None
                    if ended and not self._stop.is_set():
                        self._call_pending = max(0, self._call_pending - 1)
                if ended:
                    self._send_quietly({"t": frames.ACK, "cid": message["cid"], "ok": False,
                                        "error": frames.error_object(Codes.INVALID_REQUEST,
                                                                     "call for a request that is not running")})
                    continue
                answer = self._answer_call(message)
                # Counted until the handler is done: a handler that blocks keeps its
                # slot, so a worker cannot stack blocked handler threads past the cap.
                with self._lock:
                    if not self._stop.is_set():
                        self._call_pending = max(0, self._call_pending - 1)
                self._send_quietly(self._sendable(answer))
            clean = True
        finally:
            if not clean:
                # Something escaped the handler's own boundary: give back what
                # this thread held and drop its queue, so nothing waits on it.
                with self._lock:
                    dropped = self._call_queues.pop(key, None)
                    if not self._stop.is_set():
                        self._call_pending = max(0, self._call_pending - 1 - len(dropped or ()))

    def _answer_call(self, message):
        name = message["name"]
        cid = message["cid"]
        with self._lock:
            handler = self._host_calls.get(name)
        if handler is None:
            return {"t": frames.ACK, "cid": cid, "ok": False,
                    "error": frames.error_object(Codes.UNKNOWN_CALL, "no host call with that name")}
        try:
            result = handler(HostCall(name, message["params"], message.get("id"), self))
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise TypeError("host call %s returned %s, not a dict" % (name, type(result).__name__))
            ack = {"t": frames.ACK, "cid": cid, "ok": True, "result": result}
            # Encoded here, inside the boundary: a result that is too big, holds a
            # NaN or is not JSON must become a small failure ack, not a dead thread.
            frames.pack_control(ack, sender="host")
            return ack
        except HostCallError as error:
            text = self._describe(error.message)
            return {"t": frames.ACK, "cid": cid, "ok": False,
                    "error": frames.error_object(self._safe_code(error.code), text,
                                                 retryable=error.retryable)}
        except BaseException as error:
            # Logged redacted and without the raw exception, which may carry a
            # secret. SystemExit from a handler is a failed call like any other.
            text = self._describe(error)
            logger.warning("%s: host call %s failed: %s", self.name, name,
                           "%s: %s" % (type(error).__name__, text))
            return {"t": frames.ACK, "cid": cid, "ok": False,
                    "error": frames.error_object(Codes.HOST_CALL_FAILED, text)}

    def _sendable(self, ack):
        """The ack itself if it can be sent, else a plain failure for the same call.

        An error text can carry what the schema refuses (a lone surrogate). A
        dropped ack would leave the worker waiting out its call timeout.
        """
        try:
            frames.pack_control(ack, sender="host")
            return ack
        except Exception:
            return {"t": frames.ACK, "cid": ack["cid"], "ok": False,
                    "error": frames.error_object(Codes.HOST_CALL_FAILED, "host handler failed")}

    def _describe(self, value):
        """Redacted, bounded text for a host-side error, or a fixed phrase if it cannot be formatted."""
        try:
            return self._public(value)
        except BaseException:
            return "host handler failed"

    # --------------------------------------------------------------- watchdog

    def _watchdog(self):
        while not self._stop.wait(self.limits.tick):
            try:
                self._tick()
            except BaseException as error:
                # A dead watchdog would leave every deadline and heartbeat unenforced.
                logger.error("%s: watchdog failed: %s", self.name, type(error).__name__)
                self._fail(SessionError("watchdog failed (%s)" % type(error).__name__), kill=True)
            if self._closed:
                return

    def _tick(self):
        limits = self.limits
        now = time.monotonic()
        if now - self.last_frame_at > limits.heartbeat_timeout:
            self._fail(HeartbeatTimeout(
                "no frame from the worker for %.1fs" % (now - self.last_frame_at)), kill=True,
                still=lambda: time.monotonic() - self.last_frame_at > limits.heartbeat_timeout)
            return
        started = self._write_started
        if started is not None and now - started > limits.write_timeout:
            self._fail(SessionError(
                "worker has not read its input for %.1fs" % (now - started),
                code=Codes.WRITE_TIMEOUT), kill=True,
                still=lambda: self._write_started == started)
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
                        error = RequestCancelled(self._public(request.cancel_reason or "cancelled"))
                    self._fail(error, kill=True, only=request)
                    if self._closed:
                        return
            elif request.deadline is not None and now >= request.deadline:
                # Flag and cancel together, under the lock, and only while the
                # request is still waiting for its reply.
                self._cancel(request, Codes.TIMEOUT, timed_out=True)

    # ------------------------------------------------------------- lifecycle

    def _exited(self, process):
        """Whether the worker has exited. Notes (once and for good) if it was reaped elsewhere."""
        state = _probe(process)
        if state == _GONE:
            with self._group_lock:
                # Our own reap makes the leader vanish too, but only after the
                # group was ended; a leader that vanishes before that was
                # reaped by somebody else.
                if not self._group_ended:
                    self._group_lost = True
        return state != _RUNNING

    def _ours(self, process):
        """Whether the leader is still ours to signal. Call with ``_group_lock`` held.

        Not after the group was ended, after the leader was reaped by anyone, or
        when it turns out to be gone right now.
        """
        if self._group_ended or self._group_lost or process.returncode is not None:
            return False
        if _probe(process) == _GONE:
            self._group_lost = True
            return False
        # Whatever the probe did, a leader that is reaped now is not ours.
        return process.returncode is None

    def _poll(self):
        process = self.process
        if process is None or not self._exited(process):
            return None
        try:
            return self._reap(process, 0.0)
        except subprocess.TimeoutExpired:
            # The group was just ended and the leader is not dead yet.
            return None

    def _wait_exit(self, process, timeout):
        """Wait until the worker has exited, leaving it unreaped. False if it has not within ``timeout``."""
        deadline = time.monotonic() + timeout
        while not self._exited(process):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _hand_over(self, process):
        """Let a process that ends its own tree do it. True when ``process`` has that contract.

        A sandbox launcher or an anchored worker (anything with ``stop()``)
        knows how to end everything it started, including children that left
        the group with ``setsid``, which a signal to the group cannot reach. A
        group SIGKILL would take the launcher down before that cleanup
        finished, so the teardown goes to the process: cut its lifeline (if
        any), then ``stop()``, which waits for the confirmed exit within its own
        bound. Done once; the group is never signalled by the session after
        this, so a reaped or not-owned group number is never used.
        """
        stop = getattr(process, "stop", None)
        if not callable(stop):
            return False
        with self._group_lock:
            first = not self._group_ended
            self._group_ended = True
            if first:
                self._handover_started = True
        if first:
            try:
                cut = getattr(process, "close_lifeline", None)
                if callable(cut):
                    cut()
                stop(self.limits.kill_grace)
                with self._group_lock:
                    # A process that says it could not end the group (its leader was
                    # reaped first) has not done the teardown, however cleanly it returned.
                    self._teardown_ok = getattr(process, "group_cleaned", None) is not False
            except Exception as error:
                logger.warning("%s: stopping the worker failed: %s", self.name, type(error).__name__)
            finally:
                self._handover_done.set()
        else:
            # Somebody else is tearing it down (or did): return only when that is over.
            self._wait_handover(self.limits.kill_grace + _HANDOVER_WAIT)
        return True

    def _wait_handover(self, timeout):
        """Wait for a started hand-over to finish. False if it has not within ``timeout``."""
        if not self._handover_started:
            return True
        return self._handover_done.wait(timeout)

    @property
    def teardown_confirmed(self):
        """Whether the session ended the worker's whole group (or its process did, on request).

        False after an outside reaper took the leader: its group number is not
        ours, nothing was signalled, and descendants may live on. Where there are
        no process groups the reap is all there is to confirm.
        """
        if not _is_posix():
            return True
        with self._group_lock:
            return self._teardown_ok and not self._group_lost

    def _end_group(self, process):
        """Kill what the worker forked, once, while its leader is still unreaped."""
        if self._hand_over(process):
            return
        with self._group_lock:
            if self._group_ended:
                return
            ours = self._ours(process)
            self._group_ended = True
            if ours and _signal_group(process, _SIGKILL):
                self._teardown_ok = True

    def _reap(self, process, timeout):
        """Wait for the exit, end the group, then reap the leader. Returns the exit code.

        Once the group is ended (or the leader is known to be lost) there is
        nothing left to protect, so the reap is a plain ``wait`` for the
        remaining time: where an exit cannot be told from a live process without
        reaping, waiting for the probe would never finish.
        """
        deadline = time.monotonic() + timeout
        if not (self._group_ended or self._group_lost):
            if not self._wait_exit(process, timeout):
                raise subprocess.TimeoutExpired(process.args, timeout)
            self._end_group(process)
        # Never reap the leader while its teardown is still running.
        if not self._wait_handover(max(0.0, deadline - time.monotonic())):
            raise subprocess.TimeoutExpired(process.args, timeout)
        return process.wait(timeout=max(0.0, deadline - time.monotonic()))

    def _signal_leader_group(self, process, sig):
        """Signal the group only while the leader is certainly unreaped. True if handled.

        Held under the same lock as the teardown: once the group is ended the
        leader may be reaped and its number reused, so nothing is signalled.
        """
        # A process that checks ownership and signals under one lock of its own
        # (a launcher, an anchored worker) is asked to; the session's lock cannot
        # keep ``stop()`` or a reap on that object from interleaving with it.
        signal_group = getattr(process, "signal_group", None)
        if callable(signal_group):
            with self._group_lock:
                if self._group_ended or self._group_lost:
                    return True
            sent = signal_group(sig)
            return True if sent is None else bool(sent)
        with self._group_lock:
            if not self._ours(process):
                return True
            return _signal_group(process, sig)

    def _reap_exit_code(self):
        process = self.process
        if process is None:
            return None
        try:
            self.exit_code = self._reap(process, 1.0)
        except Exception:
            self.exit_code = None
        return self.exit_code

    def _enqueue(self, message):
        """Queue a control frame for the writer thread. Never blocks.

        Returns False if the session is already closed. Raises
        ``FrameTooLarge`` for a message over the control cap. A full outbox
        means the worker stopped reading: the group is killed.
        """
        return self._enqueue_packed(frames.pack_control(message, sender="host"))

    def _enqueue_packed(self, data):
        """``_enqueue`` for a frame already built by ``pack_control``."""
        for _ in range(3):
            status = self._put(data)
            if status != _FULL:
                return status == _QUEUED
            self._end_if_still_full(len(data))
            if self._closed:
                return False
        # Judged full three times in a row: the worker is not reading.
        self._fail(SessionError("worker is not reading its input", code=Codes.WRITE_TIMEOUT), kill=True)
        return False

    def _outbox_full(self, size):
        return (self._outbox.qsize() >= self.limits.max_queued_frames
                or self._outbox_bytes + size > self.limits.max_queued_bytes)

    def _end_if_still_full(self, size):
        """End the session for a full outbox, unless the writer drained it since it was judged full."""
        self._fail(SessionError("worker is not reading its input", code=Codes.WRITE_TIMEOUT), kill=True,
                   still=lambda: self._outbox_full(size))

    def _put(self, data):
        """Put one packed frame in the outbox. Never blocks, never ends the session.

        Returns ``_QUEUED``, ``_CLOSED`` or ``_FULL``; the caller acts on a full
        outbox once it holds no lock.
        """
        with self._lock:
            if self._closed:
                return _CLOSED
            if self._outbox_full(len(data)):
                return _FULL
            self._outbox_bytes += len(data)
            self._queued_total += 1
            self._outbox.put_nowait(data)
        return _QUEUED

    def _send_quietly(self, message):
        try:
            return self._enqueue(message)
        except (FrameError, ValueError, TypeError, RecursionError):
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
                    self._fail(WorkerExited("worker pipe closed: %s" % self._describe(error), exit_code=self._poll()),
                               kill=True)
                    return
                except BaseException as error:
                    # SystemExit from an odd stream included: a writer that dies
                    # quietly leaves every request waiting on a frame never sent.
                    self._write_started = None
                    self._fail(SessionError("writer failed (%s)" % type(error).__name__), kill=True)
                    return
                self._write_started = None
                with self._written:
                    self._written_total += 1
                    self._written.notify_all()
        finally:
            self._close_stream(stream)

    def _emit(self, name, payload):
        callback = self._on_event
        if callback is None:
            return
        try:
            callback(name, payload)
        except BaseException as error:
            self._log_callback_failure("event", error)

    def _log_callback_failure(self, what, error):
        # Redacted and without the raw exception: its text or traceback may carry a secret.
        logger.debug("%s: %s callback failed: %s: %s", self.name, what, type(error).__name__,
                     self._describe(error))

    def _fail(self, error, kill, only=None, still=None):
        """End the session with ``error``; ``only`` names the request that caused it.

        Killing the worker is how every unrecoverable case ends: a worker that
        broke the contract, went silent or ignored a cancel cannot be trusted
        to finish the work it holds.

        A watchdog decides on a snapshot, so the condition is checked again here,
        under the lock: ``only`` must still be registered and unfinished, and
        ``still`` (when given) must still hold. Otherwise the worker answered in
        the meantime and nothing is ended.
        """
        with self._lock:
            if self._closed:
                return
            if only is not None and (only.done.is_set() or self._requests.get(only.id) is not only):
                return
            if still is not None and not still():
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
        try:
            if kill and process is not None:
                self._kill_tree(process)
        finally:
            # Whatever the kill does, nobody waits on a request that was dropped.
            for request in pending:
                if request.done.is_set():
                    continue
                if only is not None and request is not only:
                    self._finish(request, error=_as_session_error(error))
                else:
                    self._finish(request, error=error)

    def _kill_tree(self, process):
        """End the worker and its whole group, once. A later call only reaps."""
        if not self._hand_over(process):
            self._kill_group(process)
        try:
            self.exit_code = self._reap(process, self.limits.kill_grace)
        except Exception:
            logger.warning("%s: worker survived a kill", self.name)

    def _kill_group(self, process):
        with self._group_lock:
            if self._ours(process):
                # The leader is not reaped (every reap goes through ``_reap``, after
                # the group is ended, and an outside reaper is checked for), so its
                # group number is still ours.
                self._group_ended = True
                if _signal_group(process, _SIGKILL):
                    self._teardown_ok = True
                else:
                    try:
                        process.kill()
                    except OSError:
                        pass

    def close(self, grace=5.0):
        """Ask the worker to stop, then end its whole group. Idempotent.

        The shutdown frame is only a request: it is queued, never awaited. If
        the worker has not exited after ``grace`` it gets SIGTERM, then after
        ``term_grace`` SIGKILL for the group, whether or not it was reading.
        """
        with self._lock:
            process = self.process
            already = self._closed
            handshaken = self.welcome is not None
            if process is None and not already:
                # Never started: mark it closed under the same lock start() takes,
                # so a start racing this call cannot spawn a worker nobody ends.
                self._closed = True
                self._failure = SessionClosed("session closed")
                already = True
        # Before the welcome the worker is not in the conversation yet, and a
        # shutdown frame ahead of the welcome would be a frame it cannot place:
        # that worker is simply ended below.
        if not already and process is not None and handshaken and not self._exited(process):
            self._send_quietly({"t": frames.SHUTDOWN, "grace_ms": min(max(int(grace * 1000), 0), frames.MAX_SHUTDOWN_GRACE_MS)})
            if not self._wait_exit(process, grace):
                if not self._signal_leader_group(process, _SIGTERM):
                    try:
                        process.terminate()
                    except OSError:
                        pass
                self._wait_exit(process, self.limits.term_grace)
        self._fail(SessionClosed("session closed"), kill=process is not None)
        if process is not None:
            # The reader may have closed the session first on a clean exit;
            # anything the worker forked still has to go.
            self._kill_tree(process)
        self._join_threads(timeout=2.0)

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
                    except BaseException:
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

    def _clean_reason(self, reason):
        """A cancel reason that always encodes and never carries a secret.

        Text with valid Unicode only (else "cancelled"). The whole reason is
        redacted first and cut afterwards, so a secret that straddles the cut is
        replaced whole instead of leaving its prefix.
        """
        if not isinstance(reason, str):
            return "cancelled"
        try:
            reason.encode("utf-8")
        except UnicodeEncodeError:
            return "cancelled"
        return self._redact(reason)[:_REASON_LIMIT] or "cancelled"

    def _public(self, value):
        """Worker-influenced text, redacted and bounded, for an exception or a log line."""
        return self._redact(str(value))[:300]

    def _safe_code(self, value):
        """A worker-supplied error code, or ``plugin_error`` if it is not a plain code.

        Redaction runs first, so a secret echoed as a code turns into ``***``
        and fails the shape check.
        """
        if not isinstance(value, str):
            return Codes.PLUGIN_ERROR
        code = self._redact(value)
        return code if _CODE_SHAPE.fullmatch(code) else Codes.PLUGIN_ERROR

    def _redact_obj(self, value, depth=0):
        """A worker-supplied JSON value with every secret taken out, then bounded.

        Strings and keys are redacted, then cut to ``_FIELD_LIMIT``. A number
        whose digits contain a secret becomes ``None``: a worker can spell a
        numeric secret as an int. A container keeps its first ``_ITEM_LIMIT``
        entries, and anything nested deeper than ``_DEPTH_LIMIT`` becomes ``None``.
        """
        if isinstance(value, str):
            return self._redact(value)[:_FIELD_LIMIT]
        if isinstance(value, (list, dict)):
            if depth >= _DEPTH_LIMIT:
                return None
            if isinstance(value, list):
                return [self._redact_obj(item, depth + 1) for item in value[:_ITEM_LIMIT]]
            return {(self._redact(key)[:_FIELD_LIMIT] if isinstance(key, str) else key): self._redact_obj(item, depth + 1)
                    for key, item in list(value.items())[:_ITEM_LIMIT]}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
            if any(secret in text for secret in self._secrets_snapshot()):
                return None
        return value


def _as_session_error(error):
    """The error the requests that were not the cause see."""
    return SessionClosed("worker was ended while this request ran")
