import time
import json
import sys
from pathlib import Path
from threading import Event, Thread
from types import ModuleType, SimpleNamespace

import pytest

from subtitles.tools.translate.services import gemini_translator


def _build_service():
    return gemini_translator.GeminiTranslatorService(
        source_srt_file="input.srt",
        dest_srt_file="output.srt",
        to_lang="eng",
        media_type="series",
        sonarr_series_id=1,
        sonarr_episode_id=1,
        radarr_id=1,
        forced=False,
        hi=False,
        video_path="/tmp/video.mkv",
        from_lang="en",
        orig_to_lang="eng",
    )


def test_get_batch_size_uses_configured_value(mocker):
    service = _build_service()
    mocker.patch.object(
        gemini_translator.settings.translator,
        "gemini_batch_size",
        450,
        create=True,
    )
    assert service._get_batch_size() == 450


def test_get_batch_size_falls_back_to_default_for_invalid_value(mocker):
    service = _build_service()
    mocker.patch.object(
        gemini_translator.settings.translator,
        "gemini_batch_size",
        "invalid",
        create=True,
    )
    assert service._get_batch_size() == gemini_translator.DEFAULT_GEMINI_BATCH_SIZE


def test_get_batch_size_is_clamped_to_minimum_of_one(mocker):
    service = _build_service()
    mocker.patch.object(
        gemini_translator.settings.translator,
        "gemini_batch_size",
        0,
        create=True,
    )
    assert service._get_batch_size() == 1


@pytest.fixture(autouse=True)
def _clear_gemini_key_cooldowns():
    cooldowns = getattr(gemini_translator, "_GEMINI_KEY_COOLDOWNS", None)
    if cooldowns is not None:
        cooldowns.clear()
    yield
    cooldowns = getattr(gemini_translator, "_GEMINI_KEY_COOLDOWNS", None)
    if cooldowns is not None:
        cooldowns.clear()


def test_get_configured_api_keys_trims_and_deduplicates(mocker):
    service = _build_service()
    mocker.patch.object(
        gemini_translator.settings.translator,
        "gemini_keys",
        [" key-1 ", "", "key-2", "key-1"],
        create=True,
    )

    assert service._get_configured_api_keys() == ["key-1", "key-2"]


def test_get_configured_api_keys_returns_empty_when_no_keys(mocker):
    service = _build_service()
    mocker.patch.object(
        gemini_translator.settings.translator,
        "gemini_keys",
        [],
        create=True,
    )

    assert service._get_configured_api_keys() == []


def test_select_next_api_key_skips_keys_on_cooldown():
    service = _build_service()
    service.api_keys = ["key-1", "key-2"]
    service.current_api_index = -1
    gemini_translator._GEMINI_KEY_COOLDOWNS["key-1"] = time.time() + 60

    selected_key = service._select_next_api_key()

    assert selected_key == "key-2"


def test_handle_rate_limited_key_applies_cooldown_and_rotates_key():
    class _RateLimitedResponse:
        status_code = 429
        headers = {"Retry-After": "7"}

    service = _build_service()
    service.api_keys = ["key-1", "key-2"]
    service.current_api_index = 0
    service.current_api_key = "key-1"

    service._handle_rate_limited_key(_RateLimitedResponse())

    assert gemini_translator._GEMINI_KEY_COOLDOWNS["key-1"] > time.time() + 6
    assert service.current_api_key == "key-2"


def test_handle_rate_limited_key_raises_when_all_keys_unavailable():
    class _RateLimitedResponse:
        status_code = 429
        headers = {"Retry-After": "3"}

    service = _build_service()
    service.api_keys = ["key-1"]
    service.current_api_index = 0
    service.current_api_key = "key-1"

    with pytest.raises(RuntimeError, match="All Gemini API keys are currently rate limited"):
        service._handle_rate_limited_key(_RateLimitedResponse())


def test_translate_with_gemini_does_not_leave_output_file_on_failure(tmp_path, mocker):
    input_file = tmp_path / "input.srt"
    output_file = tmp_path / "output.srt"
    input_file.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n",
        encoding="utf-8",
    )

    service = _build_service()
    service.input_file = str(input_file)
    service.output_file = str(output_file)
    service.api_keys = ["key-1"]
    service.current_api_key = "key-1"
    service.target_language = "English"
    service.batch_size = 1
    service.job_id = "job-1"

    def _fail_after_output_file_created(*args, **kwargs):
        # Translation output file should be opened before batch processing starts.
        assert output_file.exists()
        raise RuntimeError("boom")

    mocker.patch.object(service, "_process_batch", side_effect=_fail_after_output_file_created)
    mocker.patch.object(gemini_translator.jobs_queue, "update_job_progress")

    with pytest.raises(RuntimeError, match="boom"):
        service._translate_with_gemini()

    assert not output_file.exists()


@pytest.fixture
def queued_gemini(tmp_path, monkeypatch):
    from app import jobs_queue as queue_module

    monkeypatch.setattr(queue_module.JobsQueue, "_flush_progress_loop", lambda self: None)
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    queue = queue_module.JobsQueue()
    monkeypatch.setattr(gemini_translator, "jobs_queue", queue)
    source = tmp_path / "Video.en.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nSource text\n", encoding="utf-8")
    destination = tmp_path / "Video.hu.srt"
    destination.write_text("Previous destination", encoding="utf-8")
    video = tmp_path / "Video.mkv"
    video.touch()
    service = gemini_translator.GeminiTranslatorService(
        str(source), str(destination), "hun", "movie", None, None, 7, False, False,
        str(video), "en", "hu", arr_instance_id=2)
    monkeypatch.setattr(service, "_get_configured_api_keys", lambda: ["fixture-one", "fixture-two"])
    monkeypatch.setattr(service, "_get_batch_size", lambda: 1)
    monkeypatch.setattr(gemini_translator, "get_description", lambda *args, **kwargs: "")
    monkeypatch.setattr(gemini_translator, "language_from_alpha2", lambda value: "English")
    monkeypatch.setattr(gemini_translator, "language_from_alpha3", lambda value: "Hungarian")
    monkeypatch.setattr(gemini_translator, "create_process_result", lambda *args, **kwargs: None)
    history = []
    monkeypatch.setattr(gemini_translator, "history_log_movie", lambda **kwargs: history.append(kwargs))
    dispatch = ModuleType("gemini_cancellation_fixture")
    dispatch.translate = service.translate
    monkeypatch.setitem(sys.modules, dispatch.__name__, dispatch)
    queue.feed_jobs_pending_queue("Translation", dispatch.__name__, "translate", is_progress=True)
    job = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(job)
    worker = Thread(target=queue._run_job, args=(job,))
    release = Event()
    fixture = SimpleNamespace(queue=queue, job=job, service=service, source=source, destination=destination,
                              worker=worker, entered=Event(), release=release, calls=[], history=history)
    yield fixture
    release.set()
    if worker.ident:
        worker.join(3)
        assert not worker.is_alive()


def _gemini_response(status=200):
    response = gemini_translator.requests.Response()
    response.status_code = status
    response.headers["Retry-After"] = "7"
    content = ({"candidates": [{"content": {"parts": [{"text": json.dumps([
        {"index": "0", "content": "Translated text"}])}]}}]} if status == 200 else
               {"error": {"status": "RESOURCE_EXHAUSTED"}})
    response._content = json.dumps(content).encode()
    return response


def _record_cancellation_state(fixture, record_property):
    record_property("transport_calls", len(fixture.calls))
    record_property("progress_files_remaining", len(list(fixture.source.parent.glob("*.progress"))))
    record_property("temporary_files_remaining", len(list(fixture.source.parent.glob(".bazarr-write-*"))))
    record_property("job_cancelled", fixture.job.cancelled)
    assert fixture.job.cancelled
    assert fixture.job.status == "completed"
    assert fixture.job.progress_message == "Cancelled by user"
    assert fixture.destination.read_text() == "Previous destination"
    assert "Source text" in fixture.source.read_text()
    assert not fixture.history
    assert not list(fixture.source.parent.glob(".bazarr-write-*"))
    assert not list(fixture.source.parent.glob("*.progress"))


@pytest.mark.parametrize("response_kind", ["success", "transient-error", "rate-limit"])
def test_cancelled_response_does_not_retry_translation(queued_gemini, monkeypatch, record_property, response_kind):
    fixture = queued_gemini

    def transport(*args, **kwargs):
        fixture.calls.append(True)
        fixture.entered.set()
        assert fixture.release.wait(3)
        if response_kind == "transient-error":
            raise gemini_translator.requests.ConnectionError("synthetic transport failure")
        return _gemini_response(429 if response_kind == "rate-limit" else 200)

    monkeypatch.setattr(gemini_translator.requests, "request", transport)
    fixture.worker.start()
    assert fixture.entered.wait(3)
    assert fixture.queue.cancel_running_job(fixture.job.job_id)
    fixture.release.set()
    fixture.worker.join(3)
    assert not fixture.worker.is_alive()
    _record_cancellation_state(fixture, record_property)
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("phase", ["initial-progress", "final-progress"])
def test_cancellation_at_progress_boundary_clears_progress(queued_gemini, monkeypatch, record_property, phase):
    fixture = queued_gemini
    original = fixture.queue.update_job_progress

    def progress(*args, **kwargs):
        if ((phase == "initial-progress" and "progress_max" in kwargs) or
                (phase == "final-progress" and "progress_value" in kwargs
                 and kwargs.get("progress_message") == str(fixture.source))):
            assert Path(fixture.service.progress_file).is_file()
            assert fixture.queue.cancel_running_job(fixture.job.job_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(fixture.queue, "update_job_progress", progress)
    monkeypatch.setattr(gemini_translator.requests, "request",
                        lambda *args, **kwargs: fixture.calls.append(True) or _gemini_response())
    fixture.worker.start()
    fixture.worker.join(3)
    assert not fixture.worker.is_alive()
    _record_cancellation_state(fixture, record_property)
    assert len(fixture.calls) == (0 if phase == "initial-progress" else 1)


def test_already_cancelled_batch_does_not_send_request(queued_gemini, monkeypatch, record_property):
    from app.jobs_queue import JobCancelled

    fixture = queued_gemini
    service = fixture.service
    service.job_id = fixture.job.job_id
    service.current_api_key = "fixture"
    service.target_language = "Hungarian"
    assert fixture.queue.cancel_running_job(fixture.job.job_id)
    monkeypatch.setattr(gemini_translator.requests, "request",
                        lambda *args, **kwargs: fixture.calls.append(True) or _gemini_response())
    batch = [{"index": "0", "content": "Source text"}]
    translated = list(gemini_translator.srt.parse(fixture.source.read_text()))
    with pytest.raises(JobCancelled):
        service._process_batch(batch, translated, 1)
    record_property("transport_calls", len(fixture.calls))
    assert not fixture.calls


@pytest.mark.parametrize("failure", ["transient", "rate-limit", "exhausted"])
def test_ordinary_request_retries_keep_existing_behavior(queued_gemini, monkeypatch, failure):
    fixture = queued_gemini
    keys = []

    def transport(*args, **kwargs):
        fixture.calls.append(True)
        keys.append(fixture.service.current_api_key)
        if failure == "exhausted" or (failure == "transient" and len(fixture.calls) == 1):
            raise gemini_translator.requests.ConnectionError("synthetic transport failure")
        return _gemini_response(429 if failure == "rate-limit" and len(fixture.calls) == 1 else 200)

    monkeypatch.setattr(gemini_translator.requests, "request", transport)
    fixture.worker.start()
    fixture.worker.join(3)
    assert not fixture.worker.is_alive()
    assert len(fixture.calls) == (4 if failure == "exhausted" else 2)
    assert not fixture.job.cancelled
    assert fixture.job.status == ("failed" if failure == "exhausted" else "completed")
    assert len(fixture.history) == (0 if failure == "exhausted" else 1)
    if failure == "exhausted":
        assert fixture.destination.read_text() == "Previous destination"
    else:
        assert "Translated text" in fixture.destination.read_text()
    assert "Source text" in fixture.source.read_text()
    assert not list(fixture.source.parent.glob("*.progress"))
    assert not list(fixture.source.parent.glob(".bazarr-write-*"))
    if failure == "rate-limit":
        assert keys[0] != keys[1]
