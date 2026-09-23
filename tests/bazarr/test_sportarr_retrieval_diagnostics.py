# coding=utf-8
"""A failed sports retrieval says which stage failed.

Both routes in api/sports/subtitles.py answer a provider failure with 409, and
the reason for that failure was already attached before the route saw it:
manual_search and manual_download_subtitle return the failing stage as a
sentence, and sportarr/subtitles.py re-raises that sentence as the OSError
argument. The handlers replaced it with a constant that sent the user to
re-inspect a file and a provider which had both been fine, so a 409 could not be
acted on without reading the log.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]

# Every sentence a failed manual download can hand back, plus the one the sports
# layer substitutes for a falsy result at sportarr/subtitles.py. A retrieval
# failure can surface one of these and nothing else, which is what the guard
# test below pins.
KNOWN_REASONS = (
    "Subtitle not found in cache. Please search again.",
    "All providers are throttled",
    "Error downloading Subtitles",
    "Downloaded subtitles isn't valid. Check log.",
    "Error saving Subtitles file to disk",
    "Something went wrong, check the logs for error",
)
SPORTS_FALLBACK_REASON = "Could not download sports subtitle"

SEARCH_FALLBACK = "Could not search this sports file. Check its accessibility and providers."
DOWNLOAD_FALLBACK = (
    "Subtitle was not published. Check the file and provider before trying again."
)


def _call(resource_name, event_id, body):
    from flask import Flask

    from api.sports import subtitles as api_mod

    resource = getattr(api_mod, resource_name)
    with Flask(__name__).test_request_context(
        f"/sports/events/{event_id}/{resource_name}", method="POST", json=body
    ):
        return resource.post.__wrapped__(resource(), event_id)


def search(**body):
    return _call("SportsSearch", 61, body)


def download(**body):
    return _call("SportsDownload", 61, body)


def _failing(*args, **kwargs):
    raise OSError()


@pytest.fixture
def download_job(monkeypatch):
    """Run the queued sports download job inline against a stubbed download."""
    from app.jobs_queue import jobs_queue
    from sportarr import manual_jobs
    from sportarr import subtitles as sports_subtitles

    monkeypatch.setattr(manual_jobs, "database", SimpleNamespace(get=lambda *a, **k: SimpleNamespace(title="Match")))
    monkeypatch.setattr(manual_jobs, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(jobs_queue, "update_job_name", lambda **kwargs: True)
    progress = []
    monkeypatch.setattr(jobs_queue, "update_job_progress",
                        lambda **kwargs: progress.append(kwargs.get("progress_message")) or True)

    def run(download):
        monkeypatch.setattr(sports_subtitles, "manual_download_sports", download)
        return manual_jobs.sports_manually_download_subtitle(
            61, {"subtitle": "cached"}, 1, job_id=9)

    run.progress = progress
    return run


@pytest.fixture
def queued(monkeypatch):
    from api.sports import subtitles as api_mod

    calls = []
    monkeypatch.setattr(api_mod, "resolve_event_in_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_mod, "sports_manually_download_subtitle",
                        lambda *args: calls.append(args) or 5)
    return calls


def test_the_download_route_queues_a_job_and_answers_at_once(queued):
    assert download(arr_instance_id=1, candidate={"subtitle": "cached"}) == ({"job_id": 5}, 202)
    assert queued == [(61, {"subtitle": "cached"}, 1)]


@pytest.mark.parametrize("reason", KNOWN_REASONS + (SPORTS_FALLBACK_REASON,))
def test_the_download_job_fails_with_the_reason_it_was_given(download_job, reason):
    from app.job_errors import JobFailed

    def fails(*args):
        raise OSError(reason)

    with pytest.raises(JobFailed) as failure:
        download_job(fails)
    assert str(failure.value) == reason
    # The reason is on the job too, where the jobs drawer shows it.
    assert download_job.progress == [reason]


def test_a_reasonless_failure_still_says_something(monkeypatch, download_job):
    """An argument-less OSError from anywhere below must not fail the job with
    a blank reason, which would leave the user with a failure and nothing to
    read."""
    from api.sports import subtitles as api_mod
    from app.job_errors import JobFailed

    monkeypatch.setattr(api_mod, "manual_search_sports", _failing)
    assert search(arr_instance_id=1, language="en") == ({"message": SEARCH_FALLBACK}, 409)

    with pytest.raises(JobFailed) as failure:
        download_job(_failing)
    assert str(failure.value) == DOWNLOAD_FALLBACK


def test_the_not_found_and_malformed_branches_are_unchanged(monkeypatch, queued):
    from api.sports import subtitles as api_mod
    from sportarr.errors import SportsNotFound

    def missing(*args, **kwargs):
        raise SportsNotFound("Sports event not found for this owner")

    def malformed(*args, **kwargs):
        raise ValueError("A valid subtitle language and boolean variants are required")

    monkeypatch.setattr(api_mod, "manual_search_sports", missing)
    assert search(arr_instance_id=1, language="en") == ({"message": "Sports event not found for this owner"}, 404)
    monkeypatch.setattr(api_mod, "manual_search_sports", malformed)
    assert search(arr_instance_id=1, language="en") == (
        {"message": "A valid subtitle language and boolean variants are required"}, 400)

    # The download route answers these before queueing anything.
    monkeypatch.setattr(api_mod, "resolve_event_in_session", missing)
    assert download(arr_instance_id=1, candidate={"subtitle": "cached"}) == (
        {"message": "Sports event not found for this owner"}, 404)
    monkeypatch.setattr(api_mod, "resolve_event_in_session", malformed)
    assert download(arr_instance_id=1, candidate={"subtitle": "cached"}) == (
        {"message": "A valid subtitle language and boolean variants are required"}, 400)
    assert download(arr_instance_id=1, candidate={"subtitle": 3}) == (
        {"message": "A cached subtitle result is required"}, 400)
    assert download(candidate={"subtitle": "cached"})[1] == 400
    assert queued == []


def test_a_published_download_is_not_reported_as_a_failure(monkeypatch, download_job):
    """Publication happens before the read-back, so a later owner or read
    failure must still complete the job with no event rather than fail it."""
    from sportarr import library

    publication = {"status": "published", "message": "Published"}

    def gone(*args):
        raise RuntimeError("event vanished after publication")

    monkeypatch.setattr(library, "get_event", gone)
    assert download_job(lambda *args: SimpleNamespace(publication=publication)) == {
        "event": None, "publication": publication}


def _module_tree(relative_path):
    return ast.parse((REPO / relative_path).read_text())


def _functions(tree, names):
    found = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert names <= found, f"the producers of the surfaced sentence are gone: {names - found}"
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]


def test_the_reasons_manual_py_returns_stay_bare_literals():
    """The 409 body carries one of these verbatim, so a reason that interpolated
    a path, a provider credential or a host would put that value on the wire
    instead of only in the log. They are constants today; this keeps them so."""
    producers = {"_manual_search", "manual_download_subtitle"}
    returned, interpolated = set(), []
    for function in _functions(_module_tree("bazarr/subtitles/manual.py"), producers):
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            if isinstance(node.value, ast.JoinedStr):
                interpolated.append(f"{function.name}:{node.lineno}")
            elif isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                returned.add(node.value.value)

    assert interpolated == [], f"a returned f-string would be surfaced verbatim: {interpolated}"
    assert returned, "no reason was collected, so the producers were renamed"
    unrecorded = returned - set(KNOWN_REASONS)
    assert unrecorded == set(), f"a reason reached the caller without being recorded: {unrecorded}"


def test_the_sports_layer_attaches_a_reason_without_interpolating_it():
    """sportarr/subtitles.py is what turns the returned sentence into the
    OSError the routes read, so it is the last place a path could be woven in
    before the 409 body."""
    producers = {"manual_search_sports", "manual_download_sports"}
    raises = []
    for function in _functions(_module_tree("bazarr/sportarr/subtitles.py"), producers):
        raises += [
            node for node in ast.walk(function)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and getattr(node.exc.func, "id", None) == "OSError"
        ]

    assert raises, "the reason-carrying raise is gone"
    for node in raises:
        assert not any(isinstance(arg, ast.JoinedStr) for arg in node.exc.args), (
            f"sportarr/subtitles.py:{node.lineno} builds the reason by interpolation, "
            "which reaches the 409 body"
        )
