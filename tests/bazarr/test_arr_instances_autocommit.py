# coding=utf-8
"""Sonarr and Radarr instance writes on the application's AUTOCOMMIT engine.

app/database.py builds both engines with isolation_level="AUTOCOMMIT". The
repository keeps its multi-step default election atomic with a SAVEPOINT, and
PostgreSQL rejects a bare SAVEPOINT outside a transaction block, so every
create, every update that sets enabled or the default (which the edit dialog
always sends), every delete, and the startup backfill of the default instance
failed there with NoActiveSqlTransaction. SQLite starts a transaction for a
SAVEPOINT on its own, and the transactional test sessions the other suites use
open one first, which is why only a real PostgreSQL server showed it.

Every case runs on both backends, each with an AUTOCOMMIT engine built the way
production builds it, and checks the result through a separate connection so
that a write the session saw but never committed cannot pass.
"""
import logging
import os
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
ROOT = "/api/system/arr-instances"


@pytest.fixture(params=["sqlite", "postgresql"])
def arr_engine(request, tmp_path):
    from app.database import Base
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.fail("Dedicated PostgreSQL test URL is required")
        schema = "arr_autocommit_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        # search_path rather than schema_translate_map: the backfill stamps
        # owned rows with raw SQL, which a translate map does not rewrite.
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True,
                                  pool_size=5, max_overflow=10, pool_pre_ping=True,
                                  connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}", poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")

        # As app/database.py configures it: the foreign keys are enforced, so
        # the order library rows are deleted in matters here as it does there.
        @sa.event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        if schema:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


@pytest.fixture
def arr_session(arr_engine):
    session = sessionmaker(bind=arr_engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def stored(engine):
    """Rows as a fresh connection sees them: only what really committed."""
    with engine.connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT kind, name, enabled, is_default FROM arr_instances ORDER BY id")).all()
    return [tuple(row) for row in rows]


def driver_idle(session):
    """Whether the session's own connection has no open server transaction.

    psycopg2, the production driver, sends nothing on rollback() in
    autocommit mode, so a BEGIN left open here would go back to the pool idle
    in a transaction. sqlite3 and psycopg 3 would quietly end it on rollback(),
    which is why this is asked before the session is rolled back.
    """
    driver = session.connection().connection.driver_connection
    if session.get_bind().dialect.name == "sqlite":
        return not driver.in_transaction
    return driver.info.transaction_status == 0


@pytest.fixture
def arr_api(arr_engine, monkeypatch):
    """The real Flask resource, with the request hooks app/app.py installs."""
    from flask import Flask

    from api import api_bp
    from api.system import arr_instances as resource
    from app.config import settings as app_settings
    from arr_instances import service

    database = scoped_session(sessionmaker(bind=arr_engine, autoflush=False,
                                           expire_on_commit=False))
    monkeypatch.setitem(app_settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(resource, "database", database)
    refreshed = []
    monkeypatch.setattr(service, "refresh_runtime",
                        lambda kind, instance_id=None, removed=False:
                        refreshed.append((kind, instance_id, removed)))
    emitted = []
    monkeypatch.setattr(service, "event_stream", lambda **event: emitted.append(event))
    app = Flask(__name__)

    @app.before_request
    def _db_connect():
        database.begin()

    @app.teardown_request
    def _db_close(_exc):
        database.close()

    app.register_blueprint(api_bp)
    try:
        yield SimpleNamespace(client=app.test_client(), refreshed=refreshed, emitted=emitted)
    finally:
        database.remove()


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_repository_create_update_and_delete_commit(arr_session, arr_engine, kind):
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(arr_session)
    first = repo.create(kind, "First", api_key="first-key", port=1)
    second = repo.create(kind, "Second", api_key="second-key", port=2)
    assert stored(arr_engine) == [(kind, "First", 1, 1), (kind, "Second", 1, 0)]

    repo.update(second.id, name="Promoted", is_default=True)
    assert stored(arr_engine) == [(kind, "First", 1, 0), (kind, "Promoted", 1, 1)]

    repo.update(second.id, enabled=False)
    assert stored(arr_engine) == [(kind, "First", 1, 1), (kind, "Promoted", 0, 0)]

    repo.update(second.id, enabled=True)
    repo.set_default(second.id)
    assert stored(arr_engine) == [(kind, "First", 1, 0), (kind, "Promoted", 1, 1)]

    assert repo.delete(second.id) is True
    assert stored(arr_engine) == [(kind, "First", 1, 1)]
    assert repo.get_decrypted_api_key(first.id) == "first-key"


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_api_create_update_and_delete(arr_api, arr_engine, kind):
    client = arr_api.client
    first = client.post(ROOT, json={"kind": kind, "name": "First", "api_key": "k",
                                    "ip": "127.0.0.1", "port": 9}, headers=HEADERS)
    assert first.status_code == 201, first.json
    assert first.json["is_default"] is True
    second = client.post(ROOT, json={"kind": kind, "name": "Second", "api_key": "k",
                                     "ip": "127.0.0.1", "port": 10}, headers=HEADERS)
    assert second.status_code == 201, second.json
    assert stored(arr_engine) == [(kind, "First", 1, 1), (kind, "Second", 1, 0)]

    path = f"{ROOT}/{second.json['id']}"
    updated = client.patch(path, json={"name": "Promoted", "is_default": True}, headers=HEADERS)
    assert updated.status_code == 200, updated.json
    assert updated.json["is_default"] is True
    assert stored(arr_engine) == [(kind, "First", 1, 0), (kind, "Promoted", 1, 1)]

    deleted = client.delete(path, headers=HEADERS)
    assert deleted.status_code == 204, deleted.json
    assert stored(arr_engine) == [(kind, "First", 1, 1)]
    listed = client.get(ROOT, headers=HEADERS)
    assert [(row["name"], row["is_default"]) for row in listed.json] == [("First", True)]
    assert [entry[0] for entry in arr_api.refreshed] == [kind] * 4


def _settings():
    def scalar(port):
        return SimpleNamespace(apikey="scalar-key", ip="127.0.0.1", port=port, base_url="/",
                               ssl=False, verify_ssl=False, http_timeout=60)
    return SimpleNamespace(general=SimpleNamespace(use_sonarr=True, use_radarr=True),
                           sonarr=scalar(8989), radarr=scalar(7878))


def test_startup_backfill_creates_the_default_instances(arr_session, arr_engine):
    from app.database import TableMovies, TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    arr_session.execute(sa.insert(TableShows).values(sonarrSeriesId=1, path="/tv/a", title="A"))
    arr_session.execute(sa.insert(TableMovies).values(radarrId=1, path="/m/a.mkv", title="A",
                                                      tmdbId="10"))

    # What startup does: backfill, then commit.
    result = backfill_default_instances(arr_session, _settings())
    arr_session.commit()

    assert result["sonarr"]["created"] is True and result["sonarr"]["stamped"] == 1
    assert result["radarr"]["created"] is True and result["radarr"]["stamped"] == 1
    assert stored(arr_engine) == [("sonarr", "Sonarr", 1, 1), ("radarr", "Radarr", 1, 1)]
    with arr_engine.connect() as connection:
        owners = connection.execute(sa.text(
            "SELECT (SELECT arr_instance_id FROM table_shows), "
            "(SELECT arr_instance_id FROM table_movies)")).one()
    repo = ArrInstanceRepository(arr_session)
    assert tuple(owners) == (repo.get_default("sonarr").id, repo.get_default("radarr").id)
    assert repo.get_decrypted_api_key(owners[0]) == "scalar-key"

    # A restart is a no-op.
    again = backfill_default_instances(arr_session, _settings())
    assert again["sonarr"]["created"] is False and again["radarr"]["created"] is False
    assert len(stored(arr_engine)) == 2


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_startup_backfill_adopts_rows_with_no_owner_while_the_kind_is_off(
        arr_session, arr_engine, kind):
    """The upgrade from a single-instance database: no row has an owner yet,
    and those rows still get an instance with Use Sonarr or Use Radarr off."""
    from app.database import TableHistory, TableHistoryMovie, TableMovies, TableShows
    from arr_instances.backfill import backfill_default_instances

    if kind == "sonarr":
        arr_session.execute(sa.insert(TableShows).values(sonarrSeriesId=1, path="/tv/a",
                                                         title="A"))
        arr_session.execute(sa.insert(TableHistory).values(
            sonarrSeriesId=1, sonarrEpisodeId=2, action=1, description="Downloaded",
            timestamp=sa.func.now()))
        tables = ("table_shows", "table_history")
    else:
        arr_session.execute(sa.insert(TableMovies).values(radarrId=1, path="/m/a.mkv",
                                                          title="A", tmdbId="10"))
        arr_session.execute(sa.insert(TableHistoryMovie).values(
            radarrId=1, action=1, description="Downloaded", timestamp=sa.func.now()))
        tables = ("table_movies", "table_history_movie")
    settings = _settings()
    settings.general.use_sonarr = settings.general.use_radarr = False

    result = backfill_default_instances(arr_session, settings)
    arr_session.commit()

    assert result[kind]["created"] is True and result[kind]["stamped"] == 2
    assert result[_other(kind)]["created"] is False
    assert stored(arr_engine) == [(kind, kind.capitalize(), 1, 1)]
    with arr_engine.connect() as connection:
        owners = {connection.execute(sa.text(f"SELECT arr_instance_id FROM {table}")).scalar_one()
                  for table in tables}
    assert owners == {result[kind]["instance_id"]}


def test_a_failed_create_rolls_back_the_demoted_default(arr_session, arr_engine):
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(arr_session)
    first = repo.create("sonarr", "First", api_key="k")

    # The demote of First runs before the insert that collides on stable_key.
    # Under AUTOCOMMIT only the enclosing transaction can undo it.
    with pytest.raises(IntegrityError):
        repo.create("sonarr", "Second", api_key="k", is_default=True,
                    stable_key=first.stable_key)
    assert driver_idle(arr_session), "a failed unit must not leave its transaction open"
    arr_session.rollback()
    assert stored(arr_engine) == [("sonarr", "First", 1, 1)]

    # The session and its connection are usable again, and still commit.
    repo.create("sonarr", "Second", api_key="k", is_default=True)
    assert stored(arr_engine) == [("sonarr", "First", 1, 0), ("sonarr", "Second", 1, 1)]


# ------------------------------------------------ deleting with the synced library
#
# A Sonarr or Radarr instance that has synced once owns rows, and a plain delete
# refuses it. ?remove_library=true removes the instance together with those
# rows in one transaction. Only database rows go: no file is touched.

KIND_JOBS = {"sonarr": ("sonarr.sync.series", "update_series_for_instance"),
             "radarr": ("radarr.sync.movies", "update_movies_for_instance")}


def _library_models():
    from app.database import (TableBlacklist, TableBlacklistMovie, TableEpisodes, TableHistory,
                              TableHistoryMovie, TableMovies, TableMoviesRootfolder,
                              TableReleaseTypeMismatch, TableShows, TableShowsRootfolder)
    return (TableShows, TableEpisodes, TableMovies, TableHistory, TableHistoryMovie,
            TableBlacklist, TableBlacklistMovie, TableShowsRootfolder, TableMoviesRootfolder,
            TableReleaseTypeMismatch)


def library_rows(engine):
    """Every library row, as a fresh connection sees it."""
    dump = {}
    with engine.connect() as connection:
        for model in _library_models():
            key = list(model.__table__.primary_key.columns)
            dump[model.__tablename__] = [
                tuple(row) for row in connection.execute(sa.select(model.__table__).order_by(*key))]
    return dump


def seed_library(engine, kind, name, n, *, default, enabled=True):
    """One instance after a sync: its media, history and exclusions, the root
    folder the sync recorded, and release-type mismatch flags.

    It includes the two history shapes an owner can have beyond a plain linked
    row: one with no media link that upgrades a linked one, and a row with no
    recorded owner that is linked to this owner's media.
    """
    from app.database import (TableArrInstances, TableBlacklist, TableBlacklistMovie,
                              TableEpisodes, TableHistory, TableHistoryMovie, TableMovies,
                              TableMoviesRootfolder, TableReleaseTypeMismatch, TableShows,
                              TableShowsRootfolder)

    def insert(connection, model, **values):
        key = list(model.__table__.primary_key.columns)[0]
        return connection.execute(sa.insert(model).values(**values).returning(key)).scalar_one()

    now = sa.func.now()
    with engine.connect() as c:
        owner = insert(c, TableArrInstances, kind=kind, name=name, stable_key=f"{kind}-{n}",
                       port=1000 + n, enabled=int(enabled), is_default=int(default))
        if kind == "sonarr":
            show = insert(c, TableShows, arr_instance_id=owner, sonarrSeriesId=n, tvdbId=n,
                          title=f"{name} show", path=f"/tv/{name}")
            episodes = [insert(c, TableEpisodes, arr_instance_id=owner, series_id=show,
                               sonarrSeriesId=n, sonarrEpisodeId=n * 10 + number, season=1,
                               episode=number, title=f"{name} {number}",
                               path=f"/tv/{name}/S01E0{number}.mkv") for number in (1, 2)]
            linked = insert(c, TableHistory, arr_instance_id=owner, series_id=show,
                            episode_id=episodes[0], sonarrSeriesId=n, sonarrEpisodeId=n * 10 + 1,
                            action=1, description="Downloaded", timestamp=now)
            insert(c, TableHistory, arr_instance_id=owner, series_id=None, episode_id=None,
                   sonarrSeriesId=n, sonarrEpisodeId=n * 10 + 1, action=3,
                   description="Upgraded", timestamp=now, upgradedFromId=linked)
            insert(c, TableHistory, arr_instance_id=None, series_id=show, episode_id=episodes[1],
                   sonarrSeriesId=n, sonarrEpisodeId=n * 10 + 2, action=3,
                   description="Upgraded", timestamp=now, upgradedFromId=linked)
            insert(c, TableBlacklist, arr_instance_id=owner, series_id=show, episode_id=episodes[0],
                   sonarr_series_id=n, sonarr_episode_id=n * 10 + 1, provider="p", subs_id="a")
            insert(c, TableBlacklist, arr_instance_id=owner, sonarr_series_id=n,
                   sonarr_episode_id=n * 10 + 9, provider="p", subs_id="b")
            insert(c, TableShowsRootfolder, arr_instance_id=owner, upstream_rootfolder_id=1, id=1,
                   path="/tv", accessible=1)
            media_type, media = "series", episodes
        else:
            movie = insert(c, TableMovies, arr_instance_id=owner, radarrId=n, tmdbId=str(n),
                           title=f"{name} movie", path=f"/movies/{name}.mkv")
            linked = insert(c, TableHistoryMovie, arr_instance_id=owner, movie_id=movie, radarrId=n,
                            action=1, description="Downloaded", timestamp=now)
            insert(c, TableHistoryMovie, arr_instance_id=owner, movie_id=None, radarrId=n,
                   action=3, description="Upgraded", timestamp=now, upgradedFromId=linked)
            insert(c, TableHistoryMovie, arr_instance_id=None, movie_id=movie, radarrId=n,
                   action=3, description="Upgraded", timestamp=now, upgradedFromId=linked)
            insert(c, TableBlacklistMovie, arr_instance_id=owner, movie_id=movie, radarr_id=n,
                   provider="p", subs_id="a")
            insert(c, TableBlacklistMovie, arr_instance_id=owner, radarr_id=n + 1000,
                   provider="p", subs_id="b")
            insert(c, TableMoviesRootfolder, arr_instance_id=owner, upstream_rootfolder_id=1, id=1,
                   path="/movies", accessible=1)
            media_type, media = "movie", [movie, movie]
        for language, media_id, flag_owner in (("en", media[0], owner), ("fr", media[1], 0),
                                               ("de", 900000 + n, owner)):
            insert(c, TableReleaseTypeMismatch, media_type=media_type, media_id=media_id,
                   arr_instance_id=flag_owner, language=language, video_release_type="web",
                   subtitle_release_type="bluray")
    return owner


def expected_library(kind):
    if kind == "sonarr":
        return {"series": 1, "episodes": 2, "movies": 0, "history": 3, "blacklist": 2,
                "root_folders": 1}
    return {"series": 0, "episodes": 0, "movies": 1, "history": 3, "blacklist": 2,
            "root_folders": 1}


@pytest.fixture
def settings_saves(monkeypatch):
    """Record writes through the settings save path and apply them to the live
    settings, without touching config.yaml or the scheduler."""
    from app import config as app_config

    saves = []

    def save_settings(items):
        items = [(key, list(value)) for key, value in items]
        saves.append(items)
        for key, value in items:
            section, field = key.split("-")[1:]
            setattr(getattr(app_config.settings, section), field, value[0] == "true")

    monkeypatch.setattr(app_config, "save_settings", save_settings)
    return saves


def _other(kind):
    return "radarr" if kind == "sonarr" else "sonarr"


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_synced_instance_is_refused_by_default_and_told_about_the_library_option(
        arr_api, arr_engine, settings_saves, kind):
    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)
    before = library_rows(arr_engine)

    refused = arr_api.client.delete(f"{ROOT}/{target}", headers=HEADERS)

    assert refused.status_code == 409, refused.json
    assert refused.json == {"error": "conflict",
                            "message": "cannot delete an instance that still owns rows",
                            "can_remove_library": True,
                            "library": expected_library(kind),
                            "last_of_kind": False}
    assert library_rows(arr_engine) == before
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1), (kind, "Target", 1, 0)]
    assert arr_api.refreshed == [] and arr_api.emitted == [] and settings_saves == []


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_remove_library_deletes_the_instance_with_exactly_its_rows(
        arr_api, arr_engine, settings_saves, kind):
    seed_library(arr_engine, kind, "Sibling", 2, default=False)
    seed_library(arr_engine, _other(kind), "Neighbour", 3, default=True)
    kept = library_rows(arr_engine)
    target = seed_library(arr_engine, kind, "Target", 1, default=True)
    assert library_rows(arr_engine) != kept

    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert removed.status_code == 204, removed.json
    # Everything the target owned or described is gone, and every other row is
    # exactly as it was, other owners' history and mismatch flags included.
    assert library_rows(arr_engine) == kept
    # The sibling takes over as the default, as it does for a plain delete.
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1), (_other(kind), "Neighbour", 1, 1)]
    assert arr_api.refreshed == [(kind, target, True)]
    event = "series" if kind == "sonarr" else "movie"
    assert arr_api.emitted == [{"type": event, "action": "delete"}, {"type": "badges"}]
    # Another instance of the kind remains, so the kind stays switched on.
    assert settings_saves == []


@pytest.mark.parametrize("last", [False, True])
@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_failed_library_removal_keeps_the_instance_and_every_row(
        arr_api, arr_engine, settings_saves, monkeypatch, kind, last):
    from arr_instances.repository import ArrInstanceRepository

    if last:
        # The widest removal: the whole kind, rows no instance owns included.
        _seed_unowned_rows(arr_engine, kind)
    else:
        seed_library(arr_engine, kind, "Sibling", 2, default=False)
    target = seed_library(arr_engine, kind, "Target", 1, default=True)
    before, owners = library_rows(arr_engine), stored(arr_engine)

    # The last step inside the transaction: every row is already deleted.
    reconcile = ArrInstanceRepository._reconcile_default

    def fail(self, kind, demoted_id=None):
        raise RuntimeError("injected")
    monkeypatch.setattr(ArrInstanceRepository, "_reconcile_default", fail)
    failed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert failed.status_code == 500
    assert library_rows(arr_engine) == before
    assert stored(arr_engine) == owners
    assert arr_api.refreshed == [] and settings_saves == []

    # The connection is usable again and the removal goes through on a retry.
    monkeypatch.setattr(ArrInstanceRepository, "_reconcile_default", reconcile)
    retried = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)
    assert retried.status_code == 204, retried.json
    assert stored(arr_engine) == ([] if last else [(kind, "Sibling", 1, 1)])


def _stray_history_row(engine, kind, owner):
    """A history row with no media link, as a download writes it when its
    media lookup finds nothing, naming ``owner``."""
    from app.database import TableHistory, TableHistoryMovie

    media = ({"sonarrSeriesId": 5, "sonarrEpisodeId": 50} if kind == "sonarr"
             else {"radarrId": 5})
    model = TableHistory if kind == "sonarr" else TableHistoryMovie
    with engine.connect() as c:
        c.execute(sa.insert(model).values(arr_instance_id=owner, action=1,
                                          description="Downloaded", timestamp=sa.func.now(),
                                          **media))


@pytest.mark.parametrize("remove_library", [True, False])
@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_deleting_the_last_instance_switches_its_kind_off_so_startup_does_not_recreate_it(
        arr_api, arr_engine, arr_session, settings_saves, monkeypatch, kind, remove_library):
    from app import config as app_config
    from arr_instances.backfill import backfill_default_instances

    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    monkeypatch.setattr(app_config.settings.general, f"use_{_other(kind)}", False)
    if remove_library:
        target = seed_library(arr_engine, kind, "Only", 1, default=True)
        refused = arr_api.client.delete(f"{ROOT}/{target}", headers=HEADERS)
        assert refused.status_code == 409
        assert refused.json["last_of_kind"] is True
        path = f"{ROOT}/{target}?remove_library=true"
    else:
        # An instance that never synced: the plain delete still works.
        created = arr_api.client.post(ROOT, json={"kind": kind, "name": "Only", "api_key": "k"},
                                      headers=HEADERS)
        assert created.status_code == 201, created.json
        target = created.json["id"]
        path = f"{ROOT}/{target}"
    assert settings_saves == []

    deleted = arr_api.client.delete(path, headers=HEADERS)

    assert deleted.status_code == 204, deleted.json
    assert stored(arr_engine) == []
    # Through the settings save path, as the Settings page writes it.
    assert settings_saves == [[(f"settings-general-use_{kind}", ["false"])]]
    assert getattr(app_config.settings.general, f"use_{kind}") is False
    assert {"type": "settings"} in arr_api.emitted
    # A download that was already past its instance lookup lands afterwards
    # and records its history under the instance that is now gone.
    _stray_history_row(arr_engine, kind, target)
    # The next startup finds nothing to own and nothing switched on.
    summary = backfill_default_instances(arr_session, app_config.settings)
    arr_session.commit()
    assert summary[kind]["created"] is False
    assert stored(arr_engine) == []


def _owned_tables(kind):
    """The tables the startup backfill searches for rows with no owner."""
    from app.database import (TableBlacklist, TableBlacklistMovie, TableEpisodes, TableHistory,
                              TableHistoryMovie, TableMovies, TableMoviesRootfolder, TableShows,
                              TableShowsRootfolder)
    if kind == "sonarr":
        return (TableShows, TableEpisodes, TableHistory, TableBlacklist, TableShowsRootfolder)
    return (TableMovies, TableHistoryMovie, TableBlacklistMovie, TableMoviesRootfolder)


def ownerless_rows(engine, kind):
    with engine.connect() as connection:
        return sum(connection.execute(
            sa.select(sa.func.count()).select_from(model)
            .where(model.arr_instance_id.is_(None))).scalar_one()
            for model in _owned_tables(kind))


def _remove_the_only_instance(arr_api, arr_engine, monkeypatch, kind):
    """Sync one instance of ``kind``, then delete it with its library, which
    also switches the kind off. Returns the id it had."""
    from app import config as app_config

    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    monkeypatch.setattr(app_config.settings.general, f"use_{_other(kind)}", False)
    target = seed_library(arr_engine, kind, "Only", 1, default=True)
    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)
    assert removed.status_code == 204, removed.json
    assert stored(arr_engine) == []
    assert getattr(app_config.settings.general, f"use_{kind}") is False
    return target


@pytest.fixture
def exclusions(arr_engine, monkeypatch):
    """The Sonarr and Radarr exclusion writers on the test database."""
    from radarr import blacklist as movie_blacklist
    from sonarr import blacklist as episode_blacklist

    db = scoped_session(sessionmaker(bind=arr_engine, autoflush=False))
    for module in (episode_blacklist, movie_blacklist):
        monkeypatch.setattr(module, "database", db)
        monkeypatch.setattr(module, "event_stream", lambda **_: None)

    def exclude(kind, n, **owner):
        """Exclude a release of series ``n``, episode ``n * 10 + 1``, or of movie ``n``."""
        if kind == "sonarr":
            episode_blacklist.blacklist_log(sonarr_series_id=n, sonarr_episode_id=n * 10 + 1,
                                            provider="p", subs_id="late", language="en",
                                            **owner)
        else:
            movie_blacklist.blacklist_log_movie(radarr_id=n, provider="p", subs_id="late",
                                                language="en", **owner)

    try:
        yield exclude
    finally:
        db.remove()


def recorded_exclusion(engine, kind):
    """The owner and media links of the release ``exclusions`` recorded."""
    from app.database import TableBlacklist, TableBlacklistMovie

    if kind == "sonarr":
        stmt = sa.select(TableBlacklist.arr_instance_id, TableBlacklist.series_id,
                         TableBlacklist.episode_id).where(TableBlacklist.subs_id == "late")
    else:
        stmt = sa.select(TableBlacklistMovie.arr_instance_id, TableBlacklistMovie.movie_id) \
            .where(TableBlacklistMovie.subs_id == "late")
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(stmt)]


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_an_exclusion_that_lands_after_the_last_instance_goes_keeps_its_owner(
        arr_api, arr_engine, arr_session, settings_saves, exclusions, monkeypatch, kind):
    """The Exclude action finds the media under the instance it was asked for,
    then records the release. When the library removal commits in between,
    the media row is gone, and the entry used to be stored with no owner at
    all, which the next start rebuilt the deleted instance for."""
    from app import config as app_config
    from arr_instances.backfill import backfill_default_instances

    target = _remove_the_only_instance(arr_api, arr_engine, monkeypatch, kind)

    exclusions(kind, 1, arr_instance_id=target)

    links = (None, None) if kind == "sonarr" else (None,)
    assert recorded_exclusion(arr_engine, kind) == [(target, *links)]
    assert ownerless_rows(arr_engine, kind) == 0
    summary = backfill_default_instances(arr_session, app_config.settings)
    arr_session.commit()
    assert summary[kind]["created"] is False
    assert stored(arr_engine) == []


def _media_links(engine, kind, n):
    """The local ids an exclusion for item ``n`` of ``seed_library`` links to."""
    from app.database import TableEpisodes, TableMovies, TableShows

    with engine.connect() as connection:
        if kind == "sonarr":
            show = connection.execute(sa.select(TableShows.id)
                                      .where(TableShows.sonarrSeriesId == n)).scalar_one()
            episode = connection.execute(sa.select(TableEpisodes.id).where(
                TableEpisodes.sonarrEpisodeId == n * 10 + 1)).scalar_one()
            return (show, episode)
        return (connection.execute(sa.select(TableMovies.id)
                                   .where(TableMovies.radarrId == n)).scalar_one(),)


@pytest.mark.parametrize("named", [True, False])
@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_an_exclusion_for_a_known_item_takes_its_owner_and_links_from_it(
        arr_engine, exclusions, kind, named):
    """Unchanged when the media row is there, whether or not the caller names
    the instance."""
    owner = seed_library(arr_engine, kind, "Live", 1, default=True)
    seed_library(arr_engine, kind, "Other", 2, default=False)

    exclusions(kind, 1, **({"arr_instance_id": owner} if named else {}))

    assert recorded_exclusion(arr_engine, kind) == [(owner, *_media_links(arr_engine, kind, 1))]


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_an_exclusion_nobody_can_attribute_is_still_recorded(arr_engine, exclusions, kind):
    """A provider can reject a release for media the search could not tie to
    a library row. The entry has no owner, and it still has to be kept: it is
    what stops the release being downloaded again."""
    seed_library(arr_engine, kind, "Live", 1, default=True)

    exclusions(kind, 7)

    links = (None, None) if kind == "sonarr" else (None,)
    assert recorded_exclusion(arr_engine, kind) == [(None, *links)]


# The single-item syncs that name no instance: the webhook URLs without an
# instance key, the manual sync of one series, and the live feed of a series
# whose episodes changed, as it runs while no instance is enabled.
WRITER_KIND = {"movie_webhook": "radarr", "episode_webhook": "sonarr",
               "series_sync": "sonarr", "series_feed": "sonarr"}
WRITER_ITEM = {"movie_webhook": "movie 5", "episode_webhook": "episode 50",
               "series_sync": "series 5", "series_feed": "the episodes of series 5"}


def skipped_syncs(caplog):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("BAZARR skipping the")]


@pytest.fixture
def unscoped_syncs(arr_engine, monkeypatch):
    """Those writers on the test database, with Sonarr and Radarr answering
    from fixed payloads: series 5 with its episode 50 on file 55, and movie 5
    on file 55. Records the subtitle searches the webhooks start."""
    import semver

    from api.webhooks import radarr as radarr_hook
    from api.webhooks import sonarr as sonarr_hook
    from app import signalr_client
    from arr_instances import resolution
    from radarr.sync import movies
    from sonarr.sync import episodes, series

    db = scoped_session(sessionmaker(bind=arr_engine, autoflush=False))
    for module in (radarr_hook, sonarr_hook, signalr_client, movies, series, episodes):
        monkeypatch.setattr(module, "database", db)
    for module in (signalr_client, movies, series, episodes):
        monkeypatch.setattr(module, "event_stream", lambda **_: None)
    resolution.clear_media_defaults_cache()

    for module in (movies, series):
        monkeypatch.setattr(module, "get_profile_list", lambda **_: [])
        monkeypatch.setattr(module, "get_tags", lambda **_: {})
        monkeypatch.setattr(module, "get_language_profiles", lambda: [])
    monkeypatch.setattr(movies, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr(movies, "get_movies_from_radarr_api", lambda **_: {"id": 5})
    monkeypatch.setattr(movies, "movieParser", lambda data, **_: {
        "radarrId": 5, "tmdbId": "5", "title": "Late movie", "year": 2020,
        "path": "/movies/late.mkv", "movie_file_id": 55})

    monkeypatch.setattr(series, "get_series_from_sonarr_api", lambda **_: [{"id": 5}])
    monkeypatch.setattr(series, "seriesParser", lambda data, **_: {
        "sonarrSeriesId": 5, "tvdbId": 5, "title": "Late show", "path": "/tv/Late"})
    monkeypatch.setattr(series, "sync_episodes", lambda **_: None)

    episode = {"id": 50, "seriesId": 5, "hasFile": True, "monitored": True,
               "episodeFileId": 55,
               "episodeFile": {"size": 10 ** 9, "path": "/tv/Late/S01E05.mkv"}}
    monkeypatch.setattr(episodes, "get_episodes_from_sonarr_api",
                        lambda episode_id=None, **_: dict(episode) if episode_id else [dict(episode)])
    monkeypatch.setattr(episodes, "episodeParser", lambda data, **_: {
        "sonarrSeriesId": 5, "sonarrEpisodeId": 50, "title": "Late episode",
        "path": "/tv/Late/S01E05.mkv", "season": 1, "episode": 5, "episode_file_id": 55,
        "monitored": "True"})
    monkeypatch.setattr(episodes, "get_sonarr_info",
                        SimpleNamespace(semver=lambda: semver.Version(4, 0, 10, 0)))
    monkeypatch.setattr(episodes, "store_subtitles", lambda *a, **k: None)
    monkeypatch.setattr(episodes, "send_notifications", lambda *a, **k: None)

    searches = []
    monkeypatch.setattr(radarr_hook, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr(radarr_hook, "movies_download_subtitles",
                        lambda no, arr_instance_id=None: searches.append(("movie", no)))
    monkeypatch.setattr(sonarr_hook, "store_subtitles", lambda *a, **k: None)
    monkeypatch.setattr(sonarr_hook, "episode_download_subtitles",
                        lambda no, arr_instance_id=None: searches.append(("episode", no)))

    def run(writer, client):
        """Trigger ``writer``; the webhooks return their response."""
        if writer == "movie_webhook":
            return client.post("/api/webhooks/radarr", headers=HEADERS, json={
                "eventType": "Download", "movie": {"id": 5}, "movieFile": {"id": 55}})
        if writer == "episode_webhook":
            return client.post("/api/webhooks/sonarr", headers=HEADERS, json={
                "eventType": "Download", "episodes": [{"id": 50}], "episodeFiles": [{"id": 55}]})
        if writer == "series_sync":
            series.update_one_series(5, "updated")
        else:
            signalr_client.dispatcher({"name": "series", "body": {"action": "updated", "resource": {
                "id": 5, "title": "Late show", "year": 2020, "episodesChanged": True}}})
        return None

    try:
        yield SimpleNamespace(run=run, searches=searches)
    finally:
        db.remove()
        resolution.clear_media_defaults_cache()


def late_rows(engine, writer):
    """The owner (and for an episode, the series link) of what ``writer`` wrote."""
    from app.database import TableEpisodes, TableMovies, TableShows

    if writer == "movie_webhook":
        stmt = sa.select(TableMovies.arr_instance_id).where(TableMovies.radarrId == 5)
    elif writer == "series_sync":
        stmt = sa.select(TableShows.arr_instance_id).where(TableShows.sonarrSeriesId == 5)
    else:
        stmt = sa.select(TableEpisodes.arr_instance_id, TableEpisodes.series_id) \
            .where(TableEpisodes.sonarrEpisodeId == 50)
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(stmt)]


def _known_late_show(engine, owner):
    """Series 5 as an earlier sync of ``owner`` stored it."""
    from app.database import TableShows

    with engine.connect() as connection:
        return connection.execute(sa.insert(TableShows).values(
            arr_instance_id=owner, sonarrSeriesId=5, tvdbId=5, title="Late show",
            path="/tv/Late").returning(TableShows.id)).scalar_one()


@pytest.mark.parametrize("writer", list(WRITER_KIND))
def test_a_sync_naming_no_instance_after_the_last_one_goes_writes_nothing(
        arr_api, arr_engine, arr_session, settings_saves, unscoped_syncs, monkeypatch, caplog,
        writer):
    """They sync under the default instance. After the last instance of the
    kind is deleted there is none, so the row they wrote had no owner, and
    the next start rebuilt the deleted instance for it from the stored
    connection settings, which still answer."""
    from app import config as app_config
    from arr_instances.backfill import backfill_default_instances

    kind = WRITER_KIND[writer]
    _remove_the_only_instance(arr_api, arr_engine, monkeypatch, kind)
    before = library_rows(arr_engine)

    with caplog.at_level(logging.INFO):
        response = unscoped_syncs.run(writer, arr_api.client)

    if response is not None:
        assert response.status_code == 200, response.json
    assert skipped_syncs(caplog) == [
        f"BAZARR skipping the {kind.capitalize()} sync of {WRITER_ITEM[writer]}: "
        f"there is no {kind.capitalize()} instance to own it"]
    assert library_rows(arr_engine) == before
    assert ownerless_rows(arr_engine, kind) == 0
    assert unscoped_syncs.searches == []
    summary = backfill_default_instances(arr_session, app_config.settings)
    arr_session.commit()
    assert summary[kind]["created"] is False
    assert stored(arr_engine) == []


@pytest.mark.parametrize("writer", list(WRITER_KIND))
def test_a_sync_naming_no_instance_still_writes_under_the_default_one(
        arr_api, arr_engine, unscoped_syncs, monkeypatch, caplog, writer):
    """Unchanged while the kind has its instance: the item is stored under the
    default one and the webhook goes on to search for its subtitles."""
    from app import config as app_config

    kind = WRITER_KIND[writer]
    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    owner = seed_library(arr_engine, kind, "Live", 1, default=True)
    seed_library(arr_engine, kind, "Other", 2, default=False)
    link = ()
    if writer in ("episode_webhook", "series_feed"):
        link = (_known_late_show(arr_engine, owner),)

    with caplog.at_level(logging.INFO):
        response = unscoped_syncs.run(writer, arr_api.client)

    assert skipped_syncs(caplog) == []
    assert late_rows(arr_engine, writer) == [(owner, *link)]
    searched = {"movie_webhook": [("movie", 5)], "episode_webhook": [("episode", 50)]}
    assert unscoped_syncs.searches == searched.get(writer, [])
    if response is not None:
        assert response.status_code == 200, response.json
        assert response.json == "Finished processing subtitles."


def _instance_row(engine, kind, *, enabled):
    from app.database import TableArrInstances

    with engine.connect() as connection:
        return connection.execute(sa.insert(TableArrInstances).values(
            kind=kind, name="Main", stable_key=f"{kind}-main", port=1,
            enabled=int(enabled), is_default=int(enabled)).returning(TableArrInstances.id)
        ).scalar_one()


@pytest.mark.parametrize("instance, use, outcome", [
    # No instance at all: nothing may own the row, whatever the switch says.
    (None, True, "nothing"),
    (None, False, "nothing"),
    # A default instance owns it, even with the kind switched off.
    ("enabled", False, "default"),
    # Only a disabled instance, so no default: not written, whatever the
    # switch says. The stored connection settings need not describe that
    # instance, yet the next start would stamp a row with no owner onto it.
    ("disabled", True, "nothing"),
    ("disabled", False, "nothing"),
])
@pytest.mark.parametrize("writer", ["movie_webhook", "series_sync"])
def test_what_a_sync_naming_no_instance_writes(
        arr_api, arr_engine, unscoped_syncs, monkeypatch, writer, instance, use, outcome):
    from app import config as app_config

    kind = WRITER_KIND[writer]
    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", use)
    owner = None if instance is None else _instance_row(arr_engine, kind,
                                                         enabled=instance == "enabled")

    unscoped_syncs.run(writer, arr_api.client)

    expected = {"nothing": [], "default": [(owner,)]}[outcome]
    assert late_rows(arr_engine, writer) == expected


@pytest.mark.parametrize("writer", list(WRITER_KIND))
def test_a_sync_naming_no_instance_writes_nothing_once_only_a_disabled_one_is_left(
        arr_api, arr_engine, settings_saves, unscoped_syncs, monkeypatch, caplog, writer):
    """The enabled default goes and a disabled sibling stays, so the kind
    stays on with no default. The stored connection settings still describe
    the deleted server, and the next start stamps a row with no owner onto the
    only instance left, which is another server."""
    from app import config as app_config

    kind = WRITER_KIND[writer]
    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    gone = seed_library(arr_engine, kind, "Gone", 1, default=True)
    seed_library(arr_engine, kind, "Sibling", 2, default=False, enabled=False)
    removed = arr_api.client.delete(f"{ROOT}/{gone}?remove_library=true", headers=HEADERS)
    assert removed.status_code == 204, removed.json
    assert getattr(app_config.settings.general, f"use_{kind}") is True
    before = library_rows(arr_engine)

    with caplog.at_level(logging.INFO):
        response = unscoped_syncs.run(writer, arr_api.client)

    if response is not None:
        assert response.status_code == 200, response.json
    assert skipped_syncs(caplog) == [
        f"BAZARR skipping the {kind.capitalize()} sync of {WRITER_ITEM[writer]}: "
        f"no {kind.capitalize()} instance is enabled"]
    assert library_rows(arr_engine) == before
    assert unscoped_syncs.searches == []


@pytest.mark.parametrize("state", ["pending", "running"])
@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_library_removal_is_refused_while_that_instance_syncs(
        arr_api, arr_engine, settings_saves, monkeypatch, kind, state):
    from collections import deque

    from app.jobs_queue import Job, jobs_queue

    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)
    before = library_rows(arr_engine)
    module, func = KIND_JOBS[kind]
    job = Job(job_id=900001, job_name="Syncing", module=module, func=func,
              kwargs={"arr_instance_id": target, "job_id": None, "wait_for_completion": True})
    job.status = state
    monkeypatch.setattr(jobs_queue, f"jobs_{state}_queue", deque([job]))

    refused = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert refused.status_code == 409, refused.json
    assert refused.json["error"] == "sync_in_progress"
    assert "sync" in refused.json["message"]
    assert library_rows(arr_engine) == before
    assert len(stored(arr_engine)) == 2

    # A sync of another instance does not hold this one up.
    job.kwargs["arr_instance_id"] = target + 1000
    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)
    assert removed.status_code == 204, removed.json
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1)]


@pytest.mark.parametrize("state", ["pending", "running"])
@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_plain_delete_is_refused_while_that_instance_syncs(
        arr_api, arr_engine, settings_saves, monkeypatch, kind, state):
    """A first sync that has built its client and not written a row yet: the
    instance owns nothing, so without this the delete went through and the
    sync then wrote its whole library under an instance that is gone."""
    from collections import deque

    from app.jobs_queue import Job, jobs_queue

    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    created = arr_api.client.post(ROOT, json={"kind": kind, "name": "New", "api_key": "k",
                                              "port": 3}, headers=HEADERS)
    assert created.status_code == 201, created.json
    target = created.json["id"]
    before = library_rows(arr_engine)
    module, func = KIND_JOBS[kind]
    job = Job(job_id=900004, job_name="Syncing", module=module, func=func,
              kwargs={"arr_instance_id": target, "job_id": None, "wait_for_completion": True})
    job.status = state
    monkeypatch.setattr(jobs_queue, f"jobs_{state}_queue", deque([job]))

    refused = arr_api.client.delete(f"{ROOT}/{target}", headers=HEADERS)

    assert refused.status_code == 409, refused.json
    assert refused.json["error"] == "sync_in_progress"
    assert library_rows(arr_engine) == before
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1), (kind, "New", 1, 0)]

    monkeypatch.setattr(jobs_queue, f"jobs_{state}_queue", deque())
    deleted = arr_api.client.delete(f"{ROOT}/{target}", headers=HEADERS)
    assert deleted.status_code == 204, deleted.json
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1)]


@pytest.mark.parametrize("remove_library", [True, False])
@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_the_kind_is_off_before_the_feed_and_jobs_are_refreshed(
        arr_api, arr_engine, settings_saves, monkeypatch, kind, remove_library):
    """The refresh restarts the live feed. With the kind still on and no
    instance left it started the fallback feed on the stored connection
    settings, which still describe the server just deleted."""
    from app import config as app_config
    from arr_instances import service

    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    refreshed = []
    monkeypatch.setattr(service, "refresh_runtime",
                        lambda refreshed_kind, instance_id=None, removed=False: refreshed.append(
                            (refreshed_kind, instance_id, removed,
                             getattr(app_config.settings.general, f"use_{kind}"))))
    if remove_library:
        target = seed_library(arr_engine, kind, "Only", 1, default=True)
        path = f"{ROOT}/{target}?remove_library=true"
    else:
        target = _instance_row(arr_engine, kind, enabled=True)
        path = f"{ROOT}/{target}"

    deleted = arr_api.client.delete(path, headers=HEADERS)

    assert deleted.status_code == 204, deleted.json
    assert refreshed == [(kind, target, True, False)]


def test_removing_the_old_sonarr_library_ends_the_sportarr_ownership_ambiguity(
        arr_api, arr_engine, settings_saves, tmp_path):
    """The migration from a Sonarr-kind Sportarr connection: the same file was
    synced by the old, now disabled, Sonarr instance and by the new Sportarr
    one, and Sportarr refused to write its subtitle until the old rows went."""
    from sqlalchemy.orm import Session

    from app.database import (TableArrInstances, TableEpisodes, TableShows, TableSportsEvents,
                              TableSportsLeagues)
    from sportarr.output import SportsOutputNamespace

    folder = tmp_path / "sports" / "Formula 1"
    folder.mkdir(parents=True)
    video = str(folder / "Monaco.mkv")
    with arr_engine.connect() as c:
        def insert(model, **values):
            return c.execute(sa.insert(model).values(**values).returning(model.id)).scalar_one()
        old = insert(TableArrInstances, kind="sonarr", name="Sportarr as Sonarr",
                     stable_key="sonarr", port=1867, enabled=0, is_default=0)
        sports = insert(TableArrInstances, kind="sportarr", name="Sportarr",
                        stable_key="sportarr", port=1867, enabled=1, is_default=1)
        show = insert(TableShows, arr_instance_id=old, sonarrSeriesId=11, title="Formula 1",
                      path=str(folder), tvdbId=11)
        insert(TableEpisodes, arr_instance_id=old, series_id=show, sonarrSeriesId=11,
               sonarrEpisodeId=21, title="Monaco GP", path=video, season=2026, episode=8)
        league = insert(TableSportsLeagues, arr_instance_id=sports, sportarrLeagueId=3,
                        title="Formula 1")
        event = insert(TableSportsEvents, arr_instance_id=sports, league_id=league,
                       sportarrEventId=4, file_id=4, title="Monaco GP", path=video)
    context = SimpleNamespace(mapped_path=video, event_id=event, arr_instance_id=sports)
    session = Session(bind=arr_engine)
    try:
        with pytest.raises(ValueError, match="ambiguous between media owners"):
            SportsOutputNamespace(context, session)
        session.rollback()

        removed = arr_api.client.delete(f"{ROOT}/{old}?remove_library=true", headers=HEADERS)
        assert removed.status_code == 204, removed.json

        SportsOutputNamespace(context, session).validate(session)
    finally:
        session.close()
    assert stored(arr_engine) == [("sportarr", "Sportarr", 1, 1)]


def _load_link_index_migration():
    import importlib.util
    import pathlib

    path = (pathlib.Path(__file__).resolve().parents[2] / "migrations" / "versions"
            / "f8c3d1a7b926_library_link_indexes.py")
    spec = importlib.util.spec_from_file_location("library_link_indexes_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_existing_database_gets_the_library_link_indexes(arr_engine):
    """Without them every row a library removal deletes costs a full read of
    the tables pointing at it, which on a real library ran for minutes."""
    migration = _load_link_index_migration()
    names = sorted(name for name, _table, _column in migration.INDEXES)

    def present():
        inspector = sa.inspect(arr_engine)
        return sorted(index["name"] for table in {t for _n, t, _c in migration.INDEXES}
                      for index in inspector.get_indexes(table) if index["name"] in names)

    # The database as it was before this revision.
    with arr_engine.connect() as connection:
        migration.drop_library_link_indexes(connection)
    assert present() == []

    with arr_engine.connect() as connection:
        assert sorted(migration.create_library_link_indexes(connection)) == names
    assert present() == names
    # Run again, as an interrupted upgrade would be: nothing to do.
    with arr_engine.connect() as connection:
        assert migration.create_library_link_indexes(connection) == []


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_no_sync_of_the_instance_can_be_queued_while_its_library_goes(
        arr_api, arr_engine, settings_saves, monkeypatch, kind):
    """The check for a running sync, the removal and its commit are one step
    for the job queue. A scheduler tick or a Sync press in the middle waits
    until the instance is gone, and its sync then finds no instance to run
    for, instead of building a client for it and writing rows that name it."""
    import threading
    from collections import deque

    from app import jobs_queue as jobs_module
    from app.jobs_queue import jobs_queue
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    monkeypatch.setattr(jobs_module, "event_stream", lambda **_event: None)
    monkeypatch.setattr(jobs_queue, "jobs_pending_queue", deque())
    monkeypatch.setattr(jobs_queue, "jobs_running_queue", deque())
    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)
    module, func = KIND_JOBS[kind]
    seen = {}

    def queue_is_free():
        # From another thread: the lock is re-entrant for the one holding it.
        outcome = []

        def probe():
            acquired = jobs_queue._queue_lock.acquire(blocking=False)
            if acquired:
                jobs_queue._queue_lock.release()
            outcome.append(acquired)

        prober = threading.Thread(target=probe)
        prober.start()
        prober.join()
        return outcome[0]

    check = service._library_sync_active

    def checking(row):
        seen["free_during_check"] = queue_is_free()
        return check(row)

    remove = ArrInstanceRepository._remove_library

    def racing(self, *args, **kwargs):
        feeder = threading.Thread(target=lambda: seen.setdefault(
            "queued", jobs_queue.feed_jobs_pending_queue(
                "Syncing", module, func, kwargs={"arr_instance_id": target})))
        feeder.start()
        feeder.join(0.3)
        seen["feeder_waited"] = feeder.is_alive()
        seen["feeder"] = feeder
        return remove(self, *args, **kwargs)

    monkeypatch.setattr(service, "_library_sync_active", checking)
    monkeypatch.setattr(ArrInstanceRepository, "_remove_library", racing)

    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert removed.status_code == 204, removed.json
    seen["feeder"].join(5)
    assert seen["free_during_check"] is False
    assert seen["feeder_waited"] is True
    # It went in once the instance was gone.
    assert seen["queued"]
    assert [job.kwargs for job in jobs_queue.jobs_pending_queue] == [{"arr_instance_id": target}]
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1)]
    assert queue_is_free()


def _seed_unowned_rows(engine, kind):
    """Rows of a kind that no existing instance owns: one with no owner that
    points at nothing, and a set that names an instance which is gone."""
    from app.database import (TableBlacklist, TableBlacklistMovie, TableHistory,
                              TableHistoryMovie, TableMovies, TableMoviesRootfolder,
                              TableReleaseTypeMismatch, TableShows, TableShowsRootfolder)

    gone = 9999
    with engine.connect() as c:
        if kind == "sonarr":
            c.execute(sa.insert(TableHistory).values(
                arr_instance_id=None, sonarrSeriesId=77, sonarrEpisodeId=770, action=1,
                description="Downloaded", timestamp=sa.func.now()))
            c.execute(sa.insert(TableBlacklist).values(
                arr_instance_id=None, sonarr_series_id=77, sonarr_episode_id=770,
                provider="p", subs_id="z"))
            c.execute(sa.insert(TableShows).values(
                arr_instance_id=gone, sonarrSeriesId=78, title="Gone show", path="/tv/gone"))
            c.execute(sa.insert(TableShowsRootfolder).values(
                arr_instance_id=gone, upstream_rootfolder_id=5, id=5, path="/gone"))
            media_type = "series"
        else:
            c.execute(sa.insert(TableHistoryMovie).values(
                arr_instance_id=None, radarrId=77, action=1, description="Downloaded",
                timestamp=sa.func.now()))
            c.execute(sa.insert(TableBlacklistMovie).values(
                arr_instance_id=None, radarr_id=77, provider="p", subs_id="z"))
            c.execute(sa.insert(TableMovies).values(
                arr_instance_id=gone, radarrId=78, tmdbId="78", title="Gone movie",
                path="/movies/gone.mkv"))
            c.execute(sa.insert(TableMoviesRootfolder).values(
                arr_instance_id=gone, upstream_rootfolder_id=5, id=5, path="/gone"))
            media_type = "movie"
        c.execute(sa.insert(TableReleaseTypeMismatch).values(
            media_type=media_type, media_id=424242, arr_instance_id=gone, language="en",
            video_release_type="web", subtitle_release_type="bluray"))


def _unowned_counts(kind):
    media = {"series": 1} if kind == "sonarr" else {"movies": 1}
    return {"series": 0, "episodes": 0, "movies": 0, "history": 1, "blacklist": 1,
            "root_folders": 1, **media}


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_the_last_instance_takes_the_rows_of_its_kind_no_instance_owns(
        arr_api, arr_engine, arr_session, settings_saves, monkeypatch, kind):
    """The startup backfill hands a kind's unowned rows to its only instance,
    and builds an instance for them from the stored connection settings when
    the kind has none left. Left behind, they brought the deleted instance
    back at the next start, with Use switched off."""
    from app import config as app_config
    from arr_instances.backfill import backfill_default_instances

    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    monkeypatch.setattr(app_config.settings.general, f"use_{_other(kind)}", True)
    seed_library(arr_engine, _other(kind), "Neighbour", 3, default=True)
    kept = library_rows(arr_engine)
    target = seed_library(arr_engine, kind, "Only", 1, default=True)
    _seed_unowned_rows(arr_engine, kind)

    refused = arr_api.client.delete(f"{ROOT}/{target}", headers=HEADERS)
    assert refused.status_code == 409, refused.json
    assert refused.json["last_of_kind"] is True
    own, unowned = expected_library(kind), _unowned_counts(kind)
    assert refused.json["library"] == {key: own[key] + unowned[key] for key in own}

    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert removed.status_code == 204, removed.json
    # Nothing of the kind is left, and the other kind is exactly as it was.
    assert library_rows(arr_engine) == kept
    assert settings_saves == [[(f"settings-general-use_{kind}", ["false"])]]
    summary = backfill_default_instances(arr_session, app_config.settings)
    arr_session.commit()
    assert summary[kind]["created"] is False
    assert stored(arr_engine) == [(_other(kind), "Neighbour", 1, 1)]


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_plain_delete_of_the_last_instance_answers_for_the_unowned_rows(
        arr_api, arr_engine, arr_session, settings_saves, monkeypatch, kind):
    """It owns nothing by id, but it is the instance the backfill gives those
    rows to, so deleting it alone would leave them to rebuild it. It is
    refused like one that owns rows, and offered the library removal."""
    from app import config as app_config
    from arr_instances.backfill import backfill_default_instances

    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    created = arr_api.client.post(ROOT, json={"kind": kind, "name": "Only", "api_key": "k"},
                                  headers=HEADERS)
    assert created.status_code == 201, created.json
    _seed_unowned_rows(arr_engine, kind)
    path = f"{ROOT}/{created.json['id']}"

    refused = arr_api.client.delete(path, headers=HEADERS)

    assert refused.status_code == 409, refused.json
    assert refused.json["can_remove_library"] is True
    assert refused.json["last_of_kind"] is True
    assert refused.json["library"] == _unowned_counts(kind)
    assert len(stored(arr_engine)) == 1 and settings_saves == []

    assert arr_api.client.delete(f"{path}?remove_library=true", headers=HEADERS).status_code == 204
    assert all(rows == [] for rows in library_rows(arr_engine).values())
    summary = backfill_default_instances(arr_session, app_config.settings)
    arr_session.commit()
    assert summary[kind]["created"] is False
    assert stored(arr_engine) == []


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_an_instance_with_a_sibling_leaves_the_unowned_rows_alone(
        arr_api, arr_engine, settings_saves, kind):
    """Only the last instance answers for them: with another one left they
    may still be that one's, and the backfill hands them to nobody."""
    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    _seed_unowned_rows(arr_engine, kind)
    kept = library_rows(arr_engine)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)

    refused = arr_api.client.delete(f"{ROOT}/{target}", headers=HEADERS)
    assert refused.json["library"] == expected_library(kind)
    assert arr_api.client.delete(f"{ROOT}/{target}?remove_library=true",
                                 headers=HEADERS).status_code == 204

    assert library_rows(arr_engine) == kept


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_history_row_left_behind_stops_pointing_at_a_removed_upgrade(
        arr_api, arr_engine, settings_saves, kind):
    """A row outside the library can still name one inside it as the entry it
    upgraded: a legacy row with no owner and no media link. The upgrade link
    has no delete rule, so it used to fail the whole removal. The row stays,
    and only its link to the removed entry goes."""
    from app.database import TableHistory, TableHistoryMovie

    model = TableHistory if kind == "sonarr" else TableHistoryMovie
    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    kept = library_rows(arr_engine)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)
    media = {"sonarrSeriesId": 1, "sonarrEpisodeId": 11} if kind == "sonarr" else {"radarrId": 1}
    with arr_engine.connect() as c:
        linked = c.execute(sa.select(model.id).where(
            model.arr_instance_id == target, model.action == 1)).scalar_one()
        legacy = c.execute(sa.insert(model).values(
            arr_instance_id=None, action=3, description="Upgraded", timestamp=sa.func.now(),
            upgradedFromId=linked, **media).returning(model.id)).scalar_one()

    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert removed.status_code == 204, removed.json
    with arr_engine.connect() as c:
        assert c.execute(sa.select(model.upgradedFromId).where(model.id == legacy)).one() == (None,)
        c.execute(sa.delete(model).where(model.id == legacy))
    assert library_rows(arr_engine) == kept
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1)]


# ------------------------------------------ with Sportarr's ownership tracking
#
# With a Sportarr instance, triggers on table_episodes and table_movies record
# every row change against one global revision row, so a Sportarr subtitle write
# can tell whether media ownership moved under it.

BULK = 1500


def _seed_bulk_media(engine, kind, owner, n):
    """A library some orders of magnitude larger than seed_library's."""
    from app.database import TableEpisodes, TableMovies, TableShows

    with engine.connect() as c:
        if kind == "sonarr":
            show = c.execute(sa.select(TableShows.id).where(
                TableShows.arr_instance_id == owner)).scalar_one()
            c.execute(sa.insert(TableEpisodes), [
                {"arr_instance_id": owner, "series_id": show, "sonarrSeriesId": n,
                 "sonarrEpisodeId": 100000 * n + number, "season": 2, "episode": number,
                 "title": f"Bulk {number}", "path": f"/tv/bulk-{n}/{number}.mkv"}
                for number in range(BULK)])
        else:
            c.execute(sa.insert(TableMovies), [
                {"arr_instance_id": owner, "radarrId": 100000 * n + number,
                 "tmdbId": str(100000 * n + number), "title": f"Bulk {number}",
                 "path": f"/movies/bulk-{n}/{number}.mkv"}
                for number in range(BULK)])


def _track_ownership(engine):
    """What an install with a Sportarr instance runs: the triggers installed."""
    from app.database import TableArrInstances
    from app.ownership_revision import install_ownership_revision

    with engine.connect() as c:
        c.execute(sa.insert(TableArrInstances).values(
            kind="sportarr", name="Sportarr", stable_key="sportarr", port=1867, enabled=1,
            is_default=1))
    with engine.begin() as c:
        install_ownership_revision(c)


def _tracking(engine):
    with engine.connect() as c:
        revision = c.execute(sa.text(
            "SELECT revision FROM subtitle_ownership_revision WHERE id = 1")).scalar_one()
        changes = {(name, row): value for name, row, value in c.execute(sa.text(
            "SELECT table_name, row_id, revision FROM subtitle_ownership_changes"))}
    return revision, changes


def _ownership_protected(engine):
    from sqlalchemy.orm import Session

    from app.ownership_revision import verify_ownership_protection

    session = Session(bind=engine)
    try:
        verify_ownership_protection(session)
    finally:
        session.close()


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_library_removal_under_ownership_tracking_is_recorded_once(
        arr_api, arr_engine, settings_saves, kind):
    """One revision bump per deleted row made a large removal quadratic on
    PostgreSQL, which keeps every update of the one revision row as a new
    version inside the transaction, and held the job queue and every writer of
    the media tables for minutes. The removal is recorded once, with the same
    outcome the per-row triggers leave: a full-resync marker newer than any
    earlier snapshot, no change entry for a row that is gone, and the
    triggers back in place."""
    from app.database import TableEpisodes, TableMovies

    model = TableEpisodes if kind == "sonarr" else TableMovies
    sibling = seed_library(arr_engine, kind, "Sibling", 2, default=True)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)
    _seed_bulk_media(arr_engine, kind, target, 1)
    _track_ownership(arr_engine)
    # A change entry on each side: the target's goes with its row.
    with arr_engine.connect() as c:
        touched = {}
        for owner in (target, sibling):
            touched[owner] = c.execute(sa.select(sa.func.min(model.id)).where(
                model.arr_instance_id == owner)).scalar_one()
            c.execute(sa.update(model).where(model.id == touched[owner]).values(title="Renamed"))
    before, changes = _tracking(arr_engine)
    assert {row for name, row in changes if name == model.__tablename__} == set(touched.values())

    removed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert removed.status_code == 204, removed.json
    after, changes = _tracking(arr_engine)
    if arr_engine.dialect.name == "postgresql":
        # A few bumps for the whole removal, never one per row.
        assert after - before <= 3, after - before
    assert changes[("*", 0)] > before
    assert {row for name, row in changes if name == model.__tablename__} == {touched[sibling]}
    _ownership_protected(arr_engine)
    assert stored(arr_engine) == [(kind, "Sibling", 1, 1), ("sportarr", "Sportarr", 1, 1)]
    # Still recording: the next change is tracked as before.
    with arr_engine.connect() as c:
        c.execute(sa.update(model).where(model.id == touched[sibling]).values(title="Again"))
    assert _tracking(arr_engine)[1][(model.__tablename__, touched[sibling])] > after


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_failed_removal_under_ownership_tracking_leaves_the_tracking_as_it_was(
        arr_api, arr_engine, settings_saves, monkeypatch, kind):
    from arr_instances.repository import ArrInstanceRepository

    seed_library(arr_engine, kind, "Sibling", 2, default=True)
    target = seed_library(arr_engine, kind, "Target", 1, default=False)
    _seed_bulk_media(arr_engine, kind, target, 1)
    _track_ownership(arr_engine)
    before, tracked, owners = library_rows(arr_engine), _tracking(arr_engine), stored(arr_engine)
    reconcile = ArrInstanceRepository._reconcile_default

    def fail(self, kind, demoted_id=None):
        raise RuntimeError("injected")
    monkeypatch.setattr(ArrInstanceRepository, "_reconcile_default", fail)

    failed = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)

    assert failed.status_code == 500
    assert library_rows(arr_engine) == before
    assert stored(arr_engine) == owners
    assert _tracking(arr_engine) == tracked
    _ownership_protected(arr_engine)

    monkeypatch.setattr(ArrInstanceRepository, "_reconcile_default", reconcile)
    retried = arr_api.client.delete(f"{ROOT}/{target}?remove_library=true", headers=HEADERS)
    assert retried.status_code == 204, retried.json
    _ownership_protected(arr_engine)
