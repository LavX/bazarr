# coding=utf-8
"""The Jellyfin endpoints' `apikey` argument must not be readable from the URL.

That argument is the *Jellyfin* server's key, and the endpoint forwards it as an
auth token to a URL the caller supplies. flask_restx defaults an argument to
``location=('json', 'values')`` and ``values`` includes the query string, so the
query silently wins over the body. The comment above the parser already says
"POST so apikey rides in the request body instead of the query string", so the
mitigation was stated but never in effect: a key in a URL still reached browser
history, reverse-proxy access logs and Referer headers.
"""

import pytest

from api.jellyfin.endpoints import JellyfinLibraries, JellyfinTestConnection

QUERY_LOCATIONS = {"values", "args", "query_string"}


def _locations(parser, name):
    for argument in parser.args:
        if argument.name == name:
            location = argument.location
            return {location} if isinstance(location, str) else set(location)
    raise AssertionError(f"{name} argument not found on {parser}")


@pytest.mark.parametrize(
    "resource",
    [JellyfinTestConnection, JellyfinLibraries],
    ids=["test-connection", "libraries"],
)
def test_the_jellyfin_key_is_never_read_from_the_url(resource):
    locations = _locations(resource.post_request_parser, "apikey")

    assert not (locations & QUERY_LOCATIONS), (
        f"{resource.__name__} reads the Jellyfin key from {sorted(locations)}. "
        "A query-string location puts a third-party credential into browser "
        "history and access logs, and lets the query override the body."
    )


@pytest.mark.parametrize(
    "resource",
    [JellyfinTestConnection, JellyfinLibraries],
    ids=["test-connection", "libraries"],
)
def test_the_body_is_still_accepted(resource):
    locations = _locations(resource.post_request_parser, "apikey")

    assert "json" in locations and "form" in locations, (
        f"{resource.__name__} must still accept the key from a JSON or "
        f"form-encoded body, got {sorted(locations)}"
    )
