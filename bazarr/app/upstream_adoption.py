# coding=utf-8
"""Adopt a database created by upstream Bazarr.

Upstream's migration chain runs ``... 309dc062d2e4 -> 7e9a2b1c4d5f ->
0124f9e278fb``. This fork re-parented ``7e9a2b1c4d5f`` behind its own work and
never adopted ``0124f9e278fb``, so a database from a current upstream install
is stamped with a revision this codebase has no script for. Alembic refuses to
start and Bazarr+ crash-loops, which is how a user first ran into it.

The stamp is only half of it. ``0124f9e278fb`` also moves the per-item subtitle
list out of ``table_episodes.subtitles`` and ``table_movies.subtitles`` into
new ``table_episodes_subtitles`` / ``table_movies_subtitles`` tables, then
drops the source columns. This fork never took that split: it still models
``subtitles`` as a Text column and the indexer reads it everywhere.
``create_all()`` does not add a column to a table that already exists, so
correcting the stamp alone would produce an install that boots and then falls
over the first time it indexes anything.

So adoption is both halves, in that order: put the column back and fold the
rows into it, then rewrite the stamp.
"""

import logging
import os
import re

import sqlalchemy as sa

# The newest revision in this fork's chain that upstream also has, and so the
# revision to stamp a database from upstream back to.
#
# Every upstream revision descends from it, and it is the last point where the
# two chains agree. It is not 7e9a2b1c4d5f, which upstream also has and an
# upstream database has also run: in this fork's chain that revision sits
# behind 4bb94a033f93 and f2f74f2d6d0a, which such a database has never seen,
# and Alembic walks a line rather than a set, so claiming it would silently
# claim those two as well.
FORK_SHARED_ANCESTOR = '309dc062d2e4'

# Upstream revisions whose contents have actually been read. Adoption does not
# depend on this: an unrecognised revision is adopted too, because upstream's
# nightly line stamps new ones continuously and every nightly user would
# otherwise be locked out until this fork shipped a release naming it. The set
# only decides how loudly it is logged.
REVIEWED_UPSTREAM_REVISIONS = {
    # Moves the subtitle lists into their own tables, drops the source columns,
    # and creates a large set of indexes.
    '0124f9e278fb',
    # Indexes on the tables the revision above created. Nightly only.
    '537e9b4d10e3',
}

# Tables that say this is a Bazarr database rather than something else that
# happens to use Alembic. An unrecognised stamp is not on its own a reason to
# start rewriting a database nobody asked us about.
_BAZARR_MARKER_TABLES = ('table_shows', 'table_episodes', 'table_movies')

# (item table, split table, the column joining them)
_SPLIT_SUBTITLE_TABLES = (
    ('table_episodes', 'table_episodes_subtitles', 'sonarrEpisodeId'),
    ('table_movies', 'table_movies_subtitles', 'radarrId'),
)

_REVISION_ASSIGNMENT = re.compile(r"^revision = ['\"]([^'\"]+)['\"]", re.M)


def legacy_subtitle_value(rows):
    """Build the value the indexer writes, from upstream's split rows.

    ``rows`` are mappings with ``language``, ``hi``, ``forced``, ``path`` and
    ``size``, optionally ``embedded_track_id``. The result is the repr of a
    list of ``[language, path, size]``, which is what the indexer reads back
    with ``ast.literal_eval``.

    A row with no file is an in-container track, which the fork records as
    ``[language, None, None]`` and whose file checks short-circuit on the falsy
    path. Those have to survive the conversion: dropping them would make
    migrated media look as if it had lost its embedded languages, and set off
    searches for subtitles that are already inside the file.
    """
    subtitles = []
    for row in rows:
        path = row.get('path')
        embedded = row.get('embedded_track_id') is not None or not path
        language = row.get('language') or ''

        if embedded:
            # The embedded pass goes through normalize_subtitle_language_variant,
            # which puts hi first and can emit both variants.
            variants = []
            if row.get('hi'):
                variants.append('hi')
            if row.get('forced'):
                variants.append('forced')
            subtitles.append([':'.join([language] + variants), None, None])
            continue

        # store_subtitles picks a single suffix and tests forced first, so a
        # file flagged both is forced.
        if row.get('forced'):
            language = f'{language}:forced'
        elif row.get('hi'):
            language = f'{language}:hi'

        size = row.get('size')
        # The indexer compares this against os.stat().st_size. None would raise
        # there; zero simply never matches and the file is indexed again.
        subtitles.append([language, path, int(size) if size is not None else 0])

    return str(subtitles)


def _table_columns(connection, table_name):
    inspector = sa.inspect(connection)
    try:
        return {column['name'] for column in inspector.get_columns(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def restore_legacy_subtitle_columns(connection):
    """Put ``subtitles`` back and fill it from upstream's split tables.

    Returns the item tables it wrote to. Both steps are skipped where they are
    not needed, so this is safe to run against a database that never came from
    upstream and safe to run twice.
    """
    written = []
    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names())

    for item_table, split_table, join_column in _SPLIT_SUBTITLE_TABLES:
        if item_table not in existing_tables:
            continue

        if 'subtitles' not in _table_columns(connection, item_table):
            connection.execute(sa.text(f'ALTER TABLE {item_table} ADD COLUMN subtitles TEXT'))
            # The inspector caches per call; the column list below is read
            # fresh, so nothing here relies on the stale one.
            logging.info('BAZARR restored the %s.subtitles column dropped by upstream', item_table)

        if split_table not in existing_tables:
            # Nothing to fold in. A fork database reaches here and is left
            # exactly as the indexer wrote it.
            continue

        split_columns = _table_columns(connection, split_table)
        selected = [column for column in
                    ('language', 'hi', 'forced', 'path', 'size', 'embedded_track_id')
                    if column in split_columns]
        column_sql = ', '.join(f'"{column}"' for column in selected)

        rows_by_item = {}
        for row in connection.execute(sa.text(
                f'SELECT "{join_column}", {column_sql} FROM {split_table} '
                f'ORDER BY "{join_column}"')).mappings():
            rows_by_item.setdefault(row[join_column], []).append(row)

        item_ids = [row[0] for row in connection.execute(
            sa.text(f'SELECT "{join_column}" FROM {item_table}'))]

        for item_id in item_ids:
            connection.execute(
                sa.text(f'UPDATE {item_table} SET subtitles = :subtitles '
                        f'WHERE "{join_column}" = :item_id'),
                {'subtitles': legacy_subtitle_value(rows_by_item.get(item_id, [])),
                 'item_id': item_id})

        written.append(item_table)
        logging.info('BAZARR folded %s rows from %s back into %s.subtitles',
                     sum(len(v) for v in rows_by_item.values()), split_table, item_table)

    return written


def restore_missing_model_columns(connection, metadata=None):
    """Add back any column this fork's models declare that a table is missing.

    Upstream dropped ``subtitles`` from ``table_episodes`` and
    ``table_movies``; naming those two would mean shipping a patch the next
    time upstream drops something else. Working from the ORM instead makes the
    repair cover whatever the gap turns out to be.

    ``create_all()`` handles missing tables but never touches an existing one,
    which is the whole reason this is needed.

    A column is only restored when it can be restored faithfully. Adding the
    name and the type alone would produce a column the model does not describe:
    nullable, with no default, and NULL in every row that already exists. So a
    NOT NULL column is added only when it carries a server default the database
    can apply to those rows. One whose default is Python-side is left out and
    reported: SQLAlchemy fills that in on insert, which does nothing for rows
    already there, and SQLite cannot add the constraint afterwards either.

    Returns the ``(table, column)`` pairs it added.
    """
    if metadata is None:
        from app.database import Base

        metadata = Base.metadata

    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names())
    dialect = connection.engine.dialect
    restored = []

    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue

        present = {column['name'] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            if column.foreign_keys:
                # ALTER TABLE ADD COLUMN cannot carry a referential action on
                # SQLite, and adding the column without one produces a schema
                # that reports success and then orphans rows the next time a
                # parent is deleted. Rebuilding the table to do it properly is
                # more than a repair should attempt.
                logging.warning(
                    'BAZARR cannot restore the column %s.%s, it references another table '
                    'and the constraint cannot be added afterwards. Bazarr+ will start, '
                    'but that column stays missing.', table.name, column.name)
                continue

            if not column.nullable and column.server_default is None:
                reason = ('its default is applied by the application rather than the database'
                          if column.default is not None else 'it has no default')
                logging.warning(
                    'BAZARR cannot restore the required column %s.%s, %s. '
                    'Bazarr+ will start, but that column stays missing.',
                    table.name, column.name, reason)
                continue

            clause = f'"{column.name}" {column.type.compile(dialect)}'
            if column.server_default is not None:
                default_sql = getattr(column.server_default, 'arg', None)
                default_sql = getattr(default_sql, 'text', default_sql)
                clause += f' DEFAULT {default_sql}'
            if not column.nullable:
                clause += ' NOT NULL'

            try:
                connection.execute(sa.text(f'ALTER TABLE {table.name} ADD COLUMN {clause}'))
            except Exception:
                logging.exception('BAZARR could not restore the column %s.%s',
                                  table.name, column.name)
                continue

            restored.append((table.name, column.name))
            logging.info('BAZARR restored the missing column %s.%s', table.name, column.name)

    return restored


def retire_split_subtitle_tables(connection, folded_tables):
    """Drop the tables upstream's split created, once their rows are folded across.

    They have to go rather than sit there inertly: each carries a foreign key
    into table_movies or table_episodes, and this fork's local-id PK cutover
    rebuilds both, so the constraint fails the cutover's own
    foreign_key_check and aborts the migration. Renaming does not help, since
    the child keeps its constraint.

    Nothing in Bazarr+ reads these tables. By the time this runs their contents
    are in the column the indexer does read, which is the only reason dropping
    them is acceptable, so a table whose fold did not happen is left alone. The
    pair is named rather than derived: the restore side is additive and can
    afford to work from the ORM, this cannot.
    """
    dropped = []
    existing_tables = set(sa.inspect(connection).get_table_names())

    for item_table, split_table, join_column in _SPLIT_SUBTITLE_TABLES:
        if split_table not in existing_tables:
            continue
        if item_table not in folded_tables:
            logging.warning('BAZARR keeping %s: its rows were not folded into %s.subtitles',
                            split_table, item_table)
            continue

        # The fold walks the item table, so a row pointing at an item that is
        # not there was never copied anywhere. Foreign keys should prevent it,
        # and on SQLite they are not always enforced, so this is checked rather
        # than assumed: dropping the table would destroy those rows for good.
        orphans = connection.execute(sa.text(
            f'SELECT count(*) FROM {split_table} WHERE "{join_column}" IS NULL '
            f'OR "{join_column}" NOT IN (SELECT "{join_column}" FROM {item_table})')).scalar()
        if orphans:
            logging.warning(
                'BAZARR keeping %s: %s of its rows point at an item that is not in %s, so they '
                'were never folded across. Nothing reads this table, and it is left in place '
                'rather than dropped so those rows are not lost.',
                split_table, orphans, item_table)
            continue

        connection.execute(sa.text(f'DROP TABLE {split_table}'))
        dropped.append(split_table)
        logging.info('BAZARR dropped %s, its contents are now in %s.subtitles',
                     split_table, item_table)

    return dropped


def repair_missing_columns_after_upgrade(connection):
    """The safety net, run once the migration chain has finished.

    Upstream dropped ``subtitles``; the next thing it drops will be something
    else, and this fork would be back to shipping a patch for each one. The
    models say what every table should have, so anything still missing after
    the chain has run gets added back from them.

    Deliberately after the upgrade rather than before. Creating columns this
    fork's own migrations are about to create changes what they see, and on
    PostgreSQL the local-id PK cutover in particular wants to build its own.
    """
    return restore_missing_model_columns(connection)


def looks_like_a_bazarr_database(connection):
    """Whether this is a Bazarr database at all."""
    tables = set(sa.inspect(connection).get_table_names())
    return any(marker in tables for marker in _BAZARR_MARKER_TABLES)


def came_from_this_fork(connection):
    """Whether this database has already been through this fork's chain.

    The local-id PK cutover is the tell. Upstream keys table_shows on the
    Sonarr id and has no concept of a local one, so a table_shows keyed on its
    own id can only have been produced here.

    It matters because an unrecognised revision has two possible causes, and
    only one of them is an upstream database. The other is an older Bazarr+
    started against a database a newer release created, where adopting would
    stamp the chain back to the shared ancestor, run the old migrations over a
    newer schema, and leave the newer release re-running migrations whose
    history had been erased. create_all() cannot blur this: it never alters a
    table that already exists.
    """
    inspector = sa.inspect(connection)
    if 'table_shows' not in inspector.get_table_names():
        return False
    try:
        primary_key = inspector.get_pk_constraint('table_shows')
    except Exception:
        return False
    return 'id' in (primary_key.get('constrained_columns') or [])


def stamped_revision(connection):
    """The revision in ``alembic_version``, or None on a database without one."""
    if 'alembic_version' not in sa.inspect(connection).get_table_names():
        return None
    return connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar()


def adopt_upstream_database(connection, migrations_directory=None):
    """Make a database from upstream Bazarr one this codebase can migrate.

    Returns the revision it rewrote the stamp to, or None when there was
    nothing to adopt. Runs before Alembic, and does nothing at all to a
    database already on one of this fork's revisions.
    """
    if migrations_directory is None:
        from app.database import migrations_directory as default_directory

        migrations_directory = default_directory

    revision = stamped_revision(connection)
    if revision is None:
        return None

    ours = known_revisions(migrations_directory)
    if not ours or revision in ours:
        # An empty set means the scripts could not be read. Rewriting a stamp
        # on that basis would be a guess, so nothing is touched.
        return None

    if not looks_like_a_bazarr_database(connection):
        logging.error('BAZARR this database is stamped with the unknown migration revision %s '
                      'and does not look like a Bazarr database. Leaving it alone.', revision)
        return None

    if came_from_this_fork(connection):
        logging.error(
            'BAZARR this database is stamped with migration revision %s, which this build has '
            'no script for, but it has already been through Bazarr+\'s own migrations. It was '
            'almost certainly created by a NEWER Bazarr+ than this one. Downgrading is not '
            'supported: running these older migrations over it would corrupt it. Start the '
            'newer version again, or restore a backup taken before the upgrade.', revision)
        return None

    if revision in REVIEWED_UPSTREAM_REVISIONS:
        logging.warning('BAZARR this database was created by upstream Bazarr (revision %s). '
                        'Adopting it and continuing this fork\'s migrations from %s.',
                        revision, FORK_SHARED_ANCESTOR)
    else:
        logging.warning(
            'BAZARR this database is stamped with migration revision %s, which Bazarr+ has no '
            'script for. It looks like a Bazarr database from a newer upstream than this fork '
            'has adopted, so Bazarr+ is adopting it and continuing its own migrations from %s. '
            'Please report revision %s at https://github.com/LavX/bazarr/issues, and keep a '
            'backup of the database file.', revision, FORK_SHARED_ANCESTOR, revision)

    # Only the subtitle columns are touched here, not every gap the models
    # know about. This runs before Alembic, and pre-creating columns that this
    # fork's own migrations are about to add changes what those migrations see.
    # The general repair happens afterwards instead, once the chain has had its
    # turn: see repair_missing_columns_after_upgrade.
    #
    # These two cannot wait, though. The split tables have to be gone before
    # the local-id PK cutover, whose foreign keys they break, and their
    # contents have to be folded across before they go.
    folded = restore_legacy_subtitle_columns(connection)
    retire_split_subtitle_tables(connection, folded)

    connection.execute(sa.text('UPDATE alembic_version SET version_num = :ancestor '
                               'WHERE version_num = :revision'),
                       {'ancestor': FORK_SHARED_ANCESTOR, 'revision': revision})
    return FORK_SHARED_ANCESTOR


def known_revisions(migrations_directory):
    """Every revision id this codebase has a script for."""
    versions = os.path.join(migrations_directory, 'versions')
    revisions = set()
    try:
        names = os.listdir(versions)
    except OSError:
        return revisions
    for name in names:
        if not name.endswith('.py'):
            continue
        try:
            with open(os.path.join(versions, name), encoding='utf-8') as handle:
                source = handle.read()
        except OSError:
            continue
        match = _REVISION_ASSIGNMENT.search(source)
        if match:
            revisions.add(match.group(1))
    return revisions


def explain_unknown_revision(revision):
    """What to tell a user whose database Alembic is about to refuse.

    Alembic says only "Can't locate revision identified by X" and exits, which
    gives no clue that the database came from somewhere this fork cannot follow.
    """
    return (
        f'This database is stamped with migration revision {revision}, which Bazarr+ has no '
        'script for. That normally means it was created by a newer upstream Bazarr than this '
        'fork has adopted, and Bazarr+ cannot migrate it safely without knowing what that '
        'revision changed. Please report it at https://github.com/LavX/bazarr/issues quoting '
        f'revision {revision}, and keep a backup of the database file.'
    )


__all__ = [
    'FORK_SHARED_ANCESTOR',
    'REVIEWED_UPSTREAM_REVISIONS',
    'adopt_upstream_database',
    'explain_unknown_revision',
    'known_revisions',
    'legacy_subtitle_value',
    'looks_like_a_bazarr_database',
    'repair_missing_columns_after_upgrade',
    'restore_missing_model_columns',
    'retire_split_subtitle_tables',
    'restore_legacy_subtitle_columns',
    'stamped_revision',
]
