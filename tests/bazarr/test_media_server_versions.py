from types import SimpleNamespace

import pytest

from test_media_server_instances import payload


def settings(**overrides):
    values = dict(use_emby=True, use_silo=True, use_jellyfin=True, use_plex=True)
    values.update(overrides)
    return SimpleNamespace(general=SimpleNamespace(**values))


@pytest.fixture(autouse=True)
def clean_versions():
    from media_servers import versions
    versions.reset()
    yield
    versions.reset()


@pytest.fixture
def repo(schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    return MediaServerInstanceRepository(schema_session)


def test_unconfigured_install_reports_no_destination(schema_session, repo):
    from media_servers import versions
    assert versions.statuses(schema_session, settings()) == []


def test_disabled_and_switched_off_destinations_are_left_out(schema_session, repo):
    from media_servers import versions
    repo.create(**payload('emby', 'Off', enabled=False))
    repo.create(**payload('silo', 'On'))
    named = {entry['name'] for entry in versions.statuses(schema_session, settings(), refresh=False)}
    assert named == {'On'}
    # The product's master toggle is the other half of "in use", exactly as the
    # dispatcher reads it.
    assert versions.statuses(schema_session, settings(use_silo=False), refresh=False) == []


def test_unprobed_destination_says_checking_rather_than_claiming_a_version(schema_session, repo):
    from media_servers import versions
    row = repo.create(**payload('emby'))
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert entry == {'id': row.id, 'kind': 'emby', 'name': 'A', 'state': 'checking', 'version': ''}


def test_recorded_probe_is_reported_and_a_failure_carries_no_version(schema_session, repo):
    from media_servers import versions
    row = repo.create(**payload('emby'))
    versions.record(row.id, row.revision, {'success': True, 'version': '4.8.11.0'})
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('connected', '4.8.11.0')

    versions.record(row.id, row.revision, {'success': False, 'error_code': 'connection_error'})
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('unreachable', '')


def test_connected_server_without_a_version_is_not_faked(schema_session, repo):
    """Silo's health endpoint reports no version at all, and never will here."""
    from media_servers import versions
    row = repo.create(**payload('silo'))
    versions.record(row.id, row.revision, {'success': True, 'server_name': 'Native Silo'})
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('connected', '')


def test_two_destinations_of_one_kind_report_separately(schema_session, repo):
    from media_servers import versions
    first = repo.create(**payload('emby', 'Basement'))
    second = repo.create(**payload('emby', 'Attic'))
    versions.record(first.id, first.revision, {'success': True, 'version': '4.8.11.0'})
    versions.record(second.id, second.revision, {'success': False})
    entries = {entry['name']: entry for entry in versions.statuses(schema_session, settings(), refresh=False)}
    assert set(entries) == {'Basement', 'Attic'}
    assert entries['Basement']['version'] == '4.8.11.0'
    assert entries['Attic']['state'] == 'unreachable'
    assert entries['Basement']['id'] != entries['Attic']['id']


def test_editing_the_connection_retires_the_previous_version(schema_session, repo):
    from media_servers import versions
    row = repo.create(**payload('emby'))
    versions.record(row.id, row.revision, {'success': True, 'version': '4.8.11.0'})
    repo.update(row.id, url='http://moved.example')
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('checking', '')


def test_a_stale_entry_is_refreshed_in_the_background(schema_session, repo, monkeypatch):
    from media_servers import versions
    row = repo.create(**payload('emby'))
    versions.record(row.id, row.revision, {'success': True, 'version': '4.8.11.0'})
    monkeypatch.setattr(versions, 'CACHE_SECONDS', 0)
    monkeypatch.setattr(versions, '_probe', lambda snapshot: {'success': True, 'version': '4.9.0.0'})
    # Run the pool inline so the test observes the refresh without waiting.
    monkeypatch.setattr(versions._executor, 'submit',
                        lambda fn, *args: fn(*args))

    entry, = versions.statuses(schema_session, settings())
    # The stale value is still what the request answers with; the refresh lands
    # behind it rather than holding the response open.
    assert entry['version'] in ('4.8.11.0', '4.9.0.0')
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert entry['version'] == '4.9.0.0'


def test_a_failing_probe_never_escapes_the_worker(schema_session, repo, monkeypatch):
    from media_servers import versions
    repo.create(**payload('emby'))

    def explode(*_args, **_kwargs):
        raise RuntimeError('the server hung up')

    monkeypatch.setattr('media_servers.service._probe', explode)
    monkeypatch.setattr(versions._executor, 'submit', lambda fn, *args: fn(*args))
    entries = versions.statuses(schema_session, settings())
    assert entries[0]['state'] in ('checking', 'unreachable')
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('unreachable', '')


def test_a_saved_connection_test_records_the_version_it_just_saw(schema_session, repo, monkeypatch):
    from media_servers import service, versions
    row = repo.create(**payload('emby'))
    monkeypatch.setattr(service, '_probe',
                        lambda *_args: {'success': True, 'server_name': 'Attic', 'version': '4.8.11.0'})
    result, status = service.probe_instance(schema_session, row.id, {})
    assert (status, result['version']) == (200, '4.8.11.0')
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('connected', '4.8.11.0')


def test_a_test_of_unsaved_edits_is_not_recorded_against_the_saved_instance(schema_session, repo, monkeypatch):
    from media_servers import service, versions
    row = repo.create(**payload('emby'))
    monkeypatch.setattr(service, '_probe',
                        lambda *_args: {'success': True, 'version': '9.9.9.9'})
    _result, status = service.probe_instance(schema_session, row.id,
                                             {'url': 'http://elsewhere.example', 'api_key': 'other'})
    assert status == 200
    entry, = versions.statuses(schema_session, settings(), refresh=False)
    assert (entry['state'], entry['version']) == ('checking', '')


@pytest.fixture
def status_api(schema_session, monkeypatch):
    from api import api_bp
    from app.config import settings as app_settings
    from flask import Flask
    monkeypatch.setitem(app_settings.auth, 'apikey', 'synthetic-bazarr-key')
    monkeypatch.setattr('app.database.database', schema_session)
    for kind in ('emby', 'silo', 'jellyfin', 'plex'):
        monkeypatch.setitem(app_settings.general, 'use_' + kind, True)
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


def test_status_endpoint_reports_media_servers_beside_the_existing_fields(status_api, repo):
    from media_servers import versions
    row = repo.create(**payload('emby', 'Attic'))
    versions.record(row.id, row.revision, {'success': True, 'version': '4.8.11.0'})
    data = status_api.get('/api/system/status', headers={'X-API-KEY': 'synthetic-bazarr-key'}).json['data']
    # Adding a field is safe; the fields other readers already use are not
    # renamed or dropped.
    for field in ('bazarr_version', 'package_version', 'sonarr_version', 'radarr_version',
                  'sportarr_version', 'operating_system', 'python_version', 'database_engine',
                  'database_migration', 'bazarr_directory', 'bazarr_config_directory',
                  'start_time', 'timezone'):
        assert field in data
    assert data['media_servers'] == [{'id': row.id, 'kind': 'emby', 'name': 'Attic',
                                      'state': 'connected', 'version': '4.8.11.0'}]


def test_status_endpoint_survives_a_broken_destination_layer(status_api, monkeypatch):
    monkeypatch.setattr('media_servers.versions.statuses',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('no database')))
    data = status_api.get('/api/system/status', headers={'X-API-KEY': 'synthetic-bazarr-key'}).json['data']
    assert data['media_servers'] == []
    assert data['bazarr_version']
