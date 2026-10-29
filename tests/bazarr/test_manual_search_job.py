# coding=utf-8
"""A manual search runs as a standard job and publishes its rows as the job's result."""

from types import SimpleNamespace

import pytest
from flask import Flask

import app.database  # noqa: F401


def _rows():
    # What manual_search hands back: a bool for original_format, a float score.
    return [dict(score=87.6, orig_score=300, score_without_hash=290, forced="False", language="English",
                 hearing_impaired="False", provider="opensubtitles", subtitle="abc-123", url="http://x/1",
                 original_format=False, matches=["series"], dont_matches=["year"], release_info=["Show.S01E01"],
                 uploader=None)]


@pytest.fixture
def real_queue(monkeypatch):
    from app import jobs_queue as queue_module
    from subtitles import manual

    monkeypatch.setattr(queue_module.JobsQueue, "_flush_progress_loop", lambda self: None)
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    queue = queue_module.JobsQueue()
    monkeypatch.setattr(manual, "jobs_queue", queue)
    monkeypatch.setattr("app.get_providers.get_providers_sorted", lambda: ["opensubtitles"])

    def run(**overrides):
        kwargs = {"path": "/tv/Show/e1.mkv", "profile_id": 1, "scene_name": "None", "title": "Show",
                  "media_type": "series", "arr_instance_id": 7, **overrides}
        job_id = queue.feed_jobs_pending_queue(job_name="Search", module="subtitles.manual",
                                               func="run_manual_search_job", kwargs=kwargs, is_progress=True,
                                               progress_max=2)
        job = next(item for item in queue.jobs_pending_queue if item.job_id == job_id)
        queue.jobs_pending_queue.remove(job)
        queue.jobs_running_queue.append(job)
        queue._run_job(job)
        return job

    return SimpleNamespace(queue=queue, run=run, manual=manual)


def test_the_job_publishes_the_rows_as_the_api_sends_them(real_queue, monkeypatch):
    calls = []
    monkeypatch.setattr(real_queue.manual, "manual_search",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or _rows())

    job = real_queue.run()

    assert job.status == "completed"
    outcome = real_queue.queue.get_job_result(job.job_id)
    row = outcome["result"]["data"][0]
    # Marshalled like the GET: strings and integers, not the raw bool and float.
    assert row["original_format"] == "False"
    assert row["score"] == 87
    assert row["subtitle"] == "abc-123"
    # The search got what the request resolved, and the instance it belongs to.
    args, kwargs = calls[0]
    assert args[0] == "/tv/Show/e1.mkv" and args[2] == ["opensubtitles"] and args[5] == "series"
    # The queue observes the job, so the search does not observe itself again.
    assert {key: kwargs[key] for key in ("arr_instance_id", "observed")} == {"arr_instance_id": 7, "observed": False}
    # Stop is read while the providers answer, not only once the last one has.
    assert kwargs["cancel"].is_set() is False
    assert job.progress_message == "1 subtitle(s) found"


def test_an_empty_search_is_a_finished_job_with_an_empty_result(real_queue, monkeypatch):
    monkeypatch.setattr(real_queue.manual, "manual_search", lambda *args, **kwargs: [])

    job = real_queue.run()

    assert job.status == "completed"
    # An empty list is an answer; only a job that published nothing has None.
    assert real_queue.queue.get_job_result(job.job_id)["result"]["data"] == []


def test_a_search_that_cannot_run_fails_the_job_with_its_reason(real_queue, monkeypatch, caplog):
    monkeypatch.setattr(real_queue.manual, "manual_search", lambda *args, **kwargs: "All providers are throttled")

    with caplog.at_level("WARNING"):
        job = real_queue.run()

    assert job.status == "failed"
    assert job.error == {"reason": "providers_unavailable", "message": "All providers are throttled"}
    assert job.retryable is False
    assert real_queue.queue.get_job_result(job.job_id)["result"] is None
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "reason=providers_unavailable" in warnings[0].getMessage()


def test_a_stop_before_the_rows_are_published_ends_the_job_cancelled(real_queue, monkeypatch):
    def search(*args, **kwargs):
        # Stop is pressed while the providers are being listed.
        real_queue.queue.cancel_running_job(real_queue.queue.jobs_running_queue[0].job_id)
        return _rows()

    monkeypatch.setattr(real_queue.manual, "manual_search", search)

    job = real_queue.run()

    assert job.status == "cancelled"
    assert real_queue.queue.get_job_result(job.job_id)["result"] is None


def test_pressing_search_again_follows_the_running_search_and_starts_it_at_once(real_queue, monkeypatch):
    started = []
    monkeypatch.setattr(real_queue.queue, "force_start_pending_job",
                        lambda job_id, user_start=False: started.append((job_id, user_start)) or True)

    class Immediate:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(real_queue.manual, "Thread", Immediate)

    first = real_queue.manual.queue_manual_search("/tv/e1.mkv", 1, "None", "Show", "series", "Show - S01E01",
                                                  arr_instance_id=7)
    again = real_queue.manual.queue_manual_search("/tv/e1.mkv", 1, "None", "Show", "series", "Show - S01E01",
                                                  arr_instance_id=7)

    assert first == again
    assert started == [(first, True), (first, True)]
    job = real_queue.queue.jobs_pending_queue[0]
    assert job.job_name == "Searching subtitles for Show - S01E01"
    assert job.retryable is False and job.is_progress is True
    # A person is waiting on it, so it is forced to start, within the lane that bounds those.
    assert job.lane == "user_search"


def test_the_episode_route_queues_the_search_and_names_the_session(schema_session, tmp_path, monkeypatch):
    from api.providers import providers_episodes
    from app.database import TableEpisodes, TableShows
    from app.process_session import PROCESS_SESSION

    episode_path = tmp_path / "episode.mkv"
    episode_path.write_text("", encoding="utf-8")
    schema_session.add(TableShows(id=901, sonarrSeriesId=1, arr_instance_id=8, path=str(tmp_path), title="Show",
                                  tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=902, series_id=901, sonarrSeriesId=1, sonarrEpisodeId=2, arr_instance_id=8,
                                     path=str(episode_path), title="Pilot", season=1, episode=1, subtitles="[]",
                                     missing_subtitles="[]", sceneName=None))
    schema_session.flush()
    monkeypatch.setattr(providers_episodes, "database", schema_session)
    monkeypatch.setattr(providers_episodes.path_mappings, "path_replace", lambda value: value)
    queued = []
    monkeypatch.setattr(providers_episodes, "queue_manual_search",
                        lambda *args, **kwargs: queued.append((args, kwargs)) or 41)

    app = Flask(__name__)
    with app.test_request_context("/api/providers/episodes/search", method="POST", data={"episodeid": 902}):
        body, status = providers_episodes.ProviderEpisodesSearch.post.__wrapped__(
            providers_episodes.ProviderEpisodesSearch())

    assert status == 202
    assert body == {"job_id": 41, "session": PROCESS_SESSION}
    args, kwargs = queued[0]
    assert args == (str(episode_path), None, "None", "Show", "series", "Show - S01E01")
    assert kwargs == {"arr_instance_id": 8}


def test_the_movie_route_queues_the_search_and_a_missing_movie_is_a_404(schema_session, tmp_path, monkeypatch):
    from api.providers import providers_movies
    from app.database import TableMovies

    movie_path = tmp_path / "movie.mkv"
    movie_path.write_text("", encoding="utf-8")
    schema_session.add(TableMovies(id=801, radarrId=1, arr_instance_id=7, path=str(movie_path), title="Movie",
                                   year="1999", tmdbId="801", subtitles="[]", missing_subtitles="[]"))
    schema_session.flush()
    monkeypatch.setattr(providers_movies, "database", schema_session)
    monkeypatch.setattr(providers_movies.path_mappings, "path_replace_movie", lambda value: value)
    queued = []
    monkeypatch.setattr(providers_movies, "queue_manual_search",
                        lambda *args, **kwargs: queued.append((args, kwargs)) or 5)

    app = Flask(__name__)
    resource = providers_movies.ProviderMoviesSearch
    with app.test_request_context("/api/providers/movies/search", method="POST", data={"radarrid": 801}):
        body, status = resource.post.__wrapped__(resource())
    assert (status, body["job_id"]) == (202, 5)
    assert queued[0][0][4:] == ("movie", "Movie (1999)")

    with app.test_request_context("/api/providers/movies/search", method="POST", data={"radarrid": 999}):
        assert resource.post.__wrapped__(resource()) == ("Movie not found", 404)
    assert len(queued) == 1


def test_a_finished_search_keeps_its_rows_only_while_it_is_among_the_most_recent(real_queue, monkeypatch):
    monkeypatch.setattr(real_queue.manual, "manual_search", lambda *args, **kwargs: _rows())

    jobs = [real_queue.run(path=f"/tv/Show/e{number}.mkv") for number in range(12)]

    kept = [job.job_id for job in jobs if real_queue.queue.get_job_result(job.job_id)["result"] is not None]
    assert kept == [job.job_id for job in jobs[2:]]
    # The two oldest are still in the list, with the status they ended in.
    assert real_queue.queue.get_job_result(jobs[0].job_id)["status"] == "completed"


def _slow_providers(monkeypatch, real_queue, durations, asked):
    """The real library search with a real pool whose providers take as long as ``durations`` says."""
    import time

    from subliminal_patch.core import SZProviderPool
    from subzero.language import Language

    def list_provider(self, name, video, languages, **kwargs):
        asked.append(name)
        time.sleep(durations[name])
        return []

    monkeypatch.setattr(SZProviderPool, "list_subtitles_provider", list_provider)
    pool = SZProviderPool(providers=list(durations))
    manual = real_queue.manual
    monkeypatch.setattr(manual, "_get_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(manual, "get_video", lambda *args, **kwargs: SimpleNamespace(subtitle_languages=set()))
    monkeypatch.setattr(manual, "_get_language_obj", lambda profile_id: ({Language("eng")}, False))
    monkeypatch.setattr("app.get_providers.get_providers_sorted", lambda include_throttled=False: list(durations))
    monkeypatch.setattr(manual, "manual_search",
                        lambda *args, **kwargs: manual._manual_search(*args, **{k: v for k, v in kwargs.items()
                                                                                if k != "observed"}))
    return pool


def _run_in_thread(real_queue):
    import threading

    box = {}

    def target():
        box["job"] = real_queue.run()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _wait_until(condition, timeout=5):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_stop_ends_a_library_search_while_a_provider_is_still_answering(real_queue, monkeypatch):
    """Five searches told to stop kept running for minutes: the stop was read once the serial provider loop
    returned, and the providers answer one after another, several of them timing out."""
    import threading
    import time

    asked = []
    _slow_providers(monkeypatch, real_queue, {"slow": 3.0, "later": 0.01, "last": 0.01}, asked)

    thread, box = _run_in_thread(real_queue)
    assert _wait_until(lambda: asked == ["slow"])
    stopped_at = time.monotonic()
    assert real_queue.queue.cancel_running_job(real_queue.queue.jobs_running_queue[0].job_id)
    thread.join(5)
    elapsed = time.monotonic() - stopped_at

    assert not thread.is_alive()
    assert elapsed < 1.5, "the stop waited for the provider"
    job = box["job"]
    assert job.status == "cancelled"
    assert asked == ["slow"], "a provider after the stop was asked"
    assert real_queue.queue.get_job_result(job.job_id)["result"] is None
    assert not real_queue.queue.jobs_running_queue
    # Only the one request that was in flight is left, and it goes away with the provider's answer.
    leftovers = [item for item in threading.enumerate() if item.name.startswith("listing-")]
    assert [item.name for item in leftovers] == ["listing-slow"]
    leftovers[0].join(5)
    assert not leftovers[0].is_alive()


def test_stop_between_two_providers_asks_no_further_provider(real_queue, monkeypatch):
    asked = []
    _slow_providers(monkeypatch, real_queue, {"first": 0.4, "second": 0.01, "third": 0.01}, asked)

    thread, box = _run_in_thread(real_queue)
    assert _wait_until(lambda: asked == ["first"])
    real_queue.queue.cancel_running_job(real_queue.queue.jobs_running_queue[0].job_id)
    thread.join(5)

    assert box["job"].status == "cancelled"
    assert asked == ["first"]


def test_a_stopped_search_frees_its_lane_slot_for_the_search_waiting_on_it(real_queue, monkeypatch):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 8)
    asked = []
    _slow_providers(monkeypatch, real_queue, {"slow": 3.0}, asked)
    queue = real_queue.queue
    cap = LANE_LIMITS[USER_SEARCH_LANE]
    ids = [queue.feed_jobs_pending_queue(job_name=f"Search {index}", module="subtitles.manual",
                                         func="run_manual_search_job", is_progress=True, progress_max=2,
                                         kwargs={"path": f"/m/{index}.mkv", "profile_id": 1, "scene_name": "None",
                                                 "title": f"M{index}", "media_type": "movie"},
                                         lane=USER_SEARCH_LANE) for index in range(cap + 1)]
    started = [queue.force_start_pending_job(job_id, True) for job_id in ids]
    assert started == [True] * cap + [False]
    assert _wait_until(lambda: len(asked) == cap)

    assert queue.cancel_running_job(ids[0])
    assert _wait_until(lambda: not any(job.job_id == ids[0] for job in queue.jobs_running_queue), timeout=3)
    assert queue.force_start_pending_job(ids[-1], True) is True
    for job_id in ids[1:]:
        queue.cancel_running_job(job_id)
    assert _wait_until(lambda: not queue.jobs_running_queue, timeout=5)


def _error_records(caplog):
    import logging

    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_a_stopped_search_is_not_logged_as_an_error(real_queue, monkeypatch, caplog):
    """Stop is not a failure. Without the unwrap of the listing's stop, the generic handler below it logs
    "Error trying to get Subtitle list" with a traceback for every Stop a person presses."""
    asked = []
    _slow_providers(monkeypatch, real_queue, {"first": 0.4, "second": 0.01}, asked)

    with caplog.at_level("DEBUG"):
        thread, box = _run_in_thread(real_queue)
        assert _wait_until(lambda: asked == ["first"])
        real_queue.queue.cancel_running_job(real_queue.queue.jobs_running_queue[0].job_id)
        thread.join(5)

    assert box["job"].status == "cancelled"
    assert _error_records(caplog) == []


def test_a_signal_that_only_says_stop_ends_the_search_quietly(real_queue, monkeypatch, caplog):
    """The sports search's stop reads as True from ``is_set`` and raises nothing: the search ends empty."""
    import time

    asked = []
    _slow_providers(monkeypatch, real_queue, {"first": 0.5, "second": 0.01}, asked)
    signal = SimpleNamespace(is_set=lambda: bool(asked))

    started = time.monotonic()
    with caplog.at_level("DEBUG"):
        out = real_queue.manual._manual_search("/m.mkv", 1, ["first", "second"], "None", "M", "movie",
                                               cancel=signal)

    assert out == []
    assert time.monotonic() - started < 1.0
    assert asked == ["first"]
    assert _error_records(caplog) == []


# The sports search stops through its own signal. Reading that signal as often as the listing polls used to
# publish a progress event and, once a second, read the owner: heavy for a stop check, and one failed read
# ended the search as if Stop had been pressed.

@pytest.fixture
def sports_signal(real_queue, monkeypatch):
    """A real sports signal on a running job of the real queue, counting the progress it publishes."""
    from sportarr import workflows

    queue = real_queue.queue
    monkeypatch.setattr(workflows, "jobs_queue", queue)
    job_id = queue.feed_jobs_pending_queue(job_name="Sports search", module="sportarr.manual_jobs",
                                           func="run_sports_manual_search_job", kwargs={"event": 1},
                                           is_progress=True, progress_max=2)
    job = next(item for item in queue.jobs_pending_queue if item.job_id == job_id)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)
    published = []
    original = queue.update_job_progress
    monkeypatch.setattr(queue, "update_job_progress",
                        lambda *args, **kwargs: published.append(args) or original(*args, **kwargs))
    monkeypatch.setattr(workflows.SportsJobSignal, "OWNER_CACHE_SECONDS", 0.05)
    signal = workflows.SportsJobSignal(1, job_id)
    owner = {"enabled": True, "failures": 0}

    def owner_row(self):
        if owner["failures"]:
            owner["failures"] -= 1
            raise OSError("database is locked")
        return owner["enabled"]

    monkeypatch.setattr(workflows.SportsJobSignal, "_owner_is_enabled", owner_row)
    return SimpleNamespace(signal=signal, job_id=job_id, queue=queue, published=published, owner=owner)


def test_the_sports_stop_check_publishes_no_progress(real_queue, sports_signal, monkeypatch):
    asked = []
    _slow_providers(monkeypatch, real_queue, {"first": 0.7, "second": 0.01}, asked)

    try:
        real_queue.manual._manual_search("/m.mkv", 1, ["first", "second"], "None", "M", "movie",
                                         cancel=sports_signal.signal)
    except Exception:  # the cache is not configured in a test; the listing, which is what is measured, ran
        pass

    assert asked == ["first", "second"]
    assert sports_signal.published == [], "the stop check published progress"


def test_a_failed_read_of_the_sports_signal_does_not_end_the_search(real_queue, sports_signal, monkeypatch):
    asked = []
    _slow_providers(monkeypatch, real_queue, {"first": 0.5, "second": 0.01}, asked)
    sports_signal.owner["failures"] = 2

    try:
        real_queue.manual._manual_search("/m.mkv", 1, ["first", "second"], "None", "M", "movie",
                                         cancel=sports_signal.signal)
    except Exception:  # see above
        pass

    assert sports_signal.owner["failures"] == 0, "the owner was not read again"
    assert asked == ["first", "second"]


def test_stop_ends_a_sports_search_while_a_provider_is_still_answering(real_queue, sports_signal, monkeypatch):
    import time
    from app.jobs_queue import JobCancelled

    asked = []
    _slow_providers(monkeypatch, real_queue, {"slow": 3.0, "later": 0.01}, asked)
    timer = __import__("threading").Timer(0.3, lambda: sports_signal.queue.cancel_running_job(sports_signal.job_id))
    timer.start()

    started = time.monotonic()
    with pytest.raises(JobCancelled):
        real_queue.manual._manual_search("/m.mkv", 1, ["slow", "later"], "None", "M", "movie",
                                         cancel=sports_signal.signal)

    assert time.monotonic() - started < 1.5
    assert asked == ["slow"]


def test_an_instance_turned_off_ends_a_sports_search(real_queue, sports_signal, monkeypatch):
    import time

    asked = []
    _slow_providers(monkeypatch, real_queue, {"slow": 3.0, "later": 0.01}, asked)
    timer = __import__("threading").Timer(0.3, lambda: sports_signal.owner.update(enabled=False))
    timer.start()

    started = time.monotonic()
    out = real_queue.manual._manual_search("/m.mkv", 1, ["slow", "later"], "None", "M", "movie",
                                           cancel=sports_signal.signal)

    assert out == []
    assert time.monotonic() - started < 1.5
    assert asked == ["slow"]
