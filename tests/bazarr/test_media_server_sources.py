"""Server source registration, scans and roots through the public API on both databases."""
import json
from types import SimpleNamespace
from threading import RLock
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from flask import Flask

from media_source_helpers import add_item, add_profile, add_source, video
from test_media_server_item_pages import Response
import test_media_sources_pipeline as pipeline
import test_media_sources_no_change as legacy

isolated_jobs = pipeline.isolated_jobs
everything_uses_the_test_database = legacy.everything_uses_the_test_database
HEADERS = {'X-API-KEY': 'synthetic-bazarr-key'}
SOURCES = '/api/system/media-sources'


def _item(row, path, **fields):
    return {'Id': str(row), 'Name': f'Media {row}', 'Path': path, 'Type': 'Movie', **fields}


def _body(rows, count=None):
    return {'Items': rows, 'TotalRecordCount': len(rows) if count is None else count}


@pytest.fixture(params=['jellyfin', 'emby'])
def saved(request, everything_uses_the_test_database, isolated_jobs, tmp_path, monkeypatch, languages_loaded):
    import requests
    from api import api_bp
    from app.config import settings
    from app.database import TableMediaServerInstances
    from media_servers.repository import MediaServerInstanceRepository
    from media_sources import analysis, scanner, service

    source_db = everything_uses_the_test_database
    monkeypatch.setitem(settings.auth, 'apikey', HEADERS['X-API-KEY'])
    monkeypatch.setattr(settings.general, 'use_sonarr', False)
    monkeypatch.setattr(settings.general, 'use_radarr', False)
    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    monkeypatch.setattr(settings.general, 'use_jellyfin', False)
    monkeypatch.setattr(settings.general, 'use_emby', False)
    monkeypatch.setattr(service, 'refresh_tasks', lambda: None)
    monkeypatch.setattr(scanner, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(MediaServerInstanceRepository, 'snapshot_api_key', lambda self, row: 'fixture')
    monkeypatch.setattr(analysis.video_analyzer, 'parse_video_metadata', lambda *a, **k: {'ffprobe': {'audio': []}})
    monkeypatch.setattr(requests.Session, 'send', Mock(side_effect=AssertionError('Unexpected network request')))

    instance = str(uuid4())
    root = tmp_path / 'media'
    root.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    mappings = [{'local_path': str(other), 'remote_path': '/remote'}] if request.param == 'emby' else []
    options = {'movie_library_ids': ['movies']} if request.param == 'jellyfin' else {}
    source_db.execute(sa.insert(TableMediaServerInstances).values(
        id=instance, kind=request.param, name='Server', enabled=1, url='http://server.invalid', verify_ssl=1,
        path_mappings=json.dumps(mappings), options=json.dumps(options), revision=1))
    profile = add_profile(source_db, [('en', 'False', 'False')])
    state = SimpleNamespace(
        instance=instance, kind=request.param, db=source_db, root=root, other=other, profile=profile, jobs=isolated_jobs,
        folders=[{'ItemId': 'movies', 'Name': 'Films', 'CollectionType': 'movies', 'Locations': ['/remote/Movie']},
                 {'ItemId': 'shows', 'Name': 'Shows', 'CollectionType': 'tvshows', 'Locations': ['/tv']},
                 {'ItemId': 'sports', 'Name': 'Sports', 'CollectionType': 'homevideos', 'Locations': ['/sports']}],
        rows={}, pages={}, lookups=[], queried=[], lookup_results={}, fail_at=None, on_page=None)

    def fetch(url, params=None, **kwargs):
        from media_servers.http import MediaServerError

        assert url.startswith('http://server.invalid/')
        if url.endswith('/Library/VirtualFolders'):
            return Response(state.folders)
        assert url.endswith('/Items')
        if 'Ids' in params:
            ids = params['Ids'].split(',')
            state.lookups.append(ids)
            if any(item in state.lookup_results and state.lookup_results[item] is None for item in ids):
                raise MediaServerError('timeout')
            return Response(_body([{'Id': item} for item in ids if state.lookup_results.get(item)]))
        query = params['ParentId'], params['IncludeItemTypes'], params['StartIndex']
        state.queried.append(query)
        if state.on_page:
            state.on_page()
        if query == state.fail_at:
            raise MediaServerError('timeout')
        if query in state.pages:
            return Response(state.pages[query])
        rows = state.rows.get(query[:2], [])
        return Response(_body(rows[query[2]:query[2] + params['Limit']], len(rows)))

    monkeypatch.setattr(requests.Session, 'get', lambda self, url, **kwargs: fetch(url, **kwargs))
    monkeypatch.setattr(requests.Session, 'request', lambda self, method, url, **kwargs: fetch(url, **kwargs))
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    state.client = app.test_client()
    return state


def _register(saved, *, library=None):
    data = dict(kind=saved.kind, media_server_instance_id=saved.instance, scan_frequency_hours=0)
    if library is not None:
        data['library'] = library
    response = saved.client.post(SOURCES, headers=HEADERS, json=data)
    assert response.status_code == 201, response.json
    assert response.json['media_server_instance_id'] == saved.instance
    assert not {'api_key', 'apikey', 'url'} & response.json.keys()
    return response.json


def _library(saved, locator='movies', server_path='/remote/Movie', local_path=None, **extra):
    return {'server_library_id': locator, 'profile_id': saved.profile,
            'roots': [{'server_path': server_path, 'local_path': str(local_path or saved.root)}], **extra}


def _scan(saved, source_id, library_id):
    response = saved.client.post(f'{SOURCES}/{source_id}/libraries/{library_id}/scan', headers=HEADERS)
    assert response.status_code == 202, response.json
    job = saved.jobs._reserve_next_job()
    assert job.job_id == response.json['job_id']
    assert saved.jobs._run_job(job)
    assert job.status == 'completed', job.error
    return saved.client.get(f'{SOURCES}/{source_id}', headers=HEADERS).json['libraries']


def _indexed(saved, library_id):
    from media_sources import repository

    return {row.external_id: row for row in repository.library_items(library_id, saved.db)}


def test_scan_keeps_unseen_items_until_ids_confirm_and_restores_searchability(saved, monkeypatch):
    from media_servers import item_pages
    from media_sources import repository, service, wanted

    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    media = {item: video(saved.root / f'Film.{item}.2001.mkv') for item in (10, 20, 30, 40, 50)}
    items = {item: _item(item, f'/remote/Movie/{path.name}', ProviderIds={'Imdb': f'tt0000{item}'})
             for item, path in media.items()}
    saved.rows[('movies', 'Movie')] = [items[key] for key in (10, 20, 30, 40)]
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'ok'
    original = _indexed(saved, lib)['30']
    repository.update_item(original.id, saved.db, identity_source='user', title='My selection')
    items[20]['ProviderIds'] = {'Imdb': 'tt99999920'}
    saved.pages.update({('movies', 'Movie', 0): _body([items[10], items[20]], 4),
                        ('movies', 'Movie', 2): _body([items[40], items[50]], 4)})

    _scan(saved, source['id'], lib)
    assert _indexed(saved, lib)['30'].missing_scans == 1
    assert _indexed(saved, lib)['20'].imdb_id == 'tt99999920'
    assert not saved.lookups
    saved.fail_at = ('movies', 'Movie', 2)
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'partial'
    assert _indexed(saved, lib)['30'].missing_scans == 1
    assert not saved.lookups and len(_indexed(saved, lib)) == 5
    saved.fail_at = None
    saved.lookup_results['30'] = True
    _scan(saved, source['id'], lib)
    assert saved.lookups == [['30']] and _indexed(saved, lib)['30'].missing_scans == 0

    saved.lookup_results['30'] = False
    _scan(saved, source['id'], lib)
    _scan(saved, source['id'], lib)
    row = _indexed(saved, lib)['30']
    assert (row.id, row.identity_source, row.server_missing) == (original.id, 'user', 1)
    assert repository.get_item(row.id, saved.db).title == 'My selection' and media[30].exists()
    detail = saved.client.get(f'/api/sources/items/{row.id}', headers=HEADERS).json
    assert detail['server_missing'] is True
    wanted_rows = saved.client.get('/api/movies/wanted?sources=1', headers=HEADERS).json['data']
    assert {item['id'] for item in wanted_rows} == {other.id for key, other in _indexed(saved, lib).items() if key != '30'}
    assert saved.client.get('/api/badges?sources=1', headers=HEADERS).json['source_movies'] == 4
    search = Mock()
    monkeypatch.setattr(service, 'search_wanted_item', search)
    monkeypatch.setattr(wanted, 'jobs_queue', saved.jobs)
    wanted.wanted_search_missing_subtitles_sources('movie', job_id=71)
    assert row.id not in {call.args[0] for call in search.call_args_list}

    saved.pages.clear()
    saved.rows[('movies', 'Movie')] = [items[key] for key in (10, 20, 30, 40, 50)]
    media[40].unlink()
    # A listed file that is unavailable locally is also retained.
    _scan(saved, source['id'], lib)
    restored = _indexed(saved, lib)
    assert (restored['30'].id, restored['30'].server_missing, restored['30'].missing_scans) == (original.id, 0, 0)
    assert restored['40'].id
    saved.rows[('movies', 'Movie')].remove(items[40])
    saved.fail_at = ('movies', 'Movie', 2)
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'partial'
    assert '40' in _indexed(saved, lib)
    saved.fail_at = None
    saved.pages[('movies', 'Movie', 0)] = _body([items[10], items[20]], 5)
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'partial'
    assert '40' not in _indexed(saved, lib)


def test_saved_connection_library_choices_and_root_edit_preview(saved, monkeypatch):
    from media_servers import dispatcher
    from media_sources import repository, schedule

    choices = saved.client.get(f'{SOURCES}/server-libraries', headers=HEADERS,
                               query_string={'media_server_instance_id': saved.instance})
    assert choices.status_code == 200
    assert [entry['content_kind'] for entry in choices.json['data']] == ['movie', 'series', None]
    assert choices.json['data'][0]['selected'] is (saved.kind == 'jellyfin')
    suggested = (str(saved.other / 'Movie') if saved.kind == 'emby' else None)
    assert choices.json['data'][0]['roots'] == [{'server_path': '/remote/Movie', 'local_path': suggested}]
    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    assert saved.client.post(SOURCES, headers=HEADERS, json={
        'kind': saved.kind, 'media_server_instance_id': saved.instance, 'name': 'Second'}).status_code == 409
    assert saved.client.post(SOURCES, headers=HEADERS, json={
        'kind': saved.kind, 'media_server_instance_id': saved.instance, 'api_key': 'client-value'}).status_code == 400
    assert saved.client.post(SOURCES, headers=HEADERS, json={
        'kind': 'emby' if saved.kind == 'jellyfin' else 'jellyfin',
        'media_server_instance_id': saved.instance}).status_code == 404

    still_there = video(saved.root / 'Film.2000.mkv')
    will_move = video(saved.root / 'Another.2001.mkv')
    saved.rows[('movies', 'Movie')] = [_item(number, f'/remote/Movie/{path.name}')
                                       for number, path in ((10, still_there), (20, will_move))]
    _scan(saved, source['id'], lib)
    before = _indexed(saved, lib)
    assert {row.path for row in before.values()} == {str(still_there), str(will_move)}
    repository.update_item(before['20'].id, saved.db, identity_source='user', title='Keep my selection')
    history_id = repository.insert_history(saved.db, item_id=before['10'].id, source_id=source['id'],
                                           item_kind='movie', action=1, description='fixture download')
    url = f"{SOURCES}/{source['id']}/libraries/{lib}"

    remote_only = [{'server_path': '/new', 'local_path': str(saved.root)}]
    assert saved.client.post(f'{url}/roots/preview', headers=HEADERS, json={'roots': remote_only}).json == {
        'removed_items': 0}
    response = saved.client.patch(url, headers=HEADERS, json={'roots': remote_only, 'confirmed_removed': 0})
    assert response.status_code == 200 and response.json['removed_items'] == 0
    saved.jobs._run_job(saved.jobs._reserve_next_job())
    assert set(_indexed(saved, lib)) == {'10', '20'}

    will_move.unlink()
    destination = video(saved.other / 'Different.File.2001.mkv', size=2222)
    changed_roots = [{'server_path': '/mapped', 'local_path': str(saved.other)}]
    preview = saved.client.post(f'{url}/roots/preview', headers=HEADERS, json={'roots': changed_roots})
    assert preview.json == {'removed_items': 1}
    assert saved.client.patch(url, headers=HEADERS, json={'roots': changed_roots}).status_code == 409
    assert set(_indexed(saved, lib)) == {'10', '20'}
    saved.rows[('movies', 'Movie')] = [_item(20, f'/mapped/{destination.name}')]
    response = saved.client.patch(url, headers=HEADERS, json={'roots': changed_roots, 'confirmed_removed': 1})
    assert response.status_code == 200 and response.json['removed_items'] == 1
    assert set(_indexed(saved, lib)) == {'20'}
    saved.jobs._run_job(saved.jobs._reserve_next_job())
    moved = _indexed(saved, lib)['20']
    assert (moved.id, moved.path) == (before['20'].id, str(destination))
    assert repository.get_item(moved.id, saved.db).title == 'Keep my selection'

    configuration = SimpleNamespace(lock=RLock(), delete=Mock())
    monkeypatch.setattr(dispatcher, 'get_native_configuration', lambda: configuration)
    refresh = Mock()
    monkeypatch.setattr(schedule, 'refresh_tasks', refresh)
    response = saved.client.delete(f'/api/system/media-server-instances/{saved.instance}', headers=HEADERS)
    assert response.status_code == 204
    configuration.delete.assert_called_once_with(saved.instance)
    refresh.assert_called_once_with()
    assert saved.client.get(f"{SOURCES}/{source['id']}", headers=HEADERS).status_code == 404
    history = saved.db.execute(sa.select(repository.History).where(repository.History.c.id == history_id)).one()
    assert history.item_id is None and history.source_id is None


def test_listing_received_after_library_disable_does_not_write_items(saved):
    source = _register(saved, library=_library(saved))
    library = source['libraries'][0]['id']
    path = video(saved.root / 'Film.2001.mkv')
    saved.rows[('movies', 'Movie')] = [_item(10, f'/remote/Movie/{path.name}')]

    def disable():
        response = saved.client.patch(f"{SOURCES}/{source['id']}/libraries/{library}", headers=HEADERS,
                                      json={'enabled': False})
        assert response.status_code == 200

    saved.on_page = disable
    _scan(saved, source['id'], library)
    assert not _indexed(saved, library)


def test_each_library_maps_its_own_roots_and_server_ids(saved, tmp_path):
    source = _register(saved)
    special = tmp_path / 'special'
    special.mkdir()
    first = saved.client.post(f"{SOURCES}/{source['id']}/libraries", headers=HEADERS, json={
        **_library(saved), 'roots': [{'server_path': '/remote/Movie', 'local_path': str(saved.root)},
                                    {'server_path': '/remote/Movie/Special', 'local_path': str(special)}],
    }).json['id']
    saved.folders.append({'ItemId': 'other-movies', 'Name': 'Other films', 'CollectionType': 'movies',
                          'Locations': ['/remote/Movie']})
    second = saved.client.post(f"{SOURCES}/{source['id']}/libraries", headers=HEADERS,
                               json=_library(saved, 'other-movies', local_path=saved.other)).json['id']
    movie = video(saved.root / 'Film.2000.mkv')
    special_movie = video(special / 'Film.2001.mkv')
    other_movie = video(saved.other / 'Film.2002.mkv')
    saved.rows[('movies', 'Movie')] = [_item(10, f'/remote/Movie/{movie.name}'),
                                      _item(20, f'/remote/Movie/Special/{special_movie.name}'),
                                      _item(30, '/remote/Movies/Unmapped.mkv')]
    saved.rows[('other-movies', 'Movie')] = [_item(10, f'/remote/Movie/{other_movie.name}')]

    libraries = _scan(saved, source['id'], first)
    assert '1 server path(s)' in next(row for row in libraries if row['id'] == first)['last_scan_message']
    _scan(saved, source['id'], second)
    first_rows, second_rows = _indexed(saved, first), _indexed(saved, second)
    assert {key: row.path for key, row in first_rows.items()} == {'10': str(movie), '20': str(special_movie)}
    assert second_rows['10'].path == str(other_movie)
    assert first_rows['10'].id != second_rows['10'].id


def test_shows_get_series_ids_and_sports_use_the_local_league(saved):
    from media_sources import repository

    source = _register(saved)
    shows = saved.client.post(f"{SOURCES}/{source['id']}/libraries", headers=HEADERS,
                              json=_library(saved, 'shows', '/tv'))
    sports = saved.client.post(f"{SOURCES}/{source['id']}/libraries", headers=HEADERS,
                               json=_library(saved, 'sports', '/sports', saved.other, content_kind='sports'))
    assert shows.status_code == sports.status_code == 201
    show_lib, sport_lib = shows.json['id'], sports.json['id']
    video(saved.root / 'Example Show' / 'Different file.mkv')
    video(saved.root / 'Fallback Show (2004)' / 'Fallback Show S01E01.mkv')
    video(saved.other / 'Formula 1' / '2025' / 'Round 3.mkv')
    saved.rows[('shows', 'Series')] = [
        {'Id': 'series1', 'Name': 'Server Show', 'Type': 'Series', 'ProviderIds': {'Tvdb': '400', 'Tmdb': '500'}}]
    saved.rows[('shows', 'Episode')] = [
        _item('episode1', '/tv/Example Show/Different file.mkv', Type='Episode', Name='Episode title',
              ProviderIds={'Tvdb': '401', 'Imdb': 'tt1234567'}, SeriesId='series1', ParentIndexNumber=2, IndexNumber=7),
        _item('fallback', '/tv/Fallback Show (2004)/Fallback Show S01E01.mkv', Type='Episode'),
    ]
    saved.rows[('sports', 'Movie,Episode,Video')] = [
        _item('sport1', '/sports/Formula 1/2025/Round 3.mkv', Type='Video', Name='Monaco Grand Prix Race',
              PremiereDate='2025-05-25T12:00:00Z'),
        _item('off-root', '/different/other.mkv', Type='Video'),
    ]
    _scan(saved, source['id'], show_lib)
    outcome = _scan(saved, source['id'], sport_lib)
    result = next(library for library in outcome if library['id'] == sport_lib)
    assert '1 server path(s)' in result['last_scan_message']
    assert [query[1] for query in saved.queried if query[0] == 'shows'] == ['Series', 'Episode']
    episode = repository.get_item(_indexed(saved, show_lib)['episode1'].id, saved.db)
    show = repository.get_group(episode.group_id, saved.db)
    assert (show.title, show.tvdb_id, show.tmdb_id, show.external_id, show.identity_source) == (
        'Server Show', 400, '500', 'series1', 'media_server')
    assert (episode.title, episode.tvdb_id, episode.imdb_id, episode.tmdb_id, episode.season, episode.episode) == (
        'Episode title', 401, 'tt1234567', None, 2, 7)
    fallback = repository.get_item(_indexed(saved, show_lib)['fallback'].id, saved.db)
    assert (fallback.season, fallback.episode, fallback.identity_state) == (1, 1, 'parsed')
    sport = repository.get_item(_indexed(saved, sport_lib)['sport1'].id, saved.db)
    league = repository.get_group(sport.group_id, saved.db)
    assert (league.title, sport.title, sport.event_date, sport.episode, sport.part_name) == (
        'Formula 1', 'Monaco Grand Prix', '2025-05-25', 3, 'Race')


def test_id_lookup_batches_stop_on_first_error_and_keep_previous_flags(saved, monkeypatch):
    from media_servers import item_pages
    from media_sources import repository

    monkeypatch.setattr(item_pages, 'LOOKUP_SIZE', 1)
    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    records = []
    for number in range(4):
        path = video(saved.root / f'Title.{number}.mkv')
        records.append(_item(number, f'/remote/Movie/{path.name}'))
    saved.rows[('movies', 'Movie')] = records
    _scan(saved, source['id'], lib)
    for row in _indexed(saved, lib).values():
        repository.update_item(row.id, saved.db, missing_scans=1)
    saved.rows[('movies', 'Movie')] = records[3:]
    saved.lookup_results.update({'0': True, '1': None})
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'partial'
    assert saved.lookups == [['0'], ['1']]
    rows = _indexed(saved, lib)
    assert {key: (row.missing_scans, row.server_missing) for key, row in rows.items()} == {
        '0': (0, 0), '1': (2, 0), '2': (2, 0), '3': (0, 0)}


def test_enabled_folder_holder_wins_when_server_lists_a_hardlink(saved):
    from os import link

    source_id, library_id, root = add_source(saved.db, saved.root.parent, name='Folders', folder='Folder')
    old_path = video(root / 'Old.mkv')
    info = old_path.stat()
    holder = add_item(saved.db, source_id, library_id, old_path, file_dev=str(info.st_dev), file_ino=str(info.st_ino),
                      file_size=info.st_size, file_mtime_ns=info.st_mtime_ns)
    link(old_path, saved.root / 'Linked.mkv')
    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    saved.rows[('movies', 'Movie')] = [_item('20', '/remote/Movie/Linked.mkv')]
    result = _scan(saved, source['id'], lib)[0]
    assert 'already supplied by Folders' in result['last_scan_message'] and not _indexed(saved, lib)
    from media_sources import repository
    assert repository.get_item(holder, saved.db).path == str(old_path)


def test_server_roots_choose_the_longest_segment_prefix_and_refuse_ties(tmp_path):
    import os
    from media_servers.http import MediaServerError
    from media_sources.paths import ServerRoots

    base = str(tmp_path / 'base')
    exact = str(tmp_path / 'exact')
    rows = [SimpleNamespace(server_path='C:/media', local_path=base),
            SimpleNamespace(server_path='C:/media/TV', local_path=exact)]
    roots = ServerRoots(rows)
    assert roots.resolve(r'c:\media\tv\Episode.mkv') == (os.path.join(exact, 'Episode.mkv'), exact)
    with pytest.raises(MediaServerError, match='mapping_missing'):
        roots.resolve('C:/MediaExtra/Episode.mkv')
    rows.append(SimpleNamespace(server_path='c:/MEDIA/tv', local_path=base))
    with pytest.raises(MediaServerError, match='mapping_ambiguous'):
        ServerRoots(rows).resolve('C:/media/TV/Episode.mkv')
