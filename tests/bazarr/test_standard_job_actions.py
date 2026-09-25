# coding=utf-8
"""User-triggered background actions run on the jobs queue and fail loudly.

Subtitle mods, detail-page scan disk, the missing-subtitles recalculation
after a save, Provider Hub lifecycle actions and the sports manual download
used to run inside their HTTP request. Each is now queued through the
application's JobsQueue, returns at once, and raises with a readable reason on
failure, which is the only thing that makes the queue mark a job failed.

The queue used here is a fresh JobsQueue with no consumer thread, so a queued
job stays pending until a test runs it with the queue's own _run_job.
"""

import json
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from test_settings_profile_deletion import post_settings  # noqa: F401


@pytest.fixture
def queue(monkeypatch):
    """A private queue wired into every module these actions enqueue through."""
    from app import jobs_queue as jobs_queue_module
    from provider_hub import jobs as hub_jobs
    from sportarr import manual_jobs
    from subtitles.indexer import missing_refresh
    from subtitles.indexer import movies as movies_indexer
    from subtitles.indexer import series as series_indexer
    from subtitles.tools import mods

    private = jobs_queue_module.JobsQueue()
    for module in (hub_jobs, manual_jobs, missing_refresh, movies_indexer,
                   series_indexer, mods):
        monkeypatch.setattr(module, "jobs_queue", private)
    # add_job_from_function's wait loop and a few helpers read the module global.
    monkeypatch.setattr(jobs_queue_module, "jobs_queue", private)
    monkeypatch.setattr(jobs_queue_module, "event_stream", lambda **kwargs: None)
    return private


def run_next(queue):
    """Run the next pending job exactly as the consumer would, and report it."""
    job = queue._reserve_next_job()
    assert job is not None, "nothing was queued"
    queue._run_job(job)
    return queue.list_jobs_from_queue(job_id=job.job_id)[0]


def pending(queue):
    return list(queue.jobs_pending_queue)


# ---------------------------------------------------------------------------
# Gap 2: subtitle mods and tools
# ---------------------------------------------------------------------------

@pytest.fixture
def mod_job(monkeypatch, tmp_path):
    from app import event_handler
    from subtitles.indexer import movies as movies_indexer
    from subtitles.tools import mods

    subtitle = tmp_path / "Movie.en.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    monkeypatch.setattr(mods.settings.general, "chmod_enabled", False)
    monkeypatch.setattr(event_handler, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(movies_indexer, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr("utilities.path_mappings.path_mappings.path_replace_reverse_instance",
                        lambda path, *a: path)
    return mods, subtitle


def test_a_mod_is_queued_and_its_id_returned(queue, mod_job):
    mods, subtitle = mod_job

    job_id = mods.apply_subtitle_mods("en", str(subtitle), ["remove_HI"], str(subtitle.parent / "Movie.mkv"),
                                      media_type="movie", media_id=5, arr_instance_id=2)

    assert isinstance(job_id, int)
    [job] = pending(queue)
    assert job.job_id == job_id
    assert job.job_name == "Remove HI Tags: Movie.en.srt"
    assert (job.module, job.func) == ("subtitles.tools.mods", "apply_subtitle_mods")
    assert job.kwargs["mods"] == ["remove_HI"] and job.kwargs["arr_instance_id"] == 2


def test_a_mod_job_that_works_completes(queue, mod_job, monkeypatch):
    mods, subtitle = mod_job
    monkeypatch.setattr(mods, "subtitles_apply_mods", lambda **kwargs: str(subtitle))

    mods.apply_subtitle_mods("en", str(subtitle), ["OCR_fixes"], str(subtitle.parent / "Movie.mkv"),
                             media_type="movie", media_id=5)
    job = run_next(queue)

    assert job["status"] == "completed"
    assert job["job_name"] == "OCR Fixes: Movie.en.srt"


def test_a_mod_job_that_cannot_write_fails_with_the_reason(queue, mod_job, monkeypatch):
    mods, subtitle = mod_job

    def refuses(**kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr(mods, "subtitles_apply_mods", refuses)
    mods.apply_subtitle_mods("en", str(subtitle), ["remove_HI"], str(subtitle.parent / "Movie.mkv"),
                             media_type="movie", media_id=5)
    job = run_next(queue)

    assert job["status"] == "failed"
    assert job["job_name"] == "Failed Remove HI Tags: Movie.en.srt"
    assert job["error"]["message"] == "Remove HI Tags failed on Movie.en.srt: Read-only file system"


def test_a_mod_that_produces_nothing_fails_instead_of_completing(queue, mod_job, monkeypatch):
    """subtitles_apply_mods answers None for an unreadable file. That used to be
    a 409 nobody saw; as a job it must not end green."""
    mods, subtitle = mod_job
    monkeypatch.setattr(mods, "subtitles_apply_mods", lambda **kwargs: None)

    mods.apply_subtitle_mods("en", str(subtitle), ["common"], str(subtitle.parent / "Movie.mkv"),
                             media_type="movie", media_id=5)
    job = run_next(queue)

    assert job["status"] == "failed"
    assert "Common Fixes failed on Movie.en.srt" in job["error"]["message"]


def test_the_subtitles_route_queues_the_mod_and_answers_202(monkeypatch, tmp_path):
    from api.subtitles import subtitles as endpoint

    video = tmp_path / "Movie.mkv"
    subtitle = tmp_path / "Movie.en.srt"
    video.write_bytes(b"")
    subtitle.write_text("1\n")
    queued = []
    monkeypatch.setattr(endpoint, "apply_subtitle_mods", lambda **kwargs: queued.append(kwargs) or 12)
    monkeypatch.setattr(endpoint, "subtitle_path_within_area", lambda *a, **k: True)
    monkeypatch.setattr(endpoint.path_mappings, "path_replace_instance", lambda path, *a: path)

    class Database:
        def execute(self, statement):
            return SimpleNamespace(first=lambda: SimpleNamespace(
                path=str(video), subtitles="[]", imdbId="tt1", tmdbId="1"))

    monkeypatch.setattr(endpoint, "database", Database())
    args = dict(action="remove_HI", language="en", path=str(subtitle), type="movie", id=5,
                arr_instance_id=2, forced="False", hi="False")
    monkeypatch.setattr(endpoint.Subtitles.patch_request_parser, "parse_args", lambda: args)

    started = time.monotonic()
    response = endpoint.Subtitles.patch.__wrapped__(endpoint.Subtitles())

    assert response == ({"job_id": 12}, 202)
    assert time.monotonic() - started < 1
    assert queued == [dict(language="en", subtitle_path=str(subtitle), mods=["remove_HI"],
                           video_path=str(video), media_type="movie", media_id=5, arr_instance_id=2)]


# ---------------------------------------------------------------------------
# Gap 2: scan disk from the detail pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["series", "movie"])
def test_scan_disk_is_queued_and_the_route_answers_202(queue, monkeypatch, kind):
    if kind == "series":
        from api.series import series as api_module
        resource, arg, func = api_module.Series(), "seriesid", "series_scan_disk"
    else:
        from api.movies import movies as api_module
        resource, arg, func = api_module.Movies(), "radarrid", "movies_scan_disk"

    app = Flask(__name__)
    with app.test_request_context("/", method="PATCH",
                                  data={"action": "scan-disk", arg: "7", "arr_instance_id": "3"}):
        body, status = type(resource).patch.__wrapped__(resource)

    [job] = pending(queue)
    assert status == 202 and body == {"job_id": job.job_id}
    assert job.func == func
    assert {key: job.kwargs[key] for key in ("arr_instance_id",)} == {"arr_instance_id": 3}


@pytest.mark.parametrize("kind", ["series", "movie"])
def test_scan_disk_job_fails_with_the_title_and_reason(queue, monkeypatch, kind):
    from subtitles.indexer import movies as movies_indexer
    from subtitles.indexer import series as series_indexer

    module = series_indexer if kind == "series" else movies_indexer
    row = SimpleNamespace(title="The Show")
    monkeypatch.setattr(module, "database",
                        SimpleNamespace(execute=lambda statement: SimpleNamespace(first=lambda: row)))
    events = []
    monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs))

    def broken(*args, **kwargs):
        raise OSError("ffprobe is not installed")

    monkeypatch.setattr(module, "series_scan_subtitles" if kind == "series" else "movies_scan_subtitles",
                        broken)
    wrapper = module.series_scan_disk if kind == "series" else module.movies_scan_disk
    wrapper(7, arr_instance_id=3)
    job = run_next(queue)

    assert job["status"] == "failed"
    assert job["job_name"] == "Scanning disk for The Show"
    assert job["error"]["message"] == "Scanning disk for The Show failed: ffprobe is not installed"
    assert events == []


@pytest.mark.parametrize("kind", ["series", "movie"])
def test_scan_disk_job_that_works_completes_and_announces(queue, monkeypatch, kind):
    from subtitles.indexer import movies as movies_indexer
    from subtitles.indexer import series as series_indexer

    module = series_indexer if kind == "series" else movies_indexer
    row = SimpleNamespace(title="The Show")
    monkeypatch.setattr(module, "database",
                        SimpleNamespace(execute=lambda statement: SimpleNamespace(first=lambda: row)))
    events, scanned = [], []
    monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(module, "series_scan_subtitles" if kind == "series" else "movies_scan_subtitles",
                        lambda no, arr_instance_id=None: scanned.append((no, arr_instance_id)))
    wrapper = module.series_scan_disk if kind == "series" else module.movies_scan_disk
    wrapper(7, arr_instance_id=3)
    job = run_next(queue)

    assert job["status"] == "completed"
    assert scanned == [(7, 3)]
    assert events == [{"type": "series" if kind == "series" else "movie", "payload": 7}]


# ---------------------------------------------------------------------------
# Gap 4: missing subtitles recalculation after a save
# ---------------------------------------------------------------------------

@pytest.fixture
def recalculation(queue, monkeypatch):
    """The library-wide passes, recorded instead of run."""
    from app.config import settings
    from subtitles.indexer import movies as movies_indexer
    from subtitles.indexer import series as series_indexer
    from subtitles.indexer import missing_refresh

    calls = []
    monkeypatch.setattr(series_indexer, "list_missing_subtitles",
                        lambda **kwargs: calls.append(("series", kwargs)))
    monkeypatch.setattr(movies_indexer, "list_missing_subtitles_movies",
                        lambda **kwargs: calls.append(("movies", kwargs)))
    monkeypatch.setattr(missing_refresh, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(settings.general, "use_sonarr", True)
    monkeypatch.setattr(settings.general, "use_radarr", True)
    monkeypatch.setattr(settings.general, "use_sportarr", False)
    return missing_refresh, calls


def test_saving_a_language_profile_queues_the_recalculation_without_running_it(
        schema_session, post_settings, recalculation, monkeypatch):  # noqa: F811
    import sys

    missing_refresh, calls = recalculation
    endpoint = sys.modules["api.system.settings"]
    monkeypatch.setattr(endpoint, "queue_missing_subtitles_recalculation",
                        missing_refresh.queue_missing_subtitles_recalculation)

    started = time.monotonic()
    post_settings({"languages-profiles": json.dumps([{
        "profileId": 1, "name": "English", "cutoff": None, "items": [
            {"id": 1, "language": "en", "forced": "False", "hi": "False", "audio_exclude": "False",
             "audio_only_include": "False"}],
        "mustContain": [], "mustNotContain": [], "originalFormat": False, "tag": None}])})
    elapsed = time.monotonic() - started

    assert calls == [], "the save ran the recalculation itself"
    assert elapsed < 5
    [job] = pending(missing_refresh.jobs_queue)
    assert job.job_name == "Recalculating missing subtitles"
    assert job.is_progress is True


def test_the_toggle_hook_queues_instead_of_recalculating(recalculation, monkeypatch):
    from app import config

    missing_refresh, calls = recalculation
    source = __import__("inspect").getsource(config._save_settings)
    hook = source[source.index("if use_embedded_subs_changed or undefined_audio_track_default_changed"):]
    hook = hook[:hook.index("if clear_disabled_provider_hub_statuses")]
    assert "queue_missing_subtitles_recalculation()" in hook
    assert "list_missing_subtitles" not in hook


def test_a_second_save_is_folded_into_a_pending_recalculation(recalculation):
    missing_refresh, _ = recalculation

    first = missing_refresh.queue_missing_subtitles_recalculation()
    second = missing_refresh.queue_missing_subtitles_recalculation()

    assert first == second
    assert len(pending(missing_refresh.jobs_queue)) == 1


def test_a_save_while_the_recalculation_runs_queues_another_pass(recalculation):
    """A running pass may already be past what the second save changed."""
    missing_refresh, _ = recalculation
    queue = missing_refresh.jobs_queue

    first = missing_refresh.queue_missing_subtitles_recalculation()
    running = queue._reserve_next_job()
    running.status = "running"
    second = missing_refresh.queue_missing_subtitles_recalculation()

    assert running.job_id == first
    assert second and second != first
    assert [job.job_id for job in pending(queue)] == [second]


def test_the_library_recalculation_job_runs_every_enabled_pass(recalculation):
    missing_refresh, calls = recalculation

    missing_refresh.queue_missing_subtitles_recalculation()
    job = run_next(missing_refresh.jobs_queue)

    assert job["status"] == "completed"
    assert [name for name, _ in calls] == ["series", "movies"]


def test_the_recalculation_job_fails_with_what_failed(recalculation, monkeypatch):
    from subtitles.indexer import movies as movies_indexer

    missing_refresh, _ = recalculation

    def locked(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(movies_indexer, "list_missing_subtitles_movies", locked)
    missing_refresh.queue_missing_subtitles_recalculation()
    job = run_next(missing_refresh.jobs_queue)

    assert job["status"] == "failed"
    assert job["error"]["message"] == "Recalculating missing subtitles for movies failed: database is locked"


def test_mass_edit_recalculates_only_the_edited_items_in_a_job(recalculation, monkeypatch):
    missing_refresh, calls = recalculation
    events = []
    monkeypatch.setattr(missing_refresh, "event_stream", lambda **kwargs: events.append(kwargs))

    class Rows:
        def all(self):
            return [SimpleNamespace(sonarrEpisodeId=91)]

    monkeypatch.setattr("app.database.database", SimpleNamespace(execute=lambda statement: Rows()))

    job_id = missing_refresh.queue_missing_subtitles_recalculation(series=[(4, 2)], movies=[(8, None)])
    [job] = pending(missing_refresh.jobs_queue)
    assert job.job_id == job_id
    assert job.job_name == "Recalculating missing subtitles for 1 series and 1 movie"
    result = run_next(missing_refresh.jobs_queue)

    assert result["status"] == "completed"
    assert calls == [("series", {"no": 4, "arr_instance_id": 2}),
                     ("movies", {"no": 8, "arr_instance_id": None})]
    assert {"type": "episode-wanted", "payload": 91} in events
    assert {"type": "movie-wanted", "payload": 8} in events
    assert events[-1] == {"type": "badges"}


def test_the_mass_edit_route_hands_the_changed_items_to_the_queue(schema_session, monkeypatch):
    from api.series import series
    from app.database import TableLanguagesProfiles, TableShows

    handed = []
    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(series, "queue_missing_subtitles_recalculation",
                        lambda series=None: handed.append(series))
    schema_session.add(TableLanguagesProfiles(profileId=5, name="Profile", items="[]"))
    schema_session.add_all([
        TableShows(id=300, sonarrSeriesId=1, arr_instance_id=2, path="/a", title="A"),
        TableShows(id=301, sonarrSeriesId=2, arr_instance_id=2, path="/b", title="B"),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/series", method="POST",
                                  data={"id": ["300", "301"], "profileid": ["5", "5"]}):
        response = series.Series.post.__wrapped__(series.Series())

    assert response == ("", 204)
    assert handed == [[(1, 2), (2, 2)]], "one queued recalculation for the whole selection"


# ---------------------------------------------------------------------------
# Gap 5: Provider Hub lifecycle actions
# ---------------------------------------------------------------------------

@pytest.fixture
def hub_state(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
                          encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub", "name": "Example", "active_version": "1.0.0",
        "state": "active", "pending_restart": False,
    }
    save_state(state)
    return state_file


def call_hub(resource_name, method, *args, json_body=None, query=None):
    from api.provider_hub import provider_hub as endpoint

    resource = getattr(endpoint, resource_name)
    with Flask(__name__).test_request_context("/", method=method.upper(), json=json_body,
                                              query_string=query):
        return getattr(resource, method).__wrapped__(resource(), *args)


def test_install_is_queued_and_answers_at_once(queue, monkeypatch):
    from provider_hub import service

    monkeypatch.setattr(service, "stage_install", lambda manifest: pytest.fail("installed in the request"))
    body, status = call_hub("ProviderHubInstallations", "post",
                            json_body={"manifest": {"provider_id": "examplehub", "name": "Example"}})

    [job] = pending(queue)
    assert status == 202 and body == {"job_id": job.job_id}
    assert job.job_name == "Installing provider Example"
    assert (job.module, job.func) == ("provider_hub.jobs", "install_provider")


def test_a_second_identical_install_follows_the_first_job(queue):
    from provider_hub import jobs as hub_jobs

    manifest = {"provider_id": "examplehub", "name": "Example"}
    assert hub_jobs.queue_install(manifest) == hub_jobs.queue_install(manifest)
    assert len(pending(queue)) == 1


def test_install_job_success_returns_the_installation(queue, monkeypatch):
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    monkeypatch.setattr(service, "stage_install", lambda manifest: {"provider_id": "examplehub",
                                                                     "state": "staged"})
    hub_jobs.queue_install({"provider_id": "examplehub", "name": "Example"})
    job = run_next(queue)

    assert job["status"] == "completed"
    assert job["job_returned_value"] == {"provider_id": "examplehub", "state": "staged"}


def test_install_job_failure_names_the_provider_and_the_reason(queue, monkeypatch):
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    def mismatch(manifest):
        raise service.ProviderHubInstallError("bundle hash mismatch for provider.py")

    monkeypatch.setattr(service, "stage_install", mismatch)
    hub_jobs.queue_install({"provider_id": "examplehub", "name": "Example"})
    job = run_next(queue)

    assert job["status"] == "failed"
    assert job["error"]["message"] == "Could not install Example: bundle hash mismatch for provider.py"


def test_uninstall_is_queued_runs_as_a_job_and_keeps_the_hub_log(queue, hub_state):
    from provider_hub.service import list_jobs

    body, status = call_hub("ProviderHubInstallation", "delete", "examplehub")
    assert status == 202
    [job] = pending(queue)
    assert body == {"job_id": job.job_id}
    assert job.job_name == "Uninstalling provider Example"
    assert list_jobs() == [], "the uninstall ran inside the request"

    result = run_next(queue)
    assert result["status"] == "completed"
    ledger = list_jobs()[-1]
    assert ledger["action"] == "uninstall" and ledger["state"] == "completed"


def test_uninstall_of_an_unknown_provider_is_404_and_queues_nothing(queue, hub_state):
    assert call_hub("ProviderHubInstallation", "delete", "nothing") == ("Provider not found", 404)
    assert pending(queue) == []


def test_uninstall_job_fails_when_the_provider_is_gone(queue, hub_state, monkeypatch):
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    hub_jobs.queue_uninstall("examplehub", "Example")
    monkeypatch.setattr(service, "remove_installation", lambda provider_id: False)
    job = run_next(queue)

    assert job["status"] == "failed"
    assert job["error"]["message"] == "Could not uninstall Example: it is not installed"


def test_update_is_queued_and_fails_with_the_recorded_error(queue, hub_state, monkeypatch):
    from provider_hub import service

    body, status = call_hub("ProviderHubUpdatesApply", "post", query={"provider_id": "examplehub"})
    [job] = pending(queue)
    assert status == 202 and body == {"job_id": job.job_id}
    assert job.job_name == "Updating provider Example"

    monkeypatch.setattr(service, "apply_update",
                        lambda provider_id: {"provider_id": provider_id,
                                             "last_error": "No update manifest is available"})
    result = run_next(queue)
    assert result["status"] == "failed"
    assert result["error"]["message"] == "Could not update Example: No update manifest is available"


def test_catalog_refresh_is_queued_and_fails_with_the_source_reason(queue, monkeypatch):
    from provider_hub import service

    body, status = call_hub("ProviderHubCatalogRefresh", "post")
    [job] = pending(queue)
    assert status == 202 and body == {"job_id": job.job_id}
    assert job.job_name == "Refreshing provider catalog"

    monkeypatch.setattr(service, "refresh_catalog", lambda: {"sources": 1, "entries": 0})
    monkeypatch.setattr(service, "load_state", lambda: {"catalog_sources": {
        "official": {"name": "Official", "last_error": "GitHub answered 503"}}})
    result = run_next(queue)
    assert result["status"] == "failed"
    assert result["error"]["message"] == (
        "Could not refresh the provider catalog from Official (GitHub answered 503)")


def test_catalog_refresh_job_that_works_completes(queue, monkeypatch):
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    monkeypatch.setattr(service, "refresh_catalog", lambda: {"sources": 1, "entries": 3})
    monkeypatch.setattr(service, "load_state", lambda: {"catalog_sources": {
        "official": {"name": "Official", "last_error": None}}})
    hub_jobs.queue_catalog_refresh()

    assert run_next(queue)["status"] == "completed"


# ---------------------------------------------------------------------------
# Gap 8: sports manual download
# ---------------------------------------------------------------------------

def test_the_sports_download_is_queued_the_way_the_library_one_is(queue):
    from sportarr import manual_jobs

    job_id = manual_jobs.sports_manually_download_subtitle(61, {"subtitle": "cached"}, 1)

    [job] = pending(queue)
    assert job.job_id == job_id
    assert job.job_name == "Manually downloading Subtitles"
    assert (job.module, job.func) == ("sportarr.manual_jobs", "sports_manually_download_subtitle")
    assert job.kwargs["event_id"] == 61 and job.kwargs["arr_instance_id"] == 1


def test_a_failed_sports_download_fails_its_job_with_the_reason(queue, monkeypatch):
    from sportarr import manual_jobs
    from sportarr import subtitles as sports_subtitles

    monkeypatch.setattr(manual_jobs, "database",
                        SimpleNamespace(get=lambda *a, **k: SimpleNamespace(title="Final")))
    monkeypatch.setattr(manual_jobs, "event_stream", lambda **kwargs: None)

    def throttled(*args, **kwargs):
        raise OSError("All providers are throttled")

    monkeypatch.setattr(sports_subtitles, "manual_download_sports", throttled)
    manual_jobs.sports_manually_download_subtitle(61, {"subtitle": "cached"}, 1)
    job = run_next(queue)

    assert job["status"] == "failed"
    assert job["job_name"] == "Failed to download Subtitles for Final"
    assert job["error"]["message"] == "All providers are throttled"


def test_a_sports_download_that_publishes_completes(queue, monkeypatch):
    from sportarr import library, manual_jobs
    from sportarr import subtitles as sports_subtitles

    monkeypatch.setattr(manual_jobs, "database",
                        SimpleNamespace(get=lambda *a, **k: SimpleNamespace(title="Final")))
    events = []
    monkeypatch.setattr(manual_jobs, "event_stream", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(sports_subtitles, "manual_download_sports",
                        lambda *args, **kwargs: SimpleNamespace(publication={"published": True}))
    monkeypatch.setattr(library, "get_event", lambda *args: {"id": 61})
    manual_jobs.sports_manually_download_subtitle(61, {"subtitle": "cached"}, 1)
    job = run_next(queue)

    assert job["status"] == "completed"
    assert job["job_name"] == "Manually downloaded Subtitles for Final"
    assert job["job_returned_value"] == {"event": {"id": 61}, "publication": {"published": True}}
    assert {"type": "sports", "action": "update", "payload": 61} in events


def test_a_sports_download_published_with_warnings_says_so_on_its_job(queue, monkeypatch):
    from sportarr import library, manual_jobs
    from sportarr import subtitles as sports_subtitles

    monkeypatch.setattr(manual_jobs, "database",
                        SimpleNamespace(get=lambda *a, **k: SimpleNamespace(title="Final")))
    monkeypatch.setattr(manual_jobs, "event_stream", lambda **kwargs: None)
    warning = "Subtitle published; index did not complete. Index refresh failed; no refresh queued."
    monkeypatch.setattr(sports_subtitles, "manual_download_sports",
                        lambda *args, **kwargs: SimpleNamespace(publication={
                            "published": True, "status": "published_with_warnings", "message": warning}))
    monkeypatch.setattr(library, "get_event", lambda *args: {"id": 61})
    manual_jobs.sports_manually_download_subtitle(61, {"subtitle": "cached"}, 1)
    job = run_next(queue)

    assert job["status"] == "completed"
    assert job["job_name"] == "Downloaded Subtitles with warnings for Final"
    assert job["progress_message"] == warning
