import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


def legacy(**overrides):
    rows = {kind: SimpleNamespace(url='', apikey='', verify_ssl=True, path_mappings=[])
            for kind in ('emby', 'silo')}
    return SimpleNamespace(general=SimpleNamespace(use_emby=False, use_silo=False), **(rows | overrides))


@pytest.fixture
def autocommit_session(tmp_path):
    from app.database import Base
    engine = create_engine(f'sqlite:///{tmp_path / "migration.db"}', isolation_level='AUTOCOMMIT')
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


def migration():
    path = Path(__file__).parents[2] / 'migrations/versions/c2e7a4d9f810_media_server_instances.py'
    spec = importlib.util.spec_from_file_location('media_destination_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('create_all', [False, True])
def test_migration_guarded_for_upgrade_and_create_all(tmp_path, create_all):
    from app.database import Base
    engine = create_engine(f'sqlite:///{tmp_path / "schema.db"}')
    if create_all:
        Base.metadata.create_all(engine)
    with engine.begin() as connection:
        migration().create_media_server_tables(connection)
        migration().create_media_server_tables(connection)
        assert {'media_server_instances', 'media_server_imports'} <= set(inspect(connection).get_table_names())
    engine.dispose()


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_disabled_incomplete_scalar_preserved_once_even_after_deletion(autocommit_session, kind):
    from app.database import TableMediaServerImports
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy(**{kind: SimpleNamespace(url='', apikey='000123', verify_ssl=False, path_mappings=[])})
    assert backfill_instances(autocommit_session, config)[kind]['created'] is True
    repo = MediaServerInstanceRepository(autocommit_session)
    row, = repo.list()
    assert (row.enabled, row.url, row.verify_ssl) == (0, '', 0)
    assert repo.get_decrypted_api_key(row.id) == '000123'
    assert autocommit_session.get(TableMediaServerImports, kind) is not None
    repo.delete(row.id)
    backfill_instances(autocommit_session, config)
    assert repo.list() == []


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_row_and_import_marker_are_atomic_and_retryable(autocommit_session, monkeypatch, kind):
    from app.database import TableMediaServerImports
    from media_servers import backfill
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy(**{kind: SimpleNamespace(url='http://legacy.example/base', apikey='key',
                                            verify_ssl=True, path_mappings=[])})
    original = backfill._record_import
    def fail(session, target):
        if target == kind:
            raise RuntimeError('sensitive synthetic data')
        original(session, target)
    monkeypatch.setattr(backfill, '_record_import', fail)
    result = backfill.backfill_instances(autocommit_session, config)
    assert result[kind] == {'created': False, 'error_code': 'migration_failed'}
    assert MediaServerInstanceRepository(autocommit_session).list(kind) == []
    assert autocommit_session.get(TableMediaServerImports, kind) is None
    monkeypatch.setattr(backfill, '_record_import', original)
    assert backfill.backfill_instances(autocommit_session, config)[kind]['created'] is True
    assert len(MediaServerInstanceRepository(autocommit_session).list(kind)) == 1


def test_empty_scalar_stamped_without_dummy_and_never_imports_later(autocommit_session):
    from app.database import TableMediaServerImports
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy()
    backfill_instances(autocommit_session, config)
    assert all(autocommit_session.get(TableMediaServerImports, kind) for kind in ('emby', 'silo'))
    config.emby.url = 'http://stale.example'
    backfill_instances(autocommit_session, config)
    assert MediaServerInstanceRepository(autocommit_session).list() == []


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_import_preserves_existing_rows_and_enabled_incomplete_draft(autocommit_session, kind):
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    from test_media_server_instances import payload
    repo = MediaServerInstanceRepository(autocommit_session)
    existing = repo.create(**payload(kind))
    config = legacy(**{kind: SimpleNamespace(url='', apikey='000123', verify_ssl=False, path_mappings=[])})
    setattr(config.general, 'use_' + kind, True)
    assert backfill_instances(autocommit_session, config)[kind]['created'] is True
    imported, = [row for row in repo.list(kind) if row.id != existing.id]
    assert imported.enabled == 1 and imported.url == '' and imported.verify_ssl == 0
    assert repo.get_decrypted_api_key(imported.id) == '000123'
    assert repo.get_decrypted_api_key(existing.id) == 'opaque-A'
    assert repo.snapshot(imported.id, config).configuration_error == 'invalid_url'
    backfill_instances(autocommit_session, config)
    assert len(repo.list(kind)) == 2


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_malformed_import_is_sanitized_and_does_not_activate_kind(autocommit_session, caplog, monkeypatch, kind):
    from media_servers.backfill import backfill_instances
    from media_servers import dispatcher
    from app import config as app_config, database as app_database
    config = legacy(**{kind: SimpleNamespace(url='http://private.example', apikey=['private-key'],
                                            verify_ssl=True, path_mappings=[])})
    setattr(config.general, 'use_' + kind, True)
    assert backfill_instances(autocommit_session, config)[kind]['error_code'] == 'migration_failed'
    config.general.secrets_encryption_key = 'synthetic-durable-key'
    monkeypatch.setattr(app_config, 'settings', config)
    monkeypatch.setattr(app_database, 'database', autocommit_session)
    monkeypatch.setattr(dispatcher, '_configuration', None)
    native = dispatcher.get_native_configuration()
    assert native.list() == ()
    from media_servers.repository import MediaServerInstanceRepository
    from test_media_server_instances import payload
    repo = MediaServerInstanceRepository(autocommit_session)
    created = repo.create(**payload(kind))
    native.publish(repo.snapshot(created.id, config))
    assert native.list() == ()
    assert 'private.example' not in caplog.text and 'private-key' not in caplog.text
