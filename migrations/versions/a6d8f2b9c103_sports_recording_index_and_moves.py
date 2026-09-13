"""Index recording hashes and preserve history during owned league moves.

Revision ID: a6d8f2b9c103
Revises: f4a7c9d2e105
"""
from alembic import op
import sqlalchemy as sa

revision = 'a6d8f2b9c103'
down_revision = 'f4a7c9d2e105'
branch_labels = None
depends_on = None


def _move_constraints(onupdate):
    bind = op.get_bind()
    for table in ('table_history_sports', 'table_blacklist_sports'):
        name = f'fk_{table}_event_league_owner'
        current = next(item for item in sa.inspect(bind).get_foreign_keys(table) if item['name'] == name)
        if current.get('options', {}).get('onupdate') == onupdate:
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_='foreignkey')
            batch.create_foreign_key(name, 'table_sports_events', ['event_id', 'league_id', 'arr_instance_id'],
                                     ['id', 'league_id', 'arr_instance_id'], ondelete='CASCADE', onupdate=onupdate)


def upgrade():
    bind = op.get_bind()
    if 'table_sports_file_index' not in sa.inspect(bind).get_table_names():
        op.create_table('table_sports_file_index',
                        sa.Column('event_id', sa.Integer(), primary_key=True),
                        sa.Column('arr_instance_id', sa.Integer(), nullable=False),
                        sa.Column('file_id', sa.Integer(), nullable=False),
                        sa.Column('original_path', sa.Text(), nullable=False),
                        sa.Column('scene_name', sa.Text()),
                        sa.Column('connection', sa.Text(), nullable=False),
                        sa.Column('original_name', sa.Text(), nullable=False),
                        sa.Column('mapped_name', sa.Text(), nullable=False),
                        sa.Column('release_name', sa.Text(), nullable=False),
                        sa.Column('physical_path', sa.Text(), nullable=False),
                        sa.Column('stamp', sa.Text()), sa.Column('moviehash', sa.Text()),
                        sa.ForeignKeyConstraint(['event_id', 'arr_instance_id'],
                                                ['table_sports_events.id', 'table_sports_events.arr_instance_id'],
                                                ondelete='CASCADE', name='fk_sports_file_index_event_owner'))
    indexes = {item['name'] for item in sa.inspect(bind).get_indexes('table_sports_file_index')}
    for column, suffix in [('moviehash', 'hash'), ('original_name', 'original_name'),
                           ('mapped_name', 'mapped_name'), ('release_name', 'release_name')]:
        name = f'ix_sports_file_index_{suffix}'
        if name not in indexes:
            op.create_index(name, 'table_sports_file_index', [column])
    _move_constraints('CASCADE')


def downgrade():
    _move_constraints(None)
    op.drop_table('table_sports_file_index')
