# coding=utf-8
"""Tests for the password-confirmation gate in front of actions that run code.

While form or basic login is configured, the API key alone must not be able to
add a Provider Hub catalog source, change a source ref, install or upload a
plugin, apply a plugin update, change the post-processing command, globally or
per instance, or the login settings, or restore a backup: each of those runs code
on the host or rewrites the whole configuration. A caller has to be a signed-in
user who has just confirmed the password, and the confirmation lives in the
session for ten minutes. The confirm endpoint sits behind the same throttle as
login, so probing it is no cheaper than probing login.

With login off there is no password to confirm, so nothing may ask for one:
the request-host check is the defence there. And the gate lives only in the
route handlers, never in the modules that do the work, because startup
auto-install, activation at boot and the restore that runs at the next start
call those modules directly with no request and no user.
"""

import io
import time
import types
from pathlib import Path

import pytest
from flask import Flask

from api import api_bp
from api.system import account
from app.config import settings
from utilities.helper import hash_password

API_KEY = 'password-confirm-test-key'
USERNAME = 'admin'
PASSWORD = 'hunter2'

# One hash for the whole module: PBKDF2 with 600k iterations is not cheap, and
# the settings it protects are re-created for every test.
PASSWORD_HASH = hash_password(PASSWORD)

CONFIRM_KEY = 'password_confirmed_until'

HUB_ADD_SOURCE = 'add-source'
HUB_DEV_REF = 'dev-ref'
HUB_INSTALL = 'install'
HUB_UPLOAD = 'upload'
HUB_UPDATE = 'update'

HUB_GATED_ACTIONS = [HUB_ADD_SOURCE, HUB_DEV_REF, HUB_INSTALL, HUB_UPLOAD, HUB_UPDATE]

GATED_SETTINGS_SAVES = [
    {'settings-general-postprocessing_cmd': '/bin/touch /tmp/confirmed'},
    {'settings-general-use_postprocessing': 'True'},
    {'settings-auth-type': 'null'},
    {'settings-auth-username': 'someone-else'},
    {'settings-auth-password': 'a-new-password'},
]

GATED_SETTINGS_SAVE_CASES = [
    (f'settings:{key}', payload)
    for payload in GATED_SETTINGS_SAVES
    for key in payload
]

# The per-instance override the arr instance endpoints carry. The changing blob
# differs from the stored override by value and by presence; the unchanged one
# is the stored blob itself, which is what the edit modal resends on every save.
STORED_INSTANCE_OVERRIDE = {'general': {'use_postprocessing': False,
                                        'postprocessing_threshold': 60}}
CHANGING_INSTANCE_OVERRIDE = {'general': {'use_postprocessing': True,
                                          'postprocessing_cmd': '/bin/true'}}
KEYLESS_INSTANCE_OVERRIDE = {'subsync': {'use_subsync': True}}

INSTANCE_POST = 'instance-post'
INSTANCE_PATCH = 'instance-patch'


def _auth(monkeypatch, auth_type='form'):
    """Point the real settings object at a test account and clear the throttle."""
    monkeypatch.setitem(settings.auth, 'apikey', API_KEY)
    monkeypatch.setitem(settings.auth, 'type', auth_type)
    monkeypatch.setitem(settings.auth, 'username', USERNAME)
    monkeypatch.setitem(settings.auth, 'password', PASSWORD_HASH)
    account._login_attempts.clear()


def _client():
    """A Flask app with the real API blueprint, as the server serves it."""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.config['TESTING'] = True
    app.register_blueprint(api_bp)
    return app.test_client()


def _api_headers():
    return {'X-API-KEY': API_KEY}


def _basic_auth():
    return (USERNAME, PASSWORD)


def _prime(client, confirmed=True):
    """Give the client the session a signed-in user holds, confirmed or not."""
    with client.session_transaction() as sess:
        sess['logged_in'] = True
        if confirmed:
            sess[CONFIRM_KEY] = time.time() + 600


def _expire(client):
    """Move the confirmation mark out of its window."""
    with client.session_transaction() as sess:
        sess[CONFIRM_KEY] = time.time() - 1


def _login(client, password=PASSWORD):
    return client.post('/api/system/account',
                      data={'action': 'login', 'username': USERNAME, 'password': password})


def _confirm(client, password=PASSWORD, auth=None):
    return client.post('/api/system/account/confirm',
                      data={'username': USERNAME, 'password': password},
                      headers=_api_headers(), auth=auth)


def _hub_action(client, name, auth=None):
    if name == HUB_ADD_SOURCE:
        return client.post('/api/provider-hub/catalog/sources',
                           json={'name': 'community', 'url': 'https://github.com/owner/repo'},
                           headers=_api_headers(), auth=auth)
    if name == HUB_DEV_REF:
        return client.patch('/api/provider-hub/catalog/sources/community',
                            json={'dev_ref': 'feat/branch'},
                            headers=_api_headers(), auth=auth)
    if name == HUB_INSTALL:
        return client.post('/api/provider-hub/installations',
                           json={'source': 'official', 'provider_id': 'some-provider',
                                 'version': '1.0.0'},
                           headers=_api_headers(), auth=auth)
    if name == HUB_UPLOAD:
        return client.post('/api/provider-hub/installations/local',
                           data={'file': (io.BytesIO(b'a package'), 'plugin.zip')},
                           headers=_api_headers(), auth=auth)
    if name == HUB_UPDATE:
        return client.post('/api/provider-hub/updates/apply',
                           data={'provider_id': 'some-provider'},
                           headers=_api_headers(), auth=auth)
    raise ValueError(name)


def _settings_save(client, payload):
    return client.post('/api/system/settings', data=payload, headers=_api_headers())


def _arr_instance_save(client, verb, subtitle_settings, auth=None):
    """An arr instance create or update carrying a subtitle_settings override."""
    if verb == INSTANCE_POST:
        return client.post('/api/system/arr-instances',
                           json={'kind': 'sonarr', 'name': 'A Sonarr',
                                 'subtitle_settings': subtitle_settings},
                           headers=_api_headers(), auth=auth)
    return client.patch('/api/system/arr-instances/42',
                        json={'subtitle_settings': subtitle_settings},
                        headers=_api_headers(), auth=auth)


def _restore(client, filename='bazarr_backup_v1_20260101.zip'):
    return client.patch('/api/system/backups', data={'filename': filename},
                        headers=_api_headers())


def _assert_confirm_refusal(response):
    assert response.status_code == 403
    body = response.get_json()
    assert body['code'] == 'confirm_password'
    assert 'password' in body['message'].lower()


ALL_GATED_ACTIONS = ([f'hub:{name}' for name in HUB_GATED_ACTIONS]
                     + [name for name, _ in GATED_SETTINGS_SAVE_CASES]
                     + ['restore', INSTANCE_POST, INSTANCE_PATCH])


def _gated_action(client, name, auth=None):
    """Run the gated action a review can name, the way the caller would."""
    kind, _, detail = name.partition(':')
    if kind == 'hub':
        return _hub_action(client, detail, auth=auth)
    if kind == 'settings':
        return _settings_save(client, GATED_SETTINGS_SAVE_CASES_BY_NAME[name])
    if name == 'restore':
        return _restore(client)
    if name == INSTANCE_POST:
        return _arr_instance_save(client, INSTANCE_POST, CHANGING_INSTANCE_OVERRIDE, auth=auth)
    if name == INSTANCE_PATCH:
        return _arr_instance_save(client, INSTANCE_PATCH, CHANGING_INSTANCE_OVERRIDE, auth=auth)
    raise ValueError(name)


GATED_SETTINGS_SAVE_CASES_BY_NAME = dict(GATED_SETTINGS_SAVE_CASES)


# ---------------------------------------------------------------------------
# Fixtures: the work the gated handlers would do, recorded
# ---------------------------------------------------------------------------


@pytest.fixture
def hub_calls(monkeypatch, tmp_path):
    """The Provider Hub handlers with their service and job calls recorded."""
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    calls = []
    monkeypatch.setattr(service, 'add_catalog_source',
                        lambda name, url: calls.append((HUB_ADD_SOURCE, name)) or {'name': name})
    monkeypatch.setattr(service, 'update_catalog_source',
                        lambda name, **kwargs: calls.append((HUB_DEV_REF, name)) or {'id': name})
    monkeypatch.setattr(service, 'get_catalog_source', lambda name: {'id': name})
    monkeypatch.setattr(service, 'refresh_catalog', lambda: None)
    monkeypatch.setattr(service, 'resolve_catalog_install',
                        lambda source, provider_id, version, manifest=None:
                        {'source': source, 'provider_id': provider_id,
                         'version': version, 'name': 'Some Provider'})
    monkeypatch.setattr(hub_jobs, 'queue_install',
                        lambda source, provider_id, version, name:
                        calls.append((HUB_INSTALL, provider_id)) or 'job-install')
    monkeypatch.setattr(service, 'local_package_uploads_dir', lambda: tmp_path)
    monkeypatch.setattr(hub_jobs, 'queue_install_local',
                        lambda package_path, filename=None:
                        calls.append((HUB_UPLOAD, filename)) or 'job-local')
    monkeypatch.setattr(service, 'get_provider',
                        lambda provider_id: {'provider_id': provider_id, 'name': 'Some Provider'})
    monkeypatch.setattr(hub_jobs, 'queue_update',
                        lambda provider_id, name:
                        calls.append((HUB_UPDATE, provider_id)) or 'job-update')
    return calls


@pytest.fixture
def settings_saves(monkeypatch):
    """The settings save path with its writers recorded and its side steps off."""
    from api.system import settings as settings_api

    saves = []
    monkeypatch.setattr(settings_api, 'validate_metadata_settings', lambda items: None)
    monkeypatch.setattr(settings_api, 'save_settings',
                        lambda items: saves.append(dict(items)) or None)
    monkeypatch.setattr(settings_api, '_write_settings_rows', lambda *args: None)
    monkeypatch.setattr(settings_api, '_after_settings_rows', lambda *args: True)
    return saves


@pytest.fixture
def backup_calls(monkeypatch):
    """The backups handlers with the backup module's actions recorded."""
    from api.system import backups as backups_api

    calls = []
    monkeypatch.setattr(backups_api, 'get_backup_files', lambda fullpath=True: [])
    monkeypatch.setattr(backups_api, 'backup_to_zip',
                        lambda: calls.append('create') or True)
    monkeypatch.setattr(backups_api, 'prepare_restore',
                        lambda filename: calls.append(('restore', filename)) or True)
    monkeypatch.setattr(backups_api, 'schedule_restore_restart',
                        lambda: calls.append('restart'))
    monkeypatch.setattr(backups_api, 'delete_backup_file', lambda filename: True)
    return calls


@pytest.fixture
def arr_instance_calls(monkeypatch):
    """The arr instance handlers with their service and persistence calls recorded.

    The stored instance carries STORED_INSTANCE_OVERRIDE, so a PATCH can be
    made to change or to merely resend the post-processing override.
    """
    import secret_store
    from api.system import arr_instances as arr_api
    from arr_instances import service as arr_service

    calls = []

    class _Database:
        committed = 0

        def commit(self):
            _Database.committed += 1

    monkeypatch.setattr(arr_api, 'database', _Database())
    monkeypatch.setattr(secret_store, 'persist_master_key',
                        lambda: calls.append('master-key'))
    monkeypatch.setattr(arr_service, 'get_instance',
                        lambda session, instance_id:
                        ({'id': instance_id, 'kind': 'sonarr',
                          'subtitle_settings': STORED_INSTANCE_OVERRIDE}, 200))
    monkeypatch.setattr(arr_service, 'create_instance',
                        lambda session, args:
                        calls.append(('create', None, args.get('subtitle_settings')))
                        or ({'id': 1, 'kind': 'sonarr'}, 201))
    monkeypatch.setattr(arr_service, 'update_instance',
                        lambda session, instance_id, args:
                        calls.append(('update', instance_id, args.get('subtitle_settings')))
                        or ({'id': instance_id, 'kind': 'sonarr'}, 200))
    monkeypatch.setattr(arr_service, 'refresh_runtime', lambda *args, **kwargs: None)
    return calls


# ---------------------------------------------------------------------------
# The gated endpoints refuse anything but a confirmed signed-in user
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action', HUB_GATED_ACTIONS)
def test_hub_actions_refuse_the_api_key_alone(action, monkeypatch, hub_calls):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_hub_action(client, action))
    assert hub_calls == []


@pytest.mark.parametrize('action', HUB_GATED_ACTIONS)
def test_hub_actions_refuse_a_signed_in_session_without_a_confirmation(action, monkeypatch, hub_calls):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    _assert_confirm_refusal(_hub_action(client, action))
    assert hub_calls == []


@pytest.mark.parametrize('payload', GATED_SETTINGS_SAVES)
def test_gated_settings_saves_refuse_the_api_key_alone(payload, monkeypatch, settings_saves):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_settings_save(client, payload))
    assert settings_saves == []


def test_a_case_variant_of_a_gated_settings_key_is_refused_too(monkeypatch, settings_saves):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_settings_save(client, {'SETTINGS-AUTH-TYPE': 'null'}))
    assert settings_saves == []


def test_gated_settings_saves_refuse_a_signed_in_session_without_a_confirmation(monkeypatch, settings_saves):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    _assert_confirm_refusal(_settings_save(client, GATED_SETTINGS_SAVES[0]))
    assert settings_saves == []


def test_the_restore_refuses_the_api_key_alone(monkeypatch, backup_calls):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_restore(client))
    assert backup_calls == []


def test_the_restore_refuses_a_signed_in_session_without_a_confirmation(monkeypatch, backup_calls):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    _assert_confirm_refusal(_restore(client))
    assert backup_calls == []


@pytest.mark.parametrize('verb', [INSTANCE_POST, INSTANCE_PATCH])
def test_arr_instance_saves_refuse_the_api_key_alone(verb, monkeypatch, arr_instance_calls):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_arr_instance_save(client, verb, CHANGING_INSTANCE_OVERRIDE))
    # Refused before the master key is persisted and before the service call.
    assert arr_instance_calls == []


@pytest.mark.parametrize('verb', [INSTANCE_POST, INSTANCE_PATCH])
def test_arr_instance_saves_refuse_a_signed_in_session_without_a_confirmation(verb, monkeypatch,
                                                                              arr_instance_calls):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    _assert_confirm_refusal(_arr_instance_save(client, verb, CHANGING_INSTANCE_OVERRIDE))
    assert arr_instance_calls == []


@pytest.mark.parametrize('verb', [INSTANCE_POST, INSTANCE_PATCH])
def test_a_confirmed_session_saves_the_arr_instance_overrides(verb, monkeypatch, arr_instance_calls):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204

    response = _arr_instance_save(client, verb, CHANGING_INSTANCE_OVERRIDE)
    assert response.status_code in (200, 201), response.status_code
    # The master key is persisted before the service writes, and never after a
    # refusal: see the empty call lists in the refusal tests above. A PATCH
    # that carries no api_key never persists it at all.
    expected = [('create', None, CHANGING_INSTANCE_OVERRIDE)]
    if verb == INSTANCE_POST:
        expected = ['master-key'] + expected
    else:
        expected = [('update', 42, CHANGING_INSTANCE_OVERRIDE)]
    assert arr_instance_calls == expected


@pytest.mark.parametrize('verb', [INSTANCE_POST, INSTANCE_PATCH])
def test_an_override_save_that_changes_no_postprocessing_key_needs_no_confirmation(
        verb, monkeypatch, arr_instance_calls):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    # A create has nothing stored, so a blob that never carried the keys saves
    # freely. The edit modal resends the whole blob on every save, so a PATCH
    # saves freely only while the post-processing keys keep their stored
    # presence and value: an unchanged resend, and a resend that changes only
    # an unrelated override.
    if verb == INSTANCE_POST:
        blobs = [KEYLESS_INSTANCE_OVERRIDE]
    else:
        blobs = [STORED_INSTANCE_OVERRIDE,
                 {**STORED_INSTANCE_OVERRIDE, 'subsync': {'use_subsync': True}}]
    for blob in blobs:
        response = _arr_instance_save(client, verb, blob)
        assert response.status_code in (200, 201), response.status_code
    assert [call[0] for call in arr_instance_calls if call != 'master-key'] == \
        ['create' if verb == INSTANCE_POST else 'update'] * len(blobs)


# The merge replaces the whole stored blob, so leaving a post-processing key
# out of a PATCH clears it: a stored use_postprocessing goes away and the
# instance falls back to the global command. These PATCH bodies carry no
# post-processing key at all against STORED_INSTANCE_OVERRIDE, so the gate
# must read the drop, not just the keys that arrived.
DROPPING_INSTANCE_OVERRIDES = [{}, KEYLESS_INSTANCE_OVERRIDE]


@pytest.mark.parametrize('blob', DROPPING_INSTANCE_OVERRIDES)
def test_a_patch_that_drops_a_stored_postprocessing_key_is_gated(blob, monkeypatch,
                                                                arr_instance_calls):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_arr_instance_save(client, INSTANCE_PATCH, blob))
    # Refused before the master key and before the service rewrote the
    # stored override.
    assert arr_instance_calls == []


@pytest.mark.parametrize('blob', DROPPING_INSTANCE_OVERRIDES)
def test_a_confirmed_session_may_drop_a_stored_postprocessing_key(blob, monkeypatch,
                                                                  arr_instance_calls):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204

    response = _arr_instance_save(client, INSTANCE_PATCH, blob)
    assert response.status_code == 200, response.status_code
    assert arr_instance_calls == [('update', 42, blob)]


# ---------------------------------------------------------------------------
# A confirmation opens them, and the confirm endpoint behind the login throttle
# ---------------------------------------------------------------------------


def test_login_and_confirmation_open_the_hub_actions(monkeypatch, hub_calls):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204

    for action in HUB_GATED_ACTIONS:
        response = _hub_action(client, action)
        assert response.status_code in (200, 202), (action, response.status_code)
    assert [call[0] for call in hub_calls] == HUB_GATED_ACTIONS


def test_login_and_confirmation_open_the_gated_settings_saves(monkeypatch, settings_saves):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204

    for payload in GATED_SETTINGS_SAVES:
        response = _settings_save(client, payload)
        assert response.status_code == 204, response.status_code
    assert len(settings_saves) == len(GATED_SETTINGS_SAVES)
    for payload, saved in zip(GATED_SETTINGS_SAVES, settings_saves):
        for key, value in payload.items():
            assert saved[key] == [value]


def test_a_primed_confirmation_mark_opens_the_gated_endpoints(monkeypatch, hub_calls, settings_saves, backup_calls):
    """The mark a login plus a confirm leave in the session, applied directly."""
    _auth(monkeypatch)
    client = _client()
    _prime(client)

    assert _settings_save(client, GATED_SETTINGS_SAVES[0]).status_code == 204
    assert _restore(client).status_code == 200
    assert backup_calls == [('restore', 'bazarr_backup_v1_20260101.zip'), 'restart']


def test_the_confirmation_window_is_ten_minutes(monkeypatch, hub_calls):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204

    with client.session_transaction() as sess:
        remaining = sess[CONFIRM_KEY] - time.time()
    assert 590 <= remaining <= 600


@pytest.mark.parametrize('gate', ALL_GATED_ACTIONS)
def test_once_the_window_lapses_every_gated_action_asks_again(gate, monkeypatch, hub_calls,
                                                             settings_saves, backup_calls,
                                                             arr_instance_calls):
    """The lapsed window refuses each gated action, not just the Hub ones.

    The settings save and the restore call the liveness check directly rather
    than through the decorator, so they are parametrized in here with the Hub
    actions, the gated settings keys and the instance override saves.
    """
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204
    _expire(client)

    _assert_confirm_refusal(_gated_action(client, gate))
    assert hub_calls == []
    assert settings_saves == []
    assert backup_calls == []
    assert arr_instance_calls == []


def test_a_mixed_save_with_one_gated_key_writes_nothing_at_all(monkeypatch):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    from api.system import settings as settings_api

    saves, rows = [], []
    monkeypatch.setattr(settings_api, 'validate_metadata_settings', lambda items: None)
    monkeypatch.setattr(settings_api, 'save_settings',
                        lambda items: saves.append(dict(items)))
    monkeypatch.setattr(settings_api, '_write_settings_rows',
                        lambda *args: rows.append(args))
    monkeypatch.setattr(settings_api, '_after_settings_rows', lambda *args: True)

    # One gated key riding along ordinary keys must refuse the whole save
    # before anything is written: neither the configuration nor the rows.
    payload = {'settings-general-theme': 'dark',
               'languages-enabled': 'en',
               'settings-general-use_postprocessing': 'True'}
    _assert_confirm_refusal(_settings_save(client, payload))
    assert saves == []
    assert rows == []


def test_logout_drops_the_confirmation(monkeypatch, hub_calls):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204
    assert _confirm(client).status_code == 204
    assert client.post('/api/system/account', data={'action': 'logout'}).status_code == 204

    _assert_confirm_refusal(_hub_action(client, HUB_INSTALL))
    assert hub_calls == []


def test_a_wrong_confirmation_password_is_refused_and_counts(monkeypatch, hub_calls):
    _auth(monkeypatch)
    client = _client()
    assert _login(client).status_code == 204

    for _ in range(5):
        assert _confirm(client, password='wrong').status_code == 403
    # The same address is now locked out, even for the right password.
    assert _confirm(client).status_code == 429
    # Nothing was confirmed, and the gated action still refuses.
    with client.session_transaction() as sess:
        assert CONFIRM_KEY not in sess
    _assert_confirm_refusal(_hub_action(client, HUB_INSTALL))
    assert hub_calls == []


def test_login_failures_share_the_throttle_with_confirmations(monkeypatch):
    _auth(monkeypatch)
    client = _client()

    for _ in range(5):
        assert _login(client, password='wrong').status_code == 403
    assert _confirm(client).status_code == 429


def test_confirmation_requires_a_signed_in_session_under_form_login(monkeypatch):
    _auth(monkeypatch)
    client = _client()

    assert _confirm(client).status_code == 401
    with client.session_transaction() as sess:
        assert CONFIRM_KEY not in sess


# ---------------------------------------------------------------------------
# Basic login: the request carries valid basic credentials plus the same
# confirmation
# ---------------------------------------------------------------------------


def test_basic_gated_actions_refuse_valid_credentials_without_a_confirmation(monkeypatch, hub_calls):
    _auth(monkeypatch, auth_type='basic')
    client = _client()

    # The credentials alone, with no confirmation in the session.
    _assert_confirm_refusal(_hub_action(client, HUB_INSTALL, auth=_basic_auth()))
    # And with neither basic credentials nor a session.
    _assert_confirm_refusal(_hub_action(client, HUB_INSTALL))
    assert hub_calls == []


def test_basic_confirmation_needs_valid_credentials_and_the_password(monkeypatch):
    _auth(monkeypatch, auth_type='basic')
    client = _client()

    assert _confirm(client).status_code == 403
    assert _confirm(client, auth=(USERNAME, 'wrong')).status_code == 403
    assert _confirm(client, password='wrong', auth=_basic_auth()).status_code == 403

    response = _confirm(client, auth=_basic_auth())
    assert response.status_code == 204
    with client.session_transaction() as sess:
        assert sess[CONFIRM_KEY] > time.time()


def test_basic_gated_actions_open_with_the_confirmation(monkeypatch, hub_calls):
    _auth(monkeypatch, auth_type='basic')
    client = _client()
    assert _confirm(client, auth=_basic_auth()).status_code == 204

    response = _hub_action(client, HUB_INSTALL, auth=_basic_auth())
    assert response.status_code == 202
    assert hub_calls == [(HUB_INSTALL, 'some-provider')]


# ---------------------------------------------------------------------------
# Login off: nothing asks for a confirmation
# ---------------------------------------------------------------------------


def test_login_off_nothing_demands_a_confirmation(monkeypatch, hub_calls, settings_saves,
                                                  backup_calls, arr_instance_calls):
    _auth(monkeypatch, auth_type=None)
    client = _client()

    for action in HUB_GATED_ACTIONS:
        response = _hub_action(client, action)
        assert response.status_code in (200, 202), (action, response.status_code)
    assert [call[0] for call in hub_calls] == HUB_GATED_ACTIONS

    assert _settings_save(client, GATED_SETTINGS_SAVES[0]).status_code == 204
    assert len(settings_saves) == 1

    assert _restore(client).status_code == 200
    assert backup_calls == [('restore', 'bazarr_backup_v1_20260101.zip'), 'restart']

    for verb in (INSTANCE_POST, INSTANCE_PATCH):
        response = _arr_instance_save(client, verb, CHANGING_INSTANCE_OVERRIDE)
        assert response.status_code in (200, 201), (verb, response.status_code)
    assert arr_instance_calls == ['master-key',
                                  ('create', None, CHANGING_INSTANCE_OVERRIDE),
                                  ('update', 42, CHANGING_INSTANCE_OVERRIDE)]

def test_login_off_the_confirm_endpoint_is_not_available(monkeypatch):
    _auth(monkeypatch, auth_type=None)
    client = _client()

    assert _confirm(client).status_code == 500


# ---------------------------------------------------------------------------
# The paths that stay open: ordinary settings and backup management
# ---------------------------------------------------------------------------


def test_ordinary_settings_and_backup_management_stay_open(monkeypatch, settings_saves, backup_calls):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    # A settings save that touches none of the gated keys needs no
    # confirmation.
    assert _settings_save(client, {'settings-general-theme': 'dark'}).status_code == 204
    assert len(settings_saves) == 1

    # Listing, creating and deleting backups were never code-execution sinks.
    assert client.get('/api/system/backups', headers=_api_headers()).status_code == 200
    assert client.post('/api/system/backups', headers=_api_headers()).status_code == 204
    assert client.delete('/api/system/backups', data={'filename': 'bazarr_backup_v1_20260101.zip'},
                         headers=_api_headers()).status_code == 204
    assert backup_calls == ['create']


# ---------------------------------------------------------------------------
# The paths that run without a request: startup and the restore at boot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('auth_type', ['form', 'basic'])
def test_startup_auto_install_and_boot_activation_run_ungated(monkeypatch, auth_type):
    """The boot paths do real work, with no request and no user anywhere.

    A service-layer gate would break these on every boot; the tests exercise
    the work itself rather than an early return, so such a gate could not pass
    them. The stubbed installer records whether any request context exists,
    which is the one thing the boot paths must never have.

    The tests run with login configured, so a gate that read the live settings
    would refuse here rather than ride through on login being off.
    """
    _auth(monkeypatch, auth_type=auth_type)
    from flask import has_request_context
    from provider_hub import service

    monkeypatch.delenv('BAZARR_DISABLE_PROVIDER_AUTOINSTALL', raising=False)
    monkeypatch.setitem(settings.general, 'provider_hub_auto_install', True)
    monkeypatch.setattr(service, 'load_state', lambda: {'installations': {}})
    monkeypatch.setattr(service, '_bazarr_enabled_providers', lambda: ['stubbed-provider'])
    monkeypatch.setattr(service, 'refresh_catalog', lambda **kwargs: None)
    monkeypatch.setattr(service, '_latest_official_catalog_manifest',
                        lambda state, provider_id: {'id': provider_id, 'name': 'Stubbed'})
    staged = []
    monkeypatch.setattr(service, 'stage_install',
                        lambda manifest, install_timeout=None:
                        staged.append((manifest, has_request_context())))

    assert service.autoinstall_enabled_builtins() == ['stubbed-provider']
    assert staged == [({'id': 'stubbed-provider', 'name': 'Stubbed'}, False)]

    # The activation half runs the same way: a pending removal is applied and
    # the record of the job it opens is written with no request context either.
    activated_with_request = []
    import contextlib

    @contextlib.contextmanager
    def record_job(*args, **kwargs):
        activated_with_request.append(has_request_context())
        yield types.SimpleNamespace(update=lambda **kwargs: None, data={})
    monkeypatch.setattr(service, 'record_job', record_job)
    state = {'installations': {'gone': {'pending_restart': True, 'state': 'removed',
                                        'name': 'Gone'}}}
    monkeypatch.setattr(service, 'load_state', lambda: state)
    persisted = []
    monkeypatch.setattr(service, 'mutate_state',
                        lambda mutator: persisted.append(
                            mutator({'installations': {}})))

    assert service.activate_staged_installations() == []
    assert activated_with_request == [False]
    # The removal was applied to the state and persisted, not skipped.
    assert 'gone' not in state['installations']
    assert len(persisted) == 1


@pytest.mark.parametrize('auth_type', ['form', 'basic'])
def test_the_restore_that_runs_at_the_next_start_stays_ungated(monkeypatch, auth_type,
                                                               tmp_path):
    """The boot restore reads the restore directory directly, no gate in between.

    A minimal restore is staged for real: the database step is stubbed at the
    swap itself and the configuration swap happens, so the test proves the
    restore went ahead rather than returning on an empty-directory check a
    service-layer gate would sit behind.

    The test runs with login configured, so a gate that read the live settings
    would refuse here rather than ride through on login being off.
    """
    _auth(monkeypatch, auth_type=auth_type)
    import sys
    import utilities.backup as backup_module

    restore_dir = tmp_path / 'restore'
    (tmp_path / 'config').mkdir()
    (tmp_path / 'db').mkdir()
    restore_dir.mkdir()
    (restore_dir / 'config.yaml').write_text('general:\n  theme: dark\n')
    (restore_dir / 'bazarr.db').write_bytes(b'the restored database')

    monkeypatch.setattr(backup_module, 'get_restore_path', lambda: str(restore_dir))
    monkeypatch.setattr(backup_module, 'args',
                        types.SimpleNamespace(config_dir=str(tmp_path)))
    monkeypatch.setattr(backup_module, '_postgres_enabled', lambda: False)

    from flask import has_request_context
    swaps = []

    def fake_restore_database(restore_database_path, dest_database_path):
        swaps.append((restore_database_path, has_request_context()))
        Path(dest_database_path).write_bytes(Path(restore_database_path).read_bytes())

    monkeypatch.setattr(backup_module, '_restore_database', fake_restore_database)
    restarts = []
    monkeypatch.setattr(backup_module, 'restart_bazarr', lambda: restarts.append(True))
    # The boot restore asks the webserver to close first; at boot there is
    # none, which is what the stub stands in for.
    monkeypatch.setitem(sys.modules, 'app.server', types.SimpleNamespace(webserver=None))

    assert backup_module.restore_from_backup() is True
    assert swaps == [(str(restore_dir / 'bazarr.db'), False)]
    assert (tmp_path / 'db' / 'bazarr.db').read_bytes() == b'the restored database'
    assert (tmp_path / 'config' / 'config.yaml').read_text() == 'general:\n  theme: dark\n'
    assert restarts == [True]
    # The staged set is consumed, so the next start does not restore again.
    assert list(restore_dir.iterdir()) == []


# Settings keys that do not name a setting the way the save path writes it.
# The save looks a section up by the exact segment but creates a missing one
# through the settings library, which strips whitespace and reads dots and
# double underscores as nesting, so each of these would reach a gated setting
# (or replace the whole login section) under a name the gate does not know.
MALFORMED_SETTINGS_KEYS = [
    'settings- auth-type',
    'settings-auth -type',
    'settings-auth\t-type',
    'settings-auth-type ',
    'settings-auth__type-x',
    'settings-auth.type-x',
    'settings--auth-type',
    'settings-general- postprocessing_cmd',
]


@pytest.mark.parametrize('key', MALFORMED_SETTINGS_KEYS)
@pytest.mark.parametrize('confirmed', [False, True])
def test_a_malformed_settings_key_is_refused_before_anything_is_saved(key, confirmed, monkeypatch,
                                                                      settings_saves):
    _auth(monkeypatch)
    client = _client()
    if confirmed:
        _prime(client)

    response = _settings_save(client, {key: 'None'})

    assert response.status_code == 400
    assert settings_saves == []
    assert settings.auth.type == 'form'
    assert settings.auth.password == PASSWORD_HASH


def test_a_longer_key_under_a_gated_setting_is_refused_too(monkeypatch, settings_saves):
    _auth(monkeypatch)
    client = _client()

    _assert_confirm_refusal(_settings_save(client, {'settings-auth-type-x': 'None'}))
    assert settings_saves == []


def test_the_settings_read_never_hands_out_the_password_hash(monkeypatch):
    _auth(monkeypatch)
    client = _client()

    body = client.get('/api/system/settings', headers=_api_headers()).get_json()

    assert body['auth']['password'] == '***'
    assert PASSWORD_HASH not in client.get('/api/system/settings', headers=_api_headers()).get_data(as_text=True)


def test_the_settings_read_keeps_an_unset_password_empty(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setitem(settings.auth, 'password', '')
    client = _client()

    body = client.get('/api/system/settings', headers=_api_headers()).get_json()

    assert body['auth']['password'] == ''


@pytest.fixture
def real_save(monkeypatch):
    """The real settings writer with the disk write and the database stubbed out."""
    import sys
    from app import config

    class _FakeUpdate:
        def values(self, **_kwargs):
            return self

    monkeypatch.setattr(config, 'write_config', lambda: True)
    monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
    monkeypatch.setitem(sys.modules, 'app.database', types.SimpleNamespace(
        database=types.SimpleNamespace(execute=lambda statement: None),
        update=lambda _model: _FakeUpdate(),
        System=object,
    ))
    return config


def test_saving_the_masked_password_back_keeps_the_stored_one(monkeypatch, real_save):
    # The General page resends what it read when a typed password is cleared
    # again, and what it read is the mask, never the hash.
    _auth(monkeypatch)

    real_save.save_settings([('settings-auth-password', ['***'])])

    assert settings.auth.password == PASSWORD_HASH


def test_a_new_password_is_still_hashed_on_save(monkeypatch, real_save):
    from utilities.helper import check_credentials

    _auth(monkeypatch)

    real_save.save_settings([('settings-auth-password', ['a-new-password'])])

    assert settings.auth.password not in (PASSWORD_HASH, 'a-new-password')
    app = Flask(__name__)
    with app.test_request_context('/'):
        from flask import request
        assert check_credentials(USERNAME, 'a-new-password', request, log_success=False)


def test_a_confirmation_that_names_no_username_checks_the_configured_account(monkeypatch):
    _auth(monkeypatch)
    client = _client()
    _prime(client, confirmed=False)

    wrong = client.post('/api/system/account/confirm', data={'username': '', 'password': 'nope'},
                        headers=_api_headers())
    right = client.post('/api/system/account/confirm', data={'username': '', 'password': PASSWORD},
                        headers=_api_headers())

    assert wrong.status_code == 403
    assert right.status_code == 204
    with client.session_transaction() as sess:
        assert sess[CONFIRM_KEY] > time.time()


@pytest.mark.parametrize('key', ['settings-myplugin-api__key', 'settings-myplugin-__secret_fields'])
def test_a_double_underscore_below_the_section_is_still_a_valid_settings_key(key, monkeypatch, settings_saves):
    # A plugin's config field may carry a double underscore; only the section
    # goes through the settings library's nesting rules.
    _auth(monkeypatch)
    client = _client()

    response = _settings_save(client, {key: 'value'})

    assert response.status_code == 204
    assert settings_saves == [{key: ['value']}]
