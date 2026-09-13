"""Create the owned sports library.

Revision ID: d3f6a8b1e902
Revises: c9e4a6b2d701
"""
from alembic import op
from sqlalchemy import (MetaData, Table, Column, Integer, Text, DateTime, BigInteger, LargeBinary,
                        ForeignKey, ForeignKeyConstraint, UniqueConstraint, Index, inspect, select)

revision = 'd3f6a8b1e902'
down_revision = 'c9e4a6b2d701'
branch_labels = None
depends_on = None


def sports_tables(bind):
    metadata = MetaData()
    for name in ('arr_instances', 'table_languages_profiles'):
        Table(name, metadata, autoload_with=bind)
    tables = []
    tables.append(Table('table_sports_leagues', metadata,
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('arr_instance_id', Integer, ForeignKey('arr_instances.id', ondelete='CASCADE'), nullable=False),
        Column('sportarrLeagueId', Integer, nullable=False),
        Column('externalId', Text),
        Column('path', Text),
        Column('title', Text, nullable=False),
        Column('sortTitle', Text),
        Column('overview', Text),
        Column('poster', Text),
        Column('fanart', Text),
        Column('sport', Text),
        Column('monitored', Text),
        Column('tags', Text),
        Column('audio_language', Text),
        Column('profileId', Integer, ForeignKey('table_languages_profiles.profileId', ondelete='SET NULL')),
        Column('created_at_timestamp', DateTime),
        Column('updated_at_timestamp', DateTime),
        Index('ux_sports_leagues_owner_upstream', 'arr_instance_id', 'sportarrLeagueId', unique=True),
        UniqueConstraint('id', 'arr_instance_id', name='uq_sports_leagues_id_owner'),
    ))
    Index('ix_table_sports_leagues_profileId', tables[-1].c.profileId)
    tables.append(Table('table_sports_events', metadata,
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('arr_instance_id', Integer, ForeignKey('arr_instances.id', ondelete='CASCADE'), nullable=False),
        Column('league_id', Integer, nullable=False),
        Column('sportarrEventId', Integer, nullable=False),
        Column('file_id', Integer, nullable=False),
        Column('sportarrLeagueId', Integer),
        Column('externalId', Text),
        Column('season', Integer),
        Column('episode', Integer),
        Column('partNumber', Integer, nullable=False, server_default='0'),
        Column('partName', Text),
        Column('eventDate', Text),
        Column('broadcastDate', Text),
        Column('path', Text, nullable=False),
        Column('title', Text, nullable=False),
        Column('sceneName', Text),
        Column('audio_codec', Text),
        Column('video_codec', Text),
        Column('format', Text),
        Column('resolution', Text),
        Column('audio_language', Text),
        Column('monitored', Text),
        Column('subtitles', Text),
        Column('missing_subtitles', Text),
        Column('failedAttempts', Text),
        Column('file_size', BigInteger),
        Column('ffprobe_cache', LargeBinary),
        Column('created_at_timestamp', DateTime),
        Column('updated_at_timestamp', DateTime),
        Index('ux_sports_events_owner_event_part', 'arr_instance_id', 'sportarrEventId', 'partNumber', unique=True),
        Index('ux_sports_events_owner_file', 'arr_instance_id', 'file_id', unique=True),
        Index('ix_sports_events_league_owner', 'league_id', 'arr_instance_id'),
        UniqueConstraint('id', 'arr_instance_id', name='uq_sports_events_id_owner'),
        UniqueConstraint('id', 'league_id', 'arr_instance_id', name='uq_sports_events_id_league_owner'),
        ForeignKeyConstraint(['league_id', 'arr_instance_id'], ['table_sports_leagues.id', 'table_sports_leagues.arr_instance_id'], ondelete='CASCADE', name='fk_sports_events_league_owner'),
    ))
    tables.append(Table('table_history_sports', metadata,
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('arr_instance_id', Integer, ForeignKey('arr_instances.id', ondelete='CASCADE'), nullable=False),
        Column('league_id', Integer, nullable=False),
        Column('event_id', Integer, nullable=False),
        Column('language', Text),
        Column('provider', Text),
        Column('subs_id', Text),
        Column('timestamp', DateTime),
        Column('action', Integer),
        Column('description', Text),
        Column('score', Integer),
        Column('score_out_of', Integer),
        Column('subtitles_path', Text),
        Column('video_path', Text),
        Column('matched', Text),
        Column('not_matched', Text),
        Column('upgradedFromId', Integer),
        Index('ix_table_history_sports_owner_event', 'arr_instance_id', 'event_id'),
        ForeignKeyConstraint(['event_id', 'league_id', 'arr_instance_id'], ['table_sports_events.id', 'table_sports_events.league_id', 'table_sports_events.arr_instance_id'], ondelete='CASCADE', name='fk_table_history_sports_event_league_owner'),
        UniqueConstraint('id', 'arr_instance_id', name='uq_history_sports_id_owner'),
        ForeignKeyConstraint(['upgradedFromId', 'arr_instance_id'], ['table_history_sports.id', 'table_history_sports.arr_instance_id'], name='fk_history_sports_upgrade_owner'),
    ))
    tables.append(Table('table_blacklist_sports', metadata,
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('arr_instance_id', Integer, ForeignKey('arr_instances.id', ondelete='CASCADE'), nullable=False),
        Column('league_id', Integer, nullable=False),
        Column('event_id', Integer, nullable=False),
        Column('language', Text),
        Column('provider', Text),
        Column('subs_id', Text),
        Column('timestamp', DateTime),
        Index('ix_table_blacklist_sports_owner_event', 'arr_instance_id', 'event_id'),
        ForeignKeyConstraint(['event_id', 'league_id', 'arr_instance_id'], ['table_sports_events.id', 'table_sports_events.league_id', 'table_sports_events.arr_instance_id'], ondelete='CASCADE', name='fk_table_blacklist_sports_event_league_owner'),
    ))
    tables.append(Table('table_sports_leagues_rootfolder', metadata,
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('arr_instance_id', Integer, ForeignKey('arr_instances.id', ondelete='CASCADE'), nullable=False),
        Column('rootfolder_id', Integer, nullable=False),
        Column('path', Text, nullable=False),
        Column('accessible', Integer),
        Column('error', Text),
        Index('ux_sports_rootfolder_owner_upstream', 'arr_instance_id', 'rootfolder_id', unique=True),
    ))
    return tables


def _delete_action(action):
    return ' '.join((action or 'NO ACTION').upper().split())


def _verify_existing(bind, table):
    inspector = inspect(bind)
    columns = {column['name']: column for column in inspector.get_columns(table.name)}
    expected = {column.name: column.nullable for column in table.columns}
    actual = {name: column['nullable'] for name, column in columns.items()}
    uniques = {tuple(item['column_names']) for item in inspector.get_unique_constraints(table.name)}
    uniques.update(tuple(item['column_names']) for item in inspector.get_indexes(table.name) if item['unique'])
    required_uniques = {tuple(column.name for column in constraint.columns)
                        for constraint in table.constraints if isinstance(constraint, UniqueConstraint)}
    required_uniques.update(tuple(column.name for column in index.columns)
                            for index in table.indexes if index.unique)
    foreign_keys = {(tuple(item['constrained_columns']), item['referred_table'],
                     tuple(item['referred_columns']), _delete_action(item.get('options', {}).get('ondelete')))
                    for item in inspector.get_foreign_keys(table.name)}
    required_foreign_keys = {(tuple(element.parent.name for element in constraint.elements),
                              constraint.elements[0].column.table.name,
                              tuple(element.column.name for element in constraint.elements),
                              _delete_action(constraint.ondelete))
                             for constraint in table.foreign_key_constraints}
    # A SUBSET test, not equality. This snapshot is frozen at this revision,
    # so any column a LATER migration adds is a column this one has never
    # heard of; with `actual != expected` such a column aborted the upgrade and
    # every fresh install crash-looped. table_history_sports.artifact, added by
    # e7a9c2d4b601, is already one, which is why init_db had to exclude that
    # one table from create_all to stay bootable. Extra columns are not the
    # hazard this guard exists for: it is here to refuse a sports schema that
    # lacks the ownership columns, uniques and foreign keys, and a subset test
    # refuses exactly that while tolerating later additions.
    #
    # Column TYPES are deliberately not compared. The reflected type string
    # differs between SQLite and PostgreSQL for the same column, so comparing
    # them would reintroduce the same false-rejection crash-loop across
    # backends that this change removes.
    missing = {name: nullable for name, nullable in expected.items()
               if actual.get(name, object()) != nullable}
    if missing or not required_uniques <= uniques or not required_foreign_keys <= foreign_keys:
        raise ValueError(
            f'Cannot adopt the existing sports schema for {table.name} without verified '
            f'ownership constraints (missing or mismatched columns: '
            f'{sorted(missing) or "none"}; missing uniques: '
            f'{sorted(required_uniques - uniques) or "none"}; missing foreign keys: '
            f'{sorted(str(item) for item in required_foreign_keys - foreign_keys) or "none"})')


def upgrade():
    bind = op.get_bind()
    tables = sports_tables(bind)
    existing = set(inspect(bind).get_table_names())
    # Refuse an unowned upstream schema before creating or modifying any tables.
    for table in tables:
        if table.name in existing:
            _verify_existing(bind, table)
    for table in tables:
        table.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    tables = sports_tables(bind)
    existing = set(inspect(bind).get_table_names())
    for table in tables:
        if table.name in existing and bind.execute(select(table.c.id).limit(1)).first():
            raise ValueError('Remove sports data before downgrading the sports schema')
    for table in reversed(tables):
        table.drop(bind, checkfirst=True)
