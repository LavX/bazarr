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
    """Only what `plex_refresh_item` ever asked a section for."""

    def __init__(self, title, guids, calls, refusal=None):
        self.title, self.type, self._guids, self._calls = title, 'movie', guids, calls
        self._refusal = refusal

    # plexapi's own spelling, which the real sections answer to.
    def getGuid(self, guid):
        from plexapi.exceptions import NotFound
        if self._refusal is not None:
            raise self._refusal
        if guid not in self._guids:
            raise NotFound(guid)
        return self._guids[guid]

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


def _plex_client(monkeypatch, sections, calls, **overrides):
    from types import SimpleNamespace
    from plex import operations, refresh
    monkeypatch.setattr(operations, 'plex_server_for', lambda *_args: SimpleNamespace(
        library=SimpleNamespace(section=lambda name: sections[name])))
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
                   apikey='', token='', server_url='', verify_ssl=False,
                   path_mappings=[], update_movie_library=True, update_series_library=True,
                   movie_library=[], series_library=[], sports_library=[], instance_id='')
    section.update(overrides)
    # A durable master key, so encrypting the credential never tries to mint one
    # and write it back through the real config file.
    general = SimpleNamespace(use_emby=False, use_silo=False, use_jellyfin=False, use_plex=True,
                              secrets_encryption_key='synthetic-durable-key')
    return SimpleNamespace(general=general, plex=SimpleNamespace(**section),
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


# --- The real OAuth handlers, against a faked Plex ---------------------------

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
    monkeypatch.setattr(oauth.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {'authToken': 'fresh-plex-token'}))

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
        plex_account_api, schema_session):
    from media_servers.plex_account import sync_plex_instance
    from media_servers.repository import MediaServerInstanceRepository
    client, config = plex_account_api
    config.plex.auth_method = 'oauth'
    config.plex.token = 'token'
    config.plex.server_url = 'https://first.example:32400'
    row = sync_plex_instance(schema_session, config)
    repo = MediaServerInstanceRepository(schema_session)
    repo.update(row.id, enabled=False)

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
    monkeypatch.setattr(oauth, 'test_plex_connection', lambda uri, token: (
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
