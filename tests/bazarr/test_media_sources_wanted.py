"""Exercise the public Wanted entry points on SQLite and PostgreSQL."""

import ast
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from media_source_helpers import add_group, add_item, add_profile, add_source, video
import test_media_sources_no_change as legacy
from test_media_sources_no_change import HEADERS
import test_media_sources_pipeline as pipeline

arr_library = legacy.arr_library
client = legacy.client
everything_uses_the_test_database = legacy.everything_uses_the_test_database
isolated_jobs = pipeline.isolated_jobs
world = pipeline.world


@pytest.mark.parametrize('kind,endpoint', [('movie', 'movies'), ('episode', 'episodes')])
def test_mixed_wanted_orders_pages_and_filters_without_colliding_ids(
        client, arr_library, tmp_path, kind, endpoint):
    from app.database import TableEpisodes, TableMovies, TableReleaseTypeMismatch, TableShows

    source, library, root = add_source(
        arr_library, tmp_path, content_kind='movie' if kind == 'movie' else 'series', profile_id=1)
    group = add_group(arr_library, source, library, title='Tied') if kind == 'episode' else None
    item = add_item(arr_library, source, library, root / 'Tied.mkv', kind=kind, group_id=group, title='Tied',
                    season=1 if group else None, episode=1 if group else None, missing_subtitles="['en']")
    item2 = add_item(arr_library, source, library, root / 'Other.mkv', kind=kind, group_id=group, title='Tied',
                     season=1 if group else None, episode=1 if group else None, missing_subtitles="['en:hi']")
    table = TableMovies if kind == 'movie' else TableShows
    arr_library.execute(sa.update(table).values(title='Tied'))
    # A local Arr row and a source row share their numeric ID.
    arr_id = arr_library.execute(sa.select((TableMovies if kind == 'movie' else TableEpisodes).id)).scalar_one()
    assert arr_id == item
    owner = 2 if kind == 'movie' else 1
    arr_library.execute(sa.insert(TableReleaseTypeMismatch).values(
        media_type='movie' if kind == 'movie' else 'series', media_id=arr_id, arr_instance_id=owner,
        language='en', video_release_type='WEB', subtitle_release_type='BluRay'))

    def get(*params):
        return client.get(f'/api/{endpoint}/wanted', query_string=params, headers=HEADERS)

    rows = []
    for start in (0, 1, 2):
        data = get(('sources', '1'), ('start', start), ('length', '1')).get_json()
        assert data['total'] == 3
        assert len(data['data']) == 1
        rows += data['data']
    assert [(row['origin'], row['id'], row['source_key']) for row in rows] == [
        ('arr', arr_id, f'arr:{owner}'), ('source', item, f'source:{source}'), ('source', item2, f'source:{source}')]
    assert [row['release_mismatch'] for row in rows] == [True, False, False]
    assert rows[1]['arr_instance_id'] is None
    assert rows[1]['identity_state'] == 'parsed'
    assert rows[2]['missing_subtitles'][0]['hi'] is True
    assert get(('sources', 1), ('source_key[]', f'source:{source}')).get_json()['total'] == 2
    assert get(('sources', 1), ('source_key[]', f'source:{source}'),
               ('source_key[]', f'arr:{owner}')).get_json()['total'] == 3
    assert get(('source_key[]', f'source:{source}')).get_json()['total'] == 0
    assert get(('source_key[]', 'source:removed')).get_json()['total'] == 0
    assert get(('source_key[]', 'source:-9')).status_code == 400
    assert get(('source_key[]', f'arr:{owner}')).data == get().data


def test_wanted_job_and_badges_use_the_same_active_items(client, arr_library, tmp_path, monkeypatch):
    from media_sources import repository
    from media_sources import wanted
    from subtitles import download
    from app.config import settings

    source, library, root = add_source(arr_library, tmp_path, profile_id=add_profile(
        arr_library, [('en', 'False', 'False'), ('fr', 'False', 'True')], profile_id=2))
    disabled_source, disabled_library, disabled_root = add_source(
        arr_library, tmp_path, name='Disabled source', folder='Disabled source', profile_id=2)
    repository.update_source(disabled_source, arr_library, enabled=0)
    other_source, other_library, other_root = add_source(
        arr_library, tmp_path, name='Disabled library', folder='Disabled library', profile_id=2)
    repository.update_library(other_library, arr_library, enabled=0)
    no_profile, no_profile_library, no_profile_root = add_source(
        arr_library, tmp_path, name='No profile', folder='No profile')
    cases = {
        'parsed': (source, library, root, {}),
        'identified': (source, library, root, {'identity_state': 'identified'}),
        'ambiguous': (source, library, root, {'identity_state': 'ambiguous'}),
        'unmatched': (source, library, root, {'identity_state': 'unmatched'}),
        'server missing': (source, library, root, {'server_missing': 1}),
        'unmonitored': (source, library, root, {'monitored': 0}),
        'complete': (source, library, root, {'missing_subtitles': '[]'}),
        'not indexed': (source, library, root, {'missing_subtitles': None}),
        'disabled source': (disabled_source, disabled_library, disabled_root, {}),
        'disabled library': (other_source, other_library, other_root, {}),
        'no profile': (no_profile, no_profile_library, no_profile_root, {}),
    }
    for title, (owner, lib, folder, extra) in cases.items():
        attributes = {'missing_subtitles': "['en', 'fr:hi']", **extra}
        add_item(arr_library, owner, lib, video(folder / f'{title}.mkv'), title=title, **attributes)

    calls = {}
    def providers(path, languages, *args, source, **kwargs):
        calls[source.title] = languages
        return iter(())

    monkeypatch.setattr(download, 'generate_subtitles', providers)
    monkeypatch.setattr('media_sources.service.get_providers', lambda: ['fake-provider'])
    monkeypatch.setattr(settings.general, 'adaptive_searching', False)
    expected = {'parsed', 'identified'}

    rows = client.get('/api/movies/wanted?sources=1', headers=HEADERS).get_json()['data']
    assert {row['title'] for row in rows if row['origin'] == 'source'} == expected
    assert client.get('/api/badges?sources=1', headers=HEADERS).get_json()['movies'] == 5
    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id='test')
    assert calls == {title: [('en', 'False', 'False'), ('fr', 'True', 'False')] for title in expected}
    for row in repository.list_items(library_id=library):
        if row.title in expected:
            assert {code for code, _ in ast.literal_eval(row.failed_attempts)} == {'en', 'fr:hi'}
        else:
            assert row.failed_attempts is None


def test_legacy_filter_uses_owner_and_keeps_upstream_descending_order(client, arr_library):
    from app.database import TableArrInstances, TableEpisodes, TableMovies, TableShows

    for instance_id, kind in ((4, 'sonarr'), (5, 'radarr')):
        arr_library.execute(sa.insert(TableArrInstances).values(
            id=instance_id, kind=kind, name=f'Other {kind}', stable_key=f'other-{kind}',
            port=9000 + instance_id, enabled=1, is_default=0, path_mappings='[]'))
    arr_library.execute(sa.insert(TableShows).values(
        id=2, arr_instance_id=4, sonarrSeriesId=20, title='AAA Show', path='/other/show',
        profileId=1, monitored='True', seriesType='standard', tags='[]'))
    arr_library.execute(sa.insert(TableEpisodes).values(
        id=2, series_id=2, arr_instance_id=4, sonarrSeriesId=20, sonarrEpisodeId=990, title='Pilot',
        season=1, episode=1, path='/other/show/Pilot.mkv', monitored='True', missing_subtitles="['en']"))
    arr_library.execute(sa.insert(TableMovies).values(
        id=2, arr_instance_id=5, radarrId=99, tmdbId='700', title='ZZZ Movie', path='/other/movie.mkv', profileId=1,
        monitored='True', missing_subtitles="['en']"))

    for endpoint, key, ids, owner in (('episodes', 'sonarrEpisodeId', [990, 111], 4),
                                      ('movies', 'radarrId', [99, 21], 5)):
        url = f'/api/{endpoint}/wanted?length=2'
        legacy = client.get(url, headers=HEADERS)
        assert client.get(url + '&sources=1', headers=HEADERS).data == legacy.data
        assert [row[key] for row in legacy.get_json()['data']] == ids
        assert all('origin' not in row for row in legacy.get_json()['data'])
        filtered = client.get(url + f'&sources=1&source_key[]=arr:{owner}', headers=HEADERS).get_json()
        assert filtered['total'] == 1 and [row[key] for row in filtered['data']] == ids[:1]
        assert client.get(url + '&sources=1&source_key[]=source:7', headers=HEADERS).get_json()['total'] == 0
        assert client.get(url + '&source_key[]=nonsense', headers=HEADERS).status_code == 400


def test_source_row_format_preserves_local_paths_and_absolute_posters(languages_loaded, monkeypatch):
    from api import utils
    from media_sources.api_rows import postprocess_source

    monkeypatch.setattr(utils.path_mappings, 'path_replace', Mock(side_effect=AssertionError('Arr path mapping')))
    monkeypatch.setattr(utils.path_mappings, 'path_replace_movie', Mock(side_effect=AssertionError('Arr path mapping')))
    poster = 'https://example.test/poster.jpg'
    path = '/mnt/folder/Movie.mkv'
    result = postprocess_source({
        'path': path, 'video_path': path, 'poster': poster, 'monitored': 1,
        'subtitles': "[['en', '/mnt/folder/Movie.en.srt', 42]]", 'missing_subtitles': "['hu:forced']",
    })
    assert result['path'] == result['video_path'] == path
    assert result['poster'] == poster
    assert result['monitored'] is True
    assert result['subtitles'][0]['path'] == '/mnt/folder/Movie.en.srt'
    assert result['missing_subtitles'][0]['forced'] is True
    assert postprocess_source({'monitored': 0})['monitored'] is False


def test_wanted_refreshes_stale_shadows_and_retains_mount_shadows(client, arr_library, tmp_path, monkeypatch):
    from app.database import TableArrInstances, TableMovies
    from media_sources import repository, wanted
    from media_sources import identity
    from subtitles import download

    source, library, root = add_source(arr_library, tmp_path, profile_id=1)
    arr_library.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(movie_file_id=100))
    for row_id, name in ((2, 'second'), (3, 'same path'), (4, 'mapped path')):
        paths = {3: root / 'same path.mkv', 4: '/incoming/mapped path.mkv'}
        path = paths.get(row_id, f'/arr/movies/{name}.mkv')
        arr_library.execute(sa.insert(TableMovies).values(
            id=row_id, arr_instance_id=2, radarrId=50 + row_id, tmdbId=str(700 + row_id), title=f'Arr {name}', path=str(path),
            profileId=1, monitored='True', movie_file_id=100, missing_subtitles="['en']"))
    arr_library.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 2).values(
        path_mappings=json.dumps([['/incoming', str(root)]])))
    cases = {
        'bind': {'shadow_rung': 'inode', 'shadow_arr_row_id': 1, 'shadow_arr_path': '/arr/movies/Arr Movie.mkv'},
        'second': {'shadow_rung': 'attributes', 'shadow_arr_row_id': 2, 'shadow_arr_path': '/arr/movies/second.mkv'},
        'gone': {'shadow_rung': 'inode', 'shadow_arr_row_id': 999, 'shadow_arr_path': '/arr/removed.mkv'},
        'owner gone': {'shadow_rung': 'inode', 'shadow_instance_id': None, 'shadow_arr_row_id': 1},
        'same path': {'arr_shadow': 0},
        'mapped path': {'arr_shadow': 0},
    }
    item_ids = {}
    for title, values in cases.items():
        shadow = dict(arr_shadow=1, shadow_instance_id=2, shadow_arr_table='movie', shadow_arr_file_id=100)
        item_ids[title] = add_item(arr_library, source, library, video(root / f'{title}.mkv'), title=title,
                                   missing_subtitles="['en']", **{**shadow, **values})

    providers = Mock(return_value=[])
    monkeypatch.setattr(download, 'generate_subtitles', providers)
    monkeypatch.setattr('media_sources.service.get_providers', lambda: ['fake'])
    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    stat = Mock(side_effect=AssertionError('unchanged mount shadows should not be statted'))
    monkeypatch.setattr(identity, 'stat_file', stat)

    before = client.get('/api/movies/wanted?sources=1', headers=HEADERS).get_json()['data']
    assert all(row['origin'] == 'arr' for row in before)
    assert client.get('/api/badges?sources=1', headers=HEADERS).get_json()['movies'] == 4
    assert repository.get_item(item_ids['mapped path']).arr_shadow == 0

    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=23)

    assert {call.kwargs['source'].title for call in providers.call_args_list} == {'gone', 'owner gone'}
    stat.assert_not_called()
    assert {title for title, item_id in item_ids.items() if repository.get_item(item_id).arr_shadow} == {
        'bind', 'second', 'same path', 'mapped path'}
    rows = client.get('/api/movies/wanted?sources=1', headers=HEADERS).get_json()['data']
    assert {row['title'] for row in rows if row['origin'] == 'source'} == {'gone', 'owner gone'}
    assert client.get('/api/badges?sources=1', headers=HEADERS).get_json()['movies'] == 6


@pytest.mark.parametrize('state', ['ready', 'waiting', 'recent failure', 'translated', 'moved translation', 'low score'])
def test_wanted_checks_translation_before_asking_providers(world, isolated_jobs, monkeypatch, state):
    from app.config import settings
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, translation, wanted
    from subtitles import download
    from subtitles.tools.translate import availability, failure_record

    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    index_computation = Mock(side_effect=AssertionError('Wanted must use indexed missing subtitles'))
    monkeypatch.setattr(translation, '_current_missing', index_computation)
    monkeypatch.setattr(settings.translator, 'runtime_v2', False)
    monkeypatch.setattr(availability, 'translation_available', lambda: SimpleNamespace(available=True))
    monkeypatch.setattr(settings.translator, 'min_source_wait_minutes', 10 if state == 'waiting' else 0)
    monkeypatch.setattr(settings.translator, 'min_source_score', 50 if state == 'low score' else 0)
    context = pipeline._movie(world)
    source_subtitle = Path(context.path).with_suffix('.en.srt')
    source_subtitle.write_bytes(pipeline.CONTENT)
    items = [
        {'id': 1, 'language': 'en', 'forced': 'False', 'hi': 'False'},
        {'id': 2, 'language': 'hu', 'forced': 'False', 'hi': 'False', 'translate_from': 'en'},
    ]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(items)))
    update_profile_id_list.invalidate()
    repository.update_item(context.item_id, world.db, subtitles=str([['en', str(source_subtitle), 100]]),
                           missing_subtitles="['hu']")
    if state in ('translated', 'moved translation'):
        recorded_video = Path(context.path)
        if state == 'moved translation':
            recorded_video = world.tmp_path / 'before-move' / recorded_video.name
            recorded_video.parent.mkdir()
        translated = recorded_video.with_suffix('.hu.ass')
        translated.write_bytes(pipeline.CONTENT)
        repository.insert_history(world.db, item_id=context.item_id, source_id=context.source_id,
                                  item_kind='movie', action=6, description='translated', language='hu', subtitles_path=str(translated),
                                  video_path=str(recorded_video), source_name='Local', item_title='Film')
    if state == 'low score':
        repository.insert_history(world.db, item_id=context.item_id, source_id=context.source_id,
                                  item_kind='movie', action=1, description='downloaded', language='en', subtitles_path=str(source_subtitle),
                                  score=10, score_out_of=100, video_path=context.path, source_name='Local',
                                  item_title='Film')
    failure_args = (None, 'source', context.item_id, 'en', 'hu', False, False)
    if state == 'recent failure':
        failure_record.record_failed_translation(*failure_args, source_created_at=context.created_at)
    providers = Mock(return_value=[])
    monkeypatch.setattr(download, 'generate_subtitles', providers)
    try:
        wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=1)
        pending = isolated_jobs.jobs_pending_queue
        if state in ('ready', 'moved translation'):
            assert len(pending) == 1 and pending[0].func == 'translate_subtitles_file'
            assert pending[0].kwargs['source'] == context
            wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=2)
            assert len(isolated_jobs.jobs_pending_queue) == 1
        else:
            assert not pending
        index_computation.assert_not_called()
        assert providers.call_count == (1 if state in ('recent failure', 'low score') else 0)
    finally:
        failure_record.clear_failed_translation(*failure_args, source_created_at=context.created_at)


def test_adaptive_search_and_provider_throttle_use_existing_attempts(world, monkeypatch):
    from app.config import settings
    from media_sources import repository, wanted
    from subtitles import download

    adaptive = datetime.now()
    attempts = str([['en', (adaptive - timedelta(days=30)).timestamp()], ['en', adaptive.timestamp()]])
    monkeypatch.setattr(settings.general, 'adaptive_searching', True)
    monkeypatch.setattr(settings.general, 'adaptive_searching_delay', '1w')
    monkeypatch.setattr(settings.general, 'adaptive_searching_delta', '1w')
    held = pipeline._movie(world)
    repository.update_item(held.item_id, world.db, failed_attempts=attempts)
    available = pipeline._movie(world, 'Another.2002.mkv')
    providers = Mock(return_value=[])
    monkeypatch.setattr(download, 'generate_subtitles', providers)
    throttled = Mock(return_value=[])
    monkeypatch.setattr('media_sources.service.get_providers', throttled)

    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=1)

    assert throttled.call_count == 1
    providers.assert_not_called()
    assert repository.get_item(held.item_id).failed_attempts == attempts
    assert repository.get_item(available.item_id).failed_attempts is None


def test_search_all_queues_both_jobs_and_source_job_saves_through_shared_pipeline(
        client, arr_library, isolated_jobs, tmp_path, monkeypatch):
    from app.config import settings
    from media_sources import repository, service, wanted
    from subtitles.wanted import movies

    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(movies, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(service, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(settings.general, 'use_embedded_subs', False)
    from subtitles import download, processing, sync
    from subzero.language import Language
    monkeypatch.setattr('media_sources.indexer.get_language_set', lambda: {Language('eng')})
    monkeypatch.setattr(download, '_get_pool', lambda *args, **kwargs: SimpleNamespace(
        providers=['fake'], discarded_providers=[]))
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: False)
    monkeypatch.setattr(processing, 'call_external_webhook', Mock())
    monkeypatch.setattr(sync, '_resolve_subsync_overrides', lambda *args, **kwargs: (True, False, 0, None, '60'))
    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    monkeypatch.setattr('app.notifier.send_notifications_source', Mock())
    monkeypatch.setattr('media_servers.dispatcher.notify_subtitle_mutation', Mock())
    source, library, root = add_source(arr_library, tmp_path, profile_id=1)
    path = video(root / 'Film.2001.1080p.BluRay.x264-GRP.mkv')
    item = add_item(arr_library, source, library, path, title='Film', year='2001',
                    missing_subtitles="['en']", scene_name=path.stem)
    asked = pipeline._download(monkeypatch)
    response = client.patch('/api/movies', headers=HEADERS, data={'action': 'search-wanted', 'sources': 1})
    assert response.status_code == 204
    jobs = isolated_jobs.jobs_pending_queue
    assert {(job.module, job.func) for job in jobs} == {
        ('media_sources.wanted', wanted.WANTED_JOB_ID), ('subtitles.wanted.movies', 'wanted_search_missing_subtitles_movies')}
    queued = isolated_jobs._reserve_next_job()
    assert queued.module == 'media_sources.wanted'
    assert isolated_jobs._run_job(queued)
    assert asked['video'].source_context.item_id == item
    assert path.with_suffix('.en.srt').read_bytes() == pipeline.CONTENT
    assert repository.get_item(item).missing_subtitles == '[]'
    assert arr_library.scalar(sa.select(repository.History.c.item_id)) == item
    monkeypatch.setattr(settings.general, 'use_radarr', False)
    response = client.patch('/api/movies', headers=HEADERS, data={'action': 'search-wanted', 'sources': 1})
    assert response.status_code == 204
    assert len([job for job in isolated_jobs.jobs_pending_queue if job.module == 'subtitles.wanted.movies']) == 1


def test_automatic_search_leaves_a_subtitle_that_arrived_during_provider_search(world, monkeypatch):
    from app.config import settings
    from media_sources import repository, wanted
    from subtitles import download

    context = pipeline._movie(world)
    destination = Path(context.path).with_suffix('.en.srt')
    monkeypatch.setattr(settings.general, 'chmod_enabled', True)
    monkeypatch.setattr(settings.general, 'chmod', '0644')
    arriving_content = b'a subtitle saved by another operation'
    provider_result = pipeline.FakeSubtitle()

    def download_best(videos, **kwargs):
        (found,) = videos
        destination.write_bytes(arriving_content)
        destination.chmod(0o600)
        return {found: [provider_result]}

    monkeypatch.setattr(download, 'download_best_subtitles', download_best)
    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=1)

    assert destination.read_bytes() == arriving_content
    assert destination.stat().st_mode & 0o777 == 0o600
    assert world.db.execute(sa.select(repository.History.c.id)).all() == []


@pytest.mark.parametrize('change', ['file removed', 'video replaced', 'profile edited', 'arr claimed'])
def test_provider_return_rechecks_file_profile_and_arr_claim(world, monkeypatch, change):
    from app.config import settings
    from app.database import TableArrInstances, TableLanguagesProfiles, TableMovies, update_profile_id_list
    from media_sources import repository, wanted
    from subtitles import download

    context = pipeline._movie(world)
    video_path = Path(context.path)
    if change == 'arr claimed':
        monkeypatch.setattr(settings.general, 'use_radarr', True)

    def download_best(videos, **kwargs):
        (found,) = videos
        if change == 'file removed':
            video_path.unlink()
        elif change == 'video replaced':
            replacement = video(video_path.with_suffix('.new'), size=2000)
            os.replace(replacement, video_path)
        elif change == 'profile edited':
            profile = json.dumps([{'id': 1, 'language': 'hu', 'forced': 'False', 'hi': 'False'}])
            world.db.execute(sa.update(TableLanguagesProfiles).values(items=profile))
            update_profile_id_list.invalidate()
        else:
            world.db.execute(sa.insert(TableArrInstances).values(
                id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1, path_mappings='[]'))
            world.db.execute(sa.insert(TableMovies).values(
                id=73, arr_instance_id=42, radarrId=9000, title='Film', path=context.path, tmdbId='73'))
        return {found: [pipeline.FakeSubtitle()]}

    monkeypatch.setattr(download, 'download_best_subtitles', download_best)
    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=1)

    assert not video_path.with_suffix('.en.srt').exists()
    assert world.db.execute(sa.select(repository.History.c.id)).all() == []
    assert bool(repository.get_item(context.item_id).arr_shadow) == (change == 'arr claimed')


def test_explicit_api_opt_in_controls_search_all_but_not_the_source_schedule(
        client, arr_library, isolated_jobs, tmp_path, monkeypatch):
    from app.config import settings
    from media_sources import schedule, wanted
    from subtitles.wanted import movies

    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(movies, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(settings.media_sources, 'visible', False)
    _, _, root = add_source(arr_library, tmp_path, profile_id=1)
    assert root.is_dir() and schedule.has_enabled_source(arr_library)
    plain = client.patch('/api/movies', headers=HEADERS, data={'action': 'search-wanted'})
    assert plain.status_code == 204
    assert [job.module for job in isolated_jobs.jobs_pending_queue] == ['subtitles.wanted.movies']
    explicit = client.patch('/api/movies', headers=HEADERS, data={'action': 'search-wanted', 'sources': 1})
    assert explicit.status_code == 204
    assert any(job.module == 'media_sources.wanted' for job in isolated_jobs.jobs_pending_queue)


def test_audio_filter_can_list_a_language_only_present_in_a_folder(client, arr_library, tmp_path):
    source, library, root = add_source(arr_library, tmp_path, profile_id=1)
    add_item(arr_library, source, library, root / 'Film.mkv', audio_language="['French']", missing_subtitles="['en']")
    url = '/api/system/languages/audio'
    assert client.get(url, headers=HEADERS).get_json() == []
    assert client.get(url + '?sources=1', headers=HEADERS).get_json() == [{'code2': 'fr', 'name': 'French'}]


def test_sports_badge_excludes_library_rows_until_the_sports_wanted_list_supports_them(
        client, arr_library, tmp_path):
    source, library, root = add_source(arr_library, tmp_path, content_kind='sports', profile_id=1)
    group = add_group(arr_library, source, library, kind='league', title='F1')
    add_item(arr_library, source, library, root / 'Race.mkv', kind='sports_event', group_id=group,
             missing_subtitles="['en']")
    assert client.get('/api/badges?sources=1', headers=HEADERS).data == client.get('/api/badges', headers=HEADERS).data
