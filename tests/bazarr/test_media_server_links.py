# coding=utf-8
"""Which destinations a library row is offered on, and what each one answers."""

import threading
import time
from collections import OrderedDict
from dataclasses import replace
from itertools import count
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from test_media_server_convergence import jellyfin_server as jellyfin_server
from test_media_server_convergence import SERIES_ITEM, SERIES_LIBRARY
from test_media_server_http import http_fixture as http_fixture
from test_media_server_locate import PlexSection, snap

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


STORED = 'http://192.0.2.10:32400'
LISTED = 'https://192-0-2-10.0123abcd.plex.direct:32400'


def plex_lookup_server(section):
    """What ``plex_guarded_server_for`` hands a lookup, stubbed: sections, and a session it closes."""
    return SimpleNamespace(library=SimpleNamespace(section=section), _session=SimpleNamespace(close=lambda: None))


def held_plex(monkeypatch, listed):
    """The signed-in account's Plex row as startup and every save publish it: held.

    Only reading the row asks plex.tv about it. ``listed`` is plex.tv's answer,
    the address it lists for the account's credential, or None for none.
    """
    from media_servers import plex_account
    from media_servers.dispatcher import NativeConfiguration
    monkeypatch.setattr(plex_account, 'account_row_uri', lambda *_args, **_kwargs: listed)
    settings = SimpleNamespace(general=MASTERS.general, plex=SimpleNamespace(token='synthetic-key'))
    return NativeConfiguration(settings, snapshots=[snap('plex', 'Home', url=STORED, sendable=False)])


def test_a_held_plex_row_is_offered_and_looked_up_at_the_address_plex_tv_listed(monkeypatch):
    # Held is waiting for plex.tv, not refused, so the menu offers the row,
    # and the lookup reads it first, which asks plex.tv and releases it.
    from media_servers import links
    from plex import operations
    configuration = held_plex(monkeypatch, LISTED)
    item = SimpleNamespace(getWebURL=lambda base=None: f"{base or 'https://app.plex.tv/desktop/'}#!/server/m1")
    connected = []

    def connect(url, token, verify_ssl, _guard, timeout=None):
        connected.append((url, token))
        return plex_lookup_server(lambda name: PlexSection(name, {'imdb://tt0017136': item}))
    monkeypatch.setattr(operations, 'plex_guarded_server_for', connect)
    _stub_row(monkeypatch)
    assert [snapshot.id for snapshot, _shown in links.destinations(configuration)] == ['plex-Home']
    assert [row['instance_id'] for row in links.candidate_rows('movie', 7, configuration)] == ['plex-Home']
    assert links.resolve('plex-Home', 'movie', 7, configuration) == {
        'instance_id': 'plex-Home', 'state': 'resolved', 'url': 'https://app.plex.tv/desktop/#!/server/m1'}
    assert connected == [(LISTED, 'synthetic-key')]


def test_a_plex_row_plex_tv_does_not_list_is_offered_but_never_sent_to(monkeypatch):
    # Reading the row asks plex.tv, which lists no address for the
    # credential, so the row stays held and the lookup connects nowhere.
    from media_servers import links
    from plex import operations
    configuration = held_plex(monkeypatch, None)
    connected = []
    monkeypatch.setattr(operations, 'plex_server_for', lambda *args: connected.append(args))
    monkeypatch.setattr(operations, 'plex_guarded_server_for', lambda *args, **_kwargs: connected.append(args))
    _stub_row(monkeypatch)
    assert [snapshot.id for snapshot, _shown in links.destinations(configuration)] == ['plex-Home']
    rows = links.candidate_rows('movie', 7, configuration)
    assert [row['instance_id'] for row in rows] == ['plex-Home']
    answer = links.resolve('plex-Home', 'movie', 7, configuration)
    assert answer == {'instance_id': 'plex-Home', 'state': 'unreachable', 'url': None}
    assert connected == []
    # Nothing the menu is handed names the address the row is stored with.
    assert '192.0.2.10' not in repr((rows, answer))


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

    def lookup(target, media_type, row, _ensure_current):
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

    def lookup(target, media_type, row, _ensure_current):
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

    def lookup(target, media_type, row, _ensure_current):
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


def test_a_row_held_while_its_lookup_waits_for_a_worker_is_never_sent_the_credential(monkeypatch):
    # A lookup can wait up to a budget for a worker, so the row it was
    # resolved with is read again when it starts, and the lookup already
    # running stops at its next request, as a refresh does.
    from media_servers import links
    from plex import operations
    _stub_row(monkeypatch)
    monkeypatch.setattr(links, 'SLOTS', 1)
    monkeypatch.setitem(links.BUDGETS, 'plex', 5)
    plex = snap('plex', 'P')
    configuration = native(plex)
    sent, searched, connected, release = [], [], threading.Event(), threading.Event()

    def connect(url, token, verify_ssl, _guard, timeout=None):
        sent.append(token)
        connected.set()
        release.wait(5)
        return plex_lookup_server(lambda name: PlexSection(name, log=searched))
    monkeypatch.setattr(operations, 'plex_guarded_server_for', connect)
    answers = {}
    first = threading.Thread(target=lambda: answers.update(first=links.resolve(plex.id, 'movie', 1, configuration)))
    first.start()
    assert connected.wait(5)
    lane, queued = links._lanes[plex.id], threading.Event()
    submit = lane.pool.submit

    def queue(fn):
        future = submit(fn)
        queued.set()
        return future
    monkeypatch.setattr(lane.pool, 'submit', queue)
    second = threading.Thread(target=lambda: answers.update(second=links.resolve(plex.id, 'movie', 2, configuration)))
    second.start()
    # Resolved while it could be sent to, and held while it waits behind the first.
    assert queued.wait(5)
    configuration.publish(replace(plex, sendable=False))
    release.set()
    first.join(5)
    second.join(5)
    assert sent == ['synthetic-key']
    assert searched == []
    assert answers == {'first': {'instance_id': plex.id, 'state': 'unreachable', 'url': None},
                       'second': {'instance_id': plex.id, 'state': 'unreachable', 'url': None}}


def test_an_emby_row_edited_after_it_was_read_is_never_sent_the_old_credential(http_fixture, monkeypatch):
    # The lookup reads the row once and runs the refreshes' guard from then
    # on, so an edit landing between the read and the first request stops it.
    from media_servers import links
    base, records = http_fixture([
        (200, {'Items': [{'Id': 'm1', 'Type': 'Movie', 'Path': '/e/Film.mkv'}], 'TotalRecordCount': 1}, {}),
    ])
    _stub_row(monkeypatch)
    emby = snap('emby', 'A', url=base, **EMBY_A)
    configuration = native(emby)
    read = configuration.read

    def edited_once_read(instance_id):
        answer = read(instance_id)
        configuration.publish(replace(emby, apikey='replaced-key'))
        return answer
    monkeypatch.setattr(configuration, 'read', edited_once_read)
    assert links.resolve(emby.id, 'movie', 7, configuration) == {
        'instance_id': emby.id, 'state': 'unreachable', 'url': None}
    assert records == []


def test_an_emby_row_switched_off_after_the_item_is_found_is_not_asked_its_server_id(http_fixture, monkeypatch):
    # The server id is a request of its own, after the lookup's, and it runs
    # the guard too.
    from emby import client as emby_client
    from media_servers import links
    base, records = http_fixture([
        (200, {'Items': [{'Id': 'm1', 'Type': 'Movie', 'Path': '/e/Film.mkv'}], 'TotalRecordCount': 1}, {}),
        (200, {'ServerName': 'E', 'Version': '4.9.5.0', 'Id': 'sid'}, {}),
    ])
    _stub_row(monkeypatch)
    emby = snap('emby', 'A', url=base, **EMBY_A)
    configuration = native(emby)
    request_json = emby_client.MediaServerHTTP.request_json

    def then_switched_off(self, *args, **kwargs):
        answer = request_json(self, *args, **kwargs)
        configuration.publish(replace(emby, instance_enabled=False))
        return answer
    monkeypatch.setattr(emby_client.MediaServerHTTP, 'request_json', then_switched_off)
    assert links.resolve(emby.id, 'movie', 7, configuration) == {
        'instance_id': emby.id, 'state': 'unreachable', 'url': None}
    assert [record['path'].partition('?')[0] for record in records] == ['/Items']


@pytest.mark.parametrize('mapping, asked, state', [
    ({'local_path': '/media', 'remote_path': '/moved'}, ['/moved/NHL/Game 12.mkv'], 'resolved'),
    ({'local_path': '/elsewhere', 'remote_path': '/e'}, [], 'not_found'),
], ids=['mapped elsewhere', 'no longer mapped'])
def test_a_mapping_edited_while_a_lookup_waited_maps_the_file_it_asks_for(http_fixture, monkeypatch,
                                                                          mapping, asked, state):
    # The reproduction: the lookup read the row again and the guard passed,
    # but Emby was still asked for the file at the path the menu had mapped,
    # with the row's current credential. The lookup maps the file again from
    # the row it read, as a refresh does.
    from media_servers import links
    base, records = http_fixture([
        (200, {'Items': [{'Id': 'r1', 'Type': 'Episode', 'Path': '/moved/NHL/Game 12.mkv'}],
               'TotalRecordCount': 1}, {}),
        (200, {'ServerName': 'E', 'Version': '4.9.5.0', 'Id': 'sid'}, {}),
    ])
    monkeypatch.setattr(links, 'media_metadata_for', lambda media_type, local_id: sports_row())
    emby = snap('emby', 'A', url=base, **EMBY_A)
    configuration = native(emby)
    read = configuration.read

    def edited_then_read(instance_id):
        configuration.publish(replace(emby, path_mappings=(tuple(sorted(mapping.items())),)))
        return read(instance_id)
    monkeypatch.setattr(configuration, 'read', edited_then_read)
    answer = links.resolve(emby.id, 'sports', 3, configuration)
    assert [parse_qs(urlsplit(record['path']).query)['Path'][0] for record in records
            if record['path'].startswith('/Items?')] == asked
    assert answer['state'] == state


class PlexWire:
    """Plex as plexapi hears it over its session, and every request it sent.

    Only what a lookup of the Metropolis row in a "Movies" section asks is
    answered. ``hold`` runs while the request to ``hold_at`` is answered,
    which is when the test holds the row.
    """

    ANSWERS = {
        '/': '<MediaContainer machineIdentifier="m1" friendlyName="P" version="1.41.0"/>',
        '/library': '<MediaContainer size="0" title1="Plex Library"/>',
        '/library/sections': ('<MediaContainer size="1"><Directory key="1" type="movie" title="Movies" '
                              'agent="tv.plex.agents.movie" language="en-US"/></MediaContainer>'),
        '/library/sections/1/all': ('<MediaContainer size="1" totalSize="1"><Video type="movie" ratingKey="42" '
                                    'key="/library/metadata/42" title="Metropolis" year="1927" '
                                    'librarySectionID="1" guid="plex://movie/5d77"/></MediaContainer>'),
        '/system/agents': ('<MediaContainer size="1"><Agent identifier="tv.plex.agents.movie" '
                           'shortIdentifier="movie" name="Plex Movie"/></MediaContainer>'),
        '/library/metadata/42/matches': ('<MediaContainer size="1"><SearchResult guid="plex://movie/5d77" '
                                         'name="Metropolis" year="1927" type="movie"/></MediaContainer>'),
    }
    FILTERS = ('<MediaContainer size="0"><Meta><Type key="/library/sections/1/all?type=1" type="movie" '
               'title="Movies" active="1"/></Meta></MediaContainer>')

    def __init__(self, hold_at=None, hold=None):
        self.sent, self.hold_at, self.hold = [], hold_at, hold
        self.timeouts, self.closed = [], False

    def get(self, url, headers=None, timeout=None, **_kwargs):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        # The sample search, the section's filter listing and the guid
        # search share one path; the guid search is the one that names an item.
        label = parts.path + ('?guid' if 'guid' in query else '?includeMeta' if 'includeMeta' in query else '')
        self.sent.append((label, (headers or {}).get('X-Plex-Token')))
        self.timeouts.append(timeout)
        if parts.path == self.hold_at:
            self.hold()
        body = self.FILTERS if 'includeMeta' in query else self.ANSWERS.get(parts.path, '<MediaContainer size="0"/>')
        return SimpleNamespace(status_code=200, text=body, url=url)

    def close(self):
        self.closed = True


def test_a_plex_row_held_while_the_connection_is_answered_is_sent_nothing_after_it(monkeypatch):
    # The reproduction: the guard ran before the connection and then not
    # until the first section, so a row held while Plex answered the
    # connection still had the library and its sections asked with the
    # token. The lookup's own client runs the guard before every request.
    from media_servers import links
    from plex import operations
    _stub_row(monkeypatch)
    plex = snap('plex', 'P')
    configuration = native(plex)
    wire = PlexWire(hold_at='/', hold=lambda: configuration.publish(replace(plex, sendable=False)))
    # The lookup sends over a session of its own.
    monkeypatch.setattr(operations, '_build_lookup_session', lambda verify: wire)
    answer = links.resolve(plex.id, 'movie', 7, configuration)
    assert wire.sent == [('/', 'synthetic-key')]
    assert answer == {'instance_id': plex.id, 'state': 'unreachable', 'url': None}


def test_a_plex_row_held_between_the_agent_match_and_the_guid_search_is_not_searched(monkeypatch):
    # One guid in one section is several requests inside plexapi with no
    # step of the lookup's own between them: a sample search, the agent
    # match, the section's filter listing, then the guid search. A row held
    # while the agent match is answered stops the lookup at the next one.
    from media_servers import links
    from plex import operations
    _stub_row(monkeypatch)
    plex = snap('plex', 'P')
    configuration = native(plex)
    wire = PlexWire(hold_at='/library/metadata/42/matches',
                    hold=lambda: configuration.publish(replace(plex, sendable=False)))
    shared = PlexWire()
    monkeypatch.setattr(operations, '_plex_cache', OrderedDict())
    monkeypatch.setattr(operations, '_build_pooled_session', lambda verify: shared)
    monkeypatch.setattr(operations, '_build_lookup_session', lambda verify: wire)
    # The pooled client a refresh or a probe left for this endpoint.
    pooled = operations.plex_server_for(plex.url, plex.apikey, plex.verify_ssl)
    shared.sent.clear()
    answer = links.resolve(plex.id, 'movie', 7, configuration)
    assert wire.sent == [(label, 'synthetic-key') for label in (
        '/', '/library', '/library/sections', '/library/sections/1/all', '/system/agents',
        '/library/metadata/42/matches')]
    assert answer == {'instance_id': plex.id, 'state': 'unreachable', 'url': None}
    # The lookup went out over a session of its own, never the pooled
    # client's, and that client, which the refreshes share, was not handed
    # the lookup's guard.
    assert shared.sent == []
    pooled.query('/identity')
    assert shared.sent == [('/identity', 'synthetic-key')]


def test_a_plex_lookup_sends_each_request_once_however_the_server_answers(http_fixture):
    # The reproduction: over the pooled session, urllib3 resent a GET Plex
    # answered 503, after sleeping its Retry-After, inside the one call the
    # guard ran before, so the resend carried the token with no guard run
    # for it. The lookup's own session sends each request once.
    from media_servers import links
    base, records = http_fixture([(503, b'', {'Retry-After': '0'}),
                                  (200, PlexWire.ANSWERS['/'].encode(), {'Content-Type': 'application/xml'})])
    target = links.Target(snap('plex', 'P', url=base), 'Plex', None)
    stopped = refusal(links._lookup, target, 'movie', library_row(), lambda: None)
    assert [(record['path'], record['headers'].get('X-Plex-Token')) for record in records] == [
        ('/', 'synthetic-key')]
    assert stopped == 'request_rejected'


def test_a_plex_lookup_never_follows_a_redirect(http_fixture):
    from media_servers import links
    elsewhere, followed = http_fixture([])
    base, records = http_fixture([(302, b'', {'Location': elsewhere + '/stolen'})])
    target = links.Target(snap('plex', 'P', url=base), 'Plex', None)
    stopped = refusal(links._lookup, target, 'movie', library_row(), lambda: None)
    assert followed == []
    assert [record['path'] for record in records] == ['/']
    assert stopped == 'connection_error'


def test_a_plex_lookups_own_session_is_closed_once_the_lookup_ends(monkeypatch):
    from media_servers import links
    from plex import operations
    wires = []

    def lookup_session(verify):
        wires.append(PlexWire())
        return wires[-1]
    monkeypatch.setattr(operations, '_build_lookup_session', lookup_session)
    target = links.Target(snap('plex', 'P'), 'Plex', None)
    # Resolved: the client closes it as the lookup lets go of the client.
    url = links._lookup(target, 'movie', library_row(), lambda: None)
    # Held while connecting: the server is never handed over, so it is
    # closed where it was built.
    stopped = refusal(links._lookup, target, 'movie', library_row(), held_after(1))
    assert url.startswith('https://app.plex.tv/desktop/#!/server/m1/details')
    assert wires[1].sent == []
    assert stopped == 'configuration_changed'
    assert [wire.closed for wire in wires] == [True, True]


def test_a_plex_lookup_waits_on_each_request_no_longer_than_its_budget(monkeypatch):
    # plexapi's own default is 30 s a request, twice the budget, so a Plex
    # that stopped answering held its worker, and its lane, long after the
    # row was answered.
    from media_servers import links
    from plex import operations
    wire = PlexWire()
    monkeypatch.setattr(operations, '_build_lookup_session', lambda verify: wire)
    links._lookup(links.Target(snap('plex', 'P'), 'Plex', None), 'movie', library_row(), lambda: None)
    assert wire.sent
    assert wire.timeouts == [links.PLEX_HTTP_TIMEOUT] * len(wire.sent)
    assert links.PLEX_HTTP_TIMEOUT[1] <= links.BUDGETS['plex']


def test_a_lookup_that_waited_on_another_ones_server_id_runs_its_own_guard_before_asking(http_fixture, monkeypatch):
    # The reproduction: the second lookup passed its guard and waited on the
    # first one's fill of the server id. The row was switched off and the
    # first fill failed, so the second filled on its own and asked
    # /System/Info with the token. The second client is built as every
    # refresh builds one, with no guard of its own, so only the fill's
    # guard stands between it and the server.
    from emby.client import EmbyClient
    from media_servers import links
    from media_servers.http import MediaServerError
    base, records = http_fixture([(200, {'ServerName': 'E', 'Version': '4.9.5.0', 'Id': 'sid'}, {})])
    emby = snap('emby', 'A', url=base, **EMBY_A)
    configuration = native(emby)

    def guard():
        configuration.ensure_current(emby.id, emby.revision)
    filling, release, waiting, answers = threading.Event(), threading.Event(), threading.Event(), {}

    class FirstFill:
        def server_id(self):
            filling.set()
            release.wait(5)
            raise MediaServerError('timeout')
    get_or_create = links.links_region.get_or_create

    def entered(*args, **kwargs):
        if threading.current_thread().name == 'second':
            waiting.set()
        return get_or_create(*args, **kwargs)
    monkeypatch.setattr(links.links_region, 'get_or_create', entered)

    def ask(name, client):
        try:
            answers[name] = links._emby_server_id(emby, client, guard)
        except MediaServerError as error:
            answers[name] = error.code

    def second(client):
        guard()
        ask('second', client)
    first = threading.Thread(target=ask, args=('first', FirstFill()), name='first')
    first.start()
    assert filling.wait(5)
    with EmbyClient(emby.url, emby.apikey, emby.verify_ssl) as client:
        waiter = threading.Thread(target=second, args=(client,), name='second')
        waiter.start()
        assert waiting.wait(5)
        configuration.publish(replace(emby, instance_enabled=False))
        release.set()
        first.join(5)
        waiter.join(5)
    assert records == []
    assert answers == {'first': 'timeout', 'second': 'configuration_changed'}


def held_after(checks):
    """A guard that passes ``checks`` times and then refuses: a row held right after it was checked."""
    passed = count()

    def guard():
        if next(passed) >= checks:
            from media_servers.http import MediaServerError
            raise MediaServerError('configuration_changed')
    return guard


def refusal(call, *args):
    """What stopped a call: an error code, an exception's name, or None when nothing did.

    The tests below assert on the requests first, so a guard that did not
    run fails on the request it let out rather than on the answer.
    """
    try:
        call(*args)
    except Exception as error:
        return getattr(error, 'code', type(error).__name__)
    return None


def test_every_request_an_emby_lookups_client_sends_runs_the_guard(http_fixture):
    # Held between the step's own guard and its request: only the client's
    # guard is left to stop it, so the lookup's client must carry it.
    from media_servers import links
    base, records = http_fixture([
        (200, {'Items': [{'Id': 'm1', 'Type': 'Movie', 'Path': '/e/Film.mkv'}], 'TotalRecordCount': 1}, {}),
    ])
    target = links.Target(snap('emby', 'A', url=base, **EMBY_A), 'Emby', '/e/Film.mkv')
    stopped = refusal(links._lookup, target, 'movie', library_row(), held_after(1))
    assert records == []
    assert stopped == 'configuration_changed'


@pytest.mark.parametrize('send', ['get', 'post'])
def test_a_jellyfin_client_runs_its_guard_before_each_get_and_post(jellyfin_server, send):
    from jellyfin.client import JellyfinClient
    client = JellyfinClient(jellyfin_server.url, 'synthetic-key', verify_ssl=False, guard=held_after(0))
    stopped = refusal(getattr(client, send), '/Items')
    assert jellyfin_server.calls == []
    assert stopped == 'configuration_changed'


@pytest.mark.parametrize('send', ['get', 'post'])
def test_a_guarded_jellyfin_client_refuses_a_redirect_before_the_next_hop(http_fixture, send):
    # The reproduction: requests sent the redirect's next hop itself, with
    # the token and without coming back through get or post, so the guard
    # ran once for both requests, and up to 30 hops were allowed.
    from jellyfin.client import JellyfinClient
    elsewhere, followed = http_fixture([])
    base, records = http_fixture([(302, b'', {'Location': elsewhere + '/stolen'})])
    client = JellyfinClient(base, 'synthetic-key', verify_ssl=False, guard=lambda: None)
    stopped = refusal(getattr(client, send), '/Items')
    assert followed == []
    assert [(record['method'], record['path']) for record in records] == [(send.upper(), '/Items')]
    assert 'Token="synthetic-key"' in records[0]['headers']['Authorization']
    assert stopped == 'TooManyRedirects'


class SessionRecord:
    """A session that only records what it was handed to send, by verb."""

    def __init__(self):
        self.sent = []

    def __getattr__(self, verb):
        return lambda url, **_kwargs: self.sent.append((verb, url))


@pytest.mark.parametrize('verb', ['post', 'put', 'delete'])
def test_a_guarded_plex_session_runs_its_guard_before_each_post_put_and_delete(verb):
    # A lookup only ever gets, so these three are held to the wrapper itself:
    # a request goes through while the guard passes and the next one, held
    # in between, never reaches the session.
    from plex.operations import _GuardedSession
    session = SessionRecord()
    guarded = _GuardedSession(session, held_after(1))
    getattr(guarded, verb)('http://plex.example/first')
    stopped = refusal(getattr(guarded, verb), 'http://plex.example/second')
    assert session.sent == [(verb, 'http://plex.example/first')]
    assert stopped == 'configuration_changed'


def test_every_request_a_jellyfin_lookups_client_sends_runs_the_guard(jellyfin_server):
    from media_servers import links
    target = links.Target(snap('jellyfin', 'J', url=jellyfin_server.url), 'Jellyfin', None)
    stopped = refusal(links._lookup, target, 'movie', library_row(), held_after(1))
    assert jellyfin_server.calls == []
    assert stopped == 'configuration_changed'


def test_every_catalog_check_a_silo_lookups_client_sends_runs_the_guard(http_fixture):
    from media_servers import links
    base, records = http_fixture([(404, b'', {}), (200, {'content_id': 'movie-imdb-tt0017136'}, {})])
    target = links.Target(snap('silo', 'S', url=base, **SILO_A), 'Silo', '/s/Film.mkv')
    stopped = refusal(links._lookup, target, 'movie', library_row(), held_after(1))
    assert records == []
    assert stopped == 'configuration_changed'


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

    server = plex_lookup_server(lambda name: PlexSection(name, {'imdb://tt0017136': Item()}))
    monkeypatch.setattr(operations, 'plex_guarded_server_for', lambda *_args, **_kwargs: server)
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


def test_every_kind_says_whether_it_can_pin_a_recording_and_only_emby_and_jellyfin_can():
    # A recording has no identifiers, so a kind is offered for sports only
    # when a file name picks out one item of its own there. Silo cannot look
    # a file up, and Plex files a show's date-named recordings of one day as
    # one episode.
    from media_servers.instances import VALID_KINDS
    from media_servers.links import PINS_RECORDINGS
    assert set(PINS_RECORDINGS) == set(VALID_KINDS)
    assert {kind for kind, pins in PINS_RECORDINGS.items() if pins} == {'emby', 'jellyfin'}


def test_a_recording_is_offered_only_where_its_kind_pins_it_and_a_library_or_mapping_holds_it():
    from media_servers.links import candidates
    configuration = native(snap('emby', 'A', **EMBY_A), snap('silo', 'S', **SILO_A), snap('jellyfin', 'J'),
                           snap('jellyfin', 'K', options={'sports_library_ids': ['sports']}),
                           snap('plex', 'P', options={'movie_libraries': ['Movies'], 'sports_libraries': ['Sports']}))
    assert [target.snapshot.id for target in candidates(sports_row(), 'sports', configuration)] == [
        'emby-A', 'jellyfin-K']


def test_plex_is_never_offered_for_a_recording_even_with_a_sports_library(monkeypatch):
    # The live check: four NFL recordings of one Sunday are one Plex episode,
    # "Episode 09-20", so every sports row read "Not found on Plex". The
    # sports event row and the sports History row both ask about the event's
    # local id as a sports row, so neither is offered Plex. A movie still is.
    from media_servers import links
    monkeypatch.setattr(links, 'media_metadata_for', lambda media_type, local_id: (
        sports_row() if media_type == 'sports' else library_row()))
    monkeypatch.setattr(links, '_lookup', lambda *_args: pytest.fail('Plex is never asked about a recording'))
    plex = snap('plex', 'P', options={'movie_libraries': ['Movies'], 'sports_libraries': ['Sports']})
    assert links.candidate_rows('sports', 11, native(plex)) == []
    assert links.resolve(plex.id, 'sports', 11, native(plex)) == {
        'instance_id': plex.id, 'state': 'not_found', 'url': None}
    assert [row['kind'] for row in links.candidate_rows('movie', 7, native(plex))] == ['plex']


def test_a_recording_whose_path_could_not_be_mapped_is_offered_nowhere():
    # Every kind that is offered a recording finds it by its file, so a
    # recording without a path has no destination to ask, rather than a
    # Jellyfin row that can only ever answer "Not found".
    from media_servers.links import candidates
    configuration = native(snap('emby', 'A', **EMBY_A),
                           snap('jellyfin', 'J', options={'sports_library_ids': ['sports']}))
    assert [target.snapshot.kind for target in candidates(sports_row(), 'sports', configuration)] == [
        'emby', 'jellyfin']
    assert candidates(sports_row(path=None), 'sports', configuration) == []


def test_a_recording_is_asked_again_once_its_file_changes(monkeypatch):
    from media_servers import links
    rows = iter([sports_row('/media/NHL/Game 12.mkv'), sports_row('/media/NHL/Game 12.mkv'),
                 sports_row('/media/NHL/Game 12 Proper.mkv')])
    monkeypatch.setattr(links, 'media_metadata_for', lambda media_type, local_id: next(rows))
    asked = []

    def lookup(target, media_type, row, _ensure_current):
        asked.append(row.video_path)
        return f'http://j/{len(asked)}'
    monkeypatch.setattr(links, '_lookup', lookup)
    jellyfin = snap('jellyfin', 'J', options={'sports_library_ids': ['sports']})
    urls = [links.resolve(jellyfin.id, 'sports', 4, native(jellyfin))['url'] for _ in range(3)]
    # Jellyfin keeps no path mapping, but it finds a recording by its file
    # name, so a replaced file is a new question rather than a cached answer.
    assert urls == ['http://j/1', 'http://j/1', 'http://j/2']
    assert asked == ['/media/NHL/Game 12.mkv', '/media/NHL/Game 12 Proper.mkv']
