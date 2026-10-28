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
    row = outcome["result"][0]
    # Marshalled like the GET: strings and integers, not the raw bool and float.
    assert row["original_format"] == "False"
    assert row["score"] == 87
    assert row["subtitle"] == "abc-123"
    # The search got what the request resolved, and the instance it belongs to.
    args, kwargs = calls[0]
    assert args[0] == "/tv/Show/e1.mkv" and args[2] == ["opensubtitles"] and args[5] == "series"
    # The queue observes the job, so the search does not observe itself again.
    assert kwargs == {"arr_instance_id": 7, "observed": False}
    assert job.progress_message == "1 subtitle(s) found"


def test_an_empty_search_is_a_finished_job_with_an_empty_result(real_queue, monkeypatch):
    monkeypatch.setattr(real_queue.manual, "manual_search", lambda *args, **kwargs: [])

    job = real_queue.run()

    assert job.status == "completed"
    # An empty list is an answer; only a job that published nothing has None.
    assert real_queue.queue.get_job_result(job.job_id)["result"] == []


def test_a_search_that_cannot_run_fails_the_job_with_its_reason(real_queue, monkeypatch, caplog):
    monkeypatch.setattr(real_queue.manual, "manual_search", lambda *args, **kwargs: "All providers are throttled")

    with caplog.at_level("WARNING"):
        job = real_queue.run()

    assert job.status == "failed"
    assert job.error == {"reason": "providers_unavailable", "message": "All providers are throttled"}
    assert job.retryable is True
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
    assert job.retryable is True and job.is_progress is True


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
