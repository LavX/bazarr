# coding=utf-8
"""content.py metadata fallback and per-instance path regression tests (#156).

Finding 8: ``_get_media_metadata`` episode path looked the show up only by
``TableShows.id == row.series_id``. When ``series_id`` is NULL (transient
pre-INC4 row) that drops ``mediaTitle``/``mediaId``. It must mirror
``resolve_subtitle_path``'s fallback to the upstream ``sonarrSeriesId`` scoped
by the owning instance.

F1: ``_create_subtitle`` must use ``path_replace_instance`` for the video-path
lookup instead of the global ``path_replace`` / ``path_replace_movie``.

F4: ``_refresh_media_subtitles`` must apply the owning instance's path mapping
(from ``metadata['arrInstanceId']``) when calling ``store_subtitles`` /
``store_subtitles_movie``.
"""

import app.database  # noqa: F401


def test_get_media_metadata_episode_falls_back_to_upstream_id(schema_session, monkeypatch):
    from api.subtitles import content
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(content, "database", schema_session)

    schema_session.add(TableShows(
        id=501, sonarrSeriesId=7, arr_instance_id=2, path="/series/alpha",
        title="Alpha Show", tags="[]"))
    schema_session.flush()
    # Episode with a NULL series_id link but a valid upstream sonarrSeriesId.
    schema_session.add(TableEpisodes(
        id=601, series_id=None, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=2, path="/series/alpha/s01e01.mkv", title="Pilot",
        season=1, episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    metadata = content._get_media_metadata("episode", 42, arr_instance_id=2)

    assert metadata is not None
    assert metadata["mediaTitle"] == "Alpha Show", (
        "NULL series_id must fall back to the upstream-id-scoped show lookup")
    assert metadata["mediaId"] == 501


def test_get_media_metadata_episode_fallback_scoped_to_owner(schema_session, monkeypatch):
    """The fallback must not pick a colliding sibling instance's show."""
    from api.subtitles import content
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(content, "database", schema_session)

    # Two shows share sonarrSeriesId=7 under instances 2 and 5.
    schema_session.add(TableShows(
        id=501, sonarrSeriesId=7, arr_instance_id=2, path="/s2",
        title="Wrong Show", tags="[]"))
    schema_session.add(TableShows(
        id=502, sonarrSeriesId=7, arr_instance_id=5, path="/s5",
        title="Right Show", tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=602, series_id=None, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=5, path="/s5/e.mkv", title="Pilot", season=1,
        episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    metadata = content._get_media_metadata("episode", 42, arr_instance_id=5)

    assert metadata["mediaTitle"] == "Right Show"
    assert metadata["mediaId"] == 502


# ------------------------------------------------------------------ F1 regression


def test_create_subtitle_episode_uses_path_replace_instance(schema_session, monkeypatch):
    """_create_subtitle must call path_replace_instance, not the global path_replace.

    Regression for F1 (#156): the media row is scoped by arr_instance_id but
    the video path was derived with the global mapping, silently ignoring any
    per-instance path_mappings configured on that instance.
    """
    from unittest.mock import patch
    from flask import Flask
    from api.subtitles import content
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(content, "database", schema_session)

    schema_session.add(TableShows(
        id=701, sonarrSeriesId=9, arr_instance_id=3, path="/remote/tv/show",
        title="Test Show", tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=801, series_id=701, sonarrEpisodeId=55, sonarrSeriesId=9,
        arr_instance_id=3, path="/remote/tv/show/s01e01.mkv", title="Ep",
        season=1, episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    app = Flask(__name__)
    with app.test_request_context(
        "/api/subtitles/episode/55/create",
        method="POST",
        json={
            "content": "1\n00:00:01,000 --> 00:00:02,000\nHello",
            "language": "en",
            "format": "srt",
        },
    ):
        with patch.object(content.path_mappings, "path_replace_instance",
                          return_value="/local/tv/show/s01e01.mkv") as mock_pri, \
             patch.object(content.path_mappings, "path_replace") as mock_pr, \
             patch.object(content.path_mappings, "path_replace_movie") as mock_prm, \
             patch("api.subtitles.content.os.path.isfile", return_value=False), \
             patch("api.subtitles.content.get_target_folder", return_value=None), \
             patch("api.subtitles.content.os.path.realpath", side_effect=lambda p: p), \
             patch("api.subtitles.content.tempfile.mkstemp") as mock_mkstemp, \
             patch("api.subtitles.content.os.write"), \
             patch("api.subtitles.content.os.close"), \
             patch("api.subtitles.content.os.replace"), \
             patch("api.subtitles.content.store_subtitles"), \
             patch("api.subtitles.content.store_subtitles_movie"), \
             patch("api.subtitles.content.event_stream"), \
             patch("api.subtitles.content.settings") as mock_settings:
            mock_settings.general.chmod_enabled = False
            mock_mkstemp.return_value = (3, "/local/tv/show/s01e01.en.srt.tmp")
            content._create_subtitle("episode", 55, arr_instance_id=3)

    # path_replace_instance must have been called for the episode path
    mock_pri.assert_called_once_with("/remote/tv/show/s01e01.mkv", 3, "episode")
    # The old global helpers must NOT have been called for the video-path lookup
    mock_pr.assert_not_called()
    mock_prm.assert_not_called()


def test_create_subtitle_movie_uses_path_replace_instance(schema_session, monkeypatch):
    """_create_subtitle for movies must call path_replace_instance, not path_replace_movie."""
    from unittest.mock import patch
    from flask import Flask
    from api.subtitles import content
    from app.database import TableMovies

    monkeypatch.setattr(content, "database", schema_session)

    schema_session.add(TableMovies(
        id=901, radarrId=77, arr_instance_id=4, path="/remote/movies/film.mkv",
        title="Film", tmdbId=999, audio_language="[]", monitored="True",
        subtitles="[]"))
    schema_session.flush()

    app = Flask(__name__)
    with app.test_request_context(
        "/api/subtitles/movie/77/create",
        method="POST",
        json={
            "content": "1\n00:00:01,000 --> 00:00:02,000\nHello",
            "language": "en",
            "format": "srt",
        },
    ):
        with patch.object(content.path_mappings, "path_replace_instance",
                          return_value="/local/movies/film.mkv") as mock_pri, \
             patch.object(content.path_mappings, "path_replace") as mock_pr, \
             patch.object(content.path_mappings, "path_replace_movie") as mock_prm, \
             patch("api.subtitles.content.os.path.isfile", return_value=False), \
             patch("api.subtitles.content.get_target_folder", return_value=None), \
             patch("api.subtitles.content.os.path.realpath", side_effect=lambda p: p), \
             patch("api.subtitles.content.tempfile.mkstemp") as mock_mkstemp, \
             patch("api.subtitles.content.os.write"), \
             patch("api.subtitles.content.os.close"), \
             patch("api.subtitles.content.os.replace"), \
             patch("api.subtitles.content.store_subtitles"), \
             patch("api.subtitles.content.store_subtitles_movie"), \
             patch("api.subtitles.content.event_stream"), \
             patch("api.subtitles.content.settings") as mock_settings:
            mock_settings.general.chmod_enabled = False
            mock_mkstemp.return_value = (3, "/local/movies/film.en.srt.tmp")
            content._create_subtitle("movie", 77, arr_instance_id=4)

    mock_pri.assert_called_once_with("/remote/movies/film.mkv", 4, "movie")
    mock_pr.assert_not_called()
    mock_prm.assert_not_called()


# ------------------------------------------------------------------ F4 regression


def test_refresh_media_subtitles_episode_uses_instance_mapping(monkeypatch):
    """_refresh_media_subtitles must pass arr_instance_id from metadata to
    path_replace_instance, not call the global path_replace.

    Regression for F4 (#156): the rescan after an editor save/promote used the
    global path_replace even though the subtitle was resolved per-instance.
    """
    from unittest.mock import patch
    from api.subtitles import content

    metadata = {
        "mediaPath": "/remote/tv/show/s01e01.mkv",
        "mediaId": 701,
        "episodeId": 801,
        "arrInstanceId": 3,
    }

    with patch.object(content.path_mappings, "path_replace_instance",
                      return_value="/local/tv/show/s01e01.mkv") as mock_pri, \
         patch.object(content.path_mappings, "path_replace") as mock_pr, \
         patch("api.subtitles.content.store_subtitles") as mock_store, \
         patch("api.subtitles.content.event_stream"):

        content._refresh_media_subtitles("episode", 801, metadata)

    mock_pri.assert_called_once_with("/remote/tv/show/s01e01.mkv", 3, "episode")
    mock_pr.assert_not_called()
    mock_store.assert_called_once_with(
        "/remote/tv/show/s01e01.mkv",
        "/local/tv/show/s01e01.mkv",
        use_cache=False,
    )


def test_refresh_media_subtitles_movie_uses_instance_mapping(monkeypatch):
    """_refresh_media_subtitles for movies must use path_replace_instance, not path_replace_movie."""
    from unittest.mock import patch
    from api.subtitles import content

    metadata = {
        "mediaPath": "/remote/movies/film.mkv",
        "mediaId": 901,
        "arrInstanceId": 4,
    }

    with patch.object(content.path_mappings, "path_replace_instance",
                      return_value="/local/movies/film.mkv") as mock_pri, \
         patch.object(content.path_mappings, "path_replace_movie") as mock_prm, \
         patch("api.subtitles.content.store_subtitles_movie") as mock_store, \
         patch("api.subtitles.content.event_stream"):

        content._refresh_media_subtitles("movie", 901, metadata)

    mock_pri.assert_called_once_with("/remote/movies/film.mkv", 4, "movie")
    mock_prm.assert_not_called()
    mock_store.assert_called_once_with(
        "/remote/movies/film.mkv",
        "/local/movies/film.mkv",
        use_cache=False,
    )


def test_refresh_media_subtitles_none_instance_falls_back_to_global(monkeypatch):
    """When arrInstanceId is None (default/single-instance), path_replace_instance
    falls back to the global mapping -- the behavior must be unchanged."""
    from unittest.mock import patch
    from api.subtitles import content

    metadata = {
        "mediaPath": "/tv/show/s01e01.mkv",
        "mediaId": 700,
        "episodeId": 800,
        "arrInstanceId": None,
    }

    with patch.object(content.path_mappings, "path_replace_instance",
                      return_value="/tv/show/s01e01.mkv") as mock_pri, \
         patch("api.subtitles.content.store_subtitles"), \
         patch("api.subtitles.content.event_stream"):

        content._refresh_media_subtitles("episode", 800, metadata)

    # Must still call path_replace_instance with None (which internally falls back)
    mock_pri.assert_called_once_with("/tv/show/s01e01.mkv", None, "episode")
