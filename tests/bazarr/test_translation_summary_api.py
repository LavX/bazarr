"""The Translation page's status routes: summary, cost limits and the waiting queue.

Each is dark while the runtime switch is off, needs the API key, and answers in dollars while the ledger
underneath keeps whole micro-dollars.
"""
import json
from datetime import datetime

import pytest
from flask import Blueprint, Flask
from flask_restx import Api

from subtitles.tools.translate.runtime.history import HistoryStore
from subtitles.tools.translate.runtime.limits import ConfigLimits, StoredLimits, TranslationLimits
from subtitles.tools.translate.runtime.summary import TranslationSummary
from subtitles.tools.translate.runtime.usage import CostLimits, UsageLedger

API_KEY = "translation-summary-test-key"  # pragma: allowlist secret
MOMENT = datetime(2026, 11, 15, 12, 0, 0)


class MemoryLimits:
    """The caps held in memory, with the same shape as the config-backed store."""

    def __init__(self):
        self.stored = StoredLimits(None, None, 1)
        self.writes = 0
        self.fail = False

    def get(self):
        return self.stored

    def set(self, month_cap_micros, job_cap_micros, revision):
        if self.fail:
            return False
        self.stored = StoredLimits(month_cap_micros, job_cap_micros, revision)
        self.writes += 1
        return True


@pytest.fixture
def world(monkeypatch, schema_session):
    from subtitles.tools.translate import runtime

    import api.translation.summary as module
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)

    class World:
        pass

    w = World()
    w.session = schema_session
    w.memory = MemoryLimits()
    w.limits = TranslationLimits(w.memory)
    w.counts = {"pending": 2, "running": 1}
    w.queue_jobs = {}
    w.positions = {}
    w.titles = {}
    w.summary = TranslationSummary(
        schema_session, limits=w.limits, queue_counts=lambda: w.counts, queue_job=lambda id: w.queue_jobs.get(id),
        queue_positions=lambda: w.positions,
        title_lookup=lambda *args: w.titles.get(args, ""), translation_limit=lambda: 2, clock=lambda: MOMENT)
    w.real_get_summary = module.get_summary
    monkeypatch.setattr(module, "get_summary", lambda: w.summary)
    w.real_get_limits = module.get_limits
    monkeypatch.setattr(module, "get_limits", lambda: w.limits)

    blueprint = Blueprint("translation_summary_test", __name__, url_prefix="/api")
    Api(blueprint, authorizations={"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}},
        security="apikey").add_namespace(module.api_ns_translation_summary, "/")
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    client = app.test_client()

    def call(method, path, body=None, key=True, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body))
        return getattr(client, method)("/api/translation/" + path, headers={"X-API-KEY": API_KEY} if key else {},
                                       data=data, content_type="application/json")

    w.call, w.runtime, w.module = call, runtime, module
    return w


def _job(session, state, path="/media/Show/S01E01.en.srt", target="hu", **fields):
    from app.database import TableTranslatorJob

    row = TableTranslatorJob(source_path=path, target=target, state=state, created_at=MOMENT, updated_at=MOMENT,
                             **fields)
    session.add(row)
    session.commit()
    return row


# ------------------------------------------------------------------- the switch

ROUTES = [("get", "summary", None), ("put", "limits", {"month_cap": 1, "job_cap": None}), ("get", "queue", None)]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_404_while_the_switch_is_off(world, monkeypatch, method, path, body):
    monkeypatch.setattr(world.runtime, "runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(world.module, "get_summary", lambda: pytest.fail("the summary was read with the runtime off"))
    monkeypatch.setattr(world.module, "get_limits", lambda: pytest.fail("the limits were read with the runtime off"))
    assert world.call(method, path, body).status_code == 404
    # Without the key too: the guard sits outside authentication, so a missing route looks the same.
    assert world.call(method, path, body, key=False).status_code == 404
    assert world.memory.writes == 0


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_needs_the_api_key(world, method, path, body):
    assert world.call(method, path, body, key=False).status_code == 401
    assert world.memory.writes == 0


# --------------------------------------------------------------------- summary

def test_an_empty_ledger_has_no_spend_and_no_caps(world):
    body = world.call("get", "summary").json
    assert body["month"] == {"key": "2026-11", "spent": 0.0, "cap": None}
    assert body["job_cap"] is None
    assert body["queue"] == {"running": 1, "pending": 2, "limit": 2}


def test_the_month_spend_is_the_figure_the_cap_is_checked_against(world):
    ledger = UsageLedger(world.session, clock=lambda: MOMENT)
    limits = CostLimits.from_dollars(month_cap=20, job_cap=1)
    world.memory.stored = StoredLimits(20_000_000, 1_000_000, 1)
    assert ledger.reserve_job_budget("job-a", limits).status == "reserved"
    ledger.reserve_call("job-a", "c1", worst_case_micros=500_000, limits=limits)
    ledger.settle_call("job-a", "c1", input_tokens=1, output_tokens=1, actual_micros=250_000)
    world.session.commit()
    body = world.call("get", "summary").json
    # 0.25 settled, and 0.75 still held for the job's remaining budget of 1.00.
    assert body["month"] == {"key": "2026-11", "spent": 1.0, "cap": 20.0}
    assert body["job_cap"] == 1.0
    assert body["month"]["spent"] == ledger.month_committed() / 1_000_000


def test_a_job_awaiting_its_retry_counts_as_pending_beside_the_queue(world):
    _job(world.session, "awaiting_retry", path="/media/a.en.srt")
    _job(world.session, "completed", path="/media/b.en.srt")
    _job(world.session, "queued", path="/media/c.en.srt")
    body = world.call("get", "summary").json
    # The two on the in-memory queue, plus the one that waits only in the job table.
    assert body["queue"] == {"running": 1, "pending": 3, "limit": 2}


def test_garbage_counts_are_read_as_zero_and_a_missing_limit_is_null(world):
    world.counts = {"pending": "many", "running": None}
    world.summary = TranslationSummary(world.session, limits=world.limits, queue_counts=lambda: world.counts,
                                       translation_limit=lambda: None, clock=lambda: MOMENT)
    assert world.call("get", "summary").json["queue"] == {"running": 0, "pending": 0, "limit": None}


def test_the_real_summary_on_the_application_database_answers_the_summary_the_limits_save_and_the_queue(
        world, monkeypatch):
    """No stubbed reader or writer: ``TranslationSummary(database)`` and the config-backed limits, as built."""
    import app.config as config
    import app.database
    from app.config import settings
    from app.jobs_queue import jobs_queue

    # The live settings are put back when the test ends, and nothing is written to disk.
    for name in ("month_cap_micros", "job_cap_micros", "limits_revision"):
        monkeypatch.setattr(settings.translator, name, getattr(settings.translator, name))
    monkeypatch.setattr(config, "write_config", lambda *args, **kwargs: True)
    monkeypatch.setattr(app.database, "database", world.session)
    monkeypatch.setattr(world.module, "get_summary", world.real_get_summary)
    monkeypatch.setattr(world.module, "get_limits", world.real_get_limits)
    expected = jobs_queue.translation_lane_counts()
    summary = world.call("get", "summary")
    assert summary.status_code == 200, summary.get_data(as_text=True)
    assert summary.json["queue"]["running"] == expected["running"]
    assert summary.json["queue"]["pending"] == expected["pending"]
    assert isinstance(summary.json["queue"]["limit"], int)
    saved = world.call("put", "limits", {"month_cap": 4.5, "job_cap": None})
    assert saved.status_code == 200 and saved.json["month"]["cap"] == 4.5 and saved.json["job_cap"] is None
    assert world.call("get", "summary").json["month"]["cap"] == 4.5
    assert world.call("get", "queue").status_code == 200
    assert isinstance(jobs_queue.translation_queue_positions(), dict)


# ---------------------------------------------------------------------- limits

def test_saving_limits_answers_with_the_summary_and_keeps_micro_dollars(world):
    response = world.call("put", "limits", {"month_cap": 35.0, "job_cap": None})
    assert response.status_code == 200, response.json
    assert response.json["month"]["cap"] == 35.0 and response.json["job_cap"] is None
    assert world.memory.stored == StoredLimits(35_000_000, None, 2)
    assert world.call("get", "summary").json["month"]["cap"] == 35.0
    assert world.call("put", "limits", {"month_cap": None, "job_cap": 0.5}).json["job_cap"] == 0.5
    assert world.memory.stored.job_cap_micros == 500_000 and world.memory.stored.month_cap_micros is None


def test_a_cap_rounds_down_so_the_ledger_never_admits_more_than_was_typed(world):
    world.call("put", "limits", {"month_cap": 1.0000009, "job_cap": 0})
    assert world.memory.stored.month_cap_micros == 1_000_000
    assert world.memory.stored.job_cap_micros == 0
    cost = TranslationLimits(world.memory).cost_limits(3)
    assert (cost.month_cap, cost.job_cap, cost.translation_limit) == (1_000_000, 0, 3)


@pytest.mark.parametrize("body,field,reason", [
    ({"month_cap": -1, "job_cap": None}, "month_cap", "must be 0 or more"),
    ({"month_cap": None, "job_cap": -0.01}, "job_cap", "must be 0 or more"),
    ({"month_cap": "5", "job_cap": None}, "month_cap", "must be a number or null"),
    ({"month_cap": True, "job_cap": None}, "month_cap", "must be a number or null"),
    ({"month_cap": [1], "job_cap": None}, "month_cap", "must be a number or null"),
    ({"month_cap": float("nan"), "job_cap": None}, "month_cap", "must be a finite number"),
    ({"month_cap": float("inf"), "job_cap": None}, "month_cap", "must be a finite number"),
    ({"month_cap": 10 ** 9, "job_cap": None}, "month_cap", "must be at most 1000000"),
    ({"month_cap": 1}, "job_cap", "is required, send a number or null"),
    ({"month_cap": 1, "job_cap": None, "model": "x"}, "model", "is not a cost limit"),
])
def test_an_invalid_limit_is_refused_by_field_and_nothing_is_saved(world, body, field, reason):
    response = world.call("put", "limits", body)
    assert response.status_code == 400
    assert response.json["error"] == "invalid"
    assert response.json["field_errors"][field] == reason
    assert response.json["message"]
    assert world.memory.writes == 0 and world.memory.stored.revision == 1


def test_every_bad_field_is_reported_together(world):
    response = world.call("put", "limits", {"month_cap": -1, "job_cap": "x"})
    assert set(response.json["field_errors"]) == {"month_cap", "job_cap"}


@pytest.mark.parametrize("raw", ["[1, 2]", "not json", '"text"', ""])
def test_a_body_that_is_not_an_object_is_refused(world, raw):
    response = world.call("put", "limits", raw=raw)
    assert response.status_code == 400 and response.json["error"] == "invalid"
    assert world.memory.writes == 0


def test_a_save_made_after_the_limits_moved_on_is_refused_with_409(world):
    first = world.call("get", "summary").json["limits_revision"]
    assert world.call("put", "limits", {"month_cap": 10, "job_cap": None, "expected_revision": first}).status_code == 200
    stale = world.call("put", "limits", {"month_cap": 99, "job_cap": 9, "expected_revision": first})
    assert stale.status_code == 409
    assert stale.json["error"] == "stale_revision" and stale.json["revision"] == first + 1
    assert stale.json["message"]
    assert world.memory.stored == StoredLimits(10_000_000, None, first + 1)


def test_a_save_with_the_current_revision_lands_and_one_without_a_revision_overwrites(world):
    revision = world.call("get", "summary").json["limits_revision"]
    assert world.call("put", "limits", {"month_cap": 1, "job_cap": 2, "expected_revision": revision}).status_code == 200
    assert world.call("put", "limits", {"month_cap": 3, "job_cap": 4}).status_code == 200
    assert world.memory.stored == StoredLimits(3_000_000, 4_000_000, revision + 2)


@pytest.mark.parametrize("revision", ["1", True, 1.5, [1]])
def test_a_revision_that_is_not_a_whole_number_is_refused(world, revision):
    response = world.call("put", "limits", {"month_cap": 1, "job_cap": None, "expected_revision": revision})
    assert response.status_code == 400
    assert response.json["field_errors"] == {"expected_revision": "must be a number"}


def test_a_file_that_cannot_be_written_leaves_the_limits_as_they_were(world):
    world.memory.fail = True
    response = world.call("put", "limits", {"month_cap": 5, "job_cap": None})
    assert response.status_code == 400 and response.json["error"] == "invalid"
    assert world.memory.stored == StoredLimits(None, None, 1)


# ----------------------------------------------------------------------- queue

def test_the_queue_lists_waiting_jobs_in_the_order_they_will_run(world):
    retry = _job(world.session, "awaiting_retry", path="/media/retry.en.srt", retry_at=datetime(2026, 11, 15, 12, 5))
    _job(world.session, "running", path="/media/running.en.srt")
    _job(world.session, "completed", path="/media/done.en.srt")
    _job(world.session, "superseded", path="/media/old.en.srt")
    first = _job(world.session, "queued", path="/media/first.en.srt")
    second = _job(world.session, "queued", path="/media/second.en.srt", target="de:forced")
    body = world.call("get", "queue").json
    assert body["total"] == 3
    assert [item["job_id"] for item in body["data"]] == [str(first.id), str(second.id), str(retry.id)]
    assert [item["status"] for item in body["data"]] == ["queued", "queued", "awaiting_retry"]
    assert body["data"][0]["queued_at"] == MOMENT.isoformat()


def test_a_job_whose_place_on_the_queue_is_known_goes_first_in_that_order(world):
    a = _job(world.session, "queued", path="/media/a.en.srt", queue_job_id=21)
    b = _job(world.session, "queued", path="/media/b.en.srt", queue_job_id=22)
    c = _job(world.session, "queued", path="/media/c.en.srt")
    d = _job(world.session, "queued", path="/media/d.en.srt", queue_job_id=99)  # no longer on the queue
    retry = _job(world.session, "awaiting_retry", path="/media/r.en.srt", queue_job_id=23)
    world.positions = {21: 5, 22: 0, 23: 1}
    ids = [item["job_id"] for item in world.call("get", "queue").json["data"]]
    # Known places first (b, then a), the rest by the time they were asked for, retries last.
    assert ids == [str(b.id), str(a.id), str(c.id), str(d.id), str(retry.id)]


def test_a_queue_that_cannot_be_read_falls_back_to_the_time_they_were_asked_for(world):
    first = _job(world.session, "queued", path="/media/first.en.srt", queue_job_id=1)
    second = _job(world.session, "queued", path="/media/second.en.srt", queue_job_id=2)

    def broken():
        raise RuntimeError("queue gone")

    world.summary = TranslationSummary(world.session, limits=world.limits, queue_counts=lambda: world.counts,
                                       queue_positions=broken, translation_limit=lambda: 2, clock=lambda: MOMENT)
    assert [item["job_id"] for item in world.call("get", "queue").json["data"]] == [str(first.id), str(second.id)]


def test_a_waiting_job_is_named_from_its_history_row_first(world):
    row = _job(world.session, "awaiting_retry", path="/media/Severance/S02E05.en.srt", queue_job_id=7)
    HistoryStore(world.session).open_job(
        str(row.id), media_type="episode", media_id=4112, media_title="Severance S02E05: Trojan's Horse",
        source_language="en", target_language="hu", engine_instance_id=1)
    world.queue_jobs[7] = {"kwargs": {"media_type": "movie", "radarr_id": 9, "from_lang": "fr", "to_lang": "de"}}
    (item,) = world.call("get", "queue").json["data"]
    assert item == {
        "job_id": str(row.id), "media_type": "episode", "media_id": 4112,
        "media_title": "Severance S02E05: Trojan's Horse", "source_language": "en", "target_language": "hu",
        "engine_instance_id": 1, "status": "awaiting_retry", "queued_at": MOMENT.isoformat()}


def test_a_queued_job_is_named_from_the_job_waiting_on_the_in_memory_queue(world):
    row = _job(world.session, "queued", path="/media/Show/S01E02.en.srt", queue_job_id=11)
    world.queue_jobs[11] = {"kwargs": {"media_type": "episode", "sonarr_series_id": 3, "sonarr_episode_id": 88,
                                       "radarr_id": None, "arr_instance_id": 2, "from_lang": "en", "to_lang": "hu"}}
    world.titles[("episode", None, 3, 88, 2)] = "Show S01E02"
    (item,) = world.call("get", "queue").json["data"]
    assert (item["job_id"], item["media_type"], item["media_id"], item["media_title"]) == (
        str(row.id), "episode", 88, "Show S01E02")
    assert (item["source_language"], item["target_language"]) == ("en", "hu")


def test_a_movie_on_the_in_memory_queue_names_its_radarr_id(world):
    _job(world.session, "queued", queue_job_id=12)
    world.queue_jobs[12] = {"kwargs": {"media_type": "movie", "radarr_id": 55, "from_lang": "en", "to_lang": "hu"}}
    (item,) = world.call("get", "queue").json["data"]
    assert (item["media_type"], item["media_id"]) == ("movie", 55)


def test_a_movie_queued_as_movies_keeps_its_media_id(world):
    _job(world.session, "queued", queue_job_id=13)
    world.queue_jobs[13] = {"kwargs": {"media_type": "movies", "radarr_id": 56, "from_lang": "en", "to_lang": "hu"}}
    (item,) = world.call("get", "queue").json["data"]
    assert (item["media_type"], item["media_id"]) == ("movie", 56)


def test_a_job_nothing_knows_is_named_by_its_file_and_target_and_leaks_no_directory(world):
    _job(world.session, "queued", path="/private/library/Some Movie (2020)/Some Movie.en.srt", target="hu:forced:hi")
    response = world.call("get", "queue")
    (item,) = response.json["data"]
    assert item["media_title"] == "Some Movie.en.srt"
    assert item["target_language"] == "hu" and item["source_language"] is None
    assert item["media_type"] is None and item["media_id"] is None and item["engine_instance_id"] is None
    assert "/private/library" not in response.get_data(as_text=True)


def test_a_failing_in_memory_queue_does_not_fail_the_list(world):
    _job(world.session, "queued", queue_job_id=3)

    def broken(_id):
        raise RuntimeError("queue gone")

    world.summary = TranslationSummary(world.session, limits=world.limits, queue_counts=lambda: world.counts,
                                       queue_job=broken, translation_limit=lambda: 2, clock=lambda: MOMENT)
    assert world.call("get", "queue").json["total"] == 1


def test_the_queue_is_cut_at_fifty_and_says_how_many_there_are(world):
    for index in range(53):
        _job(world.session, "queued", path=f"/media/{index:02d}.en.srt")
    body = world.call("get", "queue").json
    assert len(body["data"]) == 50 and body["total"] == 53
    assert body["data"][0]["media_title"] == "00.en.srt"


def test_an_empty_queue_is_an_empty_list(world):
    assert world.call("get", "queue").json == {"data": [], "total": 0}


# ---------------------------------------------------------------------- config

def test_the_caps_are_server_owned_settings_with_no_cap_by_default():
    from app.config import SERVER_OWNED_SETTING_PATHS, settings

    assert {"translator.month_cap_micros", "translator.job_cap_micros",
            "translator.limits_revision"} <= SERVER_OWNED_SETTING_PATHS
    stored = ConfigLimits().get()
    assert stored.month_cap_micros is None and stored.job_cap_micros is None and stored.revision >= 1
    assert settings.translator.limits_revision >= 1


def test_a_config_write_that_fails_puts_the_live_values_back(monkeypatch):
    import app.config as config

    store = ConfigLimits()
    before = store.get()
    monkeypatch.setattr(config, "write_config", lambda *args, **kwargs: False)
    assert store.set(5_000_000, 1_000_000, before.revision + 1) is False
    assert store.get() == before

    monkeypatch.setattr(config, "write_config", lambda *args, **kwargs: True)
    assert store.set(5_000_000, None, before.revision + 1) is True
    assert store.get() == StoredLimits(5_000_000, None, before.revision + 1)
    store.set(None, None, before.revision)  # leave the live settings as they were


def test_the_translation_limit_is_the_concurrency_setting():
    from app.config import settings
    from subtitles.tools.translate.runtime.limits import translation_limit

    assert translation_limit() == settings.translator.openrouter_max_concurrent


def test_a_reader_waits_for_a_write_and_never_sees_values_a_failed_write_takes_back(monkeypatch):
    import threading

    import app.config as config
    from app.config import settings

    store = ConfigLimits()
    before = store.get()
    seen = {}

    def failing_write(*args, **kwargs):
        def read():
            seen["limits"] = store.get()

        reader = threading.Thread(target=read)
        reader.start()
        reader.join(0.3)
        seen["blocked"] = reader.is_alive()  # the caps are published, but the reader is still waiting
        seen["reader"] = reader
        return False

    monkeypatch.setattr(config, "write_config", failing_write)
    assert store.set(9_000_000, 3_000_000, before.revision + 1) is False
    seen["reader"].join(5)
    assert seen["blocked"] is True
    assert seen["limits"] == before
    assert store.get() == before
    assert settings.translator.month_cap_micros == before.month_cap_micros


def test_the_dispatcher_reads_the_caps_through_the_locked_reader_and_never_sees_a_failed_save(monkeypatch):
    import threading

    import app.config as config
    from subtitles.tools.translate.runtime import dispatch, limits

    store = ConfigLimits()
    before = store.get()
    seen = {}

    def failing_write(*args, **kwargs):
        def read():
            seen["caps"] = dispatch.configured_cost_limits()

        reader = threading.Thread(target=read)
        reader.start()
        reader.join(0.3)
        seen["blocked"] = reader.is_alive()
        seen["reader"] = reader
        return False

    monkeypatch.setattr(config, "write_config", failing_write)
    assert store.set(9_000_000, 3_000_000, before.revision + 1) is False
    seen["reader"].join(5)
    assert seen["blocked"] is True
    assert (seen["caps"].month_cap, seen["caps"].job_cap) == (before.month_cap_micros, before.job_cap_micros)
    assert seen["caps"] == limits.current_cost_limits()
