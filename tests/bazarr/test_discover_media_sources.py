"""Source-only library ownership through the Discover API."""
import pytest

import test_discover_copy_matching as copies_fixtures
import test_discover_summary as summary_fixtures

authenticated_client = copies_fixtures.authenticated_client
copy_database = copies_fixtures.copy_database
providers = copies_fixtures.providers
summary_database = summary_fixtures.summary_database
quiet_queue = summary_fixtures.quiet_queue

HEADERS = {"X-API-KEY": "discover-test-key"}


def add_source(session, kind, *, enabled=1, name=None):
    from app.database import TableMediaSourceLibraries, TableMediaSources

    source = TableMediaSources(kind="folder", name=name or f"Local {kind}", enabled=enabled)
    session.add(source)
    session.flush()
    library = TableMediaSourceLibraries(source_id=source.id, content_kind=kind, locator=f"/{kind}")
    session.add(library)
    session.flush()
    return source.id, library.id


def add_title(session, owner, model, **values):
    source_id, library_id = owner
    row = model(source_id=source_id, library_id=library_id, sort_title=values["title"],
                identity_source="user", identity_state="identified", **values)
    session.add(row)
    session.flush()
    return row.id


@pytest.fixture
def catalog_details(monkeypatch):
    from discover import metadata

    def movie_details(identity):
        return {"data": {"item": {"id": int(identity), "media_type": "movie", "source": "tmdb",
                                  "source_id": f"tmdb:movie:{identity}", "title": "Night Film"}}}

    def show_details(identity):
        return {"data": {"item": {"id": int(identity), "media_type": "show", "source": "tmdb",
                                  "source_id": f"tmdb:show:{identity}", "title": "Moon Show"}}}

    monkeypatch.setattr(metadata, "movie_details", movie_details)
    monkeypatch.setattr(metadata, "show_details", show_details)


def test_tmdb_movie_has_a_source_copy_through_details_offer_and_selection(
    authenticated_client, copy_database, catalog_details, providers, monkeypatch, tmp_path,
):
    from app.database import TableSourceItems
    from discover import metadata
    from discover.library import resolve_copy

    _, session = copy_database
    owner = add_source(session, "movie")
    path = tmp_path / "Night.Film.2024.1080p.mkv"
    path.write_bytes(b"movie file")
    item_id = add_title(session, owner, TableSourceItems, kind="movie", title="Night Film", path=str(path),
                        tmdb_id="42", year="2024")
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/movies/42?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    assert [(copy["local_id"], copy["source_id"]) for copy in response.json["data"]["item"]["copies"]] == [
        (item_id, owner[0]),
    ]
    local = authenticated_client.get(f"/api/discover/metadata/movies/source:{item_id}?source=local", headers=HEADERS)
    assert local.status_code == 200
    assert local.json["data"]["item"]["tmdb_id"] == 42
    unavailable = {"data": {"status": "unconfigured", "items": []}}
    monkeypatch.setattr(metadata, "search_movies", lambda _: unavailable)
    monkeypatch.setattr(metadata, "omdb_search", lambda *_: unavailable)
    search = authenticated_client.get("/api/discover/metadata/search?q=Night+Film&source=all", headers=HEADERS)
    assert search.status_code == 200
    assert [item["id"] for item in search.json["data"]["items"]] == [f"source:{item_id}"]

    target = {"media_type": "movie", "tmdb_id": 42}
    response = copies_fixtures.copies(authenticated_client, **target)
    assert response.status_code == 200
    [copy] = response.json["items"]
    assert copy["selectable"] is True
    assert copy["filename"] == path.name
    assert copy["source_id"] == owner[0]
    assert response.json["owning_titles"] == 1
    assert str(tmp_path) not in str(response.json)
    assert resolve_copy(copy["copy_id"], target)["path"] == str(path)
    providers.add("discover_source", copies_fixtures.catalog("discover_source", [("web", path.stem)]))
    selection = {**target, "copy_id": copy["copy_id"], "language": "eng"}
    response = copies_fixtures.post(authenticated_client, selection)
    assert response.status_code == 200
    assert response.json["context"]["copy"]["source_name"] == "Local movie"
    assert str(tmp_path) not in str(response.json)
    video = providers.videos[0][1]
    assert (video.name, video.title, video.tmdb_id, video.imdb_id) == (str(path), "Night Film", 42, None)
    conflict = copies_fixtures.post(authenticated_client, {**selection, "tmdb_id": 43})
    assert conflict.status_code == 409
    assert conflict.json["reason"] == "copy_unavailable"


def test_show_copy_uses_the_groups_identity_and_exact_episode(
    authenticated_client, copy_database, catalog_details, tmp_path,
):
    from app.database import TableSourceGroups, TableSourceItems

    _, session = copy_database
    owner = add_source(session, "series")
    group_id = add_title(session, owner, TableSourceGroups, kind="show", title="Moon Show", group_key="moon",
                         tmdb_id="84", imdb_id="tt0000084")
    episode_id = add_title(session, owner, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id,
                           tmdb_id="99", imdb_id="tt0000099", season=2, episode=3, episode_title="Eclipse",
                           path=str(tmp_path / "Moon.Show.S02E03.mkv"))
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/shows/84?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    item = response.json["data"]["item"]
    assert [copy["local_id"] for copy in item["copies"]] == [group_id]
    assert item["ownership"]["seasons_owned"] == [2]

    params = {"media_type": "episode", "imdb_id": "tt0000084", "season": 2, "episode": 3}
    response = copies_fixtures.copies(authenticated_client, **params)
    assert response.status_code == 200
    assert [(copy["local_id"], copy["series_local_id"], copy["episode_title"]) for copy in response.json["items"]] == [
        (episode_id, group_id, "Eclipse"),
    ]
    response = copies_fixtures.copies(authenticated_client, **{**params, "episode": 4})
    assert response.json["items"] == []
    assert response.json["owning_titles"] == 1
    response = copies_fixtures.copies(authenticated_client, **{**params, "imdb_id": "tt0000099"})
    assert response.json["items"] == []
    assert response.json["owning_titles"] == 0


def test_unidentified_disabled_and_shadowed_source_rows_do_not_create_copy_options(
    authenticated_client, copy_database, catalog_details, tmp_path,
):
    from app.database import TableSourceItems

    _, session = copy_database
    owner = add_source(session, "movie")
    copies_fixtures.add_instance(session, 1, "Radarr")
    copies_fixtures.add_movie(session, 8, 1, tmp_path / "shared.mkv")
    add_title(session, owner, TableSourceItems, kind="movie", title="Night Film", path=str(tmp_path / "unknown.mkv"))
    add_title(session, (owner[0], owner[1]), TableSourceItems, kind="movie", title="Night Film",
              path=str(tmp_path / "shared.mkv"), imdb_id="tt0133093", arr_shadow=1, shadow_instance_id=1,
              shadow_arr_table="movie", shadow_arr_row_id=8)
    disabled = add_source(session, "movie", enabled=0, name="Disabled movies")
    add_title(session, disabled, TableSourceItems, kind="movie", title="Night Film", path=str(tmp_path / "disabled.mkv"),
              tmdb_id="42", imdb_id="tt0133093")
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/movies/42?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    assert response.json["data"]["item"]["copies"] == []
    response = copies_fixtures.copies(authenticated_client, media_type="movie", imdb_id="tt0133093")
    assert [copy["local_id"] for copy in response.json["items"]] == [8]
    assert response.json["owning_titles"] == 1


def test_a_distinct_source_copy_and_arr_copy_can_share_a_row_id(
    authenticated_client, copy_database, catalog_details, tmp_path,
):
    from app.database import TableMovies, TableSourceItems

    _, session = copy_database
    owner = add_source(session, "movie")
    item_id = add_title(session, owner, TableSourceItems, kind="movie", title="Night Film",
                        path=str(tmp_path / "disc.mkv"), tmdb_id="42", imdb_id="tt0133093")
    copies_fixtures.add_instance(session, 1, "Radarr")
    session.add(TableMovies(id=item_id, arr_instance_id=1, radarrId=100, imdbId="tt0133093", tmdbId="42",
                            title="Night Film", path=str(tmp_path / "web.mkv")))
    session.commit()

    response = authenticated_client.get("/api/discover/metadata/movies/42?source=tmdb", headers=HEADERS)
    assert response.status_code == 200
    assert [(copy["local_id"], copy.get("source_id")) for copy in response.json["data"]["item"]["copies"]] == [
        (item_id, None), (item_id, owner[0]),
    ]
    response = copies_fixtures.copies(authenticated_client, media_type="movie", tmdb_id=42, imdb_id="tt0133093")
    assert [copy["copy_id"] for copy in response.json["items"]] == [f"c1.movie.{item_id}.1", f"c1.movie.{item_id}.s{owner[0]}"]


def test_summary_counts_source_files_once(authenticated_client, summary_database, quiet_queue, tmp_path):
    from app.database import TableSourceGroups, TableSourceItems

    session = summary_database.session
    movies = add_source(session, "movie")
    shows = add_source(session, "series")
    group_id = add_title(session, shows, TableSourceGroups, kind="show", title="Moon Show", group_key="moon",
                         tmdb_id="84")
    add_title(session, movies, TableSourceItems, kind="movie", title="Night Film", path=str(tmp_path / "movie.mkv"),
              tmdb_id="42")
    add_title(session, shows, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id, season=1,
              episode=1, path=str(tmp_path / "episode.mkv"))
    add_title(session, shows, TableSourceItems, kind="episode", title="Moon Show", group_id=group_id, season=1,
              episode=2, path=str(tmp_path / "managed.mkv"), arr_shadow=1)
    session.commit()

    response = authenticated_client.get("/api/discover/summary", headers=HEADERS)
    assert response.status_code == 200
    library = response.json["library"]
    assert (library["movies"], library["series"], library["episodes"]) == (1, 1, 1)
