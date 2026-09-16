"""Jellyfin and Plex as native destinations: capabilities, dispatch and import.

The servers are local stdlib HTTP servers bound to port 0, never a real media
server, so an unreachable destination can be produced by stopping one.
"""
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

IDS = {'jellyfin': 'd2a4c6e8-1111-4b0a-9c3d-0a1b2c3d4e5f',
       'plex': 'f1e2d3c4-2222-4b0a-9c3d-0a1b2c3d4e5f'}
# Jellyfin addresses every item, libraries included, by a 32-character hex id,
# and its client refuses anything else before it reaches a URL.
MOVIE_LIBRARY = 'aa' * 16
SERIES_LIBRARY = 'bb' * 16
SPORTS_LIBRARY = 'cc' * 16
SERIES_ITEM = 'dd' * 16
EPISODE_ITEM = 'ee' * 16


def settings(**masters):
    general = dict(use_emby=False, use_silo=False, use_jellyfin=True, use_plex=True)
    return SimpleNamespace(general=SimpleNamespace(**(general | masters)))


def snapshot(kind, url='http://server.example', *, options=None, **overrides):
    from media_servers.instances import ConnectionSnapshot
    defaults = dict(id=IDS[kind], kind=kind, name=kind.capitalize(), instance_enabled=True,
                    master_enabled=True, url=url, apikey='synthetic-key', verify_ssl=False,
                    path_mappings=(), options_json=json.dumps(options if options is not None else {
                        'movie_library_ids': [MOVIE_LIBRARY], 'series_library_ids': [SERIES_LIBRARY],
                        'sports_library_ids': [SPORTS_LIBRARY]} if kind == 'jellyfin' else {
                        'movie_libraries': ['Movies'], 'series_libraries': ['Shows'],
                        'sports_libraries': ['Sports']}))
    return ConnectionSnapshot(**(defaults | overrides))


def metadata(**overrides):
    from media_servers.resolution import MediaMetadata
    return MediaMetadata(**(dict(imdb_id='tt0017136', tmdb_id='19', tvdb_id=78874,
                                 title='Metropolis', year=1927, season=1, episode=2) | overrides))


def mutation(media_type='movie', operation='download'):
    from media_servers.events import SubtitleMutation
    return SubtitleMutation(media_type, '/movies/Metropolis/Metropolis.mkv',
                            '/movies/Metropolis/Metropolis.en.srt', operation, None)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _respond(self, payload=None, status=200):
        body = b'' if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlsplit(self.path)
        query = parse_qs(route.query)
        self.server.calls.append(('GET', route.path, query))
        if route.path == '/Items':
            items = self.server.items.get(query.get('parentId', [''])[0], [])
            return self._respond({'Items': items, 'TotalRecordCount': len(items)})
        if route.path.startswith('/Shows/'):
            return self._respond({'Items': self.server.episodes, 'TotalRecordCount': len(self.server.episodes)})
        return self._respond({})

    def do_POST(self):
        route = urlsplit(self.path)
        self.server.calls.append(('POST', route.path, parse_qs(route.query)))
        self._respond(None, 204)


@pytest.fixture
def jellyfin_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    server.calls, server.items, server.episodes = [], {}, []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f'http://127.0.0.1:{server.server_address[1]}'
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_each_kind_declares_only_the_rungs_its_server_can_climb():
    """A capability a server has not got must be absent, not faked.

    Silo's ceiling is the measured one: items come back with no ProviderIds at
    all, so neither identity rung has anything to match on. Plex has an IMDB
    guid lookup and a section update and no title fallback, and inventing one
    here would be a behaviour change dressed up as convergence.
    """
    from media_servers import resolution
    from media_servers.instances import KIND_STEPS, needs_path_mappings
    assert set(KIND_STEPS) == {'emby', 'jellyfin', 'plex', 'silo'}
    assert set(KIND_STEPS['emby']) == set(resolution.CHAIN)
    assert set(KIND_STEPS['silo']) == {resolution.PATH, resolution.LIBRARY}
    assert set(KIND_STEPS['jellyfin']) == {resolution.PROVIDER_ID, resolution.TITLE_YEAR,
                                           resolution.LIBRARY}
    assert set(KIND_STEPS['plex']) == {resolution.PROVIDER_ID, resolution.LIBRARY}
    assert needs_path_mappings('emby') and needs_path_mappings('silo')
    # The regression this convergence must not introduce.
    assert not needs_path_mappings('jellyfin') and not needs_path_mappings('plex')
    for kind in ('jellyfin', 'plex'):
        from jellyfin.refresh import JellyfinRefreshClient
        from plex.refresh import PlexRefreshClient
        client = JellyfinRefreshClient if kind == 'jellyfin' else PlexRefreshClient
        assert set(client.REFRESH_STEPS) == set(KIND_STEPS[kind])


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_an_enabled_destination_with_no_path_mappings_is_valid_and_is_dispatched_to(kind):
    """Jellyfin and Plex users have never configured a path mapping.

    Both halves matter: the instance saves without one, and the dispatcher
    still hands it the publication rather than skipping it for mapping_missing.
    """
    from media_servers.instances import validate_connection
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    assert validate_connection(dict(kind=kind, name='A', enabled=True, url='http://server.example',
                                    api_key='key', path_mappings=[], options={}))
    configuration = NativeConfiguration(settings(), snapshots=[snapshot(kind)])
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_a: None,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation())
    assert dispatcher.status(IDS[kind])['pending'] == 1


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
@pytest.mark.parametrize('media_type,toggle', [('movie', 'refresh_movies'),
                                               ('episode', 'refresh_episodes')])
def test_a_per_type_opt_out_is_not_queued_at_all(kind, media_type, toggle):
    """update_movie_library / update_series_library, as an instance property."""
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    off = snapshot(kind, **{toggle: False})
    configuration = NativeConfiguration(settings(), snapshots=[off])
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_a: None,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation(media_type))
    assert dispatcher.status(IDS[kind])['pending'] == 0
    dispatcher.notify(mutation('episode' if media_type == 'movie' else 'movie'))
    assert dispatcher.status(IDS[kind])['pending'] == 1


def _run(kind, client, event=None, *, snap=None):
    """One publication, drained, with the walk recorded."""
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    configuration = NativeConfiguration(settings(), snapshots=[snap or snapshot(kind)])
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_a: client,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(event or mutation())
    assert dispatcher.wait_idle(10, server=IDS[kind])
    return dispatcher.status(IDS[kind])


class _Recorder:
    """A client that answers only the rungs it was told to answer."""

    def __init__(self, kind, resolves):
        from media_servers.instances import KIND_STEPS
        self.REFRESH_STEPS = KIND_STEPS[kind]
        self.resolves = resolves
        self.rungs = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def _rung(self, name, ensure_current):
        self.rungs.append(name)
        ensure_current()
        return {'status': 'requested'} if name in self.resolves else None

    def refresh_by_provider_id(self, _media_type, _metadata, *, ensure_current):
        from media_servers import resolution
        return self._rung(resolution.PROVIDER_ID, ensure_current)

    def refresh_by_title_year(self, _media_type, _metadata, *, ensure_current):
        from media_servers import resolution
        return self._rung(resolution.TITLE_YEAR, ensure_current)

    def refresh_library(self, _media_type, *, ensure_current, coalesce=None):
        from media_servers import resolution
        return self._rung(resolution.LIBRARY, ensure_current)


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_the_identifier_rung_answers_first_and_stops_the_walk(kind):
    from media_servers import resolution
    client = _Recorder(kind, {resolution.PROVIDER_ID})
    assert _run(kind, client)['state'] == 'requested'
    assert client.rungs == [resolution.PROVIDER_ID]


def test_jellyfin_falls_back_to_title_and_year_then_to_the_library():
    from media_servers import resolution
    client = _Recorder('jellyfin', {resolution.TITLE_YEAR})
    assert _run('jellyfin', client)['state'] == 'requested'
    assert client.rungs == [resolution.PROVIDER_ID, resolution.TITLE_YEAR]
    client = _Recorder('jellyfin', {resolution.LIBRARY})
    assert _run('jellyfin', client)['state'] == 'requested'
    assert client.rungs == [resolution.PROVIDER_ID, resolution.TITLE_YEAR, resolution.LIBRARY]


def test_plex_has_no_title_rung_and_falls_straight_to_the_section_update():
    from media_servers import resolution
    client = _Recorder('plex', {resolution.LIBRARY})
    assert _run('plex', client)['state'] == 'requested'
    assert client.rungs == [resolution.PROVIDER_ID, resolution.LIBRARY]


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_a_sports_publication_asks_only_the_library_rung(kind):
    from media_servers import resolution
    client = _Recorder(kind, {resolution.LIBRARY})
    assert _run(kind, client, mutation('sports'))['state'] == 'requested'
    assert client.rungs == [resolution.LIBRARY]


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_a_publication_no_rung_answered_stays_pending_for_a_retry(kind):
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    client = _Recorder(kind, set())
    configuration = NativeConfiguration(settings(), snapshots=[snapshot(kind)])
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_a: client,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation())
    assert dispatcher.wait_idle(10, server=IDS[kind])
    status = dispatcher.status(IDS[kind])
    assert status == {'pending': 1, 'state': 'unconfirmed', 'error_code': 'item_missing'}
    assert dispatcher.retry(IDS[kind]) == 1
    assert dispatcher.wait_idle(10, server=IDS[kind])
    assert dispatcher.status(IDS[kind])['pending'] == 1


def test_one_unreachable_destination_does_not_hold_up_the_other(jellyfin_server):
    """A stopped server keeps its own publication pending and nothing else."""
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    live = snapshot('jellyfin', jellyfin_server.url)
    # Nothing listens here: the port was bound to find a free one and released.
    import socket
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    dead = replace(snapshot('plex', f'http://127.0.0.1:{dead_port}'), kind='jellyfin',
                   id=IDS['plex'], options_json=live.options_json)
    jellyfin_server.items[MOVIE_LIBRARY] = [{'Id': 'ff' * 16, 'Name': 'Metropolis', 'Path': '/m/x.mkv',
                                             'ProviderIds': {'Imdb': 'tt0017136'}}]
    configuration = NativeConfiguration(settings(), snapshots=[live, dead])
    dispatcher = RefreshDispatcher(configuration, metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation())
    assert dispatcher.wait_idle(20)
    assert dispatcher.status(live.id) == {'pending': 0, 'state': 'requested', 'error_code': None}
    stalled = dispatcher.status(dead.id)
    assert stalled['pending'] == 1 and stalled['state'] == 'unconfirmed'
    assert stalled['error_code'] in ('connection_error', 'timeout')
    # The retry is still there to drain once that server comes back.
    assert dispatcher.retry(dead.id) == 1


def test_jellyfin_resolves_by_provider_id_and_refreshes_the_episode(jellyfin_server):
    """The identifiers Bazarr stored locate the series, and the numbers the file."""
    jellyfin_server.items[SERIES_LIBRARY] = [{'Id': SERIES_ITEM, 'Name': 'Metropolis',
                                              'ProviderIds': {'Tvdb': '78874'}}]
    jellyfin_server.episodes = [{'Id': EPISODE_ITEM, 'IndexNumber': 2, 'Path': '/s/e2.mkv'}]
    from jellyfin.refresh import JellyfinRefreshClient
    with JellyfinRefreshClient(snapshot('jellyfin', jellyfin_server.url)) as client:
        assert client.refresh_by_provider_id('episode', metadata()) == {'status': 'requested'}
    assert f'/Items/{EPISODE_ITEM}/Refresh' in [path for _method, path, _query in jellyfin_server.calls]


def test_jellyfin_refuses_to_guess_between_two_items_with_the_same_title(jellyfin_server):
    """A title is a weak identifier, and a tie is not a resolution."""
    from media_servers.http import MediaServerError
    from jellyfin.refresh import JellyfinRefreshClient
    jellyfin_server.items[MOVIE_LIBRARY] = [{'Id': 'a' * 32, 'Name': 'Metropolis'},
                                            {'Id': 'b' * 32, 'Name': 'Metropolis'}]
    with JellyfinRefreshClient(snapshot('jellyfin', jellyfin_server.url)) as client:
        with pytest.raises(MediaServerError) as error:
            client.refresh_by_title_year('movie', metadata())
    assert error.value.code == 'item_ambiguous'
    assert not [call for call in jellyfin_server.calls if call[0] == 'POST']


def test_jellyfin_never_asks_the_title_rung_without_a_year(jellyfin_server):
    """No year is a reason to climb the next rung, not to guess a remake."""
    from jellyfin.refresh import JellyfinRefreshClient
    with JellyfinRefreshClient(snapshot('jellyfin', jellyfin_server.url)) as client:
        assert client.refresh_by_title_year('movie', metadata(year=None)) is None
    assert jellyfin_server.calls == []


def test_the_delete_path_dispatches_instead_of_calling_any_server_inline():
    """Deleting a subtitle publishes it, and that is the only refresh left."""
    import inspect
    from subtitles.tools import delete
    source = inspect.getsource(delete)
    assert 'publication_callback' in source
    for name in ('plex_refresh_item', 'jellyfin_refresh_item',
                 'plex_update_sports_library', 'jellyfin_update_sports_library'):
        assert name not in source


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_a_destination_with_no_library_for_this_type_is_not_asked_at_all(kind):
    """A library is the only scope either of them has for a file.

    The scalar settings returned early when no library of that type was
    configured, and a target queued against one could never resolve: both
    identity rungs search inside the chosen libraries too.
    """
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    keys = {'jellyfin': ('movie_library_ids', 'series_library_ids'),
            'plex': ('movie_libraries', 'series_libraries')}[kind]
    only_series = snapshot(kind, options={keys[1]: ['chosen']})
    configuration = NativeConfiguration(settings(), snapshots=[only_series])
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_a: None,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation('movie'))
    assert dispatcher.status(IDS[kind])['pending'] == 0
    dispatcher.notify(mutation('episode'))
    assert dispatcher.status(IDS[kind])['pending'] == 1


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_emptying_the_libraries_under_a_queued_target_refuses_rather_than_scanning(kind):
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    configuration = NativeConfiguration(settings(), snapshots=[snapshot(kind)])
    client = _Recorder(kind, set())
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_a: client,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation())
    configuration.publish(replace(snapshot(kind, options={}), revision=9))
    assert dispatcher.retry(IDS[kind]) == 1
    assert dispatcher.wait_idle(10, server=IDS[kind])
    # Refused by name, not swallowed into a broader scan and not reported as a
    # generic internal failure, so the card can say which setting is missing.
    assert dispatcher.status(IDS[kind]) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'library_missing'}
    assert client.rungs == []


@pytest.mark.parametrize('kind,options,code', [
    ('emby', {'movie_library_ids': ['a']}, 'invalid_settings'),
    ('silo', {'refresh_method': 'immediate'}, 'invalid_settings'),
    ('jellyfin', {'movie_libraries': ['a']}, 'invalid_settings'),
    ('plex', {'refresh_method': 'async'}, 'invalid_settings'),
    ('jellyfin', {'refresh_method': 'whenever'}, 'invalid_settings'),
    ('jellyfin', {'movie_library_ids': [7]}, 'library_invalid'),
    ('plex', {'series_libraries': ['  ']}, 'library_invalid'),
])
def test_each_kind_accepts_only_the_options_it_declares(kind, options, code):
    """The blob is per-kind, so the keys one kind accepts are a closed set."""
    from media_servers.http import MediaServerError
    from media_servers.instances import validate_options
    with pytest.raises(MediaServerError) as error:
        validate_options(kind, options)
    assert error.value.code == code


def test_an_instance_keeps_its_options_and_toggles_across_a_save(schema_session):
    from media_servers.repository import MediaServerInstanceRepository, to_safe_dict
    repo = MediaServerInstanceRepository(schema_session)
    row = repo.create(kind='jellyfin', name='Attic', url='http://jellyfin.example',
                      api_key='key', enabled=True, verify_ssl=True, path_mappings=[],
                      refresh_movies=False,
                      options={'movie_library_ids': ['lib'], 'refresh_method': 'async'})
    saved = to_safe_dict(repo.get(row.id))
    assert saved['refresh_movies'] is False and saved['refresh_episodes'] is True
    assert saved['options'] == {'movie_library_ids': ['lib'], 'refresh_method': 'async'}
    repo.update(row.id, refresh_episodes=False, options={'series_library_ids': ['other']})
    saved = to_safe_dict(repo.get(row.id))
    assert saved['refresh_movies'] is False and saved['refresh_episodes'] is False
    assert saved['options'] == {'series_library_ids': ['other']}
    snapshot = repo.snapshot(row.id, settings())
    assert snapshot.libraries('episode') == ['other'] and snapshot.libraries('movie') == []
    assert snapshot.refreshes('sports') is True


class _FakeSection:
    """Only what `plex_refresh_item` ever asked a section for."""

    def __init__(self, title, guids, calls):
        self.title, self.type, self._guids, self._calls = title, 'movie', guids, calls

    # plexapi's own spelling, which the real sections answer to.
    def getGuid(self, guid):
        if guid not in self._guids:
            raise KeyError(guid)
        return self._guids[guid]

    def update(self):
        self._calls.append(('section-update', self.title))


class _FakeItem:
    def __init__(self, calls, name, episodes=None):
        self._calls, self._name, self._episodes = calls, name, episodes or {}

    def refresh(self):
        self._calls.append(('item-refresh', self._name))

    def episode(self, season, episode):
        return self._episodes[(season, episode)]


def _plex_client(monkeypatch, sections, calls):
    from types import SimpleNamespace
    from plex import operations, refresh
    monkeypatch.setattr(operations, 'plex_server_for', lambda *_args: SimpleNamespace(
        library=SimpleNamespace(section=lambda name: sections[name])))
    return refresh.PlexRefreshClient(snapshot('plex'))


def test_plex_refreshes_the_item_the_imdb_guid_resolves(monkeypatch):
    calls = []
    movie = _FakeItem(calls, 'Metropolis')
    sections = {'Movies': _FakeSection('Movies', {'imdb://tt0017136': movie}, calls)}
    with _plex_client(monkeypatch, sections, calls) as client:
        assert client.refresh_by_provider_id('movie', metadata()) == {'status': 'requested'}
    assert calls == [('item-refresh', 'Metropolis')]


def test_plex_descends_to_the_episode_the_numbers_name(monkeypatch):
    calls = []
    episode = _FakeItem(calls, 'S01E02')
    show = _FakeItem(calls, 'Metropolis', {(1, 2): episode})
    sections = {'Shows': _FakeSection('Shows', {'imdb://tt0017136': show}, calls)}
    with _plex_client(monkeypatch, sections, calls) as client:
        assert client.refresh_by_provider_id('episode', metadata()) == {'status': 'requested'}
    assert calls == [('item-refresh', 'S01E02')]


def test_plex_updates_the_section_when_the_guid_resolves_nothing(monkeypatch):
    calls = []
    sections = {'Movies': _FakeSection('Movies', {}, calls)}
    with _plex_client(monkeypatch, sections, calls) as client:
        assert client.refresh_by_provider_id('movie', metadata()) is None
        assert client.refresh_library('movie') == {'status': 'requested'}
    assert calls == [('section-update', 'Movies')]


def test_plex_reports_nothing_requested_when_no_section_accepted_the_update(monkeypatch):
    """A renamed section is not a scan, so the rung has not answered."""
    calls = []
    with _plex_client(monkeypatch, {}, calls) as client:
        assert client.refresh_library('movie') is None
    assert calls == []


@pytest.mark.parametrize('stored', ['not json', '[]', '{"movie_library_ids": "one"}', '{}'])
def test_a_stored_blob_validation_never_saw_is_nothing_configured(stored):
    """A hand-edited row is a destination with no libraries, not a crash."""
    snap = snapshot('jellyfin', options=None)
    snap = replace(snap, options_json=stored)
    assert snap.libraries('movie') == []
    assert snap.options() == ({} if stored != '{"movie_library_ids": "one"}'
                              else {'movie_library_ids': 'one'})
