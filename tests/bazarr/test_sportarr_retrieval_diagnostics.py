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


@pytest.mark.parametrize("reason", KNOWN_REASONS + (SPORTS_FALLBACK_REASON,))
def test_the_download_route_repeats_the_reason_it_was_given(monkeypatch, reason):
    from api.sports import subtitles as api_mod

    def fails(*args):
        raise OSError(reason)

    monkeypatch.setattr(api_mod, "manual_download_sports", fails)
    body, status = download(arr_instance_id=1, candidate={"subtitle": "cached"})
    assert status == 409
    assert body == {"message": reason}


def test_the_search_route_repeats_the_reason_it_was_given(monkeypatch):
    from api.sports import subtitles as api_mod

    def fails(*args):
        raise OSError("All providers are throttled")

    monkeypatch.setattr(api_mod, "manual_search_sports", fails)
    body, status = search(arr_instance_id=1, language="en")
    assert status == 409
    assert body == {"message": "All providers are throttled"}


def test_a_reasonless_failure_still_says_something(monkeypatch):
    """An argument-less OSError from anywhere below must not answer a blank
    message, which would leave the user with a 409 and nothing to read."""
    from api.sports import subtitles as api_mod

    monkeypatch.setattr(api_mod, "manual_search_sports", _failing)
    assert search(arr_instance_id=1, language="en") == ({"message": SEARCH_FALLBACK}, 409)

    monkeypatch.setattr(api_mod, "manual_download_sports", _failing)
    assert download(arr_instance_id=1, candidate={"subtitle": "cached"}) == (
        {"message": DOWNLOAD_FALLBACK},
        409,
    )


def test_the_not_found_and_malformed_branches_are_unchanged(monkeypatch):
    from api.sports import subtitles as api_mod
    from sportarr.errors import SportsNotFound

    def missing(*args):
        raise SportsNotFound("Sports event not found for this owner")

    def malformed(*args):
        raise ValueError("A valid subtitle language and boolean variants are required")

    for attribute, call, body in (
        ("manual_search_sports", search, dict(arr_instance_id=1, language="en")),
        ("manual_download_sports", download, dict(arr_instance_id=1, candidate={"subtitle": "cached"})),
    ):
        monkeypatch.setattr(api_mod, attribute, missing)
        assert call(**body) == ({"message": "Sports event not found for this owner"}, 404)
        monkeypatch.setattr(api_mod, attribute, malformed)
        assert call(**body) == (
            {"message": "A valid subtitle language and boolean variants are required"},
            400,
        )


def test_a_published_download_is_not_reported_as_a_failure(monkeypatch):
    """Publication happens before the read-back, so a later owner or read
    failure must still answer 200 with no event rather than a 409."""
    from api.sports import subtitles as api_mod

    publication = {"status": "published", "message": "Published"}
    monkeypatch.setattr(
        api_mod, "manual_download_sports", lambda *args: SimpleNamespace(publication=publication)
    )

    def gone(*args):
        raise RuntimeError("event vanished after publication")

    monkeypatch.setattr(api_mod.library, "get_event", gone)
    assert download(arr_instance_id=1, candidate={"subtitle": "cached"}) == (
        {"event": None, "publication": publication},
        200,
    )


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
