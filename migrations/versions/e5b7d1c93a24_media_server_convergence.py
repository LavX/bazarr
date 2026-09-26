"""Jellyfin and Plex as native media server destinations.

Revision ID: e5b7d1c93a24
Revises: a6d8f2b9c103
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b7d1c93a24'
down_revision = 'a6d8f2b9c103'
branch_labels = None
depends_on = None

_KINDS = ("emby", "jellyfin", "plex", "silo")
_NATIVE_KINDS = ("emby", "silo")
_TABLES = {"media_server_instances": "ck_media_server_kind",
           "media_server_imports": "ck_media_server_import_kind"}
# Existing destinations refresh both libraries today, so they import that way.
_COLUMNS = (("refresh_movies", sa.Integer, "1"), ("refresh_episodes", sa.Integer, "1"),
            ("options", sa.Text, "{}"))
_CHECKS = {"ck_media_server_refresh_movies": "refresh_movies IN (0, 1)",
           "ck_media_server_refresh_episodes": "refresh_episodes IN (0, 1)"}


def _kind_expression(kinds):
    return "kind IN (" + ", ".join("'%s'" % kind for kind in kinds) + ")"


def _missing_columns(inspector):
    present = {column["name"] for column in inspector.get_columns("media_server_instances")}
    return [column for column in _COLUMNS if column[0] not in present]


def _apply(bind, kinds, *, add_columns):
    """Replace both kind constraints and carry the per-type columns with them.

    Batch mode rebuilds the SQLite table once for the whole change, so the
    columns and the constraints have to be decided together rather than in two
    passes that would rebuild it twice.
    """
    inspector = sa.inspect(bind)
    sqlite = bind.dialect.name == "sqlite"
    if sqlite and bind.execute(sa.text("PRAGMA foreign_keys")).scalar():
        # migrations/env.py disables enforcement before running migrations.
        raise RuntimeError("SQLite media server migration requires foreign keys disabled by the migration engine")
    missing = _missing_columns(inspector) if add_columns else []
    for table, constraint in _TABLES.items():
        checks = {item["name"]: item["sqltext"]
                  for item in inspector.get_check_constraints(table)}
        current = checks.get(constraint)
        if current is None:
            raise ValueError("Media server kind constraint is missing on " + table)
        # Metadata-created fresh installs already carry the wider constraint.
        widen = ("'jellyfin'" in current) != ("jellyfin" in kinds)
        columns = missing if table == "media_server_instances" else []
        if not widen and not columns:
            continue
        expression = _kind_expression(kinds)
        if sqlite:
            with op.batch_alter_table(table) as batch:
                for name, type_, default in columns:
                    batch.add_column(sa.Column(name, type_, nullable=False, server_default=default))
                if widen:
                    batch.drop_constraint(constraint, type_="check")
                    batch.create_check_constraint(constraint, expression)
                for name, check in (_CHECKS.items() if columns else ()):
                    batch.create_check_constraint(name, check)
        else:
            for name, type_, default in columns:
                op.add_column(table, sa.Column(name, type_, nullable=False, server_default=default))
            if widen:
                op.drop_constraint(constraint, table, type_="check")
                op.create_check_constraint(constraint, table, expression)
            for name, check in (_CHECKS.items() if columns else ()):
                op.create_check_constraint(name, table, check)


def converge_media_server_tables(bind):
    _apply(bind, _KINDS, add_columns=True)


def upgrade():
    converge_media_server_tables(op.get_bind())


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM media_server_instances "
                            "WHERE kind IN ('jellyfin', 'plex') LIMIT 1")).first():
        raise ValueError("Remove Jellyfin and Plex destinations before downgrading the media server schema")
    if bind.execute(sa.text("SELECT 1 FROM media_server_imports "
                            "WHERE kind IN ('jellyfin', 'plex') LIMIT 1")).first():
        # An import marker without a row still means the kind was migrated, and
        # narrowing the constraint under it would leave a row the table forbids.
        raise ValueError("Remove Jellyfin and Plex import markers before downgrading the media server schema")
    _apply(bind, _NATIVE_KINDS, add_columns=False)
    inspector = sa.inspect(bind)
    present = {column["name"] for column in inspector.get_columns("media_server_instances")}
    if not any(name in present for name, _type, _default in _COLUMNS):
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("media_server_instances") as batch:
            for name in _CHECKS:
                batch.drop_constraint(name, type_="check")
            for name, _type, _default in _COLUMNS:
                batch.drop_column(name)
    else:
        for name in _CHECKS:
            op.drop_constraint(name, "media_server_instances", type_="check")
        for name, _type, _default in _COLUMNS:
            op.drop_column("media_server_instances", name)
