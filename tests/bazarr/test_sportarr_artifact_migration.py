"""Sports publication proof migrates without rewriting the owned library revision."""

# ruff: noqa: F811
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_sportarr_kind_migration import migration_engine, _run  # noqa: F401


@pytest.mark.parametrize("fresh", [True, False])
def test_startup_and_applied_owned_upgrade_preserve_legacy_history(
    migration_engine, fresh, monkeypatch
):
    from app import database as db

    session = Session(migration_engine)
    monkeypatch.setattr(db, "engine", migration_engine)
    monkeypatch.setattr(db, "database", session)
    if fresh:
        db.init_db()
        _run(migration_engine, "stamp", "c9e4a6b2d701")
    else:
        metadata = sa.MetaData()
        for table in db.Base.metadata.sorted_tables:
            if not table.name.startswith("table_sports_") and table.name not in (
                "table_history_sports",
                "table_blacklist_sports",
            ):
                table.to_metadata(metadata)
        metadata.create_all(migration_engine)
        _run(migration_engine, "stamp", "c9e4a6b2d701")
        _run(migration_engine, "upgrade", "d3f6a8b1e902")
    _run(migration_engine, "upgrade", "e7a9c2d4b601")
    assert set(sa.inspect(migration_engine).get_table_names()) >= {
        "table_history_sports"
    }
    assert {
        c["name"]
        for c in sa.inspect(migration_engine).get_columns("table_history_sports")
    } == set(db.TableHistorySports.__table__.c.keys())
    with migration_engine.connect() as conn:
        conn.execute(
            sa.insert(db.TableArrInstances).values(
                id=1, kind="sportarr", name="A", stable_key="a", port=1867
            )
        )
        conn.execute(
            sa.insert(db.TableSportsLeagues).values(
                id=1, arr_instance_id=1, sportarrLeagueId=7, title="League"
            )
        )
        conn.execute(
            sa.insert(db.TableSportsEvents).values(
                id=1,
                arr_instance_id=1,
                league_id=1,
                sportarrEventId=8,
                file_id=9,
                path="/same.mkv",
                title="Event",
            )
        )
        conn.execute(
            sa.insert(db.TableHistorySports).values(
                id=1,
                arr_instance_id=1,
                league_id=1,
                event_id=1,
                description="Preserved",
                artifact="proof",
            )
        )
    with pytest.raises(ValueError, match="artifact proof"):
        _run(migration_engine, "downgrade", "d3f6a8b1e902")
    with migration_engine.connect() as conn:
        assert (
            conn.execute(sa.select(db.TableHistorySports.description)).scalar_one()
            == "Preserved"
        )
        conn.execute(sa.update(db.TableHistorySports).values(artifact=None))
    _run(migration_engine, "downgrade", "d3f6a8b1e902")
    _run(migration_engine, "upgrade", "e7a9c2d4b601")
    with migration_engine.connect() as conn:
        assert conn.execute(
            sa.select(db.TableHistorySports.description, db.TableHistorySports.artifact)
        ).one() == ("Preserved", None)
    session.close()
