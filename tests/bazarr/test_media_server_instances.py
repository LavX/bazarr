from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from uuid import UUID

import pytest


def settings():
    return SimpleNamespace(general=SimpleNamespace(use_emby=True, use_silo=True))


def payload(kind, name='A', **overrides):
    values = dict(kind=kind, name=name, url=f'http://{name.lower()}.example/prefix',
                  api_key=f'opaque-{name}', enabled=True, verify_ssl=True,
                  path_mappings=[{'local_path': '/movies', 'remote_path': f'/media/{name}',
                                  **({'library_id': '7' if name == 'A' else '8'} if kind == 'silo' else {})}])
    return values | overrides


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_two_same_kind_destinations_keep_uuid_on_rename_and_safe_keys(schema_session, kind):
    from media_servers.repository import MediaServerInstanceRepository, to_safe_dict
    repo = MediaServerInstanceRepository(schema_session)
    first = repo.create(**payload(kind))
    second = repo.create(**payload(kind, 'B'))
    first_id, second_id = first.id, second.id
    assert str(UUID(first_id)) == first_id
    repo.update(first_id, name='Renamed')
    rows = [to_safe_dict(row) for row in repo.list()]
    assert {row['id'] for row in rows} == {first_id, second_id}
    assert first_id != second_id
    assert all('api_key' not in row and 'apikey' not in row for row in rows)
    assert repo.get_decrypted_api_key(first_id) == 'opaque-A'
    assert repo.get_decrypted_api_key(second_id) == 'opaque-B'
    assert repo.get(first_id).api_key != 'opaque-A'
    snapshot = repo.snapshot(first_id, settings())
    with pytest.raises(FrozenInstanceError):
        snapshot.url = 'http://wrong.example'


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_same_path_fanout_and_failure_retry_are_destination_scoped(schema_session, kind):
    from media_servers.repository import MediaServerInstanceRepository
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    from media_servers.events import SubtitleMutation
    from media_servers.http import MediaServerError
    repo = MediaServerInstanceRepository(schema_session)
    first = repo.create(**payload(kind))
    second = repo.create(**payload(kind, 'B'))
    configuration = NativeConfiguration(settings(), snapshots=repo.snapshots(settings()))
    calls, failures = [], {first.id}

    class Client:
        def __init__(self, _kind, snapshot):
            self.snapshot = snapshot
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def get_libraries(self):
            return [{'id': '7' if self.snapshot.id == first.id else '8', 'type': 'movies',
                     'paths': [f'/media/{"A" if self.snapshot.id == first.id else "B"}']}]
        def refresh_movie(self, path, *, ensure_current):
            return self.refresh_file(None, path, ensure_current=ensure_current)
        def refresh_file(self, library_id, path, *, ensure_current):
            ensure_current()
            calls.append((self.snapshot.id, self.snapshot.url, self.snapshot.apikey, library_id, path))
            if self.snapshot.id in failures:
                raise MediaServerError('timeout')
            return {'status': 'requested' if kind == 'emby' else 'confirmed'}

    dispatcher = RefreshDispatcher(configuration, client_factory=Client)
    dispatcher.notify(SubtitleMutation('movie', '/movies/A.mkv', '/movies/A.en.srt', 'upload', 29))
    assert dispatcher.wait_idle(3)
    assert set(calls) == {(first.id, first.url, 'opaque-A', '7' if kind == 'silo' else None, '/media/A/A.mkv'),
                          (second.id, second.url, 'opaque-B', '8' if kind == 'silo' else None, '/media/B/A.mkv')}
    assert dispatcher.status(first.id) == {'pending': 1, 'state': 'unconfirmed', 'error_code': 'timeout'}
    assert dispatcher.status(second.id)['pending'] == 0
    failures.clear()
    assert dispatcher.retry(first.id) == 1
    assert dispatcher.wait_idle(3)
    assert len([row for row in calls if row[0] == second.id]) == 1
    assert dispatcher.status(first.id)['pending'] == 0


@pytest.fixture
def blocked_pair(schema_session, monkeypatch, request):
    from threading import Event
    from media_servers import dispatcher as module
    from media_servers.repository import MediaServerInstanceRepository
    from app import config
    kind = request.param
    repo = MediaServerInstanceRepository(schema_session)
    first, second = repo.create(**payload(kind)), repo.create(**payload(kind, 'B'))
    monkeypatch.setitem(config.settings.general, 'use_' + kind, True)
    configuration = module.NativeConfiguration(settings(), snapshots=repo.snapshots(settings()))
    monkeypatch.setattr(module, '_configuration', configuration)
    first_id = first.id
    calls, started, release = [], Event(), Event()
    class Client:
        def __init__(self, _kind, snapshot):
            self.snapshot = snapshot
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def get_libraries(self):
            return [{'id': '7', 'type': 'movies', 'paths': ['/media/A']},
                    {'id': '8', 'type': 'movies', 'paths': ['/media/B']}]
        def refresh_movie(self, path, *, ensure_current):
            return self.refresh_file(None, path, ensure_current=ensure_current)
        def refresh_file(self, library_id, path, *, ensure_current):
            if self.snapshot.id == first_id and not started.is_set():
                started.set()
                assert release.wait(5)
            ensure_current()
            calls.append((self.snapshot.id, self.snapshot.apikey, path, library_id))
            return {'status': 'requested' if kind == 'emby' else 'confirmed'}
    dispatch = module.RefreshDispatcher(configuration, client_factory=Client)
    yield SimpleNamespace(repo=repo, config=configuration, dispatcher=dispatch, calls=calls,
                          first=first.id, second=second.id, started=started, release=release, kind=kind)
    release.set()
    assert dispatch.wait_idle(5)


def event(owner=29, **changes):
    from dataclasses import replace
    from media_servers.events import SubtitleMutation
    return replace(SubtitleMutation('movie', '/movies/A.mkv', '/movies/A.en.srt', 'upload', owner), **changes)


@pytest.mark.parametrize('blocked_pair', ['emby', 'silo'], indirect=True)
@pytest.mark.parametrize('operation', ['edit', 'disable', 'delete'])
def test_blocked_destination_edit_disable_delete_preserves_working_sibling(blocked_pair, schema_session, operation):
    from media_servers import service
    pair = blocked_pair
    pair.dispatcher.notify(event())
    assert pair.started.wait(3)
    assert pair.dispatcher.wait_idle(3, server=pair.second)
    assert len(pair.calls) == 1 and pair.calls[0][0] == pair.second
    sibling_revision = pair.config.read(pair.second)[0]
    if operation == 'delete':
        assert service.delete_instance(schema_session, pair.first)[1] == 204
        replacement, status = service.create_instance(schema_session, payload(pair.kind))
        assert status == 201 and replacement['id'] != pair.first
    else:
        changes = {'api_key': 'new-key', 'url': 'http://edited.example/base'} if operation == 'edit' else {'enabled': False}
        assert service.update_instance(schema_session, pair.first, changes)[1] == 200
    assert pair.config.read(pair.second)[0] == sibling_revision
    pair.release.set()
    assert pair.dispatcher.wait_idle(3)
    assert len(pair.calls) == 1
    if operation == 'delete':
        from media_servers.http import MediaServerError
        with pytest.raises(MediaServerError, match='not_found'):
            pair.dispatcher.status(pair.first)
        assert pair.first not in pair.dispatcher.servers
        assert pair.dispatcher.status(replacement['id'])['pending'] == 0
    else:
        assert pair.dispatcher.status(pair.first)['pending'] == 1
        if operation == 'disable':
            assert pair.dispatcher.retry(pair.first) == 0
            assert service.update_instance(schema_session, pair.first, {'enabled': True})[1] == 200
        assert pair.dispatcher.retry(pair.first) == 1
        assert pair.dispatcher.wait_idle(3)
        assert [call[0] for call in pair.calls] == [pair.second, pair.first]
        if operation == 'edit':
            assert pair.calls[-1][1] == 'new-key'


@pytest.mark.parametrize('blocked_pair', ['emby', 'silo'], indirect=True)
def test_later_publication_and_arr_owners_remain_independent(blocked_pair):
    pair = blocked_pair
    pair.dispatcher.notify(event(29))
    assert pair.started.wait(3)
    assert pair.dispatcher.wait_idle(3, server=pair.second)
    pair.dispatcher.notify(event(29))
    pair.dispatcher.notify(event(30))
    pair.release.set()
    assert pair.dispatcher.wait_idle(3)
    assert len([call for call in pair.calls if call[0] == pair.first]) == 3
    assert len([call for call in pair.calls if call[0] == pair.second]) >= 2
    assert pair.dispatcher.status(pair.first)['pending'] == pair.dispatcher.status(pair.second)['pending'] == 0


@pytest.mark.parametrize('blocked_pair', ['silo'], indirect=True)
def test_unsupported_warning_retry_and_deletion_are_destination_scoped(blocked_pair, schema_session):
    from media_servers import service
    from media_servers.http import MediaServerError
    pair = blocked_pair
    pair.release.set()
    assert service.update_instance(schema_session, pair.second, {'enabled': False})[1] == 200
    pair.dispatcher.notify(event(subtitle_path='/movies/subtitles/A.hu.srt'))
    assert pair.dispatcher.wait_idle(3)
    assert pair.calls == []
    warning = {'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}
    assert pair.dispatcher.status(pair.first) == warning

    assert service.update_instance(schema_session, pair.second, {'enabled': True})[1] == 200
    pair.dispatcher.notify(event(owner=30))
    assert pair.dispatcher.wait_idle(3)
    assert pair.dispatcher.status(pair.first) == warning
    pair.dispatcher.notify(event())
    assert pair.dispatcher.wait_idle(3)
    assert pair.dispatcher.status(pair.first) == warning
    confirmed = {'pending': 0, 'state': 'confirmed', 'error_code': None}
    assert pair.dispatcher.status(pair.second) == confirmed
    assert [row[0] for row in pair.calls].count(pair.first) == 2
    assert [row[0] for row in pair.calls].count(pair.second) == 2
    before = list(pair.calls)
    assert pair.dispatcher.retry(pair.first) == 1
    assert pair.dispatcher.retry(pair.second) == 0
    assert pair.dispatcher.wait_idle(3)
    assert pair.calls == before
    assert pair.dispatcher.status(pair.first) == warning
    assert pair.dispatcher.status(pair.second) == confirmed

    assert service.delete_instance(schema_session, pair.first)[1] == 204
    replacement, status = service.create_instance(schema_session, payload('silo'))
    assert status == 201 and replacement['id'] != pair.first
    for operation in (pair.dispatcher.status, pair.dispatcher.retry):
        with pytest.raises(MediaServerError, match='not_found'):
            operation(pair.first)
    assert pair.dispatcher.status(replacement['id']) == {'pending': 0, 'state': 'idle', 'error_code': None}
    assert pair.dispatcher.retry(replacement['id']) == 0
    pair.dispatcher.notify(event())
    assert pair.dispatcher.wait_idle(3)
    assert pair.first not in pair.dispatcher.servers
    assert pair.dispatcher.status(replacement['id']) == confirmed
    assert pair.dispatcher.status(pair.second) == confirmed


@pytest.mark.parametrize('blocked_pair', ['silo'], indirect=True)
def test_deleted_inflight_destination_cannot_restore_unsupported_warning(blocked_pair, schema_session):
    from media_servers import service
    from media_servers.http import MediaServerError
    pair = blocked_pair
    pair.dispatcher.notify(event())
    assert pair.started.wait(10)
    assert pair.dispatcher.wait_idle(10, server=pair.second)
    pair.dispatcher.notify(event(subtitle_path='/movies/subtitles/A.hu.srt'))
    assert pair.dispatcher.wait_idle(10, server=pair.second)
    assert pair.dispatcher.status(pair.first)['error_code'] == 'sidecar_unsupported'
    warning = {'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}
    assert pair.dispatcher.status(pair.second) == warning
    assert service.delete_instance(schema_session, pair.first)[1] == 204
    replacement, status = service.create_instance(schema_session, payload('silo'))
    assert status == 201 and replacement['id'] != pair.first
    pair.release.set()
    assert pair.dispatcher.wait_idle(10)
    assert pair.first not in pair.dispatcher.servers
    assert all(row[0] != pair.first for row in pair.calls)
    with pytest.raises(MediaServerError, match='not_found'):
        pair.dispatcher.retry(pair.first)
    assert pair.dispatcher.retry(replacement['id']) == 0
    assert pair.dispatcher.status(replacement['id']) == {'pending': 0, 'state': 'idle', 'error_code': None}
    assert pair.dispatcher.status(pair.second) == warning


@pytest.mark.parametrize('blocked_pair', ['emby', 'silo'], indirect=True)
def test_disjoint_root_is_not_work_and_ambiguous_matching_instance_is_isolated(blocked_pair):
    pair = blocked_pair
    pair.release.set()
    row = pair.repo.get(pair.first)
    mappings = pair.repo.values(row)['path_mappings']
    mappings.append(dict(mappings[0]))
    pair.repo.update(pair.first, path_mappings=mappings)
    pair.config.publish(pair.repo.snapshot(pair.first, settings()))
    pair.dispatcher.notify(event())
    assert pair.dispatcher.wait_idle(3)
    assert [call[0] for call in pair.calls] == [pair.second]
    assert pair.dispatcher.status(pair.first)['error_code'] == 'mapping_ambiguous'
    before = tuple(pair.calls)
    pair.dispatcher.notify(event(video_path='/unrelated/A.mkv'))
    assert pair.dispatcher.wait_idle(3)
    assert tuple(pair.calls) == before
    assert pair.dispatcher.status(pair.first)['pending'] == 1


@pytest.mark.parametrize('blocked_pair', ['emby', 'silo'], indirect=True)
def test_failed_database_update_keeps_both_authorized_snapshots(blocked_pair, schema_session, monkeypatch):
    from sqlalchemy import event as sql_event
    from media_servers import service
    pair = blocked_pair
    before = pair.config.read(pair.first)
    sibling = pair.config.read(pair.second)
    session = schema_session()
    def fail_after_flush(_session, _context):
        raise RuntimeError('sensitive synthetic text')
    sql_event.listen(session, 'after_flush', fail_after_flush)
    try:
        body, status = service.update_instance(schema_session, pair.first, {'url': 'http://staged.example',
                                                                           'api_key': 'staged-key'})
    finally:
        sql_event.remove(session, 'after_flush', fail_after_flush)
    assert status == 500 and body == {'error_code': 'persistence_failed'}
    assert pair.config.read(pair.first) == before and pair.config.read(pair.second) == sibling
    assert pair.repo.get(pair.first).url == before[1].url
    assert pair.repo.get_decrypted_api_key(pair.first) == before[1].apikey
    pair.config.ensure_current(pair.first, before[0])
    pair.release.set()
    pair.dispatcher.notify(event())
    assert pair.dispatcher.wait_idle(3)
    assert {call[0] for call in pair.calls} == {pair.first, pair.second}


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_enabled_incomplete_legacy_values_are_retained_but_never_requested(schema_session, kind):
    from media_servers.repository import MediaServerInstanceRepository
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    repo = MediaServerInstanceRepository(schema_session)
    row = repo.import_values(payload(kind, url='', path_mappings=[]))
    configuration = NativeConfiguration(settings(), snapshots=repo.snapshots(settings()))
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *args: pytest.fail('invalid config requested'))
    assert dispatcher.status(row.id) == {'pending': 0, 'state': 'unconfirmed', 'error_code': 'invalid_url'}
    dispatcher.notify(event())
    assert dispatcher.wait_idle(3)
    assert dispatcher.status(row.id) == {'pending': 0, 'state': 'unconfirmed', 'error_code': 'invalid_url'}
    assert repo.get_decrypted_api_key(row.id) == 'opaque-A'


def test_replacement_key_recovers_unreadable_saved_ciphertext(schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    from secret_store import encrypt_secret
    repo = MediaServerInstanceRepository(schema_session)
    row = repo.create(**payload('emby'))
    row.api_key = encrypt_secret('old-key', master_key='different-test-master')
    schema_session.flush()
    assert repo.snapshot(row.id, settings()).configuration_error == 'missing_credentials'
    repo.update(row.id, api_key='replacement-key')
    assert repo.get_decrypted_api_key(row.id) == 'replacement-key'
    assert repo.snapshot(row.id, settings()).configuration_error is None


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_invalid_imported_destination_preserves_capacity_for_its_configured_root(schema_session, kind):
    from media_servers.repository import MediaServerInstanceRepository
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    repo = MediaServerInstanceRepository(schema_session)
    row = repo.import_values(payload(kind, url=''))
    configuration = NativeConfiguration(settings(), snapshots=repo.snapshots(settings()))
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *args: pytest.fail('invalid connection requested'))
    for number in range(dispatcher.PENDING_LIMIT + 2):
        dispatcher.notify(event(video_path=f'/unrelated/{number}.mkv',
                                subtitle_path=f'/unrelated/{number}.en.srt'))
    assert dispatcher.wait_idle(3)
    assert dispatcher.status(row.id) == {'pending': 0, 'state': 'unconfirmed', 'error_code': 'invalid_url'}
    dispatcher.notify(event())
    assert dispatcher.wait_idle(3)
    assert dispatcher.status(row.id) == {'pending': 1, 'state': 'unconfirmed', 'error_code': 'invalid_url'}
    assert repo.get_decrypted_api_key(row.id) == 'opaque-A'
