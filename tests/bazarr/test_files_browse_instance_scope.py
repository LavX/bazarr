# coding=utf-8
"""Filesystem-browse endpoints must browse the SELECTED instance's server (#156).

Finding 10: the Sonarr/Radarr file-browser endpoints always called
``browse_*_filesystem(path)`` with no instance, so they only ever browsed the
default server. They now accept an optional ``instance_id`` query arg and build
the owning instance's ArrClient.
"""

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
