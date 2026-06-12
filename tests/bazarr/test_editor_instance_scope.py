# coding=utf-8

from flask import Flask


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
