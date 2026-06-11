"""multiple arr instances: local-id PK cutover (Phase 1e)

Revision ID: e7f4c9d80abc
Revises: d9a3b7c1e240
Create Date: 2026-06-11 12:00:00.000000

Automatic, idempotent cutover from upstream-id primary keys (sonarrSeriesId /
radarrId / sonarrEpisodeId, and the rootfolder upstream id) to canonical local
integer PKs, so multiple Sonarr/Radarr instances can coexist. Non-destructive:
for an existing single-instance install, local id = upstream id, which preserves
the numeric id space and every parent/child relationship BY VALUE.

Runs at startup via flask_migrate.upgrade() (app/database.py migrate_db). No-ops
on fresh installs (the ORM create_all() already built the final shape) and on a
re-run, via the Step-0 guard. One-way: downgrade() raises; recovery is restore
from backup.

Build order (see docs/superpowers/specs/2026-06-11-phase1e-cutover-design.md):
this file starts as the Step-0 guard scaffold; later increments fill in
Steps A-H (bootstrap, stamp, backfill, validate, rebuild, repoint, validate).
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = 'e7f4c9d80abc'
down_revision = 'd9a3b7c1e240'
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


_SONARR_OWNED = ('table_shows', 'table_episodes', 'table_history',
                 'table_blacklist', 'table_shows_rootfolder')
_RADARR_OWNED = ('table_movies', 'table_history_movie', 'table_blacklist_movie',
                 'table_movies_rootfolder')


def _already_cut_over(insp):
    """True once table_shows' PK is the local ``id``.

    Holds on fresh installs (create_all built the final shape before migrations
    run) and after a prior successful run of this migration, so the whole
    cutover can short-circuit.
    """
    if 'table_shows' not in insp.get_table_names():
        return False
    pk = insp.get_pk_constraint('table_shows')
    return 'id' in (pk.get('constrained_columns') or [])


def _bootstrap_and_stamp(bind):
    """Step A+B: ensure the default Sonarr/Radarr arr_instances rows exist (with
    encrypted api keys) and stamp arr_instance_id on every owned row.

    arr_instance_id is NULL when this migration runs (the post-upgrade
    backfill_default_instances in migrate_db runs later), so the cutover must
    establish ownership itself before adding scoped-unique indexes / NOT NULL.
    Reuses the idempotent backfill, so the later migrate_db call no-ops.
    """
    from sqlalchemy.orm import Session

    from app.config import settings
    from arr_instances.backfill import backfill_default_instances

    session = Session(bind=bind)
    try:
        backfill_default_instances(session, settings)
        session.flush()
    finally:
        session.close()


def _validate_owners(bind):
    """Step E (ownership slice): abort before any DDL if a non-empty owned
    table still has a NULL arr_instance_id (would break the NOT NULL/scoped
    unique steps and leave rows unowned)."""
    for table in _SONARR_OWNED + _RADARR_OWNED:
        nulls = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE arr_instance_id IS NULL")
        ).scalar()
        if nulls:
            raise RuntimeError(
                f"Phase 1e abort: {table} has {nulls} rows with NULL arr_instance_id "
                f"after bootstrap; cannot proceed to the PK cutover")


# Step C+D: local id = upstream id, preserving the numeric space and every
# relationship by value (shows.id=sonarrSeriesId, episodes.id=sonarrEpisodeId,
# etc.). Only fills NULLs so it is idempotent.
_BACKFILL_SQL = (
    "UPDATE table_shows SET id = sonarrSeriesId WHERE id IS NULL",
    "UPDATE table_episodes SET id = sonarrEpisodeId WHERE id IS NULL",
    "UPDATE table_episodes SET series_id = sonarrSeriesId WHERE series_id IS NULL AND sonarrSeriesId IS NOT NULL",
    "UPDATE table_movies SET id = radarrId WHERE id IS NULL",
    "UPDATE table_shows_rootfolder SET upstream_rootfolder_id = id WHERE upstream_rootfolder_id IS NULL",
    "UPDATE table_shows_rootfolder SET local_rootfolder_id = id WHERE local_rootfolder_id IS NULL",
    "UPDATE table_movies_rootfolder SET upstream_rootfolder_id = id WHERE upstream_rootfolder_id IS NULL",
    "UPDATE table_movies_rootfolder SET local_rootfolder_id = id WHERE local_rootfolder_id IS NULL",
    "UPDATE table_history SET series_id = sonarrSeriesId WHERE series_id IS NULL AND sonarrSeriesId IS NOT NULL",
    "UPDATE table_history SET episode_id = sonarrEpisodeId WHERE episode_id IS NULL AND sonarrEpisodeId IS NOT NULL",
    "UPDATE table_history_movie SET movie_id = radarrId WHERE movie_id IS NULL AND radarrId IS NOT NULL",
    "UPDATE table_blacklist SET series_id = sonarr_series_id WHERE series_id IS NULL AND sonarr_series_id IS NOT NULL",
    "UPDATE table_blacklist SET episode_id = sonarr_episode_id WHERE episode_id IS NULL AND sonarr_episode_id IS NOT NULL",
    "UPDATE table_blacklist_movie SET movie_id = radarr_id WHERE movie_id IS NULL AND radarr_id IS NOT NULL",
)

# (table, columns) that become the integer PK and must be fully populated.
_LOCAL_PK = (
    ("table_shows", "id"),
    ("table_episodes", "id"),
    ("table_movies", "id"),
    ("table_shows_rootfolder", "local_rootfolder_id"),
    ("table_movies_rootfolder", "local_rootfolder_id"),
)

# (table, scoped-unique column) pre-checks so the rebuild's UNIQUE index cannot
# fail mid-DDL.
_SCOPED_UNIQUE = (
    ("table_shows", "sonarrSeriesId"),
    ("table_shows", "path"),
    ("table_episodes", "sonarrEpisodeId"),
    ("table_movies", "radarrId"),
    ("table_movies", "path"),
    ("table_movies", "tmdbId"),
    ("table_shows_rootfolder", "upstream_rootfolder_id"),
    ("table_movies_rootfolder", "upstream_rootfolder_id"),
)


def _backfill_local_ids(bind):
    for stmt in _BACKFILL_SQL:
        bind.execute(sa.text(stmt))


def _validate_pre_rebuild(bind):
    """Step E (full): abort before any DDL if local ids are missing or a scoped
    uniqueness collision exists (which would fail the rebuild's UNIQUE index)."""
    for table, col in _LOCAL_PK:
        nulls = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE {col} IS NULL")).scalar()
        if nulls:
            raise RuntimeError(
                f"Phase 1e abort: {table}.{col} has {nulls} NULLs; cannot become the PK")
    for table, col in _SCOPED_UNIQUE:
        dups = bind.execute(sa.text(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} "
            f"WHERE {col} IS NOT NULL GROUP BY arr_instance_id, {col} HAVING COUNT(*) > 1)"
        )).scalar()
        if dups:
            raise RuntimeError(
                f"Phase 1e abort: {table} has {dups} duplicate (arr_instance_id, {col}) "
                f"groups; reconcile before upgrading")


# Steps F+G: rebuild every media/history/blacklist/rootfolder table with the
# local-id PK and repointed FKs. SQLite has no in-place PK change, so each table
# is recreated (env.py runs this with foreign_keys=OFF). The new local id is a
# column-level INTEGER PRIMARY KEY (rowid alias) so a future bare insert
# auto-assigns MAX(id)+1. Parents are rebuilt before children so the child FKs
# resolve to the new local PKs by the time foreign_key_check runs (Step H).
_ALL_OWNED = (
    'table_shows', 'table_episodes', 'table_movies', 'table_shows_rootfolder',
    'table_movies_rootfolder', 'table_history', 'table_history_movie',
    'table_blacklist', 'table_blacklist_movie',
)


def _rebuild_table(bind, table, pk_col, fk_clauses, force_not_null, index_ddls):
    """Recreate `table` with `pk_col` as a column-level INTEGER PRIMARY KEY, the
    given table-level FK clauses, and `force_not_null` columns made NOT NULL;
    preserve every column/type/default/NOT-NULL otherwise; copy all rows; then
    (re)create `index_ddls`. The old global UNIQUE/PK/FK constraints are dropped
    simply by not re-declaring them."""
    info = bind.execute(sa.text(f'PRAGMA table_info("{table}")')).fetchall()
    coldefs, colnames = [], []
    for _cid, name, ctype, notnull, dflt, _is_pk in info:
        colnames.append(f'"{name}"')
        if name == pk_col:
            coldefs.append(f'"{name}" INTEGER PRIMARY KEY')  # rowid alias
            continue
        d = f'"{name}" {ctype or "BLOB"}'
        if notnull or name in force_not_null:
            d += " NOT NULL"
        if dflt is not None:
            d += f" DEFAULT {dflt}"
        coldefs.append(d)

    tmp = f"{table}__p1e_new"
    bind.execute(sa.text(f'DROP TABLE IF EXISTS "{tmp}"'))
    bind.execute(sa.text(
        f'CREATE TABLE "{tmp}" (\n  ' + ",\n  ".join(coldefs + list(fk_clauses)) + "\n)"))
    bind.execute(sa.text(
        f'INSERT INTO "{tmp}" ({", ".join(colnames)}) '
        f'SELECT {", ".join(colnames)} FROM "{table}"'))
    bind.execute(sa.text(f'DROP TABLE "{table}"'))
    bind.execute(sa.text(f'ALTER TABLE "{tmp}" RENAME TO "{table}"'))
    for ddl in index_ddls:
        bind.execute(sa.text(ddl))


def _rebuild_all(bind):
    profile_fk = ('FOREIGN KEY("profileId") REFERENCES table_languages_profiles'
                  '("profileId") ON DELETE SET NULL')

    _rebuild_table(
        bind, 'table_shows', 'id', [profile_fk], {'arr_instance_id'},
        ['CREATE UNIQUE INDEX ux_table_shows_instance_upstream_id ON table_shows (arr_instance_id, "sonarrSeriesId")',
         'CREATE UNIQUE INDEX ux_table_shows_instance_path ON table_shows (arr_instance_id, path)',
         'CREATE INDEX ix_table_shows_profileId ON table_shows ("profileId")'])

    _rebuild_table(
        bind, 'table_episodes', 'id',
        ['FOREIGN KEY("series_id") REFERENCES table_shows("id") ON DELETE CASCADE'],
        {'arr_instance_id'},
        ['CREATE UNIQUE INDEX ux_table_episodes_instance_upstream_id ON table_episodes (arr_instance_id, "sonarrEpisodeId")',
         'CREATE INDEX ix_table_episodes_episode_file_id ON table_episodes (episode_file_id)',
         'CREATE INDEX ix_table_episodes_sonarrSeriesId ON table_episodes ("sonarrSeriesId")',
         'CREATE INDEX ix_table_episodes_series_id ON table_episodes (series_id)'])

    _rebuild_table(
        bind, 'table_movies', 'id', [profile_fk], {'arr_instance_id'},
        ['CREATE UNIQUE INDEX ux_table_movies_instance_upstream_id ON table_movies (arr_instance_id, "radarrId")',
         'CREATE UNIQUE INDEX ux_table_movies_instance_path ON table_movies (arr_instance_id, path)',
         'CREATE UNIQUE INDEX ux_table_movies_instance_tmdbid ON table_movies (arr_instance_id, "tmdbId")',
         'CREATE INDEX ix_table_movies_profileId ON table_movies ("profileId")'])

    _rebuild_table(
        bind, 'table_shows_rootfolder', 'local_rootfolder_id', [],
        {'arr_instance_id', 'upstream_rootfolder_id'},
        ['CREATE UNIQUE INDEX ux_shows_rootfolder_instance_upstream ON table_shows_rootfolder (arr_instance_id, upstream_rootfolder_id)'])

    _rebuild_table(
        bind, 'table_movies_rootfolder', 'local_rootfolder_id', [],
        {'arr_instance_id', 'upstream_rootfolder_id'},
        ['CREATE UNIQUE INDEX ux_movies_rootfolder_instance_upstream ON table_movies_rootfolder (arr_instance_id, upstream_rootfolder_id)'])

    _rebuild_table(
        bind, 'table_history', 'id',
        ['FOREIGN KEY("series_id") REFERENCES table_shows("id") ON DELETE CASCADE',
         'FOREIGN KEY("episode_id") REFERENCES table_episodes("id") ON DELETE CASCADE',
         'FOREIGN KEY("upgradedFromId") REFERENCES table_history("id")'],
        {'arr_instance_id'},
        ['CREATE INDEX ix_table_history_video_path_language_timestamp ON table_history (video_path, language, timestamp)',
         'CREATE INDEX ix_table_history_action ON table_history (action)',
         'CREATE INDEX ix_history_instance_upstream_series ON table_history (arr_instance_id, "sonarrSeriesId")',
         'CREATE INDEX ix_history_instance_upstream_episode ON table_history (arr_instance_id, "sonarrEpisodeId")'])

    _rebuild_table(
        bind, 'table_history_movie', 'id',
        ['FOREIGN KEY("movie_id") REFERENCES table_movies("id") ON DELETE CASCADE',
         'FOREIGN KEY("upgradedFromId") REFERENCES table_history_movie("id")'],
        {'arr_instance_id'},
        ['CREATE INDEX ix_table_history_movie_video_path_language_timestamp ON table_history_movie (video_path, language, timestamp)',
         'CREATE INDEX ix_table_history_movie_action ON table_history_movie (action)',
         'CREATE INDEX ix_history_movie_instance_upstream ON table_history_movie (arr_instance_id, "radarrId")'])

    _rebuild_table(
        bind, 'table_blacklist', 'id',
        ['FOREIGN KEY("series_id") REFERENCES table_shows("id") ON DELETE CASCADE',
         'FOREIGN KEY("episode_id") REFERENCES table_episodes("id") ON DELETE CASCADE'],
        {'arr_instance_id'},
        ['CREATE INDEX ix_table_blacklist_subs_id ON table_blacklist (subs_id)',
         'CREATE INDEX ix_blacklist_instance_upstream_series ON table_blacklist (arr_instance_id, sonarr_series_id)',
         'CREATE INDEX ix_blacklist_instance_upstream_episode ON table_blacklist (arr_instance_id, sonarr_episode_id)'])

    _rebuild_table(
        bind, 'table_blacklist_movie', 'id',
        ['FOREIGN KEY("movie_id") REFERENCES table_movies("id") ON DELETE CASCADE'],
        {'arr_instance_id'},
        ['CREATE INDEX ix_table_blacklist_movie_subs_id ON table_blacklist_movie (subs_id)',
         'CREATE INDEX ix_blacklist_movie_instance_upstream ON table_blacklist_movie (arr_instance_id, radarr_id)'])


def _validate_post_rebuild(bind, pre_counts):
    """Step H: abort (transaction rollback to d9a3b7c1e240) if a rebuild lost
    rows or left a dangling FK."""
    for table, before in pre_counts.items():
        after = bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
        if after != before:
            raise RuntimeError(
                f"Phase 1e abort: {table} row count changed {before} -> {after} "
                f"during rebuild (data loss)")
    violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(
            f"Phase 1e abort: foreign_key_check reported {len(violations)} "
            f"violations after rebuild: {violations[:10]}")


def upgrade():
    bind = op.get_context().bind
    insp = sa.inspect(bind)
    if _already_cut_over(insp):
        logger.info("Phase 1e local-id PK cutover already applied; no-op")
        return

    _bootstrap_and_stamp(bind)        # Steps A+B
    _validate_owners(bind)            # Step E (ownership slice)
    _backfill_local_ids(bind)         # Steps C+D
    _validate_pre_rebuild(bind)       # Step E (local-id + scoped-uniqueness)

    pre_counts = {
        t: bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
        for t in _ALL_OWNED
    }
    _rebuild_all(bind)                # Steps F+G (parents then children)
    _validate_post_rebuild(bind, pre_counts)  # Step H (row parity + FK integrity)

    logger.info("Phase 1e local-id PK cutover complete")


def downgrade():
    raise NotImplementedError(
        "Phase 1e local-id PK cutover is one-way; restore from backup to revert")
