# coding=utf-8
"""Jellyfin and Plex answering which item a library row is, with nothing refreshed."""

import json
from types import SimpleNamespace

import pytest

from test_media_server_convergence import jellyfin_server as jellyfin_server
from test_media_server_convergence import MOVIE_LIBRARY, SERIES_ITEM, SERIES_LIBRARY


def snap(kind, name, *, url='http://server.example', mappings=(), options=None, **overrides):
    from media_servers.instances import ConnectionSnapshot
    if options is None:
        options = ({'movie_library_ids': [MOVIE_LIBRARY], 'series_library_ids': [SERIES_LIBRARY]}
                   if kind == 'jellyfin' else
                   {'movie_libraries': ['Movies'], 'series_libraries': ['Shows']} if kind == 'plex' else {})
    values = dict(id=f'{kind}-{name}', kind=kind, name=name, instance_enabled=True, master_enabled=True,
                  url=url, apikey='synthetic-key', verify_ssl=False,
                  path_mappings=tuple(tuple(sorted(row.items())) for row in mappings),
                  options_json=json.dumps(options))
    return ConnectionSnapshot(**(values | overrides))


def test_jellyfin_locate_climbs_identifiers_then_title_and_stops_at_a_tie(jellyfin_server):
    from jellyfin.refresh import JellyfinRefreshClient
    from media_servers.resolution import MediaMetadata
    jellyfin_server.items[MOVIE_LIBRARY] = [{'Id': 'a' * 32, 'Name': 'Metropolis', 'ProviderIds': {'Tmdb': '19'}},
                                            {'Id': 'b' * 32, 'Name': 'Nosferatu', 'ProviderIds': {}}]
    metadata = MediaMetadata(imdb_id='tt1', tmdb_id='19', title='Metropolis', year=1927)
    with JellyfinRefreshClient(snap('jellyfin', 'J', url=jellyfin_server.url)) as client:
        # No item carries the IMDB id, so the TMDB id answers; with neither, the title.
        assert client.locate('movie', metadata) == 'a' * 32
        assert client.locate('movie', MediaMetadata(imdb_id='tt9', title='Nosferatu', year=1922)) == 'b' * 32
        # Two items claiming the IMDB id stop the refresh's walk with item_ambiguous,
        # so the lookup stops there too rather than settling it with a lower rung.
        jellyfin_server.items[MOVIE_LIBRARY] += [{'Id': 'c' * 32, 'Name': 'Other', 'ProviderIds': {'Imdb': 'tt1'}},
                                                 {'Id': 'd' * 32, 'Name': 'Another', 'ProviderIds': {'Imdb': 'tt1'}}]
        assert client.locate('movie', metadata) is None
    assert not [call for call in jellyfin_server.calls if call[0] == 'POST']


def test_jellyfin_locate_descends_to_the_episode_the_numbers_name(jellyfin_server):
    from jellyfin.refresh import JellyfinRefreshClient
    from media_servers.resolution import MediaMetadata
    jellyfin_server.items[SERIES_LIBRARY] = [{'Id': SERIES_ITEM, 'Name': 'Firefly', 'ProviderIds': {'Tvdb': '78874'}}]
    jellyfin_server.episodes = [{'Id': 'e' * 32, 'IndexNumber': 2}, {'Id': 'f' * 32, 'IndexNumber': 3}]
    with JellyfinRefreshClient(snap('jellyfin', 'J', url=jellyfin_server.url)) as client:
        assert client.locate('episode', MediaMetadata(tvdb_id=78874, season=1, episode=2)) == 'e' * 32
        assert client.locate('episode', MediaMetadata(tvdb_id=78874, season=1, episode=9)) is None
    assert not [call for call in jellyfin_server.calls if call[0] == 'POST']


class _Section:
    """A Plex section that answers ``getGuid``, the one lookup the refresh uses."""

    def __init__(self, by_guid=None, refusal=None):
        self.by_guid, self.refusal, self.calls = by_guid or {}, refusal, []

    def getGuid(self, guid):
        from plexapi.exceptions import NotFound
        self.calls.append(guid)
        if self.refusal is not None:
            raise self.refusal
        if guid not in self.by_guid:
            raise NotFound(guid)
        return self.by_guid[guid]

    def search(self, *_args, **_filters):
        pytest.fail('an identifier lookup goes through getGuid, as the refresh does')

    def update(self):
        pytest.fail('a lookup must never scan the section')


def _plex(monkeypatch, sections, **overrides):
    from plex import operations, refresh
    from plexapi.exceptions import NotFound

    def section(name):
        if name not in sections:
            raise NotFound(name)
        return sections[name]
    monkeypatch.setattr(operations, 'plex_server_for', lambda *_args: SimpleNamespace(
        library=SimpleNamespace(section=section)))
    return refresh.PlexRefreshClient(snap('plex', 'P', **overrides))


def test_plex_locate_asks_the_imdb_guid_in_every_section_before_any_other(monkeypatch):
    from media_servers.resolution import MediaMetadata
    options = {'movie_libraries': ['Films', 'Movies']}
    by_imdb, by_tmdb = SimpleNamespace(name='by imdb'), SimpleNamespace(name='by tmdb')
    films, movies = _Section(by_guid={'tmdb://19': by_tmdb}), _Section(by_guid={'imdb://tt1': by_imdb})
    with _plex(monkeypatch, {'Films': films, 'Movies': movies}, options=options) as client:
        # The refresh's order: the IMDB guid across the sections first.
        assert client.locate('movie', MediaMetadata(imdb_id='tt1', tmdb_id='19')) is by_imdb
    assert (films.calls, movies.calls) == (['imdb://tt1'], ['imdb://tt1'])
    films, movies = _Section(by_guid={'tmdb://19': by_tmdb}), _Section()
    with _plex(monkeypatch, {'Films': films, 'Movies': movies}, options=options) as client:
        # Then the TMDB guid, which the refresh never asks and a link still can.
        assert client.locate('movie', MediaMetadata(imdb_id='tt1', tmdb_id='19')) is by_tmdb
    assert (films.calls, movies.calls) == (['imdb://tt1', 'tmdb://19'], ['imdb://tt1'])


def test_plex_locate_finds_the_item_the_refresh_refreshes(monkeypatch):
    from media_servers.resolution import MediaMetadata
    refreshed = []
    movie = SimpleNamespace(refresh=lambda: refreshed.append('Metropolis'))
    with _plex(monkeypatch, {'Movies': _Section(by_guid={'imdb://tt1': movie})}) as client:
        metadata = MediaMetadata(imdb_id='tt1', tmdb_id='19')
        assert client.refresh_by_provider_id('movie', metadata) == {'status': 'requested'}
        assert client.locate('movie', metadata) is movie
    assert refreshed == ['Metropolis']


def test_plex_locate_misses_without_a_scan(monkeypatch):
    from media_servers.resolution import MediaMetadata
    section = _Section()
    with _plex(monkeypatch, {'Movies': section, 'Shows': _Section()}) as client:
        assert client.locate('movie', MediaMetadata(imdb_id='tt1')) is None
    assert section.calls == ['imdb://tt1']


def test_plex_locate_descends_to_the_episode_and_a_missing_one_is_nothing(monkeypatch):
    from media_servers.resolution import MediaMetadata
    from plexapi.exceptions import NotFound
    episode = SimpleNamespace(name='S01E02')

    def pick(season, episode_number):
        if (season, episode_number) != (1, 2):
            raise NotFound('no such episode')
        return episode
    show = SimpleNamespace(episode=lambda season, episode: pick(season, episode))
    with _plex(monkeypatch, {'Shows': _Section(by_guid={'tvdb://78874': show})}) as client:
        assert client.locate('episode', MediaMetadata(tvdb_id=78874, season=1, episode=2)) is episode
        assert client.locate('episode', MediaMetadata(tvdb_id=78874, season=1, episode=3)) is None
        assert client.locate('series', MediaMetadata(tvdb_id=78874)) is show


@pytest.mark.parametrize('refusal, code', [('timeout', 'timeout'), ('bad request', 'request_rejected')])
def test_a_plex_server_that_stops_answering_is_a_refusal_not_a_miss(monkeypatch, refusal, code):
    import requests
    from media_servers.http import MediaServerError
    from media_servers.resolution import MediaMetadata
    from plexapi.exceptions import BadRequest
    error_type = {'timeout': requests.exceptions.ConnectTimeout, 'bad request': BadRequest}[refusal]
    with _plex(monkeypatch, {'Movies': _Section(refusal=error_type(refusal))}) as client:
        with pytest.raises(MediaServerError) as error:
            client.locate('movie', MediaMetadata(imdb_id='tt1'))
    # Exactly what the refresh reports for the same refusal.
    assert error.value.code == code
