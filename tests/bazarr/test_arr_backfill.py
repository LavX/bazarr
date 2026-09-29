# coding=utf-8
"""The startup backfill of the default Sonarr and Radarr instances.

It turns the stored single-instance connection settings into a default
arr_instances row per kind and stamps that instance onto the library rows that
have no owner yet. These cover when it builds an instance and when it must not:
it runs on every start without duplicating anything, leaves an existing
default alone, and with a kind switched off it acts only on rows that have no
owner, never on rows naming an instance that is gone.
"""
from types import SimpleNamespace

from sqlalchemy import insert, select


def _settings(use_sonarr=True, use_radarr=True):
    return SimpleNamespace(
        general=SimpleNamespace(use_sonarr=use_sonarr, use_radarr=use_radarr),
        sonarr=SimpleNamespace(ip="10.0.0.5", port=8989, base_url="/", ssl=False,
                               verify_ssl=False, http_timeout=60,
                               apikey="sonarr-key"),
        radarr=SimpleNamespace(ip="10.0.0.6", port=7878, base_url="/", ssl=True,
                               verify_ssl=False, http_timeout=90,
                               apikey="radarr-key"),
    )


def test_backfill_creates_default_instances_from_scalar_config(schema_session):
    from app.database import TableMovies, TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))
    schema_session.execute(insert(TableMovies).values(
        radarrId=1, path="/m/a.mkv", title="A", tmdbId="10"))

    backfill_default_instances(schema_session, _settings())

    repo = ArrInstanceRepository(schema_session)
    s = repo.get_default("sonarr")
    r = repo.get_default("radarr")
    assert s is not None and s.ip == "10.0.0.5" and s.port == 8989 and s.is_default == 1
    assert r is not None and r.ssl == 1 and r.http_timeout == 90
    # the scalar apikey is encrypted into the instance, decryptable for runtime
    assert repo.get_decrypted_api_key(s.id) == "sonarr-key"
    assert repo.get_decrypted_api_key(r.id) == "radarr-key"


def test_backfill_stamps_existing_owned_rows(schema_session):
    from app.database import TableEpisodes, TableMovies, TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=1, sonarrSeriesId=1, season=1, episode=1,
        path="/tv/a/s01e01.mkv", title="P"))
    schema_session.execute(insert(TableMovies).values(
        radarrId=1, path="/m/a.mkv", title="A", tmdbId="10"))

    backfill_default_instances(schema_session, _settings())
    repo = ArrInstanceRepository(schema_session)
    sid = repo.get_default("sonarr").id
    rid = repo.get_default("radarr").id

    assert schema_session.execute(select(TableShows)).scalar_one().arr_instance_id == sid
    assert schema_session.execute(select(TableEpisodes)).scalar_one().arr_instance_id == sid
    assert schema_session.execute(select(TableMovies)).scalar_one().arr_instance_id == rid


def test_backfill_is_idempotent(schema_session):
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    backfill_default_instances(schema_session, _settings())
    backfill_default_instances(schema_session, _settings())  # second run no-ops

    repo = ArrInstanceRepository(schema_session)
    assert len(repo.list("sonarr")) == 1
    assert len(repo.list("radarr")) == 1


def test_backfill_skips_kind_with_nothing_to_own(schema_session):
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    backfill_default_instances(
        schema_session, _settings(use_sonarr=False, use_radarr=True))

    repo = ArrInstanceRepository(schema_session)
    assert repo.get_default("sonarr") is None
    assert repo.get_default("radarr") is not None


def test_backfill_does_not_clobber_existing_default(schema_session):
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    existing = repo.create("sonarr", "My Sonarr", api_key="manual", ip="1.1.1.1")

    backfill_default_instances(schema_session, _settings())

    assert repo.get_default("sonarr").id == existing.id
    assert repo.get_decrypted_api_key(existing.id) == "manual"


def test_backfill_does_not_resurrect_after_default_demoted(schema_session):
    # Regression: demoting/disabling the only instance left the kind with no
    # default, and a second backfill used to resurrect a duplicate. Backfill
    # must skip a kind that already has ANY instance, not just a default.
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    backfill_default_instances(schema_session, _settings())
    sonarr = repo.get_default("sonarr")
    repo.update(sonarr.id, enabled=False)  # no default for sonarr now

    backfill_default_instances(schema_session, _settings())

    assert len(repo.list("sonarr")) == 1


def test_backfill_with_the_kind_switched_off_ignores_rows_naming_a_gone_instance(schema_session):
    # A row stamped with an instance that no longer exists was left by a write
    # that landed after that instance was deleted. With the kind switched off
    # there is nothing to rebuild an instance for: the backfill only ever
    # stamps rows that have no owner, so this one is no legacy row to adopt.
    from app.database import TableHistory, TableHistoryMovie
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(insert(TableHistory).values(
        arr_instance_id=4242, sonarrSeriesId=1, sonarrEpisodeId=2, action=1,
        description="Downloaded"))
    schema_session.execute(insert(TableHistoryMovie).values(
        arr_instance_id=4243, radarrId=1, action=1, description="Downloaded"))

    summary = backfill_default_instances(
        schema_session, _settings(use_sonarr=False, use_radarr=False))

    assert summary["sonarr"]["created"] is False
    assert summary["radarr"]["created"] is False
    assert ArrInstanceRepository(schema_session).list() == []


def test_backfill_with_the_kind_switched_off_still_adopts_rows_with_no_owner(schema_session):
    # The upgrade from a single-instance database: every row has no owner yet,
    # and they still need an instance even when Use Sonarr is off.
    from app.database import TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))

    summary = backfill_default_instances(
        schema_session, _settings(use_sonarr=False, use_radarr=False))

    assert summary["sonarr"]["created"] is True
    assert summary["radarr"]["created"] is False
    owner = ArrInstanceRepository(schema_session).get_default("sonarr")
    assert schema_session.execute(select(TableShows.arr_instance_id)).scalar_one() == owner.id


def test_backfill_run_by_the_cutover_takes_an_id_no_row_names():
    # The cutover migration runs this backfill before the tables of later
    # migrations exist. On SQLite the instance's id is chosen above every id a
    # row in the tables that are there still names, so a row a writer left
    # naming a deleted instance never becomes this instance's.
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import Session

    from app.database import Base, TableHistory, TableShows
    from arr_instances.backfill import _RADARR_TABLES, _SONARR_TABLES, backfill_default_instances

    # Every owned table a later migration adds, the Sportarr library and the
    # release-type mismatch flags, is left out.
    owned = {model.__table__ for model in _SONARR_TABLES + _RADARR_TABLES}
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        table for table in Base.metadata.sorted_tables
        if table in owned or "arr_instance_id" not in table.c])
    assert "release_type_mismatches" not in sa_inspect(engine).get_table_names()
    session = Session(bind=engine)
    try:
        session.execute(insert(TableShows).values(sonarrSeriesId=1, path="/tv/a", title="A"))
        session.execute(insert(TableHistory).values(
            arr_instance_id=7, sonarrSeriesId=9, sonarrEpisodeId=9, action=1,
            description="Downloaded"))

        summary = backfill_default_instances(session, _settings(use_radarr=False))

        assert summary["sonarr"]["created"] is True
        assert summary["sonarr"]["instance_id"] == 8
        assert session.execute(select(TableShows.arr_instance_id)).scalar_one() == 8
        assert session.execute(select(TableHistory.arr_instance_id)).scalar_one() == 7
    finally:
        session.close()
        engine.dispose()
