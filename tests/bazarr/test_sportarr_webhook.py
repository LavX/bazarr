# coding=utf-8
"""The Sportarr import webhook.

Sonarr and Radarr have both had one since forever. Sports had none, so a setup
where Sportarr can reach Bazarr but Bazarr cannot hold an outbound stream to
Sportarr had to wait for the next scheduled sync after every import.
"""

import pytest


@pytest.fixture
def webhook(schema_session, monkeypatch):
    from api.webhooks import sportarr
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues

    schema_session.add(TableArrInstances(id=42, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, is_default=1,
                                         port=1867, api_key='k'))
    schema_session.add(TableArrInstances(id=43, kind='sportarr', stable_key='sportarr-4k',
                                         name='Sportarr 4K', enabled=1, port=1868, api_key='k'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title='Formula 1', path='/sports'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=61, arr_instance_id=42, league_id=51, sportarrEventId=9, file_id=71,
        path='/sports/race.mkv', title='Race'))
    schema_session.commit()

    monkeypatch.setattr(sportarr, 'database', schema_session)
    calls = []

    class _Namespace:
        payload = {}

    monkeypatch.setattr(sportarr, 'api_ns_webhooks_sportarr', _Namespace)
    return schema_session, _Namespace, calls


def _handler():
    """The undecorated post body.

    authenticate and the flask_restx expect/response decorators each wrap it;
    unwrapping by count would silently drift the day one is added or removed.
    """
    from api.webhooks import sportarr

    handler = sportarr.WebHooksSportarr.post
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


def _post(monkeypatch, namespace, calls, payload, stable_key=None):
    from api.webhooks import sportarr
    from subtitles.indexer import sports as indexer
    from sportarr import automatic

    namespace.payload = payload
    monkeypatch.setattr(indexer, 'store_subtitles_sports',
                        lambda event_id, owner, **kw: calls.append(('index', event_id, owner)))
    monkeypatch.setattr(automatic, 'search_event',
                        lambda event_id, owner, **kw: calls.append(('search', event_id, owner)))
    return _handler()(sportarr.WebHooksSportarr(), stable_key)


def test_a_test_hook_touches_nothing(webhook, monkeypatch):
    _, namespace, calls = webhook
    message, status = _post(monkeypatch, namespace, calls, {'eventType': 'Test'})
    assert status == 200
    assert calls == []


def test_an_import_indexes_and_searches_the_event(webhook, monkeypatch):
    _, namespace, calls = webhook
    message, status = _post(monkeypatch, namespace, calls, {
        'eventType': 'Download',
        'episodes': [{'id': 9}],
        'episodeFiles': [{'id': 71}],
    })
    assert status == 200
    # Indexed before searched: a search reads the missing languages the index
    # has just written, so the other order searches against stale state.
    assert calls == [('index', 61, 42), ('search', 61, 42)]


def test_a_download_starting_carries_no_files_and_does_nothing(webhook, monkeypatch):
    """Sportarr inherits Sonarr's tell: a grab and an import differ only by
    whether file entries are present."""
    _, namespace, calls = webhook
    message, status = _post(monkeypatch, namespace, calls, {
        'eventType': 'Download', 'episodes': [{'id': 9}], 'episodeFiles': [],
    })
    assert status == 200
    assert calls == []


def test_the_key_in_the_url_picks_the_owner(webhook, monkeypatch):
    """file_id is per-Sportarr, so an unscoped lookup would act on whichever
    instance happened to hand out the same number."""
    _, namespace, calls = webhook
    _post(monkeypatch, namespace, calls,
          {'eventType': 'Download', 'episodeFiles': [{'id': 71}]},
          stable_key='sportarr-4k')
    # The 4K instance owns no event with that file id.
    assert calls == []


def test_no_key_means_the_default_instance(webhook, monkeypatch):
    _, namespace, calls = webhook
    _post(monkeypatch, namespace, calls,
          {'eventType': 'Download', 'episodeFiles': [{'id': 71}]})
    assert calls == [('index', 61, 42), ('search', 61, 42)]


def test_a_disabled_owner_is_answered_200(webhook, monkeypatch):
    """An arr marks a webhook unhealthy and demands user interaction on any
    non-200, and none of these outcomes is fixable from that side."""
    session, namespace, calls = webhook
    from app.database import TableArrInstances

    session.get(TableArrInstances, 43).enabled = 0
    session.commit()
    message, status = _post(monkeypatch, namespace, calls,
                            {'eventType': 'Download', 'episodeFiles': [{'id': 71}]},
                            stable_key='sportarr-4k')
    assert status == 200
    assert calls == []


def test_one_unreadable_file_does_not_cost_the_rest_of_the_batch(webhook, monkeypatch):
    session, namespace, calls = webhook
    from app.database import TableSportsEvents

    session.add(TableSportsEvents(id=62, arr_instance_id=42, league_id=51,
                                  sportarrEventId=10, file_id=72,
                                  path='/sports/quali.mkv', title='Qualifying'))
    session.commit()

    from subtitles.indexer import sports as indexer

    def _index(event_id, owner, **kwargs):
        if event_id == 61:
            raise OSError('file vanished')
        calls.append(('index', event_id, owner))

    namespace.payload = {'eventType': 'Download',
                         'episodeFiles': [{'id': 71}, {'id': 72}]}
    monkeypatch.setattr(indexer, 'store_subtitles_sports', _index)
    from sportarr import automatic
    monkeypatch.setattr(automatic, 'search_event',
                        lambda event_id, owner, **kw: calls.append(('search', event_id, owner)))
    from api.webhooks import sportarr

    message, status = _handler()(sportarr.WebHooksSportarr(), None)
    assert status == 200
    assert ('index', 62, 42) in calls


def test_an_unsynced_import_reconciles_through_the_streams_own_path(webhook, monkeypatch):
    """A webhook and a stream frame must converge on the same rows, so the
    webhook reuses reconcile_work rather than resolving upstream ids itself."""
    import inspect

    from api.webhooks import sportarr as module

    source = inspect.getsource(module._reconcile)
    assert 'reconcile_work(' in source
    assert 'PendingWork(' in source
    assert 'connection_identity(instance)' in source


def test_the_namespace_is_registered():
    from api.webhooks import api_ns_list_webhooks, api_ns_webhooks_sportarr

    assert api_ns_webhooks_sportarr in api_ns_list_webhooks
