"""Database-owned revision coverage, rebuild and publication race contracts."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from test_sportarr_kind_migration import migration_engine, _run  # noqa: F401


@pytest.fixture
def revision_library(migration_engine):  # noqa: F811
    from app.database import Base

    Base.metadata.create_all(migration_engine)
    return migration_engine


def test_direct_sql_insert_update_delete_truncate_and_rollback(revision_library):
    from app.ownership_revision import ownership_revision

    engine = revision_library
    inserts = {
        "arr_instances": "INSERT INTO arr_instances (id,kind,name,stable_key,port,enabled,is_default,ip,base_url,ssl,verify_ssl,http_timeout,api_key,created_at,updated_at) VALUES (1,'sportarr','A','a',1867,1,0,'127.0.0.1','/',0,0,60,'','2026-01-01','2026-01-01')",
        "table_movies": "INSERT INTO table_movies (id,\"radarrId\",\"tmdbId\",title,path) VALUES (1,1,'1','A','/a.mkv')",
        "table_episodes": "INSERT INTO table_episodes (id,\"sonarrEpisodeId\",episode,season,title,path) VALUES (1,1,1,1,'A','/a.mkv')",
        "table_sports_events": "INSERT INTO table_sports_events (id,arr_instance_id,league_id,\"sportarrEventId\",\"partNumber\",file_id,path,title) VALUES (1,2,2,1,0,1,'/a.mkv','A')",
    }
    with engine.connect() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO arr_instances (id,kind,name,stable_key,port,enabled,is_default,ip,base_url,ssl,verify_ssl,http_timeout,api_key,created_at,updated_at) VALUES (2,'sportarr','B','b',1867,1,0,'127.0.0.1','/',0,0,60,'','2026-01-01','2026-01-01')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO table_sports_leagues (id,arr_instance_id,\"sportarrLeagueId\",title) VALUES (2,2,2,'B')"
            )
        )
        for table, insert in inserts.items():
            previous = ownership_revision(connection)
            connection.execute(sa.text(insert))
            assert ownership_revision(connection) > previous
            previous = ownership_revision(connection)
            column = "name" if table == "arr_instances" else "title"
            connection.execute(
                sa.text(f"UPDATE {table} SET {column}='Changed' WHERE id=1")
            )
            assert ownership_revision(connection) > previous
            previous = ownership_revision(connection)
            connection.execute(sa.text(f"DELETE FROM {table} WHERE id=1"))
            assert ownership_revision(connection) > previous
            if engine.dialect.name == "postgresql":
                previous = ownership_revision(connection)
                connection.execute(sa.text(f"TRUNCATE {table} CASCADE"))
                assert ownership_revision(connection) > previous
                # TRUNCATE of the owner cascades through the seed league.
                if table == "arr_instances":
                    connection.execute(
                        sa.text(
                            "INSERT INTO arr_instances (id,kind,name,stable_key,port,enabled,is_default,ip,base_url,ssl,verify_ssl,http_timeout,api_key,created_at,updated_at) VALUES (2,'sportarr','B','b',1867,1,0,'127.0.0.1','/',0,0,60,'','2026-01-01','2026-01-01')"
                        )
                    )
                    connection.execute(
                        sa.text(
                            "INSERT INTO table_sports_leagues (id,arr_instance_id,\"sportarrLeagueId\",title) VALUES (2,2,2,'B')"
                        )
                    )
        before = ownership_revision(connection)
    with engine.connect().execution_options(
        isolation_level="SERIALIZABLE"
    ) as connection:
        transaction = connection.begin()
        connection.execute(sa.text(inserts["table_movies"]))
        assert ownership_revision(connection) > before
        transaction.rollback()
    with engine.connect() as connection:
        assert ownership_revision(connection) == before
        assert connection.execute(sa.text("SELECT id FROM table_movies")).all() == []


@pytest.mark.parametrize("missing", ["seed", "trigger"])
def test_missing_revision_protection_refuses_publication(
    revision_library, tmp_path, missing
):
    from sportarr.output import SportsOutputNamespace

    engine = revision_library
    with engine.connect() as connection:
        if missing == "seed":
            connection.execute(sa.text("DELETE FROM subtitle_ownership_revision"))
        elif engine.dialect.name == "postgresql":
            connection.execute(
                sa.text("DROP TRIGGER ownership_revision ON table_movies")
            )
        else:
            connection.execute(
                sa.text("DROP TRIGGER ownership_revision_table_movies_insert")
            )
    with Session(engine) as session, pytest.raises(ValueError, match="ownership"):
        SportsOutputNamespace(
            SimpleNamespace(
                mapped_path=str(tmp_path / "event.mkv"), event_id=1, arr_instance_id=1
            ),
            session,
        )


def test_migration_reinstalls_rebuilt_native_table_triggers(revision_library):
    from app.database import TableMovies, TableHistoryMovie, TableBlacklistMovie
    from app.ownership_revision import ownership_revision

    engine = revision_library
    # SQLite batch rebuilds discard triggers; PostgreSQL replacement can too.
    with engine.connect() as connection:
        TableHistoryMovie.__table__.drop(connection)
        TableBlacklistMovie.__table__.drop(connection)
        TableMovies.__table__.drop(connection)
        TableMovies.__table__.create(connection)
        TableHistoryMovie.__table__.create(connection)
        TableBlacklistMovie.__table__.create(connection)
    _run(engine, "stamp", "e7a9c2d4b601")
    _run(engine, "upgrade", "f4a7c9d2e105")
    with engine.connect() as connection:
        previous = ownership_revision(connection)
        connection.execute(
            sa.text(
                "INSERT INTO table_movies (id,\"radarrId\",\"tmdbId\",title,path) VALUES (1,1,'1','A','/a.mkv')"
            )
        )
        assert ownership_revision(connection) > previous


from test_sportarr_manual import manual_library  # noqa: E402,F401
from test_sportarr_indexer import indexed_library  # noqa: E402,F401


@pytest.mark.parametrize("mutation", ["import", "remap"])
def test_stalled_ownership_preparation_allows_native_writer_and_rejects_stale_publication(
    manual_library,  # noqa: F811
    monkeypatch,
    mutation,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy.orm import scoped_session, sessionmaker
    from app.database import TableMovies, TableArrInstances
    from sportarr import output
    from subtitles.cache import subtitle_cache
    from subtitles.tools.subsync_engines import subtitle_write_locks
    import json

    service, session, folder = manual_library
    bound = subtitle_cache.get(
        service.manual_search_sports(61, "en", arr_instance_id=1)[0]["subtitle"]
    )
    sessions = scoped_session(sessionmaker(bind=session.get_bind()))
    monkeypatch.setattr(service, "database", sessions)
    stalled, release = Event(), Event()
    original = output._physical

    def physical(path):
        if str(path) == str(folder / "2"):
            stalled.set()
            assert release.wait(5), "Test did not release ownership preparation"
        return original(path)

    monkeypatch.setattr(output, "_physical", physical)
    destination = folder / "1/event.en.srt"

    def publish():
        try:
            with subtitle_write_locks(bound.context.mapped_path, str(destination)):
                with service.sports_publication(bound):
                    destination.write_text(
                        "Must not publish a stale ownership snapshot"
                    )
        finally:
            sessions.remove()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish)
        assert stalled.wait(5)
        try:
            with Session(bind=session.get_bind()) as native:
                native.execute(
                    sa.text(
                        "PRAGMA busy_timeout=300"
                        if native.get_bind().dialect.name == "sqlite"
                        else "SET lock_timeout='300ms'"
                    )
                )
                if mutation == "import":
                    native.execute(
                        sa.insert(TableMovies).values(
                            id=100,
                            radarrId=100,
                            tmdbId="100",
                            title="Native import",
                            path=str(folder / "1/event.mkv"),
                        )
                    )
                else:
                    native.execute(
                        sa.update(TableArrInstances)
                        .where(TableArrInstances.id == 2)
                        .values(
                            path_mappings=json.dumps([["/sports", str(folder / "1")]])
                        )
                    )
        finally:
            release.set()
        with pytest.raises(ValueError, match="changed"):
            future.result(timeout=5)
    assert not destination.exists()
