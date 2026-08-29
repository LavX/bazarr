# coding=utf-8

"""The history/wanted index migration must create what the queries need.

The History pages order by timestamp and had no index behind it, so one page
of 25 rows paid a scan-and-sort of the whole table. The Wanted pages scanned
every media row twice per request (page + count). The migration adds a plain
timestamp index per history table and a partial index matching the wanted
predicate per media table; these tests prove the indexes appear, that the
upgrade is idempotent, and that SQLite actually plans the flooded queries
through them.
"""

import importlib.util
import pathlib


def _load_migration():
    path = (pathlib.Path(__file__).resolve().parents[2] / 'migrations' / 'versions'
            / 'b8d2c5f1a604_history_and_wanted_indexes.py')
    spec = importlib.util.spec_from_file_location('history_wanted_indexes_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPECTED = {
    'ix_table_history_timestamp': 'table_history',
    'ix_table_history_movie_timestamp': 'table_history_movie',
    'ix_table_history_events': 'table_history',
    'ix_table_history_movie_events': 'table_history_movie',
    'ix_table_episodes_wanted': 'table_episodes',
    'ix_table_movies_wanted': 'table_movies',
}


def _index_names(bind, table):
    import sqlalchemy as sa

    return {index['name'] for index in sa.inspect(bind).get_indexes(table)}


def test_upgrade_creates_all_four_indexes(schema_session):
    migration = _load_migration()
    bind = schema_session.get_bind()

    created = migration.create_history_and_wanted_indexes(bind)

    assert sorted(created) == sorted(EXPECTED)
    for name, table in EXPECTED.items():
        assert name in _index_names(bind, table), f'{name} missing on {table}'


def test_upgrade_is_idempotent(schema_session):
    """A re-run (or an adopted database that already has an index) must be a
    no-op rather than a boot-blocking failure."""
    migration = _load_migration()
    bind = schema_session.get_bind()

    migration.create_history_and_wanted_indexes(bind)
    assert migration.create_history_and_wanted_indexes(bind) == []


def test_wanted_partial_index_matches_the_wanted_predicate(schema_session):
    """SQLite only uses a partial index when the query implies its WHERE
    clause; assert against the actual wanted-page predicate so a drive-by
    reformulation of either side fails here first."""
    migration = _load_migration()
    bind = schema_session.get_bind()
    migration.create_history_and_wanted_indexes(bind)

    plan = schema_session.execute(__import__('sqlalchemy').text(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM table_episodes "
        "WHERE missing_subtitles IS NOT NULL AND missing_subtitles != '[]' "
        "ORDER BY \"sonarrEpisodeId\" DESC LIMIT 25")).all()
    plan_text = ' '.join(str(row) for row in plan)
    assert 'ix_table_episodes_wanted' in plan_text, plan_text


def test_history_timestamp_index_plans_the_page_query(schema_session):
    migration = _load_migration()
    bind = schema_session.get_bind()
    migration.create_history_and_wanted_indexes(bind)

    plan = schema_session.execute(__import__('sqlalchemy').text(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM table_history ORDER BY timestamp DESC LIMIT 25")).all()
    plan_text = ' '.join(str(row) for row in plan)
    assert 'ix_table_history_timestamp' in plan_text, plan_text


def test_default_history_view_plans_through_the_events_index(schema_session):
    """The default (embedded-hidden) page must not walk the embedded flood:
    with action != 7 in the query, SQLite must pick the partial events index."""
    migration = _load_migration()
    migration.create_history_and_wanted_indexes(schema_session.get_bind())

    plan = schema_session.execute(__import__('sqlalchemy').text(
        "EXPLAIN QUERY PLAN "
        "SELECT id FROM table_history WHERE action != 7 "
        "ORDER BY timestamp DESC LIMIT 25")).all()
    plan_text = ' '.join(str(row) for row in plan)
    assert 'ix_table_history_events' in plan_text, plan_text
