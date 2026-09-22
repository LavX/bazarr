"""Real PostgreSQL coverage in an isolated, disposable schema."""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from test_media_server_instance_migration import legacy, migration
from test_media_server_instances import payload


@pytest.fixture
def pg_engine():
    url = os.environ.get('BAZARR_PG_TEST_URL')
    if not url:
        pytest.skip('BAZARR_PG_TEST_URL is required for real PostgreSQL coverage')
    schema = 'media_instances_' + uuid4().hex
    admin = sa.create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True)
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA {schema}')
    except Exception:
        admin.dispose()
        pytest.fail('Could not create isolated PostgreSQL test schema', pytrace=False)
    engine = sa.create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True,
                              connect_args={'options': f'-csearch_path={schema}'})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA {schema} CASCADE')
        admin.dispose()


@pytest.fixture
def pg_session(pg_engine):
    from app.database import Base, TableMediaServerImports, TableMediaServerInstances
    with pg_engine.connect() as connection:
        Base.metadata.create_all(connection, tables=[TableMediaServerInstances.__table__,
                                                     TableMediaServerImports.__table__])
        migration().create_media_server_tables(connection)
    with sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)() as session:
        yield session


@pytest.mark.parametrize('fresh', [False, True])
def test_pg_upgrade_and_create_all_have_same_columns_and_constraints(pg_engine, fresh):
    from app.database import Base, TableMediaServerImports, TableMediaServerInstances
    with pg_engine.connect() as connection:
        if fresh:
            Base.metadata.create_all(connection, tables=[TableMediaServerInstances.__table__,
                                                         TableMediaServerImports.__table__])
        migration().create_media_server_tables(connection)
        migration().create_media_server_tables(connection)
        inspector = sa.inspect(connection)
        columns = {column['name']: column for column in inspector.get_columns('media_server_instances')}
        assert {'id', 'kind', 'name', 'enabled', 'url', 'api_key', 'verify_ssl',
                'path_mappings', 'revision'} <= set(columns)
        assert all(column['nullable'] is False for column in columns.values())
        assert {'ck_media_server_kind', 'ck_media_server_enabled', 'ck_media_server_verify_ssl'} <= {
            row['name'] for row in inspector.get_check_constraints('media_server_instances')}
        assert inspector.get_pk_constraint('media_server_instances')['constrained_columns'] == ['id']


@pytest.mark.parametrize('kind', ['emby', 'jellyfin', 'plex', 'silo'])
def test_pg_encrypted_crud_restart_and_no_import_resurrection(pg_session, pg_engine, kind):
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy()
    section = getattr(config, kind)
    section.apikey = '000123'
    section.verify_ssl = False
    assert backfill_instances(pg_session, config)[kind]['created'] is True
    repo = MediaServerInstanceRepository(pg_session)
    imported, = repo.list(kind)
    original_id = imported.id
    sibling = repo.create(**payload(kind, 'B'))
    repo.update(sibling.id, name='Renamed')
    with sessionmaker(bind=pg_engine)() as restarted:
        rows = MediaServerInstanceRepository(restarted)
        assert rows.get_decrypted_api_key(original_id) == '000123'
        assert rows.get(original_id).api_key.startswith('enc:v1:')
        assert rows.get(sibling.id).name == 'Renamed'
        assert rows.get(sibling.id).id != original_id
    repo.delete(original_id)
    backfill_instances(pg_session, config)
    assert [row.id for row in repo.list(kind)] == [sibling.id]


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_pg_import_failure_rolls_back_both_structures_then_retries(pg_session, pg_engine, monkeypatch, kind):
    from types import SimpleNamespace
    from app.database import TableMediaServerImports
    from media_servers import backfill
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy(**{kind: SimpleNamespace(url='http://legacy/base', apikey='000123',
                                            verify_ssl=True, path_mappings=[])})
    record = backfill._record_import
    def fail(session, candidate):
        if candidate == kind:
            raise RuntimeError('synthetic private detail')
        record(session, candidate)
    monkeypatch.setattr(backfill, '_record_import', fail)
    result = backfill.backfill_instances(pg_session, config)
    assert result[kind]['error_code'] == 'migration_failed'
    with sessionmaker(bind=pg_engine)() as restarted:
        assert MediaServerInstanceRepository(restarted).list(kind) == []
        assert restarted.get(TableMediaServerImports, kind) is None
    monkeypatch.setattr(backfill, '_record_import', record)
    assert backfill.backfill_instances(pg_session, config)[kind]['created'] is True


def test_pg_status_page_reads_only_destinations_in_use(pg_session):
    """The status page's destination read is engine-agnostic, so prove it here.

    It adds no column and no migration: it goes through the same repository
    snapshot every worker already uses, which is exactly why it has to hold up
    on PostgreSQL rather than only on SQLite.
    """
    from media_servers import versions
    from media_servers.repository import MediaServerInstanceRepository
    from test_media_server_versions import settings
    versions.reset()
    repo = MediaServerInstanceRepository(pg_session)
    live = repo.create(**payload('emby', 'On'))
    repo.create(**payload('emby', 'Off', enabled=False))
    versions.record(live.id, live.revision, {'success': True, 'version': '4.8.11.0'})
    try:
        entries = versions.statuses(pg_session, settings(), refresh=False)
    finally:
        versions.reset()
    entry, = entries
    # How long this answer stands, which the page needs to know when to come
    # back. It is a clock reading, not a stored value, so it is checked for
    # range rather than matched exactly.
    assert 0 < entry.pop('refresh_in') <= versions.CACHE_SECONDS
    assert entry == {'id': live.id, 'kind': 'emby', 'name': 'On',
                     'state': 'connected', 'version': '4.8.11.0'}
