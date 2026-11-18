"""How a pool entry is worded for a plugin card."""
import pytest

from subtitles.tools.translate.runtime import workers


@pytest.mark.parametrize("worker, phase", [
    ({"state": "starting"}, "starting"),
    ({"state": "ready", "in_flight": 0}, "idle"),
    ({"state": "ready", "in_flight": 2}, "healthy"),
    ({"state": "busy"}, "healthy"),
    ({"state": "backoff", "retry_after": 4.0}, "backing_off"),
    ({"state": "broken"}, "broken"),
    ({"state": "draining"}, "stopping"),
    ({"state": "stopping"}, "stopping"),
    ({"state": "stopped"}, "stopped"),
    ({}, "stopped"),
])
def test_a_pool_state_is_worded_as_one_phase(worker, phase):
    assert workers.runtime_summary(worker)["phase"] == phase


def test_the_summary_leaves_out_the_stderr_tail_and_the_pid():
    summary = workers.runtime_summary({"state": "ready", "pid": 99, "recent_stderr": ["line"], "starts": 1})
    assert set(summary) == {"phase", "in_flight", "retry_after", "last_error", "starts", "crashes", "memory_bytes"}


def test_a_plugin_that_never_had_a_worker_has_no_runtime_and_no_log():
    class Pool:
        def status(self):
            return {"workers": {}}

    port = workers.PoolWorkers(Pool(), lambda plugin_id: None)
    assert port.runtime("ghost") is None and port.stderr_tail("ghost") == []


def test_a_pool_that_cannot_answer_reads_as_no_runtime():
    class Pool:
        def status(self):
            raise RuntimeError("closed")

    assert workers.PoolWorkers(Pool(), lambda plugin_id: None).runtime("any") is None


def test_nothing_is_reported_while_no_port_is_bound():
    workers.unbind()
    assert workers.runtime_of("any") is None and workers.stderr_tail_of("any") == []


def test_a_start_failure_is_scrubbed_before_it_leaves_the_host():
    secret = "sk-" + "a1b2c3d4" * 4  # pragma: allowlist secret
    summary = workers.runtime_summary({"state": "backoff", "last_error": f"handshake failed, Bearer {secret}"})
    assert secret not in summary["last_error"] and summary["last_error"].startswith("handshake failed")
    assert workers.runtime_summary({"state": "ready"})["last_error"] is None
