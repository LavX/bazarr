import logging
from pathlib import Path

import pytest

from subtitles.tools.translate.services import openrouter_translator


def _build_service(source_srt_file="input.srt", dest_srt_file="output.srt", media_type="episode"):
    return openrouter_translator.OpenRouterTranslatorService(
        source_srt_file=source_srt_file,
        dest_srt_file=dest_srt_file,
        lang_obj=None,
        to_lang="hun",
        from_lang="en",
        media_type=media_type,
        video_path="/tmp/video.mkv",
        orig_to_lang="hu",
        forced=False,
        hi=False,
        sonarr_series_id=1,
        sonarr_episode_id=2,
        radarr_id=3,
    )


@pytest.fixture(autouse=True)
def _mock_dependencies(mocker):
    mocker.patch.object(openrouter_translator.requests, "get", return_value=mocker.Mock(status_code=200))
    mocker.patch.object(openrouter_translator.time, "sleep")
    for name in (
        "show_progress", "hide_progress", "show_message", "jobs_queue", "history_log", "history_log_movie",
        "add_translator_info", "create_process_result",
    ):
        mocker.patch.object(openrouter_translator, name)


@pytest.mark.parametrize("structured", [True, False])
def test_poll_job_keeps_partial_lines_and_bounded_error(structured, caplog):
    service = _build_service()
    lines = [{"position": 0, "line": "Szia"}, {"position": 1, "line": "Source text"}]
    error = "1 of 58 batches failed: " + "Failed to parse JSON. " * 100
    detail = error.strip()[:500]
    result = {"lines": lines, "model_used": "google/gemini-2.5-flash-lite", "tokens_used": 100} if structured else lines
    openrouter_translator.requests.get.return_value.json.return_value = {
        "status": "partial", "result": result, "error": error,
    }

    with caplog.at_level(logging.WARNING, logger=openrouter_translator.__name__):
        assert service._poll_job("http://translator", "job-1", 2, bazarr_job_id="bazarr-job-1") == lines

    assert service.partial_error == detail
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_called_once_with(
        f"Translation is partial. Some lines may remain in the source language. {detail}"
    )
    assert (openrouter_translator.__name__, logging.WARNING,
            f"Translation partially completed: {detail}") in caplog.record_tuples
    openrouter_translator.time.sleep.assert_not_called()


@pytest.mark.parametrize("result", [None, {}, [], {"lines": []}, {"lines": "invalid"}, "invalid", 42])
def test_poll_job_rejects_partial_without_lines(result, caplog):
    service = _build_service()
    error = "No lines translated"
    openrouter_translator.requests.get.return_value.json.return_value = {
        "status": "partial", "result": result, "error": error,
    }

    assert service._poll_job("http://translator", "job-1", 1) is None
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_called_once_with(f"Translation failed (partial): {error}")
    assert (openrouter_translator.__name__, logging.ERROR,
            f"Translation partially failed: {error}") in caplog.record_tuples


@pytest.mark.parametrize("structured", [True, False])
def test_poll_job_returns_completed_lines(structured):
    service = _build_service()
    lines = [{"position": 0, "line": "Szia"}]
    result = {"lines": lines, "model_used": "google/gemini-2.5-flash-lite", "tokens_used": 100} if structured else lines
    openrouter_translator.requests.get.return_value.json.return_value = {"status": "completed", "result": result}

    assert service._poll_job("http://translator", "job-1", 1) == lines
    assert service.partial_error is None
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_not_called()


@pytest.mark.parametrize("result", [None, [], {"lines": []}, {"lines": "invalid"}, {"model_used": "model"}, "invalid", 42])
def test_poll_job_rejects_completed_without_lines(result):
    service = _build_service()
    openrouter_translator.requests.get.return_value.json.return_value = {"status": "completed", "result": result}

    assert service._poll_job("http://translator", "job-1", 1) is None


@pytest.mark.parametrize("media_type", ["episode", "movie"])
def test_translate_saves_partial_subtitles_and_marks_history(tmp_path, mocker, media_type):
    source = tmp_path / "input.srt"
    destination = tmp_path / "output.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nSource text\n\n",
        encoding="utf-8",
    )
    service = _build_service(str(source), str(destination), media_type)
    error = "1 of 2 batches failed: " + "Failed to parse JSON. " * 15

    def _partial_result(lines_list, bazarr_job_id=None):
        assert lines_list == ["Hello", "Source text"]
        assert bazarr_job_id == "bazarr-job-1"
        service._mark_partial(error)
        return [{"position": 0, "line": "Szia"}, {"position": 1, "line": "Source text"}]

    mocker.patch.object(service, "_submit_and_poll", side_effect=_partial_result)
    mocker.patch.object(openrouter_translator, "language_from_alpha2", return_value="English")
    mocker.patch.object(openrouter_translator, "language_from_alpha3", return_value="Hungarian")

    assert service.translate(job_id="bazarr-job-1") == str(destination)
    assert destination.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:02,000\nSzia\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nSource text\n\n"
    )
    openrouter_translator.create_process_result.assert_called_once()
    history_message = openrouter_translator.create_process_result.call_args.args[0]
    assert "partially translated" in history_message
    assert history_message.endswith(error.strip()[:500])
    openrouter_translator.add_translator_info.assert_called_once()
    staged, footer = openrouter_translator.add_translator_info.call_args.args
    assert Path(staged).parent == destination.parent
    assert Path(staged).name.startswith(".bazarr-translate-")
    assert footer == "# Subtitles partially translated with AI Subtitle Translator #"
    result = openrouter_translator.create_process_result.return_value
    if media_type == "episode":
        openrouter_translator.history_log.assert_called_once_with(
            action=6, sonarr_series_id=1, sonarr_episode_id=2, result=result,
        )
        openrouter_translator.history_log_movie.assert_not_called()
    else:
        openrouter_translator.history_log_movie.assert_called_once_with(action=6, radarr_id=3, result=result)
        openrouter_translator.history_log.assert_not_called()


class _FakeClock:
    def __init__(self):
        self.elapsed = 0

    def monotonic(self):
        return 1000 + self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


@pytest.fixture
def poll_clock(mocker, _mock_dependencies):
    clock = _FakeClock()
    mocker.patch.object(openrouter_translator.time, "monotonic", side_effect=clock.monotonic)
    openrouter_translator.time.sleep.side_effect = clock.advance
    return clock


@pytest.mark.parametrize("status", ["processing", "queued"])
def test_poll_job_completes_after_more_than_thirty_minutes(poll_clock, mocker, status):
    service = _build_service()
    lines = [{"position": 0, "line": "Szia"}]
    response = mocker.Mock(status_code=200)

    def _get_status(*args, **kwargs):
        if poll_clock.elapsed < 40 * 60:
            response.json.return_value = {"status": status}
        else:
            response.json.return_value = {"status": "completed", "result": {"lines": lines}}
        return response

    openrouter_translator.requests.get.side_effect = _get_status

    assert service._poll_job("http://translator", "job-1", 1) == lines
    assert poll_clock.elapsed == 40 * 60
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_not_called()


@pytest.mark.parametrize("failure", ["http_500", "request_exception"])
def test_poll_job_stops_when_status_endpoint_is_unreachable(poll_clock, mocker, caplog, failure):
    service = _build_service()
    response = mocker.Mock(status_code=500)

    def _get_status(*args, **kwargs):
        poll_clock.advance(8)
        if failure == "request_exception":
            raise openrouter_translator.requests.exceptions.RequestException("Service unavailable")
        return response

    openrouter_translator.requests.get.side_effect = _get_status

    assert service._poll_job("http://translator", "job-1", 1) is None
    assert poll_clock.elapsed == 10 * 60
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_called_once()
    assert any(
        record.levelno == logging.ERROR
        and "job-1" in record.message
        and "unreachable for 10 minutes" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("failure", ["http_500", "request_exception"])
def test_poll_job_resets_unreachable_clock_after_http_200(poll_clock, mocker, failure):
    service = _build_service()
    lines = [{"position": 0, "line": "Szia"}]
    response = mocker.Mock(status_code=200)
    failed_response = mocker.Mock(status_code=500)

    def _get_status(*args, **kwargs):
        if poll_clock.elapsed == 8 * 60:
            response.json.return_value = {"status": "processing"}
            return response
        if poll_clock.elapsed >= 16 * 60:
            response.json.return_value = {"status": "completed", "result": {"lines": lines}}
            return response
        if failure == "request_exception":
            raise openrouter_translator.requests.exceptions.RequestException("Service unavailable")
        return failed_response

    openrouter_translator.requests.get.side_effect = _get_status

    assert service._poll_job("http://translator", "job-1", 1) == lines
    assert poll_clock.elapsed == 16 * 60
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_not_called()


def test_poll_job_stops_at_twelve_hour_hard_cap(poll_clock, caplog):
    service = _build_service()
    openrouter_translator.requests.get.return_value.json.return_value = {"status": "processing"}
    openrouter_translator.time.sleep.side_effect = lambda seconds: poll_clock.advance(5 * 60)

    assert service._poll_job("http://translator", "job-1", 1) is None
    assert poll_clock.elapsed == 12 * 3600
    openrouter_translator.hide_progress.assert_called_once_with(id="translate_progress_output.srt")
    openrouter_translator.show_message.assert_called_once()
    assert any(
        record.levelno == logging.ERROR
        and "job-1" in record.message
        and "hard cap" in record.message
        for record in caplog.records
    )
