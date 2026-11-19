"""The media source schema: the models and the migration build the same tables, on SQLite and on PostgreSQL.

Names, CHECK values and every column the later source kinds need are pinned here, so a later change that needs
one more value or column has to say so in a revision of its own.
"""
import importlib.util
import re
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from media_source_helpers import BIG, SOURCE_TABLES, add_group, add_item, add_source, make_engine

VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
REVISION = "b4d1f7a39c25"
PREVIOUS = "c3e8a1f5b972"


def _migration():
    spec = importlib.util.spec_from_file_location("media_sources_migration", VERSIONS / "b4d1f7a39c25_media_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as connection:
        context = MigrationContext.configure(connection, opts={"render_as_batch": True})
        with Operations.context(context):
            step()


def _heads():
    parent_of = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r"^revision\s*=\s*['\"](\w+)", text, re.M)
        parent = re.search(r"^down_revision\s*=\s*(.+)$", text, re.M)
        if revision:
            parent_of[revision.group(1)] = tuple(re.findall(r"['\"](\w+)['\"]", parent.group(1))) if parent else ()
    return parent_of, set(parent_of) - {parent for parents in parent_of.values() for parent in parents}


def _models_without_the_source_tables(metadata, engine):
    """The database the previous revision left: every model table except the seven."""
    others = [table for table in metadata.sorted_tables if table.name not in SOURCE_TABLES]
    metadata.create_all(engine, tables=others)


def _shape(engine):
    """Everything the two builds must agree on, read back from the database itself."""
    inspector = sa.inspect(engine)
    shape = {}
    for name in SOURCE_TABLES:
        columns = {}
        for column in inspector.get_columns(name):
            default = column.get("default")
            columns[column["name"]] = (str(column["type"]).upper(), column["nullable"],
                                       None if default is None else str(default).strip("()'\""))
        indexes = {}
        for index in inspector.get_indexes(name):
            where = (index.get("dialect_options") or {}).get("postgresql_where") \
                or (index.get("dialect_options") or {}).get("sqlite_where")
            indexes[index["name"]] = (tuple(index["column_names"]), bool(index["unique"]),
                                      None if where is None else str(where).replace(" ", "").replace("(", "")
                                      .replace(")", ""))
        foreign_keys = sorted(
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]),
             (fk.get("options") or {}).get("ondelete"))
            for fk in inspector.get_foreign_keys(name))
        checks = {check["name"]: re.sub(r"\s+", " ", check["sqltext"]).replace("(", "").replace(")", "").lower()
                  for check in inspector.get_check_constraints(name)}
        primary = tuple(inspector.get_pk_constraint(name)["constrained_columns"])
        shape[name] = dict(columns=columns, indexes=indexes, foreign_keys=foreign_keys, checks=checks,
                           primary=primary)
    return shape


def test_the_revision_follows_the_previous_head_and_is_the_one_head():
    parent_of, heads = _heads()
    assert parent_of[REVISION] == (PREVIOUS,)
    assert heads == {REVISION}, f"expected the media source revision to be the single head, found {sorted(heads)}"
    migration = _migration()
    assert (migration.revision, migration.down_revision) == (REVISION, PREVIOUS)


def test_the_migration_builds_what_the_models_build(backend, tmp_path):
    from app import database as db

    (tmp_path / "models").mkdir()
    (tmp_path / "migrated").mkdir()
    model_engine, clean_models = make_engine(backend, tmp_path / "models")
    migrated_engine, clean_migrated = make_engine(backend, tmp_path / "migrated")
    try:
        db.Base.metadata.create_all(model_engine)
        _models_without_the_source_tables(db.Base.metadata, migrated_engine)
        assert not set(SOURCE_TABLES) & set(sa.inspect(migrated_engine).get_table_names())
        _run(migrated_engine, _migration().upgrade)
        built, migrated = _shape(model_engine), _shape(migrated_engine)
        for name in SOURCE_TABLES:
            for part in ("columns", "indexes", "foreign_keys", "checks", "primary"):
                assert built[name][part] == migrated[name][part], (name, part)
    finally:
        clean_models()
        clean_migrated()


def test_the_migration_is_a_no_op_where_the_tables_exist_and_downgrades_cleanly(backend, tmp_path):
    from app import database as db

    engine, cleanup = make_engine(backend, tmp_path)
    try:
        db.Base.metadata.create_all(engine)
        before = _shape(engine)
        _run(engine, _migration().upgrade)
        assert _shape(engine) == before
        _run(engine, _migration().downgrade)
        assert not set(SOURCE_TABLES) & set(sa.inspect(engine).get_table_names())
        # What the previous revision owns is untouched.
        assert {"table_movies", "arr_instances", "media_server_instances"} <= set(sa.inspect(engine).get_table_names())
        _run(engine, _migration().upgrade)
        assert _shape(engine) == before
    finally:
        cleanup()


REQUIRED_CHECK_VALUES = {
    "ck_media_sources_kind": ("folder", "jellyfin", "emby"),
    "ck_media_source_libraries_content_kind": ("movie", "series", "sports"),
    "ck_source_groups_kind": ("show", "league"),
    "ck_source_groups_identity_source": ("filename", "tmdb", "media_server", "user"),
    "ck_source_groups_identity_state": ("identified", "parsed", "ambiguous", "unmatched"),
    "ck_source_items_kind": ("movie", "episode", "sports_event"),
    "ck_source_items_identity_source": ("filename", "tmdb", "media_server", "user"),
    "ck_source_items_identity_state": ("identified", "parsed", "ambiguous", "unmatched"),
    "ck_source_items_shadow_arr_table": ("movie", "episode", "sports_event"),
    "ck_source_items_shadow_rung": ("path", "inode", "attributes"),
    "ck_table_source_history_item_kind": ("movie", "episode", "sports_event"),
    "ck_table_source_blacklist_item_kind": ("movie", "episode", "sports_event"),
}


def test_every_allowed_value_the_later_slices_need_is_in_its_check():
    from app import database as db

    found = {}
    for table in db.Base.metadata.sorted_tables:
        if table.name in SOURCE_TABLES:
            for constraint in table.constraints:
                if isinstance(constraint, sa.CheckConstraint) and constraint.name:
                    found[constraint.name] = str(constraint.sqltext)
    for name, values in REQUIRED_CHECK_VALUES.items():
        assert name in found, name
        for value in values:
            assert f"'{value}'" in found[name], (name, value)
    assert "media_server_instance_id IS NOT NULL" in found["ck_media_sources_server"]
    assert "media_server_instance_id IS NULL" in found["ck_media_sources_server"]


# The columns later source kinds and pages read or write: a rename or a drop must be deliberate.
REQUIRED_COLUMNS = {
    "media_sources": {"id", "kind", "name", "enabled", "media_server_instance_id", "scan_frequency_hours",
                      "last_scan_started_at", "last_scan_finished_at", "last_scan_status", "created_at",
                      "updated_at"},
    "media_source_libraries": {"id", "source_id", "content_kind", "locator", "name", "profile_id", "enabled",
                               "last_scan_status", "last_scan_message", "created_at", "updated_at"},
    "media_source_library_roots": {"id", "library_id", "server_path", "local_path", "last_dev"},
    "table_source_groups": {"id", "source_id", "library_id", "kind", "group_key", "dir_dev", "dir_ino",
                            "resolution_note", "external_id", "title", "sort_title", "parsed_title", "year",
                            "tvdb_id", "tmdb_id", "imdb_id", "poster", "identity_source", "identity_state",
                            "identity_candidates", "profile_id", "monitored", "identify_pending", "created_at",
                            "updated_at"},
    "table_source_items": {"id", "source_id", "library_id", "group_id", "kind", "path", "file_dev", "file_ino",
                           "last_seen_at", "missing_scans", "server_missing", "title", "sort_title",
                           "parsed_title", "year", "episode_title", "season", "episode", "part_number",
                           "part_name", "event_date", "imdb_id", "tmdb_id", "tvdb_id", "external_id",
                           "identity_source", "identity_state", "identity_candidates", "scene_name", "file_size",
                           "file_mtime_ns", "ffprobe_cache", "audio_language", "audio_codec", "video_codec",
                           "resolution", "format", "subtitles", "missing_subtitles", "failed_attempts",
                           "profile_id", "monitored", "arr_shadow", "shadow_instance_id", "shadow_owner",
                           "shadow_arr_table", "shadow_arr_row_id", "shadow_arr_path", "shadow_arr_file_id",
                           "shadow_rung", "identify_pending", "created_at", "updated_at", "indexed_at"},
    "table_source_history": {"id", "item_id", "source_id", "item_kind", "item_title", "group_title",
                             "source_name", "action", "description", "language", "provider", "ai_translated",
                             "score", "score_out_of", "subs_id", "subtitles_path", "timestamp", "video_path",
                             "matched", "not_matched", "upgradedFromId"},
    "table_source_blacklist": {"id", "item_id", "source_id", "item_kind", "video_path", "item_title",
                               "group_title", "source_name", "provider", "subs_id", "language", "timestamp"},
}


def test_every_column_the_later_slices_need_is_there_with_its_type():
    from app import database as db

    for name, required in REQUIRED_COLUMNS.items():
        table = db.Base.metadata.tables[name]
        assert {column.name for column in table.columns} == required, name
    items = db.Base.metadata.tables["table_source_items"].c
    groups = db.Base.metadata.tables["table_source_groups"].c
    roots = db.Base.metadata.tables["media_source_library_roots"].c
    # Unsigned 64-bit identities are text: a signed integer column refuses the upper half.
    for column in (items.file_dev, items.file_ino, groups.dir_dev, groups.dir_ino, roots.last_dev):
        assert isinstance(column.type, sa.Text), column
    assert isinstance(groups.tmdb_id.type, sa.Text) and isinstance(groups.tvdb_id.type, sa.Integer)
    assert isinstance(items.file_size.type, sa.BigInteger) and isinstance(items.file_mtime_ns.type, sa.BigInteger)
    assert isinstance(db.Base.metadata.tables["media_sources"].c.media_server_instance_id.type, sa.Text)


def test_the_partial_indexes_carry_the_same_predicate_on_both_dialects():
    from app import database as db

    wanted = next(index for index in db.Base.metadata.tables["table_source_items"].indexes
                  if index.name == "ix_source_items_wanted")
    assert wanted.dialect_options["sqlite"]["where"].text == wanted.dialect_options["postgresql"]["where"].text
    assert "missing_subtitles != '[]'" in wanted.dialect_options["sqlite"]["where"].text
    events = next(index for index in db.Base.metadata.tables["table_source_history"].indexes
                  if index.name == "ix_table_source_history_events")
    assert events.dialect_options["sqlite"]["where"].text == events.dialect_options["postgresql"]["where"].text


def test_an_inode_and_a_device_of_two_to_the_64_minus_one_round_trip_exactly(source_db, tmp_path):
    from app.database import TableMediaSourceLibraryRoots
    from media_sources import repository

    source_id, library_id, root = add_source(source_db, tmp_path)
    item_id = add_item(source_db, source_id, library_id, root / "Film.mkv", file_dev=BIG, file_ino=BIG)
    group_id = add_group(source_db, source_id, library_id, dir_dev=BIG, dir_ino=BIG)
    source_db.execute(sa.update(TableMediaSourceLibraryRoots).values(last_dev=BIG))
    item = repository.get_item(item_id)
    assert (item.file_dev, item.file_ino) == (BIG, BIG)
    group = repository.get_group(group_id)
    assert (group.dir_dev, group.dir_ino) == (BIG, BIG)
    assert repository.get_roots(library_id)[0].last_dev == BIG
    found = source_db.execute(sa.select(repository.Items.c.id).where(
        repository.Items.c.file_ino == BIG, repository.Items.c.file_dev == BIG)).scalar_one()
    assert found == item_id


def _history(session, source_id, item_id, **extra):
    from media_sources import repository
    return repository.insert_history(
        session, item_id=item_id, source_id=source_id, item_kind="movie", action=1, description="downloaded",
        video_path="/media/Film.mkv", item_title="Film", group_title=None, source_name="Local", **extra)


def _blacklist(session, source_id, item_id):
    from media_sources import repository
    return repository.insert_blacklist(
        session, item_id=item_id, source_id=source_id, item_kind="movie", provider="p", subs_id="s1",
        language="en", video_path="/media/Film.mkv", item_title="Film", group_title=None, source_name="Local")


@pytest.mark.parametrize("deleted", ["item", "library", "source", "connection"])
def test_deleting_what_a_row_points_at_keeps_the_history_and_the_blacklist(source_db, tmp_path, deleted):
    from app.database import TableMediaServerInstances
    from media_sources import repository

    server_id = "11111111-1111-1111-1111-111111111111"
    if deleted == "connection":
        source_db.execute(sa.insert(TableMediaServerInstances).values(
            id=server_id, kind="jellyfin", name="Server", enabled=1, url="http://server"))
        source_id = repository.create_source(kind="jellyfin", name="Remote", media_server_instance_id=server_id,
                                             target=source_db)
        library_id = repository.create_library(source_id=source_id, content_kind="movie", locator="lib-1",
                                               local_roots=["/media"], target=source_db)
        root = tmp_path
    else:
        source_id, library_id, root = add_source(source_db, tmp_path)
    item_id = add_item(source_db, source_id, library_id, root / "Film.mkv", title="Film")
    history_id = _history(source_db, source_id, item_id)
    blacklist_id = _blacklist(source_db, source_id, item_id)

    if deleted == "item":
        repository.delete_items([item_id], source_db)
    elif deleted == "library":
        source_db.execute(sa.delete(repository.Libraries).where(repository.Libraries.c.id == library_id))
    elif deleted == "source":
        repository.delete_source(source_id, source_db)
    else:
        source_db.execute(sa.delete(TableMediaServerInstances).where(TableMediaServerInstances.id == server_id))

    history = source_db.execute(sa.select(repository.History).where(repository.History.c.id == history_id)).one()
    blacklist = source_db.execute(sa.select(repository.Blacklist).where(
        repository.Blacklist.c.id == blacklist_id)).one()
    assert (history.item_title, history.source_name, history.video_path) == ("Film", "Local", "/media/Film.mkv")
    assert (blacklist.item_title, blacklist.source_name, blacklist.video_path) == ("Film", "Local",
                                                                                  "/media/Film.mkv")
    assert history.item_id is None and blacklist.item_id is None
    if deleted in ("source", "connection"):
        assert history.source_id is None and blacklist.source_id is None
        assert repository.count_items(source_id=source_id) == 0
        assert repository.list_libraries(source_id) == []
    else:
        assert history.source_id == source_id
    if deleted == "library":
        assert repository.count_items(library_id=library_id) == 0


def test_a_deleted_group_takes_its_episodes_and_leaves_their_history(source_db, tmp_path):
    from media_sources import repository

    source_id, library_id, root = add_source(source_db, tmp_path, content_kind="series")
    group_id = add_group(source_db, source_id, library_id, kind="show", title="Show")
    item_id = add_item(source_db, source_id, library_id, root / "Show" / "S01E01.mkv", kind="episode",
                       group_id=group_id)
    history_id = _history(source_db, source_id, item_id)
    source_db.execute(sa.delete(repository.Groups).where(repository.Groups.c.id == group_id))
    assert repository.get_item(item_id) is None
    row = source_db.execute(sa.select(repository.History.c.item_id).where(
        repository.History.c.id == history_id)).one()
    assert row.item_id is None


def test_a_profile_removed_clears_the_override_and_keeps_the_item(source_db, tmp_path):
    from app.database import TableLanguagesProfiles
    from media_sources import repository
    from media_source_helpers import add_profile

    add_profile(source_db, [("en", "False", "False")], profile_id=3)
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=3)
    item_id = add_item(source_db, source_id, library_id, root / "Film.mkv", profile_id=3)
    source_db.execute(sa.delete(TableLanguagesProfiles))
    assert repository.get_item(item_id).profile_id is None
    assert repository.get_library(library_id).profile_id is None


def test_the_checks_refuse_what_the_model_does_not_allow(source_db, tmp_path):
    from media_sources import repository

    now = datetime.now()
    with pytest.raises(sa.exc.IntegrityError):
        repository.create_source(kind="plex", name="Nope", target=source_db)
    with pytest.raises(sa.exc.IntegrityError):
        # A folder source has no connection, a server source has one.
        repository.create_source(kind="folder", name="Folder", media_server_instance_id="x", target=source_db)
    with pytest.raises(sa.exc.IntegrityError):
        repository.create_source(kind="jellyfin", name="Server", target=source_db)
    source_id, library_id, root = add_source(source_db, tmp_path)
    with pytest.raises(sa.exc.IntegrityError):
        repository.create_library(source_id=source_id, content_kind="music", locator="/x", target=source_db)
    with pytest.raises(sa.exc.IntegrityError):
        # An episode needs a group, a movie must not have one.
        source_db.execute(sa.insert(repository.Items).values(
            source_id=source_id, library_id=library_id, kind="episode", path="/e.mkv", title="e", sort_title="e",
            identity_source="filename", identity_state="parsed", created_at=now, updated_at=now))
    with pytest.raises(sa.exc.IntegrityError):
        source_db.execute(sa.insert(repository.Items).values(
            source_id=source_id, library_id=library_id, kind="movie", path="/m.mkv", title="m", sort_title="m",
            identity_source="oracle", identity_state="parsed", created_at=now, updated_at=now))
    # The path is unique, and so are a source's name and a library's locator within its source.
    add_item(source_db, source_id, library_id, root / "a.mkv")
    with pytest.raises(sa.exc.IntegrityError):
        add_item(source_db, source_id, library_id, root / "a.mkv")
    with pytest.raises(sa.exc.IntegrityError):
        repository.create_source(kind="folder", name="Local", target=source_db)
    with pytest.raises(sa.exc.IntegrityError):
        repository.create_library(source_id=source_id, content_kind="movie", locator=str(root), target=source_db)


def test_the_repository_enforces_the_group_kind_of_an_episode_and_of_an_event(source_db, tmp_path):
    source_id, library_id, root = add_source(source_db, tmp_path, content_kind="series")
    show = add_group(source_db, source_id, library_id, kind="show", title="Show")
    league = add_group(source_db, source_id, library_id, kind="league", title="League")
    add_item(source_db, source_id, library_id, root / "e1.mkv", kind="episode", group_id=show)
    add_item(source_db, source_id, library_id, root / "s1.mkv", kind="sports_event", group_id=league)
    with pytest.raises(ValueError, match="belongs to a show"):
        add_item(source_db, source_id, library_id, root / "e2.mkv", kind="episode", group_id=league)
    with pytest.raises(ValueError, match="belongs to a league"):
        add_item(source_db, source_id, library_id, root / "s2.mkv", kind="sports_event", group_id=show)
    with pytest.raises(ValueError, match="no group"):
        add_item(source_db, source_id, library_id, root / "m.mkv", kind="movie", group_id=show)
    with pytest.raises(ValueError, match="needs a group"):
        add_item(source_db, source_id, library_id, root / "e3.mkv", kind="episode")
    with pytest.raises(ValueError, match="Unknown item kind"):
        add_item(source_db, source_id, library_id, root / "x.mkv", kind="song")
