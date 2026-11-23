"""Manual source actions through the API, shared writers and real database."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from media_source_helpers import add_group, add_item, add_source, video
from test_media_sources_no_change import HEADERS
import test_media_sources_no_change as legacy
import test_media_sources_pipeline as pipeline

arr_library = legacy.arr_library
client = legacy.client
everything_uses_the_test_database = legacy.everything_uses_the_test_database
isolated_jobs = pipeline.isolated_jobs


def _item(db, tmp_path, kind, *, folder='source'):
    from media_sources import indexer
    from media_sources.context import resolve_item

    source, library, root = add_source(db, tmp_path, content_kind='series' if kind == 'episode' else kind,
                                      name=folder, folder=folder, profile_id=1)
    group = add_group(db, source, library, title='Bluey', tvdb_id=387380) if kind == 'episode' else None
    path = video(root / ('Bluey.S01E02.mkv' if group else 'Film.2001.mkv'))
    item = add_item(db, source, library, path, kind=kind, group_id=group, title='Mum School' if group else 'Film',
                    season=1 if group else None, episode=2 if group else None)
    indexer.index_item(item)
    return resolve_item(item)


def _record_provider_sidecar(db, source, path, provider, subs_id, *, publication_proof=True):
    from media_sources import repository
    from subtitles.tools.subsync_engines import regular_subtitle_version

    return repository.insert_history(db, item_id=source.item_id, source_id=source.source_id, item_kind=source.kind,
                                     video_path=source.path, subtitles_path=str(path), language='en', action=2,
                                     provider=provider, subs_id=subs_id, description='Fixture acquisition',
                                     published_file_version=list(regular_subtitle_version(path))
                                     if publication_proof else None)


def _history_id(client, url):
    results = client.get(f'{url}/history', headers=HEADERS)
    assert results.status_code == 200
    return results.get_json()['data'][0]['history_id']


@pytest.fixture
def writes(client, arr_library, isolated_jobs, monkeypatch):
    from media_servers import dispatcher
    from media_sources import indexer, manual as source_manual
    from subzero.language import Language
    from subtitles import manual, pool, processing, upload
    from subtitles.tools import delete

    events = []
    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    monkeypatch.setattr('app.notifier.send_notifications_source', Mock())
    monkeypatch.setattr(dispatcher, 'notify_subtitle_mutation', events.append)
    monkeypatch.setattr(indexer, 'get_language_set', lambda: {Language('eng')})
    monkeypatch.setattr(pool, '_update_pool', lambda *args: False)
    monkeypatch.setattr(manual, '_get_pool', lambda *args, **kwargs: object())
    monkeypatch.setattr(manual.subliminal, 'region', Mock())
    monkeypatch.setattr('app.get_providers.get_providers_sorted', lambda **kwargs: ['fake'])
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: False)
    monkeypatch.setattr(processing, 'call_external_webhook', Mock())
    monkeypatch.setattr(upload, 'sync_subtitles', Mock())
    monkeypatch.setattr(delete, 'call_external_webhook', Mock())

    def unexpected(*args, **kwargs):
        pytest.fail('Source action called an Arr-only hook')

    for module, names in ((upload, ('notify_radarr', 'notify_sonarr')), (delete, ('notify_radarr', 'notify_sonarr')),
                          (manual, ('clear_mismatch_after_manual_save',))):
        for name in names:
            monkeypatch.setattr(module, name, unexpected)
    monkeypatch.setattr(manual, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(upload, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(source_manual, 'jobs_queue', isolated_jobs)
    return events


@pytest.mark.parametrize('kind', ['movie', 'episode'])
def test_manual_search_download_and_delete_blacklist(client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch,
                                                     kind):
    from media_sources import repository
    from subtitles import manual, pool

    source = _item(arr_library, tmp_path, kind)
    url = f'/api/sources/items/{source.item_id}'
    candidate = pipeline.FakeSubtitle()
    candidate.matches = {'series', 'season', 'episode'} if kind == 'episode' else {'title', 'year'}
    candidate.page_link = 'https://example.invalid/release'
    candidate.hash_verifiable = True

    def listing(videos, languages, _pool, **kwargs):
        (found,) = videos
        assert found.source_context == source
        return {found: [] if ('fake', candidate.id) in pool._blacklist(source.media_type, source=source)
                else [candidate]}

    def download(subtitles, _pool):
        (subtitle,) = subtitles
        assert subtitle.source_context == source

    monkeypatch.setattr(manual, 'list_all_subtitles', listing)
    monkeypatch.setattr(manual, 'download_subtitles', download)
    search = client.get(f'{url}/manual', headers=HEADERS)
    assert search.status_code == 200
    (choice,) = search.get_json()['data']

    download_response = client.post(f'{url}/manual', headers=HEADERS, json={
        'provider': 'fake', 'subtitle': choice['subtitle'], 'hi': False, 'forced': False, 'original_format': False,
    })
    assert download_response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == download_response.get_json()['job_id']
    assert isolated_jobs._run_job(job), job.error
    saved = Path(source.path).with_suffix('.en.srt')
    assert saved.read_bytes() == pipeline.CONTENT

    response = client.post(f'{url}/blacklist', headers=HEADERS, json={
        'subtitles_path': str(saved), 'language': 'en', 'provider': 'fake', 'subs_id': candidate.id,
        'history_id': _history_id(client, url),
    })
    assert response.status_code == 204
    assert not saved.exists()
    assert client.get(f'{url}/manual', headers=HEADERS).get_json()['data'] == []
    assert ('fake', candidate.id) not in pool._blacklist(source.media_type)
    response = client.post(f'{url}/manual', headers=HEADERS, json={'provider': 'fake', 'subtitle': choice['subtitle']})
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert not isolated_jobs._run_job(job)
    assert 'has been excluded' in job.error['message']
    assert not saved.exists()
    assert 'en' in repository.get_item(source.item_id).missing_subtitles
    (blacklisted,) = arr_library.execute(sa.select(repository.Blacklist)).all()
    assert (blacklisted.item_id, blacklisted.video_path, blacklisted.item_title, blacklisted.source_name) == (
        source.item_id, source.path, source.title, source.source_name)
    actions = arr_library.execute(sa.select(repository.History.c.action).order_by(repository.History.c.id)).scalars()
    assert list(actions) == [2, 0]
    assert [(event.media_type, event.operation, event.arr_instance_id) for event in writes] == [
        (source.publication_type, 'download', None), (source.publication_type, 'delete', None)]


def _search_for_download(client, monkeypatch, source, during_search=None):
    from subtitles import manual

    candidate = pipeline.FakeSubtitle()
    candidate.matches = {'series', 'season', 'episode'} if source.kind == 'episode' else {'title', 'year'}
    candidate.page_link = 'https://example.invalid/release'

    def listing(videos, *args, **kwargs):
        if during_search is not None:
            during_search()
        return {videos[0]: [candidate]}

    monkeypatch.setattr(manual, 'list_all_subtitles', listing)
    response = client.get(f'/api/sources/items/{source.item_id}/manual', headers=HEADERS)
    assert response.status_code == 200
    (choice,) = response.get_json()['data']
    return {'provider': choice['provider'], 'subtitle': choice['subtitle']}, candidate.id


@pytest.mark.parametrize('replacement', ['during_search', 'before_download', 'during_download'])
def test_manual_download_rejects_video_replaced_at_same_path(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, replacement):
    from media_sources import repository, scanner
    from media_sources.context import resolve_item
    from subtitles import manual

    source_id, library_id, root = add_source(arr_library, tmp_path, profile_id=1)
    path = video(root / 'Film.2001.mkv')
    scanner.run_scan(source_id)
    (item,) = repository.list_items(library_id=library_id)
    source = resolve_item(item.id)

    def replace_and_scan():
        video(tmp_path / 'replacement.mkv', size=2000).replace(path)
        scanner.run_scan(source_id)
        (current,) = repository.list_items(library_id=library_id)
        assert (current.id, current.created_at, current.path) == (item.id, item.created_at, item.path)
        assert current.file_size == 2000

    def download(subtitles, _pool):
        assert replacement == 'during_download', 'The stale search result reached the provider'
        replace_and_scan()

    monkeypatch.setattr(manual, 'download_subtitles', download)
    payload, _ = _search_for_download(
        client, monkeypatch, source, replace_and_scan if replacement == 'during_search' else None)
    response = client.post(f'/api/sources/items/{source.item_id}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    if replacement == 'before_download':
        replace_and_scan()

    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert not isolated_jobs._run_job(job)
    assert 'video changed' in job.error['message'].lower()
    assert not path.with_suffix('.en.srt').exists()
    assert not list(root.glob('.bazarr-write-*'))
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
    assert writes == []


def test_manual_write_refuses_symlink_retargeted_since_admission(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch):
    from media_sources import repository, scanner
    from media_sources.context import resolve_item
    from subtitles import manual

    source_id, library_id, root = add_source(arr_library, tmp_path, profile_id=1)
    path = root / 'Film.2001.mkv'
    path.symlink_to(video(tmp_path / 'original.mkv'))
    scanner.run_scan(source_id)
    (admitted,) = repository.list_items(library_id=library_id)
    source = resolve_item(admitted.id)

    path.unlink()
    path.symlink_to(video(tmp_path / 'replacement.mkv'))
    assert admitted.file_ino != str(path.stat().st_ino)
    payload, _ = _search_for_download(client, monkeypatch, source)
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    response = client.post(f'/api/sources/items/{source.item_id}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert not isolated_jobs._run_job(job)
    assert 'the scanned video no longer matches' in job.error['message'].lower()
    assert not path.with_suffix('.en.srt').exists()
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
    assert writes == []


@pytest.mark.parametrize('exclude_succeeds,pause_at', [
    (True, 'provider'), (True, 'write_lock'), (False, 'provider'),
], ids=['excluded-during-provider', 'excluded-before-write-lock', 'unlink-failed'])
def test_exclude_while_manual_provider_download_is_in_flight(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, exclude_succeeds, pause_at):
    from media_sources import indexer, repository
    from subtitles import manual
    from subtitles.tools import delete

    source = _item(arr_library, tmp_path, 'movie')
    sidecar = Path(source.path).with_suffix('.en.srt')
    sidecar.write_bytes(pipeline.CONTENT)
    indexer.index_item(source.item_id)
    url = f'/api/sources/items/{source.item_id}'
    payload, release_id = _search_for_download(client, monkeypatch, source)
    acquired = _record_provider_sidecar(arr_library, source, sidecar, payload['provider'], release_id)
    download_paused, download_may_continue = Event(), Event()
    write_locks = manual.subtitle_write_locks

    def pause_download():
        download_paused.set()
        assert download_may_continue.wait(15), 'Timed out waiting for exclusion'

    def download(subtitles, _pool):
        if pause_at == 'provider':
            pause_download()

    @contextmanager
    def pause_before_write_lock(*paths):
        pause_download()
        with write_locks(*paths) as states:
            yield states

    monkeypatch.setattr(manual, 'download_subtitles', download)
    if pause_at == 'write_lock':
        monkeypatch.setattr(manual, 'subtitle_write_locks', pause_before_write_lock)
    response = client.post(f'{url}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']

    def unlink_fails(path):
        assert path == str(sidecar)
        raise PermissionError('Simulated unlink failure')

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(isolated_jobs._run_job, job)
        try:
            assert download_paused.wait(15), job.error
            with monkeypatch.context() as patch:
                if not exclude_succeeds:
                    patch.setattr(delete.os, 'remove', unlink_fails)
                excluded = client.post(f'{url}/blacklist', headers=HEADERS, json={
                    'subtitles_path': str(sidecar), 'language': 'en', 'provider': payload['provider'],
                    'subs_id': release_id, 'history_id': acquired,
                })
            assert excluded.status_code == (204 if exclude_succeeds else 409)
            assert sidecar.exists() is not exclude_succeeds
        finally:
            download_may_continue.set()
        assert running.result(timeout=15) is not exclude_succeeds, job.error

    exclusions = arr_library.execute(sa.select(repository.Blacklist.c.subs_id)).scalars().all()
    actions = arr_library.execute(sa.select(repository.History.c.action).order_by(repository.History.c.id)).scalars().all()
    assert not list(sidecar.parent.glob('.bazarr-write-*'))
    if exclude_succeeds:
        assert 'has been excluded' in job.error['message']
        assert not sidecar.exists()
        assert exclusions == [release_id]
        assert actions == [2, 0]
        assert [event.operation for event in writes] == ['delete']
        assert 'en' in repository.get_item(source.item_id).missing_subtitles
    else:
        assert sidecar.read_bytes() == pipeline.CONTENT
        assert exclusions == []
        assert actions == [2, 2]
        assert [event.operation for event in writes] == ['download']


@pytest.mark.parametrize('custom_folder', [False, True], ids=['beside-video', 'shared-folder'])
def test_episode_upload_and_plain_delete(client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, custom_folder):
    from app.config import settings
    from media_sources import repository
    from subtitles import upload

    source = _item(arr_library, tmp_path, 'episode')
    destination = tmp_path / 'Subtitles' if custom_folder else Path(source.path).parent
    if custom_folder:
        monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
        monkeypatch.setattr(settings.general, 'subfolder_custom', str(destination))
    url = f'/api/sources/items/{source.item_id}/subtitles'
    response = client.post(url, headers=HEADERS, data={
        'file': (BytesIO(pipeline.CONTENT), 'fixture.srt'), 'language': 'en', 'forced': 'False', 'hi': 'False',
    })
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert isolated_jobs._run_job(job), job.error
    saved = destination / Path(source.path).with_suffix('.en.srt').name
    assert saved.exists()
    assert upload.sync_subtitles.call_args.kwargs['source_version'].owns_destinations
    assert repository.get_item(source.item_id).missing_subtitles == '[]'

    response = client.delete(url, headers=HEADERS, json={'subtitles_path': str(saved), 'language': 'en'})
    assert response.status_code == 204
    assert not saved.exists()
    assert 'en' in repository.get_item(source.item_id).missing_subtitles
    assert client.delete(url, headers=HEADERS, json={'subtitles_path': str(saved), 'language': 'en'}).status_code == 403
    rows = arr_library.execute(sa.select(repository.History).order_by(repository.History.c.id)).all()
    assert [(row.action, row.item_id, row.video_path) for row in rows] == [
        (4, source.item_id, source.path), (0, source.item_id, source.path)]
    assert [(event.media_type, event.operation, event.arr_instance_id) for event in writes] == [
        ('episode', 'upload', None), ('episode', 'delete', None)]
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0


def _scanned_symlink(db, tmp_path, monkeypatch, kind, folder_mode):
    from app.config import settings
    from media_sources import repository, scanner
    from media_sources.context import resolve_item

    monkeypatch.setattr(settings.general, 'subfolder', folder_mode)
    custom = tmp_path / 'Sidecars' if folder_mode == 'absolute' else 'Subs'
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(custom))
    monkeypatch.setattr('utilities.video_analyzer.parse_video_metadata', lambda *args, **kwargs: None)
    source, library, root = add_source(db, tmp_path, content_kind='series' if kind == 'episode' else kind,
                                      profile_id=1)
    target = video(tmp_path / 'physical' / 'Different.Target.mkv')
    logical = root / ('Show/Linked.S01E02.mkv' if kind == 'episode' else 'Linked.2001.mkv')
    logical.parent.mkdir(exist_ok=True)
    logical.symlink_to(target)
    (scan,) = scanner.run_scan(source, library)
    assert scan.inserted == 1
    (item,) = repository.list_items(library_id=library)
    assert item.path == str(logical)
    destination = (logical.parent if folder_mode == 'current' else
                   custom if folder_mode == 'absolute' else logical.parent / custom)
    return resolve_item(item.id), target, destination


@pytest.mark.parametrize('folder_mode', ['current', 'relative', 'absolute'])
@pytest.mark.parametrize('kind', ['movie', 'episode'])
def test_admitted_symlink_supports_download_exclude_upload_and_delete(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, kind, folder_mode):
    from media_sources import repository
    from subtitles import manual

    source, target, destination = _scanned_symlink(arr_library, tmp_path, monkeypatch, kind, folder_mode)
    url = f'/api/sources/items/{source.item_id}'
    candidate = pipeline.FakeSubtitle()
    candidate.matches = {'series', 'season', 'episode'} if kind == 'episode' else {'title', 'year'}
    candidate.page_link = 'https://example.invalid/release'
    candidate.hash_verifiable = True
    monkeypatch.setattr(manual, 'list_all_subtitles', lambda videos, *args, **kwargs: {
        next(iter(videos)): [candidate]})
    monkeypatch.setattr(manual, 'download_subtitles', lambda *args, **kwargs: None)
    response = client.get(f'{url}/manual', headers=HEADERS)
    assert response.status_code == 200
    (choice,) = response.get_json()['data']
    response = client.post(f'{url}/manual', headers=HEADERS, json={
        'provider': 'fake', 'subtitle': choice['subtitle'], 'hi': False, 'forced': False, 'original_format': False,
    })
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert isolated_jobs._run_job(job), job.error
    saved = destination / Path(source.path).with_suffix('.en.srt').name
    assert saved.read_bytes() == pipeline.CONTENT
    response = client.post(f'{url}/blacklist', headers=HEADERS, json={
        'subtitles_path': str(saved), 'language': 'en', 'provider': 'fake', 'subs_id': candidate.id,
        'history_id': _history_id(client, url),
    })
    assert response.status_code == 204, response.get_json()
    assert not saved.exists()

    response = client.post(f'{url}/subtitles', headers=HEADERS, data={
        'file': (BytesIO(pipeline.CONTENT), 'fixture.srt'), 'language': 'en', 'forced': 'False', 'hi': 'False',
    })
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert isolated_jobs._run_job(job), job.error
    assert saved.read_bytes() == pipeline.CONTENT
    response = client.delete(f'{url}/subtitles', headers=HEADERS, json={
        'subtitles_path': str(saved), 'language': 'en',
    })
    assert response.status_code == 204, response.get_json()
    assert not saved.exists()
    assert 'en' in repository.get_item(source.item_id).missing_subtitles
    assert target.read_bytes() == b'\0' * 1000
    assert list(target.parent.iterdir()) == [target]
    assert [(event.operation, event.video_path) for event in writes] == [
        ('download', source.path), ('delete', source.path), ('upload', source.path), ('delete', source.path)]


@pytest.mark.parametrize('attack', [
    'physical-folder', 'target-name', 'sibling-owner', 'source-owner', 'arr-owner', 'same-owner-other-folder',
])
def test_admitted_symlink_refuses_foreign_or_ambiguous_sidecars(
        client, arr_library, writes, tmp_path, monkeypatch, attack):
    from app.database import TableMovies
    from media_sources import repository

    folder_mode = 'absolute' if attack in {'source-owner', 'arr-owner'} else 'current'
    source, target, directory = _scanned_symlink(arr_library, tmp_path, monkeypatch, 'movie', folder_mode)
    directory.mkdir(exist_ok=True)
    logical = Path(source.path)
    saved = directory / logical.with_suffix('.en.srt').name
    if attack == 'physical-folder':
        saved = target.parent / saved.name
    elif attack == 'target-name':
        saved = directory / target.with_suffix('.en.srt').name
    elif attack == 'same-owner-other-folder':
        alias = tmp_path / 'Other' / logical.name
        alias.parent.mkdir()
        alias.symlink_to(target)
        arr_library.execute(sa.insert(TableMovies).values(
            id=2, arr_instance_id=2, radarrId=22, path=str(alias), title='Other', tmdbId='8765'))
        saved = alias.parent / saved.name
    else:
        foreign = video(tmp_path / 'other-physical' / 'Other.Target.mkv')
        alias = logical.with_suffix('.en.mkv') if attack == 'sibling-owner' else tmp_path / 'Other' / logical.name
        alias.parent.mkdir(exist_ok=True)
        alias.symlink_to(foreign)
        if attack == 'source-owner':
            add_item(arr_library, source.source_id, source.library_id, alias)
        elif attack == 'arr-owner':
            arr_library.execute(sa.insert(TableMovies).values(
                id=2, arr_instance_id=2, radarrId=22, path=str(alias), title='Other', tmdbId='8765'))
    saved.write_bytes(pipeline.CONTENT)
    # Even a stale or forged cached listing cannot authorize a different folder or owner.
    repository.update_item(source.item_id, arr_library, subtitles=repr([['en', str(saved), len(pipeline.CONTENT)]]))
    response = client.delete(f'/api/sources/items/{source.item_id}/subtitles', headers=HEADERS, json={
        'subtitles_path': str(saved), 'language': 'en',
    })
    assert response.status_code == 409
    assert saved.read_bytes() == pipeline.CONTENT
    assert writes == []
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0


@pytest.mark.parametrize('kind,segment,arr_id', [('movie', 'movies', 2), ('episode', 'episodes', 1)])
def test_blacklist_paging_and_delete_keep_origin_and_snapshots(
        client, arr_library, tmp_path, monkeypatch, kind, segment, arr_id):
    from app.database import TableBlacklist, TableBlacklistMovie
    from media_sources import repository
    from media_sources.history import blacklist_log_source

    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    arr_table = (TableBlacklistMovie if kind == 'movie' else TableBlacklist).__table__
    arr_row = arr_library.execute(sa.select(arr_table)).mappings().one()
    url = f'/api/{segment}/blacklist'
    arr_before = client.get(url, headers=HEADERS).data
    first = _item(arr_library, tmp_path, kind, folder='first')
    second = _item(arr_library, tmp_path, kind, folder='second')
    row_id = blacklist_log_source(first, 'alpha', 'm1' if kind == 'movie' else 'a1', 'en')
    other_id = blacklist_log_source(second, 'alpha', 'other', 'en')
    arr_library.execute(sa.update(repository.Blacklist).values(timestamp=datetime(2027, 1, 1)))
    arr_library.execute(sa.delete(repository.Items).where(repository.Items.c.id == second.item_id))

    assert client.get(url, headers=HEADERS).data == arr_before
    rows = []
    for offset in range(3):
        response = client.get(url, headers=HEADERS, query_string={'sources': 1, 'start': offset, 'length': 1})
        assert response.status_code == 200
        page = response.get_json()
        assert page['total'] == 3
        rows.extend(page['data'])
    assert [(row['origin'], row['source_key']) for row in rows] == [
        ('source', f'source:{second.source_id}'), ('source', f'source:{first.source_id}'), ('arr', f'arr:{arr_id}')]
    assert rows[0]['id'] == other_id
    assert rows[0]['source_item_id'] is None
    assert rows[0]['video_path'] == second.path
    assert rows[0]['title' if kind == 'movie' else 'episodeTitle'] == second.title
    assert rows[0]['source_name'] == 'second'
    if kind == 'episode':
        assert rows[0]['seriesTitle'] == 'Bluey'
        assert rows[0]['source_group_id'] is None
        assert rows[1]['episode_number'] == '1x2'
        assert rows[1]['source_group_id'] == first.group_id

    def delete(**params):
        return client.delete(url, headers=HEADERS, query_string={'sources': 1, **params})

    assert delete(source_blacklist_id=row_id, source_blacklist_key=rows[1]['removal_key']).status_code in (200, 204)
    assert client.get(url, headers=HEADERS).data == arr_before
    assert delete(all='true', **{'source_key[]': f'source:{first.source_id}'}).status_code in (200, 204)
    assert client.get(url + '?sources=1', headers=HEADERS).get_json()['total'] == 2
    assert delete(all='true', **{'source_key[]': f'arr:{arr_id}'}).status_code in (200, 204)
    assert client.get(url + '?sources=1', headers=HEADERS).get_json()['total'] == 1
    arr_library.execute(sa.insert(arr_table).values(**arr_row))
    assert delete(all='true').status_code in (200, 204)
    assert client.get(url, headers=HEADERS).get_json() == {'data': []}
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0


@pytest.mark.parametrize('kind,segment,status', [('movie', 'movies', 200), ('episode', 'episodes', 204)])
@pytest.mark.parametrize('same_release', [False, True])
def test_stale_blacklist_removal_cannot_remove_a_reused_sqlite_id(
        client, arr_library, tmp_path, monkeypatch, kind, segment, status, same_release):
    from media_sources import repository
    from media_sources.history import blacklist_log_source

    if arr_library.get_bind().dialect.name != 'sqlite':
        pytest.skip('SQLite reuses a deleted highest INTEGER PRIMARY KEY')
    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    source = _item(arr_library, tmp_path, kind)
    row_id = blacklist_log_source(source, 'alpha', 'original', 'en')
    original = dict(arr_library.execute(sa.select(repository.Blacklist)).mappings().one())
    url = f'/api/{segment}/blacklist'
    arr_before = client.get(url, headers=HEADERS).data

    def listed():
        return next(row for row in client.get(url, headers=HEADERS, query_string={'sources': 1})
                    .get_json()['data'] if row.get('origin') == 'source')

    def delete(row):
        params = {'sources': 1, 'source_blacklist_id': row['id']}
        if row.get('removal_key'):
            params['source_blacklist_key'] = row['removal_key']
        return client.delete(url, headers=HEADERS, query_string=params)

    stale = listed()
    assert client.delete(url, headers=HEADERS, query_string={
        'sources': 1, 'source_blacklist_id': row_id}).status_code == 400
    assert client.delete(url, headers=HEADERS, query_string={
        'sources': 1, 'source_blacklist_key': stale['removal_key']}).status_code == 400
    other_url = '/api/episodes/blacklist' if kind == 'movie' else '/api/movies/blacklist'
    other_arr_before = client.get(other_url, headers=HEADERS).data
    assert client.delete(other_url, headers=HEADERS, query_string={
        'sources': 1, 'source_blacklist_id': row_id,
        'source_blacklist_key': stale['removal_key']}).status_code == 409
    assert client.get(other_url, headers=HEADERS).data == other_arr_before
    assert delete(stale).status_code == status
    # Keep every snapshot, including the original timestamp, on an exact re-add.
    # The numerical ID must actually be reused, rather than simulated by a mock.
    original.pop('id')
    original.pop('removal_key', None)
    if not same_release:
        original['subs_id'] = 'newer'
    replacement_id = repository.insert_blacklist(arr_library, **original)
    assert replacement_id == row_id
    current = listed()
    assert delete(stale).status_code == 409
    assert arr_library.scalar(sa.select(repository.Blacklist.c.subs_id)) == original['subs_id']
    assert client.get(url, headers=HEADERS).data == arr_before
    assert delete(current).status_code == status
    assert client.get(url, headers=HEADERS).data == arr_before


@pytest.mark.parametrize('kind,segment', [('movie', 'movies'), ('episode', 'episodes')])
def test_retained_blacklist_filters_cover_every_owner_outside_the_requested_page(
        client, arr_library, tmp_path, monkeypatch, kind, segment):
    from media_sources import repository

    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    owners = []
    for name in ('Live', 'No library', 'Deleted'):
        source_id, library_id, _ = add_source(arr_library, tmp_path, name=name, folder=name,
                                               content_kind='series' if kind == 'episode' else 'movie')
        row_id = repository.insert_blacklist(arr_library, source_id=source_id, item_kind=kind,
                                             source_name=name, provider='alpha', subs_id=name)
        owners.append((source_id, library_id, row_id))
    repository.delete_library(owners[1][1], arr_library)
    repository.delete_source(owners[2][0], arr_library)
    url = f'/api/{segment}/blacklist'
    response = client.get(url, headers=HEADERS, query_string={
        'sources': 1, 'source_key[]': f'source:{owners[0][0]}', 'length': 1})
    assert response.status_code == 200
    page = response.get_json()
    assert page['total'] == 1
    assert [row['source_key'] for row in page['data']] == [f'source:{owners[0][0]}']
    expected = [{'key': f'source:{owners[0][0]}', 'name': 'Live'},
                {'key': f'source:{owners[1][0]}', 'name': 'No library'},
                {'key': 'source:removed', 'name': 'Removed sources'}]
    assert page['source_filters'] == expected
    assert client.delete(url, headers=HEADERS, query_string={
        'sources': 1, 'all': 'true', 'source_key[]': 'source:removed'}).status_code in (200, 204)
    refreshed = client.get(url, headers=HEADERS, query_string={'sources': 1, 'length': 1}).get_json()
    assert refreshed['source_filters'] == expected[:-1]


def test_source_manual_search_job_binds_the_result_to_its_item(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch):
    from subtitles import manual

    source = _item(arr_library, tmp_path, 'movie')
    other = _item(arr_library, tmp_path, 'movie', folder='other')
    candidate = pipeline.FakeSubtitle()
    candidate.matches = {'title', 'year'}
    candidate.page_link = 'https://example.invalid/release'

    def listing(videos, languages, _pool, **kwargs):
        (found,) = videos
        assert found.source_context == source
        return {found: [candidate]}

    def unexpected_download(*args, **kwargs):
        pytest.fail('A result from another source item reached the provider')

    monkeypatch.setattr(manual, 'Thread', Mock())
    monkeypatch.setattr(manual, 'list_all_subtitles', listing)
    monkeypatch.setattr(manual, 'download_subtitles', unexpected_download)
    response = client.post(f'/api/sources/items/{source.item_id}/manual/search', headers=HEADERS)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert isolated_jobs._run_job(job), job.error
    (choice,) = isolated_jobs.get_job_result(job.job_id)['result']['data']

    response = client.post(f'/api/sources/items/{other.item_id}/manual', headers=HEADERS, json={
        'provider': 'fake', 'subtitle': choice['subtitle'],
    })
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert not isolated_jobs._run_job(job)
    assert 'does not match the selected item' in job.error['message']
    assert not Path(other.path).with_suffix('.en.srt').exists()


def test_blacklist_cannot_delete_a_different_items_subtitle(client, arr_library, writes, tmp_path):
    from media_sources import indexer, repository

    source = _item(arr_library, tmp_path, 'movie')
    other = _item(arr_library, tmp_path, 'movie', folder='other')
    other_subtitle = Path(other.path).with_suffix('.en.srt')
    other_subtitle.write_bytes(pipeline.CONTENT)
    indexer.index_item(other.item_id)

    response = client.post(f'/api/sources/items/{source.item_id}/blacklist', headers=HEADERS, json={
        'subtitles_path': str(other_subtitle), 'language': 'en', 'provider': 'fake', 'subs_id': 'sub-1',
        'history_id': 1,
    })
    assert response.status_code == 403
    assert other_subtitle.read_bytes() == pipeline.CONTENT
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
    assert writes == []


@pytest.mark.parametrize(('kind', 'pending_history'), [('movie', False), ('episode', True)])
def test_exclude_protects_the_replacement_of_a_selected_provider_subtitle(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, kind, pending_history):
    from media_sources import indexer, repository
    from subtitles import upload

    source = _item(arr_library, tmp_path, kind)
    other = _item(arr_library, tmp_path, 'movie', folder='Different item')
    subtitle = Path(source.path).with_suffix('.en.srt')
    subtitle.write_bytes(pipeline.CONTENT)
    other_subtitle = Path(other.path).with_suffix('.en.srt')
    other_subtitle.write_bytes(pipeline.CONTENT)
    indexer.index_item(source.item_id)
    acquired = _record_provider_sidecar(arr_library, source, subtitle, 'fixture', 'ours')
    foreign = _record_provider_sidecar(arr_library, other, other_subtitle, 'fixture', 'theirs')
    url = f'/api/sources/items/{source.item_id}'
    assert acquired == _history_id(client, url)
    evidence = {'subtitles_path': str(subtitle), 'language': 'en', 'provider': 'fixture', 'subs_id': 'ours',
                'history_id': acquired}
    for wrong_evidence in ({'history_id': foreign}, {'provider': 'wrong'}, {'subs_id': 'theirs'}):
        assert client.post(f'{url}/blacklist', headers=HEADERS, json=evidence | wrong_evidence).status_code == 409
        assert subtitle.read_bytes() == pipeline.CONTENT

    new_subtitle = pipeline.CONTENT.replace(b'Downloaded', b'Uploaded replacement')
    response = client.post(f'{url}/subtitles', headers=HEADERS, data={
        'file': (BytesIO(new_subtitle), 'edition.srt'), 'language': 'en',
    })
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()

    if pending_history:
        write_finished, record_history = Event(), Event()
        finish = upload._finish_source_upload

        def pause_before_history(*args, **kwargs):
            write_finished.set()
            assert record_history.wait(10)
            return finish(*args, **kwargs)

        monkeypatch.setattr(upload, '_finish_source_upload', pause_before_history)
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(isolated_jobs._run_job, job)
            try:
                assert write_finished.wait(10), job.error
                assert subtitle.read_bytes() == new_subtitle
                assert acquired == _history_id(client, url)
                response = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence)
            finally:
                record_history.set()
            assert running.result(timeout=10), job.error
    else:
        assert isolated_jobs._run_job(job), job.error
        response = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence)

    assert response.status_code == 409
    assert subtitle.read_bytes() == new_subtitle
    assert other_subtitle.read_bytes() == pipeline.CONTENT
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0
    assert not any(event.operation == 'delete' for event in writes)


@pytest.mark.parametrize('same_release', [True, False], ids=['same-release', 'different-release'])
def test_postgres_release_exclusion_orders_writes_across_different_items(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, same_release):
    from time import monotonic, sleep

    from media_sources import indexer, manual as source_manual, repository
    from subtitles import manual

    if arr_library.get_bind().dialect.name != 'postgresql':
        pytest.skip('SQLite serializes source write transactions itself')
    downloading = _item(arr_library, tmp_path, 'movie', folder='downloads')
    excluding = _item(arr_library, tmp_path, 'movie', folder='exclusions')
    download_path = Path(downloading.path).with_suffix('.en.srt')
    exclude_path = Path(excluding.path).with_suffix('.en.srt')
    exclude_path.write_bytes(pipeline.CONTENT)
    indexer.index_item(excluding.item_id)

    payload, release = _search_for_download(client, monkeypatch, downloading)
    excluded_release = release if same_release else f'{release}-other'
    acquired = _record_provider_sidecar(arr_library, excluding, exclude_path, payload['provider'], excluded_release)
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    response = client.post(f'/api/sources/items/{downloading.item_id}/manual', headers=HEADERS, json=payload)
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']

    after_check, allow_write = Event(), Event()
    publisher_pid = []
    real_blacklist = source_manual.get_blacklist_source

    def after_blacklist_read(kind, target=None):
        result = real_blacklist(kind, target)
        if isinstance(target, sa.engine.Connection):
            # Hold the real transaction after its blacklist read, just before the atomic file replacement.
            publisher_pid.append(target.scalar(sa.text('SELECT pg_backend_pid()')))
            after_check.set()
            assert allow_write.wait(15), 'Timed out waiting for the exclusion transaction'
        return result

    monkeypatch.setattr(source_manual, 'get_blacklist_source', after_blacklist_read)
    exclusion_client = client.application.test_client()

    def exclude():
        return exclusion_client.post(f'/api/sources/items/{excluding.item_id}/blacklist', headers=HEADERS, json={
            'subtitles_path': str(exclude_path), 'language': 'en', 'provider': payload['provider'],
            'subs_id': excluded_release, 'history_id': acquired,
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        writing = executor.submit(isolated_jobs._run_job, job)
        try:
            assert after_check.wait(15), job.error
            removing = executor.submit(exclude)
            if same_release:
                until = monotonic() + 8
                waiting = sa.text("""SELECT EXISTS (
                    SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted
                    AND :publisher = ANY(pg_blocking_pids(pid)))""")
                while not removing.done() and monotonic() < until:
                    if arr_library.scalar(waiting, {'publisher': publisher_pid[0]}):
                        break
                    sleep(0.02)
                assert not removing.done(), 'The other item committed the exclusion after the final save check'
                assert arr_library.scalar(waiting, {'publisher': publisher_pid[0]})
                assert exclude_path.exists()
            else:
                assert removing.result(timeout=8).status_code == 204
                assert not download_path.exists()
        finally:
            allow_write.set()
        assert writing.result(timeout=15), job.error
        assert removing.result(timeout=15).status_code == 204

    assert download_path.read_bytes() == pipeline.CONTENT
    assert not exclude_path.exists()
    row = arr_library.execute(sa.select(repository.Blacklist)).one()
    assert (row.item_id, row.subs_id) == (excluding.item_id, excluded_release)
    assert [event.operation for event in writes] == (['download', 'delete'] if same_release else ['delete', 'download'])


@pytest.mark.parametrize('kind', ['movie', 'episode'])
def test_provider_history_recorded_after_a_source_editor_save_cannot_claim_the_edited_file(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, kind):
    from media_sources import repository
    from media_sources.subtitle_tools import SOURCE_SUBTITLE_EDITED_ACTION
    from subtitles import manual
    from subtitles.tools.subsync_engines import regular_subtitle_version

    source = _item(arr_library, tmp_path, kind)
    payload, release = _search_for_download(client, monkeypatch, source)
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    subtitle = Path(source.path).with_suffix('.en.srt')
    edited = pipeline.CONTENT.replace(b'Downloaded', b'Editor saved this cue')
    url = f'/api/sources/items/{source.item_id}'
    download = manual.manual_download_subtitle
    editor_saves = []
    provider_versions = []

    def edit_after_provider_publication(*args, **kwargs):
        result = download(*args, **kwargs)
        assert result is not None
        assert subtitle.read_bytes() == pipeline.CONTENT
        provider_versions.append(list(regular_subtitle_version(subtitle)))
        assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
        opened = client.get(f'{url}/subtitles/en/content', headers=HEADERS)
        assert opened.status_code == 200 and opened.json['content'] == pipeline.CONTENT.decode()
        saved = client.put(f'{url}/subtitles/en/content',
                           headers={**HEADERS, 'If-Match': opened.headers['ETag']},
                           json={'content': edited.decode()})
        assert saved.status_code == 204
        editor_saves.append(saved.status_code)
        return result

    monkeypatch.setattr(manual, 'manual_download_subtitle', edit_after_provider_publication)
    response = client.post(f'{url}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.json['job_id']
    assert isolated_jobs._run_job(job), job.error
    assert editor_saves == [204]
    rows = arr_library.execute(sa.select(repository.History).order_by(repository.History.c.id)).all()
    assert [row.action for row in rows] == [SOURCE_SUBTITLE_EDITED_ACTION, 2]
    acquired = rows[-1]
    assert acquired.timestamp >= rows[0].timestamp
    assert acquired.published_file_version == provider_versions[0]
    assert acquired.published_file_version != list(regular_subtitle_version(subtitle))
    assert subtitle.read_bytes() == edited
    evidence = {'subtitles_path': str(subtitle), 'language': 'en', 'provider': payload['provider'],
                'subs_id': release, 'history_id': acquired.id}

    refused = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence)
    assert refused.status_code == 409, refused.get_data(as_text=True)
    assert refused.json['code'] == 'source_release_changed'
    assert subtitle.read_bytes() == edited
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0
    assert not any(event.operation == 'delete' for event in writes)

    excluded = client.post(f'{url}/blacklist', headers=HEADERS, json={
        'delete_file': False, 'provider': payload['provider'], 'subs_id': release, 'history_id': acquired.id,
    })
    assert excluded.status_code == 204
    assert subtitle.read_bytes() == edited
    (blacklisted,) = arr_library.execute(sa.select(repository.Blacklist)).all()
    assert (blacklisted.item_id, blacklisted.provider, blacklisted.subs_id) == (
        source.item_id, payload['provider'], release)
    assert [row.action for row in arr_library.execute(
        sa.select(repository.History).order_by(repository.History.c.id))] == [SOURCE_SUBTITLE_EDITED_ACTION, 2]
    assert not any(event.operation == 'delete' for event in writes)


def _inline_source_sync(monkeypatch, source, jobs, content):
    from app.config import settings
    from subtitles import processing, sync
    from subtitles.tools import subsyncer

    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: True)
    monkeypatch.setattr(settings.general, 'use_postprocessing', False)
    monkeypatch.setattr(settings.subsync, 'use_subsync', True)
    monkeypatch.setattr(settings.subsync, 'use_subsync_threshold', False)
    monkeypatch.setattr(settings.subsync, 'use_subsync_movie_threshold', False)
    monkeypatch.setattr(settings.subsync, 'enabled_engines', ['alass'])
    monkeypatch.setattr(settings.subsync, 'output_mode', 'overwrite')
    monkeypatch.setattr(settings.subsync, 'debug', False)
    monkeypatch.setattr(sync, 'jobs_queue', jobs)
    calls = []

    def align(self, engine, output_path, video_path, no_fix_framerate):
        assert video_path == source.path
        calls.append((engine, Path(self.srtin).read_bytes()))
        output_path.write_bytes(content)
        return {'offset_seconds': 0.25, 'framerate_scale_factor': 1.0, 'quality_of_fit': 0.9}

    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
    return calls


@pytest.mark.parametrize('kind', ['movie', 'episode'])
def test_manual_download_inline_sync_records_its_publication_for_delete_and_exclude(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, kind):
    from media_sources import repository
    from subtitles import manual
    from subtitles.tools.subsync_engines import regular_subtitle_version

    source = _item(arr_library, tmp_path, kind)
    subtitle = Path(source.path).with_suffix('.en.srt')
    synced = pipeline.CONTENT.replace(b'Downloaded', b'Synchronized acquisition')
    engines = _inline_source_sync(monkeypatch, source, isolated_jobs, synced)
    payload, release = _search_for_download(client, monkeypatch, source)
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    url = f'/api/sources/items/{source.item_id}'

    response = client.post(f'{url}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.json['job_id']
    assert isolated_jobs._run_job(job), job.error
    assert engines == [('alass', pipeline.CONTENT)]
    assert subtitle.read_bytes() == synced
    (acquired,) = arr_library.execute(sa.select(repository.History).where(repository.History.c.action == 2)).all()
    assert acquired.published_file_version == list(regular_subtitle_version(subtitle))

    excluded = client.post(f'{url}/blacklist', headers=HEADERS, json={
        'subtitles_path': str(subtitle), 'language': 'en', 'provider': payload['provider'],
        'subs_id': release, 'history_id': acquired.id,
    })
    assert excluded.status_code == 204, excluded.get_data(as_text=True)
    assert not subtitle.exists()
    (blacklisted,) = arr_library.execute(sa.select(repository.Blacklist)).all()
    assert (blacklisted.item_id, blacklisted.provider, blacklisted.subs_id) == (
        source.item_id, payload['provider'], release)
    assert [event.operation for event in writes].count('delete') == 1


@pytest.mark.parametrize('kind', ['movie', 'episode'])
@pytest.mark.parametrize('replacement', ['before_processing', 'after_sync'])
def test_editor_save_between_manual_publication_and_inline_processing_keeps_user_bytes(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, kind, replacement):
    from media_sources import indexer, repository
    from media_sources.subtitle_tools import SOURCE_SUBTITLE_EDITED_ACTION
    from subtitles import manual, sync
    from subtitles.tools.subsync_engines import regular_subtitle_version

    source = _item(arr_library, tmp_path, kind)
    subtitle = Path(source.path).with_suffix('.en.srt')
    synced = pipeline.CONTENT.replace(b'Downloaded', b'Synchronized acquisition')
    edited = pipeline.CONTENT.replace(b'Downloaded', b'User saved this cue while acquiring')
    engines = _inline_source_sync(monkeypatch, source, isolated_jobs, synced)
    payload, release = _search_for_download(client, monkeypatch, source)
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    url = f'/api/sources/items/{source.item_id}'
    process = manual.process_subtitle
    synchronize = sync.sync_subtitles
    published_versions = []

    def edit_published_subtitle(expected):
        assert subtitle.read_bytes() == expected
        published_versions.append(list(regular_subtitle_version(subtitle)))
        indexer.index_item(source.item_id)
        opened = client.get(f'{url}/subtitles/en/content', headers=HEADERS)
        assert opened.status_code == 200 and opened.json['content'] == expected.decode()
        saved = client.put(f'{url}/subtitles/en/content',
                           headers={**HEADERS, 'If-Match': opened.headers['ETag']},
                           json={'content': edited.decode()})
        assert saved.status_code == 204
        assert subtitle.read_bytes() == edited

    def edit_before_processing(*args, **kwargs):
        assert subtitle.read_bytes() == pipeline.CONTENT
        assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
        if replacement == 'before_processing':
            edit_published_subtitle(pipeline.CONTENT)
        return process(*args, **kwargs)

    def edit_after_sync(*args, **kwargs):
        result = synchronize(*args, **kwargs)
        assert result is True
        edit_published_subtitle(synced)
        return result

    monkeypatch.setattr(manual, 'process_subtitle', edit_before_processing)
    if replacement == 'after_sync':
        monkeypatch.setattr(sync, 'sync_subtitles', edit_after_sync)
    response = client.post(f'{url}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.json['job_id']
    assert isolated_jobs._run_job(job), job.error
    assert len(published_versions) == 1
    assert engines == ([('alass', pipeline.CONTENT)] if replacement == 'after_sync' else [])
    assert subtitle.read_bytes() == edited
    rows = arr_library.execute(sa.select(repository.History).order_by(repository.History.c.id)).all()
    assert [row.action for row in rows] == ([5] if replacement == 'after_sync' else []) + [
        SOURCE_SUBTITLE_EDITED_ACTION, 2]
    acquired = rows[-1]
    assert acquired.published_file_version == published_versions[0]
    assert acquired.published_file_version != list(regular_subtitle_version(subtitle))
    evidence = {'subtitles_path': str(subtitle), 'language': 'en', 'provider': payload['provider'],
                'subs_id': release, 'history_id': acquired.id}
    refused = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence)
    assert refused.status_code == 409
    assert refused.json['code'] == 'source_release_changed'
    assert subtitle.read_bytes() == edited
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0
    assert not any(event.operation == 'delete' for event in writes)

    excluded = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence | {'delete_file': False})
    assert excluded.status_code == 204
    assert subtitle.read_bytes() == edited
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 1
    assert not any(event.operation == 'delete' for event in writes)


@pytest.mark.parametrize('changes_bytes', [False, True], ids=['unchanged-bytes', 'changed-bytes'])
def test_manual_download_custom_command_retains_exclusion_without_claiming_file_ownership(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch, changes_bytes):
    from app.config import settings
    from media_sources import repository
    from subtitles import manual, post_processing

    source = _item(arr_library, tmp_path, 'movie')
    subtitle = Path(source.path).with_suffix('.en.srt')
    processed = pipeline.CONTENT.replace(b'Downloaded', b'Custom command output') if changes_bytes else pipeline.CONTENT
    payload, release = _search_for_download(client, monkeypatch, source)
    monkeypatch.setattr(manual, 'download_subtitles', lambda subtitles, pool: None)
    monkeypatch.setattr(settings.general, 'use_postprocessing', True)
    monkeypatch.setattr(settings.general, 'postprocessing_cmd', 'fixture-command {{subtitles}}')
    monkeypatch.setattr(settings.general, 'use_postprocessing_threshold_movie', False)
    monkeypatch.setattr(settings.general, 'chmod_enabled', True)
    monkeypatch.setattr(settings.general, 'chmod', '0640')
    commands = []

    def command(command, path):
        assert path == source.path
        assert command == ['fixture-command', str(subtitle)]
        assert subtitle.read_bytes() == pipeline.CONTENT
        commands.append(command)
        if changes_bytes:
            subtitle.write_bytes(processed)

    monkeypatch.setattr(post_processing, '_postprocessing_locked', command)
    url = f'/api/sources/items/{source.item_id}'
    response = client.post(f'{url}/manual', headers=HEADERS, json=payload)
    assert response.status_code == 202
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.json['job_id']
    assert isolated_jobs._run_job(job), job.error
    assert len(commands) == 1 and commands[0][0] == 'fixture-command'
    assert subtitle.read_bytes() == processed
    (acquired,) = arr_library.execute(sa.select(repository.History)).all()
    assert acquired.published_file_version is None
    evidence = {'subtitles_path': str(subtitle), 'language': 'en', 'provider': payload['provider'],
                'subs_id': release, 'history_id': acquired.id}
    refused = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence)
    assert refused.status_code == 409
    assert refused.json['code'] == 'source_release_changed'
    assert subtitle.read_bytes() == processed

    excluded = client.post(f'{url}/blacklist', headers=HEADERS, json=evidence | {'delete_file': False})
    assert excluded.status_code == 204
    assert subtitle.read_bytes() == processed
    (blacklisted,) = arr_library.execute(sa.select(repository.Blacklist)).all()
    assert (blacklisted.item_id, blacklisted.provider, blacklisted.subs_id) == (
        source.item_id, payload['provider'], release)
    assert not any(event.operation == 'delete' for event in writes)


def test_legacy_release_exclusion_requires_matching_history_and_keeps_the_current_file(
        client, arr_library, writes, tmp_path):
    from media_sources import indexer, repository

    source = _item(arr_library, tmp_path, 'movie')
    other = _item(arr_library, tmp_path, 'movie', folder='Different item')
    subtitle = Path(source.path).with_suffix('.en.srt')
    other_subtitle = Path(other.path).with_suffix('.en.srt')
    subtitle.write_bytes(pipeline.CONTENT)
    other_subtitle.write_bytes(pipeline.CONTENT)
    indexer.index_item(source.item_id)
    acquired = _record_provider_sidecar(arr_library, source, subtitle, 'fixture', 'ours', publication_proof=False)
    foreign = _record_provider_sidecar(arr_library, other, other_subtitle, 'fixture', 'theirs')
    url = f'/api/sources/items/{source.item_id}/blacklist'
    evidence = {'subtitles_path': str(subtitle), 'language': 'en', 'provider': 'fixture', 'subs_id': 'ours',
                'history_id': acquired}

    refused = client.post(url, headers=HEADERS, json=evidence)
    assert refused.status_code == 409
    assert refused.json['code'] == 'source_release_changed'
    assert subtitle.read_bytes() == pipeline.CONTENT
    for changed in ({'history_id': foreign}, {'provider': 'wrong'}, {'subs_id': 'theirs'}):
        rejected = client.post(url, headers=HEADERS, json=evidence | {'delete_file': False} | changed)
        assert rejected.status_code == 409
        assert subtitle.read_bytes() == pipeline.CONTENT
        assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0

    excluded = client.post(url, headers=HEADERS, json=evidence | {'delete_file': False, 'language': 'de'})
    assert excluded.status_code == 204
    assert subtitle.read_bytes() == other_subtitle.read_bytes() == pipeline.CONTENT
    (blacklisted,) = arr_library.execute(sa.select(repository.Blacklist)).all()
    assert (blacklisted.item_id, blacklisted.provider, blacklisted.subs_id, blacklisted.language) == (
        source.item_id, 'fixture', 'ours', 'en')
    repeated = client.post(url, headers=HEADERS, json=evidence | {'delete_file': False})
    assert repeated.status_code == 204
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 1
    assert subtitle.read_bytes() == pipeline.CONTENT
    assert list(arr_library.scalars(sa.select(repository.History.c.action))) == [2, 2]
    assert not any(event.operation == 'delete' for event in writes)
