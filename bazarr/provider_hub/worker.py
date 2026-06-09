# coding=utf-8
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
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


@dataclass
class WorkerResult:
    ok: bool
    payload: dict[str, Any]
    events: list[dict[str, Any]]


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

    def stop(self, grace_seconds: float = 5.0) -> None:
        process = self.process
        if not process:
            return
        if process.poll() is not None:
            return
        try:
            self.request("shutdown", {"reason": "app_shutdown", "grace_ms": int(grace_seconds * 1000)}, grace_seconds)
            process.wait(timeout=grace_seconds)
        except Exception:
            process.kill()
            process.wait(timeout=grace_seconds)

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
        try:
            process.kill()
        except Exception:
            logger.exception("failed to kill provider worker")
        finally:
            try:
                process.wait(timeout=5.0)
            except Exception:
                pass

    def select_archive_member(
        self, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> WorkerResult:
        """Ask the worker to language-pin a member from a host-listed archive."""
        return self.request(
            "select_archive_member", payload, timeout=30.0 if timeout is None else timeout
        )

    def request(self, op: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> WorkerResult:
        self.start()
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise WorkerError("worker process did not start")

        request_id = str(uuid.uuid4())
        message = {
            "abi": WORKER_ABI_VERSION,
            "id": request_id,
            "op": op,
            "deadline_ms": int(timeout * 1000),
            "payload": payload or {},
        }

        with self._lock:
            self.process.stdin.write(
                json.dumps(message, separators=(",", ":"), default=_json_default)
                + "\n"
            )
            self.process.stdin.flush()
            line = self._read_line_with_deadline(timeout)

        if not line:
            raise WorkerError("worker closed stdout")

        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise WorkerError("worker returned malformed JSON") from error

        if response.get("abi") != WORKER_ABI_VERSION:
            raise WorkerError("worker returned unsupported ABI")
        if response.get("id") != request_id:
            raise WorkerError("worker returned mismatched request id")

        if not response.get("ok", False):
            error = response.get("error") or {}
            message = error.get("message") or error.get("code") or "worker request failed"
            raise WorkerError(str(message))

        payload = response.get("payload") or {}
        events = response.get("events") or []
        if not isinstance(payload, dict):
            raise WorkerError("worker payload must be an object")
        if not isinstance(events, list):
            events = []
        return WorkerResult(ok=True, payload=payload, events=events)


def worker_command(python_exe: str | os.PathLike[str], runner: str | os.PathLike[str]) -> list[str]:
    return [str(python_exe), "-I", "-B", str(Path(runner))]
