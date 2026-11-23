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
    from app.database import TableArrInstances, TableMovies, TableSportsEvents
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
        path_mappings=json.dumps([['/earlier', str(root)], ['/incoming', str(root)]])))
    arr_library.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 3).values(
        path_mappings=json.dumps([[r'D:\Races', str(root)]])))
    arr_library.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 1).values(
        path='D:/Races/cross-kind path.mkv'))
    cases = {
        'bind': {'shadow_rung': 'inode', 'shadow_arr_row_id': 1, 'shadow_arr_path': '/arr/movies/Arr Movie.mkv'},
        'second': {'shadow_rung': 'attributes', 'shadow_arr_row_id': 2, 'shadow_arr_path': '/arr/movies/second.mkv'},
        'gone': {'shadow_rung': 'inode', 'shadow_arr_row_id': 999, 'shadow_arr_path': '/arr/removed.mkv'},
        'owner gone': {'shadow_rung': 'inode', 'shadow_instance_id': None, 'shadow_arr_row_id': 1},
        'same path': {'arr_shadow': 0},
        'mapped path': {'arr_shadow': 0},
        'cross-kind path': {'arr_shadow': 0},
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
        'bind', 'second', 'same path', 'mapped path', 'cross-kind path'}
    assert repository.get_item(item_ids['cross-kind path']).shadow_arr_table == 'sports_event'
    rows = client.get('/api/movies/wanted?sources=1', headers=HEADERS).get_json()['data']
    assert {row['title'] for row in rows if row['origin'] == 'source'} == {'gone', 'owner gone'}
    assert client.get('/api/badges?sources=1', headers=HEADERS).get_json()['movies'] == 6


@pytest.mark.parametrize('windows,local,stored,claimed', [
    pytest.param(True, 'C:/Media', r'c:\MEDIA\épreuve.mkv', True, id='windows-filename-case'),
    pytest.param(True, 'C:/Vidéos', r'c:\VIDÉOS\Épreuve.mkv', True, id='windows-prefix-case'),
    pytest.param(True, 'C:/ΟΣ', r'c:\ος\Épreuve.mkv', True, id='windows-final-sigma'),
    pytest.param(True, 'C:/ος', r'c:\οσ\Épreuve.mkv', True, id='windows-medial-sigma'),
    pytest.param(True, 'C:/STRASSE', r'c:\Straße\Épreuve.mkv', True, id='windows-sharp-s'),
    pytest.param(True, 'C:/İzmir', r'c:\İzmir\Épreuve.mkv', True, id='windows-expanding-prefix'),
    pytest.param(True, 'C:/İzmir', r'c:\i̇zmir\épreuve.mkv', True, id='windows-longer-path-prefix'),
    pytest.param(True, 'C:/i̇zmir', r'c:\İzmir\Épreuve.mkv', True, id='windows-shorter-path-prefix'),
    pytest.param(True, 'C:/İzmir', r'c:\i̇zmir2\épreuve.mkv', False, id='windows-prefix-boundary'),
    pytest.param(False, '/media', '/media/Épreuve.mkv', True, id='posix-exact'),
    pytest.param(False, '/media', '/media/épreuve.mkv', False, id='posix-distinct-case'),
    pytest.param(False, '/media', r'/media\Épreuve.mkv', False, id='posix-distinct-separator'),
])
def test_sportarr_claim_matches_the_local_filesystem(
        client, arr_library, tmp_path, monkeypatch, windows, local, stored, claimed):
    from app.database import TableArrInstances, TableSportsEvents
    from media_sources import identity

    monkeypatch.setattr(identity, '_LOCAL_WINDOWS_PATHS', windows)
    source, library, _ = add_source(arr_library, tmp_path, profile_id=1)
    item = add_item(arr_library, source, library, stored, missing_subtitles="['en']")
    arr_library.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 3).values(
        path_mappings=json.dumps([[r'D:\Races', local]])))
    arr_library.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 1).values(
        path='D:/Races/Épreuve.mkv'))

    assert (identity.ArrOwners.load().path_stage(stored) is not None) == claimed
    result = client.get('/api/movies/wanted?sources=1', headers=HEADERS).get_json()
    assert [row['id'] for row in result['data'] if row['origin'] == 'source'] == ([] if claimed else [item])
    badges = client.get('/api/badges?sources=1', headers=HEADERS).get_json()
    assert badges['source_movies'] == (0 if claimed else 1)


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
            queued_source = pending[0].kwargs['source']
            assert queued_source.same_item(context)
            assert queued_source.wanted_file_stamp is not None
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


@pytest.mark.parametrize('expired,waiting,ready', [
    ('en', 'en:hi', 'en:forced'),
    ('en:hi', 'en:forced', 'en'),
    ('en:forced', 'en', 'en:hi'),
])
def test_wanted_adaptive_attempts_keep_subtitle_variants_independent(world, monkeypatch, expired, waiting, ready):
    from app.config import settings
    from media_sources import indexer, repository, wanted
    from subtitles import download

    now = datetime.now()
    attempts = [
        [expired, (now - timedelta(days=60)).timestamp()], [expired, now.timestamp()],
        [waiting, (now - timedelta(days=14)).timestamp()], [waiting, now.timestamp()],
    ]
    monkeypatch.setattr(settings.general, 'adaptive_searching', True)
    monkeypatch.setattr(settings.general, 'adaptive_searching_max_age', '4w')
    monkeypatch.setattr(settings.general, 'adaptive_searching_delay', '1w')
    monkeypatch.setattr(settings.general, 'adaptive_searching_delta', '1w')
    profile = add_profile(world.db, [
        ('en', 'False', 'False'), ('en', 'False', 'True'), ('en', 'True', 'False'),
    ], profile_id=2)
    item = pipeline._movie(world)
    repository.update_item(item.item_id, world.db, profile_id=profile, failed_attempts=str(attempts))
    indexer.recalculate_missing_subtitles(library_id=world.library_id)
    assert set(ast.literal_eval(repository.get_item(item.item_id).missing_subtitles)) == {waiting, ready}

    providers = Mock(return_value=[])
    monkeypatch.setattr(download, 'generate_subtitles', providers)
    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=1)
    providers.assert_called_once()
    languages = {
        'en': ('en', 'False', 'False'), 'en:hi': ('en', 'True', 'False'), 'en:forced': ('en', 'False', 'True'),
    }
    assert providers.call_args.args[1] == [languages[ready]]
    recorded = ast.literal_eval(repository.get_item(item.item_id).failed_attempts)
    assert [row for row in recorded if row[0] != ready] == sorted(attempts, key=lambda row: row[0])
    assert sum(code == ready for code, _ in recorded) == 1


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


@pytest.mark.parametrize('change', ['file removed', 'video replaced', 'profile edited', 'arr claimed',
                                   'arr claimed after preparation'])
def test_provider_return_rechecks_file_profile_and_arr_claim(world, monkeypatch, change):
    from app.config import settings
    from app.database import TableArrInstances, TableLanguagesProfiles, TableMovies, update_profile_id_list
    from media_sources import indexer, repository, wanted
    from subtitles import download

    context = pipeline._movie(world)
    video_path = Path(context.path)
    def claim_media():
        monkeypatch.setattr(settings.general, 'use_radarr', True)
        world.db.execute(sa.insert(TableArrInstances).values(
            id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1, path_mappings='[]'))
        world.db.execute(sa.insert(TableMovies).values(
            id=73, arr_instance_id=42, radarrId=9000, title='Film', path=context.path, tmdbId='73'))

    if change == 'arr claimed after preparation':
        publication = indexer.source_item_publication

        def claim_at_registry_lock(source, **kwargs):
            claim_media()
            return publication(source, **kwargs)

        monkeypatch.setattr(indexer, 'source_item_publication', claim_at_registry_lock)

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
        elif change == 'arr claimed':
            claim_media()
        return {found: [pipeline.FakeSubtitle()]}

    monkeypatch.setattr(download, 'download_best_subtitles', download_best)
    wanted.wanted_search_missing_subtitles_sources(kind='movie', job_id=1)

    assert not video_path.with_suffix('.en.srt').exists()
    assert world.db.execute(sa.select(repository.History.c.id)).all() == []
    refreshed = repository.get_item(context.item_id)
    assert bool(refreshed.arr_shadow) == change.startswith('arr claimed')
    if change != 'profile edited':
        assert refreshed.failed_attempts is None


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


def test_folder_only_badge_does_not_reuse_legacy_arr_count(client, arr_library, tmp_path):
    from app.database import TableArrInstances

    source, library, root = add_source(arr_library, tmp_path, profile_id=1)
    arr_library.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 2).values(enabled=0, is_default=0))
    legacy = client.get('/api/badges', headers=HEADERS)
    assert legacy.get_json()['movies'] == 1
    assert client.get('/api/badges?sources=1', headers=HEADERS).data == legacy.data

    add_item(arr_library, source, library, root / 'Movie.mkv', missing_subtitles="['en', 'hu']")
    combined = client.get('/api/badges?sources=1', headers=HEADERS).get_json()
    assert combined['movies'] == combined['source_movies'] == 2
    assert client.get('/api/badges', headers=HEADERS).data == legacy.data


@pytest.mark.parametrize('after_prepare', [False, True], ids=['during-staging', 'after-preparation'])
def test_runtime_keeps_manual_and_wanted_translation_reservations_separate(world, isolated_jobs, monkeypatch,
                                                                         after_prepare):
    import pysubs2

    from app import database
    from app.config import settings
    from app.database import TableArrInstances, TableLanguagesProfiles, TableMovies, update_profile_id_list
    from media_sources import indexer, repository, wanted
    from subtitles.tools.translate import main, runtime
    from subtitles.tools.translate.runtime import gate, service as runtime_service
    from subtitles.tools.translate.runtime.jobs import TranslationJobs, target_key
    from translator_dispatch_helpers import Env

    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(settings.translator, 'runtime_v2', True)
    monkeypatch.setattr(database, 'engine', world.db.get_bind())
    monkeypatch.setattr(runtime, '_tables_present', False)
    monkeypatch.setattr(runtime, '_absent_until', 0.0)
    assert runtime.runtime_v2_enabled()
    monkeypatch.setattr(settings.translator, 'min_source_wait_minutes', 0)
    monkeypatch.setattr(settings.translator, 'min_source_score', 0)
    profile = [
        {'id': 1, 'language': 'en', 'forced': 'False', 'hi': 'False'},
        {'id': 2, 'language': 'hu', 'forced': 'False', 'hi': 'False', 'translate_from': 'en'},
    ]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile)))
    update_profile_id_list.invalidate()
    context = pipeline._movie(world)
    subtitle = Path(context.path).with_suffix('.en.srt')
    pipeline._download(monkeypatch)
    env = Env(world.db, world.tmp_path)
    try:
        env.ai_engine()
        monkeypatch.setattr(gate, 'translation_gate', lambda: gate.evaluate(env.store, env.holds, env.workers))
        monkeypatch.setattr(runtime_service, 'Dispatcher', env.dispatcher)

        wanted.wanted_search_missing_subtitles_sources(kind='movie')
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        (automatic,) = isolated_jobs.jobs_pending_queue
        assert automatic.kwargs['source'].wanted_file_stamp is not None
        assert automatic.kwargs['source_srt_file'] == str(subtitle)
        assert main.translate_subtitles_file(**automatic.kwargs) == automatic.job_id
        manual = main.translate_subtitles_file(context.path, str(subtitle), 'en', 'hu', False, False, 'movies',
                                               None, None, None, None, source=context)
        assert manual is not None and manual != automatic.job_id
        assert [job.job_id for job in isolated_jobs.jobs_pending_queue] == [automatic.job_id, manual]
        key = main._job_source_key(context.path, subtitle, None, automatic.kwargs['source'])
        manual_key = main._job_source_key(context.path, subtitle, None, context)
        target = target_key('hu', False, False)
        ledger = TranslationJobs(world.db)
        assert ledger.active_for(key, target).queue_job_id == automatic.job_id

        def claim_media():
            monkeypatch.setattr(settings.general, 'use_radarr', True)
            world.db.execute(sa.insert(TableArrInstances).values(
                id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1, path_mappings='[]'))
            world.db.execute(sa.insert(TableMovies).values(
                id=73, arr_instance_id=42, radarrId=9000, title='Film', path=context.path, tmdbId='73'))

        if after_prepare:
            publication = indexer.source_item_publication

            def claim_at_registry_lock(source, **kwargs):
                claim_media()
                return publication(source, **kwargs)

            monkeypatch.setattr(indexer, 'source_item_publication', claim_at_registry_lock)
        else:
            save = pysubs2.SSAFile.save

            def save_and_claim(subtitles, *args, **kwargs):
                save(subtitles, *args, **kwargs)
                claim_media()

            monkeypatch.setattr(pysubs2.SSAFile, 'save', save_and_claim)
        assert isolated_jobs._reserve_next_job() is automatic
        assert isolated_jobs._run_job(automatic)
        (worker_request,) = env.requests()
        assert worker_request['request']['media']['title'] == 'Film'
        assert repository.get_item(context.item_id).arr_shadow == 1
        assert ledger.active_for(key, target) is None
        assert ledger.active_for(manual_key, target).queue_job_id == manual
        assert [job.job_id for job in isolated_jobs.jobs_pending_queue] == [manual]
        assert not Path(context.path).with_suffix('.hu.srt').exists()
        assert world.db.execute(sa.select(repository.History.c.action)).scalars().all() == [1]
    finally:
        env.close()


@pytest.mark.parametrize('change', [
    'translator unavailable', 'arr claimed', 'arr claimed during translation', 'arr claimed after preparation',
    'mapped input owner changed', 'published',
])
def test_translation_after_a_wanted_download_keeps_its_publication_scope(world, isolated_jobs, monkeypatch, change):
    from app.config import settings
    from app.database import TableArrInstances, TableLanguagesProfiles, TableMovies, update_profile_id_list
    from media_sources import indexer, repository, wanted
    from subtitles.tools.translate import main

    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(settings.translator, 'runtime_v2', False)
    monkeypatch.setattr(settings.translator, 'translator_type', 'google_translate')
    monkeypatch.setattr(settings.translator, 'min_source_wait_minutes', 0)
    monkeypatch.setattr(settings.translator, 'min_source_score', 0)
    profile = [
        {'id': 1, 'language': 'en', 'forced': 'False', 'hi': 'False'},
        {'id': 2, 'language': 'hu', 'forced': 'False', 'hi': 'False', 'translate_from': 'en'},
    ]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile)))
    update_profile_id_list.invalidate()
    mapped_owner = change == 'mapped input owner changed'
    if mapped_owner:
        monkeypatch.setattr(settings.general, 'subfolder', 'relative')
        monkeypatch.setattr(settings.general, 'subfolder_custom', '../Subs')
    context = pipeline._movie(world, 'Film.mkv')
    media = Path(context.path)
    folder = world.tmp_path / 'Subs' if mapped_owner else media.parent
    original = folder / 'Film.en.srt'
    destination = folder / 'Film.hu.srt'
    pipeline._download(monkeypatch)
    engine = Mock(side_effect=AssertionError('the translator should not run'))
    translates = change not in ('translator unavailable', 'arr claimed')
    if not translates:
        monkeypatch.setattr(main.TranslatorFactory, 'create_translator', engine)

    def claim_media():
        monkeypatch.setattr(settings.general, 'use_radarr', True)
        world.db.execute(sa.insert(TableArrInstances).values(
            id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1, path_mappings='[]'))
        world.db.execute(sa.insert(TableMovies).values(
            id=73, arr_instance_id=42, radarrId=9000, title='Film', path=context.path, tmdbId='73'))

    wanted.wanted_search_missing_subtitles_sources(kind='movie')
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    (translation,) = isolated_jobs.jobs_pending_queue
    assert translation.func == 'translate_subtitles_file'
    assert translation.kwargs['source'].wanted_file_stamp is not None
    assert Path(translation.kwargs['source_srt_file']).resolve() == original
    generated = folder / 'Film.hu.ffsubsync.srt'
    generated.write_bytes(pipeline.CONTENT)
    cleanup = Mock(wraps=indexer.quarantine_sync_outputs_after_mutation)
    monkeypatch.setattr(indexer, 'quarantine_sync_outputs_after_mutation', cleanup)

    if change == 'arr claimed':
        claim_media()
    elif translates:
        from subtitles.tools.translate.services import google_translator

        transport = Mock()
        transport.return_value.translate.return_value = 'Translated cue'
        monkeypatch.setattr(google_translator, 'GoogleTranslator', transport)
        monkeypatch.setattr(google_translator, 'jobs_queue', isolated_jobs)
        if change == 'arr claimed during translation':
            save = google_translator.pysubs2.SSAFile.save

            def save_and_claim(subtitles, *args, **kwargs):
                save(subtitles, *args, **kwargs)
                claim_media()

            monkeypatch.setattr(google_translator.pysubs2.SSAFile, 'save', save_and_claim)
        elif change == 'arr claimed after preparation':
            publication = indexer.source_item_publication

            def claim_at_publication(source, **kwargs):
                claim_media()
                return publication(source, **kwargs)

            monkeypatch.setattr(indexer, 'source_item_publication', claim_at_publication)
        elif mapped_owner:
            other = world.tmp_path / 'Other'
            alias = other / 'Alias.mkv'
            initial = video(other / 'Nested' / 'Other.mkv')
            alias.symlink_to(initial)
            owner = world.db.execute(sa.insert(TableArrInstances).values(
                kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1,
                path_mappings=json.dumps([['/cinema', str(other)]])).returning(TableArrInstances.id)).scalar_one()
            world.db.execute(sa.insert(TableMovies).values(
                arr_instance_id=owner, radarrId=9000, title='Other', path='/cinema/Alias.mkv', tmdbId='73'))
            monkeypatch.setattr(settings.general, 'use_radarr', True)
            publication = indexer.source_item_publication

            def change_alias_at_publication(source, **kwargs):
                alias.unlink()
                alias.symlink_to(video(world.tmp_path / 'Relocated' / 'Film.en.mkv'))
                return publication(source, **kwargs)

            monkeypatch.setattr(indexer, 'source_item_publication', change_alias_at_publication)
    else:
        monkeypatch.setattr(main, 'translation_available', lambda: SimpleNamespace(available=False))
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    if translates:
        transport.return_value.translate.assert_called()
    else:
        engine.assert_not_called()
    assert original.read_bytes() == pipeline.CONTENT
    actions = world.db.execute(sa.select(repository.History.c.action).order_by(repository.History.c.id)).scalars().all()
    if change == 'published':
        assert b'Translated cue' in destination.read_bytes()
        assert actions == [1, 6]
        cleanup.assert_called_once()
        assert not generated.exists()
        assert not isolated_jobs.jobs_pending_queue
        return

    assert not destination.exists()
    assert generated.read_bytes() == pipeline.CONTENT
    cleanup.assert_not_called()
    assert actions == [1]
    item = repository.get_item(context.item_id)
    if change.startswith('arr claimed'):
        assert (item.arr_shadow, item.shadow_instance_id, item.shadow_arr_row_id, item.shadow_arr_path) == (
            1, 42, 73, context.path)
        assert not isolated_jobs.jobs_pending_queue
    else:
        assert item.arr_shadow == 0
        (fallback,) = isolated_jobs.jobs_pending_queue
        assert fallback.func == 'search_wanted_fallback'
        assert fallback.kwargs['target_language'] == 'hu'
        providers = Mock(side_effect=AssertionError('the replacement video should not be searched'))
        monkeypatch.setattr('media_sources.service.get_providers', providers)
        os.replace(video(media.with_suffix('.new'), size=3000), media)
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        providers.assert_not_called()
