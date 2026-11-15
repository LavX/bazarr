# coding=utf-8
"""A scriptable translator worker that speaks the translator kind as the plugin SDK does, for tests.

Run it as a script (``python -B fake_translator.py``). It needs only the standard library and the sibling
``frames.py`` and ``worker_channel.py``. It serves one operation, ``translate``, whose params are
``{"request": {...}}`` (the request shape of ``bazarr.translator.v1``: job_id, source, target, lines,
media, config, checkpoints, budget), with the request's secrets in the frame's own ``secrets`` object.

Host calls are made in the SDK's shapes: ``reserve`` with ``amount`` (dollars) and ``model``, ``usage`` with
``tokens``, ``cost``, ``model`` and the ``reservations`` it settles, ``checkpoint`` with ``data``.

Behavior is scripted per connection by ``BAZARR_FAKE_TRANSLATOR`` (a JSON object): ``script`` maps a connection
id (or ``"*"``) to a list of outcome dicts applied to the 1st, 2nd, ... translate request for that
connection, the last one repeating. An outcome may set:

``batch``          lines per batch (default 2)
``delay``          seconds per batch
``reserve``        dollars held before each batch (default 0)
``cost``           dollars reported for each batch (default 0)
``tokens``         tokens reported for each batch (default 10)
``model``          the model that served the batch (default: the connection's)
``fail``           an error ``{"code", "message", "retry_after"}`` raised after ``fail_at_batch`` batches
``fail_at_batch``  0 (before any work, the default) or the number of batches to finish first
``blocked``        report a refused connect, then fail as sandbox_blocked
``die_after``      ``reserve``, ``batch:N`` or ``work``: the process exits hard there
``wait_cancel``    block until the request is cancelled (``ignore_cancel`` keeps waiting after one)
``partial``        positions the answer leaves out (status partial)
``corrupt``        ``bad_tag`` or ``control`` damages the first line, ``drop`` leaves one line out
``noreport``       settle nothing (a worker that forgets its usage call)

``BAZARR_FAKE_TRANSLATOR_STATE`` names a file that keeps the attempt counts across worker processes, so that a
script's second outcome applies to the request after a worker was killed.

``BAZARR_FAKE_TRANSLATOR_REPORT`` names a file that receives one JSON line per request: the request without its
secrets, the secret names and values, and the connection id, so a test can check what was sent.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUNTIME = os.path.dirname(_HERE)
if _RUNTIME not in sys.path:
    sys.path.insert(0, _RUNTIME)
import frames  # noqa: E402
import worker_channel  # noqa: E402

ENV_NAME = "BAZARR_FAKE_TRANSLATOR"
REPORT_ENV = "BAZARR_FAKE_TRANSLATOR_REPORT"
# A file that keeps each connection's attempt count across worker processes, so a script survives a restart.
STATE_ENV = "BAZARR_FAKE_TRANSLATOR_STATE"

_STATE = {"attempts": {}, "config": {}}
_REPORT_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


def fake_translator_command():
    return [sys.executable, "-B", os.path.abspath(__file__)]


def fake_translator_env(script=None, report=None, state=None):
    env = {ENV_NAME: json.dumps({"script": script or {}})}
    if report:
        env[REPORT_ENV] = str(report)
    if state:
        env[STATE_ENV] = str(state)
    return env


def _outcome(connection_id):
    script = _STATE["config"].get("script") or {}
    steps = script.get(connection_id) or script.get("*") or [{}]
    path = os.environ.get(STATE_ENV)
    with _STATE_LOCK:
        counts = _STATE["attempts"]
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                counts = json.load(handle)
        count = counts.get(connection_id, 0)
        counts[connection_id] = count + 1
        _STATE["attempts"] = counts
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(counts, handle)
    return dict(steps[min(count, len(steps) - 1)]), count


def _report(request, secrets, connection_id):
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    body = {key: value for key, value in request.items() if key != "secrets"}
    line = json.dumps({"request": body, "secrets": dict(secrets), "connection": connection_id}, sort_keys=True)
    with _REPORT_LOCK, open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _fail(failure):
    raise worker_channel.OperationError(
        failure.get("code", "internal"), failure.get("message", "failed"),
        retryable=failure.get("code") in ("rate_limited", "dependency_unavailable"),
        retry_after=failure.get("retry_after"))


def _call(operation, name, params, *, cancellable=True):
    try:
        return operation.call(name, params, cancellable=cancellable)
    except worker_channel.HostCallFailed as error:
        # The SDK turns a refused host call into a plugin error of the same code.
        raise worker_channel.OperationError(error.code, error.message) from None


def _done_before(checkpoints):
    """The lines earlier attempts finished: position -> text, from the checkpoints the host handed back."""
    done = {}
    for item in checkpoints or []:
        for position, text in (item.get("lines") or {}).items():
            done[int(position)] = text
    return done


def _translate(operation):
    request = operation.params["request"]
    connection = (request.get("config") or {}).get("connection") or {}
    connection_id = str(connection.get("id", "-"))
    outcome, attempt = _outcome(connection_id)
    _report(request, operation.secrets, connection_id)
    model = outcome.get("model") or (connection.get("settings") or {}).get("model") or "fake-model"
    target = (request.get("target") or {}).get("code", "xx")
    lines = request.get("lines") or []
    batch = max(1, int(outcome.get("batch", 2)))
    done = _done_before(request.get("checkpoints"))
    fail_at = int(outcome.get("fail_at_batch", 0))
    reserve = float(outcome.get("reserve", 0.0))
    cost = float(outcome.get("cost", 0.0))
    tokens = int(outcome.get("tokens", 10))
    texts = {}
    holds = []

    if outcome.get("blocked"):
        operation.sandbox_blocked(outcome.get("target", "203.0.113.9:1234"), errno=13)
        raise worker_channel.OperationError("sandbox_blocked", "connect refused by the sandbox")
    if outcome.get("wait_cancel"):
        while True:
            if operation.cancelled.wait(0.02):
                if not outcome.get("ignore_cancel"):
                    raise worker_channel.OperationCancelled()
                time.sleep(0.05)
    if fail_at == 0 and outcome.get("fail"):
        _fail(outcome["fail"])

    batches = 0
    pending = [line for line in lines if line["position"] not in done]
    for start in range(0, len(pending), batch):
        operation.check_cancelled()
        chunk = pending[start:start + batch]
        if outcome.get("delay"):
            time.sleep(float(outcome["delay"]))
        hold = _call(operation, "reserve", {"amount": reserve, "model": model}).get("hold")
        if outcome.get("die_after") == "reserve":
            os._exit(9)
        holds.append(hold)
        produced = {line["position"]: "[%s] %s" % (target, line["text"]) for line in chunk}
        texts.update(produced)
        _call(operation, "checkpoint", {"data": {
            "model": model, "lines": {str(position): text for position, text in produced.items()}}},
            cancellable=False)
        if not outcome.get("noreport"):
            _call(operation, "usage", {"tokens": tokens, "cost": cost, "model": model, "reservations": [hold]},
                  cancellable=False)
        batches += 1
        operation.progress(fraction=min(1.0, (len(done) + len(texts)) / max(1, len(lines))),
                           message="translated %d" % (len(done) + len(texts)),
                           data={"done": len(done) + len(texts), "total": len(lines)})
        if outcome.get("die_after") == "batch:%d" % batches:
            os._exit(9)
        if outcome.get("fail") and batches == fail_at:
            _fail(outcome["fail"])
    if outcome.get("die_after") == "work":
        os._exit(9)

    result_lines = {}
    for line in lines:
        position = line["position"]
        text = done.get(position) if position in done else texts.get(position)
        if position in done:
            text = "ck:" + text
        result_lines[position] = text
    missing = [int(position) for position in outcome.get("partial", [])]
    for position in missing:
        result_lines.pop(position, None)
    entries = [{"position": position, "text": text} for position, text in sorted(result_lines.items())]
    corrupt = outcome.get("corrupt")
    if corrupt == "bad_tag" and entries:
        entries[0]["text"] = entries[0]["text"] + "<script>x</script>"
    elif corrupt == "control" and entries:
        entries[0]["text"] = entries[0]["text"] + "\x01"
    elif corrupt == "drop" and entries:
        entries.pop()
    return {"lines": entries, "status": "partial" if missing else "complete", "missing": missing,
            "backend": connection.get("type", ""), "connection": connection_id, "model": model,
            "tokens": tokens * batches, "cost": cost * batches, "batches": [], "attempt": attempt}


def _handle(operation):
    if operation.op != "translate":
        raise worker_channel.OperationError("unknown_op", "no operation named %s" % operation.op)
    return _translate(operation)


def main():
    _STATE["config"] = json.loads(os.environ.get(ENV_NAME) or "{}")
    channel = worker_channel.WorkerChannel(
        _handle, kinds=[{"kind": "translator", "major": 1, "minor": 0}], max_in_flight=int(
            _STATE["config"].get("max_in_flight", frames.DEFAULT_MAX_IN_FLIGHT)),
        worker={"name": "fake-translator", "version": "0"})
    return channel.run()


if __name__ == "__main__":
    sys.exit(main())
