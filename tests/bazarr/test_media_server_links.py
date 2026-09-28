# coding=utf-8
"""Which destinations a library row is offered on, and what each one answers."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_media_server_convergence import jellyfin_server as jellyfin_server
from test_media_server_convergence import SERIES_ITEM, SERIES_LIBRARY
from test_media_server_http import http_fixture as http_fixture
from test_media_server_locate import snap

MASTERS = SimpleNamespace(general=SimpleNamespace(use_emby=True, use_silo=True, use_jellyfin=True, use_plex=True))


def native(*snapshots):
    from media_servers.dispatcher import NativeConfiguration
    return NativeConfiguration(MASTERS, snapshots=snapshots)


def library_row(path='/media/Film.mkv', **metadata):
    from media_servers.resolution import LibraryRow, MediaMetadata
    values = dict(imdb_id='tt0017136', tmdb_id='19', title='Metropolis', year=1927) | metadata
    return LibraryRow(MediaMetadata(**values), path, 1)


EMBY_A = dict(mappings=[{'local_path': '/media', 'remote_path': '/e'}])
SILO_A = dict(mappings=[{'local_path': '/media', 'remote_path': '/s', 'library_id': '7'}])


@pytest.fixture(autouse=True)
def empty_cache():
    from media_servers import links
    links.links_region.invalidate(hard=True)
    links._lanes.clear()
    yield
    links.links_region.invalidate(hard=True)
    links._lanes.clear()


def test_one_destination_of_a_kind_shows_its_kind_and_two_show_their_names():
    from media_servers.links import destination_rows
    rows = destination_rows(native(snap('silo', 'Den', **SILO_A), snap('emby', 'Upstairs', **EMBY_A),
                                   snap('emby', 'Attic', **EMBY_A), snap('plex', 'Home')))
    assert [(row['instance_id'], row['display_name']) for row in rows] == [
        ('emby-Attic', 'Attic'), ('emby-Upstairs', 'Upstairs'), ('plex-Home', 'Plex'), ('silo-Den', 'Silo')]
    assert set(rows[0]) == {'instance_id', 'kind', 'display_name'}


def test_switched_off_and_broken_destinations_are_never_offered():
    from media_servers.links import destinations
    rows = destinations(native(snap('emby', 'A', **EMBY_A),
                               snap('emby', 'Off', instance_enabled=False, **EMBY_A),
                               snap('emby', 'Master off', master_enabled=False, **EMBY_A),
                               snap('emby', 'Broken', configuration_error='missing_credentials', **EMBY_A)))
    assert [(snapshot.name, shown) for snapshot, shown in rows] == [('A', 'Emby')]


def test_a_row_is_offered_only_where_a_mapping_or_a_library_can_hold_it():
    from media_servers.links import candidates
    configuration = native(
        snap('emby', 'A', **EMBY_A),
        snap('emby', 'Elsewhere', mappings=[{'local_path': '/other', 'remote_path': '/x'}]),
        snap('silo', 'No library', mappings=[{'local_path': '/media', 'remote_path': '/s'}]),
        snap('silo', 'S', **SILO_A),
        snap('jellyfin', 'J'),
        snap('plex', 'No movies', options={'series_libraries': ['Shows']}))
    targets = candidates(library_row(), 'movie', configuration)
    assert [(target.snapshot.name, target.mapped_path) for target in targets] == [
        ('A', '/e/Film.mkv'), ('J', None), ('S', '/s/Film.mkv')]
    assert [target.snapshot.name for target in candidates(library_row(), 'series', configuration)] == [
        'A', 'J', 'No movies', 'S']


def test_a_row_whose_path_could_not_be_mapped_is_offered_only_where_no_path_is_needed():
    from media_servers.links import candidates
    targets = candidates(library_row(path=None), 'movie', native(snap('emby', 'A', **EMBY_A), snap('jellyfin', 'J')))
    assert [target.snapshot.kind for target in targets] == ['jellyfin']


def test_the_url_builders_quote_what_the_server_sent():
    from media_servers.links import emby_url, jellyfin_url, plex_web_base, silo_url
    assert emby_url('http://emby.lan:8096', 'a b', '5a69') == (
        'http://emby.lan:8096/web/index.html#!/item?id=a%20b&serverId=5a69')
    assert jellyfin_url('https://jf.example/jellyfin', 'abc') == 'https://jf.example/jellyfin/web/#/details?id=abc'
    assert silo_url('http://silo.lan:8080', 'episode-tvdb-78874-1-1') == (
        'http://silo.lan:8080/item/episode-tvdb-78874-1-1')
    assert plex_web_base(snap('plex', 'P')) is None
    assert plex_web_base(snap('plex', 'P', external_url='http://plex.lan:32400')) == (
        'http://plex.lan:32400/web/index.html')


def _stub_row(monkeypatch, row=None):
    from media_servers import links
    monkeypatch.setattr(links, 'media_metadata_for', lambda media_type, local_id: None if local_id == 404 else (
        row or library_row()))


def test_an_unknown_row_is_none_and_a_destination_that_cannot_hold_it_asks_nothing(monkeypatch):
    from media_servers import links
    _stub_row(monkeypatch)
    monkeypatch.setattr(links, '_lookup', lambda *_args: pytest.fail('no request for a non-candidate'))
    configuration = native(snap('emby', 'Elsewhere', mappings=[{'local_path': '/other', 'remote_path': '/x'}]))
    assert links.resolve('emby-Elsewhere', 'movie', 404, configuration) is None
    assert links.candidate_rows('movie', 404, configuration) is None
    assert links.resolve('emby-Elsewhere', 'movie', 7, configuration) == {
        'instance_id': 'emby-Elsewhere', 'state': 'not_found', 'url': None}


def test_answers_are_cached_until_the_destination_changes_but_unreachable_is_asked_again(monkeypatch):
    from media_servers import links
    _stub_row(monkeypatch)
    calls, answers = [], iter(['http://e/1', None, RuntimeError('down'), 'http://e/2'])

    def lookup(target, media_type, row):
        calls.append(target.snapshot.revision)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(links, '_lookup', lookup)
    first = snap('emby', 'A', **EMBY_A)
    assert links.resolve(first.id, 'movie', 7, native(first))['url'] == 'http://e/1'
    assert links.resolve(first.id, 'movie', 7, native(first))['url'] == 'http://e/1'
    edited = replace(first, revision=2)
    assert links.resolve(first.id, 'movie', 7, native(edited))['state'] == 'not_found'
    third = replace(first, revision=3)
    assert links.resolve(first.id, 'movie', 7, native(third))['state'] == 'unreachable'
    assert links.resolve(first.id, 'movie', 7, native(third)) == {
        'instance_id': first.id, 'state': 'resolved', 'url': 'http://e/2'}
    assert calls == [1, 2, 3, 3]


def test_a_server_slower_than_its_budget_is_unreachable_and_does_not_hold_the_row(monkeypatch):
    from media_servers import links
    _stub_row(monkeypatch)
    release = threading.Event()
    monkeypatch.setitem(links.BUDGETS, 'plex', 0.2)
    monkeypatch.setattr(links, '_lookup', lambda *_args: release.wait(5) and 'late')
    try:
        assert links.resolve('plex-P', 'movie', 7, native(snap('plex', 'P')))['state'] == 'unreachable'
    finally:
        release.set()


def test_a_destination_that_hangs_holds_only_its_own_rows(monkeypatch):
    from media_servers import links
    _stub_row(monkeypatch)
    release, asked = threading.Event(), []
    monkeypatch.setitem(links.BUDGETS, 'plex', 0.2)

    def lookup(target, media_type, row):
        asked.append(target.snapshot.kind)
        if target.snapshot.kind == 'plex':
            release.wait(10)
            return None
        return 'http://e/1'
    monkeypatch.setattr(links, '_lookup', lookup)
    configuration = native(snap('plex', 'P'), snap('emby', 'A', **EMBY_A))
    try:
        # More Plex rows than Plex has workers, every one of them left hanging.
        states = [links.resolve('plex-P', 'movie', local_id, configuration)['state'] for local_id in range(1, 7)]
        started = time.monotonic()
        emby = links.resolve('emby-A', 'movie', 7, configuration)
        waited = time.monotonic() - started
    finally:
        release.set()
    assert states == ['unreachable'] * 6
    # Plex was asked only as often as it has workers; the rows after that were
    # answered at once, and Emby, on its own workers, was asked and answered.
    assert asked.count('plex') == links.SLOTS
    assert emby == {'instance_id': 'emby-A', 'state': 'resolved', 'url': 'http://e/1'}
    assert waited < 1


def test_a_row_queued_behind_its_own_destination_still_gets_its_whole_budget(monkeypatch):
    from media_servers import links
    _stub_row(monkeypatch)
    monkeypatch.setattr(links, 'SLOTS', 1)
    monkeypatch.setitem(links.BUDGETS, 'emby', 1.0)
    running = threading.Event()

    def lookup(target, media_type, row):
        running.set()
        time.sleep(0.6)
        return 'http://e/1'
    monkeypatch.setattr(links, '_lookup', lookup)
    configuration = native(snap('emby', 'A', **EMBY_A))
    answers = {}
    first = threading.Thread(target=lambda: answers.update(first=links.resolve('emby-A', 'movie', 1, configuration)))
    first.start()
    assert running.wait(5)
    second = links.resolve('emby-A', 'movie', 2, configuration)
    first.join(5)
    # The second row queued about 0.6 s and then ran about 0.6 s: past one
    # budget from when it was asked for, within it from when it started.
    assert answers['first']['state'] == second['state'] == 'resolved'


def test_emby_resolves_by_identifier_and_links_with_the_server_id(http_fixture, monkeypatch):
    from media_servers import links
    base, records = http_fixture([
        (200, {'Items': [{'Id': 'm1', 'Type': 'Movie', 'Path': '/e/Film.mkv'}], 'TotalRecordCount': 1}, {}),
        (200, {'ServerName': 'E', 'Version': '4.9.5.0', 'Id': 'sid'}, {}),
    ])
    _stub_row(monkeypatch)
    emby = snap('emby', 'A', url=base + '/emby', external_url='https://emby.example.com', **EMBY_A)
    assert links.resolve(emby.id, 'movie', 7, native(emby)) == {
        'instance_id': emby.id, 'state': 'resolved',
        'url': 'https://emby.example.com/web/index.html#!/item?id=m1&serverId=sid'}
    assert [record['method'] for record in records] == ['GET', 'GET']


def test_silo_confirms_the_content_id_it_builds(http_fixture, monkeypatch):
    from media_servers import links
    base, records = http_fixture([(404, b'', {}), (200, {'content_id': 'movie-imdb-tt0017136'}, {})])
    _stub_row(monkeypatch)
    silo = snap('silo', 'S', url=base, **SILO_A)
    assert links.resolve(silo.id, 'movie', 7, native(silo))['url'] == f'{base}/item/movie-imdb-tt0017136'
    assert [record['path'] for record in records] == ['/api/v1/catalog/items/movie-tmdb-19',
                                                      '/api/v1/catalog/items/movie-imdb-tt0017136']
    assert records[0]['headers']['Authorization'] == 'Bearer synthetic-key'


def test_jellyfin_resolves_a_series_in_its_series_libraries_without_a_refresh(jellyfin_server, monkeypatch):
    from media_servers import links
    jellyfin_server.items[SERIES_LIBRARY] = [{'Id': SERIES_ITEM, 'Name': 'Firefly', 'ProviderIds': {'Tvdb': '78874'}}]
    _stub_row(monkeypatch, library_row(path='/media/Firefly', imdb_id=None, tmdb_id=None, tvdb_id=78874,
                                       title='Firefly', year=2002))
    jellyfin = snap('jellyfin', 'J', url=jellyfin_server.url)
    assert links.resolve(jellyfin.id, 'series', 5, native(jellyfin))['url'] == (
        f'{jellyfin_server.url}/web/#/details?id={SERIES_ITEM}')
    assert [method for method, _path, _query in jellyfin_server.calls] == ['GET']


def test_plex_links_to_the_hosted_app_unless_a_browser_url_is_saved(monkeypatch):
    from media_servers import links
    from plex import operations

    class Item:
        def getWebURL(self, base=None):
            return f"{base or 'https://app.plex.tv/desktop/'}#!/server/m1/details?key=%2Flibrary%2Fmetadata%2F42"

    class Section:
        def getGuid(self, guid):
            from plexapi.exceptions import NotFound
            if guid != 'imdb://tt0017136':
                raise NotFound(guid)
            return Item()

    server = SimpleNamespace(library=SimpleNamespace(section=lambda name: Section()))
    monkeypatch.setattr(operations, 'plex_server_for', lambda *_args: server)
    _stub_row(monkeypatch)
    hosted = snap('plex', 'P')
    assert links.resolve(hosted.id, 'movie', 7, native(hosted))['url'].startswith(
        'https://app.plex.tv/desktop/#!/server/m1/details')
    local = replace(hosted, revision=2, external_url='http://plex.lan:32400')
    assert links.resolve(hosted.id, 'movie', 7, native(local))['url'].startswith(
        'http://plex.lan:32400/web/index.html#!/server/m1/details')


def sports_row(path='/media/NHL/Game 12.mkv'):
    from media_servers.resolution import LibraryRow, MediaMetadata
    return LibraryRow(MediaMetadata(title='Game 12', season=2026, episode=12), path, 3)


def test_a_recording_is_never_offered_on_silo_and_needs_a_sports_library_elsewhere():
    from media_servers.links import candidates
    configuration = native(snap('emby', 'A', **EMBY_A), snap('silo', 'S', **SILO_A), snap('jellyfin', 'J'),
                           snap('plex', 'P', options={'movie_libraries': ['Movies'], 'sports_libraries': ['Sports']}))
    assert [target.snapshot.kind for target in candidates(sports_row(), 'sports', configuration)] == ['emby', 'plex']
