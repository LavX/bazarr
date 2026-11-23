"""Manual source actions through the API, shared writers and real database."""

from datetime import datetime
from io import BytesIO
from pathlib import Path
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

    assert delete(source_blacklist_id=row_id).status_code in (200, 204)
    assert client.get(url, headers=HEADERS).data == arr_before
    assert delete(all='true', **{'source_key[]': f'source:{first.source_id}'}).status_code in (200, 204)
    assert client.get(url + '?sources=1', headers=HEADERS).get_json()['total'] == 2
    assert delete(all='true', **{'source_key[]': f'arr:{arr_id}'}).status_code in (200, 204)
    assert client.get(url + '?sources=1', headers=HEADERS).get_json()['total'] == 1
    arr_library.execute(sa.insert(arr_table).values(**arr_row))
    assert delete(all='true').status_code in (200, 204)
    assert client.get(url, headers=HEADERS).get_json() == {'data': []}
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0


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
    })
    assert response.status_code == 403
    assert other_subtitle.read_bytes() == pipeline.CONTENT
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.Blacklist)) == 0
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
    assert writes == []
