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


MOVIE_AGENT, SERIES_AGENT, LEGACY_AGENT = 'tv.plex.agents.movie', 'tv.plex.agents.series', 'com.plexapp.agents.imdb'


def plex_guid(external, agent=MOVIE_AGENT):
    """The plex:// guid an agent gives an external id, in these fakes."""
    return f'plex://{agent}/{external}'


class _Sample:
    """The item plexapi's ``getGuid`` borrows to have the agent match an id."""

    def __init__(self, section):
        self.section = section

    def matches(self, agent, title):
        assert '://' not in title, 'plexapi hands the agent an id as imdb-tt1'
        external = title.replace('-', '://', 1)
        self.section.log.append(('match', agent, external))
        # The online agent knows every id but these.
        return [] if 'unknown' in external else [SimpleNamespace(guid=plex_guid(external, agent))]


class PlexSection:
    """A Plex section as plexapi's own ``getGuid`` asks it.

    A sample item has the section's agent match the external id to a plex://
    guid, and the section is searched for that guid. The real ``getGuid`` runs
    over it too, so the lookup the refresh and the link share can be held to
    plexapi's own answer.
    """

    getGuid = __import__('plexapi.library', fromlist=['LibrarySection']).LibrarySection.getGuid
    language = 'en-US'

    def __init__(self, name='Movies', by_guid=None, refusal=None, agent=MOVIE_AGENT, log=None, empty=False):
        self.name, self.by_guid, self.refusal, self.agent = name, by_guid or {}, refusal, agent
        self.log = [] if log is None else log
        self.empty = empty

    def search(self, maxresults=None, guid=None, **filters):
        if self.refusal is not None:
            raise self.refusal
        if guid is None:
            assert (maxresults, filters) == (1, {}), 'an identifier lookup never searches by title'
            return [] if self.empty else [_Sample(self)]
        self.log.append(('search', self.name, guid))
        return [item for external, item in self.by_guid.items() if plex_guid(external, self.agent) == guid]

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
    log = []
    films = PlexSection('Films', {'tmdb://19': by_tmdb}, log=log)
    movies = PlexSection('Movies', {'imdb://tt1': by_imdb}, log=log)
    with _plex(monkeypatch, {'Films': films, 'Movies': movies}, options=options) as client:
        # The refresh's order: the IMDB guid across the sections first.
        assert client.locate('movie', MediaMetadata(imdb_id='tt1', tmdb_id='19')) is by_imdb
    assert log == [('match', MOVIE_AGENT, 'imdb://tt1'), ('search', 'Films', plex_guid('imdb://tt1')),
                   ('search', 'Movies', plex_guid('imdb://tt1'))]
    log.clear()
    films, movies = PlexSection('Films', {'tmdb://19': by_tmdb}, log=log), PlexSection('Movies', log=log)
    with _plex(monkeypatch, {'Films': films, 'Movies': movies}, options=options) as client:
        # Then the TMDB guid, which the refresh never asks and a link still can.
        assert client.locate('movie', MediaMetadata(imdb_id='tt1', tmdb_id='19')) is by_tmdb
    assert log == [('match', MOVIE_AGENT, 'imdb://tt1'), ('search', 'Films', plex_guid('imdb://tt1')),
                   ('search', 'Movies', plex_guid('imdb://tt1')),
                   ('match', MOVIE_AGENT, 'tmdb://19'), ('search', 'Films', plex_guid('tmdb://19'))]


def test_plex_locate_asks_each_agent_once_per_guid_whatever_the_sections(monkeypatch):
    # getGuid has the agent match the id on every call, which can leave the
    # LAN. A miss, the usual answer from a server without the item, asked it
    # once per section per guid; the agent's answer is the same in every
    # section it serves, so it is asked once per guid.
    from media_servers.resolution import MediaMetadata
    log = []
    sections = {'Films': PlexSection('Films', log=log), 'Movies': PlexSection('Movies', log=log),
                'Old': PlexSection('Old', agent=LEGACY_AGENT, log=log)}
    options = {'movie_libraries': ['Films', 'Movies', 'Old']}
    with _plex(monkeypatch, sections, options=options) as client:
        assert client.locate('movie', MediaMetadata(imdb_id='tt1', tmdb_id='19')) is None
    assert [entry for entry in log if entry[0] == 'match'] == [
        ('match', MOVIE_AGENT, 'imdb://tt1'), ('match', LEGACY_AGENT, 'imdb://tt1'),
        ('match', MOVIE_AGENT, 'tmdb://19'), ('match', LEGACY_AGENT, 'tmdb://19')]
    # Every section is still searched for every guid, in the refresh's order.
    assert [entry[1:] for entry in log if entry[0] == 'search'] == [
        ('Films', plex_guid('imdb://tt1')), ('Movies', plex_guid('imdb://tt1')),
        ('Old', plex_guid('imdb://tt1', LEGACY_AGENT)),
        ('Films', plex_guid('tmdb://19')), ('Movies', plex_guid('tmdb://19')),
        ('Old', plex_guid('tmdb://19', LEGACY_AGENT))]


def test_plex_refresh_asks_each_agent_once_whatever_the_sections(monkeypatch):
    # The refresh after a subtitle walks every configured section for the
    # IMDB guid. A miss asked the agent in each of them; it is asked once
    # per agent now, and every section is still searched in order.
    from media_servers.resolution import MediaMetadata
    log = []
    sections = {'Films': PlexSection('Films', log=log), 'Movies': PlexSection('Movies', log=log),
                'Old': PlexSection('Old', agent=LEGACY_AGENT, log=log)}
    options = {'movie_libraries': ['Films', 'Movies', 'Old']}
    with _plex(monkeypatch, sections, options=options) as client:
        assert client.refresh_by_provider_id('movie', MediaMetadata(imdb_id='tt1', tmdb_id='19')) is None
    assert [entry for entry in log if entry[0] == 'match'] == [
        ('match', MOVIE_AGENT, 'imdb://tt1'), ('match', LEGACY_AGENT, 'imdb://tt1')]
    assert [entry[1:] for entry in log if entry[0] == 'search'] == [
        ('Films', plex_guid('imdb://tt1')), ('Movies', plex_guid('imdb://tt1')),
        ('Old', plex_guid('imdb://tt1', LEGACY_AGENT))]


@pytest.mark.parametrize('held, guid, empty', [({'imdb://tt1'}, 'imdb://tt1', False),
                                               ({'imdb://tt1'}, 'imdb://tt2', False),
                                               (set(), 'imdb://tt1', True),
                                               ({'imdb://unknown1'}, 'imdb://unknown1', False)],
                         ids=['found', 'missing', 'empty section', 'unknown to the agent'])
def test_one_match_per_agent_finds_what_plexapi_getguid_finds(held, guid, empty):
    # Neither the refresh nor the link calls getGuid, so the lookup they
    # share has to answer exactly as plexapi's own does.
    from plex.refresh import _by_guid
    from plexapi.exceptions import NotFound
    movie = SimpleNamespace(name='Metropolis')
    section = PlexSection('Movies', {external: movie for external in held}, empty=empty)

    def answer(lookup):
        try:
            return lookup()
        except NotFound:
            return NotFound
    assert answer(lambda: _by_guid(section, guid, {})) is answer(lambda: section.getGuid(guid))


def test_plex_locate_passes_an_empty_section_and_an_id_its_agent_does_not_know(monkeypatch):
    from media_servers.resolution import MediaMetadata
    movie, log = SimpleNamespace(name='Metropolis'), []
    sections = {'Empty': PlexSection('Empty', empty=True, log=log),
                'Movies': PlexSection('Movies', {'imdb://tt1': movie}, log=log)}
    with _plex(monkeypatch, sections, options={'movie_libraries': ['Empty', 'Movies']}) as client:
        # An empty section has no item to match with, so the next one asks.
        assert client.locate('movie', MediaMetadata(imdb_id='tt1')) is movie
        assert log == [('match', MOVIE_AGENT, 'imdb://tt1'), ('search', 'Movies', plex_guid('imdb://tt1'))]
        log.clear()
        # An id the agent does not know is in no section, and none is searched.
        assert client.locate('movie', MediaMetadata(imdb_id='unknown1')) is None
        assert log == [('match', MOVIE_AGENT, 'imdb://unknown1')]


def test_plex_locate_finds_the_item_the_refresh_refreshes(monkeypatch):
    from media_servers.resolution import MediaMetadata
    refreshed = []
    movie = SimpleNamespace(refresh=lambda: refreshed.append('Metropolis'))
    with _plex(monkeypatch, {'Movies': PlexSection('Movies', {'imdb://tt1': movie})}) as client:
        metadata = MediaMetadata(imdb_id='tt1', tmdb_id='19')
        # The refresh and the link go through one lookup and find one item.
        assert client.refresh_by_provider_id('movie', metadata) == {'status': 'requested'}
        assert client.locate('movie', metadata) is movie
    assert refreshed == ['Metropolis']


def test_plex_locate_misses_without_a_scan(monkeypatch):
    from media_servers.resolution import MediaMetadata
    section = PlexSection('Movies')
    with _plex(monkeypatch, {'Movies': section, 'Shows': PlexSection('Shows', agent=SERIES_AGENT)}) as client:
        assert client.locate('movie', MediaMetadata(imdb_id='tt1')) is None
    assert section.log == [('match', MOVIE_AGENT, 'imdb://tt1'), ('search', 'Movies', plex_guid('imdb://tt1'))]


def test_plex_locate_descends_to_the_episode_and_a_missing_one_is_nothing(monkeypatch):
    from media_servers.resolution import MediaMetadata
    from plexapi.exceptions import NotFound
    episode = SimpleNamespace(name='S01E02')

    def pick(season, episode_number):
        if (season, episode_number) != (1, 2):
            raise NotFound('no such episode')
        return episode
    show = SimpleNamespace(episode=lambda season, episode: pick(season, episode))
    shows = PlexSection('Shows', {'tvdb://78874': show}, agent=SERIES_AGENT)
    with _plex(monkeypatch, {'Shows': shows}) as client:
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
    with _plex(monkeypatch, {'Movies': PlexSection(refusal=error_type(refusal))}) as client:
        with pytest.raises(MediaServerError) as error:
            client.locate('movie', MediaMetadata(imdb_id='tt1'))
    # Exactly what the refresh reports for the same refusal.
    assert error.value.code == code


# ------------------------------------------------------------------- sports


def test_jellyfin_finds_a_recording_by_file_name_in_its_sports_libraries(jellyfin_server):
    from jellyfin.refresh import JellyfinRefreshClient
    from media_servers.resolution import MediaMetadata
    from test_media_server_convergence import SPORTS_LIBRARY
    jellyfin_server.items[SPORTS_LIBRARY] = [{'Id': 'a' * 32, 'Path': '/jf/NHL/Game 11.mkv'},
                                             {'Id': 'b' * 32, 'Path': '/jf/NHL/Game 12.mkv'}]
    options = {'sports_library_ids': [SPORTS_LIBRARY]}
    with JellyfinRefreshClient(snap('jellyfin', 'J', url=jellyfin_server.url, options=options)) as client:
        assert client.locate('sports', MediaMetadata(title='Game 12'), '/media/NHL/Game 12.mkv') == 'b' * 32
        jellyfin_server.items[SPORTS_LIBRARY].append({'Id': 'c' * 32, 'Path': '/jf/Other/Game 12.mkv'})
        assert client.locate('sports', MediaMetadata(title='Game 12'), '/media/NHL/Game 12.mkv') is None
    assert not [call for call in jellyfin_server.calls if call[0] == 'POST']


def test_plex_refuses_a_recording_lookup_as_silo_does(monkeypatch):
    # Plex files a show's date-named recordings of one day as one episode, so
    # no file name pins an item there and a link never asks; a lookup that is
    # asked anyway is a caller's mistake, and the server hears nothing of it.
    from media_servers.http import MediaServerError
    from media_servers.resolution import MediaMetadata
    from plex import operations
    from plex.refresh import PlexRefreshClient
    monkeypatch.setattr(operations, 'plex_server_for', lambda *_args: pytest.fail('no request for a recording'))
    client = PlexRefreshClient(snap('plex', 'P', options={'sports_libraries': ['Sports']}))
    with pytest.raises(MediaServerError) as refused:
        client.locate('sports', MediaMetadata(title='Game 12'))
    assert refused.value.code == 'internal_error'
