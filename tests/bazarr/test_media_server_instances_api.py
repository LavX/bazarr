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


def test_the_signed_in_plex_accounts_row_is_removed_by_signing_out_not_by_delete(instance_api, monkeypatch):
    """The startup import rebuilds the account's row, so a delete would come back."""
    from app.config import settings as app_settings
    created = instance_api.post(ROOT, json=payload('plex', path_mappings=[]), headers=HEADERS).json
    path = ROOT + '/' + created['id']
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth', 'token': 'plex-token',
                       'server_url': 'https://plex.example:32400'}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    listed = instance_api.get(ROOT + '?kind=plex', headers=HEADERS).json['data']
    assert [row['account_owned'] for row in listed] == [True]
    response = instance_api.delete(path, headers=HEADERS)
    assert response.status_code == 409 and response.json == {'error_code': 'account_owned'}
    assert instance_api.get(path, headers=HEADERS).status_code == 200

    # Disconnecting from Plex clears the credential, and the row deletes as any other.
    monkeypatch.setattr(app_settings.plex, 'token', '')
    assert instance_api.get(path, headers=HEADERS).json['account_owned'] is False
    assert instance_api.delete(path, headers=HEADERS).status_code == 204


def test_a_name_only_save_of_the_account_row_never_holds_the_account_lock_for_the_ask(
        instance_api, schema_session, monkeypatch):
    """The snapshot an instance save publishes consults plex.tv for the
    account's row, and the save holds the account, config, dispatcher and
    database write boundaries while it waits: a name-only PATCH could block
    an unrelated account transition for the whole ask. The save publishes the
    row held instead, the way the startup does, and the dispatcher's read
    path asks plex.tv outside every one of those boundaries and releases the
    row once the answer says the credential may be sent."""
    import threading

    from app.config import settings as app_settings
    from media_servers import plex_account, service
    from media_servers.repository import MediaServerInstanceRepository

    repo = MediaServerInstanceRepository(schema_session)
    row = repo.create(kind='plex', name='Plex', url='https://plex.example:32400',
                      api_key='plex-token', enabled=True, verify_ssl=True, path_mappings=[])
    for key, value in {'instance_id': row.id, 'auth_method': 'oauth', 'token': 'plex-token',
                       'server_machine_id': 'machine-abc',
                       'server_url': 'https://plex.example:32400'}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    asked = []
    lock_available = []

    def blocked_ask(*_args, **_kwargs):
        # Wherever this head asks plex.tv inside the save, a second thread
        # tries the account lock while the ask waits: at the unfixed head the
        # save holds it, and at the fixed head the ask never runs here.
        asked.append(True)

        def take_the_account_lock():
            lock_available.append(plex_account.ACCOUNT_LOCK.acquire(timeout=5))
            if lock_available[0]:
                plex_account.ACCOUNT_LOCK.release()

        watcher = threading.Thread(target=take_the_account_lock)
        watcher.start()
        watcher.join(timeout=8)
        return 'https://plex.example:32400'

    monkeypatch.setattr(plex_account, 'account_row_uri', blocked_ask)

    saved = service.update_instance(schema_session, row.id, {'name': 'Renamed'})

    if asked:
        assert lock_available == [True], \
            'the name-only save held the account lock while plex.tv was asked'
    assert asked == [], 'the save itself asked plex.tv nowhere'
    assert saved[1] == 200 and saved[0]['name'] == 'Renamed', 'the save did not land'
    assert repo.get(row.id).name == 'Renamed'


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
    assert instance_api.get(ROOT + '?kind=roku', headers=HEADERS).status_code == 400
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


@pytest.mark.parametrize('kind', ['emby', 'jellyfin', 'plex', 'silo'])
def test_unsaved_connection_probes_take_the_kind_as_a_value(instance_api, monkeypatch, kind):
    """One route for every kind, so adding a destination is not a per-kind path."""
    from media_servers import service
    seen = []
    monkeypatch.setattr(service, '_probe', lambda *args: seen.append(args) or {'success': True})
    body = {'kind': kind, 'url': 'http://server.example', 'apikey': 'key', 'verify_ssl': False}
    assert instance_api.post(ROOT + '/probe', json=body).status_code == 401
    response = instance_api.post(ROOT + '/probe', json=body, headers=HEADERS)
    assert response.status_code == 200 and response.json == {'success': True}
    assert seen == [(kind, 'http://server.example', 'key', False, False)]


@pytest.mark.parametrize('body,code', [
    ({'kind': 'roku', 'url': 'http://a.example', 'apikey': 'k'}, 'invalid_kind'),
    ({'kind': 'emby', 'url': 'http://a.example'}, 'missing_credentials'),
    ({'kind': 'emby', 'url': 'not-a-url', 'apikey': 'k'}, 'invalid_url'),
    ({'kind': 'emby', 'url': 'http://a.example', 'apikey': 'k', 'extra': 1}, 'invalid_settings'),
])
def test_unsaved_probe_refuses_malformed_settings(instance_api, body, code):
    response = instance_api.post(ROOT + '/probe', json=body, headers=HEADERS)
    assert response.status_code == 400
    assert response.json == {'success': False, 'error_code': code}


def test_unsaved_library_probe_is_refused_for_the_kind_that_has_none(instance_api):
    """Emby scopes its libraries through path mappings and lists none here."""
    body = {'kind': 'emby', 'url': 'http://emby.example', 'apikey': 'key'}
    response = instance_api.post(ROOT + '/probe-libraries', json=body, headers=HEADERS)
    assert response.status_code == 400
    assert response.json == {'data': [], 'error_code': 'invalid_kind'}


def test_refresh_libraries_is_authenticated_and_reports_what_it_asked_for(instance_api, monkeypatch):
    from media_servers import libraries
    from media_servers.http import MediaServerError
    created = instance_api.post(ROOT, json=payload('emby'), headers=HEADERS)
    path = ROOT + '/' + created.json['id'] + '/refresh-libraries'
    assert instance_api.post(path).status_code == 401
    assert instance_api.post(ROOT + '/1/refresh-libraries', headers=HEADERS).status_code == 404

    monkeypatch.setattr(libraries, 'refresh_libraries', lambda instance_id: {'requested': 4, 'failed': 1})
    response = instance_api.post(path, headers=HEADERS)
    assert response.status_code == 200 and response.json == {'requested': 4, 'failed': 1}

    def refuse(_instance_id):
        raise MediaServerError('library_missing')

    monkeypatch.setattr(libraries, 'refresh_libraries', refuse)
    response = instance_api.post(path, headers=HEADERS)
    assert response.status_code == 400
    assert response.json == {'requested': 0, 'error_code': 'library_missing'}


def test_an_unexpected_refresh_failure_is_logged_and_not_called_a_connection_error(
        instance_api, monkeypatch, caplog):
    """A fault in Bazarr answered connection_error and logged nothing, so the
    user checked a connection that was fine and the log had no trace of it."""
    import logging
    from media_servers import libraries
    created = instance_api.post(ROOT, json=payload('emby'), headers=HEADERS)
    path = ROOT + '/' + created.json['id'] + '/refresh-libraries'

    def broken(_instance_id):
        raise RuntimeError('synthetic fault')

    monkeypatch.setattr(libraries, 'refresh_libraries', broken)
    with caplog.at_level(logging.ERROR):
        response = instance_api.post(path, headers=HEADERS)
    assert response.status_code == 400
    assert response.json == {'requested': 0, 'error_code': 'internal_error'}
    record, = [record for record in caplog.records
               if record.exc_info and record.exc_info[0] is RuntimeError]
    assert record.levelno == logging.ERROR


@pytest.mark.parametrize('code, answered, logged', [
    ('timeout', 'timeout', False),
    # What the HTTP client raises for a 5xx.
    ('server_error', 'server_error', False),
    # A database error carries a string code of its own, and is still a fault.
    ('e3q8', 'internal_error', True),
], ids=['refusal', 'server error', 'other code'])
def test_a_refusal_of_another_class_keeps_its_code(instance_api, monkeypatch, caplog,
                                                   code, answered, logged):
    """A MediaServerError from another module generation is not the class this
    endpoint catches. It carries the same code, yet was answered internal_error
    and logged as a fault in Bazarr."""
    import logging
    from media_servers import libraries

    class Refusal(Exception):
        pass

    Refusal.code = code
    created = instance_api.post(ROOT, json=payload('emby'), headers=HEADERS)
    path = ROOT + '/' + created.json['id'] + '/refresh-libraries'

    def refused(_instance_id):
        raise Refusal('synthetic')

    monkeypatch.setattr(libraries, 'refresh_libraries', refused)
    with caplog.at_level(logging.ERROR):
        response = instance_api.post(path, headers=HEADERS)
    assert response.status_code == 400
    assert response.json == {'requested': 0, 'error_code': answered}
    faults = [record for record in caplog.records
              if record.exc_info and record.exc_info[0] is Refusal]
    assert len(faults) == (1 if logged else 0)


# ------------------------------- the stored key never follows a new address

REENTER_KEY = 'Re-enter the key to use a different address'


@pytest.mark.parametrize('suffix', ['/test-connection', '/libraries'])
def test_a_saved_probe_refuses_a_moved_address_without_the_key(instance_api, http_fixture, suffix):
    stored, records = http_fixture([])
    moved, moved_records = http_fixture([])
    created = instance_api.post(ROOT, json=payload('emby', url=stored), headers=HEADERS).json

    response = instance_api.post(ROOT + '/' + created['id'] + suffix,
                                 json={'url': moved}, headers=HEADERS)

    assert response.status_code == 400
    assert response.json['error_code'] == 'reenter_key'
    assert response.json['message'] == REENTER_KEY
    assert records == [] and moved_records == [], 'the stored key never reached the new address'


@pytest.mark.parametrize('suffix', ['/test-connection', '/libraries'])
def test_a_saved_probe_refuses_turning_verification_off_without_the_key(
        instance_api, http_fixture, suffix):
    stored, records = http_fixture([])
    created = instance_api.post(ROOT, json=payload('emby', url=stored), headers=HEADERS).json

    response = instance_api.post(ROOT + '/' + created['id'] + suffix,
                                 json={'verify_ssl': False}, headers=HEADERS)

    assert response.status_code == 400
    assert response.json['error_code'] == 'reenter_key'
    assert response.json['message'] == REENTER_KEY
    assert records == []


def test_a_saved_probe_allows_turning_verification_back_on(instance_api, http_fixture):
    """Turning verification back on only hardens the connection, so the stored
    key may follow it without being typed again."""
    stored, records = http_fixture([(200, {'ServerName': 'Test', 'Version': '4.9.5.0'}, {})])
    created = instance_api.post(ROOT, json=payload('emby', url=stored, verify_ssl=False),
                                headers=HEADERS).json

    response = instance_api.post(ROOT + '/' + created['id'] + '/test-connection',
                                 json={'verify_ssl': True}, headers=HEADERS)

    assert response.status_code == 200, response.json
    assert records, 'the stored key reached the stored address over the hardened connection'


def test_a_saved_probe_with_the_key_typed_again_goes_to_the_new_address(instance_api, http_fixture):
    stored, _ = http_fixture([])
    moved, moved_records = http_fixture([(200, {'ServerName': 'Test', 'Version': '4.9.5.0'}, {})])
    created = instance_api.post(ROOT, json=payload('emby', url=stored), headers=HEADERS).json

    response = instance_api.post(ROOT + '/' + created['id'] + '/test-connection',
                                 json={'url': moved, 'api_key': 'again'}, headers=HEADERS)

    assert response.status_code == 200, response.json
    assert response.json['success'] is True
    assert moved_records, 'the re-entered key reached the new address'
    assert moved_records[0]['headers']['X-Emby-Token'] == 'again'


def test_a_saved_library_probe_with_the_key_typed_again_goes_to_the_new_address(
        instance_api, http_fixture):
    stored, _ = http_fixture([])
    moved, moved_records = http_fixture(
        [(200, [{'id': 7, 'name': 'Movies', 'type': 'movies',
                 'paths': ['/media'], 'enabled': True}], {})])
    created = instance_api.post(ROOT, json=payload('silo', url=stored), headers=HEADERS).json

    response = instance_api.post(ROOT + '/' + created['id'] + '/libraries',
                                 json={'url': moved, 'api_key': 'again'}, headers=HEADERS)

    assert response.status_code == 200, response.json
    assert response.json['data']
    assert moved_records, 'the re-entered key reached the new address'
    assert moved_records[0]['headers']['Authorization'] == 'Bearer again'


def test_a_save_refuses_a_moved_address_without_the_key(instance_api, http_fixture):
    stored, _ = http_fixture([])
    created = instance_api.post(ROOT, json=payload('emby', url=stored), headers=HEADERS).json
    path = ROOT + '/' + created['id']

    response = instance_api.patch(path, json={'url': 'http://moved.example'}, headers=HEADERS)

    assert response.status_code == 400
    assert response.json['error_code'] == 'reenter_key'
    assert response.json['message'] == REENTER_KEY
    assert instance_api.get(path, headers=HEADERS).json['url'] == stored


def test_a_save_refuses_turning_verification_off_without_the_key(instance_api, http_fixture):
    stored, _ = http_fixture([])
    created = instance_api.post(ROOT, json=payload('emby', url=stored), headers=HEADERS).json
    path = ROOT + '/' + created['id']

    response = instance_api.patch(path, json={'verify_ssl': False}, headers=HEADERS)

    assert response.status_code == 400
    assert response.json['error_code'] == 'reenter_key'
    assert response.json['message'] == REENTER_KEY
    assert instance_api.get(path, headers=HEADERS).json['verify_ssl'] is True


def test_a_save_allows_turning_verification_back_on(instance_api, schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    created = instance_api.post(ROOT, json=payload('emby', verify_ssl=False), headers=HEADERS).json
    path = ROOT + '/' + created['id']

    response = instance_api.patch(path, json={'verify_ssl': True}, headers=HEADERS)

    assert response.status_code == 200, response.json
    assert response.json['verify_ssl'] is True
    # The stored key is kept, not cleared or replaced.
    assert MediaServerInstanceRepository(schema_session).get_decrypted_api_key(created['id']) \
        == 'opaque-A'


def test_the_validation_error_wins_over_the_destination_refusal(instance_api, http_fixture):
    stored, _ = http_fixture([])
    created = instance_api.post(ROOT, json=payload('emby', url=stored), headers=HEADERS).json
    path = ROOT + '/' + created['id']

    response = instance_api.patch(path, json={'url': 'http://moved.example', 'verify_ssl': 'nope'},
                                  headers=HEADERS)

    assert response.status_code == 400
    assert response.json['error_code'] == 'invalid_settings', response.json
    assert instance_api.get(path, headers=HEADERS).json['url'] == stored


def test_a_save_with_the_key_typed_again_moves_the_destination(instance_api, schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    created = instance_api.post(ROOT, json=payload('emby'), headers=HEADERS).json
    path = ROOT + '/' + created['id']

    response = instance_api.patch(path, json={'url': 'http://moved.example', 'api_key': 'again'},
                                  headers=HEADERS)

    assert response.status_code == 200, response.json
    assert response.json['url'] == 'http://moved.example'
    assert response.json['api_key_set'] is True
    assert MediaServerInstanceRepository(schema_session).get_decrypted_api_key(created['id']) == 'again'


def test_a_save_without_a_stored_key_moves_freely(instance_api):
    created = instance_api.post(ROOT, json={'kind': 'emby', 'name': 'Draft',
                                           'url': 'http://[::1]:8096/base'},
                                headers=HEADERS).json
    path = ROOT + '/' + created['id']

    response = instance_api.patch(path, json={'url': 'http://moved.example'}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json['url'] == 'http://moved.example'


def test_a_save_that_paused_before_its_write_keeps_the_key_a_later_save_stored(
        instance_api, schema_session, monkeypatch):
    """The check and the write share the boundary the settings save commits
    under, and the row is re-read inside it: a second save storing the key
    inside the first save's check-to-write window is seen by the first save's
    re-read, so the move is refused and the later key stays at the address its
    own save typed it for. The unfixed order checks before the write, so the
    move landed with the key the second save stored following it."""
    from discover import metadata as discover_metadata
    from media_servers import dispatcher, service

    created = instance_api.post(ROOT, json={'kind': 'emby', 'name': 'Draft',
                                            'url': 'http://[::1]:8096/base'},
                                headers=HEADERS).json
    path = ROOT + '/' + created['id']
    landed = []

    def land_the_key():
        # The second save lands inside the window wherever this head leaves
        # one: at the fixed head its entry into the write boundary runs it
        # before the real lock is taken, and at the unfixed head the write's
        # own lock entry runs it between the check and the write, before the
        # transaction opens.
        if landed:
            return
        landed.append(True)
        service.update_instance(schema_session, created['id'], {'api_key': 'late-key'})

    real_lock = discover_metadata.CONFIG_LOCK

    class _PausedLock:
        def __enter__(self):
            land_the_key()
            return real_lock.__enter__()

        def __exit__(self, *_exception):
            return real_lock.__exit__(*_exception)

    monkeypatch.setattr(discover_metadata, 'CONFIG_LOCK', _PausedLock())

    configuration = dispatcher._configuration
    real_configuration_lock = configuration.lock

    class _PausedConfigurationLock:
        def __enter__(self):
            land_the_key()
            return real_configuration_lock.__enter__()

        def __exit__(self, *_exception):
            return real_configuration_lock.__exit__(*_exception)

    monkeypatch.setattr(configuration, 'lock', _PausedConfigurationLock())

    response = instance_api.patch(path, json={'url': 'http://moved.example'}, headers=HEADERS)

    assert response.status_code == 400, response.json
    assert response.json['error_code'] == 'reenter_key'
    assert response.json['message'] == REENTER_KEY
    assert instance_api.get(path, headers=HEADERS).json['url'] == 'http://[::1]:8096/base'
    # The key the second save stored is kept, at the address it was typed for.
    from media_servers.repository import MediaServerInstanceRepository
    assert MediaServerInstanceRepository(schema_session).get_decrypted_api_key(
        created['id']) == 'late-key'


@pytest.mark.parametrize('suffix', ['/test-connection', '/libraries'])
def test_a_probe_of_the_saved_connection_sends_the_snapshot_credential(
        instance_api, monkeypatch, http_fixture, suffix):
    """The probe decides on the row it fetched and sends the credential from
    that same snapshot, never a fresh read: an address-and-key replacement
    landing between the row read and the credential read cannot pair the new
    key with the address the request already had. The card's Test probes the
    saved connection with an empty body. The unfixed order read the
    credential again from the repository, so the replacement's key reached
    the address the request named."""
    kind = 'silo' if suffix == '/libraries' else 'emby'
    from test_silo_client import HEALTH, LIBRARIES

    def replies():
        return [(200, HEALTH, {}), (200, LIBRARIES, {})] if kind == 'silo' else [
            (200, {'ServerName': 'Test', 'Version': '4.9.5.0'}, {})]

    stored, records = http_fixture(replies())
    created = instance_api.post(ROOT, json=payload(kind, url=stored), headers=HEADERS).json

    landed = []
    from media_servers import repository

    def read_with_a_replacement(self, instance_id):
        # An address-and-key replacement lands between the probe's row read
        # and its credential read, and this fresh read returns the key it
        # stored, exactly the way the unfixed order reads it.
        from sqlalchemy import update

        from app.database import TableMediaServerInstances
        landed.append(True)
        self.session.execute(
            update(TableMediaServerInstances)
            .where(TableMediaServerInstances.id == instance_id)
            .values(url='http://replaced.example/elsewhere', api_key='replacement-key'))
        self.session.flush()
        return 'replacement-key'

    monkeypatch.setattr(repository.MediaServerInstanceRepository,
                        'get_decrypted_api_key', read_with_a_replacement)

    response = instance_api.post(ROOT + '/' + created['id'] + suffix, json={}, headers=HEADERS)

    assert response.status_code == 200, response.json
    assert not landed, 'the probe read its credential from the snapshot, never from the repository'
    expected = 'Bearer opaque-A' if kind == 'silo' else 'opaque-A'
    assert records, 'the snapshot credential reached the snapshot address'
    assert all(record['headers']['Authorization' if kind == 'silo' else 'X-Emby-Token'] == expected
               for record in records), \
        'the credential the snapshot read is the one that was sent'


@pytest.mark.parametrize('suffix', ['/test-connection', '/libraries'])
def test_a_probe_of_the_form_values_sends_the_snapshot_credential(
        instance_api, monkeypatch, http_fixture, suffix):
    """The edit form's keep-current-key mode submits the address it has on
    screen and no key, so the probe answers for the saved connection with the
    stored key, and that key comes from the row snapshot the destination
    comparison used, never a fresh read: an address-and-key replacement
    landing between the two reads cannot send its key to the address the
    request already had."""
    kind = 'silo' if suffix == '/libraries' else 'emby'
    from test_silo_client import HEALTH, LIBRARIES

    def replies():
        return [(200, HEALTH, {}), (200, LIBRARIES, {})] if kind == 'silo' else [
            (200, {'ServerName': 'Test', 'Version': '4.9.5.0'}, {})]

    stored, records = http_fixture(replies())
    created = instance_api.post(ROOT, json=payload(kind, url=stored), headers=HEADERS).json

    landed = []
    from media_servers import repository

    def read_with_a_replacement(self, instance_id):
        from sqlalchemy import update

        from app.database import TableMediaServerInstances
        landed.append(True)
        self.session.execute(
            update(TableMediaServerInstances)
            .where(TableMediaServerInstances.id == instance_id)
            .values(url='http://replaced.example/elsewhere', api_key='replacement-key'))
        self.session.flush()
        return 'replacement-key'

    monkeypatch.setattr(repository.MediaServerInstanceRepository,
                        'get_decrypted_api_key', read_with_a_replacement)

    response = instance_api.post(ROOT + '/' + created['id'] + suffix,
                                 json={'url': stored}, headers=HEADERS)

    assert response.status_code == 200, response.json
    assert not landed, 'the probe read its credential from the snapshot, never from the repository'
    expected = 'Bearer opaque-A' if kind == 'silo' else 'opaque-A'
    assert records, 'the snapshot credential reached the snapshot address'
    assert all(record['headers']['Authorization' if kind == 'silo' else 'X-Emby-Token'] == expected
               for record in records), \
        'the credential the snapshot read is the one that was sent'


def test_the_account_row_is_held_to_the_plex_tv_listing(instance_api, monkeypatch):
    """The Plex row the signed-in account owns carries the account token, which
    nobody can type again: its address is probed only while plex.tv still
    lists it, and no request moves it."""
    from types import SimpleNamespace

    import requests

    from app.config import settings as app_settings
    from media_servers import plex_account

    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='account-token',
                           url='https://unlisted.example:32400'), headers=HEADERS).json
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth',
                       'token': 'account-token', 'server_machine_id': 'machine-abc',
                       'server_url': 'https://unlisted.example:32400'}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    def plex_tv(_url, **_kwargs):
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': 'https://listed.example:32400'}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    path = ROOT + '/' + created['id']
    response = instance_api.post(path + '/test-connection', json={}, headers=HEADERS)
    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed'
    assert 'Plex does not list' in response.json['message']

    response = instance_api.post(path + '/test-connection',
                                 json={'url': 'https://attacker.example:32400'},
                                 headers=HEADERS)
    assert response.status_code == 400
    assert response.json['error_code'] == 'account_owned'

    response = instance_api.patch(path, json={'url': 'https://attacker.example:32400'},
                                  headers=HEADERS)
    assert response.status_code == 409
    assert response.json['error_code'] == 'account_owned'
    assert instance_api.get(path, headers=HEADERS).json['url'] == 'https://unlisted.example:32400'


def test_an_empty_selection_still_refuses_the_unlisted_account_row(
        instance_api, monkeypatch, http_fixture):
    """A dropped selection empties the stored server URL but leaves the
    account's token on the row, so the row is the account's for as long as it
    is owned and signed in: neither a connection test nor a library listing
    may send the token to the row's unlisted address."""
    from types import SimpleNamespace

    import requests

    from app.config import settings as app_settings
    from media_servers import plex_account

    unlisted, records = http_fixture([])
    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='account-token', url=unlisted),
        headers=HEADERS).json
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth',
                       'token': 'account-token', 'server_machine_id': 'machine-abc',
                       'server_url': unlisted}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    def plex_tv(_url, **_kwargs):
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': 'https://listed.example:32400'}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    # A dropped selection: the stored address and machine are emptied, and
    # only the row keeps the unlisted address and the account token.
    monkeypatch.setattr(app_settings.plex, 'server_url', '')
    monkeypatch.setattr(app_settings.plex, 'server_machine_id', '')

    path = ROOT + '/' + created['id']
    for suffix in ('/test-connection', '/libraries'):
        response = instance_api.post(path + suffix, json={}, headers=HEADERS)
        assert response.status_code == 400, suffix
        assert response.json['error_code'] == 'server_not_listed', suffix
        assert 'Plex does not list' in response.json['message'], suffix
    assert records == [], 'the account token never reached the unlisted row address'


def test_the_account_row_can_turn_certificate_verification_back_on(instance_api, monkeypatch):
    """Only turning verification off moves the account's credential; turning
    it back on hardens the connection, and the row's checkbox is the user's
    own choice for the account server."""
    from app.config import settings as app_settings

    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='account-token',
                           url='https://listed.example:32400', verify_ssl=False),
        headers=HEADERS).json
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth',
                       'token': 'account-token', 'server_machine_id': 'machine-abc',
                       'server_url': 'https://listed.example:32400'}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    path = ROOT + '/' + created['id']
    response = instance_api.patch(path, json={'verify_ssl': True}, headers=HEADERS)
    assert response.status_code == 200, response.json
    assert response.json['verify_ssl'] is True

    response = instance_api.patch(path, json={'verify_ssl': False}, headers=HEADERS)
    assert response.status_code == 409
    assert response.json['error_code'] == 'account_verify_off'
    assert 'certificate verification' in response.json['message']
    assert instance_api.get(path, headers=HEADERS).json['verify_ssl'] is True


# The least plexapi constructs a server from.
_PLEX_ROOT = (b'<MediaContainer size="0" friendlyName="Attic" version="1.2.3">'
              b'</MediaContainer>')


def test_the_account_row_is_probed_at_the_uri_plex_tv_listed(instance_api, monkeypatch,
                                                            http_fixture):
    """The row's address is a legacy spelling of a listed origin, so the probe
    resolves it through the verified origin-to-URI map first: the credential
    goes to the URI plex.tv returned, never to the stored spelling."""
    from types import SimpleNamespace

    import requests

    from app.config import settings as app_settings
    from media_servers import plex_account

    listed, records = http_fixture([(200, _PLEX_ROOT, {'Content-Type': 'application/xml'})] * 8)
    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='account-token',
                           url=listed + '/legacy'), headers=HEADERS).json
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth',
                       'token': 'account-token', 'server_machine_id': 'machine-abc',
                       'server_url': listed + '/legacy'}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    def plex_tv(_url, **_kwargs):
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': listed}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    response = instance_api.post(ROOT + '/' + created['id'] + '/test-connection',
                                 json={}, headers=HEADERS)
    assert response.status_code == 200, response.json
    assert response.json['success'] is True
    assert records, 'the probe reached the listed origin'
    assert all('/legacy' not in record['path'] for record in records), \
        'the stored spelling was never the address the token went to'
    assert all(record['headers'].get('X-Plex-Token') == 'account-token' for record in records)


def _oauth_row_at_an_unlisted_origin(instance_api, monkeypatch, http_fixture, options=None):
    """The signed-in account, whose row still points at an address plex.tv
    does not list for it, with a local origin that records anything sent."""
    from types import SimpleNamespace

    import requests

    from app.config import settings as app_settings
    from media_servers import plex_account

    unlisted, records = http_fixture([])
    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='account-token', url=unlisted,
                           options=options or {}),
        headers=HEADERS).json
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth',
                       'token': 'account-token', 'apikey': '',
                       'server_machine_id': 'machine-abc',
                       'server_url': unlisted}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    def plex_tv(_url, **_kwargs):
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': 'https://listed.example:32400'}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()
    return created['id'], records


def test_a_settings_save_switching_the_method_does_not_lift_the_account_row_gate(
        instance_api, monkeypatch, http_fixture, schema_session):
    """The gate that holds the account's row to the plex.tv listing is the
    credential the row holds, not a setting a save can write: switching the
    method by a one-field save leaves the token on the row, so the row stays
    the account's and its token is sent nowhere."""
    from app import config as app_config
    from app.config import settings as app_settings
    from media_servers.repository import MediaServerInstanceRepository

    instance_id, records = _oauth_row_at_an_unlisted_origin(instance_api, monkeypatch, http_fixture)
    monkeypatch.setattr('app.database.database', schema_session)
    path = ROOT + '/' + instance_id
    assert instance_api.post(path + '/test-connection', json={}, headers=HEADERS
                             ).json['error_code'] == 'server_not_listed'

    app_config.save_settings([('settings-plex-auth_method', ['apikey'])])
    assert app_settings.plex.get('auth_method') == 'apikey', 'the save itself went through'

    for suffix in ('/test-connection', '/libraries'):
        response = instance_api.post(path + suffix, json={}, headers=HEADERS)
        assert response.status_code == 400, suffix
        assert response.json['error_code'] == 'server_not_listed', suffix
    assert records == [], 'the account token reached no address at all'
    assert MediaServerInstanceRepository(schema_session).snapshot(
        instance_id, app_settings).sendable is False


def test_a_settings_save_cannot_rewrite_the_recorded_plex_owner(
        instance_api, monkeypatch, http_fixture, schema_session):
    """settings.plex.instance_id is recorded by the server itself on every
    account transition, so a save cannot type in an owner: the account's row
    is not whichever row a request names, and the recorded owner is what keeps
    the token off every other row."""
    from dynaconf.validator import ValidationError

    from app import config as app_config
    from app.config import settings as app_settings

    instance_id, records = _oauth_row_at_an_unlisted_origin(instance_api, monkeypatch, http_fixture)
    path = ROOT + '/' + instance_id

    with pytest.raises(ValidationError):
        app_config.save_settings([('settings-plex-instance_id', [''])])
    assert app_settings.plex.instance_id == instance_id, 'the recorded owner was not rewritten'

    response = instance_api.post(path + '/test-connection', json={}, headers=HEADERS)
    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed'
    assert records == [], 'the account token reached no address at all'


def test_a_settings_save_touching_the_credential_carries_it_onto_the_row(
        instance_api, monkeypatch, http_fixture, schema_session):
    """A settings save that replaces the account credential is an account
    transition like a sign-in: the row the account owns is re-synced, so the
    refreshes follow the new credential rather than sending the old one."""
    from app import config as app_config
    from media_servers.repository import MediaServerInstanceRepository

    instance_id, records = _oauth_row_at_an_unlisted_origin(instance_api, monkeypatch, http_fixture)
    monkeypatch.setattr('app.database.database', schema_session)
    repo = MediaServerInstanceRepository(schema_session)
    plex_tv_listing = 'https://listed.example:32400'

    app_config.save_settings([('settings-plex-server_url', [plex_tv_listing]),
                              ('settings-plex-token', ['account-token'])])
    # The row follows the selection that was just saved, so a later save that
    # replaces the token carries it onto the row at once.
    app_config.save_settings([('settings-plex-token', ['replacement-token'])])

    assert repo.get_decrypted_api_key(instance_id) == 'replacement-token'
    assert repo.get(instance_id).url == plex_tv_listing


def test_a_settings_save_of_a_listed_address_carries_it_onto_the_row(
        instance_api, monkeypatch, http_fixture, schema_session):
    """The row is re-synced by a save of the selected server address too, not
    only by one that changes the method or the credential: an address plex.tv
    lists, stored by a save, reaches the persisted row the refreshes read,
    rather than waiting for the next account change or restart."""
    from app import config as app_config
    from media_servers.repository import MediaServerInstanceRepository

    instance_id, records = _oauth_row_at_an_unlisted_origin(instance_api, monkeypatch, http_fixture)
    monkeypatch.setattr('app.database.database', schema_session)
    repo = MediaServerInstanceRepository(schema_session)
    plex_tv_listing = 'https://listed.example:32400'

    app_config.save_settings([('settings-plex-server_url', [plex_tv_listing])])

    assert repo.get(instance_id).url == plex_tv_listing, \
        'the saved selection reached the account row'


def test_a_settings_save_dropping_the_credential_clears_it_off_the_row(
        instance_api, monkeypatch, http_fixture, schema_session):
    """A save that drops the sign-in the row's credential came from clears the
    credential off the row with it, so a stale token never sits on the
    destination waiting for a later change to wave it through."""
    from app import config as app_config
    from media_servers.repository import MediaServerInstanceRepository

    instance_id, records = _oauth_row_at_an_unlisted_origin(instance_api, monkeypatch, http_fixture)
    monkeypatch.setattr('app.database.database', schema_session)
    repo = MediaServerInstanceRepository(schema_session)

    app_config.save_settings([('settings-plex-token', [''])])

    assert repo.get(instance_id).api_key == '', 'the dropped credential was cleared'
    assert not repo.get(instance_id).enabled, 'a row with no credential runs nothing'
    assert records == []


def test_a_held_account_row_is_not_rescanned_by_the_libraries_button(
        instance_api, monkeypatch, http_fixture, schema_session):
    """The card's Refresh button walks the same snapshots the refresh worker
    does, so a destination the rule holds back is refused before any client is
    built: the stored token is sent nowhere, not even while plex.tv cannot be
    asked at all."""
    from types import SimpleNamespace

    import requests

    from media_servers import dispatcher, plex_account

    instance_id, records = _oauth_row_at_an_unlisted_origin(
        instance_api, monkeypatch, http_fixture, options={'movie_libraries': ['Movies']})
    path = ROOT + '/' + instance_id
    # Republish under the gate: an allowed edit.
    assert instance_api.patch(path, json={'name': 'Plex'}, headers=HEADERS).status_code == 200
    snapshot = dispatcher._configuration.read(instance_id)[1]
    assert snapshot.sendable is False

    response = instance_api.post(path + '/refresh-libraries', headers=HEADERS)
    assert response.status_code == 400
    assert response.json == {'requested': 0, 'error_code': 'server_not_listed'}
    assert records == [], 'the stored token was sent nowhere'

    # While plex.tv cannot be asked either, the answer stays no.
    def unreachable(_url, **_kwargs):
        return SimpleNamespace(status_code=503, headers={'content-type': 'application/json'},
                               json=lambda: [])

    monkeypatch.setattr(requests, 'get', unreachable)
    plex_account.forget_plex_tv_answer()
    response = instance_api.post(path + '/refresh-libraries', headers=HEADERS)
    assert response.status_code == 400
    assert records == [], 'the account token reached no address while plex.tv was unreachable'


def test_one_ownership_decision_carries_the_probe_through_a_sign_out(
        instance_api, monkeypatch, http_fixture):
    """The probe decides once whether the row is the account's, before its
    credential is captured, and that one decision carries the whole probe: a
    sign-out landing between the capture and the send cannot flip the decision
    and let the captured token out to the stored spelling of a listed origin.
    The unfixed order decided again after the capture, so the transition
    skipped the plex.tv resolution and the token reached the stored spelling."""
    from types import SimpleNamespace

    import requests

    from app.config import settings as app_settings
    from media_servers import plex_account, repository

    listed, records = http_fixture([(200, _PLEX_ROOT, {'Content-Type': 'application/xml'})] * 8)
    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='account-token',
                           url=listed + '/legacy'), headers=HEADERS).json
    for key, value in {'instance_id': created['id'], 'auth_method': 'oauth',
                       'token': 'account-token', 'server_machine_id': 'machine-abc',
                       'server_url': listed + '/legacy'}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    def plex_tv(_url, **_kwargs):
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': listed}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    landed = []
    real_snapshot = repository.MediaServerInstanceRepository.snapshot_api_key

    def snapshot_with_a_sign_out(self, row):
        # The sign-out lands between the probe's credential capture and its
        # send, the way a concurrent transition does.
        landed.append(True)
        for key, value in {'auth_method': 'apikey', 'token': ''}.items():
            monkeypatch.setattr(app_settings.plex, key, value)
        return real_snapshot(self, row)

    monkeypatch.setattr(repository.MediaServerInstanceRepository,
                       'snapshot_api_key', snapshot_with_a_sign_out)

    response = instance_api.post(ROOT + '/' + created['id'] + '/test-connection',
                                 json={}, headers=HEADERS)

    assert landed, 'the transition landed between the capture and the send'
    assert response.status_code == 400, response.json
    assert response.json['error_code'] == 'server_not_listed'
    assert records == [], 'the captured token reached no address at all'


def test_one_ownership_decision_carries_the_probe_through_a_late_sign_in(
        instance_api, monkeypatch, http_fixture):
    """The other route of the same rule: the probe decides once that the row
    is not the account's, before its credential is captured, and a sign-in
    landing between the capture and the send cannot turn the probe into a
    plex.tv listing check for a credential the account never vouched. The
    unfixed order decided again after the capture, so the late sign-in
    refused the stored address the probe was asked to test."""
    from types import SimpleNamespace

    import requests

    from app.config import settings as app_settings
    from media_servers import plex_account, repository

    stored, records = http_fixture([(200, _PLEX_ROOT, {'Content-Type': 'application/xml'})] * 8)
    created = instance_api.post(
        ROOT, json=payload('plex', path_mappings=[], api_key='plain-key', url=stored),
        headers=HEADERS).json
    for key, value in {'auth_method': 'apikey', 'token': '', 'instance_id': ''}.items():
        monkeypatch.setattr(app_settings.plex, key, value)

    def plex_tv(_url, **_kwargs):
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': 'https://elsewhere.example:32400'}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    landed = []
    real_snapshot = repository.MediaServerInstanceRepository.snapshot_api_key

    def snapshot_with_a_sign_in(self, row):
        # The sign-in lands between the probe's credential capture and its
        # send, and it names the row's own credential, the way a transition
        # that makes the row the account's does.
        landed.append(True)
        for key, value in {'auth_method': 'oauth', 'token': 'plain-key',
                           'server_machine_id': 'machine-abc'}.items():
            monkeypatch.setattr(app_settings.plex, key, value)
        return real_snapshot(self, row)

    monkeypatch.setattr(repository.MediaServerInstanceRepository,
                        'snapshot_api_key', snapshot_with_a_sign_in)

    response = instance_api.post(ROOT + '/' + created['id'] + '/test-connection',
                                 json={}, headers=HEADERS)

    assert landed, 'the transition landed between the capture and the send'
    assert response.status_code == 200, response.json
    assert response.json['success'] is True
    assert records, 'the probe tested the stored address it was asked to test'
