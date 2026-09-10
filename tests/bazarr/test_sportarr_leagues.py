"""Sports settings and atomic, owner-scoped league reconciliation."""
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def library(migration_engine, monkeypatch):  # noqa: F811
    from app.database import Base, TableArrInstances, TableLanguagesProfiles
    from sportarr.sync import leagues
    Base.metadata.create_all(migration_engine)
    session = sessionmaker(bind=migration_engine, expire_on_commit=False)()
    with migration_engine.connect() as conn:
        conn.execute(sa.insert(TableArrInstances), [dict(kind='sportarr', name=str(i), stable_key=str(i), port=1867) for i in [1, 2]])
        conn.execute(sa.insert(TableLanguagesProfiles), [dict(profileId=1, name='One', items='[]'), dict(profileId=2, name='Two', items='[]')])
    monkeypatch.setattr(leagues, 'database', session)
    monkeypatch.setattr(leagues, 'notify', lambda *a: None)
    yield session, leagues
    session.close()


def payload(name='League', **kwargs):
    return dict(id=7, name=name, description='Native description', path=None, monitored=True,
                sport='Football', posterUrl='https://images.example/poster.jpg',
                bannerUrl='https://images.example/banner.jpg', tags=[], **kwargs)


def remote(monkeypatch, leagues, data, status=200):
    calls = []
    def get(path):
        calls.append(path)
        return SimpleNamespace(status_code=status, json=lambda: data)
    monkeypatch.setattr(leagues.ArrClientFactory, 'from_row', lambda *a: SimpleNamespace(get=get))
    return calls


def test_sync_owner_collision_profile_and_nullable_path(library, monkeypatch):
    from app.database import TableArrInstances, TableSportsLeagues
    session, leagues = library
    session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(options=json.dumps({'media_defaults': {'default_enabled': True, 'default_profile': 1}})))
    calls = remote(monkeypatch, leagues, [payload()])
    first = leagues.sync_leagues(1)
    second = leagues.sync_leagues(2)
    assert first != second and calls == ['/api/leagues', '/api/leagues']
    a, b = session.execute(sa.select(TableSportsLeagues).order_by(TableSportsLeagues.id)).scalars().all()
    assert (a.arr_instance_id, b.arr_instance_id) == (1, 2)
    assert a.path is None and a.overview == 'Native description'
    assert a.sortTitle == 'League' and a.fanart == 'https://images.example/banner.jpg'
    assert a.profileId == 1 and b.profileId is None
    session.execute(sa.update(TableSportsLeagues).where(TableSportsLeagues.id == a.id).values(profileId=2))
    remote(monkeypatch, leagues, [payload('Renamed')])
    assert leagues.sync_leagues(1) == first
    session.expire_all()
    assert session.get(TableSportsLeagues, a.id).profileId == 2
    remote(monkeypatch, leagues, [])
    leagues.sync_leagues(1)
    assert session.execute(sa.select(TableSportsLeagues.id)).scalars().all() == second


@pytest.mark.parametrize('data,status', [({}, 200), (None, 200), ([], 503), ([payload(), payload()], 200), ([{'id': 7}], 200)])
def test_failed_response_never_prunes(library, monkeypatch, data, status):
    from app.database import TableSportsLeagues
    session, leagues = library
    remote(monkeypatch, leagues, [payload()])
    ids = leagues.sync_leagues(1)
    remote(monkeypatch, leagues, data, status)
    with pytest.raises(ValueError):
        leagues.sync_leagues(1)
    assert session.execute(sa.select(TableSportsLeagues.id)).scalars().all() == ids


def test_disabled_owner_and_transaction_failure(library, monkeypatch):
    from app.database import TableArrInstances, TableSportsLeagues
    session, leagues = library
    remote(monkeypatch, leagues, [payload()])
    ids = leagues.sync_leagues(1)
    session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 2).values(enabled=0))
    with pytest.raises(ValueError):
        leagues.sync_leagues(2)
    remote(monkeypatch, leagues, [payload('Replacement')])
    def fail_after_write(*args, **kwargs):
        raise RuntimeError('injected after mutation')
    monkeypatch.setattr(leagues, '_prune_leagues', fail_after_write)
    with pytest.raises(RuntimeError):
        leagues.sync_leagues(1)
    session.expire_all()
    assert session.get(TableSportsLeagues, ids[0]).title == 'League'


def test_sports_settings_merge_and_validation(schema_session):
    from arr_instances.service import create_instance, update_instance
    from arr_instances.repository import ArrInstanceRepository
    body, status = create_instance(schema_session, {'kind': 'sportarr', 'name': 'Sports', 'sports_settings': {'sports_sync': 13, 'excluded_sports': ['Golf']}, 'subtitle_settings': {'general': {'use_postprocessing': True}}})
    assert status == 201 and body['sports_settings']['sports_sync'] == 13

    # The submitted blob is the complete set of overrides, not an addition to
    # what is stored. This used to union with the previous value, which meant
    # an override could never go back to inheriting the global: the UI drops a
    # key when the operator switches that override off, and the union put it
    # straight back.
    changed, status = update_instance(schema_session, body['id'], {'sports_settings': {'full_update': 'Weekly'}})
    assert status == 200
    assert changed['sports_settings'] == {'full_update': 'Weekly'}
    # A different blob on the same instance leaves its neighbours alone.
    assert changed['subtitle_settings']['general']['use_postprocessing'] is True
    opts = json.loads(ArrInstanceRepository(schema_session).get(body['id']).options)
    assert opts['sports_settings'] == {'full_update': 'Weekly'}

    # And an empty blob clears the block entirely, which is how the last
    # remaining override is removed.
    cleared, status = update_instance(schema_session, body['id'], {'sports_settings': {}})
    assert status == 200 and cleared['sports_settings'] == {}
    opts = json.loads(ArrInstanceRepository(schema_session).get(body['id']).options)
    assert 'sports_settings' not in opts
    assert opts['subtitle_settings']['general']['use_postprocessing'] is True
    for bad in [{'sports_sync': 0}, {'sports_sync': True}, {'full_update': 'Hourly'}, {'full_update_hour': 24}, {'full_update_day': -1}, {'only_monitored': 1}, {'excluded_tags': 'tag'}, {'api_key': 'private'}]:
        _, status = update_instance(schema_session, body['id'], {'sports_settings': bad})
        assert status == 400


def test_apply_sports_profile_and_remove_only_its_library(schema_session):
    from app.database import TableSportsLeagues, TableLanguagesProfiles
    from arr_instances.repository import ArrInstanceRepository
    from arr_instances.service import apply_default_profile
    repo = ArrInstanceRepository(schema_session)
    first = repo.create('sportarr', 'One', options=json.dumps({'media_defaults': {'default_enabled': True, 'default_profile': 1}}))
    second = repo.create('sportarr', 'Two')
    schema_session.execute(sa.insert(TableLanguagesProfiles).values(profileId=1, name='One', items='[]'))
    for owner in [first.id, second.id]:
        schema_session.execute(sa.insert(TableSportsLeagues).values(arr_instance_id=owner, sportarrLeagueId=7, title='League'))
    body, status = apply_default_profile(schema_session, first.id)
    assert status == 200 and body['updated'] == 1
    assert schema_session.execute(sa.select(TableSportsLeagues.profileId).order_by(TableSportsLeagues.id)).scalars().all() == [1, None]
    repo.delete(first.id)
    assert schema_session.execute(sa.select(TableSportsLeagues.arr_instance_id)).scalars().all() == [second.id]


@pytest.mark.parametrize('change', [{'enabled': 0}, {'ip': 'changed.example'}])
def test_connection_or_disable_during_fetch_rejects_stale_response(library, monkeypatch, change):
    from app.database import TableArrInstances, TableSportsLeagues
    session, leagues = library
    remote(monkeypatch, leagues, [payload()])
    ids = leagues.sync_leagues(1)
    def get(path):
        session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(**change))
        return SimpleNamespace(status_code=200, json=lambda: [])
    monkeypatch.setattr(leagues.ArrClientFactory, 'from_row', lambda *a: SimpleNamespace(get=get))
    with pytest.raises(ValueError):
        leagues.sync_leagues(1)
    assert session.execute(sa.select(TableSportsLeagues.id)).scalars().all() == ids


def test_jobs_only_enabled_sportarr_and_stale_removal(schema_session, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.scheduler import configure_sports_jobs
    from app.config import settings
    monkeypatch.setattr(settings.general, "upgrade_subs", True)
    # configure_sports_jobs is gated on the master toggle, so a job test
    # has to turn it on the way a configured install does.
    monkeypatch.setattr(settings.general, "use_sportarr", True)
    repo = ArrInstanceRepository(schema_session)
    aps = BackgroundScheduler()
    configure_sports_jobs(aps, schema_session)
    assert aps.get_jobs() == []
    repo.create('sonarr', 'TV')
    first = repo.create('sportarr', 'One', options=json.dumps({'sports_settings': {'sports_sync': 13}}))
    second = repo.create('sportarr', 'Two')
    disabled = repo.create('sportarr', 'Disabled', enabled=False)
    configure_sports_jobs(aps, schema_session)
    assert {j.id for j in aps.get_jobs()} == {f'update_sports_{first.id}', f'update_sports_{second.id}',
                                             f'sports_full_scan_subtitles_{first.id}',
                                             f'sports_full_scan_subtitles_{second.id}',
                                             f'wanted_search_missing_subtitles_sports_{first.id}',
                                             f'wanted_search_missing_subtitles_sports_{second.id}',
                                             f'upgrade_sports_subtitles_{first.id}',
                                             f'upgrade_sports_subtitles_{second.id}', 'sportarr_clients'}
    assert aps.get_job(f'update_sports_{first.id}').trigger.interval.total_seconds() == 13 * 60
    repo.update(first.id, enabled=False)
    repo.delete(second.id)
    configure_sports_jobs(aps, schema_session)
    assert aps.get_jobs() == []
    repo.update(disabled.id, enabled=True)
    configure_sports_jobs(aps, schema_session)
    assert {j.id for j in aps.get_jobs()} == {f'update_sports_{disabled.id}',
                                             f'sports_full_scan_subtitles_{disabled.id}',
                                             f'wanted_search_missing_subtitles_sports_{disabled.id}',
                                             f'upgrade_sports_subtitles_{disabled.id}', 'sportarr_clients'}


def test_league_api_scope_and_profile_validation(schema_session):
    from app.database import TableSportsLeagues, TableLanguagesProfiles
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.library import list_leagues, get_league, assign_profile
    repo = ArrInstanceRepository(schema_session)
    a = repo.create('sportarr', 'A')
    b = repo.create('sportarr', 'B')
    schema_session.execute(sa.insert(TableSportsLeagues), [dict(id=i, arr_instance_id=o, sportarrLeagueId=7, title=t) for i, o, t in [(51, a.id, 'A'), (52, b.id, 'B')]])
    schema_session.execute(sa.insert(TableLanguagesProfiles).values(profileId=1, name='English', items='[]'))
    assert [r['id'] for r in list_leagues(schema_session, a.id)['data']] == [51]
    assert get_league(schema_session, 51, b.id) is None
    assert assign_profile(schema_session, 51, b.id, 1) is False
    with pytest.raises(ValueError):
        assign_profile(schema_session, 51, a.id, 999)
    assert assign_profile(schema_session, 51, a.id, 1) is True
    assert get_league(schema_session, 51, a.id)['profileId'] == 1
    assert get_league(schema_session, 52, b.id)['profileId'] is None
    repo.update(a.id, enabled=False)
    assert [r['id'] for r in list_leagues(schema_session)['data']] == [52]
    with pytest.raises(ValueError):
        list_leagues(schema_session, a.id)


def test_authenticated_local_league_endpoints(schema_session, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys
    from types import ModuleType
    from flask import Flask
    from flask_restx import Api
    from app.config import settings
    from app.database import TableSportsLeagues, TableLanguagesProfiles
    from arr_instances.repository import ArrInstanceRepository
    from sportarr import library

    root = Path(__file__).resolve().parents[2] / 'bazarr' / 'api'
    for name in ('_sports_api', '_sports_api.sports'):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in [('_sports_api.utils', root / 'utils.py'), ('_sports_api.sports.leagues', root / 'sports' / 'leagues.py')]:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    repo = ArrInstanceRepository(schema_session)
    a, b = repo.create('sportarr', 'A'), repo.create('sportarr', 'B')
    schema_session.execute(sa.insert(TableSportsLeagues).values(id=51, arr_instance_id=a.id, sportarrLeagueId=7, title='League'))
    schema_session.execute(sa.insert(TableLanguagesProfiles).values(profileId=1, name='English', items='[]'))
    monkeypatch.setattr(module, 'database', schema_session)
    queued = []
    # Returns a job id, as the real feed_jobs_pending_queue does. A stub
    # returning None (list.append does) now reads as "nothing was queued",
    # because the route reports the queue's answer instead of assuming 202.
    def _queue_stub(**kwargs):
        queued.append(kwargs)
        return len(queued)

    monkeypatch.setattr(module.jobs_queue, 'feed_jobs_pending_queue', _queue_stub)
    monkeypatch.setattr(library, 'refresh_league_profiles', lambda *a: None)
    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(module.api_ns_sports_leagues, path='/')
    client = app.test_client()
    headers = {'X-API-KEY': settings.auth.apikey}
    assert client.get('/sports/leagues').status_code == 401
    assert client.get('/sports/leagues/51').status_code == 401
    assert client.patch('/sports/leagues/51', json={}).status_code == 401
    assert client.post('/sports/leagues/sync', json={}).status_code == 401
    assert client.get('/sports/leagues', headers=headers).json['total'] == 1
    assert client.get('/sports/leagues/7', headers=headers).status_code == 404
    assert client.get('/sports/leagues/51', headers=headers).json['arr_instance_id'] == a.id
    assert client.get(f'/sports/leagues/51?arr_instance_id={b.id}', headers=headers).status_code == 404
    assert client.patch('/sports/leagues/51', headers=headers, json={'arr_instance_id': b.id, 'profileId': 1}).status_code == 404
    assert client.patch('/sports/leagues/51', headers=headers, json={'arr_instance_id': a.id, 'profileId': 1}).status_code == 204
    assert client.get('/sports/leagues/51', headers=headers).json['profileId'] == 1
    for bad in [None, {}, {'arr_instance_id': True}, {'arr_instance_id': a.id, 'profileId': '1'}]:
        assert client.patch('/sports/leagues/51', headers=headers, json=bad).status_code == 400
    assert client.post('/sports/leagues/sync', headers=headers, json={'arr_instance_id': a.id}).status_code == 202
    assert queued[-1]['kwargs'] == {'arr_instance_id': a.id}
    repo.update(a.id, enabled=False)
    assert client.post('/sports/leagues/sync', headers=headers, json={'arr_instance_id': a.id}).status_code == 400
    assert client.get('/sports/leagues', headers=headers).json['total'] == 0


def test_sportarr_crud_and_profile_application_under_real_autocommit(library):
    from app.database import TableSportsLeagues, TableHistorySports, TableSportsEvents
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository
    session, _ = library
    body, status = service.create_instance(session, {'kind': 'sportarr', 'name': 'Third', 'media_defaults': {'default_enabled': True, 'default_profile': 1}})
    assert status == 201
    owner = body['id']
    body, status = service.update_instance(session, owner, {'sports_settings': {'sports_sync': 17}})
    assert status == 200 and body['sports_settings']['sports_sync'] == 17
    session.execute(sa.insert(TableSportsLeagues).values(id=51, arr_instance_id=owner, sportarrLeagueId=7, title='Third'))
    session.execute(sa.insert(TableSportsEvents).values(id=51, arr_instance_id=owner, league_id=51, sportarrEventId=8, file_id=9, path='/third.mkv', title='Third'))
    session.execute(sa.insert(TableHistorySports).values(arr_instance_id=owner, league_id=51, event_id=51))
    body, status = service.apply_default_profile(session, owner)
    assert status == 200 and body['updated'] == 1
    assert session.execute(sa.select(TableSportsLeagues.profileId).where(TableSportsLeagues.id == 51)).scalar() == 1
    assert service.delete_instance(session, owner)[1] == 204
    assert ArrInstanceRepository(session).get(owner) is None
    assert session.execute(sa.select(sa.func.count()).select_from(TableHistorySports)).scalar() == 0


def test_failed_sports_update_rolls_back_all_fields(library, monkeypatch):
    from app.database import TableArrInstances
    from arr_instances import service
    session, _ = library
    body, status = service.create_instance(session, {'kind': 'sportarr', 'name': 'Third'})
    assert status == 201
    owner = body['id']
    from arr_instances.repository import ArrInstanceRepository
    def fail_election(*args, **kwargs):
        raise ValueError('injected election failure')
    monkeypatch.setattr(ArrInstanceRepository, '_reconcile_default', fail_election)
    _, status = service.update_instance(session, owner, {'name': 'Must roll back', 'enabled': False})
    assert status == 400
    session.expire_all()
    instance = session.get(TableArrInstances, owner)
    assert instance.name == 'Third' and instance.enabled == 1 and instance.is_default == 1


def test_sportarr_http_crud_on_both_database_engines(library, monkeypatch):
    from flask import Flask
    from flask_restx import Api
    from app.config import settings
    from app.database import TableArrInstances
    from arr_instances import service
    import secret_store
    from test_sportarr_instances import _load_endpoints
    session, _ = library
    endpoints = _load_endpoints(monkeypatch)
    monkeypatch.setattr(endpoints, 'database', session)
    monkeypatch.setattr(service, 'refresh_runtime', lambda *a, **k: None)
    monkeypatch.setattr(secret_store, 'persist_master_key', lambda: None)
    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(endpoints.api_ns_system_arr_instances, path='/')
    client = app.test_client()
    headers = {'X-API-KEY': settings.auth.apikey}
    created = client.post('/system/arr-instances', headers=headers, json={'kind': 'sportarr', 'name': 'HTTP', 'sports_settings': {'sports_sync': 19}})
    assert created.status_code == 201
    owner = created.json['id']
    url = f'/system/arr-instances/{owner}'
    patched = client.patch(url, headers=headers, json={'enabled': False, 'sports_settings': {'full_update': 'Weekly'}})
    assert patched.status_code == 200 and patched.json['enabled'] is False
    # Replacement, not a union: the blob names every override that should
    # survive, so sports_sync is gone because the patch did not resend it.
    assert patched.json['sports_settings'] == {'full_update': 'Weekly'}
    assert client.patch(url, headers=headers, json={'enabled': True, 'is_default': True}).status_code == 200
    assert client.delete(url, headers=headers).status_code == 204
    assert session.execute(sa.select(TableArrInstances.id).order_by(TableArrInstances.id)).scalars().all() == [1, 2]


@pytest.mark.parametrize('change', [{'enabled': 0}, {'ip': 'changed-after-check.example'}],
                         ids=['disable', 'connection-edit'])
@pytest.mark.parametrize('operation', ['sync', 'assign-profile', 'apply-default'])
def test_owner_changes_serialize_after_checked_sports_writes(library, monkeypatch, change, operation):
    """A separate writer cannot change the checked owner before the sports commit."""
    import threading
    from app.database import TableArrInstances, TableSportsLeagues
    from sportarr import library as sports_library

    session, leagues = library
    engine = session.get_bind()
    session.execute(sa.insert(TableSportsLeagues).values(
        id=51, arr_instance_id=1, sportarrLeagueId=7, title='Before'))
    session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(
        options=json.dumps({'media_defaults': {'default_enabled': True, 'default_profile': 1}})))
    remote(monkeypatch, leagues, [])
    attempted, allow_retry = threading.Event(), threading.Event()
    outcome = {}
    failures = []
    workers = []
    original = leagues.require_sportarr

    def competing_edit():
        try:
            with engine.connect() as connection:
                if engine.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA busy_timeout=0')
                else:
                    connection.exec_driver_sql("SET lock_timeout='100ms'")
                statement = sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(**change)
                try:
                    connection.execute(statement)
                    outcome['first_write'] = 'committed'
                except sa.exc.OperationalError as exc:
                    if engine.dialect.name == 'sqlite':
                        assert exc.orig.sqlite_errorcode == 5
                    else:
                        assert exc.orig.sqlstate == '55P03'
                    connection.rollback()
                    outcome['first_write'] = 'blocked'
                attempted.set()
                assert allow_retry.wait(5), 'Sports operation did not finish'
                connection.execute(statement)
                outcome['after_commit'] = 'committed'
        except BaseException as exc:
            failures.append(exc)
            attempted.set()

    def check_then_compete(transaction, owner):
        row = original(transaction, owner)
        if transaction is not session:
            if engine.dialect.name == 'sqlite':
                outcome['sqlite_transaction_at_check'] = transaction.connection().connection.driver_connection.in_transaction
            worker = threading.Thread(target=competing_edit)
            workers.append(worker)
            worker.start()
            assert attempted.wait(5), 'Competing owner write did not run'
        return row

    monkeypatch.setattr(leagues, 'require_sportarr', check_then_compete)
    monkeypatch.setattr(sports_library, 'require_sportarr', check_then_compete)
    try:
        if operation == 'sync':
            assert leagues.sync_leagues(1) == []
        elif operation == 'assign-profile':
            assert sports_library.assign_profile(session, 51, 1, 1)
        else:
            assert sports_library.apply_instance_default_profile(session, 1)['updated'] == 1
    finally:
        allow_retry.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
    assert not failures
    assert outcome['first_write'] == 'blocked'
    assert outcome['after_commit'] == 'committed'
    if engine.dialect.name == 'sqlite':
        assert outcome['sqlite_transaction_at_check'] is True
    with engine.connect() as connection:
        for name, value in change.items():
            assert connection.execute(sa.select(getattr(TableArrInstances, name)).where(
                TableArrInstances.id == 1)).scalar_one() == value
        profiles = connection.execute(sa.select(TableSportsLeagues.profileId).where(
            TableSportsLeagues.id == 51)).scalars().all()
        assert profiles == ([] if operation == 'sync' else [1])
