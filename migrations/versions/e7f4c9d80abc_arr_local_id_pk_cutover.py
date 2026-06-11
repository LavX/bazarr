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


def upgrade():
    bind = op.get_context().bind
    insp = sa.inspect(bind)
    if _already_cut_over(insp):
        logger.info("Phase 1e local-id PK cutover already applied; no-op")
        return

    _bootstrap_and_stamp(bind)
    _validate_owners(bind)

    # Steps C-H (local-id backfill, validation, table rebuilds, FK repoint) land
    # in build increments 2-4.
    logger.info("Phase 1e: default instances bootstrapped + owners stamped (no DDL yet)")


def downgrade():
    raise NotImplementedError(
        "Phase 1e local-id PK cutover is one-way; restore from backup to revert")
