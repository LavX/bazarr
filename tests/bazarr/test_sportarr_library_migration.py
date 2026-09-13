"""Owned sports schema acceptance on SQLite and PostgreSQL."""

import pytest
import sqlalchemy as sa
from test_sportarr_kind_migration import migration_engine, _run  # noqa: F401

REVISION = 'd3f6a8b1e902'
SPORTS = ['table_sports_leagues', 'table_sports_events', 'table_history_sports',
          'table_blacklist_sports', 'table_sports_leagues_rootfolder']


@pytest.mark.parametrize("fresh", [False, True])
def test_sports_schema_upgrade_fresh_constraints_and_safe_downgrade(
    migration_engine, fresh  # noqa: F811
):
    from app.database import Base, TableArrInstances

    engine = migration_engine
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if fresh or table.name not in SPORTS:
            table.to_metadata(metadata)
    if "table_history_sports" in metadata.tables:
        history = metadata.tables["table_history_sports"]
        history._columns.remove(history.c.artifact)
    metadata.create_all(engine)
    _run(engine, "stamp", "c9e4a6b2d701")
    _run(engine, "upgrade", REVISION)
    assert set(SPORTS) <= set(sa.inspect(engine).get_table_names())
    # A metadata-created install and an upgraded install expose the same columns/indexes.
    for name in SPORTS:
        assert {c["name"] for c in sa.inspect(engine).get_columns(name)} == (
            set(Base.metadata.tables[name].c.keys())
            - ({"artifact"} if name == "table_history_sports" else set())
        )
        assert {
            i["name"]
            for i in sa.inspect(engine).get_indexes(name)
            if not i.get("duplicates_constraint")
        } == {i.name for i in Base.metadata.tables[name].indexes}
        expected = {
            c.name
            for c in Base.metadata.tables[name].constraints
            if isinstance(c, sa.UniqueConstraint)
        }
        assert {
            c["name"] for c in sa.inspect(engine).get_unique_constraints(name)
        } == expected
    with engine.connect() as conn:
        conn.execute(
            sa.insert(TableArrInstances),
            [
                dict(id=1, kind="sportarr", name="One", stable_key="one", port=1867),
                dict(id=2, kind="sportarr", name="Two", stable_key="two", port=1867),
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO table_sports_leagues (id,arr_instance_id,\"sportarrLeagueId\",title) VALUES (1,1,7,'A'),(2,2,7,'B')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO table_sports_events (id,arr_instance_id,league_id,\"sportarrEventId\",file_id,\"partNumber\",path,title) VALUES (1,1,1,9,11,0,'/same.mkv','A'),(2,2,2,9,11,0,'/same.mkv','B')"
            )
        )
        for sql in [
            "INSERT INTO table_sports_leagues (arr_instance_id,\"sportarrLeagueId\",title) VALUES (1,7,'duplicate')",
            "INSERT INTO table_sports_leagues (\"sportarrLeagueId\",title) VALUES (8,'orphan')",
            "INSERT INTO table_sports_events (arr_instance_id,league_id,\"sportarrEventId\",file_id,path,title) VALUES (2,1,10,12,'/wrong.mkv','wrong')",
            "INSERT INTO table_sports_events (arr_instance_id,league_id,\"sportarrEventId\",file_id,path,title) VALUES (1,1,9,12,'/duplicate-part.mkv','duplicate')",
            "INSERT INTO table_sports_events (arr_instance_id,league_id,\"sportarrEventId\",file_id,path,title) VALUES (1,1,10,11,'/duplicate-file.mkv','duplicate')",
            "INSERT INTO table_history_sports (arr_instance_id,league_id,event_id) VALUES (2,2,1)",
            "INSERT INTO table_blacklist_sports (arr_instance_id,league_id,event_id) VALUES (2,2,1)",
        ]:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(sql))
            conn.rollback()
        conn.execute(
            sa.text(
                "INSERT INTO table_history_sports (arr_instance_id,league_id,event_id) VALUES (1,1,1),(2,2,2)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO table_blacklist_sports (arr_instance_id,league_id,event_id) VALUES (1,1,1),(2,2,2)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO table_sports_leagues_rootfolder (arr_instance_id,rootfolder_id,path) VALUES (1,3,'/sports'),(2,3,'/sports')"
            )
        )
    with pytest.raises(ValueError, match="sports data"):
        _run(engine, "downgrade", "c9e4a6b2d701")
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT count(*) FROM table_history_sports")).scalar()
            == 2
        )
        conn.execute(sa.text("DELETE FROM arr_instances WHERE id=1"))
        for name in SPORTS:
            assert conn.execute(
                sa.text(f"SELECT arr_instance_id FROM {name}")
            ).scalars().all() == [2]
        conn.execute(sa.text("DELETE FROM arr_instances WHERE id=2"))
    _run(engine, "downgrade", "c9e4a6b2d701")
    assert not set(SPORTS) & set(sa.inspect(engine).get_table_names())


def test_existing_unowned_sports_table_is_not_silently_adopted(migration_engine):  # noqa: F811
    from app.database import Base

    engine = migration_engine
    metadata = sa.MetaData()
    for name in ("arr_instances", "table_languages_profiles"):
        Base.metadata.tables[name].to_metadata(metadata)
    sa.Table(
        "table_sports_leagues",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.Text),
    )
    if "table_history_sports" in metadata.tables:
        history = metadata.tables["table_history_sports"]
        history._columns.remove(history.c.artifact)
    metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO table_sports_leagues VALUES (1, 'Preserved legacy league')"
            )
        )
    _run(engine, "stamp", "c9e4a6b2d701")
    with pytest.raises(ValueError, match="existing sports schema"):
        _run(engine, "upgrade", REVISION)
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT title FROM table_sports_leagues")).scalar()
            == "Preserved legacy league"
        )
    assert "table_sports_events" not in sa.inspect(engine).get_table_names()


@pytest.mark.parametrize(
    ("table_name", "columns", "ondelete"),
    [
        ("table_sports_leagues", ("arr_instance_id",), "NO ACTION"),
        ("table_sports_leagues", ("arr_instance_id",), None),
        ("table_sports_events", ("league_id", "arr_instance_id"), "NO ACTION"),
        (
            "table_history_sports",
            ("event_id", "league_id", "arr_instance_id"),
            "NO ACTION",
        ),
        (
            "table_blacklist_sports",
            ("event_id", "league_id", "arr_instance_id"),
            "NO ACTION",
        ),
        ("table_sports_leagues", ("profileId",), "CASCADE"),
    ],
    ids=[
        "owner-no-action",
        "owner-unspecified",
        "league-no-action",
        "history-no-action",
        "blacklist-no-action",
        "profile-cascade",
    ],
)
def test_existing_foreign_key_delete_mismatch_is_not_adopted(
    migration_engine, table_name, columns, ondelete  # noqa: F811
):
    from app.database import (
        Base,
        TableArrInstances,
        TableLanguagesProfiles,
        TableSportsLeagues,
    )

    engine = migration_engine
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        # A failed preflight must not create this otherwise missing sports table.
        if table.name != "table_sports_leagues_rootfolder":
            table.to_metadata(metadata)
    constraint = next(
        constraint
        for constraint in metadata.tables[table_name].foreign_key_constraints
        if tuple(column.name for column in constraint.columns) == columns
    )
    constraint.ondelete = ondelete
    for element in constraint.elements:
        element.ondelete = ondelete
    if "table_history_sports" in metadata.tables:
        history = metadata.tables["table_history_sports"]
        history._columns.remove(history.c.artifact)
    metadata.create_all(engine)
    with engine.connect() as connection:
        connection.execute(
            sa.insert(TableArrInstances).values(
                id=1,
                kind="sportarr",
                name="Preserved owner",
                stable_key="preserved",
                port=1867,
            )
        )
        connection.execute(
            sa.insert(TableLanguagesProfiles).values(
                profileId=1, name="Preserved profile", items="[]"
            )
        )
        connection.execute(
            sa.insert(TableSportsLeagues).values(
                id=1,
                arr_instance_id=1,
                sportarrLeagueId=7,
                title="Preserved league",
                profileId=1,
            )
        )
    _run(engine, "stamp", "c9e4a6b2d701")
    with pytest.raises(ValueError, match="existing sports schema"):
        _run(engine, "upgrade", REVISION)
    with engine.connect() as connection:
        assert connection.execute(
            sa.select(TableSportsLeagues.title, TableSportsLeagues.profileId)
        ).one() == ("Preserved league", 1)
        assert (
            connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
            == "c9e4a6b2d701"
        )
    assert "table_sports_leagues_rootfolder" not in sa.inspect(engine).get_table_names()
