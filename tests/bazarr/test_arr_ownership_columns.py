# coding=utf-8
"""Phase 1 additive ownership columns on the media tables (#156).

Adds nullable ``arr_instance_id`` (owning instance) and a local ``id`` (the
future canonical Bazarr id) to ``table_shows`` / ``table_episodes`` /
``table_movies``, plus a local ``series_id`` reference on episodes. Additive
and nullable only - no FK, NOT NULL, or backfill yet; those come in later
increments once writers are converted. Existing single-instance rows keep
working untouched.

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 1).
"""
from sqlalchemy import insert, select


def test_table_shows_has_nullable_ownership_and_local_id(schema_session):
    from app.database import TableShows

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=10, path="/series/show", title="Show",
        arr_instance_id=1, id=1000,
    ))
    row = schema_session.execute(select(TableShows)).scalar_one()
    assert row.arr_instance_id == 1
    assert row.id == 1000


def test_table_shows_ownership_columns_are_nullable(schema_session):
    from app.database import TableShows

    # legacy-style insert without the new columns still works
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=11, path="/series/show2", title="Show 2",
    ))
    row = schema_session.execute(
        select(TableShows).where(TableShows.sonarrSeriesId == 11)
    ).scalar_one()
    assert row.arr_instance_id is None
    assert row.id is None


def test_table_episodes_has_ownership_local_id_and_series_id(schema_session):
    from app.database import TableEpisodes, TableShows

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=10, path="/series/show", title="Show"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=20, sonarrSeriesId=10, season=1, episode=1,
        path="/series/show/s01e01.mkv", title="Pilot",
        arr_instance_id=1, id=2000, series_id=1000,
    ))
    row = schema_session.execute(select(TableEpisodes)).scalar_one()
    assert row.arr_instance_id == 1
    assert row.id == 2000
    assert row.series_id == 1000


def test_table_movies_has_nullable_ownership_and_local_id(schema_session):
    from app.database import TableMovies

    schema_session.execute(insert(TableMovies).values(
        radarrId=30, path="/movies/movie.mkv", title="Movie", tmdbId="100",
        arr_instance_id=2, id=3000,
    ))
    row = schema_session.execute(select(TableMovies)).scalar_one()
    assert row.arr_instance_id == 2
    assert row.id == 3000
