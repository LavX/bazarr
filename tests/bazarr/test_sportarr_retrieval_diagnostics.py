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
    from app.jobs_queue import JobFailed

    def fails(*args):
        raise OSError(reason)

    with pytest.raises(JobFailed) as failure:
        download_job(fails)
    assert str(failure.value) == reason
    # The queue files the reason as job.error, which the drawer shows. The
    # progress message carries progress only.
    assert download_job.progress == []


def test_the_search_route_repeats_the_reason_it_was_given(monkeypatch):
    from api.sports import subtitles as api_mod

    def fails(*args):
        raise OSError("All providers are throttled")

    monkeypatch.setattr(api_mod, "manual_search_sports", fails)
    body, status = search(arr_instance_id=1, language="en")
    assert status == 409
    assert body == {"message": "All providers are throttled"}


def test_a_reasonless_failure_still_says_something(monkeypatch, download_job):
    """An argument-less OSError from anywhere below must not fail the job with
    a blank reason, which would leave the user with a failure and nothing to
    read."""
    from api.sports import subtitles as api_mod
    from app.jobs_queue import JobFailed

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


# The index and delete routes in api/sports/events.py answered every OSError
# with a constant, so a recording that had gone and an analysis that timed out
# read the same. A raw filesystem error carries the local path in its text, so
# the route repeats only the sentences the sports layer raises on purpose, and
# the operating system's sentence for any other errno.
SECRET_PATH = "/srv/private/Formula.1.2026.R14.mkv"
INDEX_FALLBACK = "Could not index this sports file. Check its accessibility and try again."
READ_FALLBACK = "Could not read this sports file. Path mapping or accessibility issue?"


def _index(monkeypatch, error):
    from flask import Flask

    from api.sports import events
    from subtitles.indexer import sports

    def fails(*args, **kwargs):
        raise error

    monkeypatch.setattr(sports, "store_subtitles_sports", fails)
    with Flask(__name__).test_request_context(
        "/sports/events/61/subtitles", method="POST", json={"arr_instance_id": 1}
    ):
        return events.SportsEventSubtitles.post.__wrapped__(events.SportsEventSubtitles(), 61)


def _delete(monkeypatch, error):
    from flask import Flask

    from api.sports import events
    from sportarr import subtitles as sports_subtitles

    def fails(*args, **kwargs):
        raise error

    monkeypatch.setattr(sports_subtitles, "sports_manual_operation", fails)
    with Flask(__name__).test_request_context(
        "/sports/events/61/subtitles", method="DELETE",
        json={"arr_instance_id": 1, "language": "en", "path": "/sports/event.en.srt"},
    ):
        return events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)


@pytest.mark.parametrize("reason", sorted({
    "Could not analyze sports video",
    "Invalid sports analysis result",
}))
def test_the_index_route_repeats_the_reason_it_was_given(monkeypatch, reason):
    assert _index(monkeypatch, OSError(reason)) == ({"message": reason}, 409)


def test_the_index_route_says_the_analysis_timed_out(monkeypatch):
    # TimeoutError is an OSError, and the analysis raises it with a sentence.
    assert _index(monkeypatch, TimeoutError("Sports video analysis timed out")) == (
        {"message": "Sports video analysis timed out"}, 409)


@pytest.mark.parametrize("call,lead,advice", [
    (_index, "Could not index this sports file", "Check its accessibility and try again."),
    (_delete, "Could not read this sports file", "Path mapping or accessibility issue?"),
])
def test_a_filesystem_error_gives_its_reason_without_its_path(monkeypatch, call, lead, advice):
    import errno
    import os

    error = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), SECRET_PATH)
    body, status = call(monkeypatch, error)
    assert status == 409
    assert body == {"message": f"{lead}: {os.strerror(errno.ENOENT)}. {advice}"}
    assert SECRET_PATH not in body["message"]


@pytest.mark.parametrize("call,fallback", [(_index, INDEX_FALLBACK), (_delete, READ_FALLBACK)])
def test_an_unrecorded_sentence_is_not_repeated(monkeypatch, call, fallback):
    """Only the recorded sentences are repeated. An OSError raised with any
    other text, a path included, answers the constant."""
    assert call(monkeypatch, OSError(f"cannot open {SECRET_PATH}")) == ({"message": fallback}, 409)
    assert call(monkeypatch, OSError()) == ({"message": fallback}, 409)


def test_the_recorded_file_reasons_are_the_ones_the_indexer_raises():
    """KNOWN_FILE_REASONS is repeated verbatim, so it has to be exactly the
    literal sentences the index path raises: a new one is recorded on purpose
    or falls back, and none of them can interpolate a path."""
    from api.sports.events import KNOWN_FILE_REASONS

    raised, interpolated = set(), []
    for relative_path, names in (
        ("bazarr/subtitles/indexer/sports.py", {"_metadata"}),
        ("bazarr/sportarr/analysis.py", {"parse_video_metadata"}),
    ):
        for function in _functions(_module_tree(relative_path), names):
            for node in ast.walk(function):
                if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                        and getattr(node.exc.func, "id", None) in ("OSError", "TimeoutError")):
                    continue
                for arg in node.exc.args:
                    if isinstance(arg, ast.JoinedStr):
                        interpolated.append(f"{relative_path}:{node.lineno}")
                    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        raised.add(arg.value)

    assert interpolated == [], f"an interpolated reason would reach the 409 body: {interpolated}"
    assert raised == set(KNOWN_FILE_REASONS)
