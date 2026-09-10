"""Read-only bounded summary of local Bazarr work, on both supported engines.

The summary is an aggregate over real jobs, translators, subtitle history and
outstanding subtitle requirements. It must never write, never probe a provider
or an indexer, and never turn an unavailable source into a confident zero.
"""
import datetime as dt
import re
import os
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

EPOCH = dt.datetime(2026, 9, 1, 12, 0, 0)


@pytest.fixture(params=["sqlite", "postgresql"])
def summary_database(request, monkeypatch):
    from app import activity
    from app import database as db
    from discover import summary as summary_module
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.fail("Dedicated PostgreSQL test URL is required")
        engine = sa.create_engine(url)
        schema = "discover_summary_" + uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
    else:
        engine = sa.create_engine("sqlite://")
        schema = None
    db.Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(db.TableLanguagesProfiles(profileId=1, name="Default", items="[]", cutoff=None))
    session.commit()
    monkeypatch.setattr(db, "engine", engine)
    activity.reset()
    summary_module.reset_cache()
    try:
        yield SimpleNamespace(engine=engine, session=session, backend=request.param)
    finally:
        activity.reset()
        summary_module.reset_cache()
        session.close()
        if schema:
            with engine.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def quiet_queue(monkeypatch):
    """An empty, isolated jobs queue with no socket emission."""
    from app.jobs_queue import JobsQueue
    monkeypatch.setattr("app.jobs_queue.event_stream", lambda *args, **kwargs: None)
    queue = JobsQueue()
    monkeypatch.setattr("app.jobs_queue.jobs_queue", queue)
    return queue


@pytest.fixture(autouse=True)
def no_external_status(monkeypatch):
    """No summary read may reach a scheduler, a live feed or a provider probe."""
    import sys
    monkeypatch.delitem(sys.modules, "app.scheduler", raising=False)
    monkeypatch.delitem(sys.modules, "app.signalr_client", raising=False)
    from app import get_providers
    monkeypatch.setattr(get_providers, "tp", {}, raising=False)


def add_instance(session, instance_id, kind="sonarr", name="Main", enabled=1, is_default=1):
    from app.database import TableArrInstances
    session.add(TableArrInstances(id=instance_id, kind=kind, stable_key=f"{kind}-{instance_id}",
                                  name=name, enabled=enabled, is_default=is_default,
                                  ip="127.0.0.1", port=8989 + instance_id, base_url="/",
                                  ssl=0, verify_ssl=0, http_timeout=60, api_key="",
                                  created_at=EPOCH, updated_at=EPOCH))
    session.flush()


def add_show(session, local_id, owner, upstream=10, title="Northern Light", profile=1):
    from app.database import TableShows
    session.add(TableShows(id=local_id, arr_instance_id=owner, sonarrSeriesId=upstream,
                           title=title, path=f"/series/{local_id}", profileId=profile,
                           monitored="True", seriesType="standard", tags="[]"))
    session.flush()


def add_episode(session, local_id, series_id, owner, missing="['en']", upstream=20,
                season=1, episode=1, title="Pilot"):
    from app.database import TableEpisodes
    session.add(TableEpisodes(id=local_id, series_id=series_id, arr_instance_id=owner,
                              sonarrSeriesId=10, sonarrEpisodeId=upstream, season=season,
                              episode=episode, title=title, path=f"/series/{series_id}/{local_id}.mkv",
                              missing_subtitles=missing, monitored="True", subtitles="[]",
                              failedAttempts="[]", audio_language="English"))
    session.flush()


def add_movie(session, local_id, owner, missing="['en']", upstream=30, title="Example",
              profile=1):
    from app.database import TableMovies
    session.add(TableMovies(id=local_id, arr_instance_id=owner, radarrId=upstream, title=title,
                            path=f"/movies/{local_id}.mkv", tmdbId=str(1000 + local_id),
                            profileId=profile, missing_subtitles=missing, monitored="True",
                            subtitles="[]", failedAttempts="[]", audio_language="English",
                            year="2024", tags="[]"))
    session.flush()


def add_episode_history(session, history_id, action, episode_id, series_id, owner,
                        language="en", provider="example", minutes=1):
    from app.database import TableHistory
    session.add(TableHistory(id=history_id, action=action, episode_id=episode_id,
                             series_id=series_id, arr_instance_id=owner, sonarrSeriesId=10,
                             sonarrEpisodeId=20, description="Downloaded", language=language,
                             provider=provider, timestamp=EPOCH - dt.timedelta(minutes=minutes),
                             video_path="/series/1/1.mkv"))
    session.flush()


def add_movie_history(session, history_id, action, movie_id, owner, language="en",
                      provider="example", minutes=1):
    from app.database import TableHistoryMovie
    session.add(TableHistoryMovie(id=history_id, action=action, movie_id=movie_id,
                                  arr_instance_id=owner, radarrId=30, description="Downloaded",
                                  language=language, provider=provider,
                                  timestamp=EPOCH - dt.timedelta(minutes=minutes),
                                  video_path="/movies/1.mkv"))
    session.flush()


def enqueue(queue, name, module, func, kwargs, status="pending", is_progress=False):
    job_id = queue.feed_jobs_pending_queue(name, module, func, kwargs=kwargs,
                                           is_progress=is_progress)
    if status == "running":
        job = queue._reserve_next_job()
        job.status = "running"
        return job
    return next(job for job in queue.jobs_pending_queue if job.job_id == job_id)


def counted(engine, call):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        return call(), statements
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def rows(session):
    from app.database import Base
    return {table.name: session.execute(sa.select(table)).all()
            for table in Base.metadata.sorted_tables}


def _started_signalr(sonarr_connected=False, radarr_connected=False, extras=()):
    """A SignalR module shaped like the one every running Bazarr already has.

    app/signalr_client.py builds both singletons at import with connected false,
    and the badges API imports it, so the module is always present whether or
    not main.py ever started a client.
    """
    return SimpleNamespace(
        sonarr_signalr_client=SimpleNamespace(arr_instance_id=None,
                                              connected=sonarr_connected),
        radarr_signalr_client=SimpleNamespace(arr_instance_id=None,
                                              connected=radarr_connected),
        _sonarr_signalr_clients=list(extras),
        _radarr_signalr_clients=[])


def test_summary_reports_running_queued_wanted_and_successful_arrivals(summary_database, quiet_queue):
    from discover.summary import get_summary, QUERY_BUDGET
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en', 'hu:forced']")
    add_movie(session, 200, 1, missing="['de']")
    add_episode_history(session, 1, 1, 101, 100, 1, language="en")
    session.commit()

    running = enqueue(quiet_queue, "Translating Northern Light (EN to HU)",
                      "subtitles.tools.translate.main", "translate_subtitles_file",
                      {"media_type": "episode", "arr_instance_id": 1, "to_lang": "hu"},
                      status="running")
    assert running.status == "running"
    enqueue(quiet_queue, "Searching for missing movies subtitles", "subtitles.wanted.movies",
            "wanted_search_missing_subtitles_movies", {"wait_for_completion": False})
    enqueue(quiet_queue, "Manually downloading Subtitles", "subtitles.manual",
            "movie_manually_download_specific_subtitle", {"radarr_id": 30, "arr_instance_id": 1})

    before = rows(session)
    summary, statements = counted(summary_database.engine, get_summary)

    assert summary["wanted"]["requirements"] == 3
    assert summary["activity"]["running_count"] == 1
    assert summary["activity"]["queued_count"] == 2
    assert all(item["status"] == "success" for item in summary["arrivals"])
    assert summary["arrivals"], "a recorded manual download is a successful arrival"
    assert len(statements) <= QUERY_BUDGET
    assert all(statement.lstrip().upper().startswith(("SELECT", "BEGIN", "COMMIT", "ROLLBACK"))
               for statement in statements)
    assert rows(session) == before
    assert summary["state"] == "busy"


def test_wanted_counts_required_languages_not_titles(summary_database, quiet_queue):
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en', 'hu:forced', 'de:hi']")
    add_episode(session, 102, 100, 1, missing="[]", upstream=21, episode=2)
    add_movie(session, 200, 1, missing="['en']")
    session.commit()

    wanted = get_summary()["wanted"]
    assert wanted["requirements"] == 4
    assert wanted["episode_requirements"] == 3
    assert wanted["movie_requirements"] == 1
    assert wanted["media_count"] == 2
    assert wanted["complete"] is True
    assert wanted["availability"] == "available"


def test_wanted_qualifies_uncomputed_and_malformed_demand(summary_database, quiet_queue):
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en']")
    add_episode(session, 102, 100, 1, missing=None, upstream=21, episode=2)
    add_movie(session, 200, 1, missing="{'en': 1}")
    add_movie(session, 201, 1, missing="['en', 'en']", upstream=31, title="Duplicated")
    session.commit()

    wanted = get_summary()["wanted"]
    assert wanted["requirements"] == 2
    assert wanted["unknown_media_count"] == 2
    assert wanted["complete"] is False
    assert "uncomputed_media" in wanted["qualifications"]
    assert "malformed_requirements" in wanted["qualifications"]
    assert "duplicate_requirement_tokens" in wanted["qualifications"]


def test_wanted_applies_existing_exclusions_and_reports_owners(summary_database, quiet_queue,
                                                               monkeypatch):
    from app.config import settings
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    add_instance(session, 2, name="Anime", is_default=0)
    add_show(session, 100, 1)
    add_show(session, 200, 2, title="Anime", upstream=10)
    add_episode(session, 101, 100, 1, missing="['en']")
    add_episode(session, 201, 200, 2, missing="['en', 'ja']", upstream=20, season=0)
    session.commit()

    owners = {item["arr_instance_id"]: item for item in get_summary()["wanted"]["by_instance"]}
    assert owners[1]["requirements"] == 1
    assert owners[1]["instance_name"] == "Main"
    assert owners[2]["requirements"] == 2

    monkeypatch.setattr(settings.sonarr, "exclude_season_zero", True)
    from discover import summary as summary_module
    summary_module.reset_cache()
    excluded = get_summary()["wanted"]
    assert excluded["requirements"] == 1
    assert [item["arr_instance_id"] for item in excluded["by_instance"]] == [1]


def test_queue_membership_alone_is_not_running_work(summary_database, quiet_queue):
    from discover.summary import get_summary
    enqueue(quiet_queue, "Sync with Sonarr (Main)", "sonarr.sync.series",
            "update_series_for_instance", {"arr_instance_id": 1})
    reserved = quiet_queue._reserve_next_job()
    assert reserved.status == "pending"
    assert len(quiet_queue.jobs_running_queue) == 1

    activity_component = get_summary()["activity"]
    assert activity_component["running_count"] == 0
    assert activity_component["queued_count"] == 1
    assert activity_component["queued"][0]["state"] == "queued"


def test_progress_is_shown_only_when_the_source_measures_it(summary_database, quiet_queue):
    from discover.summary import get_summary
    measured = enqueue(quiet_queue, "Searching for missing series subtitles",
                       "subtitles.wanted.series", "wanted_search_missing_subtitles_series",
                       {"wait_for_completion": False}, status="running", is_progress=True)
    measured.progress_value = 4
    measured.progress_max = 12
    unmeasured = enqueue(quiet_queue, "Manually downloading Subtitles", "subtitles.manual",
                         "movie_manually_download_specific_subtitle", {"radarr_id": 30},
                         status="running")
    unmeasured.progress_value = 7
    unmeasured.progress_max = 0

    items = {item["name"]: item for item in get_summary()["activity"]["running"]}
    assert items["Searching for missing series subtitles"]["progress"] == {
        "unit": "item", "value": 4, "total": 12}
    assert items["Manually downloading Subtitles"]["progress"] is None


def test_scheduler_wrapper_and_its_queue_child_are_one_activity(summary_database, quiet_queue):
    import sys
    from app import activity
    from discover.summary import get_summary

    with activity.scheduler_run("wanted_search_missing_subtitles_series") as run_id:
        child = enqueue(quiet_queue, "Searching for missing series subtitles",
                        "subtitles.wanted.series", "wanted_search_missing_subtitles_series",
                        {"wait_for_completion": True}, status="running", is_progress=True)
    assert child.scheduler_run_id == run_id

    fake = SimpleNamespace(scheduler=SimpleNamespace(
        get_task_list=lambda: [
            {"name": "Search for Missing Series Subtitles", "interval": "every 3 hours",
             "next_run_in": "in 2 hours", "next_run_time": "in 2 hours",
             "job_id": "wanted_search_missing_subtitles_series", "job_running": True},
            {"name": "Sync with Sonarr (Main)", "interval": "every 5 minutes",
             "next_run_in": "in 3 minutes", "next_run_time": "in 3 minutes",
             "job_id": "update_series_1", "job_running": False},
        ],
        get_running_tasks=lambda: ["wanted_search_missing_subtitles_series"]))
    sys.modules["app.scheduler"] = fake
    try:
        component = get_summary()["activity"]
    finally:
        del sys.modules["app.scheduler"]

    assert component["running_count"] == 1
    assert component["running"][0]["scheduler_run_id"] == run_id
    assert [item["job_id"] for item in component["scheduled"]] == ["update_series_1"]
    assert component["scheduled_count"] == 1


def test_a_queued_remote_phase_is_not_active_translation(summary_database, quiet_queue):
    from app import activity
    from discover.summary import get_summary
    job = enqueue(quiet_queue, "Translating Example (EN to HU)",
                  "subtitles.tools.translate.main", "translate_subtitles_file",
                  {"media_type": "movie", "radarr_id": 30, "arr_instance_id": 1},
                  status="running", is_progress=True)
    identity = activity.activity_id_for_job(job.job_id)
    activity.register(identity, operation="translation", scope_kind="media")
    activity.note_scope(identity, arr_instance_id=1, media_type="movie", language="hu",
                        title="Example")
    activity.note_remote_submission(identity, service_id="translator", remote_job_id="abc")
    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="queued")

    item = get_summary()["activity"]["running"][0]
    assert item["phase"] == "waiting_for_service"
    assert item["progress"] is None
    assert item["remote"]["phase"] == "queued"

    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="processing",
                               progress=40, total=100)
    item = get_summary()["activity"]["running"][0]
    assert item["phase"] == "running"
    assert item["progress"] == {"unit": "percent", "value": 40, "total": 100}
    assert item["language"] == "hu"
    assert item["arr_instance_id"] == 1


def test_overlapping_representations_are_deduplicated(summary_database, quiet_queue):
    from app import activity
    from discover.summary import get_summary
    job = enqueue(quiet_queue, "Translating Example (EN to HU)",
                  "subtitles.tools.translate.main", "translate_subtitles_file",
                  {"media_type": "movie", "radarr_id": 30}, status="running")
    identity = activity.activity_id_for_job(job.job_id)
    activity.register(identity, operation="translation", scope_kind="media")

    component = get_summary()["activity"]
    assert component["running_count"] == 1
    assert len({item["activity_id"] for item in component["running"]}) == 1


def test_only_recorded_acquisitions_become_arrivals(summary_database, quiet_queue):
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="[]")
    add_movie(session, 200, 1, missing="[]")
    add_episode_history(session, 1, 1, 101, 100, 1, minutes=1)
    add_episode_history(session, 2, 0, 101, 100, 1, minutes=2)
    add_episode_history(session, 3, 5, 101, 100, 1, minutes=3)
    add_episode_history(session, 4, 7, 101, 100, 1, minutes=4)
    add_episode_history(session, 5, 6, 101, 100, 1, minutes=5)
    add_movie_history(session, 1, 4, 200, 1, minutes=6)
    add_movie_history(session, 2, 0, 200, 1, minutes=7)
    session.commit()

    arrivals = get_summary()["arrivals"]
    assert {(item["kind"], item["event_id"]) for item in arrivals} == {
        ("episode", "episode:1"), ("movie", "movie:1")}
    assert all(item["status"] == "success" for item in arrivals)
    assert all(item["action"] in (1, 2, 3, 4) for item in arrivals)


def test_arrivals_retain_exact_title_episode_language_and_time(summary_database, quiet_queue):
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    add_show(session, 100, 1, title="Northern Light")
    add_episode(session, 101, 100, 1, missing="[]", season=2, episode=5, title="Home")
    add_episode_history(session, 1, 2, 101, 100, 1, language="hu:forced", provider="opensubtitles")
    session.commit()

    arrival = get_summary()["arrivals"][0]
    assert arrival["title"] == "Northern Light"
    assert arrival["season"] == 2
    assert arrival["episode"] == 5
    assert arrival["episode_title"] == "Home"
    assert arrival["language"] == "hu:forced"
    assert arrival["provider"] == "opensubtitles"
    assert arrival["instance_name"] == "Main"
    assert arrival["timestamp"].startswith("2026-09-01T")


def test_history_for_unresolvable_media_is_not_an_arrival(summary_database, quiet_queue):
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1)
    add_episode_history(session, 1, 1, None, None, 1)
    session.commit()
    summary = get_summary()
    assert summary["arrivals"] == []
    assert "unresolvable_media" in summary["arrivals_status"]["qualifications"]


def test_partial_and_cancelled_translations_never_become_arrivals(summary_database, quiet_queue):
    from app import activity
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1)
    add_movie(session, 200, 1, missing="[]")
    add_movie_history(session, 1, 6, 200, 1, minutes=1)
    session.commit()

    partial = activity.activity_id_for_job(1)
    activity.register(partial, operation="translation", scope_kind="media")
    activity.note_scope(partial, media_type="movie", movie_id=200, arr_instance_id=1,
                        title="Example", language="hu")
    activity.note_publication(partial, outcome="partial", detail="18 cues untranslated")
    activity.finish(partial, outcome="success")

    cancelled = activity.activity_id_for_job(2)
    activity.register(cancelled, operation="translation", scope_kind="media")
    activity.note_publication(cancelled, outcome="cancelled")
    activity.finish(cancelled, outcome="success")

    assert get_summary()["arrivals"] == []

    succeeded = activity.activity_id_for_job(3)
    activity.register(succeeded, operation="translation", scope_kind="media")
    activity.note_scope(succeeded, media_type="movie", movie_id=200, arr_instance_id=1,
                        title="Example", language="hu")
    activity.note_publication(succeeded, outcome="success")
    activity.finish(succeeded, outcome="success")

    arrivals = get_summary()["arrivals"]
    assert [item["kind"] for item in arrivals] == ["translation"]
    assert arrivals[0]["status"] == "success"
    assert arrivals[0]["language"] == "hu"
    assert arrivals[0]["title"] == "Example"


def test_attention_names_the_capability_instance_and_one_recovery(summary_database, quiet_queue,
                                                                  monkeypatch):
    import sys
    from app import get_providers
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    add_instance(session, 2, name="Anime", is_default=0)
    from app.database import TableShowsRootfolder
    session.add(TableShowsRootfolder(local_rootfolder_id=1, arr_instance_id=2,
                                     upstream_rootfolder_id=3, id=3, accessible=0,
                                     error="No such directory", path="/anime"))
    session.commit()

    monkeypatch.setattr(get_providers, "tp", {
        "example": ("AuthenticationError", dt.datetime.now() + dt.timedelta(minutes=30), "30 minutes"),
        "expired": ("DownloadLimitExceeded", dt.datetime.now() - dt.timedelta(minutes=1), "gone"),
    })
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["example", "expired", "healthy"])
    monkeypatch.setattr(get_providers, "set_throttled_providers",
                        lambda *args, **kwargs: pytest.fail("summary must not write throttle state"))

    # A live feed is only observed for a library this install actually runs.
    from app.config import settings
    monkeypatch.setattr(settings.general, "use_sonarr", True)
    monkeypatch.setattr(settings.general, "use_radarr", True)
    sys.modules["app.signalr_client"] = SimpleNamespace(
        sonarr_signalr_client=SimpleNamespace(arr_instance_id=None, connected=True),
        radarr_signalr_client=SimpleNamespace(arr_instance_id=None, connected=True),
        _sonarr_signalr_clients=[SimpleNamespace(arr_instance_id=2, connected=False)],
        _radarr_signalr_clients=[])
    try:
        attention = get_summary()["attention"]
    finally:
        del sys.modules["app.signalr_client"]

    by_capability = {item["capability"]: item for item in attention["items"]}
    assert set(by_capability) == {"library_sync", "library_paths", "subtitle_providers"}
    for item in attention["items"]:
        assert item["recovery"]["label"] and item["recovery"]["target"].startswith("/")
        assert item["summary"]
    assert by_capability["library_sync"]["scope"]["arr_instance_id"] == 2
    assert by_capability["library_sync"]["scope"]["instance_name"] == "Anime"
    assert by_capability["library_paths"]["scope"]["arr_instance_id"] == 2
    assert by_capability["library_paths"]["freshness"] == "last_recorded_observation"
    providers = by_capability["subtitle_providers"]
    assert providers["scope"]["providers"] == ["example"]
    assert providers["scope"]["alternatives"] is True
    assert "every" not in providers["summary"].lower()


def test_an_unavailable_component_stays_unknown_and_isolates_the_rest(summary_database,
                                                                      quiet_queue, monkeypatch):
    from discover import summary as summary_module
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en']")
    add_episode_history(session, 1, 1, 101, 100, 1)
    session.commit()

    def broken(*args, **kwargs):
        raise sa.exc.OperationalError("select", {}, Exception("wanted aggregate timed out"))

    monkeypatch.setattr(summary_module, "_wanted_component", broken)
    summary = summary_module.get_summary()

    assert summary["wanted"]["availability"] == "unknown"
    assert summary["wanted"]["requirements"] is None
    assert summary["wanted"].get("complete") is False
    assert summary["arrivals"], "one failed component must not silence the others"
    assert summary["activity"]["availability"] == "available"
    assert summary["state"] == "unknown"


def test_a_stale_cached_aggregate_keeps_its_own_observation_time(summary_database, quiet_queue,
                                                                 monkeypatch):
    from discover import summary as summary_module
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en']")
    session.commit()

    first = summary_module.get_summary()
    assert first["wanted"]["requirements"] == 1
    add_episode(session, 102, 100, 1, missing="['en', 'hu']", upstream=21, episode=2)
    session.commit()

    _, statements = counted(summary_database.engine, summary_module.get_summary)
    assert not any("missing_subtitles" in statement for statement in statements), \
        "the wanted aggregate is cached briefly rather than rerun per request"
    second = summary_module.get_summary()
    assert second["wanted"]["requirements"] == 1
    assert second["wanted"]["observed_at"] == first["wanted"]["observed_at"]
    assert second["generated_at"] >= first["generated_at"]

    summary_module.reset_cache()
    refreshed = summary_module.get_summary()
    assert refreshed["wanted"]["requirements"] == 3
    assert refreshed["wanted"]["observed_at"] > first["wanted"]["observed_at"]


def test_optional_library_and_translator_are_onboarding_not_failure(summary_database, quiet_queue,
                                                                    monkeypatch):
    import sys
    from app.config import settings
    from discover.summary import get_summary
    monkeypatch.setattr(settings.translator, "openrouter_url", "")
    # The SignalR module is present and unstarted on every running Bazarr, so
    # this criterion has to be asserted against that shape rather than against
    # a fixture that removed the module the production process always has.
    monkeypatch.setitem(sys.modules, "app.signalr_client", _started_signalr())
    summary = get_summary()
    assert summary["state"] == "new_installation"
    assert summary["attention"]["items"] == []
    assert {item["id"] for item in summary["onboarding"]["items"]} == {"first_run", "library",
                                                                       "translator"}
    for item in summary["onboarding"]["items"]:
        assert item["target"].startswith("/")
    assert summary["wanted"]["requirements"] == 0
    assert summary["wanted"]["complete"] is True


@pytest.mark.parametrize("configured", ["setup_complete", "use_sonarr", "provider"])
def test_first_run_guide_is_offered_only_while_nothing_is_configured(summary_database, quiet_queue,
                                                                     monkeypatch, configured):
    """The first-run wizard is a hidden route with no redirect to it any more.

    Discover is the default entry, so the only way the guide stays reachable is
    for the summary to offer it while it still applies. Any single sign of prior
    configuration retires the offer, exactly as the wizard's own rule does.
    """
    import sys
    from app.config import settings
    from discover.summary import get_summary
    monkeypatch.setitem(sys.modules, "app.signalr_client", _started_signalr())
    fresh = get_summary()
    guide = [item for item in fresh["onboarding"]["items"] if item["id"] == "first_run"]
    assert len(guide) == 1 and guide[0]["target"] == "/setup"
    assert fresh["onboarding"]["items"][0]["id"] == "first_run"

    if configured == "setup_complete":
        monkeypatch.setattr(settings.general, "setup_complete", True)
    elif configured == "use_sonarr":
        monkeypatch.setattr(settings.general, "use_sonarr", True)
    else:
        monkeypatch.setattr(settings.general, "enabled_providers", ["catalog-example"])
    settled = get_summary()
    assert [item for item in settled["onboarding"]["items"] if item["id"] == "first_run"] == []


def test_summary_never_calls_mutating_health_throttle_or_indexer_paths(summary_database,
                                                                      quiet_queue, monkeypatch):
    from discover.summary import get_summary
    from app import get_providers
    from utilities import health
    from subtitles.indexer import movies as movie_indexer
    from subtitles.indexer import series as series_indexer
    monkeypatch.setattr(health, "check_health",
                        lambda *args, **kwargs: pytest.fail("health check mutates state"))
    monkeypatch.setattr(get_providers, "list_throttled_providers",
                        lambda *args, **kwargs: pytest.fail("throttle listing prunes state"))
    monkeypatch.setattr(get_providers, "update_throttled_provider",
                        lambda *args, **kwargs: pytest.fail("throttle pruning writes state"))
    monkeypatch.setattr(movie_indexer, "list_missing_subtitles_movies",
                        lambda *args, **kwargs: pytest.fail("indexers must not run on a read"))
    monkeypatch.setattr(series_indexer, "list_missing_subtitles",
                        lambda *args, **kwargs: pytest.fail("indexers must not run on a read"))
    assert get_summary()["activity"]["availability"] == "available"


def test_summary_preserves_settings_and_reads_are_bounded_under_load(summary_database,
                                                                     quiet_queue):
    from app.config import settings
    from discover.summary import get_summary, QUERY_BUDGET, HISTORY_CANDIDATE_LIMIT
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en']")
    for index in range(120):
        add_episode_history(session, index + 1, 7 if index % 2 else 1, 101, 100, 1,
                            minutes=index + 1)
    session.commit()

    before_settings = settings.as_dict()
    before_rows = rows(session)
    summary, statements = counted(summary_database.engine, get_summary)

    assert len(statements) <= QUERY_BUDGET
    assert len(summary["arrivals"]) <= summary["arrivals_status"]["display_limit"]
    assert summary["arrivals_status"]["candidate_limit"] == HISTORY_CANDIDATE_LIMIT
    assert all(item["action"] != 7 for item in summary["arrivals"])
    assert settings.as_dict() == before_settings
    assert rows(session) == before_rows


def test_late_and_out_of_order_observations_never_downgrade_fresher_data(summary_database,
                                                                        quiet_queue):
    from app import activity
    from discover.summary import get_summary
    job = enqueue(quiet_queue, "Translating Example (EN to HU)",
                  "subtitles.tools.translate.main", "translate_subtitles_file",
                  {"media_type": "movie", "radarr_id": 30}, status="running")
    identity = activity.activity_id_for_job(job.job_id)
    activity.register(identity, operation="translation", scope_kind="media")
    activity.note_remote_submission(identity, service_id="translator", remote_job_id="abc")
    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="processing",
                               progress=80, total=100, observed_at=EPOCH + dt.timedelta(minutes=5))
    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="queued",
                               observed_at=EPOCH)
    item = get_summary()["activity"]["running"][0]
    assert item["remote"]["phase"] == "processing"
    assert item["progress"] == {"unit": "percent", "value": 80, "total": 100}


def test_summary_endpoint_is_authenticated_and_independent_of_metadata(summary_database,
                                                                       quiet_queue, monkeypatch):
    from app.config import settings
    from discover.summary import get_summary
    monkeypatch.setattr(settings.discover, "tmdb_access_token", "")
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en']")
    session.commit()

    summary = get_summary()
    assert summary["wanted"]["requirements"] == 1
    assert summary["wanted"]["availability"] == "available"

    from api.discover import api_ns_list_discover
    from api.discover.summary import api_ns_discover_summary, DiscoverSummary
    assert api_ns_discover_summary in api_ns_list_discover
    # The route reuses the shared authenticate decorator rather than a bespoke gate.
    assert DiscoverSummary.get.__wrapped__.__qualname__ == "DiscoverSummary.get"


def test_a_synchronous_search_is_running_work_with_a_readable_name(summary_database, quiet_queue):
    from app import activity
    from discover.summary import get_summary
    with activity.observed_operation("discover_search", scope_kind="request",
                                     language="hu", title="Northern Light"):
        component = get_summary()["activity"]
        assert component["running_count"] == 1
        item = component["running"][0]
        assert item["operation"] == "discover_search"
        assert item["name"] == "Searching subtitle providers: Northern Light"
        assert item["language"] == "hu"
        assert item["scope_kind"] == "request"
    assert get_summary()["activity"]["running_count"] == 0


def test_an_unused_library_is_never_reported_as_a_disconnected_live_sync(summary_database,
                                                                         quiet_queue,
                                                                         monkeypatch):
    """T11-I1. The module is present and unstarted on every install."""
    import sys
    from app.config import settings
    from discover.summary import get_summary
    monkeypatch.setattr(settings.general, "use_sonarr", False)
    monkeypatch.setattr(settings.general, "use_radarr", False)
    monkeypatch.setattr(settings.translator, "openrouter_url", "")
    monkeypatch.setitem(sys.modules, "app.signalr_client", _started_signalr())

    summary = get_summary()
    assert summary["attention"]["items"] == []
    assert {item["id"] for item in summary["onboarding"]["items"]} == {"first_run", "library",
                                                                       "translator"}
    assert summary["state"] == "new_installation"


def test_only_the_library_this_install_runs_is_observed(summary_database, quiet_queue,
                                                        monkeypatch):
    """T11-I1. A Sonarr-only install must not be told Radarr is disconnected."""
    import sys
    from app.config import settings
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    session.commit()
    monkeypatch.setattr(settings.general, "use_sonarr", True)
    monkeypatch.setattr(settings.general, "use_radarr", False)
    monkeypatch.setitem(sys.modules, "app.signalr_client", _started_signalr())

    items = get_summary()["attention"]["items"]
    assert [item["scope"]["kind"] for item in items] == ["sonarr"]
    assert "Sonarr" in items[0]["summary"]


def test_signalr_disabled_at_startup_reports_no_live_feed_failure(summary_database, quiet_queue,
                                                                  monkeypatch):
    """T11-I1. --no-signalr means nothing is watching, not that everything failed."""
    import sys
    from app.config import settings
    from app.get_args import args
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    session.commit()
    monkeypatch.setattr(settings.general, "use_sonarr", True)
    monkeypatch.setattr(settings.general, "use_radarr", True)
    monkeypatch.setattr(args, "no_signalr", True, raising=False)
    monkeypatch.setitem(sys.modules, "app.signalr_client", _started_signalr())

    assert get_summary()["attention"]["items"] == []


def test_running_count_is_the_whole_queue_not_the_bounded_sample(summary_database, quiet_queue,
                                                                 monkeypatch):
    """T11-I2. More running jobs than the sample limit is ordinary configuration."""
    from app.config import settings
    from discover.summary import ACTIVITY_SAMPLE_LIMIT, get_summary
    total = ACTIVITY_SAMPLE_LIMIT + 3
    monkeypatch.setattr(settings.general, "concurrent_jobs", total)
    for index in range(total):
        enqueue(quiet_queue, f"Manually downloading Subtitles {index}", "subtitles.manual",
                "movie_manually_download_specific_subtitle", {"radarr_id": index},
                status="running")
    enqueue(quiet_queue, "Manually downloading Subtitles", "subtitles.manual",
            "movie_manually_download_specific_subtitle", {"radarr_id": 99})

    component = get_summary()["activity"]
    assert component["running_count"] == total
    assert len(component["running"]) == ACTIVITY_SAMPLE_LIMIT
    assert component["truncated"] is True
    assert component["queued_count"] == 1


def test_a_synchronous_search_still_counts_beside_a_truncated_queue(summary_database,
                                                                    quiet_queue, monkeypatch):
    """T11-I2. The count is the queue truth plus work that never enters it."""
    from app import activity
    from app.config import settings
    from discover.summary import ACTIVITY_SAMPLE_LIMIT, get_summary
    total = ACTIVITY_SAMPLE_LIMIT + 2
    monkeypatch.setattr(settings.general, "concurrent_jobs", total)
    for index in range(total):
        enqueue(quiet_queue, f"Manually downloading Subtitles {index}", "subtitles.manual",
                "movie_manually_download_specific_subtitle", {"radarr_id": index},
                status="running")
    with activity.observed_operation("discover_search", scope_kind="request", language="hu"):
        assert get_summary()["activity"]["running_count"] == total + 1


def test_a_just_published_translation_outranks_older_history_east_of_utc(summary_database,
                                                                         quiet_queue,
                                                                         monkeypatch):
    """T11-I3. History is naive local; an observed publication is aware UTC."""
    import time as time_module
    from app import activity
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    add_movie(session, 200, 1, missing="[]")
    # An hour older than the translation, in the host's own local convention.
    from app.database import TableHistoryMovie
    session.add(TableHistoryMovie(id=1, action=1, movie_id=200, arr_instance_id=1, radarrId=30,
                                  description="Downloaded", language="en", provider="example",
                                  timestamp=dt.datetime.now() - dt.timedelta(hours=1),
                                  video_path="/movies/1.mkv"))
    session.commit()

    monkeypatch.setenv("TZ", "Europe/Budapest")
    time_module.tzset()
    try:
        identity = activity.activity_id_for_job(1)
        activity.register(identity, operation="translation", scope_kind="media")
        activity.note_scope(identity, media_type="movie", movie_id=200, arr_instance_id=1,
                            title="Example", language="hu")
        activity.note_publication(identity, outcome="success")
        activity.finish(identity, outcome="success")

        arrivals = get_summary()["arrivals"]
        assert [item["kind"] for item in arrivals] == ["translation", "movie"]
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time_module.tzset()


def test_a_half_resolved_history_row_is_not_an_exact_arrival(summary_database, quiet_queue):
    """T11-M3. A surviving show with a deleted episode has no exact identity."""
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode_history(session, 1, 1, None, 100, 1)
    session.commit()

    summary = get_summary()
    assert summary["arrivals"] == []
    assert "unresolvable_media" in summary["arrivals_status"]["qualifications"]


def test_a_budget_breach_keeps_the_last_good_aggregate_as_stale(summary_database, quiet_queue,
                                                               monkeypatch):
    """T11-M2. A transient breach must not discard a good reading."""
    from discover import summary as summary_module
    session = summary_database.session
    add_instance(session, 1)
    add_show(session, 100, 1)
    add_episode(session, 101, 100, 1, missing="['en', 'hu']")
    session.commit()

    first = summary_module.get_summary()["wanted"]
    assert first["requirements"] == 2
    # Expire the brief cache without discarding its value, which is the state a
    # real breach happens in.
    summary_module._wanted_cache["expires"] = 0.0

    def breach(*args, **kwargs):
        raise summary_module._AggregateBudgetExceeded("episode")

    monkeypatch.setattr(summary_module, "_wanted_component", breach)
    wanted = summary_module.get_summary()["wanted"]
    assert wanted["availability"] == "stale"
    assert wanted["requirements"] == 2
    assert "group_budget_exceeded" in wanted["qualifications"]


def test_inaccessible_rootfolder_groups_are_read_in_a_deterministic_order(summary_database,
                                                                          quiet_queue,
                                                                          monkeypatch):
    """T11-M4. A bounded read without an order leaves the engine to choose."""
    from app.database import TableShowsRootfolder
    from discover import summary as summary_module
    session = summary_database.session
    for owner in range(1, 6):
        add_instance(session, owner, name=f"Instance {owner}", is_default=1 if owner == 1 else 0)
    for owner in (5, 3, 1, 4, 2):
        session.add(TableShowsRootfolder(local_rootfolder_id=owner, arr_instance_id=owner,
                                         upstream_rootfolder_id=owner, id=owner, accessible=0,
                                         error="No such directory", path=f"/library/{owner}"))
    session.commit()
    monkeypatch.setattr(summary_module, "ROOTFOLDER_GROUP_LIMIT", 3)

    # Both engines happen to return these grouped rows in key order, so the
    # returned owners alone cannot prove the bound is deterministic. The
    # statement itself can: a LIMIT without an ORDER BY leaves the choice of
    # surviving rows to the engine, and that is what has to be absent.
    with summary_module._connection() as connection:
        groups, statements = counted(
            summary_database.engine,
            lambda: summary_module._inaccessible_rootfolders(connection))
    assert [group["arr_instance_id"] for group in groups] == [1, 2, 3]
    assert len(statements) == 2
    for statement in statements:
        assert "ORDER BY" in statement.upper()
        assert "LIMIT" in statement.upper() or "FETCH FIRST" in statement.upper()


def test_a_server_wide_scan_never_claims_a_single_instance(summary_database, quiet_queue):
    """T11-M6. A cross-instance bulk search touches every owner in turn."""
    from app import activity
    from discover.summary import get_summary
    session = summary_database.session
    add_instance(session, 1, name="Main")
    add_instance(session, 2, name="Anime", is_default=0)
    session.commit()
    job = enqueue(quiet_queue, "Searching for missing movies subtitles", "subtitles.wanted.movies",
                  "wanted_search_missing_subtitles_movies", {"wait_for_completion": False},
                  status="running", is_progress=True)
    identity = activity.activity_id_for_job(job.job_id)
    activity.register(identity, operation="wanted_search", scope_kind="server")
    activity.note_scope(identity, media_type="movie", title="Second instance movie")
    # Even a stale owner recorded by some other caller must not scope the scan.
    activity.note_scope(identity, arr_instance_id=2)

    item = get_summary()["activity"]["running"][0]
    assert item["scope_kind"] == "server"
    assert item["arr_instance_id"] is None
    assert item["instance_name"] is None
    assert item["title"] == "Second instance movie"


def test_the_throttle_summary_agrees_in_number(summary_database, quiet_queue, monkeypatch):
    """T11-M1. Both clauses are composed against their own count."""
    from app import get_providers
    from discover.summary import get_summary
    live = dt.datetime.now() + dt.timedelta(minutes=30)

    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["a", "b", "c"])
    monkeypatch.setattr(get_providers, "tp", {"a": ("AuthenticationError", live, "30 minutes")})
    single = next(item for item in get_summary()["attention"]["items"]
                  if item["capability"] == "subtitle_providers")
    assert single["summary"] == ("1 of 3 providers is cooling down. "
                                 "Searches still use the other 2.")
    assert single["detail"] == "a: sign-in was rejected"
    assert single["recovery"]["label"] == "Fix provider sign-in"

    monkeypatch.setattr(get_providers, "tp", {"a": ("AuthenticationError", live, "30 minutes"),
                                              "b": ("DownloadLimitExceeded", live, "30 minutes")})
    plural = next(item for item in get_summary()["attention"]["items"]
                  if item["capability"] == "subtitle_providers")
    assert plural["summary"] == ("2 of 3 providers are cooling down. "
                                 "Searches still use the other one.")
    # Two different causes have no single recovery, so the label stays general,
    # and neither exception name reaches the reader.
    assert plural["detail"] == ("a: sign-in was rejected; "
                                "b: the daily download limit is used up")
    assert plural["recovery"]["label"] == "Review provider status"

    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["a"])
    only = next(item for item in get_summary()["attention"]["items"]
                if item["capability"] == "subtitle_providers")
    assert only["summary"] == ("1 of 1 provider is cooling down. "
                               "No other provider is enabled.")


# Every reason the incumbent throttle map can produce, plus shapes it cannot
# predict. The stored reason is the class name of whatever threw, so this list
# is a sample of an open set, not a specification of it.
_THROTTLE_REASONS = [
    "AuthenticationError", "APIThrottled", "ParseResponseError", "ReadTimeout",
    "ConnectTimeout", "Timeout", "PermissionError", "ProxyError", "ConnectionError",
    "SearchLimitReached", "ForbiddenError", "DownloadLimitExceeded", "TooManyRequests",
    "ServiceUnavailable", "ProviderError", "ConfigurationError", "IPAddressBlocked",
    "CaptchaRequired", "SomethingBrandNewError", "ValueError", "OSError",
]


@pytest.mark.parametrize("reason", _THROTTLE_REASONS)
def test_no_provider_exception_name_can_reach_the_reader(summary_database, quiet_queue,
                                                         monkeypatch, reason):
    """No class name, for any reason, recognised or not.

    The stored reason is `exception.__class__.__name__`, which is unbounded, so
    a map can never cover it. An unrecognised reason gets an honest default
    rather than the identifier, and the identifier is kept in the machine
    readable scope so nothing is lost.
    """
    from app import get_providers
    from discover.summary import get_summary
    live = dt.datetime.now() + dt.timedelta(minutes=30)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["a", "b"])
    monkeypatch.setattr(get_providers, "tp", {"a": (reason, live, "30 minutes")})
    item = next(row for row in get_summary()["attention"]["items"]
                if row["capability"] == "subtitle_providers")
    reader_text = " ".join([item["summary"], item["detail"], item["recovery"]["label"]])
    assert reason not in reader_text, reader_text
    # Nothing that looks like a class identifier, whatever the reason was.
    assert not re.search(r"\b\w*(Error|Exception|Timeout|Throttled|Reached|Blocked|Required)\b",
                         reader_text), reader_text
    assert item["detail"].startswith("a: ") and len(item["detail"]) > len("a: ")
    # The exact reason survives for diagnosis, outside the reader's copy.
    assert item["scope"]["reasons"] == {"a": reason.strip()}


def test_a_provider_with_no_recorded_reason_raises_no_attention_item(summary_database, quiet_queue,
                                                                    monkeypatch):
    """An entry with no reason is not a throttle worth reporting, so there is
    nothing for the reader to misread."""
    from app import get_providers
    from discover.summary import get_summary
    live = dt.datetime.now() + dt.timedelta(minutes=30)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["a", "b"])
    monkeypatch.setattr(get_providers, "tp", {"a": ("", live, "30 minutes")})
    assert [row for row in get_summary()["attention"]["items"]
            if row["capability"] == "subtitle_providers"] == []


def test_recognised_provider_reasons_read_as_their_own_phrase(summary_database, quiet_queue,
                                                              monkeypatch):
    from app import get_providers
    from discover.summary import get_summary
    live = dt.datetime.now() + dt.timedelta(minutes=30)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["a", "b"])
    monkeypatch.setattr(get_providers, "tp", {"a": ("AuthenticationError", live, "30 minutes")})
    known = next(row for row in get_summary()["attention"]["items"]
                 if row["capability"] == "subtitle_providers")
    assert known["detail"] == "a: sign-in was rejected"
    assert known["recovery"]["label"] == "Fix provider sign-in"
    monkeypatch.setattr(get_providers, "tp", {"a": ("SomethingBrandNewError", live, "30 minutes")})
    unknown = next(row for row in get_summary()["attention"]["items"]
                   if row["capability"] == "subtitle_providers")
    assert unknown["detail"] == "a: the provider reported a problem"
    assert unknown["recovery"]["label"] == "Review provider status"
