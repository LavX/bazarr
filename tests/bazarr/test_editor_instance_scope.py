# coding=utf-8

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
    from api.editor import editor
    from app.database import TableEpisodes, TableMovies, TableShows

    media_type = request.param
    api_key = "editor +/&=?%#"
    monkeypatch.setattr(utils, "settings", SimpleNamespace(auth=SimpleNamespace(apikey=api_key)))
    monkeypatch.setattr(editor, "database", schema_session)
    monkeypatch.setattr(editor, "HLS_CACHE_DIR", str(tmp_path / "hls"))
    monkeypatch.setattr(editor.path_mappings, "path_replace_movie", lambda path: path)
    monkeypatch.setattr(editor.path_mappings, "path_replace", lambda path: path)

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
            cache_dir = Path(editor._hls_cache_dir(
                media_type, 50, scope, 0, 0.0, video_path.stat().st_mtime,
            ))
            cache_dir.mkdir(parents=True)
            (cache_dir / "playlist.m3u8").write_text(manifest)
            for filename in ("init.mp4", "segment_000000.m4s", "segment_000001.m4s"):
                (cache_dir / filename).write_bytes(f"instance-{instance_id}:{filename}".encode())
    schema_session.flush()

    app = Flask(__name__)
    app.config["TESTING"] = True
    Api(app).add_namespace(editor.api_ns_editor, path="/api/")
    with app.test_client() as client:
        yield client, f"/api/editor/hls/{media_type}/50/0/0.000/playlist.m3u8", api_key, manifest


@pytest.mark.parametrize("scope", ["2", "02", None, ""])
@pytest.mark.parametrize("query_auth", [False, True], ids=["header-auth", "native-auth"])
def test_editor_hls_playlist_resources_keep_instance_scope(hls_client, scope, query_auth):
    client, playlist_url, api_key, manifest = hls_client
    query = {} if scope is None else {"arr_instance_id": scope}
    headers = {} if query_auth else {"X-API-KEY": api_key}
    if query_auth:
        query["apikey"] = api_key

    response = client.get(playlist_url, query_string=query, headers=headers)

    assert response.status_code == 200
    assert response.mimetype == "application/vnd.apple.mpegurl"
    assert response.headers["Cache-Control"] == "no-cache"
    body = response.get_data(as_text=True)
    if not scope and not query_auth:
        assert body == manifest

    init_uri = re.search(r'#EXT-X-MAP:URI="([^"]+)"', body).group(1)
    segment_uris = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert len(segment_uris) == 2
    expected_query = {"arr_instance_id": ["2"]} if scope else {}
    if query_auth:
        expected_query["apikey"] = [api_key]
    for uri in [init_uri, *segment_uris]:
        assert parse_qs(urlsplit(uri).query) == expected_query
        resource = client.get(urljoin(playlist_url, uri), headers=headers)
        assert resource.status_code == 200
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
