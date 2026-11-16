"""The translator wire as the host writes and reads it, and how worker and pool errors become failure classes."""
import json

import pytest

from provider_hub.runtime import errors as session_errors
from provider_hub.runtime import frames
from provider_hub.runtime.pool import PoolCodes, PoolUnavailable, WorkerKilled
from subtitles.tools.translate.runtime import failure as fail
from subtitles.tools.translate.runtime import wire
from subtitles.tools.translate.runtime.failure import AttemptFailure, classify
from subtitles.tools.translate.runtime.request import normalize_language


def _answer(**overrides):
    value = {"lines": [{"position": 0, "text": "a"}, {"position": 1, "text": "b"}], "status": "complete",
             "missing": [], "model": "m", "tokens": 7, "cost": 0.5, "batches": [{"positions": [0, 1], "model": "m"}]}
    value.update(overrides)
    return value


def test_a_request_carries_the_job_both_languages_the_lines_and_the_budget():
    request = wire.build_request(
        job_id="9", source=normalize_language("en"), target=normalize_language("zt"), lines=["x", "y"],
        media={"title": "T"}, engine_settings={"k": 1}, connection={"id": "3"}, checkpoints=[{"a": 1}], budget=0.25)
    assert request["job_id"] == "9" and request["budget"] == 0.25
    assert request["target"]["code"] == "zh-TW"
    assert request["lines"] == [{"position": 0, "text": "x"}, {"position": 1, "text": "y"}]
    assert request["config"] == {"settings": {"k": 1}, "connection": {"id": "3"}}
    assert request["checkpoints"] == [{"a": 1}]
    assert "secrets" not in request


def test_an_engine_without_connections_sends_no_connection():
    request = wire.build_request(job_id="1", source=normalize_language("en"), target=normalize_language("fr"),
                                 lines=[], media=None, engine_settings={}, connection=None, checkpoints=[],
                                 budget=None)
    assert "connection" not in request["config"] and request["budget"] is None


def test_a_result_reads_into_lines_status_models_and_totals():
    result = wire.read_result(_answer(missing=[1], status="partial"), b"", 2)
    assert result.lines == {0: "a", 1: "b"} and result.partial and result.missing == [1]
    assert (result.model, result.tokens, result.cost, result.batch_models) == ("m", 7, 0.5, ["m"])


def test_a_result_that_travelled_as_data_or_wrapped_is_unpacked():
    in_data = wire.read_result({wire.IN_DATA_KEY: True}, json.dumps(_answer()).encode(), 2)
    assert in_data.lines[1] == "b"
    wrapped = wire.read_result({wire.WRAPPED_KEY: _answer()}, b"", 2)
    assert wrapped.lines[0] == "a"


@pytest.mark.parametrize("answer", [
    [],  # not an object
    _answer(status="done"),
    _answer(lines="no"),
    _answer(lines=[{"position": 5, "text": "x"}]),  # outside the request
    _answer(lines=[{"position": 0, "text": "x"}, {"position": 0, "text": "y"}]),  # twice
    _answer(lines=[{"position": 0, "text": 3}]),
    _answer(lines=["x"]),
    _answer(missing=["x"]),
    _answer(missing=[9]),
])
def test_a_malformed_answer_is_an_internal_failure_naming_the_answer(answer):
    with pytest.raises(AttemptFailure) as caught:
        wire.read_result(answer, b"", 2)
    assert caught.value.error_class == fail.INTERNAL and caught.value.reason == "invalid_result"


def test_data_that_does_not_decode_is_refused():
    with pytest.raises(AttemptFailure):
        wire.read_result({wire.IN_DATA_KEY: True}, b"\xff", 2)


def _failed(code, **kwargs):
    return session_errors.RequestFailed(kwargs.pop("message", "words"), code=code, **kwargs)


@pytest.mark.parametrize("error, expected", [
    (_failed("auth_required"), fail.AUTH_REQUIRED),
    (_failed("rate_limited", retry_after=30), fail.RATE_LIMITED),
    (_failed("quota_exhausted"), fail.QUOTA_EXHAUSTED),
    (_failed("dependency_unavailable"), fail.DEPENDENCY_UNAVAILABLE),
    (_failed("invalid_config"), fail.INVALID_CONFIG),
    (_failed("declined"), fail.DECLINED),
    (_failed("internal"), fail.INTERNAL),
    (_failed("something_odd"), fail.INTERNAL),
    (_failed(frames.Codes.SANDBOX_BLOCKED), fail.SANDBOX_BLOCKED),
    (session_errors.WorkerExited("gone"), fail.WORKER_FAILED),
    (session_errors.HeartbeatTimeout("silent"), fail.WORKER_FAILED),
    (session_errors.ProtocolViolation("bad"), fail.WORKER_FAILED),
    (session_errors.RequestTimeout("late"), fail.WORKER_FAILED),
    (session_errors.SessionBusy("busy"), fail.WORKER_UNAVAILABLE),
    (session_errors.ResultTooLarge("big"), fail.INTERNAL),
    (WorkerKilled("memory", code=PoolCodes.MEMORY_LIMIT), fail.WORKER_FAILED),
    (PoolUnavailable("full", code=PoolCodes.POOL_FULL), fail.WORKER_UNAVAILABLE),
    (PoolUnavailable("backing off", code=PoolCodes.BACKOFF), fail.DEPENDENCY_UNAVAILABLE),
    (PoolUnavailable("open", code=PoolCodes.CIRCUIT_OPEN), fail.DEPENDENCY_UNAVAILABLE),
    (PoolUnavailable("no", code=PoolCodes.SANDBOX_BLOCKED), fail.SANDBOX_BLOCKED),
    (RuntimeError("bug"), fail.INTERNAL),
])
def test_worker_and_pool_errors_become_one_failure_class(error, expected):
    assert classify(error).error_class == expected


def test_a_cancel_after_a_refused_hold_is_a_budget_stop_and_not_a_stop():
    cancelled = _failed("cancelled", message="the cost budget does not cover this call (job_budget)")
    assert classify(cancelled, budget_refusal="job_budget").error_class == fail.BUDGET_EXHAUSTED
    assert classify(cancelled, budget_refusal="month_spent").error_class == fail.MONTH_CAP
    assert classify(session_errors.RequestCancelled("c"), budget_refusal="job_budget").error_class == \
        fail.BUDGET_EXHAUSTED
    assert classify(session_errors.RequestCancelled("c")).error_class == fail.CANCELLED


def test_a_plugin_that_stopped_itself_for_its_budget_is_a_budget_failure():
    assert classify(_failed("cancelled", message="budget_exhausted")).error_class == fail.BUDGET_EXHAUSTED


def test_the_retry_reported_is_kept_only_when_it_is_a_sensible_number():
    assert classify(_failed("rate_limited", retry_after=30)).retry_after == 30.0
    assert classify(_failed("rate_limited", retry_after=-5)).retry_after is None
    assert classify(_failed("rate_limited", retry_after=float("inf"))).retry_after is None


def test_only_the_four_shared_causes_fail_over_and_the_sandbox_is_not_one_of_them():
    assert fail.FAILOVER_CLASSES == {"rate_limited", "quota_exhausted", "auth_required", "dependency_unavailable"}
    assert AttemptFailure(fail.SANDBOX_BLOCKED).fails_over is False
    assert AttemptFailure(fail.INVALID_CONFIG).retries_later is False
    assert AttemptFailure(fail.RATE_LIMITED).retries_later is True
    assert fail.SANDBOX_BLOCKED in fail.MESSAGES and "network rule" in fail.MESSAGES[fail.SANDBOX_BLOCKED]
