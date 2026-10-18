# coding=utf-8

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from flask import Flask
from flask_restx import Api


def test_editor_resolve_video_path_scopes_movie_by_instance(schema_session, monkeypatch):
    from api.editor import editor
    from app.database import TableMovies

    monkeypatch.setattr(editor, "database", schema_session)
    monkeypatch.setattr(editor.path_mappings, "path_replace_movie", lambda path: f"/mapped{path}")

    schema_session.add_all([
        TableMovies(
            id=100,
            radarrId=50,
            arr_instance_id=1,
            path="/movies/default.mkv",
            title="Default",
            tmdbId="100",
        ),
        TableMovies(
            id=101,
            radarrId=50,
            arr_instance_id=2,
            path="/movies/secondary.mkv",
            title="Secondary",
            tmdbId="101",
        ),
    ])

    assert editor._resolve_video_path("movie", 50, arr_instance_id=2) == "/mapped/movies/secondary.mkv"
    assert editor._resolve_video_path("movie", 50, arr_instance_id=3) == ("Movie not found", 404)


def test_editor_resolve_video_path_scopes_episode_by_instance(schema_session, monkeypatch):
    from api.editor import editor
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(editor, "database", schema_session)
    monkeypatch.setattr(editor.path_mappings, "path_replace", lambda path: f"/mapped{path}")

    schema_session.add_all([
        TableShows(id=201, sonarrSeriesId=5, arr_instance_id=1, path="/series/default", title="Default"),
        TableShows(id=202, sonarrSeriesId=5, arr_instance_id=2, path="/series/secondary", title="Secondary"),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=301,
            series_id=201,
            sonarrEpisodeId=70,
            sonarrSeriesId=5,
            arr_instance_id=1,
            path="/series/default/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
        ),
        TableEpisodes(
            id=302,
            series_id=202,
            sonarrEpisodeId=70,
            sonarrSeriesId=5,
            arr_instance_id=2,
            path="/series/secondary/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
        ),
    ])

    assert editor._resolve_video_path("episode", 70, arr_instance_id=2) == "/mapped/series/secondary/s01e01.mkv"
    assert editor._resolve_video_path("episode", 70, arr_instance_id=3) == ("Episode not found", 404)


def test_editor_query_resolution_passes_arr_instance_id(monkeypatch):
    from api.editor import editor

    captured = []
    monkeypatch.setattr(
        editor,
        "_resolve_video_path",
        lambda media_type, media_id, arr_instance_id=None: (
            captured.append((media_type, media_id, arr_instance_id)) or "/mapped/video.mkv"
        ),
    )
    monkeypatch.setattr(editor, "_probe_video", lambda _path: {"format": {"duration": "12.3"}, "streams": []})
    monkeypatch.setattr(editor.os.path, "isfile", lambda _path: True)

    app = Flask(__name__)
    with app.test_request_context("/api/editor/info?mediaType=movie&mediaId=50&arr_instance_id=2"):
        result = editor.EditorInfo.get.__wrapped__(editor.EditorInfo())

    assert result["duration"] == 12.3
    assert captured == [("movie", 50, 2)]


def test_editor_hls_passes_arr_instance_id(monkeypatch):
    from api.editor import editor

    captured = []
    monkeypatch.setattr(
        editor,
        "_resolve_video_path",
        lambda media_type, media_id, arr_instance_id=None: (
            captured.append((media_type, media_id, arr_instance_id)) or ("Movie not found", 404)
        ),
    )

    app = Flask(__name__)
    with app.test_request_context("/api/editor/hls/movie/50/0/0.000/playlist.m3u8?arr_instance_id=2"):
        result = editor.EditorHls.get.__wrapped__(
            editor.EditorHls(),
            media_type="movie",
            media_id=50,
            audio_track=0,
            start_time="0.000",
            filename="playlist.m3u8",
        )

    assert result == ("Movie not found", 404)
    assert captured == [("movie", 50, 2)]


@pytest.fixture(params=["movie", "episode"])
def hls_client(request, schema_session, monkeypatch, tmp_path):
    from api import utils
    from api.editor import editor, stream_token
    from app.database import TableEpisodes, TableMovies, TableShows

    media_type = request.param
    api_key = "editor +/&=?%#"
    monkeypatch.setattr(utils, "settings", SimpleNamespace(auth=SimpleNamespace(apikey=api_key)))
    monkeypatch.setattr(stream_token, "settings", SimpleNamespace(auth=SimpleNamespace(apikey=api_key)))
    monkeypatch.setattr(editor, "database", schema_session)
    monkeypatch.setattr(editor, "HLS_CACHE_DIR", str(tmp_path / "hls"))
    monkeypatch.setattr(editor.path_mappings, "path_replace_movie", lambda path: path)
    monkeypatch.setattr(editor.path_mappings, "path_replace", lambda path: path)
    monkeypatch.setattr(editor, "_probe_video", lambda _path: {
        "format": {"duration": "12.3"},
        "streams": [{"codec_type": "audio", "codec_name": "aac", "channels": 2}],
    })

    manifest = (
        '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n'
        '#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI="init.mp4"\n'
        '#EXTINF:4.000000,\nsegment_000000.m4s\n'
        '#EXTINF:4.000000,\nsegment_000001.m4s\n#EXT-X-ENDLIST\n'
    )
    # Colliding upstream IDs and distinct bytes expose wrong-instance routing.
    for instance_id in (1, 2):
        video_path = tmp_path / f"video-{instance_id}.mkv"
        video_path.write_bytes(b"cached video")
        if media_type == "movie":
            schema_session.add(TableMovies(
                id=100 + instance_id, radarrId=50, arr_instance_id=instance_id,
                path=str(video_path), title="Movie", tmdbId=str(instance_id),
            ))
        else:
            schema_session.add(TableShows(
                id=200 + instance_id, sonarrSeriesId=5, arr_instance_id=instance_id,
                path=str(tmp_path), title="Show",
            ))
            schema_session.flush()
            schema_session.add(TableEpisodes(
                id=300 + instance_id, series_id=200 + instance_id,
                sonarrEpisodeId=50, sonarrSeriesId=5, arr_instance_id=instance_id,
                path=str(video_path), title="Episode", season=1, episode=1,
            ))

        scopes = (None, 1) if instance_id == 1 else (2,)
        for scope in scopes:
            for native_hls in (False, True):
                cache_dir = Path(editor._hls_cache_dir(
                    media_type, 50, scope, 0, 0.0, video_path.stat().st_mtime,
                    native_hls=native_hls,
                ))
                cache_dir.mkdir(parents=True)
                (cache_dir / "playlist.m3u8").write_text(manifest)
                for filename in ("init.mp4", "segment_000000.m4s", "segment_000001.m4s"):
                    (cache_dir / filename).write_bytes(f"instance-{instance_id}:{filename}".encode())
    schema_session.flush()

    app = Flask(__name__)
    app.config["TESTING"] = True
    # The production registration: the api blueprint itself, so the
    # app-wide hook it carries in bazarr/api/__init__.py is under test too.
    from api import api_bp

    app.register_blueprint(api_bp)
    with app.test_client() as client:
        yield client, f"/api/editor/hls/{media_type}/50/0/0.000/playlist.m3u8", api_key, manifest


@pytest.mark.parametrize("scope", ["2", "02", None, ""])
def test_editor_hls_playlist_resources_keep_instance_scope(hls_client, scope):
    client, playlist_url, api_key, manifest = hls_client
    headers = {"X-API-KEY": api_key}
    query = {} if scope is None else {"arr_instance_id": scope}
    media_type = playlist_url.split("/")[4]

    # The hls.js path authenticates with the header.
    response = client.get(playlist_url, query_string=query, headers=headers)

    assert response.status_code == 200
    assert response.mimetype == "application/vnd.apple.mpegurl"
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_data(as_text=True)
    if not scope:
        assert body == manifest

    # The playlist's rewritten URIs, fetched with the header, stay on the
    # same instance as the playlist request.
    header_init_uri = re.search(r'#EXT-X-MAP:URI="([^"]+)"', body).group(1)
    header_segment_uris = [
        line for line in body.splitlines() if line and not line.startswith("#")
    ]
    assert len(header_segment_uris) == 2
    for uri in [header_init_uri, *header_segment_uris]:
        parsed = parse_qs(urlsplit(uri).query)
        assert "apikey" not in parsed
        if scope:
            assert parsed["arr_instance_id"] == [str(int(scope))]
        resource = client.get(urljoin(playlist_url, uri), headers=headers)
        assert resource.status_code == 200
        assert resource.headers["Cache-Control"] == "no-store"
        instance_id = 2 if scope else 1
        assert resource.data == f"instance-{instance_id}:{urlsplit(uri).path}".encode()

    # The native path asks for a stream token over a header-authenticated
    # call and plays from st alone.
    token_response = client.get(
        "/api/editor/stream-token",
        query_string={**query, "mediaType": media_type, "mediaId": "50", "audioTrack": "0"},
        headers=headers,
    )
    assert token_response.status_code == 200
    assert token_response.headers["Cache-Control"] == "no-store"
    assert token_response.headers["X-Bazarr-Stream-Token-Route"] == "1"
    token = token_response.get_json()["token"]

    native_response = client.get(playlist_url, query_string={**query, "st": token})
    assert native_response.status_code == 200
    assert native_response.headers["Cache-Control"] == "no-store"
    native_body = native_response.get_data(as_text=True)

    init_uri = re.search(r'#EXT-X-MAP:URI="([^"]+)"', native_body).group(1)
    segment_uris = [line for line in native_body.splitlines() if line and not line.startswith("#")]
    assert len(segment_uris) == 2
    expected_query = {"st": [token]}
    if scope:
        # The echoed scope is the canonical integer form, so "02" and ""
        # style spellings normalise before they reach the URIs.
        expected_query["arr_instance_id"] = [str(int(scope))]
    for uri in [init_uri, *segment_uris]:
        parsed = parse_qs(urlsplit(uri).query)
        assert parsed == expected_query
        assert "apikey" not in parsed
        # A segment with a valid st and no key is 200.
        resource = client.get(urljoin(playlist_url, uri))
        assert resource.status_code == 200
        assert resource.headers["Cache-Control"] == "no-store"
        filename = urlsplit(uri).path
        instance_id = 2 if scope else 1
        assert resource.data == f"instance-{instance_id}:{filename}".encode()


def test_editor_hls_rejects_invalid_instance_scope(hls_client):
    client, playlist_url, api_key, _manifest = hls_client

    response = client.get(
        playlist_url, query_string={"arr_instance_id": "invalid"}, headers={"X-API-KEY": api_key},
    )

    assert response.status_code == 400
    assert "arr_instance_id must be an integer" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_editor_hls_error_answers_are_never_cacheable(hls_client, monkeypatch):
    from api.editor import editor

    client, playlist_url, api_key, _manifest = hls_client
    headers = {"X-API-KEY": api_key}

    # A filename outside the whitelist is a 400.
    response = client.get(
        playlist_url.replace("playlist.m3u8", "bogus.m3u8"), headers=headers,
    )
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"

    # Media the instance does not know is a 404.
    response = client.get(playlist_url.replace("/50/", "/999/"), headers=headers)
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"

    # A session whose encoder has produced nothing yet is a 503.
    monkeypatch.setattr(editor, "_spawn_hls_encoder", lambda *a, **k: None)
    monkeypatch.setattr(editor, "_wait_for_first_segment", lambda *a, **k: False)
    response = client.get(playlist_url.replace("0.000", "1.000"), headers=headers)
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"

    # An encoder that will not start is a 500.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("no ffmpeg")

    monkeypatch.setattr(editor, "_spawn_hls_encoder", _boom)
    response = client.get(playlist_url.replace("0.000", "2.000"), headers=headers)
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"


def test_editor_options_and_unsupported_method_answers_are_never_cacheable(hls_client):
    """The router's own answers on the no-store routes are no-store too.

    The decorators register their no-store finalizer only when a view runs,
    so the automatic OPTIONS answer and the 405 of an unsupported method,
    which the router builds before any view is dispatched, never pass
    through it. The client registers the real api blueprint, so the
    app-wide hook production wires in bazarr/api/__init__.py is what marks
    those answers, and the token route's router-built answers carry the
    route marker as well.
    """
    client, playlist_url, _api_key, _manifest = hls_client

    for method, expected_status in (("options", 200), ("post", 405)):
        for url in (playlist_url, "/api/editor/stream-token"):
            response = getattr(client, method)(url)
            assert response.status_code == expected_status
            assert response.headers["Cache-Control"] == "no-store"
            if url == "/api/editor/stream-token":
                assert response.headers["X-Bazarr-Stream-Token-Route"] == "1"
            else:
                assert "X-Bazarr-Stream-Token-Route" not in response.headers


def test_editor_unhandled_error_in_the_token_route_is_no_store_and_marked(hls_client, monkeypatch):
    """The token route's unhandled 500 carries both headers.

    The finalizer the decorator registers makes it no-store, and the
    app-wide hook the api blueprint carries adds the route marker, so the
    frontend never mistakes this 500 for a reverse proxy answering while
    Bazarr is down.
    """
    from api.editor import editor

    client, playlist_url, api_key, _manifest = hls_client
    media_type = playlist_url.split("/")[4]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("token route exploded")

    monkeypatch.setattr(editor, "_resolve_video_path", _boom)
    # The bare-500 path: the framework builds the answer from the unhandled
    # exception, so no error handler may intercept it and testing mode must
    # not propagate it.
    client.application.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get(
        "/api/editor/stream-token",
        query_string={"mediaType": media_type, "mediaId": "50"},
        headers={"X-API-KEY": api_key},
    )
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Bazarr-Stream-Token-Route"] == "1"


def test_editor_hls_playlist_drops_a_st_the_route_did_not_verify(hls_client):
    client, playlist_url, api_key, manifest = hls_client

    # A header-authenticated playlist call with a malformed st riding along:
    # served as if the st were not there, and never echoed back into the
    # manifest unescaped.
    response = client.get(
        playlist_url, query_string={"st": "abc\\q"}, headers={"X-API-KEY": api_key},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_data(as_text=True)
    assert body == manifest
    assert "st=" not in body


def test_editor_hls_rejects_query_apikey(hls_client, caplog):
    client, playlist_url, api_key, _manifest = hls_client
    caplog.set_level(logging.WARNING)

    response = client.get(playlist_url, query_string={"apikey": api_key})

    assert response.status_code == 401
    assert _deprecation_warnings(caplog) == []


def test_editor_hls_rejects_a_query_apikey_beside_a_valid_header_key(hls_client):
    client, playlist_url, api_key, _manifest = hls_client
    # The mixed-credential regression: a valid header key does not excuse
    # an apikey in the URL, whatever else the request presents.
    response = client.get(
        playlist_url, query_string={"apikey": api_key}, headers={"X-API-KEY": api_key},
    )

    assert response.status_code == 401


def test_editor_hls_rejects_a_query_apikey_beside_a_valid_stream_token(hls_client):
    from api.editor import stream_token

    client, playlist_url, api_key, _manifest = hls_client
    token = stream_token.mint_stream_token(
        media_type="movie" if "movie" in playlist_url else "episode",
        media_id=50, arr_instance_id=None, audio_track=0, lifetime_seconds=3600,
    )
    response = client.get(playlist_url, query_string={"st": token, "apikey": api_key})

    assert response.status_code == 401


def test_editor_hls_refuses_expired_scoped_or_rotated_stream_tokens(hls_client, monkeypatch):
    from api.editor import stream_token

    client, playlist_url, api_key, _manifest = hls_client

    def mint(**kwargs):
        return stream_token.mint_stream_token(**kwargs)

    expired = mint(media_type="movie" if "movie" in playlist_url else "episode",
                   media_id=50, arr_instance_id=None, audio_track=0,
                   lifetime_seconds=-10)
    response = client.get(playlist_url, query_string={"st": expired})
    assert response.status_code == 401

    other_media = mint(media_type="movie" if "movie" not in playlist_url else "episode",
                       media_id=50, arr_instance_id=None, audio_track=0,
                       lifetime_seconds=3600)
    response = client.get(playlist_url, query_string={"st": other_media})
    assert response.status_code == 401

    other_track = mint(media_type="movie" if "movie" in playlist_url else "episode",
                       media_id=50, arr_instance_id=None, audio_track=1,
                       lifetime_seconds=3600)
    response = client.get(playlist_url, query_string={"st": other_track})
    assert response.status_code == 401

    # A token minted under a rotated API key no longer verifies.
    minted = mint(media_type="movie" if "movie" in playlist_url else "episode",
                  media_id=50, arr_instance_id=None, audio_track=0,
                  lifetime_seconds=3600)
    monkeypatch.setattr(stream_token, "settings",
                       SimpleNamespace(auth=SimpleNamespace(apikey="rotated key")))
    response = client.get(playlist_url, query_string={"st": minted})
    assert response.status_code == 401


def test_editor_stream_token_route_responses_and_logging(hls_client, caplog):
    client, playlist_url, api_key, _manifest = hls_client
    headers = {"X-API-KEY": api_key}
    media_type = playlist_url.split("/")[4]
    caplog.set_level(logging.DEBUG)

    # Without the header the route's own 401 reaches the client with no-store
    # and the route marker, so the frontend can tell it from a proxy answer.
    response = client.get(
        "/api/editor/stream-token", query_string={"mediaType": media_type, "mediaId": "50"},
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Bazarr-Stream-Token-Route"] == "1"

    # The same 401 for a key in the query string: the route is header only.
    response = client.get(
        "/api/editor/stream-token",
        query_string={"mediaType": media_type, "mediaId": "50", "apikey": api_key},
    )
    assert response.status_code == 401
    assert response.headers["X-Bazarr-Stream-Token-Route"] == "1"
    assert _deprecation_warnings(caplog) == []

    # Media the HLS route would refuse: 404, and the same headers.
    response = client.get(
        "/api/editor/stream-token",
        query_string={"mediaType": media_type, "mediaId": "999"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Bazarr-Stream-Token-Route"] == "1"

    # The token reaches the client but never the log.
    response = client.get(
        "/api/editor/stream-token",
        query_string={"mediaType": media_type, "mediaId": "50", "audioTrack": "0"},
        headers=headers,
    )
    assert response.status_code == 200
    token = response.get_json()["token"]
    client.get(f"{playlist_url}?player=probe&st={token}")
    for record in caplog.records:
        assert token not in record.getMessage()


def test_editor_sync_passes_arr_instance_id(monkeypatch):
    from api.editor import editor

    captured = []
    monkeypatch.setattr(
        editor,
        "_resolve_video_path",
        lambda media_type, media_id, arr_instance_id=None: (
            captured.append((media_type, media_id, arr_instance_id)) or ("Movie not found", 404)
        ),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/editor/sync",
        method="POST",
        json={
            "mediaType": "movie",
            "mediaId": 50,
            "arrInstanceId": 2,
            "content": "1\n00:00:01,000 --> 00:00:02,000\nText\n",
            "language": "en",
        },
    ):
        result = editor.EditorSync.post.__wrapped__(editor.EditorSync())

    assert result == ("Movie not found", 404)
    assert captured == [("movie", 50, 2)]


@pytest.fixture
def editor_client(schema_session, monkeypatch, tmp_path):
    from api import utils
    from api.editor import editor
    from app.database import TableMovies

    api_key = "editor +/&=?%#"
    monkeypatch.setattr(utils, "settings", SimpleNamespace(auth=SimpleNamespace(apikey=api_key)))
    monkeypatch.setattr(editor, "database", schema_session)
    monkeypatch.setattr(editor.path_mappings, "path_replace_movie", lambda path: path)
    monkeypatch.setattr(editor, "_probe_video", lambda _path: {
        "format": {"duration": "12.3"},
        "streams": [{"codec_type": "audio", "codec_name": "aac", "channels": 2}],
    })
    monkeypatch.setattr(editor, "request_peaks", lambda _path, _track: (
        "ready", {"peaks": [0.5, -1], "duration": 0.2, "sampleRate": 10},
    ))
    monkeypatch.setitem(editor._editor_sync_jobs, "job-1", {"status": "running", "message": "Working"})

    video_path = tmp_path / "video.mkv"
    video_path.write_bytes(b"video")
    schema_session.add(TableMovies(
        id=102, radarrId=50, arr_instance_id=2, path=str(video_path), title="Movie", tmdbId="2",
        subtitles=str([["en", str(tmp_path / "video.en.srt"), 100]]),
    ))
    schema_session.flush()

    app = Flask(__name__)
    app.config["TESTING"] = True
    Api(app).add_namespace(editor.api_ns_editor, path="/api/")
    with app.test_client() as client:
        yield client, api_key


# The four requests the editor makes with fetch(): info, peaks, the subtitle
# list and the sync poll.
EDITOR_FETCHES = [
    pytest.param("/api/editor/info", {"mediaType": "movie", "mediaId": "50", "arr_instance_id": "2"}, id="info"),
    pytest.param(
        "/api/editor/peaks",
        {"mediaType": "movie", "mediaId": "50", "audioTrack": "0", "arr_instance_id": "2"},
        id="peaks",
    ),
    pytest.param(
        "/api/editor/subtitles", {"mediaType": "movie", "mediaId": "50", "arr_instance_id": "2"}, id="subtitles",
    ),
    pytest.param("/api/editor/sync", {"jobKey": "job-1"}, id="sync-poll"),
]


def _deprecation_warnings(caplog):
    return [record for record in caplog.records if "deprecated" in record.getMessage()]


@pytest.mark.parametrize("path, query", EDITOR_FETCHES)
def test_editor_fetch_with_header_key_logs_no_deprecation(editor_client, caplog, path, query):
    client, api_key = editor_client
    caplog.set_level(logging.WARNING)

    response = client.get(path, query_string=query, headers={"X-API-KEY": api_key})

    assert response.status_code == 200
    assert _deprecation_warnings(caplog) == []


@pytest.mark.parametrize("path, query", EDITOR_FETCHES)
def test_editor_fetch_with_query_key_is_refused(editor_client, caplog, path, query):
    # The editor routes exist for the frontend, which sends the key in the
    # header. A key back in a URL is a regression, so it fails at once with
    # a 401 instead of passing with a warning nobody reads.
    client, api_key = editor_client
    caplog.set_level(logging.WARNING)

    response = client.get(path, query_string={**query, "apikey": api_key})

    assert response.status_code == 401
    assert _deprecation_warnings(caplog) == []


def test_editor_sync_post_with_form_key_is_refused(editor_client, caplog):
    client, api_key = editor_client
    caplog.set_level(logging.WARNING)

    response = client.post(
        "/api/editor/sync",
        data={"apikey": api_key},
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 401
    assert _deprecation_warnings(caplog) == []


def test_editor_hls_rejects_a_multipart_form_apikey_beside_a_valid_header_key(hls_client, caplog):
    client, playlist_url, api_key, _manifest = hls_client
    caplog.set_level(logging.WARNING)

    # The mixed-credential regression of the form path: a valid header key
    # does not excuse an apikey riding in a multipart body either, whatever
    # else the request presents.
    response = client.get(
        playlist_url,
        data={"apikey": api_key},
        content_type="multipart/form-data",
        headers={"X-API-KEY": api_key},
    )

    assert response.status_code == 401
    assert _deprecation_warnings(caplog) == []


def test_query_key_outside_the_editor_still_accepted_with_a_warning(monkeypatch, caplog):
    # Only the editor routes are header only; the rest of the API keeps the
    # legacy fallback for third-party clients.
    from api import utils

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(utils, "settings", SimpleNamespace(auth=SimpleNamespace(apikey="other-key")))
    app = Flask(__name__)

    @app.route("/api/system/badges")
    @utils.authenticate
    def badges():
        return {"ok": True}

    with app.test_client() as client:
        response = client.get("/api/system/badges?apikey=other-key")

    assert response.status_code == 200
    assert len(_deprecation_warnings(caplog)) == 1
