"""add hot-path indexes for sync, history, and blacklist queries

Revision ID: 4bb94a033f93
Revises: 309dc062d2e4
Create Date: 2026-05-02 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '4bb94a033f93'
down_revision = '309dc062d2e4'
branch_labels = None
depends_on = None


_INDEXES = [
    ('ix_table_episodes_sonarrSeriesId', 'table_episodes', ['sonarrSeriesId']),
    ('ix_table_episodes_episode_file_id', 'table_episodes', ['episode_file_id']),
    ('ix_table_movies_profileId', 'table_movies', ['profileId']),
    ('ix_table_shows_profileId', 'table_shows', ['profileId']),
    ('ix_table_history_sonarrEpisodeId', 'table_history', ['sonarrEpisodeId']),
    ('ix_table_history_sonarrSeriesId', 'table_history', ['sonarrSeriesId']),
    ('ix_table_history_action', 'table_history', ['action']),
    ('ix_table_history_movie_radarrId', 'table_history_movie', ['radarrId']),
    ('ix_table_history_movie_action', 'table_history_movie', ['action']),
    ('ix_table_blacklist_subs_id', 'table_blacklist', ['subs_id']),
    ('ix_table_blacklist_movie_subs_id', 'table_blacklist_movie', ['subs_id']),
    ('ix_table_history_video_path_language_timestamp',
     'table_history', ['video_path', 'language', 'timestamp']),
    ('ix_table_history_movie_video_path_language_timestamp',
     'table_history_movie', ['video_path', 'language', 'timestamp']),
]


def _index_exists(bind, table_name, index_name):
    import sqlalchemy as sa
    insp = sa.inspect(bind)
    try:
        existing = insp.get_indexes(table_name)
    except sa.exc.NoSuchTableError:
        return True  # treat missing table as "skip"
    return any(idx['name'] == index_name for idx in existing)


def upgrade():
    bind = op.get_bind()
    for index_name, table_name, columns in _INDEXES:
        if not _index_exists(bind, table_name, index_name):
            op.create_index(index_name, table_name, columns)


def downgrade():
    bind = op.get_bind()
    for index_name, table_name, _ in _INDEXES:
        if _index_exists(bind, table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
