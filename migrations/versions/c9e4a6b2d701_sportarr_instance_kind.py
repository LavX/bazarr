"""Allow Sportarr instance registration.

Revision ID: c9e4a6b2d701
Revises: c2e7a4d9f810
"""
from alembic import op
import sqlalchemy as sa

revision = "c9e4a6b2d701"
down_revision = "c2e7a4d9f810"
branch_labels = None
depends_on = None

_TABLE = "arr_instances"
_CONSTRAINT = "ck_arr_instances_kind"


def _replace_kind_constraint(include_sportarr):
    bind = op.get_bind()
    checks = {item["name"]: item["sqltext"]
              for item in sa.inspect(bind).get_check_constraints(_TABLE)}
    current = checks.get(_CONSTRAINT)
    if current is None:
        raise ValueError("Instance kind constraint is missing")
    if ("'sportarr'" in current) == include_sportarr:
        # Metadata-created fresh installs already have the wider constraint.
        return
    expression = ("kind IN ('sonarr', 'radarr', 'sportarr')" if include_sportarr
                  else "kind IN ('sonarr', 'radarr')")
    if bind.dialect.name == "sqlite":
        # migrations/env.py disables enforcement before running migrations.
        # Rebuilding a referenced parent with it enabled could cascade deletes.
        if bind.execute(sa.text("PRAGMA foreign_keys")).scalar():
            raise RuntimeError("SQLite kind migration requires foreign keys disabled by the migration engine")
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(_CONSTRAINT, type_="check")
            batch.create_check_constraint(_CONSTRAINT, expression)
    else:
        # Native replacement keeps the parent table and all referencing rows.
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
        op.create_check_constraint(_CONSTRAINT, _TABLE, expression)


def upgrade():
    _replace_kind_constraint(include_sportarr=True)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM arr_instances WHERE kind='sportarr' LIMIT 1")).first():
        raise ValueError("Remove Sportarr instances before downgrading the instance kind constraint")
    _replace_kind_constraint(include_sportarr=False)
