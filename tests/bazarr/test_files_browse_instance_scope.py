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
