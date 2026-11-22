"""Store item artwork and queue earlier filename identities for confirmation."""
from alembic import op
import sqlalchemy as sa


revision = '1f4c2a6b8d03'
down_revision = 'b4d1f7a39c25'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'poster' not in {column['name'] for column in sa.inspect(bind).get_columns('table_source_items')}:
        op.add_column('table_source_items', sa.Column('poster', sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE table_source_items SET identify_pending = 1 "
                       "WHERE kind = 'movie' AND identity_source = 'filename' AND parsed_title IS NOT NULL"))
    op.execute(sa.text("UPDATE table_source_groups SET identify_pending = 1 "
                       "WHERE kind = 'show' AND identity_source = 'filename'"))
    # The earlier scanner kept the first number even when the file contained
    # several episodes. Invalidate their filename parse until the next scan.
    op.execute(sa.text("UPDATE table_source_items SET season = NULL, episode = NULL, "
                       "scene_name = NULL, identify_pending = 1 "
                       "WHERE kind = 'episode' AND identity_source = 'filename' AND identity_state = 'unmatched' "
                       "AND (season IS NOT NULL OR episode IS NOT NULL)"))


def downgrade():
    op.drop_column('table_source_items', 'poster')
