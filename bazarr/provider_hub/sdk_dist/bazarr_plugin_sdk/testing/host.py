"""A stand-in for the Bazarr+ host, for plugin tests and the conformance suite.

:class:`LoopbackHost` serves the plugin on threads of the current process over
two pipes. :class:`ProcessHost` starts a real worker process the way the host
does (``python -I``, frames on the original stdin and stdout). Both are a
:class:`Host`: it answers the handshake, runs the host calls a worker makes
(``reserve``, ``usage``, ``checkpoint`` get a default answer you can replace),
and collects each request's progress, result and error.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
import threading
import time

from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

from .._protocol import frames

HostCallHandler = Callable[[Mapping[str, Any]], "Mapping[str, Any] | None"]


class HostFailure(Exception):
    """The worker did something a host would refuse, or never did what it must.

    A host call handler may raise it to answer the worker with an error ack
    of that ``code``.
    """

    def __init__(self, message: str, code: str = frames.Codes.PROTOCOL_ERROR, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


class Response:
    """What came back for one request."""

    def __init__(self, req_id: int, op: str) -> None:
        self.id = req_id
        self.op = op
        self.result: dict[str, Any] = {}
        self.data = b""
        self.error: dict[str, Any] | None = None
        self.progress: list[dict[str, Any]] = []
        self.done = threading.Event()
        self.cancel_sent_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def code(self) -> str | None:
        return None if self.error is None else self.error.get("code")

    @property
    def value(self) -> Any:
        """The method's return value: unwrapped from data frames and from ``{"value": ...}``."""
        if self.result.get("json_in_data"):
            return json.loads(self.data.decode("utf-8"))
        if set(self.result) == {"value"}:
            return self.result["value"]
        return self.result

    def wait(self, timeout: float = 10.0) -> "Response":
        if not self.done.wait(timeout):
            raise HostFailure(f"no response to {self.op} within {timeout}s", frames.Codes.TIMEOUT)
        return self


def default_host_calls() -> dict[str, HostCallHandler]:
    counter = itertools.count(1)
    return {
        "reserve": lambda params: {"hold": f"hold-{next(counter)}"},
        "usage": lambda params: {},
        "checkpoint": lambda params: {},
    }


class Host:
    """The host end of one worker pipe."""

    def __init__(
        self,
        to_worker: BinaryIO,
        from_worker: BinaryIO,
        *,
        kind: str = "translator",
        kind_versions: Sequence[tuple[int, int]] = ((1, 0),),
        heartbeat_interval: float = 15.0,
        max_in_flight: int = 4,
        host_calls: Mapping[str, HostCallHandler] | None = None,
    ) -> None:
        self._writer = frames.FrameWriter(to_worker)
        self._reader = frames.FrameReader(
            from_worker, {frames.FRAME_CONTROL: frames.HANDSHAKE_CONTROL_BYTES, frames.FRAME_DATA: frames.MAX_DATA_BYTES}
        )
        self.kind = kind
        self._kind_versions = tuple(kind_versions)
        self._heartbeat_interval = heartbeat_interval
        self._max_in_flight = max_in_flight
        self.host_calls: dict[str, HostCallHandler] = {**default_host_calls(), **(host_calls or {})}
        self.hello: dict[str, Any] = {}
        self.welcome: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []
        self.calls: list[tuple[float, str, dict[str, Any]]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.blocked: list[dict[str, Any]] = []
        self.failure: BaseException | None = None
        self.bye = threading.Event()
        self.closed = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._responses: dict[int, Response] = {}
        self._chunks: dict[int, list[bytes]] = {}
        self._thread = threading.Thread(target=self._read_loop, name="host-reader", daemon=True)
        self._thread.start()

    # conversation

    def wait_ready(self, timeout: float = 10.0) -> dict[str, Any]:
        if not self._ready.wait(timeout):
            raise HostFailure("the worker never said hello", frames.Codes.HANDSHAKE_FAILED)
        if self.failure is not None and not self.welcome:
            raise HostFailure(str(self.failure), frames.Codes.HANDSHAKE_FAILED)
        return self.welcome

    def submit(
        self,
        op: str,
        params: Mapping[str, Any] | None = None,
        secrets: Mapping[str, Any] | None = None,
        *,
        max_result_bytes: int = frames.DEFAULT_MAX_RESULT_BYTES,
        timeout: float | None = None,
    ) -> Response:
        req_id = next(self._ids)
        response = Response(req_id, op)
        with self._lock:
            self._responses[req_id] = response
        message: dict[str, Any] = {
            "t": frames.REQ,
            "id": req_id,
            "op": op,
            "params": dict(params or {}),
            "max_result_bytes": max_result_bytes,
        }
        if timeout is not None:
            message["deadline_ms"] = int(timeout * 1000)
        if secrets:
            message["secrets"] = dict(secrets)
        self._writer.write_control(message)
        return response

    def request(self, op: str, params: Mapping[str, Any] | None = None, secrets: Mapping[str, Any] | None = None, *, wait: float = 10.0, **kwargs: Any) -> Response:
        return self.submit(op, params, secrets, **kwargs).wait(wait)

    def cancel(self, response: Response, reason: str = "cancelled") -> None:
        response.cancel_sent_at = time.monotonic()
        self._writer.write_control({"t": frames.CANCEL, "id": response.id, "reason": reason})

    def send(self, message: Mapping[str, Any]) -> None:
        self._writer.write_control(message)

    def shutdown(self, grace_ms: int = 2000) -> None:
        try:
            self._writer.write_control({"t": frames.SHUTDOWN, "grace_ms": grace_ms})
        except (OSError, ValueError):
            pass

    # reading

    def _read_loop(self) -> None:
        try:
            while True:
                frame = self._reader.read_frame()
                if frame is None:
                    break
                ftype, body = frame
                if ftype == frames.FRAME_DATA:
                    op_id, chunk = frames.split_data(body)
                    with self._lock:
                        self._chunks.setdefault(op_id, []).append(chunk)
                else:
                    self._on_message(frames.decode_control(body, "worker"))
        except (frames.FrameError, OSError, ValueError) as error:
            self.failure = error
        finally:
            self.closed.set()
            self._ready.set()
            with self._lock:
                pending = list(self._responses.values())
                self._responses.clear()
            for response in pending:
                if response.error is None:
                    response.error = frames.error_object(
                        frames.Codes.WORKER_EXITED, str(self.failure or "the worker closed the pipe")
                    )
                response.done.set()

    def _on_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        kind = message["t"]
        if kind == frames.HELLO:
            self._on_hello(message)
        elif kind == frames.HB:
            self.heartbeats.append(message)
        elif kind == frames.PROGRESS:
            with self._lock:
                response = self._responses.get(message["id"])
            if response is not None:
                response.progress.append(message)
        elif kind == frames.SANDBOX_BLOCKED:
            self.blocked.append(message)
        elif kind == frames.CALL:
            threading.Thread(target=self._answer_call, args=(message,), daemon=True).start()
        elif kind == frames.RES:
            self._on_result(message)
        elif kind == frames.BYE:
            self.bye.set()

    def _on_hello(self, hello: dict[str, Any]) -> None:
        self.hello = hello
        protocol = frames.negotiate_protocol(hello["protocols"])
        kind = frames.negotiate_kind(hello["kinds"], self.kind, self._kind_versions)
        if protocol is None or kind is None:
            self._writer.write_control(
                {"t": frames.REJECT, "code": frames.Codes.VERSION_MISMATCH, "message": "no common protocol or kind"}
            )
            self.failure = HostFailure("version mismatch", frames.Codes.VERSION_MISMATCH)
            self._ready.set()
            return
        self.welcome = {
            "t": frames.WELCOME,
            "protocol": {"major": protocol[0], "minor": protocol[1]},
            "kind": kind,
            "limits": {
                "heartbeat_interval_ms": int(self._heartbeat_interval * 1000),
                "max_in_flight": self._max_in_flight,
                "max_result_bytes": frames.DEFAULT_MAX_RESULT_BYTES,
            },
            "session": f"test-{os.getpid()}",
        }
        self._writer.write_control(self.welcome)
        self._reader.caps[frames.FRAME_CONTROL] = frames.MAX_CONTROL_BYTES
        self._ready.set()

    def _answer_call(self, message: dict[str, Any]) -> None:
        name, params = message["name"], message["params"]
        self.calls.append((time.monotonic(), name, params))
        handler = self.host_calls.get(name)
        if handler is None:
            ack = {"t": frames.ACK, "cid": message["cid"], "ok": False, "error": frames.error_object(frames.Codes.UNKNOWN_CALL, f"no host call named {name}")}
        else:
            try:
                ack = {"t": frames.ACK, "cid": message["cid"], "ok": True, "result": dict(handler(params) or {})}
            except HostFailure as failure:
                ack = {"t": frames.ACK, "cid": message["cid"], "ok": False, "error": frames.error_object(failure.code, failure.message, retryable=failure.retryable)}
        try:
            self._writer.write_control(ack)
        except (OSError, ValueError):
            pass

    def _on_result(self, message: dict[str, Any]) -> None:
        with self._lock:
            response = self._responses.pop(message["id"], None)
            chunks = self._chunks.pop(message["id"], [])
        if response is None:
            return
        if message["ok"]:
            data = b"".join(chunks)
            if message.get("bytes") is not None and message["bytes"] != len(data):
                response.error = frames.error_object(frames.Codes.PROTOCOL_ERROR, "byte count mismatch")
            elif message.get("sha256") is not None and message["sha256"] != hashlib.sha256(data).hexdigest():
                response.error = frames.error_object(frames.Codes.PROTOCOL_ERROR, "checksum mismatch")
            response.result = message.get("result") or {}
            response.data = data
        else:
            response.error = message["error"]
        response.done.set()


class LoopbackHost(Host):
    """Serves the plugin on threads of this process. No file descriptor of the process changes."""

    def __init__(
        self,
        plugin_class: type,
        *,
        heartbeat_interval: float = 15.0,
        max_in_flight: int = 4,
        host_calls: Mapping[str, HostCallHandler] | None = None,
        kind_versions: Sequence[tuple[int, int]] = ((1, 0),),
        wait_grace: float = 2.0,
    ) -> None:
        from ..runner import PluginWorker, build_plugin

        to_worker_r, to_worker_w = os.pipe()
        from_worker_r, from_worker_w = os.pipe()
        # Unbuffered: a reader thread may be blocked in a read when a file is closed, and a
        # buffered file would make the close wait for it.
        self._files = [
            os.fdopen(to_worker_r, "rb", buffering=0),
            os.fdopen(to_worker_w, "wb", buffering=0),
            os.fdopen(from_worker_r, "rb", buffering=0),
            os.fdopen(from_worker_w, "wb", buffering=0),
        ]
        plugin, spec = build_plugin(plugin_class)
        self.worker = PluginWorker(
            plugin, spec, stdin=self._files[0], stdout=self._files[3], redirect_stdout=False, wait_grace=wait_grace
        )
        self.exit_code: int | None = None
        self._worker_thread = threading.Thread(target=self._run, name="loopback-worker", daemon=True)
        self._worker_thread.start()
        super().__init__(
            self._files[1],
            self._files[2],
            heartbeat_interval=heartbeat_interval,
            max_in_flight=max_in_flight,
            host_calls=host_calls,
            kind_versions=kind_versions,
        )

    def _run(self) -> None:
        self.exit_code = self.worker.run()

    def close(self, timeout: float = 5.0) -> int | None:
        self.shutdown()
        # The host's write end first: it gives the worker's reader thread its end of input.
        self._close_file(1)
        self._worker_thread.join(timeout)
        self._close_file(3)
        self._thread.join(timeout)
        self._close_file(0)
        self._close_file(2)
        return self.exit_code

    def _close_file(self, index: int) -> None:
        try:
            self._files[index].close()
        except OSError:
            pass

    def __enter__(self) -> "LoopbackHost":
        self.wait_ready()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ProcessHost(Host):
    """Starts a worker process as the host does and talks to it over its stdin and stdout."""

    def __init__(
        self,
        entry: str,
        *,
        paths: Sequence[str | os.PathLike[str]] = (),
        python: str | None = None,
        sdk_root: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        **host_options: Any,
    ) -> None:
        bootstrap = Path(__file__).resolve().parent.parent / "_bootstrap.py"
        if sdk_root is not None:
            bootstrap = Path(sdk_root) / "bazarr_plugin_sdk" / "_bootstrap.py"
        argv = [python or sys.executable, "-I", str(bootstrap), "--entry", entry]
        for path in paths:
            argv += ["--path", os.fspath(path)]
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else {"PATH": os.environ.get("PATH", "")},
            cwd=cwd,
        )
        assert self.process.stdin is not None and self.process.stdout is not None and self.process.stderr is not None
        self.stderr_text = ""
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        super().__init__(self.process.stdin, self.process.stdout, **host_options)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        lines: list[str] = []
        for line in iter(self.process.stderr.readline, b""):
            lines.append(line.decode("utf-8", "replace"))
            self.stderr_text = "".join(lines)

    def close(self, timeout: float = 5.0) -> int:
        self.shutdown()
        try:
            self.process.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            code = self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait()
        self._stderr_thread.join(2)
        return code

    def __enter__(self) -> "ProcessHost":
        self.wait_ready()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
