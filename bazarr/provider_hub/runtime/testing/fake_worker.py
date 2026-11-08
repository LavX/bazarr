# coding=utf-8
"""A scriptable v2 worker for tests, standing in for a translator plugin.

Run it as a script (``python -B fake_worker.py``). It needs only the standard
library and the sibling ``frames.py`` and ``worker_channel.py``. Behavior is
set by the ``BAZARR_FAKE_WORKER`` environment variable (a JSON object, see
``CONFIG_KEYS``) and by the operation a test sends.

Operations:

``echo``           returns the params.
``sleep``          waits ``seconds``, cancellable unless ``ignore_cancel``;
                   with ``announce`` it sends a "started" progress message first.
``progress``       sends ``steps`` progress messages, then returns.
``chunks``         returns ``size`` bytes of data (``overshoot`` sends them
                   without honoring the request's limit; ``pause_file`` is
                   written after the first chunk, then the stream waits for a cancel).
``call``           makes the host call ``name`` with ``params`` and returns the ack.
``translate``      a fake translator: reserve, work in batches with a
                   checkpoint each, report usage, return the lines. Knobs in
                   params: ``lines``, ``target``, ``batch``, ``delay``,
                   ``skip_reserve``, ``fail`` (an error object) raised after
                   batch ``fail_at_batch`` (default 1), ``die_after`` (the
                   process exits hard: ``reserve``, ``batch:N`` or ``work``,
                   the last being after the work and before usage),
                   ``usage_on_failure`` (report partial usage when the
                   operation fails or is cancelled). The ``script`` setting
                   is a list of knob dicts applied to the 1st, 2nd, ...
                   translate request this process serves (the last repeats),
                   which scripts failover across attempts.
``spam``           prints to stdout and writes to stderr, then returns.
``oversize_frame`` writes a header that claims a huge control frame.
``bad_frame``      writes a control frame that is not valid JSON.
``fork``           starts a child that sleeps and returns its pid. It stays in
                   the worker's process group unless ``detached`` is set, and
                   keeps the worker's output pipes with ``inherit``.
``leak``           writes the request's secret values to stderr.
``leak_error``     raises an exception whose message starts with the request's
                   secret, ``tail`` characters long after it. With ``part``
                   only that line of a multiline secret is used.
``leak_seen_error`` raises an exception carrying a secret from an earlier
                   request: ``pad`` characters, the secret, ``tail`` more.
``parallel_calls``  makes the host calls named in ``names``, each from its own thread.
``list_result``     returns a JSON array instead of an object.
``bad_send``       calls progress, sandbox_blocked or call with arguments that break the schema (``kind``).
``unencodable_result``  returns a result the strict encoder refuses (``kind``: deep, duplicate_keys, surrogate).
``nan_result``      returns a result that cannot be encoded as JSON.
``bad_str``        raises an exception that cannot be turned into text.
``raw_error``      answers with an error carrying the secret, uncleaned.
``progress_secret`` sends a progress message carrying the secret.
``blocked``        reports a refused connect, then fails with sandbox_blocked.
``alloc``          touches ``mb`` megabytes and holds them for ``seconds``
                   (cancellable), as a memory hog does.
``exit``           ends the process with ``code``.
``raise``          raises a plain exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUNTIME = os.path.dirname(_HERE)
if _RUNTIME not in sys.path:
    sys.path.insert(0, _RUNTIME)
import frames  # noqa: E402
import worker_channel  # noqa: E402

ENV_NAME = "BAZARR_FAKE_WORKER"
CONFIG_KEYS = (
    "kinds",          # the hello kinds list
    "protocols",      # the hello protocols list
    "max_in_flight",
    "silent",         # send no heartbeats
    "script",         # translate outcomes by attempt (see the module docstring)
    "no_hello",       # never say hello (a v1 runner looks like this)
    "hello_delay",    # seconds before the hello
    "stubborn",       # ignore SIGTERM and the shutdown frame
)


def fake_worker_command():
    """The argv that runs this file with the current interpreter."""
    return [sys.executable, "-B", os.path.abspath(__file__)]


def fake_worker_env(**config):
    """The environment entry that configures the worker."""
    unknown = set(config) - set(CONFIG_KEYS)
    if unknown:
        raise ValueError("unknown fake worker settings: %s" % ", ".join(sorted(unknown)))
    return {ENV_NAME: json.dumps(config)}


def _sleep(operation, params):
    seconds = float(params.get("seconds", 1.0))
    if params.get("announce"):
        # Lets the host know the operation is running before it acts on it.
        operation.progress(message="started")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if operation.cancelled.wait(0.01) and not params.get("ignore_cancel"):
            raise worker_channel.OperationCancelled()
    return {"slept": seconds}


_STATE = {"attempts": 0, "config": {}}


def _knobs(params):
    """The request params, overlaid with this attempt's scripted outcome."""
    knobs = dict(params)
    script = _STATE["config"].get("script")
    attempt = _STATE["attempts"]
    _STATE["attempts"] += 1
    knobs["attempt"] = attempt
    if script:
        knobs.update(script[min(attempt, len(script) - 1)])
    return knobs


def _raise_scripted(failure):
    raise worker_channel.OperationError(
        failure.get("code", "plugin_error"), failure.get("message", "failed"),
        retryable=bool(failure.get("retryable")), retry_after=failure.get("retry_after"))


def _translate(operation, params):
    knobs = _knobs(params)
    lines = list(knobs.get("lines") or [])
    target = knobs.get("target", "xx")
    batch = max(1, int(knobs.get("batch", 2)))
    delay = float(knobs.get("delay", 0.0))
    die_after = knobs.get("die_after")
    if not knobs.get("skip_reserve"):
        operation.call("reserve", {"units": len(lines), "target": target})
    if die_after == "reserve":
        os._exit(9)
    out = []

    def report_usage():
        operation.call("usage", {"units": len(out), "target": target, "partial": True}, cancellable=False)

    try:
        for number, start in enumerate(range(0, len(lines), batch), 1):
            operation.check_cancelled()
            chunk = lines[start:start + batch]
            if delay:
                time.sleep(delay)
            out.extend("[%s] %s" % (target, line) for line in chunk)
            operation.progress(fraction=len(out) / max(1, len(lines)), message="translated %d" % len(out))
            operation.call("checkpoint", {"done": len(out), "lines": out[start:start + batch]})
            if die_after == "batch:%d" % number:
                os._exit(9)
            failure = knobs.get("fail")
            if failure and number == int(knobs.get("fail_at_batch", 1)):
                _raise_scripted(failure)
    except (worker_channel.OperationCancelled, worker_channel.OperationError, worker_channel.HostCallFailed):
        if knobs.get("usage_on_failure"):
            report_usage()
        raise
    if die_after == "work":
        os._exit(9)
    operation.call("usage", {"units": len(lines), "target": target})
    return {"lines": out, "target": target, "attempt": knobs["attempt"]}


def _unencodable(kind):
    """A result the host's decoder would refuse, of the named kind."""
    if kind == "deep":
        value = "bottom"
        for _ in range(frames.MAX_JSON_DEPTH + 1):
            value = [value]
        return {"deep": value}
    if kind == "duplicate_keys":
        return {"x": {1: "a", "1": "b"}}
    if kind == "surrogate":
        return {"x": "\ud800"}
    raise ValueError(kind)


def _handle(operation):
    params = operation.params
    name = operation.op
    # A careless plugin keeps what it was given; leak_seen_error uses it.
    _STATE.setdefault("seen", []).extend(v for v in operation.secrets.values() if isinstance(v, str))
    if name == "echo":
        return {"echo": params, "secrets": sorted(operation.secrets)}
    if name == "sleep":
        return _sleep(operation, params)
    if name == "progress":
        for step in range(int(params.get("steps", 3))):
            operation.progress(fraction=(step + 1) / params.get("steps", 3), message="step %d" % (step + 1))
        return {"steps": params.get("steps", 3)}
    if name == "chunks":
        size = int(params.get("size", 1000))
        pattern = (b"0123456789abcdef" * (size // 16 + 1))[:size]
        if params.get("no_chunks"):
            # Data frames and a reply that leaves out the chunk count.
            count, total = operation._channel._writer.write_data(operation.id, pattern)
            operation._channel._send({"t": frames.RES, "id": operation.id, "ok": True, "result": {},
                                      "bytes": total, "sha256": hashlib.sha256(pattern).hexdigest()})
            return {}
        if params.get("no_sha"):
            # Chunks and a reply that leaves out the checksum.
            count, total = operation._channel._writer.write_data(operation.id, pattern)
            operation._channel._send({"t": frames.RES, "id": operation.id, "ok": True, "result": {},
                                      "chunks": count, "bytes": total})
            return {}
        if params.get("bad_sha"):
            # Right byte and chunk counts, a checksum of other data.
            count, total = operation._channel._writer.write_data(operation.id, pattern)
            operation._channel._send({"t": frames.RES, "id": operation.id, "ok": True, "result": {},
                                      "chunks": count, "bytes": total,
                                      "sha256": hashlib.sha256(pattern + b"!").hexdigest()})
            return {}
        if params.get("pause_file"):
            # Stream the result, but stop after the first chunk until the host cancels.
            writer = operation._channel._writer
            real_write = writer._write
            sent = []

            def gated(ftype, body):
                real_write(ftype, body)
                if ftype == frames.FRAME_DATA and not sent:
                    sent.append(1)
                    with open(params["pause_file"], "w") as handle:
                        handle.write("1")
                    operation.cancelled.wait(20)

            writer._write = gated
            return worker_channel.OperationResult({"size": size}, pattern)
        if params.get("overshoot"):
            # A worker that sends more than the request allows.
            operation._channel._writer.write_data(operation.id, pattern)
            return {"size": size}
        return worker_channel.OperationResult({"size": size}, pattern)
    if name == "call":
        try:
            if "as_request" in params:
                # Claim to be another request.
                ack = operation._channel.call(params["name"], params.get("params") or {},
                                              request_id=params["as_request"])
            else:
                ack = operation.call(params["name"], params.get("params") or {})
            return {"ack": ack}
        except worker_channel.HostCallFailed as error:
            raise worker_channel.OperationError(error.code, error.message, retryable=error.retryable)
    if name == "translate":
        try:
            return _translate(operation, params)
        except worker_channel.HostCallFailed as error:
            raise worker_channel.OperationError(error.code, error.message, retryable=error.retryable)
    if name == "spam":
        for index in range(int(params.get("lines", 200))):
            print("stdout noise %d" % index)
        print("stderr noise", file=sys.stderr)
        os.write(1, b"raw fd one noise\n")
        return {"spammed": True}
    if name == "oversize_frame":
        stream = operation._channel._writer._stream
        stream.write(frames.HEADER.pack(frames.FRAME_CONTROL, 0x7FFFFFFF))
        stream.flush()
        time.sleep(30)
        return {}
    if name == "bad_frame":
        stream = operation._channel._writer._stream
        body = b"{not json"
        stream.write(frames.HEADER.pack(frames.FRAME_CONTROL, len(body)) + body)
        stream.flush()
        time.sleep(30)
        return {}
    if name == "fork":
        # With ``inherit`` the child keeps the worker's stdout and stderr, which are
        # the host's pipes: only its death lets the host's stderr reader finish.
        quiet = {} if params.get("inherit") else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=bool(params.get("detached")),
            stdin=subprocess.DEVNULL, **quiet,
        )
        return {"child": child.pid}
    if name == "leak":
        if params.get("after"):
            time.sleep(float(params["after"]))
        for value in operation.secrets.values():
            print("x" * int(params.get("pad", 0)) + "leaked %s" % value, file=sys.stderr, flush=True)
        return {"leaked": len(operation.secrets)}
    if name == "leak_error":
        # A plugin exception that carries a secret, with a long message so the
        # traceback is cut at the worker.
        value = next(iter(operation.secrets.values()))
        if "part" in params:
            value = value.splitlines()[int(params["part"])]
        raise ValueError(value + "B" * int(params.get("tail", 10)))
    if name == "leak_seen_error":
        raise ValueError("P" * int(params.get("pad", 0)) + _STATE["seen"][0] + "B" * int(params.get("tail", 10)))
    if name == "parallel_calls":
        # Calls for one request, sent from separate threads without waiting on each other.
        results = [None] * len(params["names"])

        def run_call(index, call_name):
            results[index] = operation.call(call_name, {})

        # Each call is started only once the one before it is on the wire, so the
        # host sees them in the order given without any sleeping.
        written = threading.Condition()
        sent = [0]
        channel = operation._channel
        real_send = channel._send

        def counting_send(message):
            real_send(message)
            if message.get("t") == frames.CALL:
                with written:
                    sent[0] += 1
                    written.notify_all()

        channel._send = counting_send
        threads = []
        for index, call_name in enumerate(params["names"]):
            thread = threading.Thread(target=run_call, args=(index, call_name))
            thread.start()
            threads.append(thread)
            with written:
                written.wait_for(lambda: sent[0] > index, timeout=10)
        for thread in threads:
            thread.join()
        return {"results": results}
    if name == "bad_send":
        # A plugin that hands the channel arguments its schema does not allow.
        kind = params["kind"]
        if kind == "progress":
            operation.progress(fraction="half")
        if kind == "progress_data":
            operation.progress(data=[1, 2])
        if kind == "event":
            operation.sandbox_blocked(5)
        if kind == "call_params":
            operation.call("lookup", [1])
        if kind == "call_name":
            operation.call(7, {})
        return {"sent": kind}
    if name == "unencodable_result":
        return _unencodable(params["kind"])
    if name == "list_result":
        return [1, 2, 3]
    if name == "nan_result":
        return {"value": float("nan")}
    if name == "bad_str":
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("no text for this one")

        raise Unprintable()
    if name == "raw_error":
        # An error reply that skips the worker's own scrubbing.
        value = next(iter(operation.secrets.values()))
        operation._channel._send({"t": frames.RES, "id": operation.id, "ok": False,
                                  "error": frames.error_object(params.get("code", "plugin_error"), "bad key " + value,
                                                               traceback="trace " + value)})
        return {}
    if name == "progress_secret":
        value = next(iter(operation.secrets.values()))
        operation.progress(message="using " + value, data={"nested": ["k=" + value]})
        return {}
    if name == "blocked":
        operation.sandbox_blocked(params.get("target", "203.0.113.9:25"), errno=13)
        raise worker_channel.OperationError("sandbox_blocked", "connect refused by the sandbox")
    if name == "alloc":
        held = bytearray(int(params.get("mb", 64)) * 1024 * 1024)
        for offset in range(0, len(held), 4096):
            held[offset] = 1
        deadline = time.monotonic() + float(params.get("seconds", 5.0))
        while time.monotonic() < deadline:
            if operation.cancelled.wait(0.02):
                raise worker_channel.OperationCancelled()
        return {"held": len(held)}
    if name == "write_deleted":
        # Creates ``path``, unlinks it and keeps writing through the open descriptor, so
        # the bytes are on disk with no directory entry to find. With ``child`` a
        # descendant does it instead; the worker only waits.
        path, megabytes = params["path"], int(params.get("mb", 4))
        code = (
            "import os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_WRONLY, 0o600)\n"
            "os.unlink(sys.argv[1])\n"
            "for _ in range(int(sys.argv[2])):\n"
            "    os.write(fd, b'x' * (1024 * 1024))\n"
            "os.fsync(fd)\n"
            "print('written', flush=True)\n"
            "time.sleep(60)\n"
        )
        if params.get("child"):
            child = subprocess.Popen([sys.executable, "-c", code, path, str(megabytes)],
                                     stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            child.stdout.readline()
        else:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.unlink(path)
            for _ in range(megabytes):
                os.write(fd, b"x" * (1024 * 1024))
            os.fsync(fd)
        operation.progress(message="written")
        deadline = time.monotonic() + float(params.get("seconds", 30.0))
        while time.monotonic() < deadline:
            if operation.cancelled.wait(0.02):
                raise worker_channel.OperationCancelled()
        return {"written": megabytes}
    if name == "exit":
        os._exit(int(params.get("code", 1)))
    if name == "raise":
        raise ValueError(params.get("message", "boom"))
    raise worker_channel.OperationError("unknown_op", "no operation named %s" % name)


def main():
    config = json.loads(os.environ.get(ENV_NAME) or "{}")
    _STATE["config"] = config
    if config.get("no_hello"):
        # Behave like a worker that does not know this protocol: read forever, say nothing.
        for _ in sys.stdin.buffer:
            pass
        return 0
    if config.get("hello_delay"):
        time.sleep(float(config["hello_delay"]))
    channel = worker_channel.WorkerChannel(
        _handle,
        kinds=config.get("kinds") or [{"kind": "translator", "major": 1, "minor": 0}],
        protocols=config.get("protocols"),
        max_in_flight=int(config.get("max_in_flight", frames.DEFAULT_MAX_IN_FLIGHT)),
        worker={"name": "fake-worker", "version": "0"},
    )
    if config.get("silent"):
        channel._heartbeat_loop = lambda: None
    if config.get("stubborn"):
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        channel._drain = threading.Event().wait
    return channel.run()


if __name__ == "__main__":
    sys.exit(main())
