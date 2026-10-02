# coding=utf-8
"""The shared auth helper must not parse an upload body to look for the key.

Reading ``request.form`` parses the whole request, whatever its content type.
Every route that accepts a multipart upload authenticates through the same
``authenticate`` helper in api/utils.py, so a body with a wrong or missing key
was parsed (and its file part spooled) before the 401, and before the route's
own declared-length refusal. The form-key fallback now reads only an
``application/x-www-form-urlencoded`` body: that is the form a legacy client
posts a key in, and the frontend sends the X-API-KEY header (apis/raw/client.ts
sets it once on the shared axios instance), so no upload path relies on a key
inside a multipart form.

One test per upload route, the complete set that goes through the helper with
a file body: episode and movie subtitle uploads, the sports upload, the
subtitle archive extraction and the Provider Hub local package.
"""

from io import BytesIO

import pytest

from flask import Flask

API_KEY = "b56-route-auth-key-0123456789"  # pragma: allowlist secret

# (label, module, resource class, the event id the view takes, the path the
# route is registered under, a factory for the multipart form fields the route
# reads). The factory builds a fresh file part per request, because the test
# client reads and closes it.
UPLOAD_ROUTES = [
    ("episode subtitles", "api.episodes.episodes_subtitles", "EpisodesSubtitles", None,
     "episodes/subtitles",
     lambda: {"seriesid": "1", "episodeid": "2", "language": "en", "forced": "false", "hi": "false",
              "file": (BytesIO(b"1\n"), "episode.en.srt")}),
    ("movie subtitles", "api.movies.movies_subtitles", "MoviesSubtitles", None,
     "movies/subtitles",
     lambda: {"radarrid": "1", "language": "en", "forced": "false", "hi": "false",
              "file": (BytesIO(b"1\n"), "movie.en.srt")}),
    ("sports upload", "api.sports.subtitles", "SportsEventSubtitleUpload", 61,
     "sports/events/61/subtitles/upload",
     lambda: {"language": "en", "forced": "false", "hi": "false",
              "file": (BytesIO(b"1\n"), "event.en.srt")}),
    ("subtitle archive", "api.subtitles.archive", "SubtitleArchive", None,
     "subtitles/archive",
     lambda: {"file": (BytesIO(b"PK"), "pack.zip")}),
    ("provider hub local package", "api.provider_hub.provider_hub", "ProviderHubLocalInstallations",
     None, "provider-hub/installations/local",
     lambda: {"file": (BytesIO(b"PK"), "package.zip")}),
]


@pytest.fixture(autouse=True)
def upload_api_key(monkeypatch):
    monkeypatch.setattr("app.config.settings.auth.apikey", API_KEY)


@pytest.fixture
def form_parses(monkeypatch):
    """Every form parse of the request under test, recorded.

    The refusal has to happen before the body is parsed, and parsing is lazy,
    so looking at request.form afterwards would trigger the very parse the
    test is about. The parse itself is instrumented instead.
    """
    parses = []
    real = Flask.request_class._load_form_data

    def spy(self):
        parses.append(self)
        return real(self)

    monkeypatch.setattr(Flask.request_class, "_load_form_data", spy)
    return parses


def _client_for(route):
    import importlib

    label, module_name, class_name, event_id, path, _form = route
    module = importlib.import_module(module_name)
    resource = getattr(module, class_name)
    instance = resource()
    app = Flask(__name__)
    if event_id is None:
        app.add_url_rule("/" + path, view_func=lambda: resource.post(instance), methods=["POST"])
    else:
        app.add_url_rule("/" + path, view_func=lambda: resource.post(instance, event_id), methods=["POST"])
    return app.test_client()


@pytest.mark.parametrize("header", ["wrong", "missing"], ids=["wrong header key", "missing header key"])
@pytest.mark.parametrize("route", UPLOAD_ROUTES, ids=[route[0] for route in UPLOAD_ROUTES])
def test_a_refused_multipart_upload_is_not_parsed_before_the_401(form_parses, route, header):
    client = _client_for(route)
    headers = {"X-API-KEY": "not-the-key"} if header == "wrong" else {}

    response = client.post("/" + route[4], data=route[5](), content_type="multipart/form-data",
                           headers=headers)

    assert response.status_code == 401
    assert form_parses == [], "the body was parsed before the request was refused"


@pytest.mark.parametrize("route", UPLOAD_ROUTES, ids=[route[0] for route in UPLOAD_ROUTES])
def test_an_api_key_inside_the_multipart_form_does_not_authenticate_the_route(form_parses, route):
    client = _client_for(route)
    form = {**route[5](), "apikey": API_KEY}

    response = client.post("/" + route[4], data=form, content_type="multipart/form-data")

    # A legacy client that posted its key this way moves the key to the
    # X-API-KEY header: the key inside a multipart form is not read, so the
    # body is not parsed and the request is refused without spooling the file.
    assert response.status_code == 401
    assert form_parses == [], "the body was parsed to look for the key"


@pytest.mark.parametrize("route", UPLOAD_ROUTES, ids=[route[0] for route in UPLOAD_ROUTES])
def test_a_urlencoded_form_key_still_authenticates_the_route(route):
    client = _client_for(route)

    response = client.post("/" + route[4], data={"apikey": API_KEY},
                           content_type="application/x-www-form-urlencoded")

    # Authentication passed, so the route answered for its own reasons: the
    # urlencoded body carries no file part, so every one of these upload
    # routes refuses it with 400 rather than 401.
    assert response.status_code != 401
    assert response.status_code == 400
