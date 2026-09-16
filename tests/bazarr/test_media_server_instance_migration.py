import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


def legacy(**overrides):
    rows = {kind: SimpleNamespace(url='', apikey='', verify_ssl=True, path_mappings=[])
            for kind in ('emby', 'silo')}
    rows['jellyfin'] = SimpleNamespace(url='', apikey='', verify_ssl=True, path_mappings=[],
                                       update_movie_library=False, update_series_library=False,
                                       movie_library_ids=[], series_library_ids=[],
                                       sports_library_ids=[], refresh_method='immediate')
    # Plex keeps the shipped ip/port defaults so an untouched install imports
    # nothing, which is what the scalar settings mean.
    rows['plex'] = SimpleNamespace(auth_method='apikey', ip='127.0.0.1', port=32400, ssl=False,
                                   apikey='', token='', server_url='', verify_ssl=False,
                                   path_mappings=[], update_movie_library=False,
                                   update_series_library=False, movie_library=[],
                                   series_library=[], sports_library=[])
    general = SimpleNamespace(use_emby=False, use_silo=False, use_jellyfin=False, use_plex=False)
    return SimpleNamespace(general=general, **(rows | overrides))


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


def test_an_empty_scalar_creates_nothing_but_still_stamps_the_kind(autocommit_session):
    """Nothing configured still spends the import, because the marker is also
    the dispatcher's gate: an unstamped kind has every destination dropped.

    A stale scalar therefore cannot resurrect a destination later either, which
    is the property this has always had.
    """
    from app.database import TableMediaServerImports
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy()
    backfill_instances(autocommit_session, config)
    assert all(autocommit_session.get(TableMediaServerImports, kind) for kind in ('emby', 'silo'))
    assert MediaServerInstanceRepository(autocommit_session).list() == []
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


def test_jellyfin_singleton_imports_its_toggles_libraries_and_refresh_method(autocommit_session):
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy(jellyfin=SimpleNamespace(
        url='http://jellyfin.example', apikey='jf-key', verify_ssl=False, path_mappings=[],
        update_movie_library=True, update_series_library=False,
        movie_library_ids=['lib-movies'], series_library_ids=['lib-shows'],
        sports_library_ids='lib-sports', refresh_method='async'))
    config.general.use_jellyfin = True
    assert backfill_instances(autocommit_session, config)['jellyfin']['created'] is True
    repo = MediaServerInstanceRepository(autocommit_session)
    row, = repo.list('jellyfin')
    values = repo.values(row)
    assert (values['url'], values['enabled'], values['verify_ssl']) == ('http://jellyfin.example', True, False)
    assert repo.get_decrypted_api_key(row.id) == 'jf-key'
    assert (values['refresh_movies'], values['refresh_episodes']) == (True, False)
    # A scalar that has held a bare string as often as a list becomes a list.
    assert values['options'] == {'movie_library_ids': ['lib-movies'],
                                 'series_library_ids': ['lib-shows'],
                                 'sports_library_ids': ['lib-sports'],
                                 'refresh_method': 'async'}
    # Jellyfin never needed path mappings and must not start needing them.
    assert values['path_mappings'] == []
    assert repo.snapshot(row.id, config).configuration_error is None


@pytest.mark.parametrize('auth_method,expected_url,expected_key', [
    ('apikey', 'https://plex.example:32400', 'plex-key'),
    ('oauth', 'http://plex.example/direct', 'plex-token')])
def test_plex_singleton_imports_whichever_endpoint_it_authenticates_against(
        autocommit_session, auth_method, expected_url, expected_key):
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy(plex=SimpleNamespace(
        auth_method=auth_method, ip='plex.example', port=32400, ssl=True, apikey='plex-key',
        token='plex-token', server_url='http://plex.example/direct', verify_ssl=False,
        path_mappings=[], update_movie_library=True, update_series_library=True,
        movie_library=['Films'], series_library='Shows', sports_library=[]))
    config.general.use_plex = True
    assert backfill_instances(autocommit_session, config)['plex']['created'] is True
    repo = MediaServerInstanceRepository(autocommit_session)
    row, = repo.list('plex')
    values = repo.values(row)
    assert values['url'] == expected_url
    assert repo.get_decrypted_api_key(row.id) == expected_key
    assert values['options'] == {'movie_libraries': ['Films'], 'series_libraries': ['Shows'],
                                 'sports_libraries': []}
    assert values['path_mappings'] == []


def test_an_untouched_plex_install_gets_no_destination(autocommit_session):
    """Plex ships an ip and a port, so a composed URL is not evidence of setup.

    Without this the shipped defaults would create a Plex destination for every
    user who has never opened the Plex settings. The credential is what counts.
    """
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = legacy()
    assert backfill_instances(autocommit_session, config)['plex'] == {'created': False}
    assert MediaServerInstanceRepository(autocommit_session).list('plex') == []


def live_box_settings():
    """The config shape a real upgrade lands on, from a live Plex install.

    Mirrors the scalar shape reported from a configured box, with a synthetic
    hostname: an OAuth account pointed at an explicit https URL on port 443
    while ip and port are still the shipped 127.0.0.1:32400, TLS verification
    off, library handles stored as strings, both per-type opt-ins on, and Emby
    and Silo already migrated so their sections are empty.
    """
    config = legacy(
        plex=SimpleNamespace(
            auth_method='oauth', ip='127.0.0.1', port=32400, ssl=False,
            apikey='', token='plex-account-token',
            server_url='https://plex.example.test:443',
            server_connections=['https://plex.example.test:443'],
            verify_ssl=False, update_movie_library=True, update_series_library=True,
            movie_library=['Movies'], series_library=['TV Shows'], sports_library=[],
            movie_library_ids=['1'], series_library_ids=['2'], sports_library_ids=[],
            set_movie_added=True, set_episode_added=True, instance_id=''),
        jellyfin=SimpleNamespace(
            url='http://jellyfin.example.test:8096', apikey='jellyfin-api-key',
            verify_ssl=True, path_mappings=[], update_movie_library=True,
            update_series_library=True, movie_library_ids=['a' * 32],
            series_library_ids=['b' * 32], sports_library_ids=[],
            refresh_method='immediate'))
    config.general.use_plex = True
    config.general.use_jellyfin = True
    return config


def test_a_live_plex_and_jellyfin_install_imports_the_way_the_box_is_configured(autocommit_session):
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = live_box_settings()
    results = backfill_instances(autocommit_session, config)
    assert results['plex']['created'] is True and results['jellyfin']['created'] is True
    repo = MediaServerInstanceRepository(autocommit_session)

    plex, = repo.list('plex')
    values = repo.values(plex)
    # The OAuth endpoint, not the legacy ip and port that are still sitting
    # there at their shipped defaults.
    assert values['url'] == 'https://plex.example.test:443'
    assert values['verify_ssl'] is False and values['enabled'] is True
    assert repo.get_decrypted_api_key(plex.id) == 'plex-account-token'
    assert (values['refresh_movies'], values['refresh_episodes']) == (True, True)
    # Plex refreshes a section by its title, which is what the account panel
    # stored alongside the ids, so the titles are what the row carries.
    assert values['options'] == {'movie_libraries': ['Movies'],
                                 'series_libraries': ['TV Shows'],
                                 'sports_libraries': []}
    assert config.plex.instance_id == plex.id

    jellyfin, = repo.list('jellyfin')
    values = repo.values(jellyfin)
    assert values['url'] == 'http://jellyfin.example.test:8096'
    assert values['verify_ssl'] is True and values['enabled'] is True
    assert repo.get_decrypted_api_key(jellyfin.id) == 'jellyfin-api-key'
    assert (values['refresh_movies'], values['refresh_episodes']) == (True, True)
    assert values['options'] == {'movie_library_ids': ['a' * 32],
                                 'series_library_ids': ['b' * 32],
                                 'sports_library_ids': [],
                                 'refresh_method': 'immediate'}
    for row in (plex, jellyfin):
        assert repo.snapshot(row.id, config).configuration_error is None

    # Both kinds are stamped, so neither is blocked and both rows reach the
    # dispatcher. An unstamped kind is dropped on publish and its status reads
    # migration_failed forever, which is what a Plex row without one did.
    from app.database import TableMediaServerImports
    from media_servers.dispatcher import NativeConfiguration
    from media_servers.instances import VALID_KINDS
    stamped = [kind for kind in VALID_KINDS
               if autocommit_session.get(TableMediaServerImports, kind) is not None]
    assert set(stamped) == set(VALID_KINDS)
    configuration = NativeConfiguration(config, snapshots=repo.snapshots(config, kinds=stamped),
                                        blocked_kinds=set(VALID_KINDS) - set(stamped))
    assert configuration.blocked_kinds == frozenset()
    assert configuration.read(plex.id)[1].kind == 'plex'
    assert configuration.read(jellyfin.id)[1].kind == 'jellyfin'


def test_already_migrated_emby_and_silo_rows_survive_the_plex_and_jellyfin_import(autocommit_session):
    """Their sections are empty because the earlier import already moved them."""
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    from test_media_server_instances import payload
    repo = MediaServerInstanceRepository(autocommit_session)
    existing = {kind: repo.create(**payload(kind)) for kind in ('emby', 'silo')}
    config = live_box_settings()
    backfill_instances(autocommit_session, config)
    for kind, row in existing.items():
        assert [item.id for item in repo.list(kind)] == [row.id]
        assert repo.get_decrypted_api_key(row.id) == 'opaque-A'


def test_the_live_plex_row_is_reconciled_rather_than_duplicated_on_every_startup(autocommit_session):
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = live_box_settings()
    backfill_instances(autocommit_session, config)
    repo = MediaServerInstanceRepository(autocommit_session)
    row, = repo.list('plex')
    # The user edits the libraries on the instance; a later startup must not
    # put the scalars back over them.
    repo.update(row.id, options={'movie_libraries': ['Films 4K'], 'series_libraries': [],
                                 'sports_libraries': []}, refresh_episodes=False)
    backfill_instances(autocommit_session, config)
    values = repo.values(repo.get(row.id))
    assert [item.id for item in repo.list('plex')] == [row.id]
    assert values['options']['movie_libraries'] == ['Films 4K']
    assert values['refresh_episodes'] is False
    assert values['url'] == 'https://plex.example.test:443'


def test_the_runtime_configuration_sees_every_kind_the_import_stamped(autocommit_session, monkeypatch):
    """Built the way the process builds it, during the import, not after it.

    The shared configuration is constructed once and cached for the life of the
    process, from whichever markers exist at that instant. Anything that builds
    it mid-import freezes a partial set and blocks the kinds that had not been
    stamped yet, for the whole first process after an upgrade. A test that
    hand-builds one afterwards cannot see that, so this one lets the runtime
    build it and only then asks what is visible.
    """
    from app import config as app_config, database as app_database
    from media_servers import dispatcher
    from media_servers.backfill import backfill_instances
    from media_servers.instances import VALID_KINDS
    from media_servers.repository import MediaServerInstanceRepository
    from test_media_server_instances import payload

    config = live_box_settings()
    config.general.secrets_encryption_key = 'synthetic-durable-key'
    for kind in ('emby', 'silo'):
        setattr(config.general, 'use_' + kind, True)
    monkeypatch.setattr(app_config, 'settings', config)
    monkeypatch.setattr(app_config, 'write_config', lambda: None)
    monkeypatch.setattr(app_database, 'database', autocommit_session)
    monkeypatch.setattr(dispatcher, '_configuration', None)
    repo = MediaServerInstanceRepository(autocommit_session)
    existing = {kind: repo.create(**payload(kind)) for kind in ('emby', 'silo')}

    backfill_instances(autocommit_session, config)

    native = dispatcher.get_native_configuration()
    assert native.blocked_kinds == frozenset()
    rows = {row.kind: row for row in repo.list()}
    assert set(rows) == set(VALID_KINDS)
    for kind in VALID_KINDS:
        assert native.read(rows[kind].id)[1].kind == kind
    for kind, row in existing.items():
        assert native.read(row.id)[1].id == row.id


def test_a_plex_reconcile_that_could_not_reach_the_database_leaves_the_kind_blocked(
        autocommit_session, monkeypatch):
    from app.database import TableMediaServerImports
    from media_servers import plex_account
    from media_servers.backfill import backfill_instances
    from media_servers.instances import VALID_KINDS

    def fail(*_args, **_kwargs):
        raise RuntimeError('synthetic database failure')

    monkeypatch.setattr(plex_account, 'apply_plex_account', fail)
    config = live_box_settings()
    results = backfill_instances(autocommit_session, config)
    assert results['plex'] == {'created': False, 'error_code': 'migration_failed'}
    blocked = [kind for kind in VALID_KINDS
               if autocommit_session.get(TableMediaServerImports, kind) is None]
    assert blocked == ['plex']


def test_the_plex_result_says_created_only_when_a_row_was_created(autocommit_session):
    from media_servers.backfill import backfill_instances
    config = live_box_settings()
    assert backfill_instances(autocommit_session, config)['plex'] == {'created': True}
    # The next startup reconciles the same row, and has created nothing.
    assert backfill_instances(autocommit_session, config)['plex'] == {'created': False}
