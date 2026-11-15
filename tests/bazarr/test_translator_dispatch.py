"""Dispatch: the engine choice, connection failover, decline and fallback, budget, checkpoints, history and
the error classes, against a real worker pool running a scripted fake translator over protocol v2.

Runs on SQLite, and on PostgreSQL when BAZARR_PG_TEST_URL is set.
"""
import json
import threading
import time
from datetime import datetime

import pytest

import translator_dispatch_helpers as helpers
from translator_dispatch_helpers import KEY, LINES, PLUGIN, SECOND_PLUGIN, SOURCE, TARGET, make_spec, usd
from subtitles.tools.translate.runtime import failure as fail
from subtitles.tools.translate.runtime.dispatch import (
    DispatchFailed, Declined, GateClosed, LegacyDeclined, LegacyResult)
from subtitles.tools.translate.runtime.jobs import TranslationJobs
from subtitles.tools.translate.runtime.store import CONNECTION
from subtitles.tools.translate.runtime.usage import CostLimits, UsageLedger


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path):
    yield from helpers.build_env(request.param, tmp_path)


def two_connections(env):
    instance = env.engine()
    first = env.connection(instance, model="model-a")
    second = env.connection(instance, model="model-b")
    return instance, first, second


# ------------------------------------------------------------------------------------------------ the happy path


def test_a_job_runs_on_its_connection_and_is_recorded_everywhere(env):
    instance = env.engine()
    connection = env.connection(instance, price_input_per_million=1.0, price_output_per_million=2.0)
    env.scripts[PLUGIN] = {"*": [{"reserve": 0.001, "cost": 0.0004, "tokens": 40}]}
    outcome = env.dispatcher().run(make_spec())

    assert outcome.kind == "lines" and outcome.partial_error is None
    assert outcome.lines == [f"[fr] {line}" for line in LINES]
    assert outcome.plugin_id == PLUGIN and outcome.engine_name == "AI Subtitle Translator"
    job = env.last_job()
    assert job.state == "completed" and job.engine_instance_id == instance.id
    assert job.release_digest == "digest-a" and job.settings_revision == instance.revision
    assert env.checkpoints() == [], "a finished job keeps no checkpoint"
    row = env.history(job)
    assert (row["status"], row["plugin_id"], row["plugin_version"]) == ("completed", PLUGIN, "2.2.0")
    assert row["media_title"] == "Film" and row["target_language"] == "fr"
    assert [item["connection_id"] for item in row["connections"]] == [connection.id]
    taken = row["connections"][0]
    assert taken["batches"] == 3 and taken["models"] == ["model-a"] and taken["attempts"] == 1
    assert taken["input_tokens"] == 120 and row["cost_micros"] == 3 * usd(0.0004)
    ledger = UsageLedger(env.session, clock=env.clock)
    assert ledger.job_hold(str(job.id)) == 0, "the job's budget is released when it ends"


def test_what_the_worker_is_sent_and_how_secrets_travel(env):
    instance = env.engine(settings={"credit_line": "hi"})
    connection = env.connection(instance, backend="openai_compatible", model="m1", price_input_per_million=3.0)
    env.store.set_secret((CONNECTION, connection.id), "api_key", KEY,
                         destinations=frozenset({"http://lan.example.test:1234/v1"}))
    env.dispatcher().run(make_spec(target="zt", source_language="en"))

    request = env.requests()[0]
    sent = request["request"]
    assert sent["target"] == {"code": "zh-TW", "name": "Chinese (Traditional)", "script": "Hant", "rtl": False}
    assert sent["source"]["code"] == "en"
    assert [line["text"] for line in sent["lines"]] == LINES
    assert sent["media"] == {"type": "movie", "title": "Film", "year": 2020}
    assert sent["config"]["settings"] == {"credit_line": "hi"}
    wire = sent["config"]["connection"]
    assert wire["id"] == str(connection.id) and wire["type"] == "openai_compatible"
    assert wire["settings"]["model"] == "m1" and wire["settings"]["price_input_per_million"] == 3.0
    assert sent["checkpoints"] == [] and sent["budget"] is None
    assert KEY not in json.dumps(sent), "a secret is not part of the request"
    assert request["secrets"] == {"api_key": KEY}


def test_a_secret_never_follows_a_new_destination(env):
    instance = env.engine()
    connection = env.connection(instance, backend="openai_compatible")
    other = env.connection(instance, backend="openai_compatible", model="model-b")
    env.store.set_secret((CONNECTION, connection.id), "api_key", KEY,
                         destinations=frozenset({"http://old.example.test/v1"}))
    # The address the connection uses now is not the one the key was entered for: the key is not sent, and
    # the next connection serves the job.
    env.dispatcher().run(make_spec())
    assert env.served() == [str(other.id)]
    assert KEY not in json.dumps(env.requests())
    row = env.history(env.last_job())
    assert {item["connection_id"]: item["failed_over"] for item in row["connections"]}[connection.id] is True


def test_languages_reach_the_engine_as_tags_with_a_right_to_left_flag_and_marks_are_added(env):
    env.ai_engine()
    outcome = env.dispatcher().run(make_spec(target="he"))
    sent = env.requests()[0]["request"]
    assert sent["target"] == {"code": "he", "name": "Hebrew", "script": "Hebr", "rtl": True}
    assert outcome.lines[0] == "‫[he] One‬"


# --------------------------------------------------------------------------------------- order and failover


def test_connections_are_tried_in_list_order_and_unusable_ones_are_skipped(env):
    instance, first, second = two_connections(env)
    env.store.update_connection(first.id, schema=helpers_schema(), enabled=False)
    env.dispatcher().run(make_spec())
    assert env.served() == [str(second.id)] * 1


def helpers_schema():
    from subtitles.tools.translate.runtime.destinations import DestinationSchema

    return DestinationSchema()


@pytest.mark.parametrize("code", ["rate_limited", "quota_exhausted", "auth_required", "dependency_unavailable"])
def test_these_failures_move_to_the_next_connection_and_it_continues_from_the_checkpoints(env, code):
    instance, first, second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"fail": {"code": code, "message": "no"}, "fail_at_batch": 1}]}
    outcome = env.dispatcher().run(make_spec())

    assert env.served() == [str(first.id), str(second.id)]
    # The first connection translated one batch before it failed; the second took it from the checkpoint.
    assert outcome.lines == ["ck:[fr] One", "ck:[fr] Two", "[fr] Three", "[fr] Four", "[fr] Five"]
    job = env.last_job()
    row = env.history(job)
    assert row["status"] == "completed" and row["failovers"] == 1
    by_connection = {item["connection_id"]: item for item in row["connections"]}
    assert by_connection[first.id]["failed_over"] is True and by_connection[first.id]["last_error_class"] == code
    assert by_connection[first.id]["batches"] == 1 and by_connection[second.id]["batches"] == 2
    assert by_connection[second.id]["failed_over"] is False


def test_a_connection_that_failed_over_before_any_batch_is_on_record(env):
    instance, first, second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"fail": {"code": "auth_required", "message": "no"}}]}
    env.dispatcher().run(make_spec())
    by_connection = {item["connection_id"]: item for item in env.history(env.last_job())["connections"]}
    assert by_connection[first.id]["batches"] == 0 and by_connection[first.id]["attempts"] == 1
    assert by_connection[first.id]["failed_over"] is True


@pytest.mark.parametrize("code", ["invalid_config", "internal"])
def test_other_failures_never_move_to_the_next_connection(env, code):
    instance, first, second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"fail": {"code": code, "message": "bad"}}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == code
    assert env.served() == [str(first.id)]


def test_a_content_error_ends_the_job_for_good_and_drops_its_checkpoints(env):
    instance, first, _second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"fail": {"code": "invalid_config", "message": "bad"},
                                            "fail_at_batch": 1}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.retry_later is False
    job = env.last_job()
    assert (job.state, job.error_class) == ("failed", "invalid_config")
    assert env.checkpoints() == []
    assert env.history(job)["status"] == "failed"


def test_an_exhausted_list_fails_the_job_to_be_retried_later_and_never_declines(env):
    instance, first, second = two_connections(env)
    fallback = env.engine(SECOND_PLUGIN, select=False)
    env.pointers.fallback = fallback.id
    env.scripts[PLUGIN] = {"*": [{"fail": {"code": "rate_limited", "message": "slow down", "retry_after": 120},
                                  "fail_at_batch": 1}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == "rate_limited" and caught.value.retry_later is True
    assert caught.value.retry_at == datetime(2026, 11, 15, 12, 2, 0)
    assert env.served() == [str(first.id), str(second.id)], "the fallback engine was never asked"
    job = env.last_job()
    assert job.state == "awaiting_retry" and job.error_class == "rate_limited"
    assert len(env.checkpoints()) == 1, "the finished batch is kept for the retry"
    row = env.history(job)
    assert row["status"] == "awaiting_retry" and row["declined_by_instance_id"] is None


def test_a_connection_inside_a_reported_retry_time_is_skipped_by_the_next_job(env):
    instance, first, second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"fail": {"code": "rate_limited", "message": "x", "retry_after": 60}},
                                           {}]}
    dispatcher = env.dispatcher()
    dispatcher.run(make_spec())
    assert env.served() == [str(first.id), str(second.id)]
    dispatcher.run(make_spec(source_key="/media/other.en.srt"))
    assert env.served()[2:] == [str(second.id)], "the limited connection is left alone for its retry time"
    env.monotonic.advance(61)
    dispatcher.run(make_spec(source_key="/media/third.en.srt"))
    assert env.served()[3:] == [str(first.id)]


def test_a_refused_connect_is_the_sandbox_and_neither_fails_over_nor_retries(env):
    instance, first, second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"blocked": True}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == fail.SANDBOX_BLOCKED and caught.value.retry_later is False
    assert "Bazarr+" in caught.value.message and "network rule" in caught.value.message
    assert env.served() == [str(first.id)], "the next connection was not tried"
    job = env.last_job()
    assert (job.state, job.error_class) == ("failed", "sandbox_blocked")


# --------------------------------------------------------------------------------------- decline and fallback


def decline(message="the language is not supported"):
    return {"fail": {"code": "declined", "message": message}}


def test_a_decline_goes_to_the_fallback_engine_once(env):
    env.engine(SECOND_PLUGIN)
    fallback = env.engine(PLUGIN, select=False)
    env.connection(fallback)
    env.pointers.fallback = fallback.id
    env.scripts[SECOND_PLUGIN] = {"*": [decline()]}
    outcome = env.dispatcher().run(make_spec())

    assert outcome.engine_instance_id == fallback.id and outcome.plugin_id == PLUGIN
    job = env.last_job()
    assert job.state == "completed" and job.engine_instance_id == fallback.id
    row = env.history(job)
    assert row["declined_by_instance_id"] == env.pointers.engine
    assert row["taken_over_by_instance_id"] == fallback.id and "not supported" in row["declined_reason"]
    assert row["plugin_id"] == PLUGIN, "the row names the engine that served the job"


def test_the_fallback_declining_too_ends_the_job_declined_with_both_reasons(env):
    env.engine(SECOND_PLUGIN)
    fallback = env.ai_engine(select=False)
    env.pointers.fallback = fallback.id
    env.scripts[SECOND_PLUGIN] = {"*": [decline("first no")]}
    env.scripts[PLUGIN] = {"*": [decline("second no")]}
    with pytest.raises(Declined) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.reasons == ["first no", "second no"]
    job = env.last_job()
    assert (job.state, job.error_class) == ("failed", "declined")
    row = env.history(job)
    assert row["status"] == "declined" and row["declined_reason"] == "first no; second no"
    assert len(env.served()) == 2 and env.served()[0] == "-", "each engine was asked once"


def test_a_decline_without_a_fallback_ends_declined(env):
    env.engine(SECOND_PLUGIN)
    env.scripts[SECOND_PLUGIN] = {"*": [decline()]}
    with pytest.raises(Declined):
        env.dispatcher().run(make_spec())
    assert env.history(env.last_job())["taken_over_by_instance_id"] is None


def test_a_fallback_that_is_not_configured_is_skipped_with_a_notice(env):
    env.engine(SECOND_PLUGIN)
    fallback = env.engine(PLUGIN, select=False)
    env.connection(fallback, enabled=False)
    env.pointers.fallback = fallback.id
    env.scripts[SECOND_PLUGIN] = {"*": [decline()]}
    with pytest.raises(Declined):
        env.dispatcher().run(make_spec())
    assert env.served() == ["-"], "the unconfigured fallback was not asked"


def test_the_notice_for_a_skipped_fallback_names_the_cause(env):
    env.engine(SECOND_PLUGIN)
    fallback = env.engine(PLUGIN, select=False)
    env.pointers.fallback = fallback.id
    env.holds.pause_for_drain(fallback.id)
    outcome = env.dispatcher().run(make_spec())
    assert [notice.code for notice in outcome.notices] == ["fallback_skipped"]
    assert "paused" in outcome.notices[0].message


def test_the_fallback_starts_from_its_own_checkpoints(env):
    env.engine(SECOND_PLUGIN)
    fallback = env.ai_engine(select=False)
    env.pointers.fallback = fallback.id
    env.scripts[SECOND_PLUGIN] = {"*": [{"fail": {"code": "declined", "message": "no"}, "fail_at_batch": 1}]}
    outcome = env.dispatcher().run(make_spec())
    assert outcome.lines == [f"[fr] {line}" for line in LINES], "nothing the first engine did was reused"


# ---------------------------------------------------------------------------------------------- a pinned job


def test_a_pinned_connection_gives_its_result_or_its_error_and_nothing_else(env):
    instance, first, second = two_connections(env)
    fallback = env.engine(SECOND_PLUGIN, select=False)
    env.pointers.fallback = fallback.id
    env.scripts[PLUGIN] = {str(second.id): [{"fail": {"code": "rate_limited", "message": "slow"}}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec(pinned_connection_id=second.id))
    assert caught.value.error_class == "rate_limited"
    assert env.served() == [str(second.id)], "no failover to the first connection, no fallback engine"
    env.scripts[PLUGIN] = {}


def test_the_pin_a_manual_pick_recorded_on_the_job_is_honoured(env):
    instance, first, second = two_connections(env)
    jobs = TranslationJobs(env.session, clock=env.clock)
    jobs.request(SOURCE, TARGET, manual=True, pinned_connection_id=second.id)
    env.dispatcher().run(make_spec())  # the spec itself names no pin
    assert env.served() == [str(second.id)]


def test_a_pinned_connection_that_cannot_be_used_closes_the_job(env):
    instance, first, second = two_connections(env)
    env.store.update_connection(second.id, schema=helpers_schema(), enabled=False)
    with pytest.raises(GateClosed) as caught:
        env.dispatcher().run(make_spec(pinned_connection_id=second.id))
    assert "cannot be used" in caught.value.reason and env.requests() == []


# ------------------------------------------------------------------------------------------------- the gate


def test_a_job_whose_gate_closed_while_it_waited_ends_quietly_and_asks_nobody(env):
    instance = env.engine()
    env.connection(instance)
    env.active.discard(PLUGIN)
    with pytest.raises(GateClosed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.cause == "not_configured" and env.requests() == []
    job = env.last_job()
    assert (job.state, job.error_class) == ("cancelled", "gate_closed")
    assert env.checkpoints() == []
    row = env.history(job)
    assert row["status"] == "declined" and row["error_class"] == "gate_closed"


@pytest.mark.parametrize("hold, cause", [("install", "awaiting_install"), ("drain", "sidecar_drain")])
def test_the_two_holds_close_the_gate_for_the_selected_engine(env, hold, cause):
    instance = env.engine()
    env.connection(instance)
    (env.holds.hold_for_install if hold == "install" else env.holds.pause_for_drain)(instance.id)
    with pytest.raises(GateClosed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.cause == cause


def test_no_engine_selected_closes_the_gate(env):
    with pytest.raises(GateClosed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.cause == "no_engine"


# ------------------------------------------------------------------------------------------------ the budget


def test_the_job_budget_is_reserved_at_dispatch_and_handed_to_the_plugin(env):
    instance = env.engine()
    env.connection(instance)
    env.limits = CostLimits(month_cap=usd(10), job_cap=usd(2), translation_limit=2)
    env.dispatcher().run(make_spec())
    assert env.requests()[0]["request"]["budget"] == 2.0


def test_without_a_job_cap_the_budget_is_an_equal_share_of_the_months_remainder(env):
    instance = env.engine()
    env.connection(instance)
    env.limits = CostLimits(month_cap=usd(10), translation_limit=4)
    env.dispatcher().run(make_spec())
    assert env.requests()[0]["request"]["budget"] == 2.5


def test_with_the_month_spent_a_free_connection_runs_and_a_priced_one_is_skipped(env):
    instance = env.engine()
    priced = env.connection(instance, model="paid", price_input_per_million=1.0, price_output_per_million=1.0)
    free = env.connection(instance, model="local")
    env.limits = CostLimits(month_cap=usd(1), translation_limit=1)
    ledger = UsageLedger(env.session, clock=env.clock)
    ledger.reserve_job_budget("other-job", env.limits)  # takes the whole month
    ledger.reserve_call("other-job", "c", worst_case_micros=usd(1), limits=env.limits)
    ledger.release_job("other-job")
    outcome = env.dispatcher().run(make_spec())
    assert outcome.kind == "lines"
    assert env.served() == [str(free.id)], "the priced connection was not asked"
    sent = env.requests()[0]["request"]
    assert sent["budget"] == 0.0 and priced.id != free.id


def test_with_the_month_spent_and_only_priced_connections_the_job_waits_for_next_month(env):
    instance = env.engine()
    env.connection(instance, price_input_per_million=1.0, price_output_per_million=1.0)
    env.limits = CostLimits(month_cap=usd(1), translation_limit=1)
    ledger = UsageLedger(env.session, clock=env.clock)
    ledger.reserve_job_budget("other-job", env.limits)
    ledger.reserve_call("other-job", "c", worst_case_micros=usd(1), limits=env.limits)
    ledger.release_job("other-job")
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == "month_cap" and caught.value.retry_later is True
    assert caught.value.retry_at == datetime(2026, 12, 1)
    assert env.requests() == [], "no attempt was made"
    job = env.last_job()
    assert (job.state, job.error_class) == ("awaiting_retry", "month_cap")


def test_a_reservation_that_does_not_fit_stops_the_attempt_as_a_budget_failure(env):
    instance = env.engine()
    env.connection(instance, price_input_per_million=1.0, price_output_per_million=1.0)
    env.limits = CostLimits(job_cap=usd(0.01), translation_limit=1)
    env.scripts[PLUGIN] = {"*": [{"reserve": 0.5}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == "budget_exhausted" and caught.value.retry_later is True


def test_a_worker_killed_between_its_hold_and_its_settlement_leaves_the_worst_case_counted(env):
    instance = env.engine()
    env.connection(instance, price_input_per_million=1.0, price_output_per_million=1.0)
    env.limits = CostLimits(job_cap=usd(1), translation_limit=1)
    env.scripts[PLUGIN] = {"*": [{"reserve": 0.4, "die_after": "reserve"}, {"reserve": 0.4, "cost": 0.1}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == "worker_failed" and caught.value.retry_later is True
    job = env.last_job()
    ledger = UsageLedger(env.session, clock=env.clock)
    assert ledger.job_cost(str(job.id)) == usd(0.4), "the hold stays at its worst case"
    # The retry cannot spend past the job cap: what the killed call held still counts.
    time.sleep(0.3)  # the pool restarts a crashed worker after a short backoff
    env.dispatcher().run(make_spec())
    assert ledger.job_cost(str(job.id)) == usd(0.4) + 3 * usd(0.1)


def test_spend_reported_without_a_hold_is_counted(env):
    instance = env.engine()
    env.connection(instance)
    from provider_hub.runtime.errors import HostCallError  # noqa: F401  (the handler's error type)
    from subtitles.tools.translate.runtime import host_calls as hc

    job_id = TranslationJobs(env.session, clock=env.clock).request(SOURCE, TARGET).job_id
    context = hc.AttemptContext(session=env.session, job_id=job_id, connection_id=None, limits=lambda: CostLimits(),
                                clock=env.clock)
    call = type("Call", (), {"context": context, "params": {"tokens": 12, "cost": 0.002, "model": "m",
                                                           "reservations": []}})()
    hc.usage(call)
    ledger = UsageLedger(env.session, clock=env.clock)
    assert ledger.job_cost(str(job_id)) == usd(0.002)


# ------------------------------------------------------------------------------------------- cancel and time


def test_a_stop_cancels_the_request_and_ends_the_job_cancelled(env):
    from app.jobs_queue import JobCancelled

    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"wait_cancel": True}]}
    stop = threading.Event()

    def check():
        if stop.is_set():
            raise JobCancelled("stopped")

    threading.Timer(0.8, stop.set).start()
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        env.dispatcher().run(make_spec(check_cancel=check))
    assert time.monotonic() - started < 6
    job = env.last_job()
    assert job.state == "cancelled" and env.checkpoints() == []
    assert env.history(job)["status"] == "cancelled"


def test_a_worker_that_stops_reporting_progress_is_cancelled_and_the_job_is_retried_later(env):
    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"wait_cancel": True}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher(monotonic=time.monotonic, deadlines={"inactivity": 0.6, "grace": 2.0}).run(make_spec())
    assert caught.value.error_class == "inactivity_timeout" and caught.value.retry_later is True
    assert env.last_job().state == "awaiting_retry"


def test_a_worker_that_ignores_the_cancel_is_abandoned_after_the_grace_period(env):
    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"wait_cancel": True, "ignore_cancel": True}]}
    env.session_overrides = {"cancel_grace": 30.0}  # the session's own kill comes later than the job's grace
    abandoned = []

    class Port:
        def __init__(self, inner):
            self.inner = inner

        def submit(self, *args, **kwargs):
            return self.inner.submit(*args, **kwargs)

        def abandon(self, plugin_id):
            abandoned.append(plugin_id)

    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher(workers=Port(env.workers), monotonic=time.monotonic,
                       deadlines={"inactivity": 0.4, "grace": 0.6}).run(make_spec())
    assert caught.value.error_class == "inactivity_timeout"
    assert abandoned == [PLUGIN]


def test_the_overall_deadline_ends_the_job_for_good(env):
    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"wait_cancel": True}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher(monotonic=time.monotonic,
                       deadlines={"overall": 0.5, "inactivity": 60, "grace": 2.0}).run(make_spec())
    assert caught.value.error_class == "deadline_exceeded" and caught.value.retry_later is False
    assert env.last_job().state == "failed"


# ----------------------------------------------------------------------------------- the output of an engine


@pytest.mark.parametrize("corrupt, code", [("bad_tag", "disallowed_tag"), ("control", "control_character"),
                                           ("drop", "line_count")])
def test_an_answer_that_fails_the_checks_is_refused_and_never_fails_over(env, corrupt, code):
    instance, first, _second = two_connections(env)
    env.scripts[PLUGIN] = {str(first.id): [{"corrupt": corrupt}]}
    with pytest.raises(DispatchFailed) as caught:
        env.dispatcher().run(make_spec())
    assert caught.value.error_class == "output_check_failed" and caught.value.retry_later is False
    assert env.served() == [str(first.id)]
    assert env.history(env.last_job())["status"] == "failed"


def test_a_bug_in_the_dispatcher_leaves_the_job_awaiting_a_retry_and_not_running(env, monkeypatch):
    from subtitles.tools.translate.runtime.dispatch import Dispatcher

    env.ai_engine()
    monkeypatch.setattr(Dispatcher, "_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("a bug")))
    with pytest.raises(RuntimeError):
        env.dispatcher().run(make_spec())
    job = env.last_job()
    assert (job.state, job.error_class) == ("awaiting_retry", "internal")
    assert env.history(job)["status"] == "awaiting_retry"
    assert UsageLedger(env.session, clock=env.clock).job_hold(str(job.id)) == 0


def test_a_partial_answer_keeps_the_source_text_for_the_lines_it_left_out(env):
    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"partial": [1, 3]}]}
    outcome = env.dispatcher().run(make_spec())
    assert outcome.lines == ["[fr] One", "Two", "[fr] Three", "Four", "[fr] Five"]
    assert outcome.partial_error == "No translated text was returned for 2 of 5 cues."
    job = env.last_job()
    assert job.state == "completed" and env.history(job)["status"] == "partial"


# ------------------------------------------------------------------------------------------ restart and retry


def test_a_retry_resumes_after_the_last_finished_batch(env):
    instance = env.engine()
    env.connection(instance)
    env.scripts[PLUGIN] = {"*": [{"die_after": "batch:1"}, {}]}
    with pytest.raises(DispatchFailed):
        env.dispatcher().run(make_spec())
    job = env.last_job()
    assert job.state == "awaiting_retry" and len(env.checkpoints()) == 1
    time.sleep(0.3)  # the pool restarts a crashed worker after a short backoff
    outcome = env.dispatcher().run(make_spec())
    assert outcome.lines == ["ck:[fr] One", "ck:[fr] Two", "[fr] Three", "[fr] Four", "[fr] Five"]
    assert env.last_job().id == job.id and env.last_job().state == "completed"
    assert env.checkpoints() == []
    assert len(env.requests()[-1]["request"]["checkpoints"]) == 1


def test_an_edited_source_does_not_resume_from_the_old_batches(env):
    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"die_after": "batch:1"}, {}]}
    with pytest.raises(DispatchFailed):
        env.dispatcher().run(make_spec())
    time.sleep(0.3)  # the pool restarts a crashed worker after a short backoff
    outcome = env.dispatcher().run(make_spec(lines=["Uno", "Dos", "Tres"]))
    assert outcome.lines == ["[fr] Uno", "[fr] Dos", "[fr] Tres"]


def test_recovery_after_a_restart_makes_cut_off_jobs_await_a_retry_and_gives_back_stale_budgets(env):
    from subtitles.tools.translate.runtime.dispatch import recover_after_restart

    jobs = TranslationJobs(env.session, clock=env.clock)
    job_id = jobs.request(SOURCE, TARGET).job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    ledger = UsageLedger(env.session, clock=env.clock)
    limits = CostLimits(month_cap=usd(10), translation_limit=1)
    ledger.reserve_job_budget("999", limits)  # a budget no live job holds
    ledger.reserve_job_budget(str(job_id), limits)
    counts = recover_after_restart(env.session, clock=env.clock)
    assert counts["recovered"] == 1 and counts["released"] == 1
    assert env.job(job_id).state == "awaiting_retry" and env.job(job_id).error_class == "interrupted"


def test_the_upkeep_runs_from_the_first_job_and_then_once_an_hour(env, monkeypatch):
    from subtitles.tools.translate.runtime import dispatch

    ran = []
    monkeypatch.setattr(dispatch, "maintain", lambda *args, **kwargs: ran.append(1) or {})
    env.ai_engine()
    dispatcher = env.dispatcher()
    dispatcher.run(make_spec())
    dispatcher.run(make_spec(source_key="/media/b.en.srt"))
    assert len(ran) == 1
    env.monotonic.advance(3601)
    dispatcher.run(make_spec(source_key="/media/c.en.srt"))
    assert len(ran) == 2


def test_the_upkeep_gives_back_stale_budgets_moves_running_ones_and_sweeps_expired_checkpoints(env):
    from subtitles.tools.translate.runtime.dispatch import maintain

    jobs = TranslationJobs(env.session, clock=env.clock)
    job_id = jobs.request(SOURCE, TARGET).job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    jobs.save_batch(job_id, 0, ["x"])
    jobs.await_retry(job_id, "rate_limited", "later")
    ledger = UsageLedger(env.session, clock=env.clock)
    limits = CostLimits(month_cap=usd(10), translation_limit=2)
    ledger.reserve_job_budget(str(job_id), limits)
    ledger.reserve_job_budget("gone", limits)
    env.clock.advance(days=8)  # past the checkpoint retention, and the budget of the live job is in an old month
    counts = maintain(env.session, clock=env.clock, limits=limits)
    assert counts["released"] == 1 and counts["swept"] == 1
    assert env.checkpoints() == []


def test_a_job_that_is_already_running_elsewhere_is_skipped(env):
    env.ai_engine()
    jobs = TranslationJobs(env.session, clock=env.clock)
    job_id = jobs.request(SOURCE, TARGET).job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    outcome = env.dispatcher().run(make_spec())
    assert outcome.kind == "skipped" and env.requests() == []


# --------------------------------------------------------------------------------------- engines with no worker


def lingarr_instance(env, select=True, settings=None):
    from subtitles.tools.translate.runtime import legacy_engines

    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, name="Lingarr",
                                         settings=settings or {legacy_engines.SAVED_FLAG: True})
    if select:
        env.pointers.engine = instance.id
    return instance


@pytest.fixture
def lingarr_url(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "lingarr_url", "http://lingarr.example.test:9876")


def test_lingarr_is_served_by_the_in_core_service_with_no_worker_no_cost_and_its_own_history(env, lingarr_url):
    instance = lingarr_instance(env)
    asked = []

    def legacy(engine, row):
        asked.append((engine.plugin_id, engine.translator_type, row.id))
        return LegacyResult(published="/media/film/film.fr.srt")

    outcome = env.dispatcher().run(make_spec(legacy=legacy))
    assert outcome.kind == "published" and outcome.published == "/media/film/film.fr.srt"
    assert asked == [("lingarr", "lingarr", instance.id)] and env.requests() == []
    job = env.last_job()
    assert job.state == "completed"
    row = env.history(job)
    assert (row["plugin_id"], row["status"], row["connections"], row["cost_micros"]) == ("lingarr", "completed", [], 0)


def test_lingarr_with_no_address_is_not_configured(env, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "lingarr_url", "")
    lingarr_instance(env, settings={})
    with pytest.raises(GateClosed) as caught:
        env.dispatcher().run(make_spec(legacy=lambda *_: LegacyResult(published="x")))
    assert "no URL" in caught.value.reason


def test_a_lingarr_server_that_is_down_declines_and_the_fallback_engine_takes_the_job(env, lingarr_url):
    lingarr_instance(env)
    fallback = env.ai_engine(select=False)
    env.pointers.fallback = fallback.id

    def legacy(engine, row):
        raise LegacyDeclined("The Lingarr server could not be reached.")

    outcome = env.dispatcher().run(make_spec(legacy=legacy))
    assert outcome.kind == "lines" and outcome.engine_instance_id == fallback.id
    row = env.history(env.last_job())
    assert "could not be reached" in row["declined_reason"] and row["taken_over_by_instance_id"] == fallback.id


def test_lingarr_can_be_the_fallback_of_a_worker_engine(env, lingarr_url):
    env.engine(SECOND_PLUGIN)
    env.pointers.fallback = lingarr_instance(env, select=False).id
    env.scripts[SECOND_PLUGIN] = {"*": [decline()]}
    outcome = env.dispatcher().run(make_spec(legacy=lambda *_: LegacyResult(lines=[f"L {x}" for x in LINES])))
    assert outcome.kind == "lines" and outcome.lines[0] == "L One" and outcome.plugin_id == "lingarr"


def test_an_engine_with_no_connections_takes_one_attempt(env):
    env.engine(SECOND_PLUGIN)
    outcome = env.dispatcher().run(make_spec())
    assert outcome.lines == [f"[fr] {line}" for line in LINES]
    assert env.history(env.last_job())["connections"] == []
    assert "connection" not in env.requests()[0]["request"]["config"]


# ------------------------------------------------------------------------------------------ the host calls


def _host_call(env, job_id, params, limits=None, **context_values):
    from subtitles.tools.translate.runtime import host_calls as hc

    context = hc.AttemptContext(session=env.session, job_id=job_id, connection_id=None,
                                limits=lambda: limits or CostLimits(), clock=env.clock, **context_values)
    return context, type("Call", (), {"context": context, "params": params})()


def _running_job(env):
    jobs = TranslationJobs(env.session, clock=env.clock)
    job_id = jobs.request(SOURCE, TARGET).job_id
    assert jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    return job_id


@pytest.mark.parametrize("params", [{}, {"amount": -1, "model": "m"}, {"amount": "x"}, {"amount": float("nan")},
                                    {"amount": True}])
def test_a_hold_with_a_bad_amount_is_refused(env, params):
    from provider_hub.runtime.errors import HostCallError
    from subtitles.tools.translate.runtime import host_calls as hc

    _context, call = _host_call(env, _running_job(env), params)
    with pytest.raises(HostCallError) as caught:
        hc.reserve(call)
    assert caught.value.code == "invalid_request"


def test_a_hold_that_does_not_fit_is_refused_as_cancelled_and_leaves_its_reason(env):
    from provider_hub.runtime.errors import HostCallError
    from subtitles.tools.translate.runtime import host_calls as hc

    job_id = _running_job(env)
    limits = CostLimits(job_cap=usd(0.01), translation_limit=1)
    UsageLedger(env.session, clock=env.clock).reserve_job_budget(str(job_id), limits)
    context, call = _host_call(env, job_id, {"amount": 0.5, "model": "m"}, limits=limits)
    with pytest.raises(HostCallError) as caught:
        hc.reserve(call)
    assert caught.value.code == "cancelled" and context.refusal == "job_budget"


def test_a_hold_that_fits_is_stored_before_it_is_answered(env):
    from subtitles.tools.translate.runtime import host_calls as hc

    job_id = _running_job(env)
    limits = CostLimits(job_cap=usd(1), translation_limit=1)
    ledger = UsageLedger(env.session, clock=env.clock)
    ledger.reserve_job_budget(str(job_id), limits)
    context, call = _host_call(env, job_id, {"amount": 0.25, "model": "m"}, limits=limits)
    hold = hc.reserve(call)["hold"]
    assert ledger.job_cost(str(job_id)) == usd(0.25) and context.held == [hold]
    settle = type("Call", (), {"context": context, "params": {"tokens": 5, "cost": 0.1, "model": "m",
                                                             "reservations": [hold]}})()
    hc.usage(settle)
    assert ledger.job_cost(str(job_id)) == usd(0.1)


def test_settling_a_hold_that_is_not_the_jobs_is_refused(env):
    from provider_hub.runtime.errors import HostCallError
    from subtitles.tools.translate.runtime import host_calls as hc

    _context, call = _host_call(env, _running_job(env), {"tokens": 1, "cost": 0.0, "model": "m",
                                                          "reservations": ["nope"]})
    with pytest.raises(HostCallError) as caught:
        hc.usage(call)
    assert caught.value.code == "invalid_request"


def test_a_settlement_that_crosses_the_budget_cancels_the_attempt(env):
    from subtitles.tools.translate.runtime import host_calls as hc

    job_id = _running_job(env)
    limits = CostLimits(job_cap=usd(0.1), translation_limit=1)
    ledger = UsageLedger(env.session, clock=env.clock)
    ledger.reserve_job_budget(str(job_id), limits)
    stopped = []
    context, call = _host_call(env, job_id, {"amount": 0.05, "model": "m"}, limits=limits)
    context.cancel = stopped.append
    hold = hc.reserve(call)["hold"]
    over = type("Call", (), {"context": context, "params": {"tokens": 5, "cost": 0.3, "model": "m",
                                                           "reservations": [hold]}})()
    hc.usage(over)
    assert context.over_budget is True and stopped == ["budget_exhausted"]


def test_a_checkpoint_of_a_job_that_is_not_running_is_refused(env):
    from provider_hub.runtime.errors import HostCallError
    from subtitles.tools.translate.runtime import host_calls as hc

    jobs = TranslationJobs(env.session, clock=env.clock)
    job_id = jobs.request(SOURCE, TARGET).job_id  # queued, never started
    _context, call = _host_call(env, job_id, {"data": {"lines": {}}})
    with pytest.raises(HostCallError) as caught:
        hc.checkpoint(call)
    assert caught.value.code == "cancelled" and env.checkpoints() == []


def test_a_checkpoint_must_be_plain_json_and_is_kept_as_written(env):
    from provider_hub.runtime.errors import HostCallError
    from subtitles.tools.translate.runtime import host_calls as hc

    job_id = _running_job(env)
    context, bad = _host_call(env, job_id, {"data": {"x": float("nan")}})
    with pytest.raises(HostCallError):
        hc.checkpoint(bad)
    context, call = _host_call(env, job_id, {"data": {"lines": {"0": "a"}, "model": "m"}})
    hc.checkpoint(call)
    assert TranslationJobs(env.session, clock=env.clock).finished_batches(job_id) == {0: ['{"lines": {"0": "a"}, "model": "m"}']}


def test_each_host_call_gives_its_threads_database_session_back(env, monkeypatch):
    from subtitles.tools.translate.runtime import host_calls as hc

    released = []
    monkeypatch.setattr(env.session, "remove", lambda: released.append(1))
    job_id = _running_job(env)
    context, call = _host_call(env, job_id, {"amount": 0, "model": "m"})
    hc.reserve(call)
    hc.checkpoint(_host_call(env, job_id, {"data": {"a": 1}})[1])
    hc.usage(_host_call(env, job_id, {"tokens": 1, "cost": 0, "model": "m", "reservations": []})[1])
    assert len(released) == 3, "a call that failed releases it too"
    with pytest.raises(Exception):
        hc.usage(_host_call(env, job_id, {"tokens": -1, "cost": 0})[1])
    assert len(released) == 4


def test_a_call_that_belongs_to_no_running_translation_is_refused(env):
    from provider_hub.runtime.errors import HostCallError
    from subtitles.tools.translate.runtime import host_calls as hc

    with pytest.raises(HostCallError):
        hc.reserve(type("Call", (), {"context": None, "params": {"amount": 0, "model": "m"}})())


def test_two_jobs_at_once_never_hold_the_same_money(env):
    instance = env.engine()
    env.connection(instance)
    env.limits = CostLimits(month_cap=usd(1), translation_limit=2)
    env.scripts[PLUGIN] = {"*": [{"delay": 0.05}]}
    outcomes, errors = [], []

    def run(key):
        try:
            outcomes.append(env.dispatcher().run(make_spec(source_key=key)))
        except Exception as error:  # reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run, args=(key,)) for key in ("/media/a.en.srt", "/media/b.en.srt")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [] and len(outcomes) == 2
    budgets = sorted(item["request"]["budget"] for item in env.requests())
    assert budgets[-1] <= 0.5 and sum(budgets) <= 1.0, "two jobs were each handed no more than their share"
