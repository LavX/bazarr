# coding=utf-8
"""The startup backfill of the default Sonarr and Radarr instances.

It turns the stored single-instance connection settings into a default
arr_instances row per kind and stamps that instance onto the library rows that
have no owner yet. These cover when it builds an instance and when it must not:
it runs on every start without duplicating anything, leaves an existing
default alone, and with a kind switched off it acts only on rows that have no
owner, never on rows naming an instance that is gone. Once an instance has
been deleted, its kind's ownerless rows may be straggler writes of the deleted
server, so they are quarantined instead of stamped onto the survivor or used
to rebuild it, and a row whose upstream id the stamping instance already owns
is skipped alone instead of aborting every kind's stamp. The collision retries
keep the ownerless filter, so a retry never touches a row another owner holds,
and one colliding group never blocks another table's or kind's stamp. A
collision inside the NULL-upstream group quarantines only the row that
collided, and the refusal of a rebuild names the deployment's retirement, not
the kind's own.
"""
import logging
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


def test_backfill_quarantines_ownerless_rows_of_a_kind_with_a_deleted_instance(
        schema_session, caplog):
    # After a secondary instance is deleted, a straggler writer can still leave
    # a row with no owner behind: a one-item sync past its default check, an
    # exclusion whose episode row is already gone. The resume path used to
    # stamp every ownerless row onto the single remaining instance, putting the
    # deleted server's media under the default one, under its paths and its
    # credentials. With a retirement on record the rows stay ownerless: the
    # scoped reads and the delete-guard already handle rows with no owner.
    from app.database import TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    repo.create("sonarr", "Default", api_key="k")
    secondary = repo.create("sonarr", "Secondary", api_key="k2")
    repo.delete(secondary.id)  # its id is retired on record
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/secondary", title="Straggler"))  # no owner

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(
            schema_session, _settings(use_radarr=False))

    assert schema_session.execute(
        select(TableShows.arr_instance_id)).scalar_one() is None
    assert summary["sonarr"]["stamped"] == 0
    assert summary["sonarr"]["quarantined"] == 1
    assert any("Quarantined 1 ownerless sonarr row" in record.getMessage()
               for record in caplog.records)


def test_backfill_skips_an_ownerless_row_the_instance_already_own_and_stamps_the_rest(
        schema_session, caplog):
    # The same series, episode or movie can be tracked by both the default and
    # a deleted secondary, so an ownerless row can carry an upstream id the
    # remaining instance already owns. The bulk stamp used to die on the unique
    # index (ux_table_episodes_instance_upstream_id) and abort the whole
    # backfill, every other kind's stamp included. The colliding row alone is
    # skipped and logged; every other row stamps.
    from app.database import TableEpisodes
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sonarr", "Sonarr", api_key="k")
    schema_session.execute(insert(TableEpisodes).values(
        arr_instance_id=instance.id, sonarrEpisodeId=5, sonarrSeriesId=1,
        season=1, episode=1, path="/tv/a/s01e01.mkv", title="Owned"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=5, sonarrSeriesId=2, season=1, episode=1,
        path="/tv/b/s01e01.mkv", title="Orphaned duplicate"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=6, sonarrSeriesId=2, season=1, episode=2,
        path="/tv/b/s01e02.mkv", title="Orphaned unique"))

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(
            schema_session, _settings(use_radarr=False))

    owners = dict(schema_session.execute(
        select(TableEpisodes.path, TableEpisodes.arr_instance_id)).all())
    assert owners["/tv/a/s01e01.mkv"] == instance.id
    assert owners["/tv/b/s01e01.mkv"] is None  # the colliding row stays ownerless
    assert owners["/tv/b/s01e02.mkv"] == instance.id
    assert summary["sonarr"]["stamped"] == 1
    assert any("Quarantined an ownerless table_episodes row" in record.getMessage()
               for record in caplog.records)


def test_backfill_does_not_rebuild_a_kind_whose_last_instance_was_deleted(
        schema_session, caplog):
    # A kind whose last instance was deliberately deleted had it rebuilt from
    # the scalar connection settings, which still describe the deleted server,
    # with every ownerless row stamped onto the copy. The switch-off after the
    # delete is best effort, and a straggler row can land after it, so Use
    # being on with an ownerless row present is no reason to resurrect it.
    from app.database import TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    only = repo.create("sonarr", "Only", api_key="k")
    repo.delete(only.id)  # the kind's last instance goes; its id is retired
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/only", title="Straggler"))  # no owner

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(
            schema_session, _settings(use_radarr=False))

    assert summary["sonarr"]["created"] is False
    assert summary["sonarr"]["reason"] == "deployment has a retired instance id"
    assert ArrInstanceRepository(schema_session).list("sonarr") == []
    assert schema_session.execute(
        select(TableShows.arr_instance_id)).scalar_one() is None
    assert any("Refused to rebuild the sonarr instance" in record.getMessage()
               for record in caplog.records)


def test_backfill_refusal_names_the_deployments_retirement_not_the_kinds_own(
        schema_session, caplog):
    # The retired-id table is deployment-wide: it keeps no kind column, so the
    # retirement on record may be the other kind's. The refusal used to
    # attribute it to the kind it was refusing, saying a sonarr instance was
    # deleted when only radarr was ever retired. Both the log line and the
    # summary reason now name the deployment's retirement instead.
    from app.database import TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    radarr = repo.create("radarr", "Radarr", api_key="k")
    repo.delete(radarr.id)  # radarr retires; sonarr was never deleted
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="Legacy"))  # no owner

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(
            schema_session, _settings(use_radarr=False))

    assert summary["sonarr"]["created"] is False
    assert summary["sonarr"]["reason"] == "deployment has a retired instance id"
    assert summary["sonarr"]["quarantined"] == 1
    assert ArrInstanceRepository(schema_session).list("sonarr") == []
    assert schema_session.execute(
        select(TableShows.arr_instance_id)).scalar_one() is None
    refusals = [record.getMessage() for record in caplog.records
                if "Refused to rebuild" in record.getMessage()]
    assert any("Refused to rebuild the sonarr instance" in message
               for message in refusals)
    assert any("deployment has a retired instance id" in message
               for message in refusals)
    assert all("deleted sonarr instance" not in message for message in refusals)


def test_backfill_collision_retries_never_touch_a_row_another_owner_owns(
        schema_session, caplog):
    # The bulk stamp collides on an unrelated group and the table is retried
    # row-group by row-group. Those retries used to match only the group's
    # upstream id, so a retry also updated rows other owners already held: an
    # ownerless row whose upstream id another owner carries was stamped
    # together with that owner's row, the two collided on the unique index and
    # the stampable orphan was left quarantined, while on a NULL upstream id,
    # whose duplicates the unique index cannot reject, the other owner's row
    # itself was reassigned. Every retry keeps the ownerless filter.
    from app.database import TableEpisodes
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sonarr", "Sonarr", api_key="k")
    other, gone = 4243, 4242  # rows naming owners that are not this instance
    schema_session.execute(insert(TableEpisodes).values(
        arr_instance_id=instance.id, sonarrEpisodeId=6, sonarrSeriesId=1,
        season=1, episode=1, path="/tv/a/s01e01.mkv", title="Owned"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=6, sonarrSeriesId=2, season=1, episode=1,
        path="/tv/b/s01e01.mkv", title="Unrelated collision"))
    schema_session.execute(insert(TableEpisodes).values(
        arr_instance_id=other, sonarrEpisodeId=5, sonarrSeriesId=3,
        season=1, episode=2, path="/tv/c/s01e02.mkv", title="Another owner"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=5, sonarrSeriesId=4, season=1, episode=2,
        path="/tv/d/s01e02.mkv", title="Orphan under another owner"))
    schema_session.execute(insert(TableEpisodes).values(
        arr_instance_id=gone, sonarrSeriesId=5, season=1, episode=3,
        path="/tv/e/s01e03.mkv", title="Another owner without an upstream id"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrSeriesId=5, season=1, episode=3,
        path="/tv/f/s01e03.mkv", title="Orphan without an upstream id"))

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(
            schema_session, _settings(use_radarr=False))

    owners = dict(schema_session.execute(
        select(TableEpisodes.path, TableEpisodes.arr_instance_id)).all())
    assert owners["/tv/a/s01e01.mkv"] == instance.id
    assert owners["/tv/b/s01e01.mkv"] is None  # the unrelated collision stays ownerless
    assert owners["/tv/d/s01e02.mkv"] == instance.id  # the orphan stamps
    assert owners["/tv/c/s01e02.mkv"] == other  # the other owner keeps its row
    assert owners["/tv/f/s01e03.mkv"] == instance.id  # the NULL-id orphan stamps
    assert owners["/tv/e/s01e03.mkv"] == gone  # and the other owner keeps its row
    assert summary["sonarr"]["stamped"] == 2
    assert any("Quarantined an ownerless table_episodes row (sonarrEpisodeId = 6)"
               in record.getMessage() for record in caplog.records)


def test_backfill_collision_isolates_to_its_group_across_tables_and_kinds(
        schema_session, caplog):
    # The savepoint recovery is per table and per kind: an episode collision
    # quarantines only that episode group, while the same kind's other rows,
    # another table's rows and the live Radarr kind's rows (movies and their
    # own rootfolder table) all stamp, and the backfill returns cleanly with
    # a summary for every kind.
    from app.database import (
        TableEpisodes, TableMovies, TableMoviesRootfolder, TableShows,
        TableShowsRootfolder,
    )
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    sonarr = repo.create("sonarr", "Sonarr", api_key="k")
    schema_session.execute(insert(TableEpisodes).values(
        arr_instance_id=sonarr.id, sonarrEpisodeId=5, sonarrSeriesId=1,
        season=1, episode=1, path="/tv/a/s01e01.mkv", title="Owned"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=5, sonarrSeriesId=2, season=1, episode=1,
        path="/tv/b/s01e01.mkv", title="Colliding orphan"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=6, sonarrSeriesId=2, season=1, episode=2,
        path="/tv/b/s01e02.mkv", title="Uncollided orphan"))
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))
    schema_session.execute(insert(TableShowsRootfolder).values(
        upstream_rootfolder_id=1, path="/tv"))
    schema_session.execute(insert(TableMovies).values(
        radarrId=1, path="/m/a.mkv", title="A", tmdbId="10"))
    schema_session.execute(insert(TableMoviesRootfolder).values(
        upstream_rootfolder_id=1, path="/movies"))

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(schema_session, _settings())

    radarr = repo.get_default("radarr")
    assert radarr is not None
    owners = dict(schema_session.execute(
        select(TableEpisodes.path, TableEpisodes.arr_instance_id)).all())
    assert owners["/tv/a/s01e01.mkv"] == sonarr.id
    assert owners["/tv/b/s01e01.mkv"] is None  # only the colliding group quarantines
    assert owners["/tv/b/s01e02.mkv"] == sonarr.id
    assert schema_session.execute(
        select(TableShows.arr_instance_id)).scalar_one() == sonarr.id
    assert schema_session.execute(
        select(TableShowsRootfolder.arr_instance_id)).scalar_one() == sonarr.id
    assert schema_session.execute(
        select(TableMovies.arr_instance_id)).scalar_one() == radarr.id
    assert schema_session.execute(
        select(TableMoviesRootfolder.arr_instance_id)).scalar_one() == radarr.id
    assert summary["sonarr"]["stamped"] == 3  # episode 6, the show, the rootfolder
    assert summary["radarr"]["created"] is True and summary["radarr"]["stamped"] == 2
    assert any("Quarantined an ownerless table_episodes row" in record.getMessage()
               for record in caplog.records)


def test_backfill_a_null_upstream_collision_quarantines_only_the_colliding_row(
        schema_session, caplog):
    # The NULL-upstream retry used to stamp every ownerless NULL row in one
    # statement, so a collision on another of the instance-scoped unique
    # indexes (a show's path) rolled the whole statement back and quarantined
    # every other NULL row with it. The NULL rows are now stamped one by one,
    # so only the colliding row stays ownerless and the rest stamp.
    from app.database import TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sonarr", "Sonarr", api_key="k")
    schema_session.execute(insert(TableShows).values(
        arr_instance_id=instance.id, path="/tv/a", title="Owned"))
    schema_session.execute(insert(TableShows).values(
        path="/tv/a", title="Colliding path"))  # NULL upstream id, owned path
    schema_session.execute(insert(TableShows).values(
        path="/tv/b", title="Unique path"))  # NULL upstream id, unique path

    with caplog.at_level(logging.INFO, logger="arr_instances.backfill"):
        summary = backfill_default_instances(
            schema_session, _settings(use_radarr=False))

    owners = dict(schema_session.execute(
        select(TableShows.title, TableShows.arr_instance_id)).all())
    assert owners["Owned"] == instance.id
    assert owners["Colliding path"] is None  # the colliding row stays ownerless
    assert owners["Unique path"] == instance.id  # the rest of the NULL group stamps
    assert summary["sonarr"]["stamped"] == 1
    assert any("Quarantined an ownerless table_shows row" in record.getMessage()
               for record in caplog.records)
