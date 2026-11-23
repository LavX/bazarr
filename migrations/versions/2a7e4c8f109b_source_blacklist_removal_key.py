"""Give retained source exclusions an identity independent of a reusable row ID."""
import uuid

from alembic import op
import sqlalchemy as sa


revision = '2a7e4c8f109b'
down_revision = '1f4c2a6b8d03'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column['name']: column for column in sa.inspect(bind).get_columns('table_source_blacklist')}
    if 'removal_key' not in columns:
        op.add_column('table_source_blacklist', sa.Column('removal_key', sa.Text(), nullable=True))
    table = sa.table('table_source_blacklist', sa.column('id', sa.Integer()), sa.column('removal_key', sa.Text()))
    for row_id in bind.execute(sa.select(table.c.id).where(table.c.removal_key.is_(None))).scalars():
        bind.execute(sa.update(table).where(table.c.id == row_id).values(removal_key=uuid.uuid4().hex))
    if 'removal_key' not in columns or columns['removal_key']['nullable']:
        with op.batch_alter_table('table_source_blacklist') as batch:
            batch.alter_column('removal_key', existing_type=sa.Text(), nullable=False)


def downgrade():
    with op.batch_alter_table('table_source_blacklist') as batch:
        batch.drop_column('removal_key')
