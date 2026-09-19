"""Profile jobs use real sports files, both database engines and real writers."""

# ruff: noqa: F401, F811
import ast
import json
from collections import deque
from types import SimpleNamespace

import pytest
import pysubs2
import sqlalchemy as sa
from subtitles.tools.subsync_engines import SubtitleSourceChanged
from subtitles.tools.translate.services.lingarr_translator import LingarrAuthError

from test_sportarr_manual import manual_library, indexed_library, migration_engine


@pytest.fixture
def profile_library(manual_library, monkeypatch):
    from app import database as db
    from app.config import settings
    from app.jobs_queue import jobs_queue
    from app.event_handler import socketio
    from sportarr import profile_hooks, workflows
    from subtitles.indexer import sports
    from subtitles.tools.translate.services import (
        google_translator,
        gemini_translator,
        lingarr_translator,
        openrouter_translator,
    )

    service, session, folder = manual_library
    monkeypatch.setattr(profile_hooks, "database", session)
    monkeypatch.setattr(socketio, "emit", lambda *a, **k: None)
    monkeypatch.setattr(workflows, "database", session)
    events = []
    monkeypatch.setattr(sports, "notify", lambda ids: events.extend(ids))

    def native_history(*a, **k):
        raise AssertionError("Sports must not dispatch native history")

    for module in (
        google_translator,
        gemini_translator,
        lingarr_translator,
        openrouter_translator,
    ):
        monkeypatch.setattr(module, "history_log", native_history)
        monkeypatch.setattr(module, "history_log_movie", native_history)
    monkeypatch.setattr(settings.translator, "translator_info", False)
    monkeypatch.setattr(settings.translator, "min_source_score", 0)
    monkeypatch.setattr(settings.translator, "default_score", 80)
    monkeypatch.setattr(settings.translator, "gemini_keys", ["fixture"])
    monkeypatch.setattr(settings.translator, "openrouter_url", "http://fixture")
    monkeypatch.setattr(settings.translator, "lingarr_url", "http://fixture/base")
    monkeypatch.setattr(settings.translator, "lingarr_token", "")
    monkeypatch.setattr(settings.translator, "openrouter_api_key", "")
    monkeypatch.setattr(settings.translator, "openrouter_encryption_key", "")
    for queue in (
        "jobs_pending_queue",
        "jobs_running_queue",
        "jobs_failed_queue",
        "jobs_completed_queue",
    ):
        monkeypatch.setattr(jobs_queue, queue, deque())
    items = [
        dict(
            id=i,
            language=lang,
            hi="False",
            forced="False",
            translate_from="en" if lang == "hu" else None,
        )
        for i, lang in enumerate(("en", "hu", "de"), 1)
    ]
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=1,
            name="Sports",
            items=json.dumps(items),
            combine=json.dumps({"languages": ["en", "hu"], "format": "srt"}),
        )
    )
    session.execute(
        sa.update(db.TableSportsLeagues)
        .where(db.TableSportsLeagues.id == 51)
        .values(profileId=1)
    )
    db.update_profile_id_list.invalidate()
    session.execute(
        sa.update(db.TableSettingsLanguages)
        .where(db.TableSettingsLanguages.code2 == "hu")
        .values(enabled=1)
    )
    source = folder / "1/event.en.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nThe final lap.\n\n"
        "2\n00:00:01,100 --> 00:00:02,000\nThe winner arrives.\n"
    )
    sports.store_subtitles_sports(61, 1)
    context = service.resolve_event_in_session(session, 61, 1)
    base = profile_hooks.capture_profile_operation(
        context, service.candidate_signature(context)
    )
    yield SimpleNamespace(
        hooks=profile_hooks,
        service=service,
        session=session,
        folder=folder,
        source=source,
        base=base,
        queue=jobs_queue,
        items=items,
        events=events,
    )
    with jobs_queue._progress_buffer_lock:
        jobs_queue._progress_buffer.clear()


def transport(monkeypatch, engine, phase="complete", during=None):
    import requests
    from subtitles.tools.translate.services import google_translator, gemini_translator

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if during:
            during()
        if engine == "lingarr":
            assert url == "http://fixture/base/api/translate/line"
            assert set(kwargs["json"]) <= {
                "subtitleLine",
                "sourceLanguage",
                "targetLanguage",
                "contextLinesBefore",
                "contextLinesAfter",
            }
            if phase == "fail" or (phase == "partial" and len(calls) == 2):
                return SimpleNamespace(status_code=400)
            return SimpleNamespace(
                status_code=200,
                text="" if phase == "empty" else "A végső kör.",
                headers={"Content-Type": "text/plain"},
            )
        payload = kwargs["json"]
        assert "arrMediaId" not in payload and "mediaType" not in payload
        if "/jobs/" in url:
            return SimpleNamespace(status_code=404)
        if phase == "fail":
            return SimpleNamespace(status_code=400, text="fixture failure")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "lines": [
                    {"position": i, "line": "A végső kör."}
                    for i in range(
                        0 if phase == "empty" else 1 if phase == "partial" else 2
                    )
                ]
            },
        )

    def google(self, text):
        if during:
            during()
        calls.append(text)
        if phase == "fail" or (phase == "partial" and "winner" in text):
            from deep_translator.exceptions import TranslationNotFound

            raise TranslationNotFound(text)
        if phase == "empty":
            return ""
        return "A végső kör."

    def gemini(method, url, **kwargs):
        if during:
            during()
        calls.append(url)
        payload = json.loads(kwargs["data"])
        batch = json.loads(payload["contents"][0]["parts"][0]["text"])
        if phase == "fail":
            raise ValueError("fixture transport failure")
        if phase == "partial":
            batch = batch[:1]
        if phase == "empty":
            batch = []
        response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    [
                                        {
                                            "index": item["index"],
                                            "content": "A végső kör.",
                                        }
                                        for item in batch
                                    ]
                                )
                            }
                        ]
                    }
                }
            ]
        }
        return SimpleNamespace(text=json.dumps(response), raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "request", gemini)
    monkeypatch.setattr(google_translator.GoogleTranslator, "translate", google)
    return calls


def enqueue(library):
    from subtitles.processing import _trigger_auto_translation

    _trigger_auto_translation(
        "en",
        str(library.source),
        library.base.context.mapped_path,
        "sports",
        source_score_percent=100,
        sports_operation=library.base,
    )
    return library.queue.jobs_pending_queue


def run_job(library, job):
    from subtitles.tools.translate.main import translate_subtitles_file

    library.queue.jobs_pending_queue.remove(job)
    library.queue.jobs_running_queue.append(job)
    job.status = "running"
    return translate_subtitles_file(**(job.kwargs | {"job_id": job.job_id}))


@pytest.mark.parametrize(
    "engine", ["google_translate", "gemini", "lingarr", "openrouter"]
)
def test_real_queue_engine_history_and_combine(profile_library, monkeypatch, engine):
    from app.config import settings
    from app.database import TableHistorySports, TableSportsEvents

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", engine)
    calls = transport(monkeypatch, engine)
    jobs = enqueue(lib)
    assert len(jobs) == 1
    enqueue(lib)
    assert len(jobs) == 1, (
        "Identical immutable operations must deduplicate in the real queue"
    )
    job = jobs[0]
    run_job(lib, job)
    target = lib.folder / "1/event.hu.srt"
    actual = pysubs2.load(str(target))
    assert [(cue.start, cue.end) for cue in actual] == [(0, 1000), (1100, 2000)]
    assert all(cue.plaintext == "A végső kör." for cue in actual)
    assert calls
    row = lib.session.execute(sa.select(TableHistorySports)).scalar_one()
    assert (
        row.event_id,
        row.arr_instance_id,
        row.action,
        row.score,
        row.score_out_of,
    ) == (61, 1, 6, 144, 180)
    assert row.artifact and row.provider is None and row.subs_id is None
    assert row.subtitles_path == "/sports/event.hu.srt"
    assert (lib.folder / "1/event.en.combined-hu.srt").is_file()
    lib.session.expire_all()
    assert "hu" not in ast.literal_eval(
        lib.session.get(TableSportsEvents, 61).missing_subtitles
    )
    assert lib.session.get(TableSportsEvents, 62).subtitles == "[]"
    assert not (lib.folder / "2/event.hu.srt").exists()
    assert lib.events and set(lib.events) == {61}


@pytest.mark.parametrize(
    "change", ["source", "media", "mapping", "owner", "profile", "rule", "destination"]
)
def test_queued_snapshot_rejects_changed_input(profile_library, monkeypatch, change):
    from app.config import settings
    from app.database import (
        TableArrInstances,
        TableSportsLeagues,
        TableLanguagesProfiles,
    )

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    job = enqueue(lib)[0]
    if change == "source":
        lib.source.write_text("new source")
    elif change == "media":
        with open(lib.base.context.mapped_path, "ab") as stream:
            stream.write(b"new video")
    elif change == "mapping":
        lib.session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(path_mappings="[]")
        )
    elif change == "owner":
        lib.session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(enabled=0)
        )
    elif change == "profile":
        lib.session.execute(
            sa.update(TableSportsLeagues)
            .where(TableSportsLeagues.id == 51)
            .values(profileId=None)
        )
    elif change == "rule":
        lib.session.execute(sa.update(TableLanguagesProfiles).values(items="[]"))
    else:
        (lib.folder / "1/event.hu.srt").write_text("new destination")
    calls = transport(monkeypatch, "lingarr")
    with pytest.raises((ValueError, RuntimeError, OSError, SubtitleSourceChanged)):
        run_job(lib, job)
    assert not calls, (
        "Queued source, destination, owner and profile validation precedes transport"
    )


@pytest.mark.parametrize(
    "engine", ["google_translate", "gemini", "lingarr", "openrouter"]
)
@pytest.mark.parametrize("phase", ["partial", "empty", "fail"])
def test_engine_output_accounting(profile_library, monkeypatch, engine, phase):
    from app.config import settings
    from app.database import TableHistorySports

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", engine)
    transport(monkeypatch, engine, phase)
    job = enqueue(lib)[0]
    source = lib.source.read_bytes()
    partial = phase == "partial" and engine != "gemini"
    if partial:
        run_job(lib, job)
        row = lib.session.execute(sa.select(TableHistorySports)).scalar_one()
        assert row.action == 6 and "Partial result" in row.description
        assert "Partially translated" in job.job_name
        cues = pysubs2.load(str(lib.folder / "1/event.hu.srt"))
        assert cues[0].plaintext == "A végső kör."
        assert cues[1].plaintext == "The winner arrives."
    else:
        with pytest.raises((ValueError, RuntimeError, OSError, LingarrAuthError)):
            run_job(lib, job)
        assert not (lib.folder / "1/event.hu.srt").exists()
        assert not lib.session.execute(sa.select(TableHistorySports)).all()
    assert lib.source.read_bytes() == source


def test_google_mixed_whitespace_keeps_source_cue(profile_library, monkeypatch):
    from app.config import settings
    from app.database import TableHistorySports, TableSportsEvents
    from subtitles.tools.translate.services.google_translator import GoogleTranslator

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    monkeypatch.setattr(
        GoogleTranslator,
        "translate",
        lambda self, text: "A final lap." if "final lap" in text else "   ",
    )
    source = lib.source.read_bytes()
    job = enqueue(lib)[0]
    run_job(lib, job)
    cues = pysubs2.load(str(lib.folder / "1/event.hu.srt"))
    assert [cue.plaintext for cue in cues] == [
        "A final lap.",
        "The winner arrives.",
    ]
    assert [(cue.start, cue.end) for cue in cues] == [(0, 1000), (1100, 2000)]
    row = lib.session.execute(sa.select(TableHistorySports)).scalar_one()
    assert (row.event_id, row.arr_instance_id, row.action) == (61, 1, 6)
    assert "Partial result" in row.description
    assert "No translated text was returned for 1 cues." in row.description
    assert "Partially translated" in job.job_name
    assert lib.source.read_bytes() == source
    combined = pysubs2.load(str(lib.folder / "1/event.en.combined-hu.srt"))
    assert "The winner arrives." in combined[1].plaintext
    lib.session.expire_all()
    assert "hu" not in ast.literal_eval(
        lib.session.get(TableSportsEvents, 61).missing_subtitles
    )
    assert not (lib.folder / "2/event.hu.srt").exists()


def independent_update(lib, statement):
    """A separate connection has a bounded wait, including SQLite AUTOCOMMIT."""
    from concurrent.futures import ThreadPoolExecutor

    def execute():
        with lib.session.get_bind().connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA busy_timeout=80")
            else:
                connection.exec_driver_sql("SET lock_timeout = '80ms'")
            connection.execute(statement)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(execute).result(timeout=2)


def change_input(lib, change):
    from app.database import (
        TableArrInstances,
        TableSportsLeagues,
        TableLanguagesProfiles,
    )

    if change == "source":
        lib.source.write_text("new source")
    elif change == "media":
        with open(lib.base.context.mapped_path, "ab") as stream:
            stream.write(b"new video")
    elif change == "destination":
        (lib.folder / "1/event.hu.srt").write_text("new destination")
    else:
        statements = {
            "owner": sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(enabled=0),
            "mapping": sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(path_mappings="[]"),
            "profile": sa.update(TableSportsLeagues)
            .where(TableSportsLeagues.id == 51)
            .values(profileId=None),
            "rule": sa.update(TableLanguagesProfiles)
            .where(TableLanguagesProfiles.profileId == 1)
            .values(items="[]"),
            "output": sa.update(TableArrInstances)
            .where(TableArrInstances.id == 2)
            .values(path_mappings=json.dumps([["/sports", str(lib.folder / "1")]])),
        }
        independent_update(lib, statements[change])


@pytest.mark.parametrize(
    "change",
    ["source", "media", "destination", "owner", "mapping", "profile", "rule", "output"],
)
def test_slow_translation_releases_owner_and_rejects_stale_result(
    profile_library, monkeypatch, change
):
    from app.config import settings
    from app.database import TableArrInstances, TableHistorySports
    from subtitles.tools.subsync_engines import SubtitleDestinationChanged

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    touched = []

    def during():
        if not touched:
            independent_update(
                lib,
                sa.update(TableArrInstances)
                .where(TableArrInstances.id == 2)
                .values(name="responsive"),
            )
            change_input(lib, change)
            touched.append(True)

    transport(monkeypatch, "lingarr", during=during)
    with pytest.raises(
        (
            ValueError,
            RuntimeError,
            OSError,
            SubtitleSourceChanged,
            SubtitleDestinationChanged,
        )
    ):
        run_job(lib, enqueue(lib)[0])
    assert touched
    target = lib.folder / "1/event.hu.srt"
    assert (
        (target.read_text() == "new destination")
        if change == "destination"
        else not target.exists()
    )
    assert not lib.session.execute(sa.select(TableHistorySports)).all()
    assert (
        lib.session.execute(
            sa.select(TableArrInstances.name).where(TableArrInstances.id == 2)
        ).scalar_one()
        == "responsive"
    )


@pytest.mark.parametrize("protected", ["owner", "profile", "rule"])
def test_final_publication_protects_assignment_and_rule(
    profile_library, monkeypatch, protected
):
    import os
    from app.config import settings
    from app.database import (
        TableArrInstances,
        TableLanguagesProfiles,
        TableSportsLeagues,
    )
    from subtitles.tools import subsync_engines

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    transport(monkeypatch, "lingarr")
    statements = {
        "owner": sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(name="changed"),
        "profile": sa.update(TableSportsLeagues)
        .where(TableSportsLeagues.id == 51)
        .values(title="changed"),
        "rule": sa.update(TableLanguagesProfiles)
        .where(TableLanguagesProfiles.profileId == 1)
        .values(name="changed"),
    }
    original = os.replace
    protected_calls = []

    def replace(source, destination):
        if str(destination).endswith("event.hu.srt"):
            with pytest.raises(sa.exc.OperationalError):
                independent_update(lib, statements[protected])
            protected_calls.append(True)
        return original(source, destination)

    monkeypatch.setattr(subsync_engines.os, "replace", replace)
    run_job(lib, enqueue(lib)[0])
    assert protected_calls == [True]
    independent_update(lib, statements[protected])


@pytest.mark.parametrize("when", ["queued", "response", "retry", "publication"])
def test_lingarr_cancellation_never_publishes(profile_library, monkeypatch, when):
    import requests
    from app.config import settings
    from app.database import TableHistorySports
    from app.jobs_queue import JobCancelled
    from sportarr import output

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    job = enqueue(lib)[0]
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if when in ("response", "retry"):
            job.cancelled = True
        if when == "retry":
            raise requests.exceptions.ConnectionError("fixture connection failed")
        return SimpleNamespace(
            status_code=200, text="A végső kör.", headers={"Content-Type": "text/plain"}
        )

    monkeypatch.setattr(requests, "post", post)
    if when == "queued":
        job.cancelled = True
    if when == "publication":
        original = output.SportsOutputNamespace.validate

        def validate(namespace, session):
            result = original(namespace, session)
            job.cancelled = True
            return result

        monkeypatch.setattr(output.SportsOutputNamespace, "validate", validate)
    with pytest.raises(JobCancelled):
        run_job(lib, job)
    assert len(calls) == (0 if when == "queued" else 2 if when == "publication" else 1)
    assert not (lib.folder / "1/event.hu.srt").exists()
    assert not lib.session.execute(sa.select(TableHistorySports)).all()


@pytest.mark.parametrize(
    "count,format", [(2, "srt"), (3, "srt"), (2, "ass"), (3, "ass")]
)
def test_real_composition_has_no_download_history(profile_library, count, format):
    from app.database import TableLanguagesProfiles, TableHistorySports
    from subtitles.tools.combine.main import try_combine_for_video

    lib = profile_library
    rule = {"languages": ["en", "hu", "de"][:count], "format": format}
    lib.session.execute(
        sa.update(TableLanguagesProfiles).values(combine=json.dumps(rule))
    )
    base = lib.hooks.capture_profile_operation(lib.base.context, lib.base.signature)
    for language in ("hu", "de"):
        (lib.folder / f"1/event.{language}.srt").write_text(
            lib.source.read_text().replace("The final lap.", language)
        )
    stem = "event.en.combined-" + "-".join(rule["languages"][1:])
    stale = lib.folder / f"1/{stem}.{'ass' if format == 'srt' else 'srt'}"
    stale.write_text("old format")
    result = try_combine_for_video(
        base.context.mapped_path, "sports", sports_operation=base
    )
    assert result.status == "built" and not result.error
    parsed = pysubs2.load(result.path)
    assert len(parsed) >= 2
    assert all(
        language in open(result.path).read() for language in rule["languages"][1:]
    )
    assert not stale.exists()
    assert not lib.session.execute(sa.select(TableHistorySports)).all()


def test_refresh_failure_preserves_translation_and_history(
    profile_library, monkeypatch
):
    from app.config import settings
    from app.database import TableHistorySports
    from subtitles.indexer import sports

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "openrouter")
    transport(monkeypatch, "openrouter")

    def failed(*a, **k):
        raise OSError("fixture refresh failure")

    monkeypatch.setattr(sports, "store_subtitles_sports", failed)
    with pytest.raises(OSError, match="published.*refresh failed"):
        run_job(lib, enqueue(lib)[0])
    assert (lib.folder / "1/event.hu.srt").is_file()
    row = lib.session.execute(sa.select(TableHistorySports)).scalar_one()
    assert row.action == 6 and row.artifact


@pytest.mark.parametrize(
    "status,content_type,body",
    [
        (200, "application/json", "A végső kör."),
        (200, "application/json", {"error": "bad"}),
        (200, "text/html", "<html>error</html>"),
        (200, "text/plain", "<!DOCTYPE html>error"),
        (401, "application/json", None),
        (403, "application/json", None),
        (404, "text/plain", None),
        (405, "text/plain", None),
        (422, "application/json", None),
    ],
)
def test_lingarr_negotiation_and_terminal_status(
    profile_library, monkeypatch, status, content_type, body
):
    import requests
    from app.config import settings
    from app.database import TableHistorySports

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    requests_seen = []

    def post(url, **kwargs):
        requests_seen.append((url, kwargs))
        return SimpleNamespace(
            status_code=status,
            text=body,
            json=lambda: body,
            headers={"Content-Type": content_type},
        )

    monkeypatch.setattr(requests, "post", post)
    job = enqueue(lib)[0]
    if status == 200 and content_type == "application/json" and isinstance(body, str):
        run_job(lib, job)
        assert len(requests_seen) == 2
    else:
        with pytest.raises((ValueError, LingarrAuthError)):
            run_job(lib, job)
        assert len(requests_seen) == 1
        assert not lib.session.execute(sa.select(TableHistorySports)).all()
        assert not (lib.folder / "1/event.hu.srt").exists()
    assert all(url.endswith("/api/translate/line") for url, _ in requests_seen)


def test_lingarr_retries_only_the_current_line(profile_library, monkeypatch):
    import requests
    from app.config import settings

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["subtitleLine"])
        return SimpleNamespace(
            status_code=503 if len(calls) == 2 else 200,
            text="A végső kör.",
            headers={"Content-Type": "text/plain"},
        )

    monkeypatch.setattr(requests, "post", post)
    run_job(lib, enqueue(lib)[0])
    assert calls == ["The final lap.", "The winner arrives.", "The winner arrives."]


@pytest.mark.parametrize("engine", ["google_translate", "gemini", "openrouter"])
def test_each_engine_preserves_queue_cancellation(profile_library, monkeypatch, engine):
    from app.config import settings
    from app.jobs_queue import JobCancelled
    from app.database import TableHistorySports

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", engine)
    job = enqueue(lib)[0]
    transport(monkeypatch, engine, during=lambda: setattr(job, "cancelled", True))
    with pytest.raises(JobCancelled):
        run_job(lib, job)
    assert not (lib.folder / "1/event.hu.srt").exists()
    assert not lib.session.execute(sa.select(TableHistorySports)).all()


@pytest.mark.parametrize("phase", ["complete", "partial", "empty", "fail"])
def test_openrouter_async_sports_protocol(profile_library, monkeypatch, phase):
    import requests
    from app.config import settings
    from app.database import TableHistorySports

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "openrouter")
    posted = []

    def post(url, **kwargs):
        posted.append((url, kwargs["json"]))
        return SimpleNamespace(status_code=200, json=lambda: {"jobId": "fixture-job"})

    def get(url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "status": "failed"
                if phase == "fail"
                else "partial"
                if phase == "partial"
                else "completed",
                "progress": 100,
                "error": "fixture partial failure" if phase == "partial" else None,
                "result": {
                    "lines": [
                        {"position": i, "line": "A végső kör."}
                        for i in range(
                            0 if phase == "empty" else 1 if phase == "partial" else 2
                        )
                    ]
                },
            },
        )

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)
    job = enqueue(lib)[0]
    if phase in ("complete", "partial"):
        run_job(lib, job)
        row = lib.session.execute(sa.select(TableHistorySports)).scalar_one()
        assert ("Partial result" in row.description) == (phase == "partial")
    else:
        with pytest.raises((RuntimeError, OSError)):
            run_job(lib, job)
        assert not lib.session.execute(sa.select(TableHistorySports)).all()
        assert not (lib.folder / "1/event.hu.srt").exists()
    assert len(posted) == 1 and posted[0][0].endswith("/api/v1/jobs/translate/content")
    assert "arrMediaId" not in posted[0][1] and "mediaType" not in posted[0][1]


def test_provider_dispatch_occurs_after_history_index_and_unlock(
    profile_library, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import TableHistorySports, TableSportsEvents
    from subtitles.tools.subsync_engines import subtitle_write_lock
    from subtitles import processing

    lib = profile_library
    original = processing._trigger_auto_translation
    checked = []

    def trigger(*args, **kwargs):
        assert lib.session.execute(
            sa.select(TableHistorySports.action)
        ).scalars().all() == [2]
        assert (
            "en"
            in lib.session.get(TableSportsEvents, 61, populate_existing=True).subtitles
        )
        state = subtitle_write_lock(
            lib.base.context.mapped_path, str(lib.source.parent)
        )

        def probe():
            acquired = state.lock.acquire(timeout=0.2)
            if acquired:
                state.lock.release()
            return acquired

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(probe).result(timeout=1), (
                "Provider saver still owns its coordinator"
            )
        checked.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(processing, "_trigger_auto_translation", trigger)
    candidates = lib.service.manual_search_sports(61, "en", arr_instance_id=1)
    lib.service.manual_download_sports(61, candidates[0], arr_instance_id=1)
    assert checked == [True] and len(lib.queue.jobs_pending_queue) == 1
    operation = lib.queue.jobs_pending_queue[0].kwargs["sports_operation"]
    assert operation.anchors and operation.signature == lib.base.signature


@pytest.mark.parametrize(
    "case",
    ["no_profile", "forced_source", "threshold", "satisfied", "cutoff", "hi_target"],
)
def test_exact_profile_gates(profile_library, monkeypatch, case):
    from app.config import settings
    from app.database import TableSportsLeagues, TableLanguagesProfiles
    from subtitles.processing import _trigger_auto_translation
    from subtitles.indexer.sports import store_subtitles_sports

    lib = profile_library
    if case == "no_profile":
        lib.session.execute(
            sa.update(TableSportsLeagues)
            .where(TableSportsLeagues.id == 51)
            .values(profileId=None)
        )
    elif case == "threshold":
        monkeypatch.setattr(settings.translator, "min_source_score", 101)
    elif case == "satisfied":
        (lib.folder / "1/event.hu.srt").write_text(lib.source.read_text())
        store_subtitles_sports(61, 1)
    elif case == "cutoff":
        lib.session.execute(sa.update(TableLanguagesProfiles).values(cutoff=1))
    elif case == "hi_target":
        lib.items[1]["hi"] = "True"
        lib.session.execute(
            sa.update(TableLanguagesProfiles).values(items=json.dumps(lib.items))
        )
    context = lib.service.resolve_event_in_session(lib.session, 61, 1)
    base = lib.hooks.capture_profile_operation(
        context, lib.service.candidate_signature(context)
    )
    _trigger_auto_translation(
        "en",
        str(lib.source),
        context.mapped_path,
        "sports",
        source_score_percent=100,
        forced=case == "forced_source",
        sports_operation=base,
    )
    assert len(lib.queue.jobs_pending_queue) == (1 if case == "hi_target" else 0)
    if case == "hi_target":
        job = lib.queue.jobs_pending_queue[0]
        assert (
            job.kwargs["hi"] is True
            and job.kwargs["sports_operation"].target == "hu:hi"
        )


@pytest.mark.parametrize("change", ["source", "rule", "owner", "sibling"])
def test_composition_is_unlocked_and_rejects_late_changes(
    profile_library, monkeypatch, change
):
    from app.database import TableArrInstances
    from subtitles.tools.combine import main

    lib = profile_library
    (lib.folder / "1/event.hu.srt").write_text(lib.source.read_text())
    original = main.compose
    calls = []

    def compose(**kwargs):
        independent_update(
            lib,
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 2)
            .values(name="responsive"),
        )
        content = original(**kwargs)
        if change == "sibling":
            (lib.folder / "1/event.en.combined-hu.ass").write_text("new sibling")
        else:
            change_input(lib, change)
        calls.append(True)
        return content

    monkeypatch.setattr(main, "compose", compose)
    result = main.try_combine_for_video(
        lib.base.context.mapped_path, "sports", sports_operation=lib.base
    )
    assert calls == [True] and result.status == "failed"
    assert not (lib.folder / "1/event.en.combined-hu.srt").exists()
    if change == "sibling":
        assert (lib.folder / "1/event.en.combined-hu.ass").read_text() == "new sibling"


@pytest.mark.parametrize("folder_mode", ["relative", "absolute"])
def test_sports_profile_output_folders(profile_library, monkeypatch, folder_mode):
    from app.config import settings
    from app.database import TableSportsEvents

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    target = (
        lib.folder / "1/subtitles"
        if folder_mode == "relative"
        else lib.folder / "all-subtitles"
    )
    target.mkdir()
    monkeypatch.setattr(
        settings.general,
        "subfolder",
        "relative" if folder_mode == "relative" else "absolute",
    )
    monkeypatch.setattr(
        settings.general,
        "subfolder_custom",
        "subtitles" if folder_mode == "relative" else str(target),
    )
    if folder_mode == "absolute":
        # A shared folder must reserve a distinct basename for the sibling owner.
        (lib.folder / "2/event.mkv").rename(lib.folder / "2/other.mkv")
        lib.session.execute(
            sa.update(TableSportsEvents)
            .where(TableSportsEvents.id == 62)
            .values(path="/sports/other.mkv")
        )
    lib.base = lib.hooks.capture_profile_operation(
        lib.base.context, lib.service.candidate_signature(lib.base.context)
    )
    transport(monkeypatch, "lingarr")
    run_job(lib, enqueue(lib)[0])
    assert (target / "event.hu.srt").is_file()
    assert (target / "event.en.combined-hu.srt").is_file()


def test_native_lingarr_content_contract_remains_native(profile_library, monkeypatch):
    import requests
    from app.database import TableMovies, TableArrInstances
    from subtitles.tools.translate.services.lingarr_translator import (
        LingarrTranslatorService,
    )
    from subtitles.tools.translate.services import lingarr_translator
    from subtitles.tools.translate.core import translator_utils
    from subzero.language import Language

    lib = profile_library
    lib.session.execute(
        sa.insert(TableArrInstances).values(
            id=3, kind="radarr", name="Native", stable_key="native", port=7878
        )
    )
    lib.session.execute(
        sa.insert(TableMovies).values(
            id=61,
            arr_instance_id=3,
            radarrId=61,
            path="/native/movie.mkv",
            title="Native movie collision",
            tmdbId="61",
            subtitles="[]",
        )
    )
    monkeypatch.setattr(translator_utils, "database", lib.session)
    seen, history = [], []

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: [
                {"position": 0, "line": "First translation"},
                {"position": 1, "line": "Second translation"},
            ],
        )

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        lingarr_translator, "history_log_movie", lambda **kwargs: history.append(kwargs)
    )
    destination = str(lib.folder / "1/native.hu.srt")
    service = LingarrTranslatorService(
        source_srt_file=str(lib.source),
        dest_srt_file=destination,
        lang_obj=Language("hun"),
        to_lang="hun",
        from_lang="en",
        media_type="movies",
        video_path=lib.base.context.mapped_path,
        orig_to_lang="hu",
        forced=False,
        hi=False,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=61,
        arr_instance_id=3,
    )
    service.translate()
    assert seen[0][0] == "http://fixture/base/api/translate/content"
    assert seen[0][1]["json"]["arrMediaId"] == 61
    assert seen[0][1]["json"]["mediaType"] == "Movie"
    assert seen[0][1]["json"]["title"] == "Native movie collision"
    assert seen[0][1]["headers"] == {"Content-Type": "application/json"}
    assert history[0]["radarr_id"] == 61 and history[0]["action"] == 6
    assert [(cue.start, cue.end) for cue in pysubs2.load(destination)] == [
        (0, 1000),
        (1100, 2000),
    ]


@pytest.mark.parametrize(
    "reply",
    [
        [{"bad": "shape"}, {"bad": "shape"}],
        [{"index": "0", "content": "A"}, {"index": "0", "content": "B"}],
    ],
)
def test_gemini_does_not_publish_malformed_or_duplicate_positions(
    profile_library, monkeypatch, reply
):
    import requests
    from app.config import settings
    from app.database import TableHistorySports

    lib = profile_library
    monkeypatch.setattr(settings.translator, "translator_type", "gemini")
    response = {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}
    monkeypatch.setattr(
        requests,
        "request",
        lambda *a, **k: SimpleNamespace(
            text=json.dumps(response), raise_for_status=lambda: None
        ),
    )
    with pytest.raises((ValueError, RuntimeError)):
        run_job(lib, enqueue(lib)[0])
    assert not (lib.folder / "1/event.hu.srt").exists()
    assert not lib.session.execute(sa.select(TableHistorySports)).all()
