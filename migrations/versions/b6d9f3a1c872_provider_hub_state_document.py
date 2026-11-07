"""The Plugin Hub state as one versioned document row.

Revision ID: b6d9f3a1c872
Revises: f3b8d2a6c417
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "b6d9f3a1c872"
down_revision = "f3b8d2a6c417"
branch_labels = None
depends_on = None

_TABLE = "provider_hub_state"

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


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

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

    # Bring the existing state.json in once. The file is read and left where
    # it is: it is the backup a downgrade falls back to. A file that cannot be
    # read is not fatal here, because the Hub tries the same import when it is
    # first used and reports the problem there.
    from provider_hub.state_store import import_legacy_state, legacy_state_path
    try:
        import_legacy_state(bind, legacy_state_path())
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
