"""The conformance suite a plugin runs against its own class.

``run_conformance(MyPlugin, kind_api="bazarr.translator.v1")`` serves the
class over the real runner and protocol and checks what the host relies on.
Without samples it checks the structure: the class derives from the kind's
base, starts without arguments, completes the handshake, describes itself,
answers unknown operations and bad arguments with an error instead of a crash,
and heartbeats. Give it samples to also run real requests and the cancel check::

    run_conformance(
        MyPlugin,
        kind_api="bazarr.translator.v1",
        samples={
            "validate": {"args": {"config": {...}}},
            "translate": {"args": {"request": {...}}, "secrets": {...}},
        },
    )

A sample may carry ``expect_error`` (an error code) when the request is meant
to fail. A ``translate`` sample also drives the cancel check.
"""

from __future__ import annotations

import time

from typing import Any, Callable, Mapping

from .._protocol import frames
from ..errors import ERROR_CODES
from ..kinds import get_kind
from .host import HostFailure, LoopbackHost, ProcessHost, Response

CANCEL_GRACE = 5.0
_SPENDING_CALLS = ("reserve", "checkpoint")


class ConformanceError(AssertionError):
    """One or more checks failed. ``failures`` lists them."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.failures = failures


def run_conformance(
    plugin_class: type,
    *,
    kind_api: str,
    samples: Mapping[str, Mapping[str, Any]] | None = None,
    entry: str | None = None,
    paths: tuple[str, ...] = (),
    timeout: float = 10.0,
) -> list[str]:
    """Run every check. Returns the names of the checks that passed; raises :class:`ConformanceError`.

    ``entry`` (``module:Class``) and ``paths`` additionally start a real worker
    process and repeat the handshake and ``describe`` over it.
    """
    samples = samples or {}
    passed: list[str] = []
    failures: list[str] = []

    def check(name: str, func: Callable[[], str | None]) -> None:
        try:
            note = func()
        except (AssertionError, HostFailure) as error:
            failures.append(f"{name}: {error}")
        except Exception as error:  # a crash in the plugin is a failed check, not a crash of the suite
            failures.append(f"{name}: {type(error).__name__}: {error}")
        else:
            passed.append(name if note is None else f"{name} ({note})")

    spec = get_kind(kind_api)
    if spec is None:
        raise ConformanceError([f"this SDK does not serve {kind_api}"])

    def structure() -> None:
        assert isinstance(plugin_class, type) and issubclass(plugin_class, spec.base_class), (
            f"{plugin_class!r} does not derive from {spec.base_class.__name__}"
        )
        assert kind_api in plugin_class.kind_apis, f"kind_apis {plugin_class.kind_apis!r} lacks {kind_api}"
        limit = plugin_class.max_in_flight
        assert isinstance(limit, int) and not isinstance(limit, bool) and limit > 0, "max_in_flight must be a positive int"

    check("structure", structure)
    if failures:
        raise ConformanceError(failures)

    host = LoopbackHost(plugin_class, heartbeat_interval=0.05, wait_grace=1.0)
    try:

        def handshake() -> None:
            welcome = host.wait_ready(timeout)
            assert spec.offer() in host.hello["kinds"], "hello does not offer the kind"
            assert welcome["kind"] == spec.offer(), f"the host settled on {welcome['kind']!r}"
            assert host.hello["protocols"] == [{"major": frames.PROTOCOL_MAJOR, "minor": frames.PROTOCOL_MINOR}]
            assert host.hello["max_in_flight"] == plugin_class.max_in_flight, "hello max_in_flight differs from the class"

        check("handshake", handshake)
        if failures:
            raise ConformanceError(failures)

        def describe() -> None:
            response = host.request("describe", wait=timeout)
            _need_ok(response)
            assert isinstance(response.value, dict), "describe must return an object"

        check("describe", describe)

        def unknown_operation() -> None:
            response = host.request("no_such_operation", wait=timeout)
            assert response.code == frames.Codes.UNKNOWN_OP, "an unknown operation must be an unknown_op error"
            _need_ok(host.request("describe", wait=timeout))

        check("unknown_operation", unknown_operation)

        def bad_arguments() -> None:
            method = next((m for m in spec.methods.values() if m.params), None)
            if method is None:
                return
            response = host.request(method.name, {"unexpected": 1}, wait=timeout)
            assert response.code == frames.Codes.INVALID_REQUEST, "wrong arguments must be an invalid_request error"

        check("bad_arguments", bad_arguments)

        def heartbeat() -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not host.heartbeats:
                time.sleep(0.02)
            assert host.heartbeats, "no heartbeat within 2 seconds of a 50 ms interval"

        check("heartbeat", heartbeat)

        for name, sample in samples.items():
            if name not in spec.methods:
                failures.append(f"samples: {name} is not an operation of {kind_api}")
                continue

            def sampled(name: str = name, sample: Mapping[str, Any] = sample) -> None:
                params = dict(sample.get("args") or {})
                if sample.get("budget") is not None:
                    params["budget"] = sample["budget"]
                response = host.request(name, params, sample.get("secrets"), wait=timeout)
                expected = sample.get("expect_error")
                if expected is not None:
                    assert expected in ERROR_CODES, f"expect_error {expected!r} is not an error code"
                    assert response.code == expected, f"expected {expected}, got {response.code or 'a result'}"
                else:
                    _need_ok(response)

            check(f"request {name}", sampled)

        if "translate" in samples and "expect_error" not in samples["translate"]:
            check("cancel", lambda: _cancel_check(host, samples["translate"], timeout))
    finally:
        host.close()

    if entry:

        def process() -> None:
            with ProcessHost(entry, paths=paths) as process_host:
                welcome = process_host.wait_ready(timeout)
                assert welcome["kind"] == spec.offer()
                _need_ok(process_host.request("describe", wait=timeout))

        check("process", process)

    if failures:
        raise ConformanceError(failures)
    return passed


def _need_ok(response: Response) -> None:
    if response.error is not None:
        tail = response.error.get("traceback") or ""
        raise AssertionError(f"the request failed: {response.code}: {response.error.get('message')}{' | ' + tail[-300:] if tail else ''}")


def _cancel_check(host: Any, sample: Mapping[str, Any], timeout: float) -> str | None:
    params = dict(sample.get("args") or {})
    if sample.get("budget") is not None:
        params["budget"] = sample["budget"]
    seen_calls = len(host.calls)
    response = host.submit("translate", params, sample.get("secrets"))
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and not response.progress and len(host.calls) == seen_calls and not response.done.is_set():
        time.sleep(0.01)
    if response.done.is_set():
        return "finished before it could be cancelled"
    host.cancel(response)
    response.wait(CANCEL_GRACE)
    if response.ok:
        return "finished before the cancel arrived"
    assert response.code == frames.Codes.CANCELLED, f"a cancelled request answered {response.code}, not cancelled"
    time.sleep(0.3)
    late = [name for at, name, _ in host.calls if name in _SPENDING_CALLS and at > (response.cancel_sent_at or 0) + 0.05]
    assert not late, "the plugin started new spending or a checkpoint after the cancel"
    return None
