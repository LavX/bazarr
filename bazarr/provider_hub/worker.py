# coding=utf-8
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import weakref
import time
import uuid

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import WORKER_ABI_VERSION

logger = logging.getLogger(__name__)

# Cap a single worker->host NDJSON response line. ``readline()`` buffers a whole
# line before ``json.loads`` runs, and the protocol-level archive cap only fires
# after the line is already in memory, so a runaway or malicious worker could OOM
# the host with one giant line. Sized comfortably above the 32 MB archive cap
# (base64 is ~43 MB, plus the JSON envelope) so legitimate responses still pass.
_MAX_RESPONSE_LINE_BYTES = 48 * 1024 * 1024
# Read granularity for the bounded readline loop, so the cap is enforced before a
# whole oversized line accumulates.
_READ_CHUNK_CHARS = 1024 * 1024
# Queued by the reader thread when a response line exceeds the cap, so the
# consumer kills the worker at the transport layer instead of assembling it.
_OVERSIZE_RESPONSE = object()


def _json_default(obj):
    """Coerce values not natively JSON-serializable into safe representations.

    Subliminal's Video objects carry numeric fields (notably ``fps``) as
    ``decimal.Decimal``, which the stdlib JSON encoder rejects. We convert
    Decimals to float (lossy precision is acceptable for transport payloads,
    the provider only uses these for matching, not for arithmetic). Other
    surprise types fall back to ``str(obj)`` so the worker call surfaces a
    debuggable payload instead of a hard crash.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


class WorkerError(RuntimeError):
    """Raised when a provider worker fails or returns an error."""

    def __init__(self, message, *, remote_class_name=None, code=None, retryable=False):
        super().__init__(message)
        self.remote_class_name = remote_class_name
        self.code = code
        self.retryable = retryable


def _raise_worker_error(payload):
    if not isinstance(payload, dict):
        raise WorkerError("worker request failed")

    remote_name = payload.get("class_name")
    remote_name = remote_name if isinstance(remote_name, str) else None
    code = payload.get("code")
    code = code if isinstance(code, str) else None
    retryable = payload.get("retryable", False)
    retryable = retryable if isinstance(retryable, bool) else False
    message = payload.get("message")
    message = message if isinstance(message, str) and message else code or "worker request failed"
    error = WorkerError(message, remote_class_name=remote_name, code=code, retryable=retryable)
    if code != "provider":
        raise error

    # Keep host exception imports out of worker startup. Only these fixed
    # semantic names can cross the boundary as host provider exceptions.
    from subliminal.exceptions import (
        AuthenticationError,
        ConfigurationError,
        DownloadLimitExceeded,
        ServiceUnavailable,
    )
    from subliminal_patch.exceptions import APIThrottled, TooManyRequests

    exception_type = {
        "DownloadLimitExceeded": DownloadLimitExceeded,
        "TooManyRequests": TooManyRequests,
        "RateLimited": TooManyRequests,
        "ServiceUnavailable": ServiceUnavailable,
        "APIThrottled": APIThrottled,
        "AuthenticationError": AuthenticationError,
        "AuthenticationRequired": AuthenticationError,
        "ConfigurationError": ConfigurationError,
    }.get(remote_name)
    if exception_type is None:
        raise error
    raise exception_type(message) from error


@dataclass
class WorkerResult:
    ok: bool
    payload: dict[str, Any]
    events: list[dict[str, Any]]


# Every started worker registers here so idle ones can be reclaimed. A weak set,
# so a client the pool has dropped does not stay alive just by being listed.
#
# The fleet needs reclaiming because nothing else does it: a worker is spawned
# lazily the first time a pool searches with that provider, and the pool's own
# recycle only fires the next time that same pool is searched. On a quiet install
# the processes simply accumulate, at 17.6 to 44.6 MiB each.
_live_clients: "weakref.WeakSet[ProviderWorkerClient]" = weakref.WeakSet()
_live_clients_lock = threading.Lock()

# Clients whose process survived a kill. The weak set alone would let garbage
# collection drop such a client once its pool does, and with it the only handle
# to the still-running subprocess; held strongly until a later sweep wins.
_unreaped_survivors: "set[ProviderWorkerClient]" = set()

# Resolved once: signal.SIGKILL does not exist on Windows, and referencing it
# at a call site raises AttributeError before _signal_group's own guards can
# report False, breaking the process.kill() fallback.
_SIGKILL = getattr(signal, "SIGKILL", None)


def reap_idle_workers(idle_seconds: float) -> int:
    """Stop workers that have served no request for ``idle_seconds``.

    The decision and the shutdown happen together inside each client, under the
    same lock a request holds. Deciding here and acting afterwards is a
    check-then-act race: a search can pass ``start()`` and be queued on that
    lock in the window between the two, and would then write into a process
    this sweep had already ended.
    """
    with _live_clients_lock:
        candidates = list(_live_clients)

    reaped = 0
    for client in candidates:
        try:
            if client.stop_if_idle(idle_seconds):
                reaped += 1
        except Exception:
            # One plugin refusing to shut down must not strand the rest.
            logger.warning("Provider Hub worker did not stop cleanly", exc_info=True)

    if reaped:
        logger.debug("Provider Hub reclaimed %d idle worker(s)", reaped)
    return reaped


class ProviderWorkerClient:
    """Small NDJSON client for a single provider worker process."""

    def __init__(
        self,
        command: list[str],
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = command
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.process: subprocess.Popen | None = None
        # monotonic timestamp of the last request, read by reap_idle_workers
        self.last_used: float = time.monotonic()
        self._lock = threading.Lock()
        self._stdout_queue: queue.Queue[Any] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if self.env:
            env.update(self.env)

        self.last_used = time.monotonic()
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._stdout_queue = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._enqueue_stdout,
            args=(self.process, self._stdout_queue),
            daemon=True,
        )
        self._stdout_thread.start()
        # Continuously drain stderr too: the worker writes tracebacks there, and an
        # undrained pipe can fill before the worker writes its JSON error to stdout,
        # blocking the worker until the request times out. Drain + log instead.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self.process,),
            daemon=True,
        )
        self._stderr_thread.start()

        with _live_clients_lock:
            _live_clients.add(self)

    @staticmethod
    def _enqueue_stdout(process: subprocess.Popen, stdout_queue: queue.Queue[Any]) -> None:
        stdout = process.stdout
        if stdout is None:
            stdout_queue.put(None)
            return
        try:
            # Read in bounded chunks (readline(size) stops at a newline or after
            # ``size`` chars) and track the current line's length, so an oversized
            # response is rejected here instead of buffering in full before
            # json.loads. A normal multi-chunk line is reassembled downstream.
            line_len = 0
            while True:
                chunk = stdout.readline(_READ_CHUNK_CHARS)
                if not chunk:
                    break
                line_len += len(chunk)
                if line_len > _MAX_RESPONSE_LINE_BYTES:
                    stdout_queue.put(_OVERSIZE_RESPONSE)
                    break
                stdout_queue.put(chunk)
                if chunk.endswith("\n"):
                    line_len = 0
        finally:
            stdout_queue.put(None)

    @staticmethod
    def _drain_stderr(process: subprocess.Popen) -> None:
        """Drain the worker's stderr so a large or repeated traceback can never
        fill the pipe buffer and block the worker before it writes its JSON error
        to stdout. Surfaces the diagnostics in the host log (per-line capped)."""
        stderr = process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                line = line.rstrip("\n")
                if line:
                    logging.debug("provider-worker stderr: %s", line[:2000])
        except Exception:
            pass

    def _deregister(self) -> None:
        with _live_clients_lock:
            _live_clients.discard(self)
            _unreaped_survivors.discard(self)

    def _retain_survivor(self) -> None:
        with _live_clients_lock:
            _unreaped_survivors.add(self)

    @staticmethod
    def _signal_group(process, sig) -> bool:
        """Send ``sig`` to the worker's whole process group. False if it could not.

        ``start_new_session=True`` makes the worker its own session and group
        leader, so its group id is its pid. Signalling only that pid leaves
        anything the plugin forked running and reparented to init, where nothing
        counts or reclaims it, and a reap would report success having freed
        nothing. Plugin code is third party, so that is a mechanism to close,
        not an unlikely accident.

        ``os.killpg`` is POSIX only, so this reports failure rather than raising
        on Windows and the caller falls back to signalling the worker alone.
        ``AttributeError`` is not an ``OSError``, so catching it is not enough:
        it has to be checked before the call.
        """
        killpg = getattr(os, "killpg", None)
        if killpg is None or sig is None:
            return False
        try:
            killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    @classmethod
    def _terminate_tree(cls, process, grace_seconds: float) -> None:
        """Ask the group to stop, wait for the worker, reclaim what stayed.

        Raises if the worker itself outlives the grace period, which is the
        caller's signal to escalate to ``_kill_tree``.

        The trailing SIGKILL is what makes the graceful path complete. A worker
        that exits promptly never reaches ``_kill_tree``, so without it a child
        that ignored SIGTERM would survive a reap that reported success. It
        addresses the group by the id the exited worker led; a pid freed
        microseconds earlier could in principle be reused, but only a process
        that had made itself a group leader in that window would be reachable
        by it.
        """
        if not cls._signal_group(process, signal.SIGTERM):
            process.terminate()
        process.wait(timeout=grace_seconds)
        cls._signal_group(process, _SIGKILL)

    @classmethod
    def _kill_tree(cls, process, grace_seconds: float) -> bool:
        """SIGKILL the worker's whole process group, not just the worker.

        True only when the worker is confirmed gone; the caller must not
        deregister a process this could not end.
        """
        if not cls._signal_group(process, _SIGKILL):
            try:
                process.kill()
            except Exception:
                logger.warning("failed to kill provider worker", exc_info=True)
        try:
            process.wait(timeout=grace_seconds)
        except Exception:
            return False
        return True

    def stop_if_idle(self, idle_seconds: float, grace_seconds: float = 5.0) -> bool:
        """End this worker only if it is genuinely idle. True if it was stopped.

        Never waits. A worker serving a request holds ``_lock``, so the
        non-blocking acquire fails and it is left alone. That covers both halves
        of the problem: the sweep cannot end a request in flight, and it cannot
        pin the scheduler thread behind one. The second half matters because a
        request may run for hours, and a blocking acquire would hold the sweep
        for all of it and then kill the worker the instant it succeeded.

        Idleness is checked again once the lock is held, because the sweep chose
        this client without it.

        The process is ended directly rather than through ``stop()``, which
        sends a shutdown request and so would take the lock this already holds.
        An idle worker has nothing in progress to unwind.
        """
        process = self.process
        if process is None or process.poll() is not None:
            # Nothing to reap. Mirrors stop(): a held survivor whose process
            # finally exited on its own must not stay pinned in the strong
            # set; start() re-registers on the next spawn either way.
            self._deregister()
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if time.monotonic() - self.last_used < idle_seconds:
                return False
            if process.poll() is not None:
                return False
            ended = True
            try:
                self._terminate_tree(process, grace_seconds)
            except Exception:
                ended = self._kill_tree(process, grace_seconds)
        finally:
            self._lock.release()

        if not ended:
            # Both attempts left it running. Deregistering now would make it
            # permanently unreapable; keep it listed, and strongly, so a later
            # sweep retries even after the pool drops its own reference.
            logger.warning("idle provider worker survived termination; keeping it registered")
            self._retain_survivor()
            return False

        self._deregister()
        return True

    def stop(self, grace_seconds: float = 5.0) -> None:
        process = self.process
        if not process or process.poll() is not None:
            # Nothing left to reap, so this is the one safe unconditional
            # discard.
            self._deregister()
            return
        try:
            self.request("shutdown", {"reason": "app_shutdown", "grace_ms": int(grace_seconds * 1000)}, grace_seconds)
            process.wait(timeout=grace_seconds)
            # A clean shutdown ends the worker, not necessarily what it forked.
            self._signal_group(process, _SIGKILL)
        except Exception:
            self._kill_tree(process, grace_seconds)

        # Leave the registry only once this process is confirmed dead AND is
        # still the one this client owns. Two ways to get that wrong:
        #
        # A worker can outlive its own stop. start() registers only on the spawn
        # path, so a client discarded while its process still runs is never
        # re-added by a later request(), and that subprocess becomes permanently
        # unreapable: precisely the leak this reaper exists to prevent.
        #
        # And request() calls start(), which respawns if the process died
        # between the check above and the shutdown. self.process is then a NEW
        # worker that registered itself, and discarding here would strand it.
        if self.process is process and process.poll() is not None:
            self._deregister()
        elif self.process is process:
            # stop() could not end it. The pool is about to drop this client,
            # and the weak registry alone would let it be collected with the
            # process still running; hold it until a sweep succeeds.
            self._retain_survivor()

    def _read_line_with_deadline(self, timeout: float) -> str:
        """Read one NDJSON line from the worker, honoring ``timeout`` seconds.

        ``subprocess.PIPE.readline()`` blocks forever when a plugin hangs.
        Read stdout on a daemon thread and wait on a queue so the timeout path
        works on platforms where ``select`` cannot poll subprocess pipes.
        """
        process = self.process
        if process is None or process.stdout is None:
            raise WorkerError("worker process did not start")
        stdout_queue = self._stdout_queue
        if stdout_queue is None:
            raise WorkerError("worker stdout reader did not start")
        deadline = time.monotonic() + max(0.0, float(timeout))
        chunks: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_worker()
                raise WorkerError(
                    f"worker exceeded {timeout:.1f}s deadline"
                )
            try:
                chunk = stdout_queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if chunk is None:
                return "".join(chunks)
            if chunk is _OVERSIZE_RESPONSE:
                self._kill_worker()
                raise WorkerError(
                    f"worker response exceeded {_MAX_RESPONSE_LINE_BYTES} bytes"
                )
            chunks.append(chunk)
            if chunk.endswith("\n"):
                return "".join(chunks)

    def _kill_worker(self) -> None:
        process = self.process
        if process is None:
            return
        self._kill_tree(process, 5.0)

    def select_archive_member(
        self, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> WorkerResult:
        """Ask the worker to language-pin a member from a host-listed archive."""
        return self.request(
            "select_archive_member", payload, timeout=30.0 if timeout is None else timeout
        )

    def request(self, op: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> WorkerResult:
        request_id = str(uuid.uuid4())
        message = {
            "abi": WORKER_ABI_VERSION,
            "id": request_id,
            "op": op,
            "deadline_ms": int(timeout * 1000),
            "payload": payload or {},
        }

        with self._lock:
            # Startup and the freshness stamp live under the request lock:
            # outside it, the sweep can acquire the lock after start() returns,
            # read the old timestamp, and kill the worker this request is
            # about to write to.
            self.start()
            self.last_used = time.monotonic()
            if self.process is None or self.process.stdin is None or self.process.stdout is None:
                raise WorkerError("worker process did not start")

            self.process.stdin.write(
                json.dumps(message, separators=(",", ":"), default=_json_default)
                + "\n"
            )
            self.process.stdin.flush()
            line = self._read_line_with_deadline(timeout)
            # Stamped on the way out as well as the way in. A single request may
            # legitimately run for hours (transcription is allowed up to the 24
            # hour ceiling), and a worker judged only on when its request
            # STARTED would read as idle for almost all of that.
            self.last_used = time.monotonic()

        if not line:
            raise WorkerError("worker closed stdout")

        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise WorkerError("worker returned malformed JSON") from error

        if not isinstance(response, dict):
            raise WorkerError("worker response must be an object")
        if response.get("abi") != WORKER_ABI_VERSION:
            raise WorkerError("worker returned unsupported ABI")
        if response.get("id") != request_id:
            raise WorkerError("worker returned mismatched request id")

        if not response.get("ok", False):
            _raise_worker_error(response.get("error"))

        payload = response.get("payload") or {}
        events = response.get("events") or []
        if not isinstance(payload, dict):
            raise WorkerError("worker payload must be an object")
        if not isinstance(events, list):
            events = []
        return WorkerResult(ok=True, payload=payload, events=events)


def worker_command(python_exe: str | os.PathLike[str], runner: str | os.PathLike[str]) -> list[str]:
    return [str(python_exe), "-I", "-B", str(Path(runner))]
