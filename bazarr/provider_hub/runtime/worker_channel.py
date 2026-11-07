# coding=utf-8
"""Worker half of protocol v2, a reference loop on top of ``frames``.

Stdlib only and free of Bazarr imports, like ``frames``, so a plugin SDK can
vendor both. The fake worker is built on it; the SDK runner may use it as is
or write its own loop on ``frames``.

A handler is ``handler(operation) -> dict | OperationResult``. Each request
runs on its own thread; the main thread only reads frames, so a cancel, an
ack or a shutdown is always seen while an operation runs. Raise
``OperationError`` for an error the host should classify, ``OperationCancelled``
after seeing ``operation.cancelled``. Anything else becomes ``plugin_error``
with the traceback tail.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import traceback

try:
    from . import frames
except ImportError:  # run as a plain script next to frames.py
    import frames  # type: ignore[no-redef]
Codes = frames.Codes

_TRACEBACK_LIMIT = 8000


class OperationError(Exception):
    def __init__(self, code, message="", *, retryable=False, retry_after=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.retry_after = retry_after


class OperationCancelled(Exception):
    pass


class HostCallFailed(Exception):
    """The host answered a call with an error ack."""

    def __init__(self, code, message="", retryable=False):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable


class ChannelClosed(Exception):
    """The pipe or the session ended while the operation waited on the host."""


class OperationResult:
    def __init__(self, result=None, data=b""):
        self.result = result or {}
        self.data = data


class Operation:
    """What a handler sees for one request."""

    def __init__(self, channel, message):
        self._channel = channel
        self.id = message["id"]
        self.op = message["op"]
        self.params = message["params"]
        self.secrets = message.get("secrets") or {}
        self.deadline_ms = message.get("deadline_ms")
        self.max_result_bytes = message.get("max_result_bytes")
        self.cancelled = threading.Event()
        self.cancel_reason = None

    def progress(self, fraction=None, message=None, data=None):
        payload = {"t": frames.PROGRESS, "id": self.id}
        if fraction is not None:
            payload["fraction"] = fraction
        if message is not None:
            payload["message"] = message
        if data is not None:
            payload["data"] = data
        self._channel._send(payload)

    def call(self, name, params=None, timeout=60.0):
        """Ask the host something and wait for the ack. Returns the result dict."""
        return self._channel.call(name, params, request_id=self.id, timeout=timeout)

    def sandbox_blocked(self, target, errno=None):
        self._channel.sandbox_blocked(target, errno=errno, request_id=self.id)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise OperationCancelled(self.cancel_reason or "cancelled")


class WorkerChannel:
    """Runs the worker side of one session.

    ``redirect_stdout=True`` (the default for a real worker process) keeps the
    original stdout and stdin for frames, then points fd 1 at stderr and fd 0
    at the null device, so a stray ``print`` or ``input`` cannot touch the
    stream.
    """

    def __init__(self, handler, *, kinds, protocols=None, max_in_flight=frames.DEFAULT_MAX_IN_FLIGHT,
                 worker=None, stdin=None, stdout=None, redirect_stdout=True,
                 heartbeat_interval=None, wait_grace=5.0):
        self._handler = handler
        self._kinds = list(kinds)
        self._protocols = protocols or [{"major": frames.PROTOCOL_MAJOR, "minor": frames.PROTOCOL_MINOR}]
        self._max_in_flight = max_in_flight
        self._worker = worker or {}
        self._heartbeat_interval = heartbeat_interval
        self._wait_grace = wait_grace
        if stdin is None or stdout is None:
            stdin, stdout = self._take_stdio(redirect_stdout)
        self._reader = frames.FrameReader(stdin, {frames.FRAME_CONTROL: frames.HANDSHAKE_CONTROL_BYTES})
        self._writer = frames.FrameWriter(stdout)
        self._lock = threading.Lock()
        self._operations = {}
        self._threads = []
        self._pending_calls = {}
        self._next_cid = 1
        self._closed = threading.Event()
        self._stopping = threading.Event()
        self._hb_seq = 0
        self.session = None
        self.protocol = None
        self.kind = None
        self.exit_status = 0

    @staticmethod
    def _take_stdio(redirect):
        stdin = os.fdopen(os.dup(0), "rb")
        stdout = os.fdopen(os.dup(1), "wb")
        if redirect:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os.dup2(2, 1)
            # print() goes through sys.stdout's own buffer; point it at stderr too so
            # it is not held back until exit.
            sys.stdout = sys.stderr
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.close(devnull)
        return stdin, stdout

    # --------------------------------------------------------------- sending

    def _send(self, message):
        if self._closed.is_set():
            raise ChannelClosed("channel is closed")
        try:
            self._writer.write_control(message)
        except (OSError, ValueError) as error:
            self._close()
            raise ChannelClosed(str(error)) from error

    def call(self, name, params=None, request_id=None, timeout=60.0):
        slot = {"event": threading.Event()}
        with self._lock:
            cid = self._next_cid
            self._next_cid += 1
            self._pending_calls[cid] = slot
        message = {"t": frames.CALL, "cid": cid, "name": name, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        try:
            self._send(message)
            if not slot["event"].wait(timeout):
                raise HostCallFailed(Codes.TIMEOUT, "host did not answer %s in %.1fs" % (name, timeout))
        finally:
            with self._lock:
                self._pending_calls.pop(cid, None)
        ack = slot.get("ack")
        if ack is None:
            raise ChannelClosed("channel closed while waiting for %s" % name)
        if ack["ok"]:
            return ack.get("result") or {}
        error = ack["error"]
        raise HostCallFailed(error.get("code") or Codes.HOST_CALL_FAILED, error.get("message") or "",
                             error.get("retryable") is True)

    def sandbox_blocked(self, target, errno=None, request_id=None):
        message = {"t": frames.SANDBOX_BLOCKED, "target": target}
        if errno is not None:
            message["errno"] = errno
        if request_id is not None:
            message["id"] = request_id
        self._send(message)

    # ------------------------------------------------------------------ loop

    def run(self):
        """Handshake and serve until shutdown or EOF. Returns the exit status."""
        try:
            self._handshake()
        except frames.FrameError:
            return 3
        except (OSError, ValueError):
            return 3
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat.start()
        try:
            while True:
                frame = self._reader.read_frame()
                if frame is None:
                    break
                ftype, body = frame
                if ftype == frames.FRAME_DATA:
                    raise frames.FrameError("host sent a data frame")
                if self._dispatch(frames.decode_control(body, "host")):
                    break
        except frames.FrameError:
            self.exit_status = 3
        except (OSError, ValueError):
            pass
        self._drain()
        return self.exit_status

    def _handshake(self):
        hello = {
            "t": frames.HELLO,
            "protocols": self._protocols,
            "kinds": self._kinds,
            "max_in_flight": self._max_in_flight,
            "worker": self._worker,
            "pid": os.getpid(),
        }
        self._writer.write_control(hello)
        frame = self._reader.read_frame()
        if frame is None:
            raise frames.MalformedFrame("host closed before welcome")
        ftype, body = frame
        if ftype != frames.FRAME_CONTROL:
            raise frames.MalformedFrame("host sent data before welcome")
        message = frames.decode_control(body, "host")
        if message["t"] == frames.REJECT:
            raise frames.MalformedFrame("host rejected the worker: %s" % message["message"])
        if message["t"] != frames.WELCOME:
            raise frames.MalformedFrame("expected welcome")
        self.session = message["session"]
        self.protocol = (message["protocol"].get("major"), message["protocol"].get("minor"))
        self.kind = message.get("kind")
        limits = message["limits"]
        if self._heartbeat_interval is None:
            interval = limits.get("heartbeat_interval_ms")
            self._heartbeat_interval = interval / 1000.0 if isinstance(interval, int) and interval > 0 \
                else frames.DEFAULT_HEARTBEAT_INTERVAL
        self._reader.caps[frames.FRAME_CONTROL] = frames.MAX_CONTROL_BYTES

    def _dispatch(self, message):
        """True means stop reading."""
        kind = message["t"]
        if kind == frames.REQ:
            self._start_operation(message)
        elif kind == frames.CANCEL:
            with self._lock:
                operation = self._operations.get(message["id"])
            if operation is not None:
                operation.cancel_reason = message.get("reason")
                operation.cancelled.set()
        elif kind == frames.ACK:
            with self._lock:
                slot = self._pending_calls.get(message["cid"])
            if slot is not None:
                slot["ack"] = message
                slot["event"].set()
        elif kind == frames.SHUTDOWN:
            self._stopping.set()
            return True
        return False

    def _start_operation(self, message):
        req_id = message["id"]
        with self._lock:
            if req_id in self._operations:
                busy = Codes.INVALID_REQUEST
            elif len(self._operations) >= self._max_in_flight:
                busy = Codes.BUSY
            else:
                busy = None
                operation = Operation(self, message)
                self._operations[req_id] = operation
        if busy is not None:
            self._respond_error(req_id, OperationError(busy, "request refused"))
            return
        thread = threading.Thread(target=self._run_operation, args=(operation,), daemon=True)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _run_operation(self, operation):
        try:
            try:
                value = self._handler(operation)
                if isinstance(value, OperationResult):
                    result, data = value.result, value.data
                else:
                    result, data = (value or {}), b""
                self._respond_ok(operation, result, data)
            except OperationCancelled:
                self._respond_error(operation.id, OperationError(Codes.CANCELLED, "cancelled"))
            except OperationError as error:
                self._respond_error(operation.id, error)
            except ChannelClosed:
                pass
            except Exception as error:
                self._respond_error(operation.id, error)
        except ChannelClosed:
            pass
        finally:
            with self._lock:
                self._operations.pop(operation.id, None)

    def _respond_ok(self, operation, result, data):
        cap = operation.max_result_bytes
        if cap is not None and len(data) > cap:
            raise OperationError(Codes.RESULT_TOO_LARGE, "result is %d bytes, the request allows %d" % (len(data), cap))
        message = {"t": frames.RES, "id": operation.id, "ok": True, "result": result}
        if data:
            chunks, total = self._writer.write_data(operation.id, data)
            message.update({"chunks": chunks, "bytes": total, "sha256": hashlib.sha256(data).hexdigest()})
        self._send(message)

    def _respond_error(self, req_id, error):
        if isinstance(error, OperationError):
            payload = frames.error_object(error.code, error.message, retryable=error.retryable,
                                          retry_after=error.retry_after)
        else:
            tail = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-_TRACEBACK_LIMIT:]
            print(tail, file=sys.stderr, flush=True)
            payload = frames.error_object(Codes.PLUGIN_ERROR, str(error), class_name=type(error).__name__,
                                          traceback=tail)
        self._send({"t": frames.RES, "id": req_id, "ok": False, "error": payload})

    def _heartbeat_loop(self):
        while not self._closed.wait(self._heartbeat_interval):
            self._hb_seq += 1
            message = {"t": frames.HB, "seq": self._hb_seq}
            with self._lock:
                message["in_flight"] = len(self._operations)
            rss = _rss_kb()
            if rss is not None:
                message["rss_kb"] = rss
            try:
                self._send(message)
            except ChannelClosed:
                return

    def _drain(self):
        """Cancel what still runs, give it a moment, say goodbye."""
        with self._lock:
            running = list(self._operations.values())
            threads = list(self._threads)
        for operation in running:
            operation.cancel_reason = "shutdown"
            operation.cancelled.set()
        for thread in threads:
            thread.join(timeout=self._wait_grace)
        if self._stopping.is_set():
            try:
                self._send({"t": frames.BYE})
            except ChannelClosed:
                pass
        self._close()

    def _close(self):
        self._closed.set()
        with self._lock:
            slots = list(self._pending_calls.values())
        for slot in slots:
            slot["event"].set()


def _rss_kb():
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None

