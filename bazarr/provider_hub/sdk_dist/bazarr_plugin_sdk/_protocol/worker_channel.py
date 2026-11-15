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
import selectors
import sys
import threading
import time
import traceback

try:
    from . import frames
except ImportError:  # run as a plain script next to frames.py
    import frames  # type: ignore[no-redef]
Codes = frames.Codes

_TRACEBACK_LIMIT = 8000
_MESSAGE_LIMIT = 4096
_CODE_LIMIT = 200


def _secret_parts(value):
    """A secret value plus every nonempty line of a multiline one.

    Logs and tracebacks are cut into lines, so each line of a multiline secret
    has to be scrubbed on its own. A line is kept at any length: a short line is
    still part of the secret.
    """
    parts = [value]
    if "\n" in value or "\r" in value:
        parts.extend(line.strip() for line in value.splitlines() if line.strip())
    return parts


def _secret_spellings(value):
    """Each part of a secret in every escaped spelling an error text may use."""
    out = []
    for part in _secret_parts(value):
        out.extend(frames.secret_forms(part))
    return out


def _passes_through(error):
    """Exits a thread boundary must not swallow after it has done its cleanup."""
    return isinstance(error, (KeyboardInterrupt, GeneratorExit))


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
        # Set once the handler has returned. The host forgets the request when
        # it reads the reply, so anything a stray thread sends for it later is
        # refused there; it is not sent at all.
        self.finished = threading.Event()

    def progress(self, fraction=None, message=None, data=None):
        if self.finished.is_set():
            return
        payload = {"t": frames.PROGRESS, "id": self.id}
        if fraction is not None:
            payload["fraction"] = fraction
        if message is not None:
            payload["message"] = message
        if data is not None:
            payload["data"] = data
        self._channel._send(payload)

    def call(self, name, params=None, timeout=60.0, cancellable=True):
        """Ask the host something and wait for the ack. Returns the result dict.

        While it waits, a cancel of this operation raises ``OperationCancelled``
        within a fraction of a second. Pass ``cancellable=False`` for a call
        that must still go out after a cancel, such as settling usage. Once the
        handler has returned the request is over: the call fails at once.
        """
        if self.finished.is_set():
            raise HostCallFailed(Codes.INVALID_REQUEST, "the request has finished")
        return self._channel.call(name, params, request_id=self.id, timeout=timeout,
                                  cancelled=self.cancelled if cancellable else None)

    def sandbox_blocked(self, target, errno=None):
        if self.finished.is_set():
            return
        self._channel.sandbox_blocked(target, errno=errno, request_id=self.id)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise OperationCancelled(self.cancel_reason or "cancelled")


def _select_on_pipes():
    """True where the selector can wait on a pipe descriptor (POSIX; on Windows only sockets work)."""
    return os.name == "posix"


class _WakeableInput:
    """The channel's input, with a read that another thread can end where the platform allows.

    A read blocked on a pipe whose peer keeps its end open never returns, and
    closing the descriptor does not wake it. On POSIX this waits on the
    descriptor and a private wake pipe together: ``interrupt`` makes the blocked
    ``read`` return ``b""``, the end of the stream. The stream must be unbuffered
    (it is read straight from its descriptor).

    Where ``select`` cannot wait on pipes (Windows), or the stream has no
    descriptor, the read is a plain blocking read and ``interruptible`` is
    False: the reader thread cannot be woken, so ``run`` abandons it (it is a
    daemon) and only attempts the close without waiting on it.
    """

    def __init__(self, stream):
        self._stream = stream
        self._fd = self._wake_r = self._wake_w = None
        self._selector = None
        if _select_on_pipes():
            try:
                self._fd = stream.fileno()
                self._wake_r, self._wake_w = os.pipe()
                # Not ``select.select``: that refuses descriptors past FD_SETSIZE (1024).
                self._selector = selectors.DefaultSelector()
                self._selector.register(self._fd, selectors.EVENT_READ)
                self._selector.register(self._wake_r, selectors.EVENT_READ)
            except (AttributeError, OSError, ValueError):
                self._release_wait()

    @property
    def interruptible(self):
        return self._fd is not None

    def read(self, size):
        if self._fd is None:
            return self._stream.read(size)
        while True:
            selector, fd, wake = self._selector, self._fd, self._wake_r
            try:
                ready = selector.select()
            except InterruptedError:
                continue
            except (AttributeError, OSError, ValueError):
                # Closed under a reader that was not woken in time: the end of the stream.
                return b""
            if any(key.fd == wake for key, _ in ready):
                return b""
            return os.read(fd, size)

    def interrupt(self):
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def _release_wait(self):
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass
        for fd in (self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._selector = None
        self._wake_r = self._wake_w = None
        self._fd = None

    def close(self):
        self._release_wait()
        try:
            self._stream.close()
        except Exception:
            pass


class _WakeableOutput:
    """The channel's output, with a write that another thread can end where the platform allows.

    A buffered stream blocked writing to a pipe the peer no longer reads holds
    its own lock, so nothing can close it. On POSIX this writes straight to the
    descriptor in non-blocking mode and waits for it to become writable together
    with a private wake pipe: ``interrupt`` makes a blocked ``write`` raise
    ``BrokenPipeError``, which the channel treats as a closed transport. Where
    that is not possible the writes go through the stream itself, and a write
    blocked on a stalled peer can outlive ``run()`` (its thread is a daemon).
    """

    def __init__(self, stream):
        self._stream = stream
        self._fd = self._wake_r = self._wake_w = None
        self._selector = None
        if _select_on_pipes():
            try:
                self._fd = stream.fileno()
                self._wake_r, self._wake_w = os.pipe()
                self._selector = selectors.DefaultSelector()
                self._selector.register(self._fd, selectors.EVENT_WRITE)
                self._selector.register(self._wake_r, selectors.EVENT_READ)
                os.set_blocking(self._fd, False)
            except (AttributeError, OSError, ValueError):
                self._release_wait()

    @property
    def interruptible(self):
        return self._fd is not None

    @property
    def closed(self):
        return self._stream.closed

    def write(self, data):
        if self._fd is None:
            return self._stream.write(data)
        view = memoryview(data)
        while view:
            fd = self._fd
            try:
                if fd is None:
                    raise BrokenPipeError("output closed")
                view = view[os.write(fd, view):]
                continue
            except BlockingIOError:
                pass
            selector, wake = self._selector, self._wake_r
            try:
                ready = selector.select()
            except InterruptedError:
                continue
            except (AttributeError, OSError, ValueError):
                raise BrokenPipeError("output closed") from None
            if any(key.fd == wake for key, _ in ready):
                raise BrokenPipeError("output closed")
        return len(data)

    def flush(self):
        if self._fd is None:
            self._stream.flush()

    def interrupt(self):
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def _release_wait(self):
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass
        for fd in (self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._selector = None
        self._wake_r = self._wake_w = None
        self._fd = None

    def close(self):
        self._release_wait()
        try:
            self._stream.close()
        except Exception:
            pass


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
        # The drain budget when the host did not name one (it hung up, or its
        # shutdown frame carried no grace). A grace in the frame replaces it.
        self._wait_grace = wait_grace
        self._shutdown_grace = None
        # What a result may weigh when a request names no cap; the welcome carries the host's.
        self._result_limit = frames.DEFAULT_MAX_RESULT_BYTES
        if stdin is None or stdout is None:
            stdin, stdout = self._take_stdio(redirect_stdout)
        # The channel owns both streams from here on and releases them when run() ends.
        self._input = _WakeableInput(stdin)
        self._reader = frames.FrameReader(self._input, {frames.FRAME_CONTROL: frames.HANDSHAKE_CONTROL_BYTES})
        self._output = _WakeableOutput(stdout)
        self._writer = frames.FrameWriter(self._output, sender="worker")
        self._reader_thread = None
        self._heartbeat_thread = None
        self._lock = threading.Lock()
        # Operations whose handler is running; these are what the in-flight cap counts.
        self._operations = {}
        # Operations whose handler returned and whose reply is being written.
        # They no longer hold a slot, but a cancel must still find them.
        self._transmitting = {}
        # Every secret value the host has sent this worker, whichever request
        # carried it, in every escaped spelling. A plugin may keep a secret past
        # its request, so an error reply is scrubbed with all of them, not only
        # its own request's.
        self._secret_values = set()
        self._threads = []
        # Ids of operations whose terminal reply has been written.
        self._replied = set()
        self._pending_calls = {}
        self._next_cid = 1
        self._closed = threading.Event()
        self._stopping = threading.Event()
        self._ended = threading.Event()
        # Set once no ack can arrive any more; a call made after that must not wait.
        self._acks_closed = False
        self._hb_seq = 0
        self.session = None
        self.protocol = None
        self.kind = None
        self.exit_status = 0

    @staticmethod
    def _take_stdio(redirect):
        # Unbuffered: the reader thread may still be blocked in a read when the
        # process exits, and a raw file has no lock for that thread to hold.
        stdin = os.fdopen(os.dup(0), "rb", buffering=0)
        try:
            stdout = os.fdopen(os.dup(1), "wb")
        except BaseException:
            stdin.close()
            raise
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
        # Serialization first, on its own: a message that cannot be encoded (not
        # JSON, NaN, over the cap) is the sender's problem and raises as it is,
        # to reach the per-request error reply. Only a failed write means the
        # transport is gone.
        data = frames.pack_control(message, self._writer.caps[frames.FRAME_CONTROL], sender="worker")
        try:
            self._writer.write_packed(data)
        except (OSError, ValueError) as error:
            self._close()
            raise ChannelClosed(str(error)) from error
        if message.get("t") == frames.RES:
            with self._lock:
                if message.get("id") in self._operations or message.get("id") in self._transmitting:
                    self._replied.add(message["id"])

    def call(self, name, params=None, request_id=None, timeout=60.0, cancelled=None):
        if cancelled is not None and cancelled.is_set():
            raise OperationCancelled("cancelled")
        slot = {"event": threading.Event()}
        with self._lock:
            if self._acks_closed or self._closed.is_set():
                raise ChannelClosed("channel closed before %s was sent" % name)
            cid = self._next_cid
            self._next_cid += 1
            self._pending_calls[cid] = slot
        message = {"t": frames.CALL, "cid": cid, "name": name, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        try:
            self._send(message)
            deadline = time.monotonic() + timeout
            while not slot["event"].wait(0.05 if cancelled is not None else max(0.0, deadline - time.monotonic())):
                if cancelled is not None and cancelled.is_set():
                    raise OperationCancelled("cancelled")
                if time.monotonic() >= deadline:
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
        """Handshake and serve until shutdown or EOF. Returns the exit status.

        However it ends, the channel's input and output are released before it
        returns: a reader blocked on a peer that keeps its end open is woken
        first, so no thread outlives ``run`` holding the streams.
        """
        try:
            return self._serve()
        finally:
            self._release_streams()

    def _serve(self):
        try:
            self._handshake()
        except frames.FrameError:
            return 3
        except (OSError, ValueError):
            return 3
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread = heartbeat
        try:
            heartbeat.start()
            # Frames are read on their own thread for the whole life of the channel,
            # so acks keep arriving while operations drain after a shutdown.
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()
        except Exception:
            # A thread could not start: the worker cannot serve. Stop what did
            # start and report failure rather than run half a channel.
            self._close()
            self._close_acks()
            if heartbeat.is_alive():
                heartbeat.join(timeout=2.0)
            return 3
        self._ended.wait()
        self._drain()
        return self.exit_status

    def _release_streams(self):
        status = self.exit_status
        self._close()
        # Wake a reader blocked in a read, then give it a moment to leave.
        interruptible = self._input.interruptible
        self._input.interrupt()
        for thread in (self._reader_thread, self._heartbeat_thread):
            if thread is self._reader_thread and not interruptible:
                # A blocking read cannot be woken: the reader is abandoned, not waited for.
                continue
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        if interruptible:
            self._input.close()
        else:
            # Closing a stream another thread is blocked reading may itself block.
            self._close_bounded(self._input.close)
        # A write blocked on a peer that stopped reading is ended by the wake pipe
        # (where the platform allows), so the threads stuck in it can leave.
        self._output.interrupt()
        end = time.monotonic() + 2.0
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, end - time.monotonic()))
        # Closing a buffered stream waits for a write in progress, so where the
        # write could not be interrupted the close runs on its own bounded thread.
        if self._output.interruptible:
            self._output.close()
        else:
            self._close_bounded(self._close_output)
        # A reader woken by the interrupt may report the cut stream as a failure.
        self.exit_status = status

    @staticmethod
    def _close_bounded(action):
        closer = threading.Thread(target=action, daemon=True)
        closer.start()
        closer.join(timeout=1.0)

    def _close_output(self):
        try:
            self._output.close()
        except Exception:
            pass

    def _read_loop(self):
        try:
            while True:
                frame = self._reader.read_frame()
                if frame is None:
                    break
                ftype, body = frame
                if ftype == frames.FRAME_DATA:
                    raise frames.FrameError("host sent a data frame")
                self._dispatch(frames.decode_control(body, "host"))
        except frames.FrameError:
            self.exit_status = 3
        except (OSError, ValueError):
            pass
        except BaseException:
            # Whatever else ends the reader ends the worker, with a failure
            # status. SystemExit and friends included: the reader thread dying
            # quietly would leave run() waiting on a channel nobody reads.
            self.exit_status = 3
        finally:
            self._ended.set()
            self._close_acks()

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
        limit = limits.get("max_result_bytes")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            self._result_limit = limit
        self._reader.caps[frames.FRAME_CONTROL] = frames.MAX_CONTROL_BYTES

    def _dispatch(self, message):
        """True means stop reading."""
        kind = message["t"]
        if kind == frames.REQ:
            self._start_operation(message)
        elif kind == frames.CANCEL:
            with self._lock:
                operation = self._operations.get(message["id"]) or self._transmitting.get(message["id"])
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
            # Reading goes on: draining operations may still wait for acks.
            grace_ms = message.get("grace_ms")
            if grace_ms is not None:
                self._shutdown_grace = grace_ms / 1000.0
            self._stopping.set()
            self._ended.set()
        return False

    def _remember_secrets(self, message):
        # Frame validation has already held this to a flat name -> non-empty text map.
        secrets = message.get("secrets") or {}
        with self._lock:
            for value in secrets.values():
                self._secret_values.update(_secret_spellings(value))

    def _start_operation(self, message):
        req_id = message["id"]
        self._remember_secrets(message)
        if self._stopping.is_set():
            self._respond_error(req_id, OperationError(Codes.SHUTTING_DOWN, "worker is shutting down"))
            return
        with self._lock:
            if req_id in self._operations or req_id in self._transmitting:
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
        try:
            thread.start()
        except Exception:
            # No thread, no reply: tell the host now instead of leaving it waiting.
            self._forget(operation)
            self._respond_error(req_id, OperationError(Codes.PLUGIN_ERROR, "the operation could not be started"))
            return
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)

    def _release(self, operation):
        """Free the operation's slot. It stays findable by a cancel until ``_forget``."""
        operation.finished.set()
        with self._lock:
            if self._operations.pop(operation.id, None) is not None:
                self._transmitting[operation.id] = operation

    def _forget(self, operation):
        with self._lock:
            self._replied.discard(operation.id)
            self._transmitting.pop(operation.id, None)
            self._operations.pop(operation.id, None)

    def _run_operation(self, operation):
        """Run one handler and answer it. Whatever the handler does, it is answered.

        The boundary catches ``BaseException``: a handler that calls
        ``sys.exit()`` raises ``SystemExit``, and a bare ``Exception`` clause
        would let it skip the reply and the cleanup, leaving the host to wait on
        a request nobody will answer. ``SystemExit`` fails that request only
        (``plugin_error``, the class name says what it was) and the channel keeps
        serving: a handler runs on a thread, where an exit would end the thread
        and nothing else. ``KeyboardInterrupt`` and ``GeneratorExit`` are
        answered the same way and then raised again once cleanup is done.
        """
        reply = None
        escaping = None
        try:
            try:
                value = self._handler(operation)
                if isinstance(value, OperationResult):
                    result, data = value.result, value.data
                else:
                    result, data = ({} if value is None else value), b""
                if not isinstance(result, dict):
                    # Not the host's protocol violation: this request's failure.
                    raise OperationError(
                        Codes.PLUGIN_ERROR, "the operation returned %s, not an object" % type(result).__name__)
                reply = ("ok", result, data)
            except OperationCancelled:
                reply = ("error", OperationError(Codes.CANCELLED, "cancelled"))
            except ChannelClosed:
                reply = None
            except BaseException as error:
                reply = ("error", error)
                if _passes_through(error):
                    escaping = error
            finally:
                # The slot is free before the reply goes out: the host may send
                # the next request the moment it reads this reply.
                self._release(operation)
            if reply is not None:
                try:
                    self._reply(operation, reply)
                except BaseException as failure:
                    # A reply that cannot be written at all leaves the host
                    # waiting: end the channel so the host sees the worker go.
                    if not _passes_through(failure):
                        self.exit_status = 3
                        self._close()
                    else:
                        escaping = escaping or failure
        finally:
            # Findable by a cancel until the terminal reply is written, and
            # gone whatever happened above. A request that reaches this point
            # without its terminal reply on the wire (building the reply raised
            # an interrupt, say) gets a fixed one, or the channel closes: the host
            # must never wait on an operation nobody will answer.
            self._ensure_replied(operation)
            self._forget(operation)
        if escaping is not None:
            raise escaping

    def _ensure_replied(self, operation):
        """Send a fixed failure for an operation that has no terminal reply. Never raises."""
        try:
            if self._closed.is_set():
                return
            with self._lock:
                replied = operation.id in self._replied
            if not replied:
                self._send({"t": frames.RES, "id": operation.id, "ok": False,
                            "error": frames.error_object(Codes.PLUGIN_ERROR, "the operation ended without a reply")})
        except BaseException:
            # Nothing more can be said for this request: end the channel so the
            # host sees the worker go instead of waiting.
            self.exit_status = 3
            self._close()

    def _reply(self, operation, reply):
        try:
            try:
                if reply[0] == "ok":
                    try:
                        self._respond_ok(operation, reply[1], reply[2])
                    except (ChannelClosed, OperationCancelled):
                        raise
                    except BaseException as error:
                        if _passes_through(error):
                            raise
                        self._respond_error(operation.id, error)
                else:
                    self._respond_error(operation.id, reply[1])
            except OperationCancelled:
                # Cancelled while the result was being sent: stop, and say so.
                self._respond_error(operation.id, OperationError(Codes.CANCELLED, "cancelled"))
        except ChannelClosed:
            pass

    def _respond_ok(self, operation, result, data):
        if not isinstance(result, dict):
            raise OperationError(Codes.PLUGIN_ERROR, "the result must be an object")
        # The cap covers the whole result: the JSON of the inline part and every data byte.
        # A request without one gets the host's limit from the welcome.
        cap = operation.max_result_bytes
        if cap is None or cap > self._result_limit:
            cap = self._result_limit
        size = len(data) + len(frames.encode_control(result))
        if size > cap:
            raise OperationError(Codes.RESULT_TOO_LARGE, "result is %d bytes, the request allows %d" % (size, cap))
        message = {"t": frames.RES, "id": operation.id, "ok": True, "result": result}
        if data:
            # A cancel is honoured between chunks, not only before the transfer.
            chunks, total = self._writer.write_data(operation.id, data, before_chunk=operation.check_cancelled)
            message.update({"chunks": chunks, "bytes": total, "sha256": hashlib.sha256(data).hexdigest()})
        self._send(message)

    def _respond_error(self, req_id, error):
        """Send the terminal error reply for a request. Always sends something.

        Every secret value this worker has received, from any request, is
        replaced in the full text before it is cut to size, so a cut cannot
        leave half of a secret. Every text field is
        bounded after that. If the reply cannot be built or serialized, a small
        fixed error goes out instead, so the host never waits on a request that
        has no terminal reply.
        """
        with self._lock:
            secrets = sorted(self._secret_values, key=len, reverse=True)

        def clean(text, limit):
            text = text if isinstance(text, str) else str(text)
            for secret in secrets:
                text = text.replace(secret, "***")
            return text[:limit]

        try:
            if isinstance(error, OperationError):
                retry_after = error.retry_after
                if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool) \
                        or retry_after != retry_after or retry_after in (float("inf"), float("-inf")):
                    retry_after = None
                payload = frames.error_object(clean(error.code, _CODE_LIMIT), clean(error.message, _MESSAGE_LIMIT),
                                              retryable=error.retryable is True, retry_after=retry_after)
            else:
                full = clean("".join(traceback.format_exception(type(error), error, error.__traceback__)), 1 << 20)
                tail = full[-_TRACEBACK_LIMIT:]
                print(tail, file=sys.stderr, flush=True)
                payload = frames.error_object(Codes.PLUGIN_ERROR, clean(str(error), _MESSAGE_LIMIT),
                                              class_name=clean(type(error).__name__, _CODE_LIMIT), traceback=tail)
            self._send({"t": frames.RES, "id": req_id, "ok": False, "error": payload})
        except ChannelClosed:
            raise
        except BaseException as caught:
            # An error object whose text raises SystemExit is still only a failed reply.
            if _passes_through(caught):
                raise
            self._send({"t": frames.RES, "id": req_id, "ok": False,
                        "error": frames.error_object(Codes.PLUGIN_ERROR, "the error reply could not be sent")})

    def _heartbeat_loop(self):
        try:
            self._beat()
        except BaseException:
            # A heartbeat that dies silently reads as a hung worker to the host.
            # Say so instead: fail and let run() return.
            self.exit_status = 3
            self._close()

    def _beat(self):
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
        """Cancel what still runs, give it a moment, say goodbye.

        The reader thread keeps delivering acks meanwhile, so an operation that
        settles usage after its cancel is not left waiting.
        """
        with self._lock:
            running = list(self._operations.values()) + list(self._transmitting.values())
            threads = list(self._threads)
        for operation in running:
            operation.cancel_reason = "shutdown"
            operation.cancelled.set()
        grace = self._shutdown_grace if self._shutdown_grace is not None else self._wait_grace
        deadline = time.monotonic() + grace
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # The goodbye is a write that can block on a full pipe or a held frame
        # lock. It is skipped when the budget ran out with work still running,
        # and otherwise bounded by what is left of it (a quarter second at least).
        if self._stopping.is_set() and not any(thread.is_alive() for thread in threads):
            self._say_bye(max(deadline - time.monotonic(), _BYE_FLOOR))
        self._close()

    def _say_bye(self, budget):
        def write():
            try:
                self._send({"t": frames.BYE})
            except ChannelClosed:
                pass

        sender = threading.Thread(target=write, daemon=True)
        sender.start()
        sender.join(timeout=budget)

    def _close_acks(self):
        """The host side is gone: no ack will come, so waiting calls give up."""
        with self._lock:
            self._acks_closed = True
            slots = list(self._pending_calls.values())
        for slot in slots:
            slot["event"].set()

    def _close(self):
        self._closed.set()
        # The channel can no longer write: run() has nothing left to wait for.
        self._ended.set()
        with self._lock:
            slots = list(self._pending_calls.values())
        for slot in slots:
            slot["event"].set()


_BYE_FLOOR = 0.25


def _rss_kb():
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None

