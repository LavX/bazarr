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

# Upstream-only revisions, each mapped to the newest revision in this fork's
# chain all of whose ancestors that database has provably applied.
#
# For 0124f9e278fb that is 309dc062d2e4 rather than 7e9a2b1c4d5f. Both are in
# upstream's chain and an upstream database has run both, but in this fork's
# chain 7e9a2b1c4d5f sits behind 4bb94a033f93 and f2f74f2d6d0a, which that
# database has never seen. Alembic walks a line, not a set, so claiming
# 7e9a2b1c4d5f would silently claim those two as well.
UPSTREAM_REVISION_EQUIVALENTS = {
    '0124f9e278fb': '309dc062d2e4',
}

# (item table, split table, the column joining them)
_SPLIT_SUBTITLE_TABLES = (
    ('table_episodes', 'table_episodes_subtitles', 'sonarrEpisodeId'),
    ('table_movies', 'table_movies_subtitles', 'radarrId'),
)

_REVISION_ASSIGNMENT = re.compile(r"^revision = ['\"]([^'\"]+)['\"]", re.M)


def legacy_subtitle_value(rows):
    """Build the value ``store_subtitles`` writes, from upstream's split rows.

    ``rows`` are mappings with ``language``, ``hi``, ``forced``, ``path`` and
    ``size``, optionally ``embedded_track_id``. The result is the repr of a
    list of ``[language, path, size]``, which is what the indexer reads back
    with ``ast.literal_eval``.
    """
    subtitles = []
    for row in rows:
        # Upstream keeps in-container tracks in the same table. The fork's
        # legacy column only ever held files on disk, and the indexer stats
        # every path in it, so an embedded row has no place here.
        if row.get('embedded_track_id') is not None:
            continue
        path = row.get('path')
        if not path:
            continue

        language = row.get('language') or ''
        # store_subtitles tests forced first, so a row flagged both is forced.
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


def stamped_revision(connection):
    """The revision in ``alembic_version``, or None on a database without one."""
    if 'alembic_version' not in sa.inspect(connection).get_table_names():
        return None
    return connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar()


def adopt_upstream_database(connection):
    """Make an upstream database one this codebase can migrate.

    Returns the revision it rewrote the stamp to, or None when there was
    nothing to adopt. Runs before Alembic, and does nothing at all to a
    database already on one of this fork's revisions.
    """
    revision = stamped_revision(connection)
    if revision not in UPSTREAM_REVISION_EQUIVALENTS:
        return None

    equivalent = UPSTREAM_REVISION_EQUIVALENTS[revision]
    logging.warning('BAZARR this database was created by upstream Bazarr (revision %s). '
                    'Adopting it: restoring the subtitle columns upstream dropped, then '
                    'continuing this fork\'s migrations from %s.', revision, equivalent)

    # The column has to be back before the stamp changes. A stamp this fork
    # understands with the column still missing would start and then fail in
    # the indexer, which is a worse failure than not starting.
    restore_legacy_subtitle_columns(connection)

    connection.execute(sa.text('UPDATE alembic_version SET version_num = :equivalent '
                               'WHERE version_num = :revision'),
                       {'equivalent': equivalent, 'revision': revision})
    return equivalent


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
    'UPSTREAM_REVISION_EQUIVALENTS',
    'adopt_upstream_database',
    'explain_unknown_revision',
    'known_revisions',
    'legacy_subtitle_value',
    'restore_legacy_subtitle_columns',
    'stamped_revision',
]
