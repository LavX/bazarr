# coding=utf-8
"""Adopting a database created by upstream Bazarr (inbound LavX/bazarr#302).

Upstream's chain runs ``... 309dc062d2e4 -> 7e9a2b1c4d5f -> 0124f9e278fb``. The
fork re-parented ``7e9a2b1c4d5f`` behind its own work and never adopted
``0124f9e278fb``, so an upstream database is stamped with a revision this
codebase has no script for and Alembic refuses to start. That revision also
moves the per-item subtitle list out of ``table_episodes.subtitles`` into a
``table_episodes_subtitles`` table the fork does not model, and drops the
column the fork's indexer reads.

These build a faithful upstream-shaped database and assert both halves of the
adoption: the list is folded back into the legacy column, and the stamp is
rewritten to the newest fork revision whose ancestors that database has
provably applied.
"""
import sqlalchemy as sa

import pytest


def _upstream_database(engine, *, stamp='0124f9e278fb', with_embedded_column=True):
    """A database shaped the way upstream leaves one at ``0124f9e278fb``.

    The ``subtitles`` columns are gone, the split tables hold the rows, and
    alembic_version carries a revision this codebase has no script for.
    """
    embedded = ', embedded_track_id TEXT' if with_embedded_column else ''
    with engine.begin() as connection:
        connection.execute(sa.text(
            'CREATE TABLE table_episodes ('
            '"sonarrEpisodeId" INTEGER PRIMARY KEY, "sonarrSeriesId" INTEGER, path TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_movies ("radarrId" INTEGER PRIMARY KEY, path TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_episodes_subtitles ('
            'id INTEGER PRIMARY KEY, "sonarrEpisodeId" INTEGER, "sonarrSeriesId" INTEGER, '
            f'language TEXT, hi BOOLEAN, forced BOOLEAN, path TEXT, size INTEGER{embedded})'))
        connection.execute(sa.text(
            'CREATE TABLE table_movies_subtitles ('
            'id INTEGER PRIMARY KEY, "radarrId" INTEGER, '
            f'language TEXT, hi BOOLEAN, forced BOOLEAN, path TEXT, size INTEGER{embedded})'))
        connection.execute(sa.text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
        connection.execute(sa.text('INSERT INTO alembic_version (version_num) VALUES (:v)'),
                           {'v': stamp})
    return engine


@pytest.fixture
def upstream_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}")
    try:
        yield _upstream_database(engine)
    finally:
        engine.dispose()


def _stamp(engine):
    with engine.connect() as connection:
        return connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar()


def _subtitles_of(engine, episode_id):
    with engine.connect() as connection:
        return connection.execute(
            sa.text('SELECT subtitles FROM table_episodes WHERE "sonarrEpisodeId" = :i'),
            {'i': episode_id}).scalar()


# --- the language string the indexer writes -------------------------------

def test_a_plain_language_is_carried_across_unchanged():
    from app.upstream_adoption import legacy_subtitle_value

    assert legacy_subtitle_value([
        {'language': 'en', 'hi': False, 'forced': False, 'path': '/m/a.en.srt', 'size': 42},
    ]) == str([['en', '/m/a.en.srt', 42]])


def test_hearing_impaired_and_forced_carry_the_suffix_the_indexer_writes():
    from app.upstream_adoption import legacy_subtitle_value

    value = legacy_subtitle_value([
        {'language': 'en', 'hi': True, 'forced': False, 'path': '/m/a.en.hi.srt', 'size': 1},
        {'language': 'fr', 'hi': False, 'forced': True, 'path': '/m/a.fr.forced.srt', 'size': 2},
    ])

    assert value == str([['en:hi', '/m/a.en.hi.srt', 1], ['fr:forced', '/m/a.fr.forced.srt', 2]])


def test_forced_wins_over_hearing_impaired_the_way_the_indexer_decides_it():
    """store_subtitles tests ``forced`` first, so a row flagged both is forced."""
    from app.upstream_adoption import legacy_subtitle_value

    assert legacy_subtitle_value([
        {'language': 'en', 'hi': True, 'forced': True, 'path': '/m/a.srt', 'size': 1},
    ]) == str([['en:forced', '/m/a.srt', 1]])


def test_a_row_with_no_file_is_an_embedded_track():
    """The fork's indexer records an in-container track as [language, None,
    None] and its file checks short-circuit on the falsy path. Upstream keeps
    those tracks in the same table with no path, so dropping them would make
    migrated media look as if it had lost its embedded languages and set off
    searches for subtitles that are already in the file."""
    from app.upstream_adoption import legacy_subtitle_value

    assert legacy_subtitle_value([
        {'language': 'en', 'hi': False, 'forced': False, 'path': None, 'size': None},
        {'language': 'de', 'hi': False, 'forced': False, 'path': '', 'size': None},
    ]) == str([['en', None, None], ['de', None, None]])


def test_a_row_carrying_a_track_id_is_embedded_whatever_its_path_says():
    from app.upstream_adoption import legacy_subtitle_value

    assert legacy_subtitle_value([
        {'language': 'en', 'hi': False, 'forced': False, 'path': '/m/video.mkv',
         'size': None, 'embedded_track_id': '3'},
        {'language': 'fr', 'hi': False, 'forced': False, 'path': '/m/a.fr.srt', 'size': 7},
    ]) == str([['en', None, None], ['fr', '/m/a.fr.srt', 7]])


def test_an_embedded_track_uses_the_variant_order_the_embedded_indexer_writes():
    """The two paths disagree and both are load-bearing. store_subtitles picks
    one suffix, forced first. normalize_subtitle_language_variant, which the
    embedded pass uses, emits hi first and can emit both."""
    from app.upstream_adoption import legacy_subtitle_value

    assert legacy_subtitle_value([
        {'language': 'en', 'hi': True, 'forced': True, 'path': None, 'size': None},
        {'language': 'fr', 'hi': True, 'forced': False, 'path': None, 'size': None},
        {'language': 'de', 'hi': False, 'forced': True, 'path': None, 'size': None},
    ]) == str([['en:hi:forced', None, None], ['fr:hi', None, None],
               ['de:forced', None, None]])


def test_a_missing_size_becomes_zero_rather_than_none():
    """The indexer compares the stored size against os.stat().st_size. None
    would raise there; zero simply never matches and the file is re-indexed."""
    from app.upstream_adoption import legacy_subtitle_value

    assert legacy_subtitle_value([
        {'language': 'en', 'hi': False, 'forced': False, 'path': '/m/a.srt', 'size': None},
    ]) == str([['en', '/m/a.srt', 0]])


# --- restoring the column -------------------------------------------------

def test_the_dropped_subtitles_column_is_restored(upstream_engine):
    from app.upstream_adoption import restore_legacy_subtitle_columns

    with upstream_engine.begin() as connection:
        restore_legacy_subtitle_columns(connection)

    columns = {c['name'] for c in sa.inspect(upstream_engine).get_columns('table_episodes')}
    assert 'subtitles' in columns
    columns = {c['name'] for c in sa.inspect(upstream_engine).get_columns('table_movies')}
    assert 'subtitles' in columns


def test_the_split_rows_are_folded_back_into_the_restored_column(upstream_engine):
    from app.upstream_adoption import restore_legacy_subtitle_columns

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            'VALUES (11, 1, \'/m/s01e01.mkv\')'))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            'VALUES (11, 1, \'en\', 0, 0, \'/m/s01e01.en.srt\', 120)'))
        connection.execute(sa.text(
            'INSERT INTO table_movies ("radarrId", path) VALUES (5, \'/m/movie.mkv\')'))
        connection.execute(sa.text(
            'INSERT INTO table_movies_subtitles '
            '("radarrId", language, hi, forced, path, size) '
            'VALUES (5, \'fr\', 0, 1, \'/m/movie.fr.srt\', 30)'))

    with upstream_engine.begin() as connection:
        restore_legacy_subtitle_columns(connection)

    assert _subtitles_of(upstream_engine, 11) == str([['en', '/m/s01e01.en.srt', 120]])
    with upstream_engine.connect() as connection:
        assert connection.execute(
            sa.text('SELECT subtitles FROM table_movies WHERE "radarrId" = 5')).scalar() == \
            str([['fr:forced', '/m/movie.fr.srt', 30]])


def test_an_item_with_no_subtitle_rows_gets_an_empty_list_not_null(upstream_engine):
    """``ast.literal_eval`` is only reached when the column is truthy, but the
    API serialises the raw value; an empty list is what the indexer writes for
    an item it found nothing for."""
    from app.upstream_adoption import restore_legacy_subtitle_columns

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            'VALUES (12, 1, \'/m/s01e02.mkv\')'))

    with upstream_engine.begin() as connection:
        restore_legacy_subtitle_columns(connection)

    assert _subtitles_of(upstream_engine, 12) == str([])


def test_restoring_twice_leaves_the_same_value(upstream_engine):
    from app.upstream_adoption import restore_legacy_subtitle_columns

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            'VALUES (13, 1, \'/m/s01e03.mkv\')'))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            'VALUES (13, 1, \'en\', 1, 0, \'/m/s01e03.en.hi.srt\', 9)'))

    for _ in range(2):
        with upstream_engine.begin() as connection:
            restore_legacy_subtitle_columns(connection)

    assert _subtitles_of(upstream_engine, 13) == str([['en:hi', '/m/s01e03.en.hi.srt', 9]])


def test_an_upstream_build_without_the_embedded_column_still_restores(tmp_path):
    """The embedded column arrived later than the split. Inspecting for it
    rather than selecting it blind keeps older upstream databases working."""
    from app.upstream_adoption import restore_legacy_subtitle_columns

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    _upstream_database(engine, with_embedded_column=False)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
                'VALUES (14, 1, \'/m/x.mkv\')'))
            connection.execute(sa.text(
                'INSERT INTO table_episodes_subtitles '
                '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
                'VALUES (14, 1, \'de\', 0, 0, \'/m/x.de.srt\', 4)'))

        with engine.begin() as connection:
            restore_legacy_subtitle_columns(connection)

        assert _subtitles_of(engine, 14) == str([['de', '/m/x.de.srt', 4]])
    finally:
        engine.dispose()


def test_a_fork_database_is_left_completely_alone(tmp_path):
    """The fork's own schema already has the column and no split tables. The
    restore must not touch a value the indexer wrote."""
    from app.upstream_adoption import restore_legacy_subtitle_columns

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fork.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'CREATE TABLE table_episodes ("sonarrEpisodeId" INTEGER PRIMARY KEY, '
                '"sonarrSeriesId" INTEGER, path TEXT, subtitles TEXT)'))
            connection.execute(sa.text(
                'CREATE TABLE table_movies ("radarrId" INTEGER PRIMARY KEY, path TEXT, subtitles TEXT)'))
            connection.execute(sa.text(
                'INSERT INTO table_episodes VALUES (1, 1, \'/m/a.mkv\', :s)'),
                {'s': str([['en', '/m/a.en.srt', 5]])})

        with engine.begin() as connection:
            restore_legacy_subtitle_columns(connection)

        assert _subtitles_of(engine, 1) == str([['en', '/m/a.en.srt', 5]])
    finally:
        engine.dispose()


# --- rewriting the stamp --------------------------------------------------

def test_the_upstream_revision_is_rewritten_to_the_shared_ancestor(upstream_engine):
    """309dc062d2e4, not 7e9a2b1c4d5f. Both are in upstream's chain, but in the
    fork's chain 7e9a2b1c4d5f sits behind two fork revisions this database has
    never run, and claiming it would skip them."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        assert adopt_upstream_database(connection) == '309dc062d2e4'

    assert _stamp(upstream_engine) == '309dc062d2e4'


def test_a_database_already_on_a_fork_revision_is_not_rewritten(tmp_path):
    from app.upstream_adoption import adopt_upstream_database

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ours.db'}")
    _upstream_database(engine, stamp='e7f4c9d80abc')
    try:
        with engine.begin() as connection:
            assert adopt_upstream_database(connection) is None
        assert _stamp(engine) == 'e7f4c9d80abc'
    finally:
        engine.dispose()


def test_an_empty_database_is_not_rewritten(tmp_path):
    """A first run has no alembic_version table at all."""
    from app.upstream_adoption import adopt_upstream_database

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        with engine.begin() as connection:
            assert adopt_upstream_database(connection) is None
    finally:
        engine.dispose()


def test_adopting_twice_is_a_no_op_the_second_time(upstream_engine):
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        assert adopt_upstream_database(connection) == '309dc062d2e4'
    with upstream_engine.begin() as connection:
        assert adopt_upstream_database(connection) is None
    assert _stamp(upstream_engine) == '309dc062d2e4'


def test_adoption_restores_the_column_before_it_rewrites_the_stamp(upstream_engine):
    """The two halves have to happen together: a stamp the fork understands
    with the column still missing would start and then fail in the indexer."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            'VALUES (21, 2, \'/m/y.mkv\')'))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            'VALUES (21, 2, \'es\', 0, 0, \'/m/y.es.srt\', 88)'))

    with upstream_engine.begin() as connection:
        adopt_upstream_database(connection)

    assert _stamp(upstream_engine) == '309dc062d2e4'
    assert _subtitles_of(upstream_engine, 21) == str([['es', '/m/y.es.srt', 88]])


def test_an_unrecognised_revision_is_named_in_the_explanation():
    """Alembic's own failure says only "Can't locate revision identified by X",
    which tells a user nothing about where their database came from."""
    from app.upstream_adoption import explain_unknown_revision

    message = explain_unknown_revision('ffffffffffff')

    assert 'ffffffffffff' in message
    assert 'upstream' in message.lower()


def test_every_revision_in_the_chain_is_recognised():
    """The check that decides whether to print that explanation reads the
    scripts on disk, so it has to actually find them."""
    from app.database import migrations_directory
    from app.upstream_adoption import known_revisions

    revisions = known_revisions(migrations_directory)

    assert '309dc062d2e4' in revisions
    assert _chain_head() in revisions
    assert '0124f9e278fb' not in revisions


def test_a_directory_without_scripts_yields_nothing_rather_than_raising(tmp_path):
    from app.upstream_adoption import known_revisions

    assert known_revisions(str(tmp_path)) == set()


# --- the whole way through startup ----------------------------------------

def _chain_head():
    """The single head of this fork's migration chain, read from the scripts."""
    import os
    import re

    from app.database import migrations_directory

    versions = os.path.join(migrations_directory, 'versions')
    revisions, parents = set(), set()
    for name in os.listdir(versions):
        if not name.endswith('.py'):
            continue
        with open(os.path.join(versions, name), encoding='utf-8') as handle:
            source = handle.read()
        revision = re.search(r"^revision = ['\"]([^'\"]+)['\"]", source, re.M)
        parent = re.search(r"^down_revision = ['\"]([^'\"]+)['\"]", source, re.M)
        if revision:
            revisions.add(revision.group(1))
        if parent:
            parents.add(parent.group(1))
    heads = revisions - parents
    assert len(heads) == 1, f'expected a single head, found {sorted(heads)}'
    return heads.pop()


def _restore_the_upstream_primary_key(connection, table, upstream_id):
    """Rebuild a table the way upstream keys it, on the arr id rather than a
    local one.

    create_all builds this fork's post-cutover shape, which adoption uses to
    tell an upstream database from one a newer Bazarr+ produced. A fixture that
    skipped this would be asking adoption to accept something no upstream
    release can emit.
    """
    columns = connection.execute(sa.text(f'PRAGMA table_info({table})')).fetchall()
    kept = [row[1] for row in columns if row[1] != 'id']
    definitions = []
    for row in columns:
        name, column_type = row[1], row[2] or 'TEXT'
        if name == 'id':
            continue
        if name == upstream_id:
            definitions.append(f'"{name}" {column_type} NOT NULL PRIMARY KEY')
        else:
            definitions.append(f'"{name}" {column_type}')
    column_list = ', '.join(f'"{name}"' for name in kept)
    # Build the replacement first and rename it into place. Renaming the
    # original out of the way instead would have SQLite rewrite every foreign
    # key that points at it, which is not what a fixture wants.
    connection.execute(sa.text(f'CREATE TABLE {table}__upstream ({", ".join(definitions)})'))
    connection.execute(sa.text(
        f'INSERT INTO {table}__upstream ({column_list}) SELECT {column_list} FROM {table}'))
    connection.execute(sa.text(f'DROP TABLE {table}'))
    connection.execute(sa.text(f'ALTER TABLE {table}__upstream RENAME TO {table}'))


def _looks_like_upstream(engine):
    """Turn a fork-shaped database into the shape upstream leaves at 0124f9e278fb.

    Upstream's migration moves the subtitle lists into their own tables and
    drops the source columns, so that is what this does, and then stamps the
    revision this codebase has no script for. The item tables are also keyed
    back the way upstream keys them, because create_all built them in this
    fork's post-cutover shape.
    """
    # Rebuilding a table other tables reference trips the foreign-key check,
    # and the pragma only takes effect outside a transaction.
    rebuild = engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    try:
        rebuild.execute(sa.text('PRAGMA foreign_keys=OFF'))
        _restore_the_upstream_primary_key(rebuild, 'table_shows', 'sonarrSeriesId')
        _restore_the_upstream_primary_key(rebuild, 'table_episodes', 'sonarrEpisodeId')
        _restore_the_upstream_primary_key(rebuild, 'table_movies', 'radarrId')
        rebuild.execute(sa.text('PRAGMA foreign_keys=ON'))
    finally:
        rebuild.close()

    with engine.begin() as connection:
        for item_table in ('table_episodes', 'table_movies'):
            connection.execute(sa.text(f'ALTER TABLE {item_table} DROP COLUMN subtitles'))
        connection.execute(sa.text(
            'CREATE TABLE table_episodes_subtitles (id INTEGER PRIMARY KEY, '
            '"sonarrEpisodeId" INTEGER, "sonarrSeriesId" INTEGER, language TEXT, '
            'hi BOOLEAN, forced BOOLEAN, path TEXT, size INTEGER, embedded_track_id TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_movies_subtitles (id INTEGER PRIMARY KEY, '
            '"radarrId" INTEGER, language TEXT, hi BOOLEAN, forced BOOLEAN, '
            'path TEXT, size INTEGER, embedded_track_id TEXT)'))
        connection.execute(sa.text('DELETE FROM alembic_version'))
        connection.execute(sa.text(
            "INSERT INTO alembic_version (version_num) VALUES ('0124f9e278fb')"))


def test_an_upstream_database_migrates_to_the_fork_head_through_startup(tmp_path, monkeypatch):
    """The failure a user reported is a crash-loop at startup, so the assertion
    that matters is that startup completes. This drives migrate_db, the same
    call main.py makes, against a database shaped the way upstream leaves one."""
    import sqlite3

    from flask import Flask
    from sqlalchemy.orm import scoped_session, sessionmaker

    import app.database as db_module

    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip('DROP COLUMN needs SQLite 3.35; cannot build the upstream shape')

    db_path = tmp_path / 'bazarr.db'
    engine = sa.create_engine(f'sqlite:///{db_path}')
    db_module.Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE IF NOT EXISTS alembic_version '
                                   '(version_num VARCHAR(32) NOT NULL)'))
        # The cutover checks referential integrity, so the episode needs a show.
        connection.execute(sa.text(
            'INSERT INTO table_shows ("sonarrSeriesId", title, path) '
            'VALUES (3, \'Breaking Bad\', \'/m/Breaking Bad\')'))
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", title, path, '
            'season, episode) VALUES (31, 3, \'Ozymandias\', \'/m/s05e14.mkv\', 5, 14)'))

    _looks_like_upstream(engine)

    # A column this fork models that upstream dropped and no migration of ours
    # adds back, because it predates the point the two chains diverged. Only
    # the post-upgrade repair can restore this one.
    with engine.begin() as connection:
        connection.execute(sa.text('ALTER TABLE table_shows DROP COLUMN overview'))

    with engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            'VALUES (31, 3, \'en\', 0, 0, \'/m/s05e14.en.srt\', 31613)'))

    monkeypatch.setattr(db_module, 'engine', engine)
    monkeypatch.setattr(db_module, 'url', f'sqlite:///{db_path}')
    monkeypatch.setattr(db_module, 'database', scoped_session(sessionmaker(bind=engine)))

    application = Flask(__name__)
    application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db_module.migrate_db(application)

    assert _stamp(engine) == _chain_head()
    assert _subtitles_of(engine, 31) == str([['en', '/m/s05e14.en.srt', 31613]])
    assert 'overview' in {c['name'] for c in
                          sa.inspect(engine).get_columns('table_shows')}

    engine.dispose()


# --- a revision nobody has reviewed yet -----------------------------------

def test_an_unknown_upstream_revision_is_adopted_as_well(tmp_path):
    """Upstream's nightly line adds revisions continuously. 537e9b4d10e3 is the
    one a user actually reported, and it did not exist when the first pass of
    this was written. Waiting for a release each time upstream stamps a new
    revision means every nightly user is locked out in the meantime, so an
    unrecognised revision is adopted too rather than left to crash-loop."""
    from app.upstream_adoption import adopt_upstream_database

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'nightly.db'}")
    _upstream_database(engine, stamp='537e9b4d10e3')
    try:
        with engine.begin() as connection:
            assert adopt_upstream_database(connection) == '309dc062d2e4'
        assert _stamp(engine) == '309dc062d2e4'
    finally:
        engine.dispose()


def test_a_revision_from_some_other_database_entirely_is_left_alone(tmp_path):
    """The stamp being unrecognised is not on its own a reason to rewrite it.
    Without the tables Bazarr recognises there is nothing to adopt, and
    guessing would do more harm than refusing to start."""
    from app.upstream_adoption import adopt_upstream_database

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stranger.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text('CREATE TABLE widgets (id INTEGER PRIMARY KEY)'))
            connection.execute(sa.text(
                'CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
            connection.execute(sa.text(
                "INSERT INTO alembic_version (version_num) VALUES ('aaaaaaaaaaaa')"))
        with engine.begin() as connection:
            assert adopt_upstream_database(connection) is None
        assert _stamp(engine) == 'aaaaaaaaaaaa'
    finally:
        engine.dispose()


# --- restoring whatever else upstream dropped -----------------------------

def test_a_column_this_fork_still_models_is_restored_generically(tmp_path):
    """The subtitles column is the one upstream dropped so far. Naming it
    explicitly would mean shipping a patch for the next one, so the repair
    works from the ORM: any column the model declares and the table lacks is
    added back."""
    from app.database import Base
    from app.upstream_adoption import restore_missing_model_columns

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'gap.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(sa.text('ALTER TABLE table_shows DROP COLUMN overview'))
        assert 'overview' not in {c['name'] for c in
                                  sa.inspect(engine).get_columns('table_shows')}

        with engine.begin() as connection:
            restored = restore_missing_model_columns(connection)

        assert ('table_shows', 'overview') in restored
        assert 'overview' in {c['name'] for c in
                              sa.inspect(engine).get_columns('table_shows')}
    finally:
        engine.dispose()


def test_a_table_the_fork_does_not_have_yet_is_not_touched(tmp_path):
    """create_all makes the missing tables; this only fills gaps in the ones
    that are already there."""
    from app.upstream_adoption import restore_missing_model_columns

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        with engine.begin() as connection:
            assert restore_missing_model_columns(connection) == []
    finally:
        engine.dispose()


def test_a_required_column_with_no_default_is_reported_not_forced(tmp_path):
    """Adding a NOT NULL column with no default to a table that already has
    rows fails outright. Better to leave it, say so, and let the rest of the
    adoption succeed than to abort the whole startup."""
    from app.upstream_adoption import restore_missing_model_columns

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'strict.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'CREATE TABLE table_shows ("sonarrSeriesId" INTEGER PRIMARY KEY, '
                'path TEXT NOT NULL)'))
            connection.execute(sa.text(
                'INSERT INTO table_shows VALUES (1, \'/tv/Show\')'))

        with engine.begin() as connection:
            restored = restore_missing_model_columns(connection)

        # title is NOT NULL in the model and the table already has a row, so it
        # cannot be added; the nullable ones around it still are.
        assert ('table_shows', 'title') not in restored
        assert ('table_shows', 'overview') in restored
    finally:
        engine.dispose()


# --- retiring the tables whose contents have been folded across -----------

def test_the_split_tables_are_dropped_once_their_rows_are_folded_across(upstream_engine):
    """They carry a foreign key into table_movies, and this fork's local-id PK
    cutover rebuilds that table, so the constraint fails its own
    foreign_key_check and aborts the migration. Nothing in Bazarr+ reads these
    tables; their contents live in the legacy column by the time they go."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            "VALUES (51, 5, '/m/z.mkv')"))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            "VALUES (51, 5, 'en', 0, 0, '/m/z.en.srt', 6)"))

    with upstream_engine.begin() as connection:
        adopt_upstream_database(connection)

    tables = set(sa.inspect(upstream_engine).get_table_names())
    assert 'table_episodes_subtitles' not in tables
    assert 'table_movies_subtitles' not in tables
    # and only after the rows reached the column this fork reads
    assert _subtitles_of(upstream_engine, 51) == str([['en', '/m/z.en.srt', 6]])


def test_a_split_table_is_kept_when_its_rows_could_not_be_folded(upstream_engine, monkeypatch):
    """Dropping is only safe because the data has already been copied. If the
    copy did not happen, the table stays and the user keeps their rows."""
    import app.upstream_adoption as adoption

    monkeypatch.setattr(adoption, 'restore_legacy_subtitle_columns', lambda connection: [])

    with upstream_engine.begin() as connection:
        adoption.adopt_upstream_database(connection)

    tables = set(sa.inspect(upstream_engine).get_table_names())
    assert 'table_episodes_subtitles' in tables
    assert 'table_movies_subtitles' in tables


def test_no_table_outside_the_known_upstream_pair_is_ever_dropped(upstream_engine):
    """The restore side works from the ORM and is additive, so it can afford to
    be general. Dropping cannot, so it is limited to the two tables upstream's
    split created and whose contents this fork keeps elsewhere."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE somebody_elses_table (id INTEGER PRIMARY KEY)'))
        connection.execute(sa.text('INSERT INTO somebody_elses_table VALUES (1)'))

    with upstream_engine.begin() as connection:
        adopt_upstream_database(connection)

    with upstream_engine.connect() as connection:
        assert connection.execute(
            sa.text('SELECT count(*) FROM somebody_elses_table')).scalar() == 1


# --- restoring a column faithfully, not just by name ----------------------

def test_a_restored_column_keeps_its_server_default_and_not_null(tmp_path):
    """Adding the name and the type only produces a column the model does not
    describe: nullable, no default, NULL in every existing row."""
    import sqlalchemy as sa_
    from app.upstream_adoption import restore_missing_model_columns

    metadata = sa_.MetaData()
    sa_.Table('widgets', metadata,
              sa_.Column('id', sa_.Integer, primary_key=True),
              sa_.Column('kept', sa_.Integer, nullable=False, server_default='7'))

    engine = sa_.create_engine(f"sqlite:///{tmp_path / 'sd.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa_.text('CREATE TABLE widgets (id INTEGER PRIMARY KEY)'))
            connection.execute(sa_.text('INSERT INTO widgets (id) VALUES (1)'))

        with engine.begin() as connection:
            restore_missing_model_columns(connection, metadata=metadata)

        column = next(c for c in sa.inspect(engine).get_columns('widgets')
                      if c['name'] == 'kept')
        assert column['nullable'] is False
        with engine.connect() as connection:
            assert connection.execute(sa.text('SELECT kept FROM widgets WHERE id=1')).scalar() == 7
    finally:
        engine.dispose()


def test_a_required_column_with_only_a_client_side_default_is_not_restored(tmp_path):
    """SQLAlchemy applies a Python-side default on insert, so the database
    would take the column as nullable with no default and leave every existing
    row NULL. That is a schema the model does not describe, and the rows would
    read back as None. Better to leave it out and say so."""
    import sqlalchemy as sa_
    from datetime import datetime

    from app.upstream_adoption import restore_missing_model_columns

    metadata = sa_.MetaData()
    sa_.Table('gadgets', metadata,
              sa_.Column('id', sa_.Integer, primary_key=True),
              sa_.Column('created', sa_.DateTime, nullable=False, default=datetime.now))

    engine = sa_.create_engine(f"sqlite:///{tmp_path / 'cd.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa_.text('CREATE TABLE gadgets (id INTEGER PRIMARY KEY)'))
            connection.execute(sa_.text('INSERT INTO gadgets (id) VALUES (1)'))

        with engine.begin() as connection:
            restored = restore_missing_model_columns(connection, metadata=metadata)

        assert ('gadgets', 'created') not in restored
        assert 'created' not in {c['name'] for c in sa.inspect(engine).get_columns('gadgets')}
    finally:
        engine.dispose()


def test_a_nullable_column_is_still_restored_plainly(tmp_path):
    import sqlalchemy as sa_

    from app.upstream_adoption import restore_missing_model_columns

    metadata = sa_.MetaData()
    sa_.Table('doodads', metadata,
              sa_.Column('id', sa_.Integer, primary_key=True),
              sa_.Column('note', sa_.Text))

    engine = sa_.create_engine(f"sqlite:///{tmp_path / 'nl.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa_.text('CREATE TABLE doodads (id INTEGER PRIMARY KEY)'))
        with engine.begin() as connection:
            restored = restore_missing_model_columns(connection, metadata=metadata)
        assert ('doodads', 'note') in restored
    finally:
        engine.dispose()


# --- telling an upstream database from a newer Bazarr+ one ----------------

def _fork_shaped_database(engine, stamp):
    """A database this fork has already migrated: the local-id PK cutover has
    run, so table_shows is keyed on its own id rather than the upstream one."""
    with engine.begin() as connection:
        connection.execute(sa.text(
            'CREATE TABLE table_shows (id INTEGER PRIMARY KEY, '
            'arr_instance_id INTEGER, "sonarrSeriesId" INTEGER, path TEXT, title TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_episodes (id INTEGER PRIMARY KEY, '
            '"sonarrEpisodeId" INTEGER, "sonarrSeriesId" INTEGER, path TEXT, subtitles TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_movies (id INTEGER PRIMARY KEY, "radarrId" INTEGER, '
            'path TEXT, subtitles TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
        connection.execute(sa.text(
            'INSERT INTO alembic_version (version_num) VALUES (:v)'), {'v': stamp})


def test_a_database_from_a_newer_bazarr_plus_is_left_alone(tmp_path):
    """An older build started against a database a newer release created sees
    a revision it has no script for, on a database carrying every table it
    recognises. Adopting it would stamp the chain back to the shared ancestor
    and run the old migrations over a newer schema, and the newer release
    would then re-run migrations whose history had been erased.

    The local-id PK cutover is the tell: upstream has no such concept, so a
    table_shows keyed on its own id can only have come from this fork."""
    from app.upstream_adoption import adopt_upstream_database

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'newer.db'}")
    try:
        _fork_shaped_database(engine, stamp='ffffffffffff')

        with engine.begin() as connection:
            assert adopt_upstream_database(connection) is None

        assert _stamp(engine) == 'ffffffffffff'
        # and nothing was dropped or rewritten on the way past
        tables = set(sa.inspect(engine).get_table_names())
        assert {'table_shows', 'table_episodes', 'table_movies'} <= tables
    finally:
        engine.dispose()


def test_an_upstream_database_is_still_adopted(upstream_engine):
    """The guard must not cost the case the whole feature exists for: upstream
    keys table_shows on sonarrSeriesId, never on a local id."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'CREATE TABLE table_shows ("sonarrSeriesId" INTEGER PRIMARY KEY, path TEXT)'))

    with upstream_engine.begin() as connection:
        assert adopt_upstream_database(connection) == '309dc062d2e4'


# --- not dropping rows that were never folded -----------------------------

def test_orphan_rows_are_archived_and_the_split_table_still_goes(upstream_engine):
    """The fold walks the item table, so a row whose parent id is missing is
    never copied. Keeping the table to protect those rows does not work: it
    carries a foreign key into the item table, and the local-id PK cutover
    aborts on any foreign_key_check violation, so the user crash-loops anyway
    with adoption already half committed. The rows are copied into a plain
    table with no constraints instead, and the split table goes."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            "VALUES (61, 6, '/m/a.mkv')"))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            "VALUES (61, 6, 'en', 0, 0, '/m/a.en.srt', 5)"))
        # no episode 99 exists
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            "VALUES (99, 6, 'fr', 0, 0, '/m/orphan.fr.srt', 5)"))

    with upstream_engine.begin() as connection:
        adopt_upstream_database(connection)

    tables = set(sa.inspect(upstream_engine).get_table_names())
    assert 'table_episodes_subtitles' not in tables, \
        'the split table has to go or the cutover aborts on its foreign key'
    assert 'orphaned_table_episodes_subtitles' in tables

    with upstream_engine.connect() as connection:
        archived = connection.execute(sa.text(
            'SELECT "sonarrEpisodeId", language, path FROM orphaned_table_episodes_subtitles'
        )).fetchall()
    assert archived == [(99, 'fr', '/m/orphan.fr.srt')], archived

    # the row that did have a parent still reached the column
    assert _subtitles_of(upstream_engine, 61) == str([['en', '/m/a.en.srt', 5]])


def test_nothing_is_archived_when_every_row_has_a_parent(upstream_engine):
    """The archive is for a database that lost referential integrity, not a
    table left behind on every adoption."""
    from app.upstream_adoption import adopt_upstream_database

    with upstream_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            "VALUES (62, 6, '/m/b.mkv')"))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            "VALUES (62, 6, 'en', 0, 0, '/m/b.en.srt', 5)"))

    with upstream_engine.begin() as connection:
        adopt_upstream_database(connection)

    tables = set(sa.inspect(upstream_engine).get_table_names())
    assert 'table_episodes_subtitles' not in tables
    assert not any(name.startswith('orphaned_') for name in tables), tables


# --- not restoring a column whose constraint would be lost ----------------

def test_a_foreign_key_column_is_not_restored_bare(tmp_path):
    """Adding the column without its referential action leaves a schema that
    reports success and then orphans rows on the next parent delete."""
    import sqlalchemy as sa_

    from app.upstream_adoption import restore_missing_model_columns

    metadata = sa_.MetaData()
    sa_.Table('parents', metadata, sa_.Column('id', sa_.Integer, primary_key=True))
    sa_.Table('children', metadata,
              sa_.Column('id', sa_.Integer, primary_key=True),
              sa_.Column('parent_id', sa_.Integer,
                         sa_.ForeignKey('parents.id', ondelete='CASCADE')))

    engine = sa_.create_engine(f"sqlite:///{tmp_path / 'fk.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa_.text('CREATE TABLE parents (id INTEGER PRIMARY KEY)'))
            connection.execute(sa_.text('CREATE TABLE children (id INTEGER PRIMARY KEY)'))

        with engine.begin() as connection:
            restored = restore_missing_model_columns(connection, metadata=metadata)

        assert ('children', 'parent_id') not in restored
        assert 'parent_id' not in {c['name'] for c in
                                   sa.inspect(engine).get_columns('children')}
    finally:
        engine.dispose()


def test_a_column_a_pending_migration_indexes_does_not_crash_the_upgrade(tmp_path, monkeypatch):
    """The repair runs after the chain, so the chain has to survive the gap.

    An upstream build that dropped a column this fork indexes reaches
    4bb94a033f93, which creates an index on it and aborts. Nothing after that
    runs, including the repair that would have put the column back, so the
    install crash-loops exactly as it did before adoption existed.
    """
    import sqlite3

    from flask import Flask
    from sqlalchemy.orm import scoped_session, sessionmaker

    import app.database as db_module

    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip('DROP COLUMN needs SQLite 3.35; cannot build the upstream shape')

    db_path = tmp_path / 'bazarr.db'
    engine = sa.create_engine(f'sqlite:///{db_path}')
    db_module.Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE IF NOT EXISTS alembic_version '
                                   '(version_num VARCHAR(32) NOT NULL)'))
        connection.execute(sa.text(
            'INSERT INTO table_shows ("sonarrSeriesId", title, path) '
            'VALUES (3, \'Breaking Bad\', \'/m/Breaking Bad\')'))
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", title, path, '
            'season, episode) VALUES (31, 3, \'Ozymandias\', \'/m/s05e14.mkv\', 5, 14)'))

    _looks_like_upstream(engine)

    with engine.begin() as connection:
        connection.execute(sa.text('DROP INDEX IF EXISTS ix_table_episodes_episode_file_id'))
        connection.execute(sa.text('ALTER TABLE table_episodes DROP COLUMN episode_file_id'))

    monkeypatch.setattr(db_module, 'engine', engine)
    monkeypatch.setattr(db_module, 'url', f'sqlite:///{db_path}')
    monkeypatch.setattr(db_module, 'database', scoped_session(sessionmaker(bind=engine)))

    application = Flask(__name__)
    application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db_module.migrate_db(application)

    assert _stamp(engine) == _chain_head(), 'the chain did not finish'
    inspector = sa.inspect(engine)
    assert 'episode_file_id' in {c['name'] for c in
                                 inspector.get_columns('table_episodes')}
    assert 'ix_table_episodes_episode_file_id' in {
        index['name'] for index in inspector.get_indexes('table_episodes')}, (
        'the column came back but the index the migration meant to create did '
        'not, so the hot-path query it exists for stays unindexed')

    engine.dispose()


def test_a_previously_stamped_adoption_still_gets_its_columns_repaired(tmp_path, monkeypatch):
    """An older build could adopt (stamp the shared ancestor) and then fail the
    upgrade on a migration that assumed a column upstream had dropped. On the
    next start with the tolerant migrations, adoption finds a known revision
    and returns None; the post-upgrade repairs must still run, or that install
    advances to head with an ORM-required column permanently missing."""
    import sqlite3

    from flask import Flask
    from sqlalchemy.orm import scoped_session, sessionmaker

    import app.database as db_module
    from app.upstream_adoption import FORK_SHARED_ANCESTOR

    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip('DROP COLUMN needs SQLite 3.35; cannot build the shape')

    db_path = tmp_path / 'bazarr.db'
    engine = sa.create_engine(f'sqlite:///{db_path}')
    db_module.Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE IF NOT EXISTS alembic_version '
                                   '(version_num VARCHAR(32) NOT NULL)'))
        connection.execute(sa.text('DELETE FROM alembic_version'))
        connection.execute(sa.text(
            f"INSERT INTO alembic_version (version_num) VALUES ('{FORK_SHARED_ANCESTOR}')"))
        connection.execute(sa.text('ALTER TABLE table_shows DROP COLUMN overview'))

    monkeypatch.setattr(db_module, 'engine', engine)
    monkeypatch.setattr(db_module, 'url', f'sqlite:///{db_path}')
    monkeypatch.setattr(db_module, 'database', scoped_session(sessionmaker(bind=engine)))

    application = Flask(__name__)
    application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db_module.migrate_db(application)

    assert _stamp(engine) == _chain_head()
    assert 'overview' in {c['name'] for c in
                          sa.inspect(engine).get_columns('table_shows')}, (
        'the stamped-but-unrepaired install kept its missing column')

    engine.dispose()
