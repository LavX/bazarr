# coding=utf-8
"""A scriptable v2 worker for tests, standing in for a translator plugin.

Run it as a script (``python -B fake_worker.py``). It needs only the standard
library and the sibling ``frames.py`` and ``worker_channel.py``. Behavior is
set by the ``BAZARR_FAKE_WORKER`` environment variable (a JSON object, see
``CONFIG_KEYS``) and by the operation a test sends.

Operations:

``echo``           returns the params.
``sleep``          waits ``seconds``, cancellable unless ``ignore_cancel``.
``progress``       sends ``steps`` progress messages, then returns.
``chunks``         returns ``size`` bytes of data (``overshoot`` sends them
                   without honoring the request's limit).
``call``           makes the host call ``name`` with ``params`` and returns the ack.
``translate``      a fake translator: reserve, work in batches with a
                   checkpoint each, report usage, return the lines. Knobs in
                   params: ``lines``, ``target``, ``batch``, ``delay``,
                   ``fail`` (an error object to raise after the first batch),
                   ``skip_reserve``.
``spam``           prints to stdout and writes to stderr, then returns.
``oversize_frame`` writes a header that claims a huge control frame.
``bad_frame``      writes a control frame that is not valid JSON.
``fork``           starts a child that sleeps and returns its pid. It stays in
                   the worker's process group unless ``detached`` is set.
``leak``           writes the request's secret values to stderr.
``blocked``        reports a refused connect, then fails with sandbox_blocked.
``exit``           ends the process with ``code``.
``raise``          raises a plain exception.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    "no_hello",       # never say hello (a v1 runner looks like this)
    "hello_delay",    # seconds before the hello
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
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if operation.cancelled.wait(0.01) and not params.get("ignore_cancel"):
            raise worker_channel.OperationCancelled()
    return {"slept": seconds}


def _translate(operation, params):
    lines = list(params.get("lines") or [])
    target = params.get("target", "xx")
    batch = max(1, int(params.get("batch", 2)))
    delay = float(params.get("delay", 0.0))
    if not params.get("skip_reserve"):
        operation.call("reserve", {"units": len(lines), "target": target})
    out = []
    for start in range(0, len(lines), batch):
        operation.check_cancelled()
        chunk = lines[start:start + batch]
        if delay:
            time.sleep(delay)
        out.extend("[%s] %s" % (target, line) for line in chunk)
        operation.progress(fraction=len(out) / max(1, len(lines)), message="translated %d" % len(out))
        operation.call("checkpoint", {"done": len(out), "lines": out[start:start + batch]})
        failure = params.get("fail")
        if failure:
            raise worker_channel.OperationError(
                failure.get("code", "plugin_error"), failure.get("message", "failed"),
                retryable=bool(failure.get("retryable")), retry_after=failure.get("retry_after"))
    operation.call("usage", {"units": len(lines), "target": target})
    return {"lines": out, "target": target}


def _handle(operation):
    params = operation.params
    name = operation.op
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
        if params.get("overshoot"):
            # A worker that sends more than the request allows.
            operation._channel._writer.write_data(operation.id, pattern)
            return {"size": size}
        return worker_channel.OperationResult({"size": size}, pattern)
    if name == "call":
        try:
            return {"ack": operation.call(params["name"], params.get("params") or {})}
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
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=bool(params.get("detached")),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"child": child.pid}
    if name == "leak":
        for value in operation.secrets.values():
            print("leaked %s" % value, file=sys.stderr, flush=True)
        return {"leaked": len(operation.secrets)}
    if name == "blocked":
        operation.sandbox_blocked(params.get("target", "203.0.113.9:25"), errno=13)
        raise worker_channel.OperationError("sandbox_blocked", "connect refused by the sandbox")
    if name == "exit":
        os._exit(int(params.get("code", 1)))
    if name == "raise":
        raise ValueError(params.get("message", "boom"))
    raise worker_channel.OperationError("unknown_op", "no operation named %s" % name)


def main():
    config = json.loads(os.environ.get(ENV_NAME) or "{}")
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
    return channel.run()


if __name__ == "__main__":
    sys.exit(main())
