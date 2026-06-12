# coding=utf-8

from flask import Flask


def test_movie_profile_update_accepts_local_id(schema_session, monkeypatch):
    from api.movies import movies
    from app.database import TableLanguagesProfiles, TableMovies, select

    captured = []
    monkeypatch.setattr(movies, "database", schema_session)
    monkeypatch.setattr(
        movies,
        "list_missing_subtitles_movies",
        lambda no, arr_instance_id=None: captured.append((no, arr_instance_id)),
    )
    monkeypatch.setattr(movies, "event_stream", lambda **kwargs: None)

    schema_session.add(TableLanguagesProfiles(profileId=4, name="Profile", items="[]"))
    schema_session.add_all([
        TableMovies(
            id=100,
            radarrId=1,
            arr_instance_id=2,
            path="/movies/alpha.mkv",
            title="Alpha",
            tmdbId="100",
        ),
        TableMovies(
            id=200,
            radarrId=1,
            arr_instance_id=3,
            path="/movies/beta.mkv",
            title="Beta",
            tmdbId="200",
        ),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/movies", method="POST", data={"id": "100", "profileid": "4"}):
        response = movies.Movies.post.__wrapped__(movies.Movies())

    rows = schema_session.execute(
        select(TableMovies.id, TableMovies.profileId).order_by(TableMovies.id)
    ).all()
    assert response == ("", 204)
    assert rows == [(100, 4), (200, None)]
    assert captured == [(1, 2)]


def test_movie_profile_update_rejects_ambiguous_upstream_id(schema_session, monkeypatch):
    from api.movies import movies
    from app.database import TableLanguagesProfiles, TableMovies, select

    monkeypatch.setattr(movies, "database", schema_session)
    monkeypatch.setattr(movies, "list_missing_subtitles_movies", lambda **kwargs: None)
    monkeypatch.setattr(movies, "event_stream", lambda **kwargs: None)

    schema_session.add(TableLanguagesProfiles(profileId=4, name="Profile", items="[]"))
    schema_session.add_all([
        TableMovies(id=100, radarrId=1, arr_instance_id=2, path="/movies/alpha.mkv", title="Alpha", tmdbId="100"),
        TableMovies(id=200, radarrId=1, arr_instance_id=3, path="/movies/beta.mkv", title="Beta", tmdbId="200"),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/movies", method="POST", data={"radarrid": "1", "profileid": "4"}):
        response = movies.Movies.post.__wrapped__(movies.Movies())

    rows = schema_session.execute(
        select(TableMovies.id, TableMovies.profileId).order_by(TableMovies.id)
    ).all()
    assert response == ("Ambiguous Radarr movie ID; pass id or arr_instance_id", 400)
    assert rows == [(100, None), (200, None)]


def test_movie_profile_update_scopes_legacy_upstream_id(schema_session, monkeypatch):
    from api.movies import movies
    from app.database import TableLanguagesProfiles, TableMovies, select

    captured = []
    monkeypatch.setattr(movies, "database", schema_session)
    monkeypatch.setattr(
        movies,
        "list_missing_subtitles_movies",
        lambda no, arr_instance_id=None: captured.append((no, arr_instance_id)),
    )
    monkeypatch.setattr(movies, "event_stream", lambda **kwargs: None)

    schema_session.add(TableLanguagesProfiles(profileId=4, name="Profile", items="[]"))
    schema_session.add_all([
        TableMovies(id=100, radarrId=1, arr_instance_id=2, path="/movies/alpha.mkv", title="Alpha", tmdbId="100"),
        TableMovies(id=200, radarrId=1, arr_instance_id=3, path="/movies/beta.mkv", title="Beta", tmdbId="200"),
    ])

    app = Flask(__name__)
    with app.test_request_context(
        "/api/movies",
        method="POST",
        data={"radarrid": "1", "profileid": "4", "arr_instance_id": "3"},
    ):
        response = movies.Movies.post.__wrapped__(movies.Movies())

    rows = schema_session.execute(
        select(TableMovies.id, TableMovies.profileId).order_by(TableMovies.id)
    ).all()
    assert response == ("", 204)
    assert rows == [(100, None), (200, 4)]
    assert captured == [(1, 3)]


def test_movie_search_missing_action_passes_arr_instance_id(monkeypatch):
    from api.movies import movies

    captured = {}
    monkeypatch.setattr(
        movies,
        "movies_download_subtitles",
        lambda no, arr_instance_id=None: captured.update(no=no, arr_instance_id=arr_instance_id),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/movies",
        method="PATCH",
        data={"action": "search-missing", "radarrid": "1", "arr_instance_id": "3"},
    ):
        response = movies.Movies.patch.__wrapped__(movies.Movies())

    assert response == ("", 204)
    assert captured == {"no": 1, "arr_instance_id": 3}


def test_series_profile_update_accepts_local_id(schema_session, monkeypatch):
    from api.series import series
    from app.database import TableLanguagesProfiles, TableShows, select

    captured = []
    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(
        series,
        "list_missing_subtitles",
        lambda no, arr_instance_id=None: captured.append((no, arr_instance_id)),
    )
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)

    schema_session.add(TableLanguagesProfiles(profileId=5, name="Profile", items="[]"))
    schema_session.add_all([
        TableShows(
            id=300,
            sonarrSeriesId=1,
            arr_instance_id=2,
            path="/series/alpha",
            title="Alpha",
        ),
        TableShows(
            id=400,
            sonarrSeriesId=1,
            arr_instance_id=3,
            path="/series/beta",
            title="Beta",
        ),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/series", method="POST", data={"id": "300", "profileid": "5"}):
        response = series.Series.post.__wrapped__(series.Series())

    rows = schema_session.execute(
        select(TableShows.id, TableShows.profileId).order_by(TableShows.id)
    ).all()
    assert response == ("", 204)
    assert rows == [(300, 5), (400, None)]
    assert captured == [(1, 2)]


def test_series_profile_update_rejects_ambiguous_upstream_id(schema_session, monkeypatch):
    from api.series import series
    from app.database import TableLanguagesProfiles, TableShows, select

    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(series, "list_missing_subtitles", lambda **kwargs: None)
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)

    schema_session.add(TableLanguagesProfiles(profileId=5, name="Profile", items="[]"))
    schema_session.add_all([
        TableShows(id=300, sonarrSeriesId=1, arr_instance_id=2, path="/series/alpha", title="Alpha"),
        TableShows(id=400, sonarrSeriesId=1, arr_instance_id=3, path="/series/beta", title="Beta"),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/series", method="POST", data={"seriesid": "1", "profileid": "5"}):
        response = series.Series.post.__wrapped__(series.Series())

    rows = schema_session.execute(
        select(TableShows.id, TableShows.profileId).order_by(TableShows.id)
    ).all()
    assert response == ("Ambiguous Sonarr series ID; pass id or arr_instance_id", 400)
    assert rows == [(300, None), (400, None)]


def test_series_profile_update_scopes_legacy_upstream_id(schema_session, monkeypatch):
    from api.series import series
    from app.database import TableLanguagesProfiles, TableEpisodes, TableShows, select

    captured = []
    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(
        series,
        "list_missing_subtitles",
        lambda no, arr_instance_id=None: captured.append((no, arr_instance_id)),
    )
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)

    schema_session.add(TableLanguagesProfiles(profileId=5, name="Profile", items="[]"))
    schema_session.add_all([
        TableShows(id=300, sonarrSeriesId=1, arr_instance_id=2, path="/series/alpha", title="Alpha"),
        TableShows(id=400, sonarrSeriesId=1, arr_instance_id=3, path="/series/beta", title="Beta"),
    ])
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=500,
        series_id=400,
        sonarrSeriesId=1,
        sonarrEpisodeId=9,
        arr_instance_id=3,
        path="/series/beta/s01e01.mkv",
        title="Pilot",
        season=1,
        episode=1,
    ))

    app = Flask(__name__)
    with app.test_request_context(
        "/api/series",
        method="POST",
        data={"seriesid": "1", "profileid": "5", "arr_instance_id": "3"},
    ):
        response = series.Series.post.__wrapped__(series.Series())

    rows = schema_session.execute(
        select(TableShows.id, TableShows.profileId).order_by(TableShows.id)
    ).all()
    assert response == ("", 204)
    assert rows == [(300, None), (400, 5)]
    assert captured == [(1, 3)]


def test_series_search_missing_action_passes_arr_instance_id(monkeypatch):
    from api.series import series

    captured = {}
    monkeypatch.setattr(
        series,
        "series_download_subtitles",
        lambda no, arr_instance_id=None: captured.update(no=no, arr_instance_id=arr_instance_id),
    )

    app = Flask(__name__)
    with app.test_request_context(
        "/api/series",
        method="PATCH",
        data={"action": "search-missing", "seriesid": "1", "arr_instance_id": "3"},
    ):
        response = series.Series.patch.__wrapped__(series.Series())

    assert response == ("", 204)
    assert captured == {"no": 1, "arr_instance_id": 3}
