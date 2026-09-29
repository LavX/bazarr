# coding=utf-8
"""Filesystem-browse endpoints must browse the SELECTED instance's server (#156).

Finding 10: the Sonarr/Radarr file-browser endpoints always called
``browse_*_filesystem(path)`` with no instance, so they only ever browsed the
default server. They now accept an optional ``instance_id`` query arg and build
the owning instance's ArrClient.

The listing each server answers is its own JSON, so a payload of the wrong
shape answers an empty listing rather than a 500, and entries without a string
name and path are skipped.
"""

import importlib
import logging

import pytest
from flask import Flask


def test_sonarr_browse_routes_to_selected_instance(monkeypatch):
    from api.files import files_sonarr

    client_calls = []
    browse_calls = []
    sentinel = object()

    monkeypatch.setattr(
        files_sonarr, "client_for_instance",
        lambda db, instance_id, *a, **k: client_calls.append(instance_id) or sentinel)
    monkeypatch.setattr(
        files_sonarr, "browse_sonarr_filesystem",
        lambda path, arr_client=None: browse_calls.append(arr_client) or {"directories": []})

    app = Flask(__name__)
    with app.test_request_context("/api/files/sonarr?path=/tv&instance_id=4"):
        files_sonarr.BrowseSonarrFS.get.__wrapped__(files_sonarr.BrowseSonarrFS())

    assert client_calls == [4]
    assert browse_calls == [sentinel], "the selected instance's client must be passed through"


def test_sonarr_browse_default_when_no_instance(monkeypatch):
    from api.files import files_sonarr

    browse_calls = []
    monkeypatch.setattr(
        files_sonarr, "client_for_instance",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build a client")))
    monkeypatch.setattr(
        files_sonarr, "browse_sonarr_filesystem",
        lambda path, arr_client=None: browse_calls.append(arr_client) or {"directories": []})

    app = Flask(__name__)
    with app.test_request_context("/api/files/sonarr?path=/tv"):
        files_sonarr.BrowseSonarrFS.get.__wrapped__(files_sonarr.BrowseSonarrFS())

    assert browse_calls == [None], "no instance_id => default server (arr_client None)"


def test_radarr_browse_routes_to_selected_instance(monkeypatch):
    from api.files import files_radarr

    client_calls = []
    browse_calls = []
    sentinel = object()

    monkeypatch.setattr(
        files_radarr, "client_for_instance",
        lambda db, instance_id, *a, **k: client_calls.append(instance_id) or sentinel)
    monkeypatch.setattr(
        files_radarr, "browse_radarr_filesystem",
        lambda path, arr_client=None: browse_calls.append(arr_client) or {"directories": []})

    app = Flask(__name__)
    with app.test_request_context("/api/files/radarr?path=/movies&instance_id=9"):
        files_radarr.BrowseRadarrFS.get.__wrapped__(files_radarr.BrowseRadarrFS())

    assert client_calls == [9]
    assert browse_calls == [sentinel]


def test_sportarr_browse_seeds_the_initial_listing_with_root_folders(monkeypatch):
    """An empty-path sports browse answers from the root folders already synced
    for the (default) owner, before any live probe of the Sportarr server."""
    from api.files import files_sportarr

    monkeypatch.setattr(files_sportarr, "default_instance_id", lambda db, kind: 9)
    monkeypatch.setattr(
        files_sportarr, "list_rootfolder_paths",
        lambda db, instance_id: ["/sports/", "/soccer/"])
    monkeypatch.setattr(
        files_sportarr, "client_for_instance",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a seeded listing must not build a client")))

    app = Flask(__name__)
    with app.test_request_context("/api/files/sportarr?path="):
        data = files_sportarr.BrowseSportarrFS.get.__wrapped__(
            files_sportarr.BrowseSportarrFS())

    assert data == [
        {"name": "sports", "children": True, "path": "/sports/"},
        {"name": "soccer", "children": True, "path": "/soccer/"},
    ]


def test_sportarr_browse_lists_directories_live(monkeypatch):
    """A non-empty-path sports browse asks the selected Sportarr server, and a
    path with no seeded roots falls back to the live listing too."""
    from api.files import files_sportarr

    sentinel = object()
    browse_calls = []
    monkeypatch.setattr(files_sportarr, "default_instance_id", lambda db, kind: 9)
    monkeypatch.setattr(files_sportarr, "list_rootfolder_paths", lambda db, instance_id: [])
    monkeypatch.setattr(
        files_sportarr, "client_for_instance",
        lambda db, instance_id, *a, **k: sentinel)
    monkeypatch.setattr(
        files_sportarr, "browse_sportarr_filesystem",
        lambda path, arr_client=None: browse_calls.append((path, arr_client)) or {
            "directories": [{"name": "sub", "path": "/sports/sub"}]})

    app = Flask(__name__)
    with app.test_request_context("/api/files/sportarr?path=/sports/"):
        data = files_sportarr.BrowseSportarrFS.get.__wrapped__(
            files_sportarr.BrowseSportarrFS())

    assert browse_calls == [("/sports/", sentinel)]
    assert data == [{"name": "sub", "children": True, "path": "/sports/sub"}]


# (module, resource, browse function, request) for each arr file browser. The
# Sportarr request names a path so the live listing is asked, not the seeded
# root folders.
ARR_BROWSERS = [
    ("files_sonarr", "BrowseSonarrFS", "browse_sonarr_filesystem",
     "/api/files/sonarr?path=/tv/&instance_id=4"),
    ("files_radarr", "BrowseRadarrFS", "browse_radarr_filesystem",
     "/api/files/radarr?path=/movies/&instance_id=4"),
    ("files_sportarr", "BrowseSportarrFS", "browse_sportarr_filesystem",
     "/api/files/sportarr?path=/sports/&instance_id=4"),
]


def _browse_with_payload(monkeypatch, browser, payload):
    module_name, resource_name, browse_name, url = browser
    module = importlib.import_module(f"api.files.{module_name}")
    monkeypatch.setattr(module, "client_for_instance", lambda *a, **k: object())
    monkeypatch.setattr(module, browse_name, lambda path, arr_client=None: payload)
    resource = getattr(module, resource_name)

    app = Flask(__name__)
    with app.test_request_context(url):
        return resource.get.__wrapped__(resource())


@pytest.mark.parametrize("browser", ARR_BROWSERS, ids=lambda browser: browser[0])
@pytest.mark.parametrize("payload", [
    {},
    {"directories": None},
    {"directories": "x"},
    {"directories": {"a": 1}},
    [],
    ["x"],
    "Sonarr",
    42,
], ids=["no-directories", "null", "string", "object", "empty-list", "list", "text", "number"])
def test_browse_answers_empty_for_a_malformed_payload(monkeypatch, caplog, browser, payload):
    """A 200 whose JSON is not an object with a list of directories answers the
    same empty listing as a transport failure, not a 500, and says why in the
    debug log, since an empty folder browser explains nothing on its own."""
    caplog.set_level(logging.DEBUG)

    assert _browse_with_payload(monkeypatch, browser, payload) == []

    server = browser[0].split("_")[1].capitalize()
    assert [record.getMessage() for record in caplog.records
            if "no list of directories" in record.getMessage()] == [
        f"BAZARR {server} answered a filesystem listing with no list of directories"]


@pytest.mark.parametrize("browser", ARR_BROWSERS, ids=lambda browser: browser[0])
def test_browse_does_not_log_a_failed_request_twice(monkeypatch, caplog, browser):
    """None is a request browse_*_filesystem already logged as it failed."""
    caplog.set_level(logging.DEBUG)

    assert _browse_with_payload(monkeypatch, browser, None) == []

    assert not [record for record in caplog.records
                if "no list of directories" in record.getMessage()]


@pytest.mark.parametrize("browser", ARR_BROWSERS, ids=lambda browser: browser[0])
def test_browse_skips_entries_without_a_string_name_and_path(monkeypatch, browser):
    payload = {"directories": [
        {"name": "ok", "path": "/s/ok"},
        {"name": "nopath"},
        {"path": "/s/noname"},
        {"name": 1, "path": "/s/x"},
        {"name": "nullpath", "path": None},
        "junk",
        None,
        {"name": "also ok", "path": "/s/also ok", "size": 0},
    ]}

    assert _browse_with_payload(monkeypatch, browser, payload) == [
        {"name": "ok", "children": True, "path": "/s/ok"},
        {"name": "also ok", "children": True, "path": "/s/also ok"},
    ]
