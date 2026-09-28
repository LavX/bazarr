"""Jellyfin and Plex as native destinations: capabilities, dispatch and import.

The servers are local stdlib HTTP servers bound to port 0, never a real media
server, so an unreachable destination can be produced by stopping one.
"""
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from test_media_server_http import http_fixture as http_fixture
from test_media_server_instances import payload

HEADERS = {'X-API-KEY': 'synthetic-bazarr-key'}
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
        self._respond(None, self.server.refusals.get(route.path, 204))


@pytest.fixture
def jellyfin_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    server.calls, server.items, server.episodes, server.refusals = [], {}, [], {}
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
    # A real Plex destination, not a second Jellyfin wearing its id: isolation
    # has to hold across kinds, which is the whole point of the shared layer.
    dead = snapshot('plex', f'http://127.0.0.1:{dead_port}')
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


def _posts(server):
    return [path for method, path, _query in server.calls if method == 'POST']


def test_a_refused_jellyfin_library_does_not_stop_the_libraries_after_it(jellyfin_server):
    """A stale saved library id used to end the walk, so a valid library listed
    after it never saw a subtitle. Every library is asked, and the refusal is
    still reported, so the refresh is not called requested while one was not."""
    from media_servers.http import MediaServerError
    from jellyfin.refresh import JellyfinRefreshClient
    stale = '11' * 16
    jellyfin_server.refusals[f'/Items/{stale}/Refresh'] = 404
    snap = snapshot('jellyfin', jellyfin_server.url, options={'movie_library_ids': [stale, MOVIE_LIBRARY]})
    with JellyfinRefreshClient(snap) as client, pytest.raises(MediaServerError) as error:
        client.refresh_library('movie')
    assert error.value.code == 'not_found'
    assert _posts(jellyfin_server) == [f'/Items/{stale}/Refresh', f'/Items/{MOVIE_LIBRARY}/Refresh']


def test_a_jellyfin_server_that_refuses_the_credential_stops_at_the_first_library(jellyfin_server):
    """A refusal about the server, not the library, would meet every library the same way."""
    from media_servers.http import MediaServerError
    from jellyfin.refresh import JellyfinRefreshClient
    first = '11' * 16
    jellyfin_server.refusals[f'/Items/{first}/Refresh'] = 401
    snap = snapshot('jellyfin', jellyfin_server.url, options={'movie_library_ids': [first, MOVIE_LIBRARY]})
    with JellyfinRefreshClient(snap) as client, pytest.raises(MediaServerError) as error:
        client.refresh_library('movie')
    assert error.value.code == 'unauthorized'
    assert _posts(jellyfin_server) == [f'/Items/{first}/Refresh']


@pytest.mark.parametrize('media_type,kwargs,expected', [
    ('movies', {'radarr_id': 30}, 'movie'),
    ('series', {'sonarr_series_id': 5, 'sonarr_episode_id': 9}, 'episode'),
])
def test_deleting_a_subtitle_dispatches_the_publication(tmp_path, monkeypatch,
                                                        media_type, kwargs, expected):
    """The delete path's only media server refresh is the publication itself.

    Driven through ``delete_subtitles`` rather than read out of its source, so
    a future caller that removes the publication callback fails here instead of
    passing a grep.
    """
    from media_servers import events
    from subtitles.tools import delete as delete_mod

    subtitle = tmp_path / 'movie.en.srt'
    subtitle.write_text('1\n')
    video = tmp_path / 'movie.mkv'
    video.write_text('')
    published = []
    monkeypatch.setattr(events, 'notify_subtitle_mutation', published.append)
    for name in ('history_log', 'history_log_movie', 'store_subtitles',
                 'store_subtitles_movie', 'call_external_webhook', 'event_stream'):
        monkeypatch.setattr(delete_mod, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(delete_mod, 'language_from_alpha2', lambda code: 'English')
    monkeypatch.setattr(delete_mod, 'client_for_instance', lambda *args, **kwargs: None)
    monkeypatch.setattr(delete_mod, 'notify_sonarr', lambda *args, **kwargs: None)
    monkeypatch.setattr(delete_mod, 'notify_radarr', lambda *args, **kwargs: None)
    monkeypatch.setattr(delete_mod.path_mappings, 'path_replace_instance',
                        lambda path, *args: path)
    monkeypatch.setattr(delete_mod.path_mappings, 'path_replace_reverse_instance',
                        lambda path, *args: path)

    assert delete_mod.delete_subtitles(media_type=media_type, language='en', forced=False,
                                       hi=False, media_path=str(video),
                                       subtitles_path=str(subtitle), arr_instance_id=7,
                                       **kwargs) is True
    assert not subtitle.exists()
    assert [(event.media_type, event.operation, event.arr_instance_id)
            for event in published] == [(expected, 'delete', 7)]


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
    """Only what the Plex refresh asks a section for."""

    agent, language = 'tv.plex.agents.movie', 'en-US'

    def __init__(self, title, guids, calls, refusal=None):
        self.title, self.type, self._guids, self._calls = title, 'movie', guids, calls
        self._refusal = refusal

    # What plexapi's getGuid asks a real section: a sample item, whose agent
    # match turns the external id into a plex:// guid, then that guid.
    def search(self, maxresults=None, guid=None):
        if self._refusal is not None:
            raise self._refusal
        if guid is None:
            return [SimpleNamespace(matches=lambda agent, title: [SimpleNamespace(guid=f'plex://{title}')])]
        external = guid.removeprefix('plex://').replace('-', '://', 1)
        return [self._guids[external]] if external in self._guids else []

    def update(self):
        self._calls.append(('section-update', self.title))
        if self._refusal is not None:
            raise self._refusal


class _FakeItem:
    def __init__(self, calls, name, episodes=None, refusal=None):
        self._calls, self._name, self._episodes = calls, name, episodes or {}
        self._refusal = refusal

    def refresh(self):
        self._calls.append(('item-refresh', self._name))
        if self._refusal is not None:
            raise self._refusal

    def episode(self, season, episode):
        from plexapi.exceptions import NotFound
        if (season, episode) not in self._episodes:
            raise NotFound(f'S{season:02}E{episode:02}')
        return self._episodes[(season, episode)]


def _plex_client(monkeypatch, sections, calls, *, lookup_refusal=None, **overrides):
    from types import SimpleNamespace
    from plexapi.exceptions import NotFound
    from plex import operations, refresh

    def section(name):
        # plexapi's own answer for a title the server has no section for.
        if lookup_refusal is not None:
            raise lookup_refusal
        if name not in sections:
            raise NotFound(f'Invalid library section: {name}')
        return sections[name]

    monkeypatch.setattr(operations, 'plex_server_for', lambda *_args: SimpleNamespace(
        library=SimpleNamespace(section=section)))
    return refresh.PlexRefreshClient(snapshot('plex', **overrides))


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


def _refusals():
    import requests
    from plexapi.exceptions import Unauthorized
    return [(requests.exceptions.ConnectionError('refused'), 'connection_error'),
            (requests.exceptions.ReadTimeout('slow'), 'timeout'),
            (Unauthorized('(401) unauthorized'), 'unauthorized')]


@pytest.mark.parametrize('refusal, code', _refusals())
def test_a_plex_lookup_that_fails_is_not_reported_as_a_missing_item(monkeypatch, refusal, code):
    """Only a lookup miss is "not in this section"; a dead server is itself."""
    from media_servers.http import MediaServerError
    calls = []
    sections = {'Movies': _FakeSection('Movies', {}, calls, refusal=refusal)}
    with _plex_client(monkeypatch, sections, calls) as client, pytest.raises(MediaServerError) as error:
        client.refresh_by_provider_id('movie', metadata())
    assert error.value.code == code


@pytest.mark.parametrize('refusal, code', _refusals())
@pytest.mark.parametrize('rung', [lambda client: client.refresh_by_provider_id('movie', metadata()),
                                  lambda client: client.refresh_library('movie')],
                         ids=['provider_id', 'library'])
def test_a_plex_section_lookup_that_fails_is_not_a_missing_section(monkeypatch, refusal, code, rung):
    """Resolving the section name can reach the server too. A dead server or an
    expired token there is itself, not a renamed section, or every rung comes
    back empty and the destination reads as item_missing."""
    from media_servers.http import MediaServerError
    calls = []
    with _plex_client(monkeypatch, {}, calls, lookup_refusal=refusal) as client, \
            pytest.raises(MediaServerError) as error:
        rung(client)
    assert error.value.code == code
    assert calls == []


@pytest.mark.parametrize('refusal, code', _refusals())
def test_a_refused_item_refresh_fails_rather_than_falling_back_to_the_section(monkeypatch, refusal, code):
    from media_servers.http import MediaServerError
    calls = []
    movie = _FakeItem(calls, 'Metropolis', refusal=refusal)
    sections = {'Movies': _FakeSection('Movies', {'imdb://tt0017136': movie}, calls)}
    with _plex_client(monkeypatch, sections, calls) as client, pytest.raises(MediaServerError) as error:
        client.refresh_by_provider_id('movie', metadata())
    assert error.value.code == code


def test_one_failed_section_update_is_not_reported_as_requested(monkeypatch):
    """The other sections are still asked, but the refresh stays pending."""
    import requests
    from media_servers.http import MediaServerError
    calls = []
    sections = {'Movies': _FakeSection('Movies', {}, calls,
                                       refusal=requests.exceptions.ConnectionError('refused')),
                'Films': _FakeSection('Films', {}, calls)}
    options = {'movie_libraries': ['Movies', 'Films']}
    with _plex_client(monkeypatch, sections, calls, options=options) as client, \
            pytest.raises(MediaServerError) as error:
        client.refresh_library('movie')
    assert error.value.code == 'connection_error'
    assert calls == [('section-update', 'Movies'), ('section-update', 'Films')]


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


# --- The Plex account owning its destination row -----------------------------

def plex_settings(**overrides):
    """The scalar Plex account, as a fresh install ships it."""
    section = dict(auth_method='apikey', ip='127.0.0.1', port=32400, ssl=False,
                   apikey='', token='', server_url='', server_connections=[],
                   server_machine_id='', server_name='', server_local=False,
                   verify_ssl=False,
                   path_mappings=[], update_movie_library=True, update_series_library=True,
                   movie_library=[], series_library=[], sports_library=[], instance_id='')
    section.update(overrides)
    # A durable master key, so encrypting the credential never tries to mint one
    # and write it back through the real config file.
    general = SimpleNamespace(use_emby=False, use_silo=False, use_jellyfin=False, use_plex=True,
                              secrets_encryption_key='synthetic-durable-key')
    # The account routes read the section through .get, the way a Dynaconf
    # section answers, so the stand-in has to too.
    namespace = SimpleNamespace(**section)
    namespace.get = lambda key, default=None: getattr(namespace, key, default)
    return SimpleNamespace(general=general, plex=namespace,
                           emby=SimpleNamespace(url='', apikey='', verify_ssl=True, path_mappings=[]),
                           silo=SimpleNamespace(url='', apikey='', verify_ssl=True, path_mappings=[]),
                           jellyfin=SimpleNamespace(url='', apikey='', verify_ssl=True,
                                                    path_mappings=[], update_movie_library=False,
                                                    update_series_library=False, movie_library_ids=[],
                                                    series_library_ids=[], sports_library_ids=[],
                                                    refresh_method='immediate'))


def test_signing_in_with_oauth_creates_the_plex_destination(schema_session):
    """The Critical case: a new install signs in and gets a refreshable row.

    Nothing has ever written the Plex scalars before this point, so there is no
    row to update and no field in the instance form to paste a token into. The
    account transitions have to create it.
    """
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    config = plex_settings()
    assert sync_plex_instance(schema_session, config) is None, 'nothing configured yet'
    config.plex.auth_method = 'oauth'
    config.plex.token = 'plex-oauth-token'
    # Signing in comes before picking a server, so there is still nothing to
    # connect to and nothing to create.
    assert sync_plex_instance(schema_session, config) is None
    assert MediaServerInstanceRepository(schema_session).list('plex') == []
    config.plex.server_url = 'https://plex.example:32400'
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    assert row is not None and [item.id for item in repo.list('plex')] == [row.id]
    assert (row.url, bool(row.enabled)) == ('https://plex.example:32400', True)
    assert repo.get_decrypted_api_key(row.id) == 'plex-oauth-token'
    assert config.plex.instance_id == row.id


def test_the_account_updates_its_row_through_every_transition(schema_session):
    """Re-sign-in, a server switch and sign-out all land on the same row."""
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='first-token',
                           server_url='https://first.example:32400')
    row = sync_plex_instance(schema_session, config)

    # A server switch carries the URL and leaves the token alone.
    config.plex.server_url = 'https://second.example:32400'
    assert sync_plex_instance(schema_session, config).id == row.id
    assert repo.get(row.id).url == 'https://second.example:32400'
    assert repo.get_decrypted_api_key(row.id) == 'first-token'

    # Signing out switches the row off and strips the credential, and keeps it.
    config.general.use_plex = False
    config.plex.token = ''
    sync_plex_instance(schema_session, config, signed_out=True)
    assert bool(repo.get(row.id).enabled) is False
    assert repo.get(row.id).api_key == ''
    assert len(repo.list('plex')) == 1

    # Signing back in reuses that row rather than adding a second, restores the
    # credential, and switches it back on: completing a sign-in is the user
    # saying to use Plex. The address it had is kept until a server is picked.
    config.general.use_plex = True
    config.plex.token = 'second-token'
    config.plex.server_url = ''
    assert sync_plex_instance(schema_session, config, signed_in=True).id == row.id
    assert repo.get_decrypted_api_key(row.id) == 'second-token'
    assert bool(repo.get(row.id).enabled) is True
    assert repo.get(row.id).url == 'https://second.example:32400'
    assert len(repo.list('plex')) == 1


def test_a_reconcile_never_reverts_the_instance_settings_a_user_changed(schema_session):
    """The account owns the connection; the toggles on the row are the user's.

    Rewriting them from the scalars on every startup and every server switch
    would switch an instance a user turned off back on, and undo a TLS setting
    they changed, behind their back. Only a sign-in and a sign-out speak for the
    account, and neither of those is a server switch.
    """
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='token',
                           server_url='https://plex.example:32400')
    row = sync_plex_instance(schema_session, config)
    repo.update(row.id, enabled=False, verify_ssl=True, refresh_episodes=False)

    config.plex.server_url = 'https://moved.example:32400'
    sync_plex_instance(schema_session, config)
    saved = repo.values(repo.get(row.id))
    assert saved['url'] == 'https://moved.example:32400'
    assert saved['enabled'] is False and saved['verify_ssl'] is True
    assert saved['refresh_episodes'] is False

    # And a plain startup reconcile is not a server switch either.
    sync_plex_instance(schema_session, config)
    assert repo.values(repo.get(row.id))['enabled'] is False


def test_the_owner_id_survives_a_restart_and_keeps_the_row_it_was_bound_to(schema_session):
    """Two Plex rows, and the account owns the second one.

    Without persisting the id the fallback is whichever row sorts first over
    random UUIDs, so a restart could hand the account someone else's row.
    """
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='first-token',
                           server_url='https://first.example:32400')
    first = repo.create(kind='plex', name='Hand added', url='https://hand.example:32400',
                        api_key='hand-key', enabled=True, verify_ssl=True, path_mappings=[])
    owned = repo.create(kind='plex', name='Account', url='https://first.example:32400',
                        api_key='first-token', enabled=True, verify_ssl=False, path_mappings=[])
    config.plex.instance_id = owned.id

    writes = []
    config.plex.server_url = 'https://second.example:32400'
    assert sync_plex_instance(schema_session, config,
                              persist=lambda: writes.append(config.plex.instance_id)).id == owned.id
    # Already recorded, so nothing new to write.
    assert writes == []

    # The restart: a fresh settings object carrying only what reached disk.
    restarted = plex_settings(auth_method='oauth', token='first-token',
                              server_url='https://third.example:32400',
                              instance_id=config.plex.instance_id)
    assert sync_plex_instance(schema_session, restarted).id == owned.id
    assert repo.get(owned.id).url == 'https://third.example:32400'
    assert repo.get(first.id).url == 'https://hand.example:32400'


def test_a_newly_recorded_owner_id_is_written_to_disk(schema_session):
    from media_servers.plex_account import sync_plex_instance
    config = plex_settings(auth_method='oauth', token='token',
                           server_url='https://plex.example:32400')
    writes = []
    row = sync_plex_instance(schema_session, config, persist=lambda: writes.append(True))
    assert config.plex.instance_id == row.id
    assert writes == [True], 'the id the account was just bound to has to survive a restart'
    # A reconcile that changes nothing does not rewrite the config.
    sync_plex_instance(schema_session, config, persist=lambda: writes.append(True))
    assert writes == [True]


@pytest.mark.parametrize('outcome', [False, OSError('read-only file system')],
                         ids=['write_config_false', 'write_raises'])
def test_a_plex_owner_that_cannot_be_saved_leaves_no_row_behind(schema_session, outcome):
    """The recorded id is the only way back to the account's row after a
    restart, and it is never guessed. A row whose id never reached disk would be
    orphaned there, and every startup would create another one beside it."""
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='token',
                           server_url='https://plex.example:32400')

    def persist():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert sync_plex_instance(schema_session, config, persist=persist) is None
    assert repo.list('plex') == []
    assert config.plex.instance_id == ''

    # Once the config can be written, the next change binds a row as usual.
    row = sync_plex_instance(schema_session, config, persist=lambda: True)
    assert [item.id for item in repo.list('plex')] == [row.id]
    assert config.plex.instance_id == row.id


def test_the_startup_import_creates_no_plex_row_it_cannot_record(schema_session, monkeypatch):
    from app import config as app_config
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    monkeypatch.setattr(app_config, 'write_config', lambda: False)
    config = plex_settings(auth_method='oauth', token='token',
                           server_url='https://plex.example:32400')
    assert backfill_instances(schema_session, config)['plex']['error_code'] == 'migration_failed'
    assert MediaServerInstanceRepository(schema_session).list('plex') == []
    assert config.plex.instance_id == ''


def test_switching_auth_method_replaces_the_credential_on_the_row(schema_session):
    """The automatic apikey-to-OAuth migration, and its rollback."""
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(apikey='legacy-key', ip='plex.example', ssl=True)
    row = sync_plex_instance(schema_session, config)
    assert repo.get(row.id).url == 'https://plex.example:32400'
    assert repo.get_decrypted_api_key(row.id) == 'legacy-key'

    config.plex.auth_method = 'oauth'
    config.plex.token = 'migrated-token'
    config.plex.server_url = 'https://direct.example:32400'
    config.plex.apikey = ''
    sync_plex_instance(schema_session, config)
    assert repo.get(row.id).url == 'https://direct.example:32400'
    assert repo.get_decrypted_api_key(row.id) == 'migrated-token'

    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'legacy-key'
    config.plex.token = ''
    sync_plex_instance(schema_session, config)
    assert repo.get(row.id).url == 'https://plex.example:32400'
    assert repo.get_decrypted_api_key(row.id) == 'legacy-key'


def test_a_second_plex_instance_added_by_hand_is_never_taken_over(schema_session):
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='account-token',
                           server_url='https://account.example:32400')
    owned = sync_plex_instance(schema_session, config)
    other = repo.create(kind='plex', name='Second', url='https://other.example:32400',
                        api_key='other-key', enabled=True, verify_ssl=True, path_mappings=[])
    config.plex.server_url = 'https://moved.example:32400'
    assert sync_plex_instance(schema_session, config).id == owned.id
    assert repo.get(other.id).url == 'https://other.example:32400'
    assert repo.get_decrypted_api_key(other.id) == 'other-key'


def test_a_fresh_install_stamps_every_kind_so_new_destinations_are_dispatched(schema_session):
    """The marker is also the dispatcher's gate, not only the import latch.

    A kind with no marker is blocked: `NativeConfiguration.publish` drops its
    snapshots and `read` refuses it, so a destination the user adds in
    Connections or the wizard is invisible to refreshes and its status reads
    migration_failed forever. On a fresh install nothing is configured, so
    leaving those kinds unstamped strands every one of them.
    """
    from app.database import TableMediaServerImports
    from media_servers.backfill import backfill_instances
    from media_servers.dispatcher import NativeConfiguration
    from media_servers.instances import VALID_KINDS
    from media_servers.repository import MediaServerInstanceRepository
    config = plex_settings()
    config.general.use_plex = False
    results = backfill_instances(schema_session, config)
    assert all(results[kind] == {'created': False} for kind in VALID_KINDS)
    assert all(schema_session.get(TableMediaServerImports, kind) is not None for kind in VALID_KINDS)
    assert MediaServerInstanceRepository(schema_session).list() == []

    # Now the user adds one of each by hand, as the Connections page does.
    repo = MediaServerInstanceRepository(schema_session)
    rows = {kind: repo.create(**payload(kind)) for kind in VALID_KINDS if kind != 'plex'}
    rows['plex'] = repo.create(kind='plex', name='Plex', url='https://plex.example:32400',
                               api_key='key', enabled=True, verify_ssl=False, path_mappings=[])
    for kind in VALID_KINDS:
        setattr(config.general, 'use_' + kind, True)
    stamped = [kind for kind in VALID_KINDS
               if schema_session.get(TableMediaServerImports, kind) is not None]
    configuration = NativeConfiguration(config, snapshots=repo.snapshots(config, kinds=stamped),
                                        blocked_kinds=set(VALID_KINDS) - set(stamped))
    assert configuration.blocked_kinds == frozenset()
    for kind, row in rows.items():
        assert configuration.read(row.id)[1].kind == kind


def test_only_a_kind_whose_import_failed_stays_blocked(schema_session, monkeypatch):
    from app.database import TableMediaServerImports
    from media_servers import backfill
    from media_servers.instances import VALID_KINDS
    config = plex_settings()
    config.emby.url = 'http://emby.example'
    config.emby.apikey = 'emby-key'
    original = backfill._record_import

    def fail(session, kind):
        if kind == 'emby':
            raise RuntimeError('synthetic import failure')
        original(session, kind)

    monkeypatch.setattr(backfill, '_record_import', fail)
    results = backfill.backfill_instances(schema_session, config)
    assert results['emby'] == {'created': False, 'error_code': 'migration_failed'}
    blocked = [kind for kind in VALID_KINDS
               if schema_session.get(TableMediaServerImports, kind) is None]
    assert blocked == ['emby']


def test_a_wizard_created_jellyfin_row_is_not_joined_by_a_phantom_import(schema_session):
    """The wizard writes use_jellyfin and a row, and no settings.jellyfin.*.

    With the marker stamped on the first startup, that state cannot make a
    second import at the next one and leave the user with two Jellyfin rows.
    """
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    config = plex_settings()
    config.general.use_plex = False
    backfill_instances(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    wizard = repo.create(kind='jellyfin', name='Jellyfin', url='http://jellyfin.example:8096',
                         api_key='jf-key', enabled=True, verify_ssl=True, path_mappings=[])
    config.general.use_jellyfin = True

    assert backfill_instances(schema_session, config)['jellyfin'] == {'created': False}
    assert [row.id for row in repo.list('jellyfin')] == [wizard.id]


def test_the_startup_import_reconciles_plex_rather_than_importing_it_once(schema_session):
    """Plex's scalars keep changing, so its row is reconciled every startup."""
    from media_servers.backfill import backfill_instances
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='token',
                           server_url='https://plex.example:32400')
    assert backfill_instances(schema_session, config)['plex'] == {'created': True}
    row, = repo.list('plex')
    config.plex.server_url = 'https://moved.example:32400'
    backfill_instances(schema_session, config)
    assert [item.id for item in repo.list('plex')] == [row.id]
    assert repo.get(row.id).url == 'https://moved.example:32400'


# --- Refreshing every library a destination is scoped to ---------------------

class _Rescanner:
    def __init__(self, kind, answers=None):
        from media_servers.instances import KIND_STEPS
        self.REFRESH_STEPS = KIND_STEPS[kind]
        self.calls = []
        self.answers = answers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def refresh_library(self, *args, **_kwargs):
        self.calls.append(args)
        if self.answers is None:
            return {'status': 'requested'}
        answer = self.answers.get(args, None)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _rescan(monkeypatch, snap, client):
    """The real refresh_libraries, on the saved snapshot, with a fake client.

    ``refresh_libraries`` resolves both of these from the dispatcher at call
    time, so patching them there is what it actually reads.
    """
    from media_servers import dispatcher, libraries
    configuration = dispatcher.NativeConfiguration(settings(), snapshots=[snap])
    monkeypatch.setattr(dispatcher, '_configuration', configuration)
    monkeypatch.setattr(dispatcher, '_client', lambda *_args: client)
    return libraries.refresh_libraries(snap.id)


@pytest.mark.parametrize('kind', ['jellyfin', 'plex'])
def test_refreshing_libraries_asks_for_every_configured_type(monkeypatch, kind):
    """The button retry never was: it scans without anything being queued."""
    client = _Rescanner(kind)
    assert _rescan(monkeypatch, snapshot(kind), client) == {'requested': 3, 'failed': 0}
    assert client.calls == [('movie',), ('episode',), ('sports',)]


def test_refreshing_libraries_skips_a_type_with_nothing_chosen(monkeypatch):
    keys = {'movie_library_ids': [MOVIE_LIBRARY]}
    client = _Rescanner('jellyfin')
    assert _rescan(monkeypatch, snapshot('jellyfin', options=keys), client) == {'requested': 1, 'failed': 0}
    assert client.calls == [('movie',)]


def test_refreshing_libraries_refuses_when_the_instance_points_at_none(monkeypatch):
    from media_servers.http import MediaServerError
    client = _Rescanner('plex')
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, snapshot('plex', options={}), client)
    assert error.value.code == 'library_missing'
    assert client.calls == []


def test_refreshing_libraries_refuses_a_destination_that_is_switched_off(monkeypatch):
    from media_servers.http import MediaServerError
    client = _Rescanner('plex')
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, replace(snapshot('plex'), instance_enabled=False), client)
    assert error.value.code == 'connection_disabled'
    assert client.calls == []


def test_a_scope_the_server_holds_no_library_for_is_not_a_failure(monkeypatch):
    """A rung that answers nothing is that library missing, not a broken scan."""
    client = _Rescanner('plex', answers={('movie',): {'status': 'requested'}})
    assert _rescan(monkeypatch, snapshot('plex'), client) == {'requested': 1, 'failed': 0}
    assert client.calls == [('movie',), ('episode',), ('sports',)]


def test_a_destination_deleted_mid_rescan_is_not_asked_again(monkeypatch):
    """Each scope checks the saved destination first, as the refresh worker does."""
    from media_servers import dispatcher
    from media_servers.http import MediaServerError

    class Deleting(_Rescanner):
        def refresh_library(self, *args, ensure_current=None, **_kwargs):
            # Every real client runs the guard before it sends a request.
            if ensure_current:
                ensure_current()
            self.calls.append(args)
            dispatcher.get_native_configuration().delete(IDS['plex'])
            return {'status': 'requested'}

    client = Deleting('plex')
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, snapshot('plex'), client)
    assert error.value.code == 'configuration_changed'
    assert client.calls == [('movie',)]


def test_an_emby_root_is_scanned_once_not_again_for_sports(monkeypatch, http_fixture):
    """Emby's sports request matches any library holding the root, typed or not."""
    from urllib.parse import urlsplit
    from emby.client import EmbyClient
    folders = [{'Name': 'Movies', 'ItemId': '3', 'CollectionType': 'movies', 'Locations': ['/media/movies']},
               {'Name': 'TV', 'ItemId': '19', 'CollectionType': 'tvshows', 'Locations': ['/media/series']}]
    base, records = http_fixture([(200, folders, {}), (204, b'', {}), (200, folders, {}),
                                  (200, folders, {}), (204, b'', {})])
    snap = replace(snapshot('jellyfin'), id='0e3b8c1a-3333-4b0a-9c3d-0a1b2c3d4e5f', kind='emby',
                   name='Emby', url=base, options_json='{}',
                   path_mappings=((('local_path', '/movies'), ('remote_path', '/media/movies')),))
    assert _rescan(monkeypatch, snap, EmbyClient(base, 'synthetic-key'))['failed'] == 0
    posts = [urlsplit(record['path']).path for record in records if record['method'] == 'POST']
    assert posts == ['/Items/3/Refresh']


def test_a_full_rescan_clears_the_overflow_it_covers(monkeypatch):
    """The overflow warning says to refresh the libraries, so doing that clears it.

    A mutation dropped while the rescan runs may have missed the scans it had
    already sent, so one of those keeps the warning.
    """
    from media_servers import dispatcher
    snap = snapshot('plex')
    workers = dispatcher.RefreshDispatcher(dispatcher.NativeConfiguration(settings(), snapshots=[snap]))
    monkeypatch.setattr(dispatcher, '_dispatcher', workers)
    workers._server(snap.id).dropped = 3
    assert workers.status(snap.id)['error_code'] == 'queue_overflow'

    class DroppingMeanwhile(_Rescanner):
        def refresh_library(self, *args, **kwargs):
            workers.servers[snap.id].dropped += 1
            return super().refresh_library(*args, **kwargs)

    _rescan(monkeypatch, snap, DroppingMeanwhile('plex'))
    assert workers.status(snap.id)['error_code'] == 'queue_overflow'

    # A library the server refused was not rescanned, so its drops are not covered.
    from media_servers.http import MediaServerError
    refusing = _Rescanner('plex', answers={('movie',): MediaServerError('server_error'),
                                           ('episode',): {'status': 'requested'},
                                           ('sports',): {'status': 'requested'}})
    assert _rescan(monkeypatch, snap, refusing) == {'requested': 2, 'failed': 1}
    assert workers.status(snap.id)['error_code'] == 'queue_overflow'

    # Nor is one that failed in a way the client never turned into a refusal.
    failing = _Rescanner('plex', answers={('movie',): KeyError('Items'),
                                          ('episode',): {'status': 'requested'},
                                          ('sports',): {'status': 'requested'}})
    assert _rescan(monkeypatch, snap, failing) == {'requested': 2, 'failed': 1}
    assert workers.status(snap.id)['error_code'] == 'queue_overflow'

    _rescan(monkeypatch, snap, _Rescanner('plex'))
    assert workers.status(snap.id)['error_code'] is None


def test_one_refused_scope_does_not_stop_the_rest(monkeypatch):
    """A scope the server refuses used to abort the whole run, so the
    libraries after it were never asked. It is counted and the rest still run."""
    from media_servers.http import MediaServerError
    client = _Rescanner('plex', answers={('movie',): MediaServerError('server_error'),
                                         ('episode',): {'status': 'requested'},
                                         ('sports',): {'status': 'requested'}})
    assert _rescan(monkeypatch, snapshot('plex'), client) == {'requested': 2, 'failed': 1}
    assert client.calls == [('movie',), ('episode',), ('sports',)]


def test_a_run_the_server_refused_everywhere_fails_with_its_reason(monkeypatch):
    from media_servers.http import MediaServerError
    client = _Rescanner('plex', answers={('movie',): MediaServerError('server_error'),
                                         ('episode',): MediaServerError('not_found')})
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, snapshot('plex'), client)
    assert error.value.code == 'server_error'
    assert client.calls == [('movie',), ('episode',), ('sports',)]


@pytest.mark.parametrize('code', ['timeout', 'connection_error', 'unauthorized'])
def test_a_server_that_cannot_be_reached_stops_the_run_at_once(monkeypatch, code):
    """Every other scope would wait out the same timeout or meet the same
    refusal, so the button would spin for each of them to say the same thing."""
    from media_servers.http import MediaServerError
    client = _Rescanner('plex', answers={('movie',): MediaServerError(code),
                                         ('episode',): {'status': 'requested'}})
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, snapshot('plex'), client)
    assert error.value.code == code
    assert client.calls == [('movie',)]


def test_a_scope_that_fails_unexpectedly_is_counted_and_logged(monkeypatch, caplog):
    """Only a refusal used to count as a failure. Anything else, such as a
    KeyError from a response shaped differently than the client expects, went
    to the debug log, so the button reported success with nothing failed and
    the log above debug held no trace of it."""
    import logging
    client = _Rescanner('plex', answers={('movie',): {'status': 'requested'},
                                         ('episode',): KeyError('Items'),
                                         ('sports',): {'status': 'requested'}})
    with caplog.at_level(logging.WARNING):
        assert _rescan(monkeypatch, snapshot('plex'), client) == {'requested': 2, 'failed': 1}
    assert client.calls == [('movie',), ('episode',), ('sports',)]
    record, = [record for record in caplog.records if 'episode' in record.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.exc_info and record.exc_info[0] is KeyError


def test_a_run_that_failed_unexpectedly_everywhere_is_not_a_missing_library(monkeypatch):
    """With nothing requested and nothing counted as refused, the run told the
    user the server held no library the instance points at, which sends them
    to a setting that is fine."""
    from media_servers.http import MediaServerError
    client = _Rescanner('plex', answers={('movie',): TypeError('synthetic'),
                                         ('episode',): TypeError('synthetic'),
                                         ('sports',): TypeError('synthetic')})
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, snapshot('plex'), client)
    assert error.value.code == 'internal_error'
    assert client.calls == [('movie',), ('episode',), ('sports',)]


class _ForeignRefusal(Exception):
    """A MediaServerError of another module generation: same code, other class."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def test_a_refusal_is_read_by_its_code_not_its_class(monkeypatch):
    """As the refresh worker does, so a refusal whose class came from another
    module generation is still counted as that refusal, not as a fault."""
    client = _Rescanner('plex', answers={('movie',): _ForeignRefusal('server_error'),
                                         ('episode',): {'status': 'requested'}})
    assert _rescan(monkeypatch, snapshot('plex'), client) == {'requested': 1, 'failed': 1}

    client = _Rescanner('plex', answers={('movie',): _ForeignRefusal('timeout'),
                                         ('episode',): {'status': 'requested'}})
    with pytest.raises(_ForeignRefusal) as error:
        _rescan(monkeypatch, snapshot('plex'), client)
    assert error.value.code == 'timeout'
    assert client.calls == [('movie',)]


def test_a_code_that_is_not_a_refusal_is_still_a_fault(monkeypatch, caplog):
    """Only a code the dispatcher knows is a refusal. Other exceptions carry
    codes of their own, as SQLAlchemy's do, and one of those was counted as a
    refusal and logged without the traceback that finds the fault."""
    import logging
    from media_servers.http import MediaServerError
    client = _Rescanner('plex', answers={('movie',): _ForeignRefusal('e3q8'),
                                         ('episode',): {'status': 'requested'}})
    with caplog.at_level(logging.WARNING):
        assert _rescan(monkeypatch, snapshot('plex'), client) == {'requested': 1, 'failed': 1}
    record, = [record for record in caplog.records if 'movie' in record.getMessage()]
    assert record.levelno == logging.WARNING
    assert 'unexpected error' in record.getMessage()
    assert record.exc_info and record.exc_info[0] is _ForeignRefusal

    client = _Rescanner('plex', answers={('movie',): _ForeignRefusal('e3q8'),
                                         ('episode',): _ForeignRefusal('e3q8')})
    with pytest.raises(MediaServerError) as error:
        _rescan(monkeypatch, snapshot('plex'), client)
    assert error.value.code == 'internal_error'
    assert client.calls == [('movie',), ('episode',), ('sports',)]


# --- The real OAuth handlers, against a faked Plex ---------------------------

PLEX_MACHINE = 'machine-abc'
LISTED = 'https://listed.example:32400'
UNLISTED = 'https://unlisted.example:32400'

_SECTIONS = {'MediaContainer': {'Directory': [
    {'key': '1', 'title': 'Movies', 'type': 'movie', 'count': 1, 'agent': 'tv.plex.agents.imdb',
     'scanner': 'Plex Movie Scanner', 'language': 'en', 'uuid': 'uuid-1',
     'updatedAt': 1, 'createdAt': 1}]}}


class _PlexTV:
    """One requests.get fake serving both plex.tv and the Plex server itself.

    ``listing`` maps each account token to the addresses plex.tv lists for
    ``machine``; ``status`` overrides the plex.tv answer's status. Every other
    URL is answered from ``replies`` (an exact URL to ``(status, payload)``) and
    recorded, so a test can see exactly where a credential went and how often
    plex.tv was asked.
    """

    def __init__(self, listing=None, replies=None, status=200, machine=PLEX_MACHINE,
                 content_type='application/json', body=None, xml=None):
        self.listing = listing or {}
        self.replies = replies or {}
        self.status = status
        self.machine = machine
        self.content_type = content_type
        self.body = body
        self.xml = xml
        self.tv_calls = 0
        self.server_calls = []
        self.redirect_flags = []

    def __call__(self, url, **kwargs):
        token = (kwargs.get('headers') or {}).get('X-Plex-Token')
        self.redirect_flags.append(kwargs.get('allow_redirects'))
        if 'plex.tv/pms/resources' in url:
            self.tv_calls += 1
            if self.body is not None:
                return SimpleNamespace(
                    status_code=self.status, headers={'content-type': self.content_type},
                    raise_for_status=lambda: None, json=lambda: self.body, text=self.xml or '')
            return SimpleNamespace(
                status_code=self.status, headers={'content-type': self.content_type},
                raise_for_status=lambda: None, text=self.xml or '',
                json=lambda: [{'name': 'Attic', 'clientIdentifier': self.machine,
                               'provides': 'server', 'owned': True,
                               'connections': [{'uri': uri} for uri in self.listing.get(token, ())]}])
        self.server_calls.append((url, token))
        status, payload = self.replies.get(url, (200, {}))
        return SimpleNamespace(status_code=status, headers={'content-type': 'application/json'},
                               raise_for_status=lambda: None, json=lambda: payload)


def plex_tv(monkeypatch, fake):
    """Answer every plex.tv ask with ``fake`` and forget any remembered answer.

    The requests module is patched itself, so one fake serves the plex.tv ask,
    the server probes and the library fetches alike.
    """
    import requests

    from media_servers import plex_account
    plex_account.forget_plex_tv_answer()
    monkeypatch.setattr(requests, 'get', fake)


# A Plex root document, the least plexapi will construct a server from.
_PLEX_ROOT = (b'<MediaContainer size="0" friendlyName="Attic" version="1.2.3">'
              b'</MediaContainer>')


@pytest.fixture
def redirect_pair():
    """Two local Plex origins; the first redirects one path to the second.

    Everything else is answered as a Plex root document, and each origin
    records the requests it received with the credential they arrived with, so
    a test can prove the credential never crossed to the second origin.
    """
    def build(records, redirect_to, redirect_path):
        class _RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _serve(self):
                records.append((self.path, self.headers.get('X-Plex-Token')))
                if redirect_to is not None and self.path == redirect_path:
                    self.send_response(302)
                    self.send_header('Location', redirect_to + '/stolen')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.send_header('Content-Length', str(len(_PLEX_ROOT)))
                self.end_headers()
                self.wfile.write(_PLEX_ROOT)

            do_GET = _serve
            do_POST = _serve

        return _RedirectHandler

    started = []

    def start(redirect_to=None, redirect_path='/library/sections'):
        records = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), build(records, redirect_to, redirect_path))
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        server.url = f'http://127.0.0.1:{server.server_address[1]}'
        server.records = records
        started.append((server, thread))
        return server

    def pair(redirect_path='/library/sections'):
        target = start()
        source = start(redirect_to=target.url, redirect_path=redirect_path)
        return source, target

    yield pair
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def plex_account_api(schema_session, monkeypatch):
    """The Plex account endpoints, on a real app, with plex.tv faked out."""
    from api import api_bp
    from api.plex import oauth
    from app import config as app_config
    from app import database as app_database
    from flask import Flask

    # api.utils bound the real settings when it was imported, so the API-key
    # check reads that one whatever this fixture swaps in for the handlers.
    monkeypatch.setitem(app_config.settings.auth, 'apikey', 'synthetic-bazarr-key')
    config = plex_settings()
    monkeypatch.setattr(app_config, 'settings', config)
    monkeypatch.setattr(oauth, 'settings', config)
    monkeypatch.setattr(oauth, 'write_config', lambda: None)
    # The destination layer records the row's owner through its own import of
    # write_config, and the real one cannot save this stand-in settings object.
    monkeypatch.setattr(app_config, 'write_config', lambda: True)
    monkeypatch.setattr(app_database, 'database', schema_session)
    monkeypatch.setattr(oauth, 'validate_plex_token',
                        lambda token: {'id': 7, 'username': 'someone', 'email': 'a@b.example'})
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client(), config


def test_the_oauth_pin_handler_creates_the_plex_destination(plex_account_api, monkeypatch,
                                                            schema_session):
    from api.plex import oauth
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    monkeypatch.setattr(oauth.pin_cache, 'get', lambda pin: {'client_id': 'bazarr'})
    monkeypatch.setattr(oauth.pin_cache, 'delete', lambda pin: None)
    # One requests fake answers both the PIN check and the plex.tv server
    # listing a selection is verified against.
    plex_tv(monkeypatch, _PlexTV(
        listing={'fresh-plex-token': ('https://plex.example:32400',)}, machine='abc',
        replies={'https://plex.tv/api/v2/pins/123': (200, {'authToken': 'fresh-plex-token'})}))

    response = client.get('/api/plex/oauth/pin/123/check', headers=HEADERS)
    assert response.status_code == 200 and response.json['data']['authenticated'] is True
    # The picker is the second half of the same flow, and it is what gives the
    # destination an address to refresh.
    assert client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': 'abc', 'name': 'Attic',
        'uri': 'https://plex.example:32400'}).status_code == 200
    row, = MediaServerInstanceRepository(schema_session).list('plex')
    assert bool(row.enabled) is True
    assert row.url == 'https://plex.example:32400'
    assert MediaServerInstanceRepository(schema_session).get_decrypted_api_key(row.id) == 'fresh-plex-token'
    assert config.plex.instance_id == row.id


def test_selecting_a_server_moves_the_destination_and_signing_out_disables_it(
        plex_account_api, monkeypatch, schema_session):
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'existing-token'
    config.plex.server_url = 'https://first.example:32400'
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    # The selection is verified against plex.tv before it is stored.
    plex_tv(monkeypatch, _PlexTV(
        listing={'existing-token': ('https://second.example:32400',)}, machine='abc'))

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': 'abc', 'name': 'Attic', 'uri': 'https://second.example:32400'})
    assert response.status_code == 200
    assert repo.get(row.id).url == 'https://second.example:32400'
    assert repo.get_decrypted_api_key(row.id) == 'existing-token'

    assert client.post('/api/plex/oauth/logout', headers=HEADERS, json={}).status_code == 200
    assert bool(repo.get(row.id).enabled) is False
    assert repo.get(row.id).api_key == ''
    assert len(repo.list('plex')) == 1


def test_signing_back_in_through_the_handlers_switches_the_destination_on(
        plex_account_api, monkeypatch, schema_session):
    """Sign-out disables the row; completing a sign-in turns it back on.

    Driven through the real endpoints, because the re-enable has to survive the
    order the handlers run in: the PIN step has no server URL yet at that point,
    and the picker that follows is a server switch, which must not touch the
    toggle on its own.
    """
    from api.plex import oauth
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'first-token'
    config.plex.server_url = 'https://first.example:32400'
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    assert bool(repo.get(row.id).enabled) is True

    assert client.post('/api/plex/oauth/logout', headers=HEADERS, json={}).status_code == 200
    assert bool(repo.get(row.id).enabled) is False

    monkeypatch.setattr(oauth.pin_cache, 'get', lambda pin: {'client_id': 'bazarr'})
    monkeypatch.setattr(oauth.pin_cache, 'delete', lambda pin: None)
    monkeypatch.setattr(oauth.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {'authToken': 'second-token'}))
    assert client.get('/api/plex/oauth/pin/123/check', headers=HEADERS).status_code == 200
    assert bool(repo.get(row.id).enabled) is True
    assert repo.get_decrypted_api_key(row.id) == 'second-token'
    # The address it had is kept until the picker supplies a new one.
    assert repo.get(row.id).url == 'https://first.example:32400'
    assert len(repo.list('plex')) == 1


def test_a_server_switch_leaves_a_hand_disabled_destination_disabled(
        plex_account_api, monkeypatch, schema_session):
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'token'
    config.plex.server_url = 'https://first.example:32400'
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    repo.update(row.id, enabled=False)
    plex_tv(monkeypatch, _PlexTV(
        listing={'token': ('https://second.example:32400',)}, machine='abc'))

    assert client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': 'abc', 'name': 'Attic',
        'uri': 'https://second.example:32400'}).status_code == 200
    assert repo.get(row.id).url == 'https://second.example:32400'
    assert bool(repo.get(row.id).enabled) is False


def test_listing_plex_servers_does_not_rewrite_the_selected_connection(plex_account_api,
                                                                       monkeypatch):
    """Opening the Plex tab lists servers. That GET must not persist a
    newly-won connection. Account features still read the plex.* scalars, and
    subtitle refreshes read the destination row, which only moves on an
    explicit pick. If listing re-picks a LAN address over the reverse-proxied
    one, the two surfaces diverge without the user choosing anything.
    """
    from api.plex import oauth
    client, config = plex_account_api
    stored_url = 'https://proxy.example:443'
    lan_url = 'https://192.168.1.10:32400'
    stored_connections = [stored_url, lan_url]
    config.plex.auth_method = 'oauth'
    config.plex.token = 'oauth-token'
    config.plex.server_machine_id = 'machine-abc'
    config.plex.server_url = stored_url
    config.plex.server_local = False
    config.plex.server_connections = list(stored_connections)
    config.plex.get = lambda key, default=None: getattr(config.plex, key, default)

    writes = []
    monkeypatch.setattr(oauth, 'write_config', lambda: writes.append(True))
    monkeypatch.setattr(oauth, 'test_plex_connection', lambda uri, token, verify: (
        True, 5 if uri == lan_url else 50))

    class _Resources:
        status_code = 200
        headers = {'content-type': 'application/json'}

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                'name': 'Attic',
                'clientIdentifier': 'machine-abc',
                'provides': 'server',
                'owned': True,
                'connections': [
                    {'uri': stored_url, 'protocol': 'https',
                     'address': 'proxy.example', 'port': 443, 'local': False},
                    {'uri': lan_url, 'protocol': 'https',
                     'address': '192.168.1.10', 'port': 32400, 'local': True},
                ],
                'productVersion': '1.40.0',
                'platform': 'Linux',
                'device': 'PC',
            }]

    monkeypatch.setattr(oauth.requests, 'get', lambda *args, **kwargs: _Resources())

    response = client.get('/api/plex/oauth/servers', headers=HEADERS)
    assert response.status_code == 200
    servers = response.json['data']
    assert len(servers) == 1
    assert servers[0]['bestConnection']['uri'] == lan_url
    assert config.plex.server_url == stored_url
    assert config.plex.server_local is False
    assert config.plex.server_connections == stored_connections
    assert writes == []


def test_saving_an_api_key_by_hand_reaches_the_destination(plex_account_api, monkeypatch,
                                                           schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.ip = 'plex.example'
    response = client.post('/api/plex/apikey', headers=HEADERS, json={'apikey': 'typed-key'})
    assert response.status_code == 200
    row, = MediaServerInstanceRepository(schema_session).list('plex')
    assert row.url == 'http://plex.example:32400'
    assert MediaServerInstanceRepository(schema_session).get_decrypted_api_key(row.id) == 'typed-key'


def test_signing_out_does_not_let_the_next_startup_point_plex_at_localhost(schema_session):
    """Logging out resets ip, port and auth_method to what Plex ships with.

    The reconcile that follows would otherwise compose http://127.0.0.1:32400
    out of those defaults and write localhost over the address the destination
    actually had, and a later sign-in would switch that on.
    """
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='token',
                           server_url='https://plex.example:32400')
    row = sync_plex_instance(schema_session, config)

    # Sign-out, exactly as the handler leaves the scalars.
    config.general.use_plex = False
    config.plex.token = ''
    config.plex.auth_method = 'apikey'
    config.plex.ip = '127.0.0.1'
    config.plex.port = 32400
    config.plex.ssl = False
    config.plex.server_url = ''
    sync_plex_instance(schema_session, config, signed_out=True)
    assert repo.get(row.id).url == 'https://plex.example:32400'

    # The restart that follows reconciles from those same defaults.
    sync_plex_instance(schema_session, config)
    assert repo.get(row.id).url == 'https://plex.example:32400'

    # An address someone actually configured still replaces it.
    config.plex.ip = 'plex.example'
    config.plex.apikey = 'typed-key'
    sync_plex_instance(schema_session, config, signed_in=True)
    assert repo.get(row.id).url == 'http://plex.example:32400'


def test_saving_a_key_after_a_sign_out_switches_the_destination_back_on(plex_account_api,
                                                                        schema_session):
    """The key save is a sign-in, and a sign-in cannot depend on a scalar that
    the sign-out switched off and this handler had not turned back on."""
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.ip = 'plex.example'
    config.plex.apikey = 'first-key'
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)

    assert client.post('/api/plex/oauth/logout', headers=HEADERS, json={}).status_code == 200
    assert bool(repo.get(row.id).enabled) is False
    assert config.general.use_plex is False

    config.plex.ip = 'plex.example'
    assert client.post('/api/plex/apikey', headers=HEADERS,
                       json={'apikey': 'second-key'}).status_code == 200
    assert bool(repo.get(row.id).enabled) is True
    assert config.general.use_plex is True
    assert repo.get_decrypted_api_key(row.id) == 'second-key'


def test_an_api_key_account_signs_out_before_its_card_deletes(plex_account_api, schema_session,
                                                              monkeypatch):
    """An API-key account has no OAuth sign-in to disconnect in the account
    panel. The card's own Disconnect runs this same sign-out, which clears the
    key, and only then does the delete go through."""
    from media_servers import dispatcher, service
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    monkeypatch.setattr(dispatcher, '_configuration', dispatcher.NativeConfiguration(settings()))
    config.plex.ip = 'plex.example'
    config.plex.apikey = 'legacy-key'
    row = sync_plex_instance(schema_session, config)
    assert service.get_instance(schema_session, row.id)[0]['account_owned'] is True
    assert service.delete_instance(schema_session, row.id) == ({'error_code': 'account_owned'}, 409)

    assert client.post('/api/plex/oauth/logout', headers=HEADERS, json={}).status_code == 200
    assert config.plex.apikey == ''
    assert service.get_instance(schema_session, row.id)[0]['account_owned'] is False
    assert service.delete_instance(schema_session, row.id) == (None, 204)
    assert MediaServerInstanceRepository(schema_session).list('plex') == []


def test_the_account_never_binds_a_row_it_did_not_create(schema_session):
    """An unrecorded id owns nothing, however few Plex rows there are.

    Adopting the only row there is looked harmless while the account was the
    only thing that ever made one. It is not: a row somebody added by hand is
    also the only row there is, and binding it hands the next sign-in that
    server's URL and credential to overwrite.
    """
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    hand_added = repo.create(kind='plex', name='Hand added', url='https://hand.example:32400',
                             api_key='hand-key', enabled=True, verify_ssl=True, path_mappings=[])
    config = plex_settings()
    writes = []

    # Nothing in the account section: no row of its own, and none adopted.
    assert sync_plex_instance(schema_session, config,
                              persist=lambda: writes.append(True)) is None
    assert config.plex.instance_id == ''
    assert writes == []

    # Signing in gets the account its own row beside the hand-added one.
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_url = 'https://account.example:32400'
    owned = sync_plex_instance(schema_session, config, signed_in=True,
                               persist=lambda: writes.append(True))
    assert owned.id != hand_added.id
    assert config.plex.instance_id == owned.id and writes == [True]
    assert repo.get(hand_added.id).url == 'https://hand.example:32400'
    assert repo.get_decrypted_api_key(hand_added.id) == 'hand-key'


def test_a_hand_added_row_survives_a_fresh_sign_in_after_a_disconnect(schema_session):
    """The regression: disconnecting deletes the account's row, and the next
    sign-in must build a new one rather than take over the sibling left behind.

    Disconnecting the Plex destination in the wizard signs the account out and
    deletes its row, which is what the reader asked for. The recorded id then
    points at nothing, and under the old fallback the very next sign-in claimed
    whichever Plex row remained and wrote its own server URL and token over it.
    """
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='account-token',
                           server_url='https://account.example:32400')
    owned = sync_plex_instance(schema_session, config)
    hand_added = repo.create(kind='plex', name='Hand added', url='https://hand.example:32400',
                             api_key='hand-key', enabled=True, verify_ssl=True, path_mappings=[])

    # Disconnect: sign out, then delete the row the account owned.
    config.general.use_plex = False
    config.plex.token = ''
    config.plex.server_url = ''
    sync_plex_instance(schema_session, config, signed_out=True)
    repo.delete(owned.id)
    assert config.plex.instance_id == owned.id, 'the id outlives the row it pointed at'

    # A fresh sign-in, with a server picked.
    config.general.use_plex = True
    config.plex.token = 'new-token'
    config.plex.server_url = 'https://new.example:32400'
    rebuilt = sync_plex_instance(schema_session, config, signed_in=True)

    assert rebuilt.id not in (owned.id, hand_added.id)
    assert config.plex.instance_id == rebuilt.id
    assert repo.get(rebuilt.id).url == 'https://new.example:32400'
    assert repo.get_decrypted_api_key(rebuilt.id) == 'new-token'
    kept = repo.get(hand_added.id)
    assert kept.url == 'https://hand.example:32400'
    assert repo.get_decrypted_api_key(hand_added.id) == 'hand-key'
    assert bool(kept.enabled) is True and bool(kept.verify_ssl) is True


# --- A stored Plex credential never follows a new address ----------------------

def test_a_selection_plex_tv_does_not_list_is_refused_and_stores_nothing(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]
    fake = _PlexTV(listing={'account-token': (LISTED,)},
                  replies={LISTED + '/library/sections': (200, _SECTIONS)})
    plex_tv(monkeypatch, fake)

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': UNLISTED})
    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed'
    assert config.plex.server_url == LISTED
    assert config.plex.server_connections == [LISTED]

    # The library list still uses the stored address only, so the refusal sent
    # the token nowhere new.
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).status_code == 200
    assert fake.server_calls and all(url.startswith(LISTED) for url, _ in fake.server_calls)


def test_a_selection_with_one_unlisted_connection_is_refused_whole(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': LISTED,
        'connections': [LISTED, UNLISTED]})
    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed'
    assert config.plex.server_url == '' and config.plex.server_connections == []


def test_a_selection_plex_tv_does_not_list_is_refused_and_sign_in_is_required(
        plex_account_api, monkeypatch):
    """Without a stored token there is nothing to verify a selection against."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = ''
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': LISTED})
    assert response.status_code == 409
    assert response.json['error_code'] == 'sign_in_required'
    assert config.plex.server_url == ''


def test_a_selection_is_stored_only_when_plex_tv_can_be_reached(plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    fake = _PlexTV(listing={'account-token': (LISTED,)}, status=503)
    plex_tv(monkeypatch, fake)

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': LISTED})
    assert response.status_code == 400
    assert response.json['error_code'] == 'plex_unreachable'
    assert config.plex.server_url == '' and config.plex.server_connections == []
    assert fake.server_calls == []


def test_a_listed_selection_is_stored(plex_account_api, monkeypatch, schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': LISTED,
        'connections': [LISTED]})
    assert response.status_code == 200
    assert config.plex.server_url == LISTED
    assert config.plex.server_connections == [LISTED]
    row, = MediaServerInstanceRepository(schema_session).list('plex')
    assert row.url == LISTED


def test_the_first_library_call_drops_a_stored_address_plex_tv_does_not_list(
        plex_account_api, monkeypatch):
    """An upgrade can leave a selection behind that plex.tv never checked.

    The first use drops it: the token goes nowhere, and the card has to ask
    the user to choose the server again.
    """
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_name = 'Attic'
    config.plex.server_url = UNLISTED
    config.plex.server_connections = [UNLISTED]
    fake = _PlexTV(listing={'account-token': ()})
    plex_tv(monkeypatch, fake)

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200 and response.json == {'data': []}
    assert fake.server_calls == [], 'the token was sent nowhere'
    assert config.plex.server_url == ''
    assert config.plex.server_machine_id == '' and config.plex.server_name == ''
    assert config.plex.server_local is False and config.plex.server_connections == []


def test_a_listed_stored_address_is_used_for_libraries(plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]
    fake = _PlexTV(listing={'account-token': (LISTED,)}, replies={
        LISTED + '/library/sections': (200, _SECTIONS),
        LISTED + '/library/sections/1/all': (200, {'MediaContainer': {'size': 1}}),
        LISTED + '/library/sections/1': (200, {'MediaContainer': {'Directory': [
            {'Location': [{'path': '/media'}]}]}}),
    })
    plex_tv(monkeypatch, fake)

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200
    assert response.json['data'][0]['title'] == 'Movies'
    assert fake.server_calls[0] == (LISTED + '/library/sections', 'account-token')


def test_plex_tv_unreachable_sends_the_token_nowhere_and_retries_after_a_pause(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    fake = _PlexTV(listing={'account-token': (LISTED,)}, status=503)
    plex_tv(monkeypatch, fake)
    from media_servers import plex_account
    clock = {'now': 0.0}
    monkeypatch.setattr(plex_account, 'time',
                        SimpleNamespace(monotonic=lambda: clock['now']))

    for _ in range(2):
        response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
        assert response.status_code == 200 and response.json == {'data': []}
    # One ask is remembered briefly, so reading the destination does not block
    # on a network call on every use while plex.tv is down.
    assert fake.tv_calls == 1, 'an unreachable plex.tv is remembered briefly'
    assert fake.server_calls == []
    # The selection is kept for when plex.tv answers again, and the next use
    # after the pause asks again.
    clock['now'] += plex_account.UNREACHABLE_CACHE_SECONDS + 1
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json == {'data': []}
    assert fake.tv_calls == 2, 'the next use after the pause asks again'
    assert config.plex.server_url == LISTED


def test_a_replaced_token_is_checked_again_before_it_goes_anywhere(
        plex_account_api, monkeypatch):
    """The remembered answer belongs to the credential that asked for it."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'first-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    fake = _PlexTV(listing={'first-token': (LISTED,)}, replies={
        LISTED + '/library/sections': (200, _SECTIONS),
        LISTED + '/library/sections/1/all': (200, {'MediaContainer': {'size': 1}}),
        LISTED + '/library/sections/1': (200, {'MediaContainer': {'Directory': []}}),
    })
    plex_tv(monkeypatch, fake)
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json['data']

    # The token is replaced without a restart, and plex.tv lists nothing for
    # the new account.
    config.plex.token = 'second-token'
    fake.listing = {'second-token': ()}
    fake.tv_calls = 0
    fake.server_calls.clear()

    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json == {'data': []}
    assert fake.tv_calls == 1, 'the new credential asked plex.tv again'
    assert fake.server_calls == [], 'the new token was sent nowhere'


def test_plex_test_connection_sends_the_credential_only_to_a_verified_stored_origin(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]
    # Under the sign-in method the address plex.ip names is not a destination
    # the credential may go to: only what plex.tv lists for the account is.
    config.plex.ip = '203.0.113.10'
    fake = _PlexTV(listing={'account-token': (LISTED,)},
                   replies={LISTED + '/identity': (200, {})})
    plex_tv(monkeypatch, fake)

    response = client.post('/api/plex/test-connection', headers=HEADERS, json={'uri': LISTED})
    assert response.status_code == 200 and response.json == {'success': True}
    assert fake.server_calls == [(LISTED + '/identity', 'account-token')]

    for uri in ('https://listed.example:32401', 'http://listed.example:32400',
                'https://203.0.113.10:32400'):
        fake.server_calls.clear()
        response = client.post('/api/plex/test-connection', headers=HEADERS, json={'uri': uri})
        assert response.status_code == 400, uri
        assert fake.server_calls == [], uri


def test_plex_test_connection_under_the_api_key_method_trusts_only_the_stored_origin(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'server-key'
    config.plex.ip = '203.0.113.10'
    config.plex.port = 32400
    config.plex.ssl = False
    fake = _PlexTV(replies={'http://203.0.113.10:32400/identity': (200, {})})
    plex_tv(monkeypatch, fake)

    response = client.post('/api/plex/test-connection', headers=HEADERS,
                           json={'uri': 'http://203.0.113.10:32400'})
    assert response.status_code == 200 and response.json == {'success': True}

    for uri in ('http://203.0.113.10:32401', 'https://203.0.113.10:32400'):
        fake.server_calls.clear()
        response = client.post('/api/plex/test-connection', headers=HEADERS, json={'uri': uri})
        assert response.status_code == 400, uri
        assert fake.server_calls == [], uri


def test_a_method_switch_between_the_origin_check_and_the_send_cannot_swap_the_credential(
        plex_account_api, monkeypatch):
    """The origin the URI is checked against, the credential and the TLS
    choice the request sends with are one snapshot under the account lock, so
    a settings save switching the method while the test runs cannot make the
    later credential read hand back the sign-in token for an origin the API
    key settings checked."""
    from media_servers import plex_account
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'server-key'
    config.plex.ip = '203.0.113.10'
    config.plex.port = 32400
    config.plex.ssl = False
    fake = _PlexTV(replies={'http://203.0.113.10:32400/identity': (200, {})})
    plex_tv(monkeypatch, fake)

    # The save lands once the route has read the configured address and
    # before it reads the credential, the way a concurrent settings save
    # switching to the sign-in method would: from then on the account holds
    # the sign-in token.
    real_account_url = plex_account.account_url

    def switching_account_url(section):
        url = real_account_url(section)
        config.plex.auth_method = 'oauth'
        config.plex.token = 'account-token'
        return url

    monkeypatch.setattr(plex_account, 'account_url', switching_account_url)

    response = client.post('/api/plex/test-connection', headers=HEADERS,
                           json={'uri': 'http://203.0.113.10:32400'})

    # The request either refuses or sends the snapshot's own credential to
    # the snapshot's own origin; the sign-in token the save switched in
    # never follows the origin the API key settings checked.
    assert fake.server_calls in ([], [('http://203.0.113.10:32400/identity', 'server-key')]), \
        'the sign-in token followed the origin the API key settings checked'
    assert (response.status_code == 200) == bool(fake.server_calls), \
        'the request refused exactly when nothing was sent'


def test_plex_test_connection_refuses_every_origin_while_plex_tv_cannot_be_reached(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}, status=503))

    response = client.post('/api/plex/test-connection', headers=HEADERS, json={'uri': LISTED})
    assert response.status_code == 400
    assert 'could not be reached' in response.json['error']


def test_a_settings_save_cannot_set_a_server_address_plex_tv_does_not_list(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/system/settings',
                           data={'settings-plex-server_url': UNLISTED}, headers=HEADERS)
    assert response.status_code == 406
    assert 'Plex does not list this address' in response.get_data(as_text=True)
    assert config.plex.server_url == ''

    response = client.post('/api/system/settings',
                           data={'settings-plex-server_connections': UNLISTED}, headers=HEADERS)
    assert response.status_code == 406
    assert config.plex.server_connections == []


def test_a_settings_save_of_an_address_plex_tv_lists_passes_the_destination_rule(
        plex_account_api, monkeypatch):
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    app_config.save_settings([('settings-plex-server_url', [LISTED]),
                              ('settings-plex-server_connections', [LISTED])])
    assert saved, 'the save was not refused'


def test_an_api_key_settings_save_needs_the_key_again_to_move_the_address(
        plex_account_api, monkeypatch):
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'

    response = client.post('/api/system/settings',
                           data={'settings-plex-ip': 'moved.example'}, headers=HEADERS)
    assert response.status_code == 406
    assert 'Re-enter the key to use a different address' in response.get_data(as_text=True)
    assert config.plex.ip == 'plex.example'

    # With the key carried, the save passes the destination rule.
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))
    app_config.save_settings([('settings-plex-ip', ['moved.example']),
                              ('settings-plex-apikey', ['typed-again'])])
    assert saved


def test_the_account_row_reaches_refreshes_only_while_plex_tv_lists_its_address(
        schema_session, monkeypatch):
    """The refresh workers send the account token to the row's address, so the
    row is dispatched to them only while plex.tv lists that address for the
    signed-in account. The check runs once per credential per start and again
    after every short pause while plex.tv does not answer."""
    from media_servers import plex_account
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    repo = MediaServerInstanceRepository(schema_session)
    config = plex_settings(auth_method='oauth', token='account-token',
                           server_url='https://listed.example:32400')
    config.plex.server_machine_id = 'machine-abc'
    row = sync_plex_instance(schema_session, config)

    def answer(status, uris):
        import requests
        plex_account.forget_plex_tv_answer()
        monkeypatch.setattr(requests, 'get', lambda *_args, **_kwargs: SimpleNamespace(
            status_code=status, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': 'machine-abc',
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': uri} for uri in uris]}]))

    answer(200, ('https://listed.example:32400',))
    snapshot = repo.snapshot(row.id, config)
    assert snapshot.enabled is True and snapshot.sendable is True
    answer(503, ())
    snapshot = repo.snapshot(row.id, config)
    assert snapshot.enabled is True, 'the switch the user set is left alone'
    assert snapshot.sendable is False, 'plex.tv unreachable: no token is sent'
    answer(200, ())
    snapshot = repo.snapshot(row.id, config)
    assert snapshot.sendable is False, 'an unlisted address is not sent to'

    # A token the settings no longer carry is never dispatched: the row would
    # send its own credential, which plex.tv was never asked about. A failed
    # row sync after a replacement is exactly this state.
    config.plex.token = 'replaced-token'
    answer(200, ('https://listed.example:32400',))
    assert repo.snapshot(row.id, config).sendable is False, 'a row/settings mismatch holds the refresh'
    # The sync carries the replacement onto the row, and the refresh resumes.
    sync_plex_instance(schema_session, config)
    assert repo.get_decrypted_api_key(row.id) == 'replaced-token'
    assert repo.snapshot(row.id, config).sendable is True

    # Clearing the credential without signing out holds it too.
    config.plex.token = ''
    plex_account.forget_plex_tv_answer()
    assert repo.snapshot(row.id, config).sendable is False

    # A row the account does not own is none of this rule's business: its key
    # was typed with its address.
    config.plex.token = 'account-token'
    other = repo.create(kind='plex', name='Hand added', url='https://hand.example:32400',
                        api_key='hand-key', enabled=True, verify_ssl=True, path_mappings=[])
    answer(503, ())
    other_snapshot = repo.snapshot(other.id, config)
    assert other_snapshot.enabled is True and other_snapshot.sendable is True


def test_a_url_two_parsers_read_differently_is_refused_everywhere(plex_account_api,
                                                                  monkeypatch):
    """A backslash authority names the listed host for one parser and another
    host for the HTTP library, so the credential would follow it to the second.
    It is refused by the picker, by the settings save and by the connection
    test, and nothing is stored from it."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]
    fake = _PlexTV(listing={'account-token': (LISTED,)},
                   replies={LISTED + '/identity': (200, {})})
    plex_tv(monkeypatch, fake)
    sneaky = r'https://attacker.example\@' + LISTED

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': sneaky,
        'connections': [sneaky, LISTED]})
    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed'
    assert config.plex.server_url == LISTED
    assert config.plex.server_connections == [LISTED]

    response = client.post('/api/system/settings',
                           data={'settings-plex-server_url': sneaky}, headers=HEADERS)
    assert response.status_code == 406
    assert config.plex.server_url == LISTED

    response = client.post('/api/plex/test-connection', headers=HEADERS, json={'uri': sneaky})
    assert response.status_code == 400
    assert fake.server_calls == [], 'the token was sent nowhere'

    # A stored selection built out of such a string is dropped at first use
    # rather than connected to.
    config.plex.server_url = sneaky
    config.plex.server_connections = [sneaky]
    plex_tv(monkeypatch, fake)
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json == {'data': []}
    assert config.plex.server_url == '' and config.plex.server_connections == []
    assert fake.server_calls == [], 'the token was sent nowhere'


def test_a_mixed_case_settings_key_cannot_move_the_plex_address(plex_account_api, monkeypatch):
    """The settings store is case-insensitive, so the guard parses keys the way
    the writer does, and a mixed-case name cannot skip the destination rule."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    for key in ('settings-Plex-Server_Url', 'settings-plex-SERVER_URL'):
        response = client.post('/api/system/settings',
                               data={key: UNLISTED}, headers=HEADERS)
        assert response.status_code == 406, key
        assert 'Plex does not list this address' in response.get_data(as_text=True), key
        assert config.plex.server_url == LISTED, key


def test_switching_methods_cannot_move_the_address_past_the_other_rules_check(
        plex_account_api, monkeypatch):
    """The rule runs under the method the save will leave in place, so
    submitting the API key method alongside a moved host does not skip the
    check the API key method owes."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.apikey = 'old-key'
    config.plex.ip = 'plex.example'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/system/settings',
                           data={'settings-plex-auth_method': 'apikey',
                                 'settings-plex-ip': 'moved.example'}, headers=HEADERS)
    assert response.status_code == 406
    assert 'Re-enter the key to use a different address' in response.get_data(as_text=True)
    assert config.plex.ip == 'plex.example'
    assert config.plex.auth_method == 'oauth', 'the refused save changed nothing'


def test_selecting_the_api_key_method_cannot_move_the_address_past_the_stored_token(
        plex_account_api, monkeypatch):
    """The plex.tv listing is owed whenever the sign-in token stays stored,
    whatever method the save selects: the method that reads the token can be
    switched back, so an address saved while it is retained is verified now
    rather than only after the switch back."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([('settings-plex-auth_method', ['apikey']),
                                  ('settings-plex-server_url', [UNLISTED])])
    assert 'Plex does not list this address' in str(refusal.value)
    assert saved == [], 'the refused save reached no writer'

    # An address plex.tv lists is stored, method switch and all.
    app_config.save_settings([('settings-plex-auth_method', ['apikey']),
                              ('settings-plex-server_url', [LISTED])])
    assert saved, 'the save was refused'


def test_a_method_change_drops_the_remembered_answer(plex_account_api, monkeypatch):
    """The remembered plex.tv answer belongs to the credential and the method,
    so switching methods asks again rather than trusting the other method's."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    fake = _PlexTV(listing={'account-token': (LISTED,)},
                   replies={LISTED + '/library/sections': (200, _SECTIONS)})
    plex_tv(monkeypatch, fake)
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json['data']
    assert fake.tv_calls == 1

    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'server-key'
    fake.listing = {'server-key': (LISTED,)}
    fake.tv_calls = 0
    plex_tv(monkeypatch, fake)
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json['data']
    assert fake.tv_calls == 1, 'the new method asked plex.tv again'


@pytest.mark.parametrize('field,value', [('port', '32500'), ('ssl', 'true')])
def test_the_api_key_method_needs_the_key_for_every_address_field(
        plex_account_api, monkeypatch, field, value):
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'
    config.plex.port = 32400
    config.plex.ssl = False

    response = client.post('/api/system/settings',
                           data={f'settings-plex-{field}': value}, headers=HEADERS)
    assert response.status_code == 406
    assert 'Re-enter the key to use a different address' in response.get_data(as_text=True)


@pytest.mark.parametrize('field,stored', [('ssl', True), ('ip', 'plex.example'),
                                           ('port', 32500)])
@pytest.mark.parametrize('spelling', ['null', 'None', 'undefined'])
def test_an_empty_spelling_of_an_address_field_still_moves_the_address(
        plex_account_api, monkeypatch, field, stored, spelling):
    """An empty spelling clears the stored value, and the cleared field falls
    back to the default the connection applies, so the effective destination
    moves from the stored address to the default one while the stored key
    stays stored: the same re-enter refusal a typed move gets, on the HTTP
    route the settings page saves through. The unfixed rule skipped values
    the writer normalizes to nothing, so the cleared field moved the address
    with the key kept."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'
    config.plex.port = 32400
    config.plex.ssl = False
    setattr(config.plex, field, stored)
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    response = client.post('/api/system/settings',
                           data={f'settings-plex-{field}': spelling}, headers=HEADERS)

    assert response.status_code == 406
    assert 'Re-enter the key to use a different address' in response.get_data(as_text=True)
    assert saved == [], 'the refused save reached no writer'
    assert config.plex.get(field) == stored, "the refused save changed nothing"


def test_turning_certificate_verification_off_under_the_sign_in_method_is_refused(
        plex_account_api, monkeypatch):
    """The sign-in token cannot be typed again, so it cannot be asked to travel
    without certificate verification either; the account has to sign in again."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.verify_ssl = True
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/system/settings',
                           data={'settings-plex-verify_ssl': 'false'}, headers=HEADERS)
    assert response.status_code == 406
    assert 'Sign in to Plex again' in response.get_data(as_text=True)
    assert config.plex.verify_ssl is True


def test_switching_to_the_api_key_method_cannot_turn_verification_off_on_a_stored_token(
        plex_account_api, monkeypatch):
    """The stored sign-in token stays stored across a method switch, and the
    method that reads it can be switched back, so turning certificate
    verification off is refused while the token is there even when the save
    switches to the API key method and keeps no key to re-enter."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.apikey = ''
    config.plex.verify_ssl = True
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([('settings-plex-auth_method', ['apikey']),
                                  ('settings-plex-verify_ssl', ['false'])])
    assert 'Sign in to Plex again' in str(refusal.value)
    assert not saved, 'the refused save reached no writer'
    assert config.plex.auth_method == 'oauth', 'the refused save changed nothing'
    assert config.plex.verify_ssl is True


@pytest.mark.parametrize('apikey,stored', [('typed-again', 'stored-plex-key'),
                                           ('', 'stored-plex-key'),
                                           ('typed-again', '')])
def test_an_address_move_with_a_replaced_or_cleared_or_absent_key_passes(
        plex_account_api, monkeypatch, apikey, stored):
    """A replaced or explicitly cleared key travels no stored secret, and an
    install with no key stored has nothing to re-enter."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = stored
    config.plex.ip = 'plex.example'
    plex_tv(monkeypatch, _PlexTV())
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    items = [('settings-plex-ip', ['moved.example'])]
    if apikey is not None:
        items.append(('settings-plex-apikey', [apikey]))
    app_config.save_settings(items)
    assert saved, 'the save was not refused'


def test_an_address_move_keeping_a_stored_key_is_refused_even_partially_submitted(
        plex_account_api, monkeypatch):
    """An omitted key keeps the stored one, so the move is refused."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'
    plex_tv(monkeypatch, _PlexTV())
    with pytest.raises(Exception) as refusal:
        app_config.save_settings([('settings-plex-ip', ['moved.example'])])
    assert 'Re-enter the key' in str(refusal.value)


def test_an_unlisted_fallback_is_skipped_and_removed_while_a_listed_primary_is_used(
        plex_account_api, monkeypatch):
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED, UNLISTED]
    fake = _PlexTV(listing={'account-token': (LISTED,)}, replies={
        LISTED + '/library/sections': (200, _SECTIONS),
        LISTED + '/library/sections/1/all': (200, {'MediaContainer': {'size': 1}}),
        LISTED + '/library/sections/1': (200, {'MediaContainer': {'Directory': [
            {'Location': [{'path': '/media'}]}]}}),
    })
    plex_tv(monkeypatch, fake)

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200 and response.json['data']
    assert all(url.startswith(LISTED) for url, _ in fake.server_calls), \
        'the token went to the listed address only'
    assert config.plex.server_connections == [LISTED], \
        'the fallback plex.tv does not list is removed from the settings'


def test_a_redirect_is_never_followed_with_the_token(plex_account_api, monkeypatch):
    """Every credential-bearing request refuses to follow a redirect: a server
    that answers with one is not the server the address was verified for."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    fake = _PlexTV(listing={'account-token': (LISTED,)},
                   replies={LISTED + '/library/sections': (302, {})})
    plex_tv(monkeypatch, fake)

    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json == {'data': []}
    assert fake.server_calls == [(LISTED + '/library/sections', 'account-token')]
    assert fake.redirect_flags and all(flag is False for flag in fake.redirect_flags), \
        'every credential-bearing request opted out of redirects'


def test_an_unreadable_plex_tv_answer_sends_nothing_and_wipes_nothing(
        plex_account_api, monkeypatch):
    """A 200 that cannot be read (a captive portal, a proxy error page, JSON
    that is not a list of resources) is plex.tv not answering: nothing is sent,
    nothing is wiped, and the next use asks again."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = UNLISTED
    config.plex.server_connections = [UNLISTED]

    for content_type, body in (('text/html', '<html>captive portal</html>'),
                                ('application/json', {'error': 'not a list'})):
        fake = _PlexTV(listing={'account-token': (LISTED,)},
                       content_type=content_type, body=body)
        plex_tv(monkeypatch, fake)

        response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
        assert response.status_code == 200 and response.json == {'data': []}
        assert fake.server_calls == [], 'the token was sent nowhere'
        assert config.plex.server_url == UNLISTED, 'the selection was kept'
        assert config.plex.server_connections == [UNLISTED]


def test_a_refused_credential_signs_in_again_without_wiping_the_selection(
        plex_account_api, monkeypatch):
    """A 401 or 403 from plex.tv answers sign-in-again: neither remembered nor
    cleared, so a transient auth failure cannot drop the stored selection."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'expired-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]
    fake = _PlexTV(listing={'account-token': (LISTED,)}, status=401)
    plex_tv(monkeypatch, fake)

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200 and response.json == {'data': []}
    assert config.plex.server_url == LISTED, 'the selection was kept'
    assert client.get('/api/plex/oauth/libraries', headers=HEADERS).json == {'data': []}
    assert fake.tv_calls == 2, 'a refused credential is not remembered'

    response = client.post('/api/plex/test-connection', headers=HEADERS,
                           json={'uri': LISTED})
    assert response.status_code == 409
    assert response.json['error_code'] == 'sign_in_required'


def test_the_picker_refuses_connection_entries_that_are_not_strings(
        plex_account_api, monkeypatch):
    """Whatever a non-string entry would store was never checked against the
    listing, so the whole selection is refused."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': LISTED,
        'connections': [LISTED, {'uri': UNLISTED}]})
    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed'
    assert config.plex.server_url == '' and config.plex.server_connections == []


def test_a_settings_save_stores_the_address_plex_tv_listed(plex_account_api, monkeypatch):
    """A submitted string that normalizes to a listed origin is stored as the
    URI plex.tv returned, not as the caller's spelling of it."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    app_config.save_settings([('settings-plex-server_url', ['https://LISTED.example:32400']),
                              ('settings-plex-server_connections', ['https://LISTED.example:32400'])])
    assert saved, 'the save was not refused'
    written = {key: list(values) for key, values in saved[0]}
    assert written['settings-plex-server_url'] == [LISTED]
    assert written['settings-plex-server_connections'] == [LISTED]


# --- One credential, one address, one spelling --------------------------------

def test_one_settings_name_submitted_twice_is_refused_whatever_its_spelling(
        plex_account_api, monkeypatch):
    """The store is case-insensitive, so two spellings of one name reach the
    same stored key and the writer would apply the last of them after every
    check had read only the first. A save that submits the name twice is
    refused rather than silently preferring one of the values."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'
    plex_tv(monkeypatch, _PlexTV())
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    for items in (
            [('settings-plex-ip', ['plex.example']), ('settings-Plex-ip', ['evil.example'])],
            [('settings-plex-verify_ssl', ['true']), ('settings-Plex-verify_ssl', ['false'])],
            [('settings-plex-auth_method', ['oauth']), ('settings-Plex-auth_method', ['apikey']),
             ('settings-plex-ip', ['evil.example'])],
    ):
        with pytest.raises(Exception) as refusal:
            app_config.save_settings(list(items))
        assert 'can only be submitted once' in str(refusal.value), items
    assert saved == [], 'no refused save reached the writer'


def test_switching_the_method_back_cannot_park_a_moved_host_on_a_kept_key(
        plex_account_api, monkeypatch):
    """The stored API key travels to the host, port and TLS settings whatever
    method the save leaves in place, so moving the host under the sign-in
    method is refused exactly as moving it under the API key method is: a later
    save could switch the method back and the kept key would follow the moved
    host. The writer is stubbed out, so each save is recorded on the settings
    the way the writer would have stored it."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'
    plex_tv(monkeypatch, _PlexTV())
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    app_config.save_settings([('settings-plex-auth_method', ['oauth'])])
    config.plex.auth_method = 'oauth'
    with pytest.raises(Exception) as refusal:
        app_config.save_settings([('settings-plex-ip', ['evil.example'])])
    assert 'Re-enter the key to use a different address' in str(refusal.value)
    app_config.save_settings([('settings-plex-auth_method', ['apikey'])])
    config.plex.auth_method = 'apikey'
    assert config.plex.ip == 'plex.example', 'no save in between moved the host'
    assert len(saved) == 2, 'the refused save in the middle reached no writer'


@pytest.mark.parametrize('field,value', [('verify_ssl', 'true')])
def test_turning_certificate_verification_on_under_the_api_key_needs_no_key(
        plex_account_api, monkeypatch, field, value):
    """Turning verification back on only hardens the connection, so the stored
    key keeps travelling to the same address and needs no re-entry."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.ip = 'plex.example'
    config.plex.verify_ssl = False
    plex_tv(monkeypatch, _PlexTV())
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    app_config.save_settings([(f'settings-plex-{field}', [value])])
    assert saved, 'the save was not refused'


def test_turning_certificate_verification_off_under_the_api_key_needs_the_key(
        plex_account_api, monkeypatch):
    """Turning verification off hands the stored key to whatever answers in
    place of the server, so it counts as moving the destination."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.verify_ssl = True
    plex_tv(monkeypatch, _PlexTV())

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([('settings-plex-verify_ssl', ['false'])])
    assert 'Re-enter the key to use a different address' in str(refusal.value)


def test_a_rotation_between_the_verification_and_the_send_cannot_swap_the_credential(
        plex_account_api, monkeypatch):
    """The route's own token read is stale the moment a sign-in lands, so the
    credential the plex.tv listing was verified with is the one every send
    carries: a stale one never reaches an address verified for a fresh one."""
    client, config = plex_account_api
    from api.plex import oauth
    config.plex.auth_method = 'oauth'
    config.plex.token = 'fresh-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]
    fake = _PlexTV(listing={'fresh-token': (LISTED,)}, replies={
        LISTED + '/library/sections': (200, _SECTIONS),
        LISTED + '/library/sections/1/all': (200, {'MediaContainer': {'size': 1}}),
        LISTED + '/library/sections/1': (200, {'MediaContainer': {'Directory': [
            {'Location': [{'path': '/media'}]}]}}),
    })
    plex_tv(monkeypatch, fake)
    # A sign-in landed between the route's first read and the verification.
    monkeypatch.setattr(oauth, 'get_decrypted_token',
                        lambda: 'stale-token' if fake.tv_calls == 0 else 'fresh-token')

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200 and response.json['data']
    assert all(token == 'fresh-token' for _url, token in fake.server_calls), \
        'the credential the listing was verified with is the one that was sent'
    assert 'stale-token' not in [token for _url, token in fake.server_calls]


def test_the_pooled_plex_session_refuses_a_redirect_on_every_verb(redirect_pair):
    """Every path that talks to a Plex server shares this session, and its
    credential never follows a redirect: a server that answers with one is
    not the server the address was verified for, so the next hop is never
    contacted."""
    import requests
    from plex.operations import _build_pooled_session
    source, target = redirect_pair()
    session = _build_pooled_session(verify=False)

    with pytest.raises(requests.exceptions.TooManyRedirects):
        session.get(source.url + '/library/sections',
                    headers={'X-Plex-Token': 'account-token'}, timeout=5)
    with pytest.raises(requests.exceptions.TooManyRedirects):
        session.post(source.url + '/library/sections',
                     headers={'X-Plex-Token': 'account-token'}, timeout=5)

    assert target.records == [], 'the credential never reached the second origin'
    assert len(source.records) == 2, 'each request was answered by the first origin only'


def test_a_plex_probe_refuses_to_follow_a_redirect_to_another_origin(redirect_pair):
    """The instance probe answers a redirect with a connection error rather
    than letting the credential cross to the origin it names."""
    from media_servers import service
    source, target = redirect_pair()

    result = service._plex_probe(source.url, 'account-token', False, False)

    assert result == {'success': False, 'error_code': 'connection_error'}
    assert target.records == [], 'the credential never reached the second origin'


def test_a_plex_refresh_refuses_to_follow_a_redirect_to_another_origin(redirect_pair):
    """A refresh whose section listing answers with a redirect fails with its
    reason rather than sending the token to the origin the redirect names."""
    from media_servers.http import MediaServerError
    from plex.refresh import PlexRefreshClient
    source, target = redirect_pair()

    with PlexRefreshClient(snapshot('plex', url=source.url, apikey='account-token',
                                   verify_ssl=False,
                                   options={'movie_libraries': ['Movies']})) as client:
        with pytest.raises(MediaServerError) as refusal:
            client.refresh_library('movie')
    assert str(refusal.value) == 'connection_error'
    assert target.records == [], 'the credential never reached the second origin'


def test_connecting_for_operations_refuses_to_follow_a_redirect_to_another_origin(
        redirect_pair, monkeypatch):
    """The account connection itself is refused rather than completed through
    a redirect, and the origin the redirect names never sees the token."""
    from plex import operations
    source, target = redirect_pair(redirect_path='/')
    config = plex_settings(auth_method='oauth', token='account-token',
                           server_url=source.url, server_machine_id=PLEX_MACHINE)
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (source.url,)}))
    monkeypatch.setattr(operations, 'settings', config)

    with pytest.raises(Exception):
        operations.get_plex_server()

    assert target.records == [], 'the credential never reached the second origin'


def test_an_account_row_destination_is_resolved_to_the_uri_plex_tv_listed(
        schema_session, monkeypatch):
    """The row's address is a legacy spelling of a listed origin, so what the
    refresh workers and the probe are handed is the URI plex.tv itself
    returned, never the stored spelling."""
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    stored = LISTED + '/legacy'
    config = plex_settings(auth_method='oauth', token='account-token',
                           server_url=stored)
    config.plex.server_machine_id = 'machine-abc'
    row = sync_plex_instance(schema_session, config)
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    snapshot = MediaServerInstanceRepository(schema_session).snapshot(row.id, config)

    assert snapshot.sendable is True
    assert snapshot.url == LISTED, 'the snapshot carries the URI plex.tv listed'


def test_a_stored_selection_spelled_differently_is_rewritten_to_the_listed_uri(
        plex_account_api, monkeypatch, schema_session):
    """The selection and the row that reads it keep the URI plex.tv listed for
    the same origin, so the refresh, probe and status paths all send to the
    address plex.tv itself returned."""
    import app.database
    from media_servers.plex_account import (sync_plex_instance, verified_server_addresses)
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    stored = LISTED + '/legacy'
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = stored
    row = sync_plex_instance(schema_session, config)
    monkeypatch.setattr(app.database, 'database', schema_session)
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))
    persisted = []

    result = verified_server_addresses(
        config, persist=lambda: persisted.append(True))

    # The rewrite itself is asserted before the answer is unpacked, so a
    # source that returns a differently shaped answer fails on the missing
    # rewrite rather than on the unpack.
    assert config.plex.server_url == LISTED, 'the selection was rewritten'
    assert MediaServerInstanceRepository(schema_session).get(row.id).url == LISTED, \
        'the row was rewritten with it'
    assert result == ([LISTED], 'ok', 'account-token')
    assert persisted, 'the rewritten selection was persisted'


def test_the_status_page_probes_no_destination_the_rule_holds_back(
        schema_session, monkeypatch):
    """The version probe runs on the snapshot a refresh would be dispatched
    with, so a destination the rule holds back is not probed either: its
    credential is sent nowhere, not even by the status page."""
    from media_servers import versions
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    config = plex_settings(auth_method='oauth', token='account-token',
                           server_url=UNLISTED)
    config.plex.server_machine_id = 'machine-abc'
    row = sync_plex_instance(schema_session, config)
    assert MediaServerInstanceRepository(schema_session).snapshot(row.id, config).sendable is False
    probed = []
    monkeypatch.setattr(versions, '_probe', lambda snap: probed.append(snap) or {'success': False})
    versions.reset()

    entries = versions.statuses(schema_session, config)

    assert entries == [], 'a held destination has no version entry'
    for _attempt in range(50):
        with versions._lock:
            busy = bool(versions._inflight)
        if not busy:
            break
        time.sleep(0.02)
    assert probed == [], 'no probe was scheduled for the held destination'


def test_a_parseable_document_that_is_not_a_resource_listing_preserves_the_selection(
        plex_account_api, monkeypatch):
    """An XML answer whose root is not a resource listing is plex.tv not
    answering, not plex.tv listing nothing: nothing is sent, and a stored
    selection is kept rather than remembered as empty and dropped."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = UNLISTED
    config.plex.server_connections = [UNLISTED]

    fake = _PlexTV(listing={'account-token': (LISTED,)},
                   content_type='application/xml', xml='<html>captive portal</html>')
    plex_tv(monkeypatch, fake)

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200 and response.json == {'data': []}
    assert fake.server_calls == [], 'the token was sent nowhere'
    assert config.plex.server_url == UNLISTED, 'the selection was kept'
    assert config.plex.server_connections == [UNLISTED]


def test_a_listed_ipv6_address_keeps_its_selection_and_receives_the_token(
        plex_account_api, monkeypatch):
    """A bracketed IPv6 literal is the one host two URL parsers spell
    differently, so the origin comparison strips the brackets: a stored IPv6
    selection plex.tv also lists is used, not wiped."""
    client, config = plex_account_api
    address = 'http://[2001:db8::1]:32400'
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = address
    config.plex.server_connections = [address]
    fake = _PlexTV(listing={'account-token': (address,)}, replies={
        address + '/library/sections': (200, _SECTIONS),
        address + '/library/sections/1/all': (200, {'MediaContainer': {'size': 1}}),
        address + '/library/sections/1': (200, {'MediaContainer': {'Directory': [
            {'Location': [{'path': '/media'}]}]}}),
    })
    plex_tv(monkeypatch, fake)

    response = client.get('/api/plex/oauth/libraries', headers=HEADERS)
    assert response.status_code == 200 and response.json['data'], \
        'the bracketed address was treated as listed'
    assert config.plex.server_url == address, 'the selection was not wiped'
    assert fake.server_calls, 'the token reached the listed IPv6 address'


def test_a_held_account_snapshot_is_asked_again_once_plex_tv_recovers(
        schema_session, monkeypatch):
    """Snapshots are computed once and republished only by an instance write or
    an account sync, so one plex.tv failure at the wrong moment must not hold
    Plex refreshes until a restart: reading a held account snapshot asks
    plex.tv again once the unreachable pause has passed, and a dispatch that
    arrives after the answer runs rather than staying pending."""
    import requests

    from media_servers import plex_account, resolution
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    config = plex_settings(auth_method='oauth', token='account-token', server_url=LISTED,
                           movie_library=['Movies'])
    config.plex.server_machine_id = PLEX_MACHINE
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    clock = {'now': 1000.0}
    monkeypatch.setattr(plex_account, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    state = {'status': 503}

    def fake(url, **_kwargs):
        return SimpleNamespace(
            status_code=state['status'], headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': PLEX_MACHINE,
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': LISTED}]}])

    monkeypatch.setattr(requests, 'get', fake)
    plex_account.forget_plex_tv_answer()

    # The configuration is built while plex.tv does not answer.
    configuration = NativeConfiguration(config, snapshots=repo.snapshots(config))
    assert configuration.read(row.id)[1].sendable is False
    # Still inside the short pause, the answer stays no.
    assert configuration.read(row.id)[1].sendable is False

    # plex.tv answers again, well past the unreachable pause.
    state['status'] = 200
    clock['now'] += 3600
    revision, snapshot, _changing = configuration.read(row.id)
    assert snapshot.sendable is True, 'the held snapshot was released'
    assert snapshot.url == LISTED, 'the released snapshot carries the listed URI'
    assert revision == snapshot.revision

    # A refresh that arrives now dispatches instead of waiting for a restart.
    client = _Recorder('plex', {resolution.LIBRARY})
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *_args: client,
                                   metadata_factory=lambda _event: metadata())
    dispatcher.notify(mutation())
    assert dispatcher.wait_idle(10, server=row.id)
    assert dispatcher.status(row.id)['state'] == 'requested'


def test_the_locked_initialization_holds_the_account_row_rather_than_asking_plex_tv(
        schema_session, monkeypatch):
    """get_native_configuration builds the saved snapshots while it holds the
    one lock every dispatcher caller shares, so a plex.tv ask there would hold
    the whole refresh machinery for the ask's whole timeout. The account's row
    is published held instead, and the ask moves outside the locked
    initialization: reading the row asks plex.tv and releases it once the
    answer says the credential may be sent to the address plex.tv listed."""
    import requests

    from app import config as app_config, database as app_database
    from app.database import TableMediaServerImports
    from media_servers import dispatcher, plex_account
    from media_servers.plex_account import sync_plex_instance
    config = plex_settings(auth_method='oauth', token='account-token', server_url=LISTED)
    config.plex.server_machine_id = PLEX_MACHINE
    monkeypatch.setattr(app_config, 'settings', config)
    monkeypatch.setattr(app_database, 'database', schema_session)
    monkeypatch.setattr(dispatcher, '_configuration', None)
    row = sync_plex_instance(schema_session, config)
    schema_session.add(TableMediaServerImports(kind='plex'))
    schema_session.flush()
    asks = []

    def fake(url, **_kwargs):
        asks.append(url)
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': PLEX_MACHINE,
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': LISTED}]}])

    monkeypatch.setattr(requests, 'get', fake)
    plex_account.forget_plex_tv_answer()

    native = dispatcher.get_native_configuration()

    assert asks == [], 'the locked initialization asked plex.tv for nothing'
    published, = native.list()
    assert published.sendable is False, 'the account row is held until plex.tv has answered'
    _revision, released, _changing = native.read(row.id)
    assert asks, 'reading the row asks plex.tv outside the locked initialization'
    assert released.sendable is True, 'the row was released once plex.tv answered'
    assert released.url == LISTED, 'the released row carries the URI plex.tv listed'


def test_reading_a_held_account_row_does_not_hold_the_shared_lock(
        schema_session, monkeypatch):
    """The held-row release can reach plex.tv, and that ask takes up to its
    whole timeout: it runs without the lock every dispatcher caller shares,
    so a read that arrives while the first one is still asking is answered
    rather than blocked behind it, and the released row is published only
    when the answer still finds it the current one."""
    import requests

    from media_servers import plex_account
    from media_servers.dispatcher import NativeConfiguration
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    config = plex_settings(auth_method='oauth', token='account-token', server_url=LISTED,
                           movie_library=['Movies'])
    config.plex.server_machine_id = PLEX_MACHINE
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    asking = threading.Event()
    proceed = threading.Event()
    asks = []
    asks_lock = threading.Lock()

    def fake(url, **_kwargs):
        with asks_lock:
            first = not asks
            asks.append(url)
        if first:
            asking.set()
            assert proceed.wait(10), 'the first ask was never let finish'
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': PLEX_MACHINE,
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': LISTED}]}])

    monkeypatch.setattr(requests, 'get', fake)
    plex_account.forget_plex_tv_answer()
    configuration = NativeConfiguration(config, snapshots=repo.snapshots(config,
                                                                         hold_account_row=True))
    published, = configuration.list()
    assert published.sendable is False, 'the account row starts out held'

    first_read = []
    reader = threading.Thread(target=lambda: first_read.append(configuration.read(row.id)))
    reader.start()
    assert asking.wait(10), 'reading the held row asked plex.tv'

    # A second read that arrives while the first one is still asking is
    # answered, not held behind the ask on the lock every caller shares.
    second_read = []
    arrived = threading.Thread(target=lambda: second_read.append(configuration.read(row.id)))
    arrived.start()
    arrived.join(5)
    assert not arrived.is_alive(), 'a second read was blocked behind the plex.tv ask'
    assert second_read[0][1].sendable is True, 'the second read released the row'

    proceed.set()
    reader.join(10)
    assert not reader.is_alive()
    _revision, released, _changing = first_read[0]
    assert released.sendable is True and released.url == LISTED, \
        'the first read answered with the released row'


def test_an_older_verification_cannot_erase_a_newer_account_selection(
        plex_account_api, monkeypatch):
    """The plex.tv ask can outlast a sign-in landing in the meantime, so the
    account and its selection are read once and written to only when they are
    still what is stored: an older account's answer never clears or rewrites
    the selection a newer account just made."""
    import requests

    from media_servers import plex_account
    from media_servers.plex_account import verified_server_addresses
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'old-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = UNLISTED
    config.plex.server_connections = [UNLISTED]
    listings = {
        'old-token': [],
        'new-token': [{'name': 'Attic', 'clientIdentifier': 'machine-new',
                       'provides': 'server', 'owned': True,
                       'connections': [{'uri': LISTED}]}],
    }

    def plex_tv(url, **_kwargs):
        token = _kwargs['headers'].get('X-Plex-Token')
        if token == 'old-token':
            # A newer sign-in lands while the older account's ask is in
            # flight, and picks its own server.
            config.plex.token = 'new-token'
            config.plex.server_machine_id = 'machine-new'
            config.plex.server_url = LISTED
            config.plex.server_connections = [LISTED]
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: listings[token])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    addresses, status, credential = verified_server_addresses(config, persist=lambda: None)

    assert (addresses, status, credential) == ([LISTED], 'ok', 'new-token')
    assert config.plex.server_url == LISTED, 'the newer selection was not erased'
    assert config.plex.server_machine_id == 'machine-new'
    assert config.plex.server_connections == [LISTED]


def test_a_settings_save_changing_the_account_waits_for_an_older_verification(
        schema_session, monkeypatch):
    """A settings save that replaces the account credential is an account
    transition too, so it runs under the account lock like every verification's
    write phase: while an older verification is inside its write phase, past
    its account check, the save waits for it, and the older account's answer
    can no longer clear the selection the newer save just stored."""
    import threading

    import requests

    from app import config as app_config, database as app_database
    from app.config import settings as app_settings
    from media_servers import plex_account
    from media_servers.plex_account import verified_server_addresses
    monkeypatch.setattr(app_database, 'database', schema_session)
    for field, value in (('auth_method', 'oauth'), ('token', 'old-token'),
                         ('server_machine_id', PLEX_MACHINE), ('server_url', UNLISTED)):
        monkeypatch.setattr(app_settings.plex, field, value)
    listings = {
        'old-token': [],
        'new-token': [{'name': 'Attic', 'clientIdentifier': PLEX_MACHINE,
                       'provides': 'server', 'owned': True,
                       'connections': [{'uri': LISTED}]}],
    }
    asked = threading.Event()
    in_write_phase = threading.Event()
    save_finished = threading.Event()
    save_contended = threading.Event()

    def plex_tv(url, **_kwargs):
        token = _kwargs['headers'].get('X-Plex-Token')
        if token == 'old-token':
            asked.set()
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: listings[token])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    # The older verification is held inside its write phase, past the account
    # check it made under the lock and before the write that clears the
    # selection plex.tv does not list for it. It resumes once the save has
    # either finished writing, the unfixed order, or asked for the account
    # lock the verification still holds, the fixed one. What its clear wipes
    # is recorded: the older selection at the fixed head, the save's newer one
    # at the unfixed head.
    erased = []
    original_clear = plex_account._clear_server_selection

    def gated_clear(section, persist):
        in_write_phase.set()
        deadline = time.monotonic() + 10
        while not (save_finished.is_set() or save_contended.is_set()):
            assert time.monotonic() < deadline, \
                'the save neither finished nor waited for the account lock'
            time.sleep(0.01)
        erased.append(getattr(section, 'server_url', '') or '')
        return original_clear(section, persist)

    monkeypatch.setattr(plex_account, '_clear_server_selection', gated_clear)

    # The account lock is wrapped so the save's wait for it is visible here:
    # the first entry is the verification itself, and a second one is the save
    # waiting on the lock the verification holds.
    real_lock = plex_account.ACCOUNT_LOCK
    first_entry = []

    class _ObservedLock:
        def __enter__(self):
            if first_entry:
                save_contended.set()
            else:
                first_entry.append(True)
            return real_lock.__enter__()

        def __exit__(self, *exception):
            return real_lock.__exit__(*exception)

    monkeypatch.setattr(plex_account, 'ACCOUNT_LOCK', _ObservedLock())

    verified = []
    older = threading.Thread(target=lambda: verified.append(
        verified_server_addresses(app_settings, persist=lambda: None)))
    older.start()
    assert asked.wait(10), 'the older verification asked plex.tv first'
    assert in_write_phase.wait(10), 'the older verification reached its write phase'

    # While the older verification is held past its check, the save changes
    # the account and stores the newer selection.
    app_config.save_settings([('settings-plex-token', ['new-token']),
                              ('settings-plex-server_url', [LISTED])])
    save_finished.set()
    older.join(30)
    assert not older.is_alive()
    assert erased == [UNLISTED], \
        'the older verification wiped its own selection, not the newer save\'s'
    assert app_settings.plex.token == 'new-token', 'the newer credential was saved'
    assert app_settings.plex.server_url == LISTED, \
        'the selection the newer save stored survived the older verification'


def test_a_settings_save_cannot_store_an_address_verified_for_a_replaced_account(
        schema_session, monkeypatch):
    """The plex.tv round-trip runs before the write locks, because no lock may
    be held across it, so the guard carries what it verified into the locks
    and confirms it there against the account as it is now stored: a second
    save replacing the stored token while the first save's listing is in
    flight makes the listing stale, and the first save is refused and asked
    to try again rather than storing an address the newer account never had
    verified. The unfixed order lands the move beside the newer token."""
    from app import config as app_config, database as app_database
    from app.config import settings as app_settings
    from media_servers import plex_account
    monkeypatch.setattr(app_database, 'database', schema_session)
    for field, value in (('auth_method', 'oauth'), ('token', 'old-token'),
                         ('server_machine_id', PLEX_MACHINE), ('server_url', '')):
        monkeypatch.setattr(app_settings.plex, field, value)

    # plex.tv lists the moved address for the account the first save verified
    # with, so the listing itself succeeds and only its capture can go stale.
    plex_tv(monkeypatch, _PlexTV(listing={'old-token': (LISTED,)}, machine=PLEX_MACHINE))

    # The listing is wrapped so a second save lands inside it: config.py
    # imports it at call time, so the patch is the one the first save asks,
    # and the second save replaces the stored token before the listing
    # answers, exactly inside the first save's check-to-write window. The
    # second save carries no address, so it asks plex.tv for nothing and the
    # wrapper never recurses.
    original_listed = plex_account.remembered_listed

    def listed_with_a_newer_save(method, credential, machine_id):
        app_config.save_settings([('settings-plex-token', ['new-token'])])
        return original_listed(method, credential, machine_id)

    monkeypatch.setattr(plex_account, 'remembered_listed', listed_with_a_newer_save)

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([('settings-plex-server_url', [LISTED])])
    assert 'The Plex account changed while the server address was being verified' \
        in str(refusal.value)
    assert 'Try the save again' in str(refusal.value)
    assert app_settings.plex.server_url == '', 'the stale verification stored nothing'
    assert app_settings.plex.token == 'new-token', 'the newer save kept its write'


def test_an_address_only_save_racing_the_verification_keeps_the_newer_address(
        plex_account_api, monkeypatch):
    """The account lock covers method and credential changes, but an
    address-only save changes neither, so the verification rechecks the
    selected URL and its connections before it writes or returns: an address
    a save landed while plex.tv answered survives the older verification's
    write rather than being rewritten from the earlier snapshot."""
    import requests

    from media_servers import plex_account
    from media_servers.plex_account import verified_server_addresses
    client, config = plex_account_api
    newer = 'https://newer.example:32400'
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    config.plex.server_url = LISTED
    config.plex.server_connections = [LISTED]

    def plex_tv(url, **_kwargs):
        # A save lands while the ask is in flight, moving the selection to
        # another listed address without touching the credential or the
        # server it was asked about.
        config.plex.server_url = newer
        config.plex.server_connections = [newer]
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: [{'name': 'Attic', 'clientIdentifier': PLEX_MACHINE,
                           'provides': 'server', 'owned': True,
                           'connections': [{'uri': LISTED}, {'uri': newer}]}])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    addresses, status, credential = verified_server_addresses(config, persist=lambda: None)

    assert (addresses, status, credential) == ([newer], 'ok', 'account-token')
    assert config.plex.server_url == newer, \
        'the newer address survived the older verification'
    assert config.plex.server_connections == [newer], \
        'the newer connections survived the older verification'


def test_an_address_only_save_landing_after_the_check_waits_for_the_older_clear(
        schema_session, monkeypatch):
    """An address-only save is serialized with the verification's write phase:
    held past its address check, at the clear its older answer is about to
    make, the verification lets the save write only once its own write is
    done, so the clear wipes the selection it was asked about and never the
    newer address the save landed in under it."""
    import threading

    from app import config as app_config, database as app_database
    from app.config import settings as app_settings
    from media_servers import plex_account
    from media_servers.plex_account import verified_server_addresses
    monkeypatch.setattr(app_database, 'database', schema_session)
    for field, value in (('auth_method', 'oauth'), ('token', 'account-token'),
                         ('server_machine_id', PLEX_MACHINE), ('server_url', UNLISTED)):
        monkeypatch.setattr(app_settings.plex, field, value)
    in_write_phase = threading.Event()
    save_finished = threading.Event()
    save_contended = threading.Event()

    # plex.tv lists only the newer address for this account's server, so the
    # verification of the stored, unlisted one ends at its clear, and a save
    # that moves the selection to the listed address is the interleave.
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}, machine=PLEX_MACHINE))

    # The older verification is held inside its write phase, past its address
    # check and at the clear itself. It resumes once the save has either
    # finished writing, the unfixed order, or asked for the account lock the
    # verification still holds, the fixed one. What its clear wipes is
    # recorded: the older selection at the fixed head, the newer address the
    # save landed at the unfixed head.
    erased = []
    original_clear = plex_account._clear_server_selection

    def gated_clear(section, persist):
        in_write_phase.set()
        deadline = time.monotonic() + 10
        while not (save_finished.is_set() or save_contended.is_set()):
            assert time.monotonic() < deadline, \
                'the save neither finished nor waited for the account lock'
            time.sleep(0.01)
        erased.append(getattr(section, 'server_url', '') or '')
        return original_clear(section, persist)

    monkeypatch.setattr(plex_account, '_clear_server_selection', gated_clear)

    # The account lock is wrapped so the save's wait for it is visible here:
    # the first entry is the verification itself, and a second one is the save
    # waiting on the lock the verification holds.
    real_lock = plex_account.ACCOUNT_LOCK
    first_entry = []

    class _ObservedLock:
        def __enter__(self):
            if first_entry:
                save_contended.set()
            else:
                first_entry.append(True)
            return real_lock.__enter__()

        def __exit__(self, *exception):
            return real_lock.__exit__(*exception)

    monkeypatch.setattr(plex_account, 'ACCOUNT_LOCK', _ObservedLock())

    older = threading.Thread(target=lambda: verified_server_addresses(
        app_settings, persist=lambda: None))
    older.start()
    assert in_write_phase.wait(10), 'the older verification reached its clear'

    # The save lands while the older verification is held at its clear, past
    # the address check, and moves the selection without touching the
    # credential or the server it was asked about.
    app_config.save_settings([('settings-plex-server_url', [LISTED])])
    save_finished.set()
    older.join(30)
    assert not older.is_alive()
    assert erased == [UNLISTED], \
        'the older verification wiped its own selection, not the newer save\'s'
    assert app_settings.plex.server_url == LISTED, \
        'the newer address survived the older verification'


def test_the_picker_does_not_store_a_selection_the_account_changed_under(
        plex_account_api, monkeypatch, schema_session):
    """The picker verifies with the account it read, and a sign-in landing
    while plex.tv answers moves the pick to the newer account rather than
    storing an older account's choice beside a newer credential."""
    import requests

    from media_servers import plex_account
    from media_servers.plex_account import sync_plex_instance
    client, config = plex_account_api
    older = 'https://older.example:32400'
    config.plex.auth_method = 'oauth'
    config.plex.token = 'old-token'
    config.plex.server_machine_id = 'machine-old'
    config.plex.server_url = older
    sync_plex_instance(schema_session, config)
    listings = {
        'old-token': [{'name': 'Old', 'clientIdentifier': 'machine-old',
                       'provides': 'server', 'owned': True,
                       'connections': [{'uri': older}]}],
        'new-token': [{'name': 'New', 'clientIdentifier': 'machine-new',
                       'provides': 'server', 'owned': True,
                       'connections': [{'uri': LISTED}]}],
    }

    def plex_tv(url, **_kwargs):
        token = _kwargs['headers'].get('X-Plex-Token')
        if token == 'old-token':
            # A newer sign-in lands while the picker's ask is in flight.
            config.plex.token = 'new-token'
            config.plex.server_machine_id = 'machine-new'
            config.plex.server_url = LISTED
            config.plex.server_connections = [LISTED]
        return SimpleNamespace(
            status_code=200, headers={'content-type': 'application/json'},
            json=lambda: listings[token])

    monkeypatch.setattr(requests, 'get', plex_tv)
    plex_account.forget_plex_tv_answer()

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': 'machine-old', 'name': 'Old', 'uri': older})

    assert response.status_code == 400
    assert response.json['error_code'] == 'server_not_listed', response.json
    assert config.plex.server_url == LISTED, 'the newer selection was not erased'
    assert config.plex.server_machine_id == 'machine-new'
    assert config.plex.server_connections == [LISTED]


def test_a_save_switching_to_the_sign_in_method_verifies_with_the_sign_in_token(
        plex_account_api, monkeypatch):
    """A save that switches the method checks a submitted address with the
    credential the method it leaves in place reads, not the one the stored
    method does: switching to the sign-in method verifies against the stored
    token, so an address plex.tv lists for it is stored instead of being
    checked against the API key and dropped at first use."""
    from app import config as app_config
    client, config = plex_account_api
    config.plex.auth_method = 'apikey'
    config.plex.apikey = 'stored-plex-key'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))
    saved = []
    monkeypatch.setattr(app_config, '_save_settings_with_native',
                        lambda items, **_kwargs: saved.append(items))

    app_config.save_settings([('settings-plex-auth_method', ['oauth']),
                              ('settings-plex-server_url', [LISTED])])
    assert saved, 'the save was not refused'
    written = {key: list(values) for key, values in saved[0]}
    assert written['settings-plex-server_url'] == [LISTED], \
        'the address was stored as the URI plex.tv listed'


def test_a_selection_url_no_parser_reads_plainly_is_refused_not_raised(
        plex_account_api, monkeypatch):
    """A malformed selection URL raises out of the URL parser itself, and the
    picker answers its refusal rather than failing on an unhandled error."""
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'account-token'
    config.plex.server_machine_id = PLEX_MACHINE
    plex_tv(monkeypatch, _PlexTV(listing={'account-token': (LISTED,)}))

    response = client.post('/api/plex/select-server', headers=HEADERS, json={
        'machineIdentifier': PLEX_MACHINE, 'name': 'Attic', 'uri': 'http://[::1'})

    assert response.status_code == 400, 'the malformed URL was refused, not raised'
    assert response.json['error_code'] == 'server_not_listed'
    assert config.plex.server_url == '' and config.plex.server_connections == []

