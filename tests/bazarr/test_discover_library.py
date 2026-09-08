"""Bounded identity-only library reads on both supported database engines."""
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session


@pytest.fixture(params=["sqlite", "postgresql"])
def library_database(request, monkeypatch):
    from app import database as db
    from app.config import settings
    from subliminal_patch.refiners import omdb
    monkeypatch.setattr(settings.discover, "tmdb_access_token", "")
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: None)
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.fail("Dedicated PostgreSQL test URL is required")
        engine = sa.create_engine(url)
        schema = "discover_" + uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
    else:
        engine = sa.create_engine("sqlite://")
        schema = None
    db.Base.metadata.create_all(engine)
    session = Session(engine)
    monkeypatch.setattr(db, "engine", engine)
    try:
        yield engine, session
    finally:
        session.close()
        if schema:
            with engine.begin() as connection:
                connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def add_movie(session, local_id, owner, imdb="tt0000042", tmdb="42", title="Example", year="2024"):
    from app.database import TableMovies
    session.add(TableMovies(id=local_id, arr_instance_id=owner, radarrId=7, imdbId=imdb,
                            tmdbId=tmdb, title=title, year=year, path=f"/private/{local_id}.mkv"))


def test_copies_keep_canonical_identity_and_never_mutate(library_database):
    from discover import metadata
    from app.database import Base
    engine, session = library_database
    add_movie(session, 5, 1)
    add_movie(session, 8, 2, title="Different edition title", tmdb=" 0042 ", imdb=" TT0000042 ")
    session.commit()
    before = {t.name: session.execute(sa.select(t)).all() for t in Base.metadata.sorted_tables}
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        data = metadata.candidates("Example", "movie")["data"]
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    local = next(item for item in data["items"] if item["source"] == "local")
    assert {copy["arr_instance_id"] for copy in local["copies"]} == {1, 2}
    assert {copy["local_id"] for copy in local["copies"]} == {5, 8}
    assert all(not copy.get("selected") for copy in local["copies"])
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert "private" not in str(data)
    assert {t.name: session.execute(sa.select(t)).all() for t in Base.metadata.sorted_tables} == before


def test_local_ownership_is_canonical_and_owner_scoped(library_database, monkeypatch):
    from discover import metadata
    from app.database import TableShows, TableEpisodes
    _, session = library_database
    for local_id, owner in [(5, 1), (8, 2), (9, None)]:
        session.add(TableShows(id=local_id, arr_instance_id=owner, sonarrSeriesId=7,
                               imdbId="tt0000042", tvdbId=42, title="Example", path=f"/shows/{local_id}"))
    session.flush()
    for i, parent, owner in [(1, 5, 1), (2, 5, 1), (3, 8, 2), (4, 5, 2), (5, 9, None)]:
        session.add(TableEpisodes(id=i, series_id=parent, arr_instance_id=owner,
                                 sonarrEpisodeId=i, sonarrSeriesId=7, season=1, episode=i,
                                 title="Episode", path=f"/episodes/{i}"))
    session.commit()
    item = metadata.candidates("Example", "show")["data"]["items"][0]
    assert {c["local_id"]: c["episode_count"] for c in item["copies"]} == {5: 2, 8: 1, 9: None}
    assert item["ownership"]["episode_count"] == 3
    assert item["ownership"]["selected_episode_owned"] is None
    assert item["ownership"]["complete_series"] is None
    from app.config import settings
    monkeypatch.setattr(settings.discover, "tmdb_access_token", "synthetic-primary-ownership")
    def primary(config, path, params=None):
        if path.endswith("/external_ids"):
            return {"id": 100, "imdb_id": "tt0000042", "tvdb_id": 42}
        return {"id": 100, "name": "Example", "first_air_date": "2024-01-01", "seasons": []}
    monkeypatch.setattr(metadata, "_request", primary)
    primary_item = metadata.title_details("100", "show", "tmdb")["data"]["item"]
    assert primary_item["source_id"] == "tmdb:show:100"
    assert primary_item["ownership"] == item["ownership"]
    assert {copy["local_id"]: copy["episode_count"] for copy in primary_item["copies"]} == {5: 2, 8: 1, 9: None}


@pytest.mark.parametrize("query,title", [("50%_done\\path", "A 50%_done\\path film"), ("Örvény", "Örvény"), ("  EXAMPLE  ", "Example")])
def test_literal_binding_and_malformed_ids(library_database, query, title):
    from discover.library import local_candidates
    engine, session = library_database
    add_movie(session, 1, 1, imdb="malformed", tmdb="42oops", title=title, year="20xx")
    add_movie(session, 2, 2, title="A 500xdoneZpath film")
    session.commit()
    captured = []
    def record(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        result = local_candidates(query, "movie")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    # Unicode case-folding varies by database collation; an unchanged accented
    # query is not a promise of accent-normalized exhaustive search.
    if query == "Örvény" and engine.dialect.name == "sqlite":
        assert result["items"] == []
    else:
        assert [item["id"] for item in result["items"]] == [1]
        assert result["items"][0]["imdb_id"] is None
        assert result["items"][0]["tmdb_id"] is None
        assert result["items"][0]["year"] is None
    assert result["match_scope"] == "literal_title_contains"
    assert all(query.strip() not in sql for sql, _ in captured)
    assert all(" LIMIT " in sql or "count(" in sql for sql, _ in captured)


def test_colliding_upstream_ids_remakes_and_conflicts_stay_distinct(library_database):
    from discover.library import local_candidates
    _, session = library_database
    add_movie(session, 1, 1, imdb="tt0000001", tmdb="41", title="Example", year="1980")
    add_movie(session, 2, 2, imdb="tt0000002", tmdb="42", title="Example", year="2024")
    add_movie(session, 3, 3, imdb="tt0000001", tmdb="42", title="Example", year="1980")
    add_movie(session, 4, None, imdb=None, tmdb="invalid", title="Example", year="1980")
    session.commit()
    items = local_candidates("Example", "movie")["items"]
    assert len(items) == 4
    assert {item["copies"][0]["local_id"] for item in items} == {1, 2, 3, 4}
    assert all(len(item["copies"]) == 1 for item in items)


def test_shortlist_and_copy_expansion_have_separate_honest_caps(library_database):
    from discover.library import COPY_LIMIT, local_candidates
    _, session = library_database
    for i in range(1, COPY_LIMIT + 8):
        add_movie(session, i, i, title="Example" if i == 1 else "Other edition")
    session.commit()
    result = local_candidates("Example", "movie")
    assert len(result["items"]) == 1
    assert len(result["items"][0]["copies"]) == COPY_LIMIT
    assert result["items"][0]["copies_truncated"] is True
    for i in range(200, 260):
        add_movie(session, i, i, imdb=f"tt{i:07}", tmdb=str(i), title="Many titles")
    session.commit()
    limited = local_candidates("Many", "movie", limit=3)
    assert len(limited["items"]) == 3
    assert limited["truncated"] is True


def test_local_detail_uses_kind_and_canonical_id_without_autoflush(library_database):
    from app.database import TableShows, TableMovies
    from discover.library import local_details
    _, session = library_database
    add_movie(session, 5, 1)
    session.add(TableShows(id=5, arr_instance_id=2, sonarrSeriesId=999, title="Different show", path="/private/show"))
    session.commit()
    session.get(TableMovies, 5).title = "Pending unsaved edit"
    assert local_details("5", "movie")["title"] == "Example"
    assert local_details("5", "show")["title"] == "Different show"
    assert session.dirty


def test_merge_does_not_use_cross_kind_or_title_only_identity():
    from discover.library import merge_candidates
    items = [{"media_type": kind, "source_id": f"local:{kind}:{local_id}", "title": "Same name", "imdb_id": imdb,
              "copies": [{"local_id": local_id}]} for kind, local_id, imdb in [
                  ("movie", 5, None), ("movie", 8, None), ("show", 5, "tt0000042"), ("movie", 9, "tt0000042")]]
    assert len(merge_candidates(items)) == 4


@pytest.mark.parametrize("kind", ["movie", "show"])
@pytest.mark.parametrize("ordering", [(41, 42, None, 41), (None, 42, 41, 41), (41, None, 41, 42)])
def test_partial_conflict_preserves_seeds_and_order_independent_copy_groups(library_database, kind, ordering):
    from app.database import Base, TableShows, TableEpisodes
    from discover.library import local_candidates, local_details
    engine, session = library_database
    for identity, external in enumerate(ordering, 1):
        if kind == "movie":
            add_movie(session, identity, identity, imdb="tt0000001", tmdb=str(external) if external else "invalid")
        else:
            session.add(TableShows(id=identity, arr_instance_id=identity, sonarrSeriesId=7,
                                   imdbId="tt0000001", tvdbId=external, title="Example", path=f"/private/{identity}"))
    session.flush()
    if kind == "show":
        for identity in range(1, 5):
            session.add(TableEpisodes(id=identity, series_id=identity, arr_instance_id=identity,
                                     sonarrSeriesId=7, sonarrEpisodeId=7, season=1, episode=identity,
                                     title="Episode", path=f"/private/episode-{identity}"))
        session.add(TableEpisodes(id=5, series_id=1, arr_instance_id=99, sonarrSeriesId=7,
                                 sonarrEpisodeId=7, season=1, episode=5, title="Wrong owner", path="/private/wrong"))
    session.commit()
    before = {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables}
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        candidates = local_candidates("Example", kind)["items"]
        assert len(candidates) == 3
        left = {i for i, external in enumerate(ordering, 1) if external == 41}
        expected = {i: left if external == 41 else {i} for i, external in enumerate(ordering, 1)}
        for item in candidates:
            assert {copy["local_id"] for copy in item["copies"]} == expected[item["id"]]
        for identity, external in enumerate(ordering, 1):
            detail = local_details(str(identity), kind)
            assert {copy["local_id"] for copy in detail["copies"]} == expected[identity]
            assert detail["tmdb_id" if kind == "movie" else "tvdb_id"] == external
            assert {copy["arr_instance_id"] for copy in detail["copies"]} == expected[identity]
            if kind == "show":
                assert detail["ownership"]["episode_count"] == len(expected[identity])
                assert detail["ownership"]["selected_episode_owned"] is None
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables} == before


@pytest.mark.parametrize("kind", ["movie", "show"])
@pytest.mark.parametrize("ordering", [(41, 42, None), (None, 42, 41), (41, None, 42)])
def test_conflict_outside_title_shortlist_survives_final_grouping(library_database, kind, ordering):
    from app.database import Base, TableShows, TableEpisodes
    from discover.library import local_candidates, local_details
    engine, session = library_database
    for identity, external in enumerate(ordering, 1):
        title = "Other title" if external == 42 else "Example"
        if kind == "movie":
            add_movie(session, identity, identity, imdb="tt0000001", tmdb=str(external) if external else "invalid", title=title)
        else:
            session.add(TableShows(id=identity, arr_instance_id=identity, sonarrSeriesId=7,
                                   imdbId="tt0000001", tvdbId=external, title=title, path=f"/private/{identity}"))
    session.flush()
    if kind == "show":
        for identity in range(1, 4):
            session.add(TableEpisodes(id=identity, series_id=identity, arr_instance_id=identity,
                                     sonarrSeriesId=7, sonarrEpisodeId=7, season=1, episode=identity,
                                     title="Episode", path=f"/private/episode-{identity}"))
        session.add(TableEpisodes(id=4, series_id=1, arr_instance_id=99, sonarrSeriesId=7,
                                 sonarrEpisodeId=7, season=1, episode=4, title="Wrong owner", path="/private/wrong"))
    session.commit()
    before = {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables}
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        items = local_candidates("Example", kind)["items"]
        assert {item["id"]: [copy["local_id"] for copy in item["copies"]] for item in items} == {
            i: [i] for i, value in enumerate(ordering, 1) if value != 42}
        for item in items:
            assert item["tmdb_id" if kind == "movie" else "tvdb_id"] == ordering[item["id"] - 1]
            if kind == "show":
                assert item["ownership"]["episode_count"] == sum(copy["episode_count"] for copy in item["copies"]) == 1
        for identity, external in enumerate(ordering, 1):
            item = local_details(str(identity), kind)
            assert [copy["local_id"] for copy in item["copies"]] == [identity]
            assert item["tmdb_id" if kind == "movie" else "tvdb_id"] == external
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables} == before
