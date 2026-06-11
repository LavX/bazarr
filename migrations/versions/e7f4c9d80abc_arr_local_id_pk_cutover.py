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


def upgrade():
    insp = sa.inspect(op.get_context().bind)
    if _already_cut_over(insp):
        logger.info("Phase 1e local-id PK cutover already applied; no-op")
        return
    # Steps A-H are implemented across build increments 1-4. Scaffold only here.
    logger.info("Phase 1e local-id PK cutover: scaffold reached (no DDL yet)")


def downgrade():
    raise NotImplementedError(
        "Phase 1e local-id PK cutover is one-way; restore from backup to revert")
