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


# ---------------------------------------------------------------------------
# F8 regression: manual-download POST must resolve by upstream id, not local id
# ---------------------------------------------------------------------------

def test_movie_manual_download_routes_by_upstream_id_not_local_id(schema_session, monkeypatch):
    """Regression for F8: the POST handler received the upstream radarrId from the
    frontend but looked up the row using TableMovies.id (local autoincrement).
    When one movie's local id numerically equals another movie's radarrId the
    download silently routed to the wrong movie.

    Seed:
      - Movie A: local id=10,  radarrId=99, instance=2  (the target)
      - Movie B: local id=99,  radarrId=77, instance=3  (the red herring: local id 99 == target's radarrId)

    The frontend POST sends radarrid=99, arr_instance_id=2.
    Before the fix the handler resolved TableMovies.id==99, found Movie B, then
    saw arr_instance_id mismatch (3 != 2) and returned 404.
    After the fix it passes radarr_id=99, arr_instance_id=2 straight to the
    downstream function which resolves by (radarrId, instance) and finds Movie A.
    """
    from api.providers import providers_movies
    from app.database import TableMovies

    captured = {}
    monkeypatch.setattr(providers_movies, "database", schema_session)
    monkeypatch.setattr(
        providers_movies,
        "movie_manually_download_specific_subtitle",
        lambda radarr_id, arr_instance_id=None, **kwargs: captured.update(
            radarr_id=radarr_id, arr_instance_id=arr_instance_id
        ),
    )

    schema_session.add_all([
        TableMovies(
            id=10, radarrId=99, arr_instance_id=2,
            path="/movies/target.mkv", title="Target", tmdbId="999",
        ),
        TableMovies(
            id=99, radarrId=77, arr_instance_id=3,
            path="/movies/decoy.mkv", title="Decoy", tmdbId="888",
        ),
    ])
    schema_session.flush()

    app = Flask(__name__)
    with app.test_request_context(
        "/api/providers/movies",
        method="POST",
        data={
            "radarrid": "99",
            "arr_instance_id": "2",
            "hi": "False",
            "forced": "False",
            "original_format": "False",
            "provider": "myprovider",
            "subtitle": "sub123",
        },
    ):
        response = providers_movies.ProviderMovies.post.__wrapped__(
            providers_movies.ProviderMovies()
        )

    assert response == ("", 204), f"Expected 204, got {response}"
    assert captured.get("radarr_id") == 99, f"Wrong radarr_id routed: {captured}"
    assert captured.get("arr_instance_id") == 2, f"Wrong instance routed: {captured}"


def test_episode_manual_download_routes_by_upstream_id_not_local_id(schema_session, monkeypatch):
    """Regression for F8 (episodes): the POST handler received sonarrEpisodeId from
    the frontend but looked up the row using TableEpisodes.id (local autoincrement).

    Seed:
      - Show A: local id=20, sonarrSeriesId=5, instance=2
      - Episode A: local id=30, sonarrEpisodeId=88, sonarrSeriesId=5, instance=2  (target)
      - Show B: local id=40, sonarrSeriesId=9, instance=3
      - Episode B: local id=88, sonarrEpisodeId=55, sonarrSeriesId=9, instance=3  (decoy: local id 88 == target's upstream ep id)

    Frontend sends episodeid=88, seriesid=5, arr_instance_id=2.
    Before the fix: handler resolved TableEpisodes.id==88, found Episode B, saw
    instance mismatch (3 != 2) and returned 404.
    After the fix: passes sonarr_episode_id=88, arr_instance_id=2 to downstream
    which resolves by (sonarrEpisodeId, instance) and finds Episode A.
    """
    from api.providers import providers_episodes
    from app.database import TableEpisodes, TableShows

    captured = {}
    monkeypatch.setattr(providers_episodes, "database", schema_session)
    monkeypatch.setattr(
        providers_episodes,
        "episode_manually_download_specific_subtitle",
        lambda sonarr_series_id, sonarr_episode_id, arr_instance_id=None, **kwargs: captured.update(
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id,
            arr_instance_id=arr_instance_id,
        ),
    )

    schema_session.add_all([
        TableShows(id=20, sonarrSeriesId=5, arr_instance_id=2, path="/series/a", title="ShowA"),
        TableShows(id=40, sonarrSeriesId=9, arr_instance_id=3, path="/series/b", title="ShowB"),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=30, series_id=20, sonarrSeriesId=5, sonarrEpisodeId=88, arr_instance_id=2,
            path="/series/a/s01e01.mkv", title="PilotA", season=1, episode=1,
        ),
        TableEpisodes(
            id=88, series_id=40, sonarrSeriesId=9, sonarrEpisodeId=55, arr_instance_id=3,
            path="/series/b/s01e01.mkv", title="PilotB", season=1, episode=1,
        ),
    ])
    schema_session.flush()

    app = Flask(__name__)
    with app.test_request_context(
        "/api/providers/episodes",
        method="POST",
        data={
            "episodeid": "88",
            "seriesid": "5",
            "arr_instance_id": "2",
            "hi": "False",
            "forced": "False",
            "original_format": "False",
            "provider": "myprovider",
            "subtitle": "sub456",
        },
    ):
        response = providers_episodes.ProviderEpisodes.post.__wrapped__(
            providers_episodes.ProviderEpisodes()
        )

    assert response == ("", 204), f"Expected 204, got {response}"
    assert captured.get("sonarr_episode_id") == 88, f"Wrong episode_id routed: {captured}"
    assert captured.get("sonarr_series_id") == 5, f"Wrong series_id routed: {captured}"
    assert captured.get("arr_instance_id") == 2, f"Wrong instance routed: {captured}"
