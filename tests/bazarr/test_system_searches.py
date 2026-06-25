# coding=utf-8

from types import SimpleNamespace

from flask import Flask
from flask_restx import Api


def _run_search(query):
    from api.system.searches import Searches

    app = Flask(__name__)
    with app.test_request_context(f"/api/system/searches?query={query}"):
        return Searches.get.__wrapped__(Searches())


def _search_settings(use_sonarr=False, use_radarr=False, apikey="secret"):
    return SimpleNamespace(
        auth=SimpleNamespace(apikey=apikey),
        general=SimpleNamespace(use_sonarr=use_sonarr, use_radarr=use_radarr),
    )


def test_search_movie_poster_carries_owner_instance(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableMovies

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", _search_settings(use_radarr=True))

    schema_session.add(TableMovies(
        id=21,
        radarrId=1,
        arr_instance_id=7,
        path="/movies/the-matrix.mkv",
        title="The Matrix",
        tmdbId="603",
        poster="/MediaCover/1/poster-500.jpg?lastWrite=639168628859197277",
        year="1999",
    ))

    results = _run_search("matrix")

    assert results[0]["poster"] == (
        "/images/movies/MediaCover/1/poster-500.jpg"
        "?lastWrite=639168628859197277&arr_instance_id=7"
    )


def test_search_movie_poster_keeps_base_url_prefix(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableMovies

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "/bazarr")
    monkeypatch.setattr(searches, "settings", _search_settings(use_radarr=True))

    schema_session.add(TableMovies(
        id=22,
        radarrId=4,
        arr_instance_id=7,
        path="/movies/dune.mkv",
        title="Dune",
        tmdbId="438631",
        poster="/MediaCover/4/poster-500.jpg",
        year="2021",
    ))

    results = _run_search("dune")

    assert results[0]["poster"] == (
        "/bazarr/images/movies/MediaCover/4/poster-500.jpg?arr_instance_id=7"
    )


def test_search_movie_poster_replaces_existing_arr_instance_query(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableMovies

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", _search_settings(use_radarr=True))

    schema_session.add(TableMovies(
        id=23,
        radarrId=5,
        arr_instance_id=7,
        path="/movies/arrival.mkv",
        title="Arrival",
        tmdbId="329865",
        poster="/MediaCover/5/poster-500.jpg?arr_instance_id=1&lastWrite=222",
        year="2016",
    ))

    results = _run_search("arrival")

    assert results[0]["poster"] == (
        "/images/movies/MediaCover/5/poster-500.jpg?lastWrite=222&arr_instance_id=7"
    )


def test_search_series_poster_carries_owner_instance(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableShows

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", _search_settings(use_sonarr=True))

    schema_session.add(TableShows(
        id=31,
        sonarrSeriesId=2,
        arr_instance_id=8,
        path="/series/the-expanse",
        title="The Expanse",
        poster="/MediaCover/2/poster-250.jpg",
        year="2015",
    ))

    results = _run_search("expanse")

    assert results[0]["poster"] == (
        "/images/series/MediaCover/2/poster-250.jpg?arr_instance_id=8"
    )


def test_search_returns_none_for_missing_poster(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableShows

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", _search_settings(use_sonarr=True))

    schema_session.add(TableShows(
        id=32,
        sonarrSeriesId=5,
        arr_instance_id=8,
        path="/series/no-poster",
        title="No Poster",
        poster=None,
        year="2020",
    ))

    results = _run_search("poster")

    assert results[0]["poster"] is None


def test_search_poster_stays_legacy_for_unowned_media(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableMovies

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", _search_settings(use_radarr=True))

    schema_session.add(TableMovies(
        id=41,
        radarrId=3,
        arr_instance_id=None,
        path="/movies/legacy.mkv",
        title="Legacy Movie",
        tmdbId="1",
        poster="/MediaCover/3/poster-500.jpg?lastWrite=111",
        year="2000",
    ))

    results = _run_search("legacy")

    assert results[0]["poster"] == (
        "/images/movies/MediaCover/3/poster-500.jpg?lastWrite=111"
    )


def test_search_mixed_results_keep_local_ids_and_owners(schema_session, monkeypatch):
    from api.system import searches
    from app.database import TableMovies, TableShows

    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", _search_settings(use_sonarr=True, use_radarr=True))

    schema_session.add(TableShows(
        id=501,
        sonarrSeriesId=1,
        arr_instance_id=2,
        path="/series/shared",
        title="Shared Title",
        poster="/MediaCover/1/poster-250.jpg",
        year="2024",
    ))
    schema_session.add(TableMovies(
        id=901,
        radarrId=1,
        arr_instance_id=3,
        path="/movies/shared.mkv",
        title="Shared Title",
        tmdbId="99",
        poster="/MediaCover/1/poster-500.jpg?lastWrite=999",
        year="2024",
    ))

    results = _run_search("shared")
    by_kind = {
        "series": next(item for item in results if "sonarrSeriesId" in item),
        "movie": next(item for item in results if "radarrId" in item),
    }

    assert by_kind["series"]["id"] == 501
    assert by_kind["series"]["sonarrSeriesId"] == 1
    assert by_kind["series"]["arr_instance_id"] == 2
    assert by_kind["series"]["poster"].endswith("?arr_instance_id=2")
    assert by_kind["movie"]["id"] == 901
    assert by_kind["movie"]["radarrId"] == 1
    assert by_kind["movie"]["arr_instance_id"] == 3
    assert by_kind["movie"]["poster"].endswith("&arr_instance_id=3")


def test_system_searches_route_requires_api_key(schema_session, monkeypatch):
    from api import utils
    from api.system import searches
    from api.system.searches import api_ns_system_searches

    settings = _search_settings(use_radarr=True, apikey="secret")
    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", settings)
    monkeypatch.setattr(utils, "settings", settings)

    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(api_ns_system_searches, "/")

    response = app.test_client().get("/system/searches?query=missing")

    assert response.status_code == 401


def test_system_searches_route_returns_json_with_api_key(schema_session, monkeypatch):
    from api import utils
    from api.system import searches
    from api.system.searches import api_ns_system_searches
    from app.database import TableMovies

    settings = _search_settings(use_radarr=True, apikey="secret")
    monkeypatch.setattr(searches, "database", schema_session)
    monkeypatch.setattr(searches, "base_url", "")
    monkeypatch.setattr(searches, "settings", settings)
    monkeypatch.setattr(utils, "settings", settings)

    schema_session.add(TableMovies(
        id=51,
        radarrId=6,
        arr_instance_id=7,
        path="/movies/api-route.mkv",
        title="API Route",
        tmdbId="6",
        poster="/MediaCover/6/poster-500.jpg",
        year="2026",
    ))

    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(api_ns_system_searches, "/")

    response = app.test_client().get(
        "/system/searches?query=api",
        headers={"X-API-KEY": "secret"},
    )

    assert response.status_code == 200
    assert response.get_json()[0]["poster"] == (
        "/images/movies/MediaCover/6/poster-500.jpg?arr_instance_id=7"
    )
