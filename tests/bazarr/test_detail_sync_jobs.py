# coding=utf-8
"""A detail page's sync with Sonarr or Radarr is queued instead of held in the request."""

from types import SimpleNamespace

import pytest
from flask import Flask

import app.database  # noqa: F401
from app.jobs_queue import JobFailed


@pytest.fixture
def queue(monkeypatch):
    names, calls = [], []
    fake = SimpleNamespace(
        add_job_from_function=lambda name, is_progress, return_existing=False: calls.append(
            (name, is_progress, return_existing)) or 17,
        update_job_name=lambda job_id, new_job_name: names.append(new_job_name))
    return SimpleNamespace(fake=fake, names=names, calls=calls)


def _database(title):
    row = SimpleNamespace(title=title) if title else None
    return SimpleNamespace(execute=lambda statement: SimpleNamespace(first=lambda: row))


def test_a_series_sync_queues_itself_and_pressing_it_again_follows_the_running_one(monkeypatch, queue):
    from sonarr.sync import series

    monkeypatch.setattr(series, "jobs_queue", queue.fake)

    assert series.sync_one_series(5, arr_instance_id=2) == 17
    assert queue.calls == [("Syncing series with Sonarr", False, True)]


@pytest.mark.parametrize("instance,expected", [(None, "plain"), (2, "scoped")])
def test_the_series_job_syncs_the_one_series_for_its_instance(monkeypatch, queue, instance, expected):
    from sonarr.sync import series

    seen = []
    monkeypatch.setattr(series, "jobs_queue", queue.fake)
    monkeypatch.setattr(series, "database", _database("Dark"))
    monkeypatch.setattr(series, "update_one_series",
                        lambda *args, **kwargs: seen.append(("plain", args, kwargs)))
    monkeypatch.setattr(series, "update_one_series_for_instance",
                        lambda *args, **kwargs: seen.append(("scoped", args, kwargs)))

    series.sync_one_series(5, arr_instance_id=instance, job_id=9)

    assert seen == [(expected, (5, "updated") if instance is None else (2, 5, "updated"),
                     {"raise_on_failure": True})]
    assert queue.names == ["Syncing Dark with Sonarr", "Synced Dark with Sonarr"]


def test_a_series_that_cannot_be_synced_fails_the_job_with_the_reason_and_one_warning(monkeypatch, queue, caplog):
    from sonarr.sync import series

    def boom(*args, **kwargs):
        raise RuntimeError("Sonarr refused the key")

    monkeypatch.setattr(series, "jobs_queue", queue.fake)
    monkeypatch.setattr(series, "database", _database(None))
    monkeypatch.setattr(series, "update_one_series", boom)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        series.sync_one_series(5, job_id=9)

    # A series Bazarr has not seen yet is still named.
    assert str(failure.value) == "Syncing series 5 with Sonarr failed: Sonarr refused the key"
    assert queue.names == ["Syncing series 5 with Sonarr"]
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "reason=Sonarr refused the key" in warnings[0].getMessage()


def _fetch_fails_for_series(monkeypatch, queue, fetch):
    """The real update_one_series, with Sonarr's answer replaced and nothing else."""
    from sonarr.sync import series

    result = SimpleNamespace(first=lambda: SimpleNamespace(title="Dark"), scalar=lambda: 5)
    monkeypatch.setattr(series, "jobs_queue", queue.fake)
    monkeypatch.setattr(series, "database", SimpleNamespace(execute=lambda statement: result))
    monkeypatch.setattr(series, "skip_unscoped_sync", lambda *args: False)
    monkeypatch.setattr(series, "get_profile_list", lambda: [])
    monkeypatch.setattr(series, "get_tags", lambda: {})
    monkeypatch.setattr(series, "get_language_profiles", lambda: [])
    monkeypatch.setattr(series, "get_series_from_sonarr_api", fetch)
    return series


@pytest.mark.parametrize("answer", [None, []])
def test_a_series_sync_fails_when_sonarr_gives_nothing_back(monkeypatch, queue, caplog, answer):
    # The fetch helper logs its own error and returns nothing when Sonarr is down.
    series = _fetch_fails_for_series(monkeypatch, queue, lambda **kwargs: answer)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        series.sync_one_series(5, job_id=9)

    assert failure.value.reason == "sonarr_unreachable"
    assert str(failure.value) == "Syncing Dark with Sonarr failed: Sonarr did not return series 5"
    assert queue.names == ["Syncing Dark with Sonarr"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_series_sync_fails_when_the_fetch_raises(monkeypatch, queue, caplog):
    def refused(**kwargs):
        raise ConnectionError("connection refused")

    series = _fetch_fails_for_series(monkeypatch, queue, refused)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        series.sync_one_series(5, job_id=9)

    assert failure.value.reason == "sonarr_unreachable"
    assert "connection refused" in str(failure.value)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_series_sync_for_an_unknown_instance_fails_the_job(monkeypatch, queue, caplog):
    from sonarr.sync import series

    monkeypatch.setattr(series, "jobs_queue", queue.fake)
    monkeypatch.setattr(series, "database", _database("Dark"))
    monkeypatch.setattr(series, "client_for_instance", lambda database, instance: None)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        series.sync_one_series(5, arr_instance_id=4, job_id=9)

    assert failure.value.reason == "arr_instance_unknown"
    assert "instance 4" in str(failure.value)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_the_other_series_callers_still_log_and_carry_on(monkeypatch, queue):
    series = _fetch_fails_for_series(monkeypatch, queue, lambda **kwargs: None)

    assert series.update_one_series(5, "updated") is None


def _fetch_fails_for_movie(monkeypatch, queue, fetch):
    from radarr.sync import movies

    result = SimpleNamespace(first=lambda: SimpleNamespace(title="Heat", path="/m/heat.mkv"))
    monkeypatch.setattr(movies, "jobs_queue", queue.fake)
    monkeypatch.setattr(movies, "database", SimpleNamespace(execute=lambda statement: result))
    monkeypatch.setattr(movies, "skip_unscoped_sync", lambda *args: False)
    monkeypatch.setattr(movies, "get_profile_list", lambda arr_client=None: [])
    monkeypatch.setattr(movies, "get_tags", lambda arr_client=None: {})
    monkeypatch.setattr(movies, "get_language_profiles", lambda: [])
    monkeypatch.setattr(movies, "default_instance_id", lambda database, kind: 1)
    monkeypatch.setattr(movies, "resolve_default_profile", lambda *args, **kwargs: None)
    monkeypatch.setattr(movies, "get_movies_from_radarr_api", fetch)
    return movies


@pytest.mark.parametrize("answer", [None, []])
def test_a_movie_sync_fails_when_radarr_gives_nothing_back(monkeypatch, queue, caplog, answer):
    movies = _fetch_fails_for_movie(monkeypatch, queue, lambda **kwargs: answer)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        movies.sync_one_movie(8, job_id=9)

    assert failure.value.reason == "radarr_unreachable"
    assert str(failure.value) == "Syncing Heat with Radarr failed: Radarr did not return movie 8"
    assert queue.names == ["Syncing Heat with Radarr"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_movie_sync_fails_when_the_fetch_raises(monkeypatch, queue, caplog):
    def refused(**kwargs):
        raise ConnectionError("connection refused")

    movies = _fetch_fails_for_movie(monkeypatch, queue, refused)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        movies.sync_one_movie(8, job_id=9)

    assert failure.value.reason == "radarr_unreachable"
    assert "connection refused" in str(failure.value)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_movie_sync_for_an_unknown_instance_fails_the_job(monkeypatch, queue, caplog):
    from radarr.sync import movies

    monkeypatch.setattr(movies, "jobs_queue", queue.fake)
    monkeypatch.setattr(movies, "database", _database("Heat"))
    monkeypatch.setattr(movies, "client_for_instance", lambda database, instance: None)

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        movies.sync_one_movie(8, arr_instance_id=4, job_id=9)

    assert failure.value.reason == "arr_instance_unknown"
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_the_other_movie_callers_still_log_and_carry_on(monkeypatch, queue):
    movies = _fetch_fails_for_movie(monkeypatch, queue, lambda **kwargs: None)

    assert movies.update_one_movie(8, "updated") is None


def test_a_movie_sync_defers_the_search_the_way_the_button_always_did(monkeypatch, queue):
    from radarr.sync import movies

    seen = []
    monkeypatch.setattr(movies, "jobs_queue", queue.fake)
    monkeypatch.setattr(movies, "database", _database("Heat"))
    monkeypatch.setattr(movies, "update_one_movie", lambda *args, **kwargs: seen.append(("plain", args, kwargs)))
    monkeypatch.setattr(movies, "update_one_movie_for_instance",
                        lambda *args, **kwargs: seen.append(("scoped", args, kwargs)))

    assert movies.sync_one_movie(8) == 17
    assert queue.calls == [("Syncing movie with Radarr", False, True)]
    movies.sync_one_movie(8, job_id=9)
    movies.sync_one_movie(8, arr_instance_id=3, job_id=9)

    assert seen == [("plain", (8, "updated", True), {"raise_on_failure": True}),
                    ("scoped", (3, 8, "updated"), {"defer_search": True, "raise_on_failure": True})]
    assert queue.names[:2] == ["Syncing Heat with Radarr", "Synced Heat with Radarr"]


def test_the_detail_page_routes_answer_with_the_queued_job(monkeypatch):
    from api.movies import movies as movies_api
    from api.series import series as series_api
    from app.process_session import PROCESS_SESSION

    monkeypatch.setattr(series_api, "sync_one_series", lambda series_id, arr_instance_id=None: (series_id, arr_instance_id))
    monkeypatch.setattr(movies_api, "sync_one_movie", lambda movie_id, arr_instance_id=None: 31)

    series_resource, movie_resource = series_api.Series, movies_api.Movies
    with Flask(__name__).test_request_context("/series?seriesid=5&action=sync&arr_instance_id=2", method="PATCH"):
        body, status = series_resource.patch.__wrapped__(series_resource())
    assert status == 202 and body == {"job_id": (5, 2), "session": PROCESS_SESSION}

    with Flask(__name__).test_request_context("/movies?radarrid=8&action=sync", method="PATCH"):
        assert movie_resource.patch.__wrapped__(movie_resource()) == (
            {"job_id": 31, "session": PROCESS_SESSION}, 202)



class _LooksLikeASyncFailure(Exception):
    """Carries a ``reason`` of its own that is not a sync failure's code."""

    reason = "something_else"


def test_only_a_sync_failure_names_the_reason_the_job_fails_with(monkeypatch, queue):
    from sonarr.sync import series

    def boom(*args, **kwargs):
        raise _LooksLikeASyncFailure("odd")

    monkeypatch.setattr(series, "jobs_queue", queue.fake)
    monkeypatch.setattr(series, "database", _database("Dark"))
    monkeypatch.setattr(series, "update_one_series", boom)

    with pytest.raises(JobFailed) as failure:
        series.sync_one_series(5, job_id=9)

    assert failure.value.reason == "failed"


def test_a_movie_failure_that_only_looks_like_a_sync_failure_is_a_plain_one(monkeypatch, queue):
    from radarr.sync import movies

    def boom(*args, **kwargs):
        raise _LooksLikeASyncFailure("odd")

    monkeypatch.setattr(movies, "jobs_queue", queue.fake)
    monkeypatch.setattr(movies, "database", _database("Heat"))
    monkeypatch.setattr(movies, "update_one_movie", boom)

    with pytest.raises(JobFailed) as failure:
        movies.sync_one_movie(8, job_id=9)

    assert failure.value.reason == "failed"


def test_a_series_sync_with_no_enabled_default_instance_is_named_as_skipped(monkeypatch, queue):
    series = _fetch_fails_for_series(monkeypatch, queue, lambda **kwargs: pytest.fail("Sonarr was asked"))
    monkeypatch.setattr(series, "skip_unscoped_sync", lambda *args: True)

    series.sync_one_series(5, job_id=9)

    assert queue.names == ["Syncing Dark with Sonarr",
                           "Skipped syncing Dark with Sonarr: no enabled Sonarr instance owns it"]


def test_a_movie_sync_with_no_enabled_default_instance_is_named_as_skipped(monkeypatch, queue):
    movies = _fetch_fails_for_movie(monkeypatch, queue, lambda **kwargs: pytest.fail("Radarr was asked"))
    monkeypatch.setattr(movies, "skip_unscoped_sync", lambda *args: True)

    movies.sync_one_movie(8, job_id=9)

    assert queue.names == ["Syncing Heat with Radarr",
                           "Skipped syncing Heat with Radarr: no enabled Radarr instance owns it"]


def _series_with_episode_sync(monkeypatch, queue, outcome, existing=True):
    """The real update_one_series, with Sonarr's answer and the episode sync's outcome replaced."""
    from sonarr.sync import series

    result = SimpleNamespace(first=lambda: SimpleNamespace(title="Dark"),
                             scalar=lambda: 5 if existing else None)
    episode_syncs = []
    monkeypatch.setattr(series, "jobs_queue", queue.fake)
    monkeypatch.setattr(series, "database", SimpleNamespace(execute=lambda statement: result))
    monkeypatch.setattr(series, "skip_unscoped_sync", lambda *args: False)
    monkeypatch.setattr(series, "get_profile_list", lambda: [])
    monkeypatch.setattr(series, "get_tags", lambda: {})
    monkeypatch.setattr(series, "get_language_profiles", lambda: [])
    monkeypatch.setattr(series, "default_instance_id", lambda database, kind: 1)
    monkeypatch.setattr(series, "resolve_default_profile", lambda *args, **kwargs: None)
    monkeypatch.setattr(series, "get_series_from_sonarr_api", lambda **kwargs: [{"id": 5}])
    monkeypatch.setattr(series, "seriesParser", lambda *args, **kwargs: {"sonarrSeriesId": 5, "path": "/tv/dark"})
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(series, "path_mappings", SimpleNamespace(path_replace=lambda path: path))
    monkeypatch.setattr(series, "sync_episodes",
                        lambda **kwargs: episode_syncs.append(kwargs) or outcome)
    return series, episode_syncs


@pytest.mark.parametrize("existing", [True, False])
def test_a_series_whose_episodes_were_skipped_is_named_as_skipped_not_synced(monkeypatch, queue, existing):
    from sonarr.sync.episodes import SYNC_SKIPPED_UNREADABLE_EPISODE_FILES

    series, episode_syncs = _series_with_episode_sync(
        monkeypatch, queue, SYNC_SKIPPED_UNREADABLE_EPISODE_FILES, existing=existing)

    series.sync_one_series(5, job_id=9)

    assert len(episode_syncs) == 1
    assert queue.names == ["Syncing Dark with Sonarr",
                           "Skipped syncing Dark with Sonarr: its episodes were left unchanged, because "
                           "Sonarr's episode list could not be read or no enabled default Sonarr "
                           "instance owns the series"]


@pytest.mark.parametrize("existing", [True, False])
def test_a_series_whose_episodes_synced_is_named_as_synced(monkeypatch, queue, existing):
    series, _ = _series_with_episode_sync(monkeypatch, queue, None, existing=existing)

    series.sync_one_series(5, job_id=9)

    assert queue.names == ["Syncing Dark with Sonarr", "Synced Dark with Sonarr"]


def test_the_other_series_callers_do_not_hear_about_a_skipped_episode_sync(monkeypatch, queue):
    from sonarr.sync.episodes import SYNC_SKIPPED_UNREADABLE_EPISODE_FILES

    series, episode_syncs = _series_with_episode_sync(
        monkeypatch, queue, SYNC_SKIPPED_UNREADABLE_EPISODE_FILES)

    assert series.update_one_series(5, "updated") is None
    assert len(episode_syncs) == 1


def test_the_other_callers_still_skip_without_a_word(monkeypatch, queue):
    """The webhook and the live feed do not ask for failures, so a skip stays the quiet return it was."""
    series = _fetch_fails_for_series(monkeypatch, queue, lambda **kwargs: pytest.fail("Sonarr was asked"))
    monkeypatch.setattr(series, "skip_unscoped_sync", lambda *args: True)
    assert series.update_one_series(5, "updated") is None
