"""The Plugin Hub state as one versioned document row.

Revision ID: b6d9f3a1c872
Revises: f3b8d2a6c417
"""
import json
import logging
import os
from datetime import datetime

from alembic import op
import sqlalchemy as sa

revision = "b6d9f3a1c872"
down_revision = "f3b8d2a6c417"
branch_labels = None
depends_on = None

_TABLE = "provider_hub_state"
# The shape of the document this revision stores. A later schema change makes
# its own revision; this one keeps writing what it always wrote.
_SCHEMA_VERSION = 1

# The tables the first Plugin Hub revision created and nothing ever wrote to.
# The catalog entries go first: they reference the catalog sources.
_UNUSED_TABLES = (
    "provider_hub_install_events",
    "provider_hub_jobs",
    "provider_hub_secrets",
    "provider_hub_config",
    "provider_hub_installations",
    "provider_hub_catalog_entries",
    "provider_hub_catalog_sources",
)


def _legacy_path():
    from app.get_args import args
    return os.path.join(args.config_dir, "provider_hub", "state.json")


def _legacy_document():
    """The text to import from state.json, or an empty object without one.

    A file that is not valid JSON raises, so the import is skipped and the file
    stays as it is.
    """
    path = _legacy_path()
    if not os.path.exists(path):
        return "{}"
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        logging.warning("The Plugin Hub state file is not an object; importing an empty state")
        return "{}"
    return json.dumps(data, sort_keys=True)


def _import_state(bind, table):
    """Create the single row from state.json unless a row exists.

    The file is only read: it stays where it is as the copy a downgrade falls
    back to, and an existing row is never touched, so a second run, or an
    upgrade after a downgrade, imports only when the row is absent.
    """
    document = _legacy_document()
    if bind.execute(sa.select(table.c.id).where(table.c.id == 1)).first() is not None:
        return
    values = {"id": 1, "schema_version": _SCHEMA_VERSION, "revision": 1,
              "document": document, "updated_at": datetime.now()}
    if bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    bind.execute(insert(table).values(**values).on_conflict_do_nothing(index_elements=["id"]))


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    # A database that started on a build with the model already has the table
    # from create_all, and the app creates it on first use too, so this has
    # to be a no-op when it is there.
    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("schema_version", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("document", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("id = 1", name="ck_provider_hub_state_single_row"),
        )

    # A state file that cannot be read is not fatal here, because the Hub tries
    # the same import when it is first used and reports the problem there.
    table = sa.table(_TABLE, sa.column("id", sa.Integer), sa.column("schema_version", sa.Integer),
                     sa.column("revision", sa.Integer), sa.column("document", sa.Text),
                     sa.column("updated_at", sa.DateTime))
    try:
        _import_state(bind, table)
    except (OSError, ValueError):
        logging.exception("Could not import the Plugin Hub state file; it is left as it is")

    for name in _UNUSED_TABLES:
        if name in tables:
            op.drop_table(name)


def downgrade():
    # The older build reads state.json, which the upgrade kept. The unused
    # tables it declared are created again by its own startup.
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)
