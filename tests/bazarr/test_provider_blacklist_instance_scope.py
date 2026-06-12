# coding=utf-8

from flask import Flask


def test_provider_movie_missing_recompute_keeps_instance_scope(schema_session, tmp_path, monkeypatch):
    from api.providers import providers_movies
    from app.database import TableMovies

    movie_path = tmp_path / "movie.mkv"
    movie_path.write_text("", encoding="utf-8")
    schema_session.add(TableMovies(
        id=801,
        radarrId=1,
        arr_instance_id=7,
        path=str(movie_path),
        title="Movie",
        tmdbId="801",
        subtitles="[]",
        missing_subtitles=None,
    ))
    schema_session.flush()

    calls = []
    monkeypatch.setattr(providers_movies, "database", schema_session)
    monkeypatch.setattr(providers_movies.path_mappings, "path_replace_movie", lambda value: value)
    monkeypatch.setattr(
        providers_movies,
        "list_missing_subtitles_movies",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(providers_movies, "get_providers_sorted", lambda: [])
    monkeypatch.setattr(providers_movies, "manual_search", lambda *args, **kwargs: [])

    app = Flask(__name__)
    with app.test_request_context("/api/providers/movies?radarrid=801"):
        providers_movies.ProviderMovies.get.__wrapped__(providers_movies.ProviderMovies())

    assert calls == [{"no": 1, "arr_instance_id": 7}]


def test_provider_episode_missing_recompute_keeps_instance_scope(schema_session, tmp_path, monkeypatch):
    from api.providers import providers_episodes
    from app.database import TableEpisodes, TableShows

    episode_path = tmp_path / "episode.mkv"
    episode_path.write_text("", encoding="utf-8")
    schema_session.add(TableShows(
        id=901,
        sonarrSeriesId=1,
        arr_instance_id=8,
        path=str(tmp_path),
        title="Show",
        tags="[]",
    ))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=902,
        series_id=901,
        sonarrSeriesId=1,
        sonarrEpisodeId=2,
        arr_instance_id=8,
        path=str(episode_path),
        title="Pilot",
        season=1,
        episode=1,
        subtitles="[]",
        missing_subtitles=None,
    ))
    schema_session.flush()

    calls = []
    monkeypatch.setattr(providers_episodes, "database", schema_session)
    monkeypatch.setattr(providers_episodes.path_mappings, "path_replace", lambda value: value)
    monkeypatch.setattr(
        providers_episodes,
        "list_missing_subtitles",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(providers_episodes, "get_providers_sorted", lambda: [])
    monkeypatch.setattr(providers_episodes, "manual_search", lambda *args, **kwargs: [])

    app = Flask(__name__)
    with app.test_request_context("/api/providers/episodes?episodeid=902"):
        providers_episodes.ProviderEpisodes.get.__wrapped__(providers_episodes.ProviderEpisodes())

    assert calls == [{"epno": 2, "arr_instance_id": 8}]


def test_provider_movie_post_resolves_local_id_before_download(schema_session, monkeypatch):
    from api.providers import providers_movies
    from app.database import TableMovies

    schema_session.add_all([
        TableMovies(
            id=801,
            radarrId=1,
            arr_instance_id=7,
            path="/movies/local.mkv",
            title="Local",
            tmdbId="801",
        ),
        TableMovies(
            id=802,
            radarrId=801,
            arr_instance_id=7,
            path="/movies/collider.mkv",
            title="Collider",
            tmdbId="802",
        ),
    ])

    calls = []
    monkeypatch.setattr(providers_movies, "database", schema_session)
    monkeypatch.setattr(
        providers_movies,
        "movie_manually_download_specific_subtitle",
        lambda **kwargs: calls.append(kwargs),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/providers/movies",
        method="POST",
        data={
            "radarrid": "801",
            "hi": "False",
            "forced": "False",
            "original_format": "False",
            "provider": "provider",
            "subtitle": "subtitle",
        },
    ):
        response = providers_movies.ProviderMovies.post.__wrapped__(providers_movies.ProviderMovies())

    assert response == ("", 204)
    assert calls[0]["radarr_id"] == 1
    assert calls[0]["arr_instance_id"] == 7


def test_provider_episode_post_resolves_local_id_before_download(schema_session, monkeypatch):
    from api.providers import providers_episodes
    from app.database import TableEpisodes, TableShows

    schema_session.add_all([
        TableShows(id=901, sonarrSeriesId=1, arr_instance_id=8, path="/series/local", title="Local", tags="[]"),
        TableShows(id=902, sonarrSeriesId=7, arr_instance_id=8, path="/series/collider", title="Collider", tags="[]"),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=1001,
            series_id=901,
            sonarrSeriesId=1,
            sonarrEpisodeId=2,
            arr_instance_id=8,
            path="/series/local/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
        ),
        TableEpisodes(
            id=1002,
            series_id=902,
            sonarrSeriesId=7,
            sonarrEpisodeId=1001,
            arr_instance_id=8,
            path="/series/collider/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
        ),
    ])

    calls = []
    monkeypatch.setattr(providers_episodes, "database", schema_session)
    monkeypatch.setattr(
        providers_episodes,
        "episode_manually_download_specific_subtitle",
        lambda **kwargs: calls.append(kwargs),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/providers/episodes",
        method="POST",
        data={
            "seriesid": "999",
            "episodeid": "1001",
            "hi": "False",
            "forced": "False",
            "original_format": "False",
            "provider": "provider",
            "subtitle": "subtitle",
        },
    ):
        response = providers_episodes.ProviderEpisodes.post.__wrapped__(providers_episodes.ProviderEpisodes())

    assert response == ("", 204)
    assert calls[0]["sonarr_series_id"] == 1
    assert calls[0]["sonarr_episode_id"] == 2
    assert calls[0]["arr_instance_id"] == 8


def test_movie_blacklist_post_keeps_instance_scope(schema_session, tmp_path, monkeypatch):
    from api.movies import blacklist
    from app.database import TableMovies

    movie_path = tmp_path / "movie.mkv"
    movie_path.write_text("", encoding="utf-8")
    schema_session.add(TableMovies(
        id=1001,
        radarrId=3,
        arr_instance_id=9,
        path=str(movie_path),
        title="Movie",
        tmdbId="1001",
    ))
    schema_session.flush()

    delete_calls = []
    download_calls = []
    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist.path_mappings, "path_replace_movie", lambda value: value)
    monkeypatch.setattr(blacklist, "blacklist_log_movie", lambda **kwargs: None)
    monkeypatch.setattr(blacklist, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(blacklist, "delete_subtitles", lambda **kwargs: delete_calls.append(kwargs) or True)
    monkeypatch.setattr(blacklist, "movies_download_subtitles", lambda *args, **kwargs: download_calls.append((args, kwargs)))

    app = Flask(__name__)
    with app.test_request_context(
        "/api/movies/blacklist?radarrid=3&provider=p&subs_id=s&language=en"
        "&subtitles_path=/subs/movie.en.srt&arr_instance_id=9",
        method="POST",
    ):
        result = blacklist.MoviesBlacklist.post.__wrapped__(blacklist.MoviesBlacklist())

    assert result == ("", 200)
    assert delete_calls[0]["arr_instance_id"] == 9
    assert download_calls == [((3,), {"arr_instance_id": 9})]


def test_episode_blacklist_post_keeps_instance_scope(schema_session, tmp_path, monkeypatch):
    from api.episodes import blacklist
    from app.database import TableEpisodes, TableShows

    episode_path = tmp_path / "episode.mkv"
    episode_path.write_text("", encoding="utf-8")
    schema_session.add(TableShows(
        id=1101,
        sonarrSeriesId=4,
        arr_instance_id=10,
        path=str(tmp_path),
        title="Show",
        tags="[]",
    ))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=1102,
        series_id=1101,
        sonarrSeriesId=4,
        sonarrEpisodeId=5,
        arr_instance_id=10,
        path=str(episode_path),
        title="Pilot",
        season=1,
        episode=1,
    ))
    schema_session.flush()

    delete_calls = []
    download_calls = []
    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist.path_mappings, "path_replace", lambda value: value)
    monkeypatch.setattr(blacklist, "blacklist_log", lambda **kwargs: None)
    monkeypatch.setattr(blacklist, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(blacklist, "delete_subtitles", lambda **kwargs: delete_calls.append(kwargs) or True)
    monkeypatch.setattr(blacklist, "episode_download_subtitles", lambda *args, **kwargs: download_calls.append((args, kwargs)))

    app = Flask(__name__)
    with app.test_request_context(
        "/api/episodes/blacklist?seriesid=4&episodeid=5&provider=p&subs_id=s&language=en"
        "&subtitles_path=/subs/episode.en.srt&arr_instance_id=10",
        method="POST",
    ):
        result = blacklist.EpisodesBlacklist.post.__wrapped__(blacklist.EpisodesBlacklist())

    assert result == ("", 200)
    assert delete_calls[0]["arr_instance_id"] == 10
    assert download_calls == [((), {"no": 5, "arr_instance_id": 10})]
