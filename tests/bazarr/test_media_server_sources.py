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
        rows={}, pages={}, lookups=[], queried=[], lookup_results={}, fail_at=None, on_page=None, on_lookup=None)

    def fetch(url, params=None, **kwargs):
        from media_servers.http import MediaServerError

        assert url.startswith('http://server.invalid/')
        if url.endswith('/Library/VirtualFolders'):
            return Response(state.folders)
        assert url.endswith('/Items')
        if 'Ids' in params:
            ids = params['Ids'].split(',')
            state.lookups.append(ids)
            if state.on_lookup:
                state.on_lookup()
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


@pytest.mark.parametrize('whole_source', [False, True], ids=['library', 'source'])
@pytest.mark.parametrize('running', [False, True], ids=['pending', 'running'])
def test_manual_scan_follows_the_existing_job(saved, whole_source, running):
    from media_sources import scanner

    source = _register(saved, library=_library(saved))
    library_id = source['libraries'][0]['id']
    job_id = scanner.scan_media_source(source['id'], None if whole_source else library_id)
    url = f"{SOURCES}/{source['id']}" + ('' if whole_source else f'/libraries/{library_id}')
    responses = []

    def request_scan():
        response = saved.client.post(f'{url}/scan', headers=HEADERS)
        assert response.status_code == 202, response.json
        assert response.json['job_id'] == job_id
        assert response.json['session']
        assert saved.jobs.get_job_status(job_id) == ('running' if running else 'pending')
        responses.append(response)

    if running:
        saved.on_page = request_scan
    else:
        request_scan()
    job = saved.jobs._reserve_next_job()
    assert job.job_id == job_id
    assert saved.jobs._reserve_next_job() is None
    assert saved.jobs._run_job(job)
    assert responses
    assert saved.jobs._reserve_next_job() is None
    assert saved.jobs.get_job_status(job_id) == 'completed'


@pytest.mark.parametrize('scan_finished', [False, True], ids=['pending', 'before-job-completion'])
def test_root_edit_scans_new_mappings_and_removes_sports_item(saved, monkeypatch, scan_finished):
    from media_sources import scanner

    source = _register(saved, library=_library(saved, 'sports', '/sports', content_kind='sports'))
    library_id = source['libraries'][0]['id']
    prior = video(saved.root / 'Formula 1' / '2025' / 'Round 3.mkv')
    saved.rows['sports', 'Movie,Episode,Video'] = [
        _item('prior', f'/sports/{prior.relative_to(saved.root)}', Type='Video')]
    _scan(saved, source['id'], library_id)
    prior_id = _indexed(saved, library_id)['prior'].id
    assert saved.client.get(f'/api/sources/items/{prior_id}', headers=HEADERS).status_code == 200
    job_id = scanner.scan_media_source(source['id'], library_id)
    replacement = video(saved.other / 'Formula 1' / '2025' / 'Round 4.mkv')
    url = f"{SOURCES}/{source['id']}/libraries/{library_id}"

    def change_roots():
        saved.rows['sports', 'Movie,Episode,Video'] = [
            _item('replacement', f'/mapped/{replacement.relative_to(saved.other)}', Type='Video')]
        response = saved.client.patch(url, headers=HEADERS, json={
            'roots': [{'server_path': '/mapped', 'local_path': str(saved.other)}], 'confirmed_removed': 1})
        assert response.status_code == 200, response.json
        assert response.json['removed_items'] == 1
        manual = saved.client.post(f'{url}/scan', headers=HEADERS)
        assert manual.status_code == 202, manual.json
        assert manual.json['job_id'] == response.json['job_id']
        return response.json['job_id']

    if scan_finished:
        updated_jobs = []
        update_progress = saved.jobs.update_job_progress

        def edit_after_scan(**progress):
            if progress['job_id'] == job_id and progress.get('progress_value') == 'max':
                assert saved.jobs.get_job_status(job_id) == 'running'
                unchanged = saved.client.patch(url, headers=HEADERS, json={'roots': [
                    {'server_path': '/sports', 'local_path': str(saved.root)}]})
                assert unchanged.status_code == 200, unchanged.json
                assert unchanged.json['job_id'] == job_id
                updated_jobs.append(change_roots())
            return update_progress(**progress)

        monkeypatch.setattr(saved.jobs, 'update_job_progress', edit_after_scan)
        job = saved.jobs._reserve_next_job()
        assert job.job_id == job_id
        assert saved.jobs._run_job(job), job.error
        new_job_id, = updated_jobs
        assert new_job_id != job_id
    else:
        new_job_id = change_roots()
        assert new_job_id == job_id

    assert saved.client.get(f'/api/sources/items/{prior_id}', headers=HEADERS).status_code == 404
    assert prior.exists()
    job = saved.jobs._reserve_next_job()
    assert job.job_id == new_job_id
    assert saved.jobs._reserve_next_job() is None
    assert saved.jobs._run_job(job)
    assert job.status == 'completed', job.error
    assert set(_indexed(saved, library_id)) == {'replacement'}


def test_validate_new_server_roots_without_mutation(saved):
    url = f'{SOURCES}/server-roots/validate'
    roots = _library(saved)['roots']
    payload = {'content_kind': 'movie', 'roots': roots}
    assert saved.client.post(url, headers=HEADERS, json=payload).status_code == 200
    missing = [{'server_path': '/remote', 'local_path': str(saved.root / 'missing')}]
    assert saved.client.post(url, headers=HEADERS, json={**payload, 'roots': missing}).status_code == 400
    duplicate = roots + [{'server_path': '/another', 'local_path': str(saved.root)}]
    assert saved.client.post(url, headers=HEADERS, json={**payload, 'roots': duplicate}).status_code == 400
    assert saved.client.post(url, headers=HEADERS, json={**payload, 'content_kind': 'invalid'}).status_code == 400
    assert saved.client.get(SOURCES, headers=HEADERS).json == []
    source = _register(saved, library=_library(saved))
    before = saved.client.get(f"{SOURCES}/{source['id']}", headers=HEADERS).json
    assert saved.client.post(url, headers=HEADERS, json=payload).status_code == 409
    assert saved.client.get(f"{SOURCES}/{source['id']}", headers=HEADERS).json == before


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
    assert '40' in _indexed(saved, lib)
    saved.pages.clear()
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'ok'
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
    job = saved.jobs._reserve_next_job()
    assert response.json['job_id'] == job.job_id
    saved.jobs._run_job(job)
    assert set(_indexed(saved, lib)) == {'10', '20'}

    will_move.unlink()
    destination = video(saved.other / 'Different.File.2001.mkv', size=2222)
    changed_roots = [{'server_path': '/mapped', 'local_path': str(saved.other)}]
    preview = saved.client.post(f'{url}/roots/preview', headers=HEADERS, json={'roots': changed_roots})
    assert preview.json == {'removed_items': 1}
    assert saved.client.patch(url, headers=HEADERS, json={'roots': changed_roots}).status_code == 409
    assert set(_indexed(saved, lib)) == {'10', '20'}
    listed = video(saved.other / 'Listed.2002.mkv')
    saved.rows[('movies', 'Movie')] = [_item(30, f'/mapped/{listed.name}')]
    response = saved.client.patch(url, headers=HEADERS, json={'roots': changed_roots, 'confirmed_removed': 1})
    assert response.status_code == 200 and response.json['removed_items'] == 1
    assert set(_indexed(saved, lib)) == {'20'}
    job = saved.jobs._reserve_next_job()
    assert response.json['job_id'] == job.job_id
    saved.jobs._run_job(job)
    assert _indexed(saved, lib)['20'].id == before['20'].id
    saved.rows[('movies', 'Movie')].append(_item(20, f'/mapped/{destination.name}'))
    _scan(saved, source['id'], lib)
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


def test_add_library_refuses_a_choice_from_an_old_connection(saved, monkeypatch):
    from media_sources import service

    source = _register(saved)
    original = service._server_library_fields

    def edit_after_response(*args, **kwargs):
        fields = original(*args, **kwargs)
        _change_connection(saved)
        return fields

    with monkeypatch.context() as patch:
        patch.setattr(service, '_server_library_fields', edit_after_response)
        response = saved.client.post(f"{SOURCES}/{source['id']}/libraries", headers=HEADERS, json=_library(saved))
    assert response.status_code == 409, response.json
    current = saved.client.get(f"{SOURCES}/{source['id']}", headers=HEADERS).json
    assert current['libraries'] == []
    assert saved.client.post(f"{SOURCES}/{source['id']}/libraries", headers=HEADERS, json=_library(saved)).status_code == 201


def _change_connection(saved):
    from app.database import TableMediaServerInstances

    saved.db.execute(sa.update(TableMediaServerInstances).where(TableMediaServerInstances.id == saved.instance)
                     .values(revision=TableMediaServerInstances.revision + 1))


@pytest.mark.parametrize('change', [
    'connection_after_listing', 'connection_during_lookup', 'connection_after_lookup', 'roots_during_lookup',
])
def test_scan_rejects_configuration_changes_before_writing(saved, monkeypatch, change):
    from media_sources import identity, repository, scanner
    from media_sources.db import source_transaction

    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    prior = video(saved.root / 'Prior.2000.mkv')
    new = video(saved.root / 'New.2001.mkv')
    saved.rows[('movies', 'Movie')] = [_item(10, f'/remote/Movie/{prior.name}')]
    _scan(saved, source['id'], lib)
    before = _indexed(saved, lib)['10']
    repository.update_item(before.id, saved.db, missing_scans=1)
    saved.rows[('movies', 'Movie')] = [_item(20, f'/remote/Movie/{new.name}')]

    def replace_roots():
        with source_transaction(saved.db) as connection:
            repository.replace_roots(lib, [('/remote/Movie', str(saved.other))], connection)

    with monkeypatch.context() as patch:
        if change == 'connection_after_listing':
            match = identity.match_library

            def edit_after_listing(*args, **kwargs):
                matching = match(*args, **kwargs)
                _change_connection(saved)
                return matching

            patch.setattr(identity, 'match_library', edit_after_listing)
        elif change == 'connection_after_lookup':
            decide = scanner._decide

            def edit_after_lookup(*args, **kwargs):
                decisions = decide(*args, **kwargs)
                _change_connection(saved)
                return decisions

            patch.setattr(scanner, '_decide', edit_after_lookup)
        else:
            saved.on_lookup = replace_roots if change.startswith('roots') else lambda: _change_connection(saved)
        outcome = _scan(saved, source['id'], lib)[0]
    saved.on_lookup = None
    assert outcome['last_scan_status'] == 'partial'
    assert saved.lookups == ([] if change == 'connection_after_listing' else [['10']])
    after = _indexed(saved, lib)
    assert set(after) == {'10'}
    assert (after['10'].id, after['10'].missing_scans, after['10'].server_missing) == (before.id, 1, 0)

    if change.startswith('roots'):
        new.rename(saved.other / new.name)
    retry = saved.jobs._reserve_next_job()
    assert retry.kwargs['retry_on_configuration_change'] is False
    assert saved.jobs._run_job(retry)
    assert retry.status == 'completed', retry.error
    status = saved.client.get(f"{SOURCES}/{source['id']}", headers=HEADERS).json['libraries'][0]
    assert status['last_scan_status'] == 'ok'
    assert '20' in _indexed(saved, lib)


def test_scan_rechecks_revision_at_subtitle_index_write(saved, monkeypatch):
    from media_sources import indexer, repository

    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    path = video(saved.root / 'Film.2001.mkv')
    saved.rows[('movies', 'Movie')] = [_item(10, f'/remote/Movie/{path.name}')]
    probe = indexer.probe_item

    def probe_then_edit(*args, **kwargs):
        result = probe(*args, **kwargs)
        _change_connection(saved)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(indexer, 'probe_item', probe_then_edit)
        assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'partial'
    old_item = repository.get_item(_indexed(saved, lib)['10'].id, saved.db)
    assert old_item.indexed_at is None
    retry = saved.jobs._reserve_next_job()
    assert retry.kwargs['retry_on_configuration_change'] is False
    assert saved.jobs._run_job(retry)
    assert retry.status == 'completed', retry.error
    assert repository.get_item(old_item.id, saved.db).indexed_at is not None


def test_confirmed_displacement_preserves_both_server_identities(saved):
    from media_sources import repository

    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    first = video(saved.root / 'First.2000.mkv')
    second = video(saved.root / 'Second.2001.mkv', size=2222)
    saved.rows[('movies', 'Movie')] = [_item(10, f'/remote/Movie/{first.name}'),
                                     _item(20, f'/remote/Movie/{second.name}')]
    _scan(saved, source['id'], lib)
    before = _indexed(saved, lib)
    repository.update_item(before['10'].id, saved.db, identity_source='user', title='Keep this identity')
    history_id = repository.insert_history(saved.db, item_id=before['10'].id, source_id=source['id'],
                                           item_kind='movie', action=1, description='fixture download')
    second.replace(first)
    saved.rows[('movies', 'Movie')] = [_item(20, f'/remote/Movie/{first.name}')]

    _scan(saved, source['id'], lib)
    assert _indexed(saved, lib)['20'].path == str(second)
    saved.lookup_results['10'] = True
    _scan(saved, source['id'], lib)
    assert _indexed(saved, lib)['20'].path == str(second)
    assert _indexed(saved, lib)['10'].missing_scans == 0
    saved.lookup_results['10'] = False
    _scan(saved, source['id'], lib)
    _scan(saved, source['id'], lib)
    moved = _indexed(saved, lib)
    assert (moved['20'].id, moved['20'].path) == (before['20'].id, str(first))
    assert (moved['10'].id, moved['10'].server_missing) == (before['10'].id, 1)
    detail = saved.client.get(f"/api/sources/items/{before['10'].id}", headers=HEADERS)
    assert detail.status_code == 200
    assert (detail.json['path'], detail.json['server_missing']) == (str(first), True)
    history = saved.db.execute(sa.select(repository.History).where(repository.History.c.id == history_id)).one()
    assert history.item_id == before['10'].id

    returned = video(saved.root / 'Returned.2000.mkv')
    saved.rows[('movies', 'Movie')].append(_item(10, f'/remote/Movie/{returned.name}'))
    _scan(saved, source['id'], lib)
    restored = _indexed(saved, lib)['10']
    assert (restored.id, restored.path, restored.server_missing) == (before['10'].id, str(returned), 0)
    assert repository.get_item(restored.id, saved.db).title == 'Keep this identity'


@pytest.mark.parametrize('replace_file', [False, True])
def test_partial_scan_preserves_the_identity_at_a_reused_path(saved, monkeypatch, replace_file):
    from media_servers import item_pages
    from media_sources import repository

    source = _register(saved, library=_library(saved))
    lib = source['libraries'][0]['id']
    movie = video(saved.root / 'Film.2000.mkv')
    remote = f'/remote/Movie/{movie.name}'
    saved.rows[('movies', 'Movie')] = [_item(10, remote)]
    _scan(saved, source['id'], lib)
    before = _indexed(saved, lib)['10']
    repository.update_item(before.id, saved.db, identity_source='user', title='Keep this identity', server_missing=1)
    if replace_file:
        video(saved.root / 'Replacement.mkv', size=2222).replace(movie)
    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 1)
    saved.pages[('movies', 'Movie', 0)] = _body([_item(20, remote)], 2)
    saved.fail_at = ('movies', 'Movie', 1)

    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'partial'
    rows = _indexed(saved, lib)
    assert set(rows) == {'10'}
    assert (rows['10'].id, rows['10'].path, rows['10'].server_missing) == (before.id, str(movie), 1)
    assert repository.get_item(before.id, saved.db).title == 'Keep this identity'

    saved.pages.clear()
    saved.fail_at = None
    saved.rows[('movies', 'Movie')] = [_item(20, remote)]
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'ok'
    assert _indexed(saved, lib)['20'].id == before.id


@pytest.mark.parametrize('original_first', [False, True], ids=['replacement-first', 'original-first'])
def test_conflicting_server_paths_stay_out_of_search_until_confirmed(saved, monkeypatch, original_first):
    from app import database
    from media_servers import item_pages
    from media_sources import repository, service
    from subtitles import download

    monkeypatch.setattr(database, 'engine', saved.db.get_bind())

    source = _register(saved, library=_library(saved))
    source_id, lib = source['id'], source['libraries'][0]['id']
    movie = video(saved.root / 'Film.2000.mkv')
    control = video(saved.root / 'Control.2001.mkv')
    remote = f'/remote/Movie/{movie.name}'
    original = _item(10, remote, ProviderIds={'Tmdb': '42'})
    replacement = _item(20, remote)
    other = _item(30, f'/remote/Movie/{control.name}', ProviderIds={'Tmdb': '42'})
    listing = ('movies', 'Movie')
    saved.rows[listing] = [original, other]
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'ok'
    before = _indexed(saved, lib)
    original_id, control_id = before['10'].id, before['30'].id
    repository.update_item(original_id, saved.db, identity_source='user', title='Keep this identity')

    def discover_copies():
        response = saved.client.get('/api/discover/copies', headers=HEADERS,
                                    query_string={'media_type': 'movie', 'tmdb_id': 42})
        assert response.status_code == 200, response.json
        return response.json['items']

    initial_copies = discover_copies()
    assert {copy['local_id'] for copy in initial_copies if copy['selectable']} == {original_id, control_id}
    original_copy = next(copy['copy_id'] for copy in initial_copies if copy['local_id'] == original_id)
    last_seen = repository.get_item(original_id, saved.db).last_seen_at
    history_id = repository.insert_history(saved.db, item_id=original_id, source_id=source_id,
                                           item_kind='movie', action=1, description='fixture download')

    generate = Mock(side_effect=lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(download, 'generate_subtitles', generate)
    monkeypatch.setattr(service, 'get_providers', lambda: ['fake'])

    def search(item_id):
        response = saved.client.post(f'/api/sources/items/{item_id}/search', headers=HEADERS)
        assert response.status_code == 202, response.json
        job = saved.jobs._reserve_next_job()
        assert job.job_id == response.json['job_id']
        assert saved.jobs._run_job(job)
        assert job.status == 'completed', job.error

    search(original_id)
    assert [call.args[0] for call in generate.call_args_list] == [str(movie)]
    generate.reset_mock()

    original_file = saved.root / 'Original.mkv'
    movie.rename(original_file)
    video(movie, size=2222)
    competing = [original, replacement] if original_first else [replacement, original]
    saved.rows[listing] = [*competing, other]
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'partial'
    search(original_id)
    search(control_id)
    assert [call.args[0] for call in generate.call_args_list] == [str(control)]
    rows = _indexed(saved, lib)
    assert set(rows) == {'10', '30'}
    assert (rows['10'].id, rows['10'].path, rows['10'].server_missing) == (original_id, str(movie), 1)
    assert rows['10'].file_size == before['10'].file_size
    item = repository.get_item(original_id, saved.db)
    assert (item.title, item.last_seen_at) == ('Keep this identity', last_seen)
    history = saved.db.execute(sa.select(repository.History).where(repository.History.c.id == history_id)).one()
    assert history.item_id == original_id
    assert movie.is_file()
    assert [copy['local_id'] for copy in discover_copies()] == [control_id]
    rejected = saved.client.post('/api/discover/search', headers=HEADERS,
                                 json={'media_type': 'movie', 'tmdb_id': 42, 'language': 'eng',
                                       'copy_id': original_copy})
    assert rejected.status_code == 409, rejected.json
    assert rejected.json['reason'] == 'copy_unavailable'

    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    saved.pages[(*listing, 0)] = _body([original, other], 3)
    saved.fail_at = (*listing, 2)
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'partial'
    search(original_id)
    assert [call.args[0] for call in generate.call_args_list] == [str(control)]

    saved.pages.clear()
    saved.fail_at = None
    saved.rows[listing] = [other]
    saved.lookup_results['10'] = True
    for _ in range(2):
        assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'ok'
    assert saved.lookups == [['10']]
    search(original_id)
    assert [call.args[0] for call in generate.call_args_list] == [str(control)]

    original_file.replace(movie)
    saved.rows[listing] = [original, other]
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'ok'
    restored = _indexed(saved, lib)['10']
    assert (restored.id, restored.path, restored.server_missing) == (original_id, str(movie), 0)
    assert original_copy in {copy['copy_id'] for copy in discover_copies() if copy['selectable']}
    search(original_id)
    assert [call.args[0] for call in generate.call_args_list] == [str(control), str(movie)]


@pytest.mark.parametrize('original_first', [False, True], ids=['replacement-first', 'original-first'])
def test_complete_replacement_after_path_conflict_keeps_the_movie_identity(saved, monkeypatch, original_first):
    from media_servers import item_pages
    from media_sources import repository, service
    from subtitles import download

    source = _register(saved, library=_library(saved))
    source_id, lib = source['id'], source['libraries'][0]['id']
    movie = video(saved.root / 'Film.2000.mkv')
    control = video(saved.root / 'Control.2001.mkv')
    remote = f'/remote/Movie/{movie.name}'
    original = _item(10, remote)
    replacement = _item(20, remote, ProviderIds={'Imdb': 'tt0000020'})
    other = _item(30, f'/remote/Movie/{control.name}')
    listing = ('movies', 'Movie')
    saved.rows[listing] = [original, other]
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'ok'
    before = repository.get_item(_indexed(saved, lib)['10'].id, saved.db)
    repository.update_item(before.id, saved.db, identity_source='user', title='Keep this identity',
                           imdb_id='tt7654321')
    history_id = repository.insert_history(saved.db, item_id=before.id, source_id=source_id,
                                           item_kind='movie', action=1, description='fixture download')
    history_query = sa.select(repository.History).where(repository.History.c.id == history_id)
    history_before = saved.db.execute(history_query).one()
    fingerprint = ('file_dev', 'file_ino', 'file_size', 'file_mtime_ns')

    video(saved.root / 'Replacement.mkv', size=2222).replace(movie)
    competing = [original, replacement] if original_first else [replacement, original]
    saved.rows[listing] = [*competing, other]
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'partial'
    assert set(_indexed(saved, lib)) == {'10', '30'}

    generate = Mock(side_effect=lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(download, 'generate_subtitles', generate)
    monkeypatch.setattr(service, 'get_providers', lambda: ['fake'])

    def search():
        response = saved.client.post(f'/api/sources/items/{before.id}/search', headers=HEADERS)
        assert response.status_code == 202, response.json
        job = saved.jobs._reserve_next_job()
        assert job.job_id == response.json['job_id']
        assert saved.jobs._run_job(job)
        assert job.status == 'completed', job.error

    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    saved.pages[(*listing, 0)] = _body([replacement, other], 3)
    saved.fail_at = (*listing, 2)
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'partial'
    held = _indexed(saved, lib)
    assert set(held) == {'10', '30'}
    assert (held['10'].id, held['10'].source_id, held['10'].library_id, held['10'].external_id,
            held['10'].path, held['10'].server_missing) == (before.id, source_id, lib, '10', str(movie), 1)
    assert (held['10'].identity_source, held['10'].title, held['10'].imdb_id) == (
        'user', 'Keep this identity', 'tt7654321')
    assert tuple(getattr(held['10'], field) for field in fingerprint) == tuple(
        getattr(before, field) for field in fingerprint)
    assert repository.get_item(before.id, saved.db).last_seen_at == before.last_seen_at
    assert saved.db.execute(history_query).one() == history_before
    search()
    generate.assert_not_called()

    saved.pages.clear()
    saved.fail_at = None
    saved.rows[listing] = [replacement, other]
    assert _scan(saved, source_id, lib)[0]['last_scan_status'] == 'ok'
    rows = _indexed(saved, lib)
    assert set(rows) == {'20', '30'}
    recovered = rows['20']
    assert (recovered.id, recovered.source_id, recovered.library_id, recovered.external_id, recovered.kind,
            recovered.path, recovered.server_missing, recovered.missing_scans) == (
                before.id, source_id, lib, '20', 'movie', str(movie), 0, 0)
    assert (recovered.identity_source, recovered.title, recovered.imdb_id) == (
        'user', 'Keep this identity', 'tt7654321')
    stat = movie.stat()
    assert tuple(getattr(recovered, field) for field in fingerprint) == (
        str(stat.st_dev), str(stat.st_ino), stat.st_size, stat.st_mtime_ns)
    assert saved.db.execute(history_query).one() == history_before
    detail = saved.client.get(f'/api/sources/items/{before.id}', headers=HEADERS)
    assert detail.status_code == 200
    assert (detail.json['source_id'], detail.json['library_id'], detail.json['external_id'],
            detail.json['kind'], detail.json['identity_source']) == (source_id, lib, '20', 'movie', 'user')
    search()
    assert [call.args[0] for call in generate.call_args_list] == [str(movie)]
    context = generate.call_args.kwargs['source']
    assert (context.item_id, context.source_id, context.library_id, context.kind) == (before.id, source_id, lib, 'movie')


@pytest.mark.parametrize('edit', [None, 'roots', 'connection'])
def test_handover_rechecks_previous_library_settings(saved, tmp_path, monkeypatch, edit):
    from app.database import TableMediaServerInstances
    from media_sources import repository, scanner

    prior = _register(saved, library=_library(saved))
    prior_id, prior_lib, prior_connection = prior['id'], prior['libraries'][0]['id'], saved.instance
    saved.instance = str(uuid4())
    saved.db.execute(sa.insert(TableMediaServerInstances).values(
        id=saved.instance, kind=saved.kind, name='Other server', enabled=1, url='http://server.invalid',
        verify_ssl=1, revision=1))
    saved.folders.append({'ItemId': 'other-movies', 'Name': 'Other films', 'CollectionType': 'movies',
                          'Locations': ['/remote/Movie']})
    destination = _register(saved, library=_library(saved, 'other-movies', local_path=saved.other))
    destination_id, destination_lib = destination['id'], destination['libraries'][0]['id']
    movie = video(saved.root / 'Film.2000.mkv')
    witness = video(saved.root / 'Witness.2001.mkv')
    remote = f'/remote/Movie/{movie.name}'
    witness_item = _item(11, f'/remote/Movie/{witness.name}')
    saved.rows[('movies', 'Movie')] = [_item(10, remote), witness_item]
    _scan(saved, prior_id, prior_lib)
    before = _indexed(saved, prior_lib)['10']
    movie.rename(saved.other / movie.name)
    saved.rows[('movies', 'Movie')] = [witness_item]
    saved.rows[('other-movies', 'Movie')] = [_item(20, remote)]

    decide = scanner._decide

    def edit_previous_owner(*args, **kwargs):
        decisions = decide(*args, **kwargs)
        assert [(row.id, handover) for row, _, handover in decisions.repathed] == [(before.id, True)]
        if edit is None:
            return decisions
        if edit == 'roots':
            new_root = tmp_path / 'new-root'
            new_root.mkdir()
            response = saved.client.patch(f'{SOURCES}/{prior_id}/libraries/{prior_lib}', headers=HEADERS, json={
                'roots': [{'server_path': '/remote/Movie', 'local_path': str(new_root)}], 'confirmed_removed': 1,
            })
        else:
            response = saved.client.patch(f'/api/system/media-server-instances/{prior_connection}', headers=HEADERS,
                                          json={'api_key': 'fixture-replacement'})
        assert response.status_code == 200, response.json
        return decisions

    monkeypatch.setattr(scanner, '_decide', edit_previous_owner)
    outcome = _scan(saved, destination_id, destination_lib)[0]
    after = repository.get_item(before.id, saved.db)
    if edit is None:
        assert outcome['last_scan_status'] == 'ok'
        assert (after.library_id, after.external_id, after.path) == (destination_lib, '20', str(saved.other / movie.name))
    else:
        assert outcome['last_scan_status'] == 'partial'
        assert (after.library_id, after.external_id, after.path) == (prior_lib, '10', str(movie))
        assert not _indexed(saved, destination_lib)


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

    group = saved.client.get(f'/api/sources/groups/{league.id}', headers=HEADERS)
    assert group.status_code == 200
    assert (group.json['kind'], group.json['title']) == ('league', 'Formula 1')
    detail = saved.client.get(f'/api/sources/items/{sport.id}', headers=HEADERS)
    assert detail.status_code == 200
    assert (detail.json['group_id'], detail.json['event_date']) == (league.id, '2025-05-25')
    wanted = saved.client.get('/api/sports/wanted', headers=HEADERS,
                              query_string={'sources': 1, 'source_key[]': f"source:{source['id']}"})
    assert wanted.status_code == 200
    assert [(row['id'], row['origin']) for row in wanted.json['data']] == [(sport.id, 'source')]


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


def test_enabled_folder_holder_wins_when_server_lists_a_hardlink(saved, monkeypatch):
    from os import link

    from media_servers import item_pages
    from media_sources import repository

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
    assert repository.get_item(holder, saved.db).path == str(old_path)

    response = saved.client.patch(f'{SOURCES}/{source_id}/libraries/{library_id}', headers=HEADERS,
                                  json={'enabled': False})
    assert response.status_code == 200, response.json
    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 1)
    saved.pages[('movies', 'Movie', 0)] = _body(saved.rows[('movies', 'Movie')], 2)
    saved.fail_at = ('movies', 'Movie', 1)
    result = _scan(saved, source['id'], lib)[0]
    assert result['last_scan_status'] == 'partial' and not _indexed(saved, lib)
    assert repository.get_item(holder, saved.db).library_id == library_id

    saved.pages.clear()
    saved.fail_at = None
    assert _scan(saved, source['id'], lib)[0]['last_scan_status'] == 'ok'
    assert _indexed(saved, lib)['20'].id == holder


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


@pytest.mark.parametrize('content_kind', ['series', 'sports'])
@pytest.mark.parametrize('group_state', ['new-empty', 'new-used', 'pre-existing'])
def test_refused_server_handover_cleans_up_only_its_new_empty_group(saved, monkeypatch, content_kind, group_state):
    from media_sources import repository

    series = content_kind == 'series'
    locator, server_root, types = ('shows', '/tv', 'Episode') if series else ('sports', '/sports', 'Movie,Episode,Video')
    item_type, group_kind = ('Episode', 'show') if series else ('Video', 'league')
    prior_source, prior_lib, prior_root = add_source(saved.db, saved.root.parent, name='Folders', folder='Folder',
                                                   content_kind=content_kind)
    incoming = video(prior_root / 'Incoming' / 'Incoming.S01E01.mkv')
    witness = video(prior_root / 'Witness' / 'Witness.S01E01.mkv')
    assert _scan(saved, prior_source, prior_lib)[0]['last_scan_status'] == 'ok'
    before = repository.get_item_by_path(str(incoming), saved.db)
    prior_groups = repository.library_groups(prior_lib, saved.db)

    source = _register(saved, library=_library(saved, locator, server_root, content_kind=content_kind))
    source_id, library_id = source['id'], source['libraries'][0]['id']
    target = saved.root / 'Destination' / incoming.name
    target.parent.mkdir()
    incoming.rename(target)
    group_fields = {'SeriesId': 'group-1', 'ParentIndexNumber': 1} if series else {}
    saved.rows[locator, types] = [
        _item('incoming', f'{server_root}/Destination/{target.name}', Type=item_type, IndexNumber=1, **group_fields),
    ]
    if series:
        saved.rows[locator, 'Series'] = [{'Id': 'group-1', 'Name': 'Destination', 'Type': 'Series'}]
    if group_state == 'pre-existing':
        group_id = repository.create_group(source_id=source_id, library_id=library_id, kind=group_kind,
                                           group_key='server:group-1' if series else str(target.parent),
                                           title='Existing identity', identity_source='user', target=saved.db)
    if group_state == 'new-used':
        companion = video(target.parent / 'Companion.S01E02.mkv')
        saved.rows[locator, types].append(_item('companion', f'{server_root}/Destination/{companion.name}',
                                               Type=item_type, IndexNumber=2, **group_fields))

    update = repository.update_item
    attempts = []

    def refuse_handover(item_id, connection=None, **values):
        if item_id == before.id and values.get('library_id') == library_id:
            assert connection.in_nested_transaction()
            group = repository.get_group(values['group_id'], connection)
            assert (group.library_id, group.kind) == (library_id, group_kind)
            attempts.append(group.id)
            values['path'] = str(witness)  # Force the database to refuse the write on its unique path constraint.
        return update(item_id, connection, **values)

    monkeypatch.setattr(repository, 'update_item', refuse_handover)
    result = _scan(saved, source_id, library_id)[0]

    assert result['last_scan_status'] == 'ok'
    assert '1 left for a scan' in result['last_scan_message']
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    retained = repository.get_item(before.id, saved.db)
    assert (retained.source_id, retained.library_id, retained.path, retained.group_id) == (
        prior_source, prior_lib, str(incoming), before.group_id)
    assert repository.library_groups(prior_lib, saved.db) == prior_groups
    groups = repository.library_groups(library_id, saved.db)
    rows = _indexed(saved, library_id)
    assert set(rows) == ({'companion'} if group_state == 'new-used' else set())
    if group_state == 'new-empty':
        assert not groups
    else:
        (group,) = groups
        assert group.id == attempts[0]
        if group_state == 'pre-existing':
            assert (group.id, group.title, group.identity_source) == (group_id, 'Existing identity', 'user')
        else:
            assert rows['companion'].group_id == group.id

    monkeypatch.setattr(repository, 'update_item', update)
    assert _scan(saved, source_id, library_id)[0]['last_scan_status'] == 'ok'
    (group,) = repository.library_groups(library_id, saved.db)
    after = _indexed(saved, library_id)['incoming']
    assert (after.id, after.source_id, after.library_id, after.path, after.group_id) == (
        before.id, source_id, library_id, str(target), group.id)
    if groups:
        assert group.id == groups[0].id
