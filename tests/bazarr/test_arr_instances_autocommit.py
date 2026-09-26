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
    app = Flask(__name__)

    @app.before_request
    def _db_connect():
        database.begin()

    @app.teardown_request
    def _db_close(_exc):
        database.close()

    app.register_blueprint(api_bp)
    try:
        yield SimpleNamespace(client=app.test_client(), refreshed=refreshed)
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
