"""Queued request work and concurrent owner-scoped provider exclusions."""

from concurrent.futures import ThreadPoolExecutor
import ast
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session, sessionmaker
from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_webhook import webhook, _handler  # noqa: F401
from test_sportarr_workflows import private_queue


@pytest.fixture
def callback_library(migration_engine, monkeypatch):  # noqa: F811
    from app import database as db
    from sportarr import history

    db.Base.metadata.create_all(migration_engine)
    session = scoped_session(sessionmaker(bind=migration_engine))
    for owner in (1, 2):
        session.execute(sa.insert(db.TableArrInstances).values(
            id=owner, kind='sportarr', name=str(owner), stable_key=str(owner), port=1867))
        session.execute(sa.insert(db.TableSportsLeagues).values(
            id=50 + owner, arr_instance_id=owner, sportarrLeagueId=7, title='League'))
        session.execute(sa.insert(db.TableSportsEvents).values(
            id=60 + owner, league_id=50 + owner, arr_instance_id=owner,
            sportarrEventId=9, file_id=71, title='Event', path='/sports/event.mkv'))
    session.commit()
    monkeypatch.setattr(history, 'database', session)
    monkeypatch.setattr(history, 'notify', lambda *args: None)
    try:
        yield history, session, migration_engine
    finally:
        session.remove()


def test_provider_exclusion_is_idempotent_owned_and_removable(callback_library):
    from app.database import TableBlacklistSports
    from sportarr.identity import resolve_event_in_session

    history, session, _ = callback_library
    owner_one = resolve_event_in_session(session, 61, 1)
    owner_two = resolve_event_in_session(session, 62, 2)
    history.blacklist_log_sports(owner_one, 'provider', 'release', 'en:hi')
    history.blacklist_log_sports(owner_one, 'provider', 'release', 'de:forced')
    history.blacklist_log_sports(owner_two, 'provider', 'release', 'de:forced')
    rows = session.execute(sa.select(TableBlacklistSports).order_by(
        TableBlacklistSports.arr_instance_id)).scalars().all()
    assert [(r.arr_instance_id, r.event_id, r.language) for r in rows] == [
        (1, 61, 'en:hi'), (2, 62, 'de:forced')]
    assert history.remove_blacklist(session, rows[0].id, 1)
    history.blacklist_log_sports(owner_one, 'provider', 'release', 'en:hi')
    assert session.execute(sa.select(sa.func.count()).select_from(TableBlacklistSports)).scalar_one() == 2


@pytest.mark.parametrize('change', [{'arr_instance_id': 2}, {'league_id': 52}])
def test_provider_exclusion_revalidates_context(callback_library, change):
    from app.database import TableBlacklistSports
    from sportarr.identity import resolve_event_in_session

    history, session, _ = callback_library
    context = replace(resolve_event_in_session(session, 61, 1), **change)
    with pytest.raises(ValueError):
        history.blacklist_log_sports(context, 'provider', 'release', 'en')
    assert session.execute(sa.select(TableBlacklistSports)).all() == []


def test_overlapping_provider_callbacks_publish_one_exclusion(callback_library):
    from app.database import TableBlacklistSports
    from sportarr.identity import resolve_event_in_session

    history, session, engine = callback_library
    context = resolve_event_in_session(session, 61, 1)
    first_inserting = Event()
    second_attempting = Event()
    allow_first_commit = Event()

    def overlap(connection, cursor, statement, parameters, execution_context, many):
        if statement.lstrip().upper().startswith('INSERT INTO TABLE_BLACKLIST_SPORTS'):
            if not first_inserting.is_set():
                first_inserting.set()
                assert allow_first_commit.wait(5)
        elif first_inserting.is_set():
            if statement == 'BEGIN IMMEDIATE' or 'FOR UPDATE' in statement:
                second_attempting.set()

    # ORM table names are an implementation detail of SQL logging only.
    table_name = TableBlacklistSports.__tablename__.upper()

    def capture(*args):
        statement = args[2].replace(table_name.lower(), 'table_blacklist_sports')
        overlap(*args[:2], statement, *args[3:])

    def callback():
        try:
            history.blacklist_log_sports(context, 'provider', 'release', 'en:hi')
        finally:
            session.remove()

    sa.event.listen(engine, 'before_cursor_execute', capture)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(callback)
            try:
                assert first_inserting.wait(5), 'first callback did not reach publication'
                second = pool.submit(callback)
                assert second_attempting.wait(5), 'callbacks did not overlap in the transaction'
            finally:
                allow_first_commit.set()
            first.result(timeout=5)
            second.result(timeout=5)
    finally:
        sa.event.remove(engine, 'before_cursor_execute', capture)
    assert session.execute(sa.select(sa.func.count()).select_from(TableBlacklistSports)).scalar_one() == 1


@pytest.mark.parametrize('sqlstate, attempts', [('40001', 3), ('08006', 1)])
def test_provider_exclusion_retries_only_serialization_and_is_bounded(
    callback_library, monkeypatch, sqlstate, attempts
):
    from sportarr.identity import resolve_event_in_session

    history, session, _ = callback_library
    context = resolve_event_in_session(session, 61, 1)
    calls = []

    def fail(*args):
        calls.append(True)
        raise sa.exc.OperationalError('SELECT', {}, SimpleNamespace(sqlstate=sqlstate))

    monkeypatch.setattr(history, 'require_sportarr', fail)
    with pytest.raises(sa.exc.OperationalError):
        history.blacklist_log_sports(context, 'provider', 'release', 'en')
    assert len(calls) == attempts


def test_webhook_queues_owned_cancellable_search_without_provider_work(webhook, monkeypatch):  # noqa: F811
    from api.webhooks import sportarr
    from sportarr import automatic, workflows
    from subtitles.indexer import sports

    session, namespace, indexed = webhook
    queue = private_queue(monkeypatch)
    monkeypatch.setattr(workflows, 'database', session)
    monkeypatch.setattr(workflows, 'jobs_queue', queue)
    monkeypatch.setattr(sports, 'store_subtitles_sports', lambda *args: indexed.append(args))

    def provider(*args, **kwargs):
        pytest.fail('provider search ran in the request')

    monkeypatch.setattr(automatic, 'search_event', provider)
    monkeypatch.setattr(workflows, 'search_event', provider)
    namespace.payload = {'eventType': 'Download', 'episodeFiles': [{'id': 71}]}
    assert _handler()(sportarr.WebHooksSportarr(), 'sportarr-1')[1] == 200
    assert _handler()(sportarr.WebHooksSportarr(), 'sportarr-1')[1] == 200
    assert indexed == [(61, 42), (61, 42)]
    job, = queue.jobs_pending_queue
    assert (job.module, job.func, job.is_progress) == ('sportarr.workflows', 'automatic_search_sports', True)
    assert job.kwargs == {'event_id': 61, 'arr_instance_id': 42, 'job_id': None, 'language': None}
    signals = []

    def worker(event_id, owner, **kwargs):
        signals.append(kwargs['cancel'])
        return {'status': 'no_result', 'downloads': 0, 'message': 'No result'}

    monkeypatch.setattr(workflows, 'search_event', worker)
    workflows.automatic_search_sports(61, 42, job_id=job.job_id)
    assert isinstance(signals[0], workflows.SportsJobSignal)


@pytest.mark.parametrize('queue_failure', [False, True])
def test_batch_profiles_commit_before_best_effort_queue(callback_library, monkeypatch, queue_failure):
    from flask import Flask
    from api.sports import leagues
    from app.database import TableLanguagesProfiles, TableSportsLeagues

    _, session, engine = callback_library
    session.execute(sa.insert(TableLanguagesProfiles).values(profileId=5, name='English', items='[]'))
    monkeypatch.setattr(leagues, 'database', session)
    queued = []

    def enqueue(**kwargs):
        # Independent connection proves assignment is committed before follow-up work.
        with engine.connect() as connection:
            assert connection.execute(sa.select(TableSportsLeagues.profileId)).scalars().all() == [5, 5]
        queued.append(kwargs)
        if queue_failure and len(queued) == 1:
            raise OSError('queue unavailable')
        return 123

    monkeypatch.setattr(leagues.jobs_queue, 'feed_jobs_pending_queue', enqueue)
    monkeypatch.setattr(leagues.library, 'refresh_league_profiles',
                        lambda *args: pytest.fail('profile refresh ran on the request thread'))
    with Flask(__name__).test_request_context(json={
        'id': [51, 52], 'arr_instance_id': [1, 2], 'profileId': [5, 5],
    }):
        body, status = leagues.SportsLeagueProfiles.post.__wrapped__(leagues.SportsLeagueProfiles())
    assert (status, body) == (200, {'updated': 2, 'requested': 2})
    assert len({job['kwargs']['refresh_id'] for job in queued}) == 2
    assert [{key: value for key, value in job['kwargs'].items() if key != 'refresh_id'} for job in queued] == [
        {'league_ids': [51], 'arr_instance_id': 1}, {'league_ids': [52], 'arr_instance_id': 2}]
    assert all(job['module'] == 'sportarr.library' and job['func'] == 'refresh_league_profiles' for job in queued)


def test_new_profile_assignment_retains_refresh_while_previous_job_runs(callback_library, monkeypatch):
    from flask import Flask
    from api.sports import leagues
    from app.database import TableLanguagesProfiles, TableSportsEvents, TableSportsLeagues
    from subtitles.indexer import sports

    _, session, _ = callback_library
    session.execute(sa.insert(TableLanguagesProfiles), [
        {'profileId': 5, 'name': 'English', 'items': '[]'},
        {'profileId': 6, 'name': 'German', 'items': '[]'},
    ])
    session.commit()
    queue = private_queue(monkeypatch)
    monkeypatch.setattr(leagues, 'database', session)
    monkeypatch.setattr(leagues, 'jobs_queue', queue)
    monkeypatch.setattr(sports, 'database', session)
    monkeypatch.setattr(sports, 'notify', lambda *args: None)
    monkeypatch.setattr(sports, 'get_audio_profile_languages', lambda *args: [])
    monkeypatch.setattr(sports, 'get_profile_cutoff', lambda *args: None)
    monkeypatch.setattr(sports, 'get_profiles_list', lambda profile_id: {
        'items': [{'id': 1, 'language': {5: 'en', 6: 'de'}[profile_id],
                   'forced': 'False', 'hi': 'False'}], 'cutoff': None})

    def assign(profile_id):
        with Flask(__name__).test_request_context(json={
            'id': [51], 'arr_instance_id': [1], 'profileId': [profile_id],
        }):
            assert leagues.SportsLeagueProfiles.post.__wrapped__(leagues.SportsLeagueProfiles())[1] == 200

    assign(5)
    first, = queue.jobs_pending_queue
    original = sports.list_missing_subtitles_sports

    def change_after_processed(*args, **kwargs):
        original(*args, **kwargs)
        session.expire_all()
        assert ast.literal_eval(session.get(TableSportsEvents, 61).missing_subtitles) == ['en']
        assign(6)

    monkeypatch.setattr(sports, 'list_missing_subtitles_sports', change_after_processed)
    queue.jobs_pending_queue.remove(first)
    queue.jobs_running_queue.append(first)
    assert queue._run_job(first)
    second, = queue.jobs_pending_queue
    assert second.kwargs['refresh_id'] != first.kwargs['refresh_id']
    monkeypatch.setattr(sports, 'list_missing_subtitles_sports', original)
    queue.jobs_pending_queue.remove(second)
    queue.jobs_running_queue.append(second)
    assert queue._run_job(second)
    session.expire_all()
    assert session.get(TableSportsLeagues, 51).profileId == 6
    assert ast.literal_eval(session.get(TableSportsEvents, 61).missing_subtitles) == ['de']
    assert not queue.jobs_pending_queue and not queue.jobs_running_queue


@pytest.mark.parametrize('failure', ['runtime', 'notification'])
def test_sportarr_runtime_followups_remain_best_effort(monkeypatch, failure):
    from arr_instances import service
    from sportarr import scheduler

    calls = []

    def run(name):
        calls.append(name)
        if name == failure:
            raise OSError('follow-up unavailable')

    monkeypatch.setattr(scheduler, 'refresh_sports_runtime', lambda: run('runtime'))
    monkeypatch.setattr(service, 'event_stream', lambda **kwargs: run('notification'))
    service.refresh_runtime('sportarr', instance_id=1)
    assert calls == ['runtime', 'notification']
