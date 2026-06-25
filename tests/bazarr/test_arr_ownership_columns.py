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


def test_table_shows_legacy_insert_autoincrements_local_id(schema_session):
    from app.database import TableShows

    # Post-flip: a legacy-style insert without the new columns still works; the
    # owner stays nullable, but the local id is the PK so it autoincrements
    # rather than staying NULL.
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=11, path="/series/show2", title="Show 2",
    ))
    row = schema_session.execute(
        select(TableShows).where(TableShows.sonarrSeriesId == 11)
    ).scalar_one()
    assert row.arr_instance_id is None
    assert row.id is not None  # local id PK autoincremented


def test_table_episodes_has_ownership_local_id_and_series_id(schema_session):
    from app.database import TableEpisodes, TableShows

    # Post-flip series_id is an FK to table_shows.id, so the parent must exist.
    schema_session.execute(insert(TableShows).values(
        id=1000, sonarrSeriesId=10, path="/series/show", title="Show"))
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


def _seed_show_episode_movie(schema_session):
    # Post-flip the history/blacklist local refs are FKs to the parents' local
    # id, so parent rows must exist (id 1000 show, 2000 episode, 3000 movie).
    from app.database import TableEpisodes, TableMovies, TableShows

    schema_session.execute(insert(TableShows).values(
        id=1000, sonarrSeriesId=10, path="/series/show", title="Show"))
    schema_session.execute(insert(TableEpisodes).values(
        id=2000, sonarrEpisodeId=20, sonarrSeriesId=10, series_id=1000, season=1,
        episode=1, path="/series/show/s01e01.mkv", title="Pilot"))
    schema_session.execute(insert(TableMovies).values(
        id=3000, radarrId=30, path="/movies/movie.mkv", title="Movie", tmdbId="100"))


def test_table_history_has_ownership_and_local_refs(schema_session):
    from app.database import TableHistory

    _seed_show_episode_movie(schema_session)
    schema_session.execute(insert(TableHistory).values(
        action=1, description="x",
        arr_instance_id=1, series_id=1000, episode_id=2000,
    ))
    row = schema_session.execute(select(TableHistory)).scalar_one()
    assert row.arr_instance_id == 1
    assert row.series_id == 1000
    assert row.episode_id == 2000


def test_table_history_movie_has_ownership_and_local_ref(schema_session):
    from app.database import TableHistoryMovie

    _seed_show_episode_movie(schema_session)
    schema_session.execute(insert(TableHistoryMovie).values(
        action=1, description="x",
        arr_instance_id=2, movie_id=3000,
    ))
    row = schema_session.execute(select(TableHistoryMovie)).scalar_one()
    assert row.arr_instance_id == 2
    assert row.movie_id == 3000


def test_table_blacklist_has_ownership_and_local_refs(schema_session):
    from app.database import TableBlacklist

    _seed_show_episode_movie(schema_session)
    schema_session.execute(insert(TableBlacklist).values(
        arr_instance_id=1, series_id=1000, episode_id=2000,
    ))
    row = schema_session.execute(select(TableBlacklist)).scalar_one()
    assert row.arr_instance_id == 1
    assert row.series_id == 1000
    assert row.episode_id == 2000


def test_table_blacklist_movie_has_ownership_and_local_ref(schema_session):
    from app.database import TableBlacklistMovie

    _seed_show_episode_movie(schema_session)
    schema_session.execute(insert(TableBlacklistMovie).values(
        arr_instance_id=2, movie_id=3000,
    ))
    row = schema_session.execute(select(TableBlacklistMovie)).scalar_one()
    assert row.arr_instance_id == 2
    assert row.movie_id == 3000


def test_rootfolder_tables_have_ownership_and_identity_columns(schema_session):
    # Both rootfolder tables gain arr_instance_id plus an upstream/local id
    # split. The same upstream rootfolder id can later live under two
    # instances; the scoped unique constraint enforcing that lands in a
    # later increment.
    from app.database import TableMoviesRootfolder, TableShowsRootfolder

    schema_session.execute(insert(TableShowsRootfolder).values(
        id=1, path="/tv", arr_instance_id=1,
        upstream_rootfolder_id=1, local_rootfolder_id=500,
    ))
    schema_session.execute(insert(TableMoviesRootfolder).values(
        id=1, path="/movies", arr_instance_id=2,
        upstream_rootfolder_id=1, local_rootfolder_id=600,
    ))
    s = schema_session.execute(select(TableShowsRootfolder)).scalar_one()
    m = schema_session.execute(select(TableMoviesRootfolder)).scalar_one()
    assert (s.arr_instance_id, s.upstream_rootfolder_id, s.local_rootfolder_id) == (1, 1, 500)
    assert (m.arr_instance_id, m.upstream_rootfolder_id, m.local_rootfolder_id) == (2, 1, 600)
