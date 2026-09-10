import pytest
from flask import Flask

from test_media_server_instances import payload, settings
from test_media_server_http import http_fixture as http_fixture


@pytest.fixture
def instance_api(schema_session, monkeypatch):
    from api import api_bp
    from app.config import settings as app_settings
    from api import system
    media_server_instances = getattr(system, "media_server_instances", None)
    from media_servers import dispatcher
    monkeypatch.setitem(app_settings.auth, 'apikey', 'synthetic-bazarr-key')
    if media_server_instances is not None:
        monkeypatch.setattr(media_server_instances, 'database', schema_session)
    configuration = dispatcher.NativeConfiguration(settings())
    monkeypatch.setattr(dispatcher, '_configuration', configuration)
    monkeypatch.setattr(dispatcher, '_dispatcher', dispatcher.RefreshDispatcher(configuration))
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


HEADERS = {'X-API-KEY': 'synthetic-bazarr-key'}
ROOT = '/api/system/media-server-instances'


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_authenticated_crud_uuid_safe_key_and_status(instance_api, kind):
    assert instance_api.get(ROOT).status_code == 401
    first = instance_api.post(ROOT, json=payload(kind), headers=HEADERS)
    second = instance_api.post(ROOT, json=payload(kind, 'B'), headers=HEADERS)
    assert first.status_code == second.status_code == 201
    assert first.json['id'] != second.json['id']
    path = ROOT + '/' + first.json['id']
    response = instance_api.patch(path, json={'name': 'Renamed', 'api_key': ''}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json['id'] == first.json['id']
    assert response.json['api_key_set'] is True
    listed = instance_api.get(ROOT + '?kind=' + kind, headers=HEADERS)
    assert len(listed.json['data']) == 2
    assert all('api_key' not in row and 'apikey' not in row for row in listed.json['data'])
    assert instance_api.get(path + '/status', headers=HEADERS).json['pending'] == 0
    assert instance_api.post(path + '/retry-pending', headers=HEADERS).json == {'queued': 0}
    assert instance_api.delete(path, headers=HEADERS).status_code == 204
    assert instance_api.get(path, headers=HEADERS).status_code == 404
    assert instance_api.get(path + '/status', headers=HEADERS).status_code == 404


@pytest.mark.parametrize('patch', [{'id': '4'}, {'kind': 'silo'}, {'enabled': 1}, {'api_key': 123},
                                  {'verify_ssl': 'false'}, {'url': 'http://user:secret@host'},
                                  {'clear_api_key': True, 'api_key': 'new'}, {'unknown': 'x'}])
def test_invalid_edit_never_mutates(instance_api, patch):
    first = instance_api.post(ROOT, json=payload('emby'), headers=HEADERS).json
    path = ROOT + '/' + first['id']
    assert instance_api.patch(path, json=patch, headers=HEADERS).status_code == 400
    assert instance_api.get(path, headers=HEADERS).json == first


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_default_disabled_create_and_explicit_key_clear(instance_api, schema_session, kind):
    from media_servers.repository import MediaServerInstanceRepository
    created = instance_api.post(ROOT, json={'kind': kind, 'name': 'Draft', 'url': 'http://[::1]:8096/base',
                                           'api_key': '000123'}, headers=HEADERS)
    assert created.status_code == 201
    first = created.json
    assert first['enabled'] is False and first['verify_ssl'] is True and first['path_mappings'] == []
    path = ROOT + '/' + first['id']
    assert instance_api.patch(path, json={'api_key': ''}, headers=HEADERS).json['api_key_set'] is True
    assert MediaServerInstanceRepository(schema_session).get_decrypted_api_key(first['id']) == '000123'
    assert instance_api.patch(path, json={'clear_api_key': True}, headers=HEADERS).json['api_key_set'] is False
    assert instance_api.patch(path, json={'enabled': True}, headers=HEADERS).status_code == 400


@pytest.mark.parametrize('body', [None, [], {}, {'kind': 'emby'}, {'kind': 'emby', 'name': 'A'},
                                 payload('emby', api_key=''), payload('silo', path_mappings=[]),
                                 payload('emby', name=' '), payload('silo', path_mappings=[
                                     {'local_path': '/movies', 'remote_path': '/media', 'library_id': '0'}])])
def test_invalid_create_fails_without_rows(instance_api, body):
    response = instance_api.post(ROOT, json=body, headers=HEADERS)
    assert response.status_code == 400
    assert instance_api.get(ROOT, headers=HEADERS).json == {'data': []}


@pytest.mark.parametrize('method,suffix', [('get', ''), ('patch', ''), ('delete', ''),
                                         ('post', '/test-connection'), ('post', '/libraries'),
                                         ('get', '/status'), ('post', '/retry-pending')])
def test_all_item_operations_require_authentication(instance_api, method, suffix):
    path = ROOT + '/01f0e1bc-68fe-4013-84b1-d4becad3e8fb' + suffix
    assert getattr(instance_api, method)(path).status_code == 401
    assert getattr(instance_api, method)(path, json={}, headers=HEADERS).status_code == 404


def test_invalid_filters_and_numerical_ids_are_not_coerced(instance_api):
    assert instance_api.get(ROOT + '?kind=jellyfin', headers=HEADERS).status_code == 400
    assert instance_api.get(ROOT + '/1', headers=HEADERS).status_code == 404


@pytest.mark.parametrize('kind', ['emby', 'silo'])
@pytest.mark.parametrize('override', [False, True])
def test_saved_probe_resolves_only_its_own_key_and_never_saves(instance_api, http_fixture, kind, override):
    from test_silo_client import HEALTH, LIBRARIES
    def replies():
        return [(200, {'ServerName': 'Test', 'Version': '4.9.5.0'}, {})] if kind == 'emby' else [
            (200, HEALTH, {}), (200, LIBRARIES, {})]
    targets = [http_fixture(replies()) for _ in range(2)]
    if override:
        targets.append(http_fixture(replies()))
    for index, name in enumerate(('A', 'B')):
        created = instance_api.post(ROOT, json=payload(kind, name, url=targets[index][0] + '/saved'),
                                    headers=HEADERS).json
        path = ROOT + '/' + created['id']
        body = {'url': targets[2][0] + '/override', 'api_key': 'unsaved', 'verify_ssl': False} if override and index == 0 else {}
        response = instance_api.post(path + '/test-connection?api_key=wrong', json=body, headers=HEADERS)
        assert response.status_code == 200 and response.json['success'] is True
        target = targets[2] if override and index == 0 else targets[index]
        expected_key = 'unsaved' if override and index == 0 else 'opaque-' + name
        expected_header = expected_key if kind == 'emby' else 'Bearer ' + expected_key
        assert target[1][0]['headers']['X-Emby-Token' if kind == 'emby' else 'Authorization'] == expected_header
        assert all(row['method'] == 'GET' for row in target[1])
        assert instance_api.get(path, headers=HEADERS).json == created


@pytest.mark.parametrize('body', [{'verify_ssl': 'false'}, {'api_key': 1}, {'clear_api_key': True},
                                 {'unknown': 'x'}, {'url': 'ftp://bad'}, {'api_key': 'x', 'clear_api_key': True}])
def test_invalid_saved_probe_never_requests(instance_api, http_fixture, body):
    base, records = http_fixture([])
    first = instance_api.post(ROOT, json=payload('emby', url=base), headers=HEADERS).json
    response = instance_api.post(ROOT + '/' + first['id'] + '/test-connection', json=body, headers=HEADERS)
    assert response.status_code == 400
    assert records == []


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_unknown_new_probe_fields_fail_before_network(instance_api, http_fixture, kind):
    base, records = http_fixture([])
    response = instance_api.post('/api/' + kind + '/test-connection', headers=HEADERS,
                                 json={'url': base, 'apikey': 'synthetic', 'unexpected': 'field'})
    assert response.status_code == 400
    assert records == []


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_saved_probe_unknown_body_type_fails(instance_api, kind):
    first = instance_api.post(ROOT, json=payload(kind), headers=HEADERS).json
    assert instance_api.post(ROOT + '/' + first['id'] + '/test-connection', json=[], headers=HEADERS).status_code == 400


def test_silo_saved_library_probes_keep_destination_credentials_and_library_ids(instance_api, http_fixture):
    targets = [http_fixture([(200, [{'id': index, 'name': name, 'type': 'movies',
                                    'paths': [f'/media/{name}'], 'enabled': True}], {})])
               for index, name in ((7, 'A'), (8, 'B'))]
    for (base, records), name, library in zip(targets, ('A', 'B'), ('7', '8')):
        created = instance_api.post(ROOT, json=payload('silo', name, url=base), headers=HEADERS).json
        response = instance_api.post(ROOT + '/' + created['id'] + '/libraries', json={}, headers=HEADERS)
        assert response.status_code == 200
        assert response.json['data'][0]['id'] == library
        assert response.json['error_code'] is None
        assert records[0]['headers']['Authorization'] == 'Bearer opaque-' + name
        assert [(row['method'], row['path']) for row in records] == [('GET', '/api/v1/libraries')]


@pytest.mark.parametrize('library_id', [0, 7, '', '0', '0000', '-1', '1.0', 'one', ' 7'])
def test_silo_mapping_ids_are_positive_decimal_strings(instance_api, library_id):
    data = payload('silo', path_mappings=[{'local_path': '/movies', 'remote_path': '/media', 'library_id': library_id}])
    assert instance_api.post(ROOT, json=data, headers=HEADERS).status_code == 400
    assert instance_api.get(ROOT, headers=HEADERS).json == {'data': []}


@pytest.mark.parametrize('broken', ['unreadable_key', 'legacy_missing_url'])
def test_a_broken_destination_can_still_be_turned_off(instance_api, schema_session, broken):
    """The card's disable action submits ``{'enabled': False}`` and nothing else.

    A saved key that no longer decrypts (a database restored without its master
    key) and a legacy import that never had a URL are both destinations whose
    retained values cannot pass validation. Refusing the edit leaves them
    enabled and accumulating failed refresh targets with no way out short of
    deleting the instance, so turning one off must not depend on reading or
    re-validating whatever it retains.
    """
    from media_servers.repository import MediaServerInstanceRepository
    from secret_store import encrypt_secret
    created = instance_api.post(ROOT, json=payload('emby'), headers=HEADERS).json
    path = ROOT + '/' + created['id']
    repo = MediaServerInstanceRepository(schema_session)
    row = repo.get(created['id'])
    if broken == 'unreadable_key':
        row.api_key = encrypt_secret('old-key', master_key='different-test-master')
    else:
        row.url = ''
    retained_key, retained_url = row.api_key, row.url
    schema_session.flush()

    response = instance_api.patch(path, json={'enabled': False}, headers=HEADERS)
    assert response.status_code == 200, response.json
    assert response.json['enabled'] is False

    # Nothing the destination retains was rewritten, so restoring the master key
    # (or filling the URL back in) still recovers it.
    saved = repo.get(created['id'])
    assert (saved.api_key, saved.url) == (retained_key, retained_url)
    assert bool(saved.enabled) is False
    # Turning it back on has no usable connection and must still be refused.
    assert instance_api.patch(path, json={'enabled': True}, headers=HEADERS).status_code == 400
