"""Folder sports parsing and standalone subtitle activity."""

from pathlib import Path

import pytest

from media_source_helpers import add_profile, add_source, video
import test_media_sources_manual as manual_tests
import test_media_sources_no_change as legacy
import test_media_sources_pipeline as pipeline

HEADERS = legacy.HEADERS
arr_library = legacy.arr_library
client = legacy.client
everything_uses_the_test_database = legacy.everything_uses_the_test_database
isolated_jobs = pipeline.isolated_jobs
writes = manual_tests.writes


@pytest.fixture(autouse=True)
def keep_application_scheduler_stopped(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self: None)


# Paths below the sports root, with date, season year, season, round, title, part, and state.
FIXTURES = [
    ('Formula 1/2026-05-24 Monaco Grand Prix Race.mkv',
     ('2026-05-24', 2026, 2026, None, 'Monaco Grand Prix', 'Race', 'parsed')),
    ('Formula 1/2026.05.23 Monaco Grand Prix Qualifying.mkv',
     ('2026-05-23', 2026, 2026, None, 'Monaco Grand Prix', 'Qualifying', 'parsed')),
    ('NFL/Season 2026/NFL S2026E05 Bears vs Packers.mkv',
     (None, 2026, 2026, 5, 'Bears vs Packers', None, 'parsed')),
    ('NFL/2025-2026/Week 18 Bears vs Packers.mkv',
     (None, 2025, 2025, 18, 'Bears vs Packers', None, 'parsed')),
    ('Premier League/2026/Round 12 Arsenal vs Chelsea.mkv',
     (None, 2026, 2026, 12, 'Arsenal vs Chelsea', None, 'parsed')),
    ('MotoGP/2026/R07 Mugello Sprint.mkv',
     (None, 2026, 2026, 7, 'Mugello', 'Sprint', 'parsed')),
    ('NHL/Season 3/Game 12.mkv',
     (None, None, 3, None, 'Game 12', None, 'unmatched')),
    ('UFC/24.05.2026 UFC 300.mkv',
     (None, None, None, None, '300', None, 'unmatched')),
    ('Formula 1/2026-05-24.mkv',
     ('2026-05-24', 2026, 2026, None, '2026-05-24', None, 'unmatched')),
    ('NFL/S2026E05.mkv',
     (None, 2026, 2026, 5, 'S2026E05', None, 'unmatched')),
    ('Formula 1/2026/Formula.1.2026_07_05.British.Grand.Prix.Post-Race.1080p.WEB-DL.H264-FLY.mkv',
     ('2026-07-05', 2026, 2026, None, 'British Grand Prix', 'Post-Race', 'parsed')),
    ('NFL/Season 3/NFL.2026 01 25.Conference.Championship.Pt2.720p.HDTV.x264-GRP.mkv',
     ('2026-01-25', 2026, 3, None, 'Conference Championship', 'Part 2', 'parsed')),
    ('UFC/2026-02-29.UFC.1080p.WEB-DL.H264-FLY.mkv',
     (None, None, None, None, '2026-02-29.UFC.1080p.WEB-DL.H264-FLY', None, 'unmatched')),
    ('MotoGP/2026-05-24.1080p.WEB-DL.H264-FLY.mkv',
     ('2026-05-24', 2026, 2026, None, '2026-05-24.1080p.WEB-DL.H264-FLY', None, 'unmatched')),
    ('NFL/2025-2026/2026-01-01 Week 18 Bears vs Packers.mkv',
     ('2026-01-01', 2025, 2025, 18, 'Bears vs Packers', None, 'parsed')),
]


@pytest.mark.parametrize('name,expected', FIXTURES, ids=[name for name, _ in FIXTURES])
def test_sports_filename_identity(name, expected):
    from media_sources.sports_parse import parse_sports_file

    parsed = parse_sports_file(name, str(Path(name).parts[0]))
    assert (parsed.event_date, parsed.year, parsed.season, parsed.round, parsed.title, parsed.part_name,
            parsed.identity_state) == expected
    if parsed.title == Path(name).stem and expected[-1] == 'unmatched' and parsed.event_date:
        assert parsed.parsed_title is None


def test_a_bare_filename_year_does_not_identify_a_round():
    from media_sources.sports_parse import parse_sports_file

    parsed = parse_sports_file('NFL/2026 Week 05 Bears vs Packers.mkv', 'NFL')
    assert (parsed.year, parsed.round, parsed.identity_state) == (None, 5, 'unmatched')


def test_folder_scan_admits_leagues_and_events_with_no_sportarr(source_db, tmp_path, monkeypatch, languages_loaded):
    import sqlalchemy as sa
    from app.config import settings
    from media_sources import indexer, repository, scanner, wanted
    from media_sources.context import resolve_item
    from subzero.language import Language

    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    monkeypatch.setattr(indexer, 'get_language_set', lambda: {Language('eng')})
    monkeypatch.setattr(indexer, 'probe_item', lambda *args, **kwargs: ({}, None))

    def unexpected(*args, **kwargs):
        pytest.fail('A standalone sports scan called Sportarr mapping or TMDB')

    monkeypatch.setattr('discover.metadata.search_movies', unexpected)
    monkeypatch.setattr('discover.metadata.search_shows', unexpected)
    monkeypatch.setattr('utilities.path_mappings.apply_sports_mapping', unexpected)
    profile = add_profile(source_db, [('en', 'False', 'False')])
    source, library, root = add_source(source_db, tmp_path, content_kind='sports', folder='Sports', profile_id=profile)
    video(root / '2026-05-24 ungrouped.mkv')
    for name, _ in FIXTURES:
        video(root / name)

    statements = []
    engine = source_db.get_bind()

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    sa.event.listen(engine, 'before_cursor_execute', record)
    try:
        (scan,) = scanner.run_scan(source)
    finally:
        sa.event.remove(engine, 'before_cursor_execute', record)
    assert scan.status == 'ok' and scan.inserted == len(FIXTURES)
    assert not any('table_sports_' in stmt for stmt in statements)
    leagues = {row.id: row for row in repository.library_groups(library)}
    assert {league.kind for league in leagues.values()} == {'league'}
    items = {row.path: row for row in repository.list_items(library_id=library)}
    for name, (date, year, season, round_number, title, part_name, status) in FIXTURES:
        event = items[str(root / name)]
        assert leagues[event.group_id].title == Path(name).parts[0]
        assert (event.kind, event.event_date, event.year, event.season, event.episode, event.title,
                event.part_name, event.identity_state) == (
                    'sports_event', date, str(year) if year else None, season, round_number, title, part_name, status)
        context = resolve_item(event.id)
        assert context.year == (int(date[:4]) if date else year)
        assert context.publication_type == 'sports'

    eligible = source_db.scalars(wanted.wanted_list_items(
        'sports_event', source_db, repository.Items.c.path)).all()
    assert set(eligible) == {str(root / name) for name, expected in FIXTURES if expected[-1] == 'parsed'}

    original = next(row for row in leagues.values() if row.title == 'Formula 1')
    (root / 'Formula 1').rename(root / 'Formula One')
    assert scanner.run_scan(source)[0].status == 'ok'
    renamed = repository.get_group(original.id)
    assert renamed.kind == 'league' and renamed.title == renamed.parsed_title == 'Formula One'
    assert not renamed.identify_pending


def _events(session, root, *, name='Local Sports'):
    from media_source_helpers import add_group, add_item

    source, library, folder = add_source(session, root, content_kind='sports', name=name, folder=name, profile_id=1)
    league = add_group(session, source, library, kind='league', title='Z Local League')
    dated = add_item(session, source, library, video(folder / 'Dated.mkv'), kind='sports_event', group_id=league,
                     title='Race Day', event_date='2026-05-24', year='2026', season=2026, part_number=3,
                     part_name='Race', missing_subtitles="['en']")
    round_id = add_item(session, source, library, video(folder / 'Round.mkv'), kind='sports_event', group_id=league,
                        title='Championship', year='2025', season=2025, episode=18, missing_subtitles="['en']")
    return source, league, dated, round_id


def test_leagues_union_keeps_legacy_owners_and_scopes_group_events(client, arr_library, tmp_path, monkeypatch):
    import sqlalchemy as sa
    from app.config import settings
    from app.database import TableArrInstances, TableSportsLeagues
    from media_source_helpers import add_item
    from media_sources import repository

    arr_library.execute(sa.insert(TableArrInstances).values(
        id=4, kind='sportarr', name='Other Sportarr', stable_key='sportarr-secondary', port=9004,
        enabled=1, is_default=0, path_mappings='[]'))
    arr_library.execute(sa.insert(TableSportsLeagues).values(
        id=2, arr_instance_id=4, sportarrLeagueId=31, title='Other League', sortTitle='other league',
        monitored='True', path='/other/league'))

    def get(route, **query):
        response = client.get('/api/' + route, query_string=query, headers=HEADERS)
        assert response.status_code == 200, response.get_json()
        return response

    baseline = get('sports/leagues').data
    assert get('sports/leagues', sources=1).data == baseline
    assert [row['id'] for row in get('sports/leagues', **{'source_key[]': 'arr:4'}).json['data']] == [2]
    for route in ('wanted', 'history', 'blacklist'):
        assert get(f'sports/{route}', **{'source_key[]': 'arr:3'}).json['total'] == 1
        assert get(f'sports/{route}', **{'source_key[]': 'arr:4'}).json['data'] == []
        assert get(f'sports/{route}', arr_instance_id=3, **{'source_key[]': 'arr:4'}).json['data'] == []

    owner, league, dated, round_id = _events(arr_library, tmp_path)
    group = repository.get_group(league, arr_library)
    early = add_item(arr_library, owner, group.library_id, video(tmp_path / 'earlier.mkv'),
                     kind='sports_event', group_id=league, title='Earlier Race', event_date='2026-01-01', season=2026)
    unknown = add_item(arr_library, owner, group.library_id, video(tmp_path / 'unknown.mkv'),
                       kind='sports_event', group_id=league, title='Unknown Race')
    other_owner, _, _, _ = _events(arr_library, tmp_path, name='Other Folder')
    assert get('sports/leagues').data == baseline
    mixed = get('sports/leagues', sources=1, length=1).json
    assert mixed['total'] == 4
    full = get('sports/leagues', sources=1, length=-1).json['data']
    assert ('arr', 1) in {(row['origin'], row['id']) for row in full}
    assert ('source', league) in {(row['origin'], row['id']) for row in full}
    scoped = get('sports/leagues', sources=1, **{'source_key[]': f'source:{owner}'}).json['data']
    assert len(scoped) == 1 and scoped[0]['source_key'] == f'source:{owner}'
    assert scoped[0]['eventFileCount'] == 4
    assert get('sports/leagues', sources=1, arr_instance_id=3, **{'source_key[]': 'arr:4'}).json['data'] == []

    events = get(f'sources/groups/{league}/items').json['data']
    assert [item['id'] for item in events] == [early, dated, round_id, unknown]
    assert all(item['kind'] == 'sports_event' and item['group_id'] == league for item in events)
    assert get(f'sources/groups/{league}', kind='league').json['title'] == 'Z Local League'
    assert client.get(f'/api/sources/groups/{league}?kind=show', headers=HEADERS).status_code == 404
    monkeypatch.setattr('api.sources.items.delete_subtitles', lambda *_, **__: pytest.fail('Wrong-owner delete'))
    exclude = client.post(f'/api/sources/items/{dated}/blacklist', headers=HEADERS, data={
        'source_id': other_owner, 'subtitles_path': '/fixture/race.en.srt', 'provider': 'fixture',
        'language': 'en', 'subs_id': 'recorded-release',
    })
    assert exclude.status_code == 409
    hits = get('system/searches', sources=1, query='local league').json
    assert {hit['detail_url'] for hit in hits} == {f'/sports/source/{row["id"]}' for row in full
                                                   if row['origin'] == 'source'}
    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    assert all(row['origin'] == 'source' for row in get('sports/leagues', sources=1).json['data'])


def test_sports_activity_unions_keep_legacy_shape_filter_and_page(client, arr_library, tmp_path, monkeypatch):
    from datetime import datetime
    from unittest.mock import Mock

    import sqlalchemy as sa
    from app.database import TableBlacklistSports, TableHistorySports
    from media_sources import repository

    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    endpoints = ('wanted', 'history', 'blacklist')

    def get(endpoint, **params):
        response = client.get(f'/api/sports/{endpoint}', query_string=params, headers=HEADERS)
        assert response.status_code == 200, response.get_json()
        return response

    previous = {endpoint: get(endpoint).data for endpoint in endpoints}
    assert {endpoint: get(endpoint, sources=1).data for endpoint in endpoints} == previous
    ownerless = client.delete('/api/sports/blacklist?sources=1', headers=HEADERS)
    assert ownerless.status_code == 400
    assert get('blacklist').data == previous['blacklist']
    owner, league, dated, round_id = _events(arr_library, tmp_path)
    rows = {}
    at = datetime(2027, 1, 1)
    for item_id, title in ((dated, 'Race Day'), (round_id, 'Championship')):
        base = dict(item_id=item_id, source_id=owner, item_kind='sports_event',
                    source_name='Local Sports', item_title=title, group_title='Z Local League',
                    video_path=f'/retained/{title}.mkv', provider='alpha', subs_id=f'sp{item_id}',
                    language='en', timestamp=at)
        rows[item_id] = repository.insert_blacklist(arr_library, **base)
        repository.insert_history(arr_library, action=1, description='Downloaded', **base)

    assert {endpoint: get(endpoint).data for endpoint in endpoints} == previous
    for endpoint in endpoints:
        full = get(endpoint, sources=1).get_json()
        assert full['total'] == 3
        assert [row['id'] for row in full['data']] == (
            [1, round_id, dated] if endpoint == 'wanted' else [2, 1, 1])
        assert [row['origin'] for row in full['data']] == (
            ['arr', 'source', 'source'] if endpoint == 'wanted' else ['source', 'source', 'arr'])
        page = get(endpoint, sources=1, start=1, length=1).get_json()
        assert page == {'data': [full['data'][1]], 'total': 3}
        assert get(endpoint, sources=1, start=1, length=-1).get_json()['data'] == full['data'][1:]
        selected = get(endpoint, sources=1, **{'source_key[]': f'source:{owner}'}).get_json()['data']
        assert len(selected) == 2 and all(row['source_key'] == f'source:{owner}' for row in selected)
        assert all(row['source_group_id'] == league for row in selected)
        if endpoint == 'wanted':
            assert selected[0]['missing_subtitles'][0]['code2'] == 'en'
            assert selected[0]['mapped_path'] == selected[0]['path']
            assert all(selected[0][key] is None for key in ('arr_instance_id', 'sportarrLeagueId', 'sportarrEventId'))
        assert get(endpoint, **{'source_key[]': f'source:{owner}'}).get_json()['total'] == 0
        assert get(endpoint, sources=1, arr_instance_id=3).get_json()['total'] == 1

    arr_library.execute(sa.delete(repository.Items).where(repository.Items.c.id == round_id))
    rows_after = get('blacklist', sources=1).get_json()['data']
    assert rows_after[0]['source_item_id'] is None and rows_after[0]['source_group_id'] is None
    assert rows_after[0]['title'] == 'Championship'
    assert rows_after[0]['video_path'] == '/retained/Championship.mkv'
    response = client.delete(f'/api/sports/blacklist/{rows[dated]}', headers=HEADERS,
                             query_string={'sources': 1, 'origin': 'source'})
    assert response.status_code == 200 and response.get_json() == {'removed': True}
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(TableBlacklistSports)) == 1
    response = client.delete('/api/sports/blacklist', headers=HEADERS,
                             query_string={'sources': 1, 'source_key[]': f'source:{owner}'})
    assert response.status_code == 200 and response.get_json() == {'removed': 1}
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(TableBlacklistSports)) == 1
    repository.insert_blacklist(arr_library, source_id=owner, item_kind='sports_event', source_name='Local Sports')
    response = client.delete('/api/sports/blacklist?sources=1', headers=HEADERS)
    assert response.status_code == 200 and response.get_json() == {'removed': 2}
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(TableHistorySports)) == 1
    repository.insert_blacklist(arr_library, item_kind='sports_event', source_name='Removed Sports')
    detached = get('blacklist', sources=1, **{'source_key[]': 'source:removed'}).get_json()['data']
    assert len(detached) == 1 and detached[0]['source_name'] == 'Removed Sports'
    assert detached[0]['source_item_id'] is None and detached[0]['source_group_id'] is None

    from media_sources.context import resolve_item
    from media_sources.history import blacklist_log_source

    blacklist_log_source(resolve_item(dated), 'fake', 'race-release', 'en')
    source_filter = {'source_key[]': f'source:{owner}'}
    before_correction = get('blacklist', sources=1, **source_filter).get_json()['data']
    assert before_correction[0]['title'] == 'Race Day Race'
    correction = client.patch(f'/api/sources/items/{dated}', headers=HEADERS, json={
        'title': 'Corrected Race Day', 'part_name': 'Qualifying',
    })
    assert correction.status_code == 200
    after_correction = get('blacklist', sources=1, **source_filter).get_json()['data']
    assert after_correction[0]['title'] == before_correction[0]['title']
    arr_library.execute(sa.delete(repository.Items).where(repository.Items.c.id == dated))
    retained = get('blacklist', sources=1, **{'source_key[]': f'source:{owner}'}).get_json()['data']
    assert len(retained) == 1 and retained[0]['title'] == 'Race Day Race'


def test_source_only_sports_badge_and_search_do_not_require_a_sportarr_owner(
        client, arr_library, tmp_path, monkeypatch, isolated_jobs):
    from app.config import settings
    from media_sources import service, wanted

    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    owner, _, dated, round_id = _events(arr_library, tmp_path)
    assert client.get('/api/badges', headers=HEADERS).get_json()['sports'] == 0
    assert client.get('/api/badges?sources=1', headers=HEADERS).get_json()['sports'] == 2
    response = client.get('/api/sports/wanted?sources=1', headers=HEADERS)
    assert response.status_code == 200
    assert [row['origin'] for row in response.get_json()['data']] == ['source', 'source']
    _events(arr_library, tmp_path, name='Another source')
    before = client.post('/api/sports/wanted', headers=HEADERS, json={},
                         query_string={'sources': 1, 'source_key[]': 'source:removed'})
    assert before.status_code == 200 and before.get_json()['job_id'] is None
    response = client.post('/api/sports/wanted', headers=HEADERS, json={},
                           query_string={'sources': 1, 'source_key[]': f'source:{owner}'})
    assert response.status_code == 202
    queued = isolated_jobs._reserve_next_job()
    assert queued.job_id == response.get_json()['job_id']
    assert (queued.kwargs['kind'], queued.kwargs['source_ids']) == ('sports_event', (owner,))
    searched = []
    monkeypatch.setattr(service, 'search_wanted_item',
                        lambda item_id, *_, expected_source_id: searched.append((item_id, expected_source_id)))
    assert isolated_jobs._run_job(queued)
    assert searched == [(dated, owner), (round_id, owner)]


def test_sports_item_override_controls_provider_identity(client, arr_library, tmp_path):
    from media_sources.context import resolve_item

    _, _, dated, round_id = _events(arr_library, tmp_path)
    response = client.patch(f'/api/sources/items/{dated}', headers=HEADERS, json={
        'title': 'Test Grand Prix', 'event_date': '2027-05-21', 'round': 10, 'part_name': 'Qualifying',
    })
    assert response.status_code == 200
    assert response.get_json()['identity_source'] == 'user'
    assert response.get_json()['event_date'] == '2027-05-21'
    assert response.get_json()['round'] == 10
    context = resolve_item(dated)
    assert (context.title, context.year) == ('Test Grand Prix Qualifying', 2027)
    for patch in ({'event_date': '2026-02-29'}, {'round': None}, {'candidate_id': 65536}):
        assert client.patch(f'/api/sources/items/{round_id}', headers=HEADERS, json=patch).status_code == 400


def test_scanned_events_reach_the_sports_threshold_and_publish_through_the_source_pool(
        source_db, tmp_path, monkeypatch, languages_loaded, isolated_jobs):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import sqlalchemy as sa
    from app.config import settings
    from media_sources import history, indexer, repository, scanner, service, wanted
    from media_sources.context import resolve_item
    from media_servers import dispatcher
    from subliminal_patch.score import MAX_SCORES
    from subzero.language import Language
    from subtitles import download, pool, processing

    monkeypatch.setattr(settings.general, 'use_sonarr', False)
    monkeypatch.setattr(settings.general, 'use_radarr', False)
    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    monkeypatch.setattr(settings.general, 'minimum_score_movie', 5)
    monkeypatch.setattr(settings.general, 'minimum_score_sports', 65)
    monkeypatch.setattr(settings.general, 'adaptive_searching', False)
    monkeypatch.setattr(indexer, 'get_language_set', lambda: {Language('eng')})
    monkeypatch.setattr(indexer, 'probe_item', lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(download.subliminal, 'region', Mock())
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: False)
    monkeypatch.setattr(processing, 'call_external_webhook', Mock())
    monkeypatch.setattr(wanted, 'jobs_queue', isolated_jobs)
    monkeypatch.setattr(service, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr('app.event_handler.socketio.emit', Mock())
    monkeypatch.setattr('app.notifier.send_notifications_source', Mock())

    def unexpected(*args, **kwargs):
        pytest.fail('A folder sports recording was passed to a Sportarr provider path')

    for target in ('sportarr.video.get_sports_video', 'sportarr.subtitles.get_blacklist_sports',
                   'sportarr.subtitles.validate_context', 'sportarr.sync.leagues.require_sportarr',
                   'utilities.path_mappings.apply_sports_mapping', 'discover.metadata.search_shows'):
        monkeypatch.setattr(target, unexpected)
    published = []
    monkeypatch.setattr(dispatcher, 'notify_subtitle_mutation', published.append)
    profile = add_profile(source_db, [('en', 'False', 'False')])
    source_id, library, root = add_source(source_db, tmp_path, content_kind='sports', profile_id=profile)
    paths = [
        video(root / 'Formula 1' / '2028-05-24 Monaco Grand Prix Race.mkv'),
        video(root / 'Formula 1' / '2028.05.24 Monaco Grand Prix Qualifying.mkv'),
        video(root / 'NFL' / '2025-2026' / 'Week 18 Bears vs Packers.mkv'),
        video(root / 'NFL' / 'Season 3' / 'Game 12.mkv'),
    ]
    assert scanner.run_scan(source_id)[0].inserted == len(paths)
    items = {row.path: row for row in repository.list_items(library_id=library)}
    race = resolve_item(items[str(paths[0])].id)
    history.blacklist_log_source(race, 'fake', 'excluded-sports-release', 'en')

    seen = {}

    def get_pool(media_type, profile_id, *, source):
        assert media_type == 'movie' and profile_id == profile
        assert pool._pool_key(media_type, profile, None, source) == f'source_sports_event_{profile}'
        assert pool._blacklist(media_type, source=source) == [('fake', 'excluded-sports-release')]
        return SimpleNamespace(providers=['fake'], discarded_providers=[])

    def choose(videos, languages, min_score, **kwargs):
        (recording,) = videos
        assert not hasattr(recording, 'sports_context') and recording.imdb_id is None
        assert recording.size == 1000
        assert min_score == int(MAX_SCORES['movie'] * 65 / 100)
        seen[recording.title] = recording.year
        score = min_score - 1 if recording.title.endswith('Qualifying') else min_score + 1
        return {recording: [pipeline.FakeSubtitle(score=score)]} if score >= min_score else {}

    monkeypatch.setattr(pool, '_update_pool', lambda *args: False)
    monkeypatch.setattr(download, '_get_pool', get_pool)
    monkeypatch.setattr(download, 'download_best_subtitles', choose)
    job_id = wanted.queue_wanted_search('sports_event')
    job = isolated_jobs._reserve_next_job()
    assert job is not None and job.job_id == job_id and isolated_jobs._run_job(job)
    assert seen == {'Monaco Grand Prix Race': 2028, 'Monaco Grand Prix Qualifying': 2028,
                    'Bears vs Packers': 2025}

    assert paths[0].with_suffix('.en.srt').read_bytes() == pipeline.CONTENT
    assert not paths[1].with_suffix('.en.srt').exists()
    assert paths[2].with_suffix('.en.srt').read_bytes() == pipeline.CONTENT
    assert not paths[3].with_suffix('.en.srt').exists()
    assert {(event.media_type, event.operation, event.arr_instance_id, event.source_item_id) for event in published} == {
        ('sports', 'download', None, items[str(path)].id) for path in (paths[0], paths[2])}
    rows = source_db.execute(sa.select(repository.History)).all()
    assert {(row.item_kind, row.action, row.video_path) for row in rows} == {
        ('sports_event', 1, str(path)) for path in (paths[0], paths[2])}


def test_source_and_sportarr_videos_share_the_sports_fields_but_keep_separate_pools(
        arr_library, tmp_path, monkeypatch):
    import sqlalchemy as sa
    from app.database import TableSportsEvents
    from media_sources import history, repository
    from media_sources.analysis import local_content_stamp, stamp_index
    from media_sources.context import resolve_item
    from sportarr.identity import resolve_event_in_session
    from sportarr import video as sportarr_video
    from subtitles import pool
    from subtitles.utils import get_video

    _, _, dated, _ = _events(arr_library, tmp_path)
    source = resolve_item(dated)
    recording_data = {'ffprobe': {'duration': 90}}
    repository.update_item(dated, arr_library, ffprobe_cache=stamp_index(recording_data, local_content_stamp(source.path)))
    arr_library.execute(sa.update(TableSportsEvents).values(
        path=source.path, title=source.title, eventDate=source.event_date))
    owner = resolve_event_in_session(arr_library, 1, 3)
    monkeypatch.setattr(sportarr_video, 'parse_sports_video_metadata', lambda *args, **kwargs: recording_data)

    source_video = get_video(source.path, source.title, 'None', providers=[], media_type='movie', source=source)
    arr_video = sportarr_video.get_sports_video(source.path, source.title, 'None', [], owner)
    assert (source_video.title, source_video.year, source_video.imdb_id, source_video.size,
            source_video.duration) == ('Race Day Race', 2026, None, 1000, 90)
    assert (source_video.title, source_video.year, source_video.imdb_id, source_video.size,
            source_video.duration) == (arr_video.title, arr_video.year, arr_video.imdb_id, arr_video.size,
                                       arr_video.duration)
    assert source_video.source_context is source
    assert arr_video.sports_context is owner

    history.blacklist_log_source(source, 'fake', 'source-only', 'en')
    assert pool._blacklist('movie', source=source) == [('fake', 'source-only')]
    assert pool._blacklist('sports', context=owner) == [('alpha', 'sp1')]
    assert pool._pool_key('movie', source.profile_id, None, source) != pool._pool_key('sports', source.profile_id, owner)


def test_manual_sports_uses_the_same_floor_and_rechecks_it_at_download(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch):
    from app.config import settings
    from media_sources import repository, service
    from media_sources.context import resolve_item
    from app.jobs_queue import JobFailed
    from subliminal_patch.score import MAX_SCORES
    from subtitles import manual

    _, _, dated, _ = _events(arr_library, tmp_path)
    expected_source = resolve_item(dated)
    repository.update_item(dated, arr_library, identity_state='unmatched')
    unattended = client.post(f'/api/sources/items/{dated}/search', headers=HEADERS)
    assert unattended.status_code == 409 and unattended.get_json()['error'] == 'incomplete_identity'
    with pytest.raises(JobFailed, match='sports event identity'):
        service.search_source_item(dated, job_id=1, expected_source=expected_source)
    monkeypatch.setattr(settings.general, 'minimum_score_sports', 65)
    monkeypatch.setattr(settings.general, 'minimum_score_movie', 0)
    maximum = MAX_SCORES['movie']
    high, low = pipeline.FakeSubtitle(score=int(maximum * 0.8)), pipeline.FakeSubtitle(score=int(maximum * 0.5))
    for result in (high, low):
        result.matches = {'title'}
        result.page_link = 'https://example.invalid/release'

    def search(videos, languages, pool, **kwargs):
        (recording,) = videos
        return {recording: [high, low]}

    def score(matches, subtitle, video, **kwargs):
        return subtitle.score, subtitle.score

    def unexpected(*args, **kwargs):
        pytest.fail('A result below the sports minimum reached the provider')

    monkeypatch.setattr(manual, 'list_all_subtitles', search)
    monkeypatch.setattr(manual, 'compute_score', score)
    monkeypatch.setattr(manual, 'download_subtitles', unexpected)
    url = f'/api/sources/items/{dated}/manual'
    (choice,) = client.get(url, headers=HEADERS).get_json()['data']
    assert choice['orig_score'] == high.score
    correction = client.patch(f'/api/sources/items/{dated}', headers=HEADERS, json={
        'title': 'Race Day', 'part_name': 'Qualifying',
    })
    assert correction.status_code == 200
    response = client.post(url, headers=HEADERS, json={'provider': 'fake', 'subtitle': choice['subtitle']})
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id'] and not isolated_jobs._run_job(job)
    assert job.error['reason'] == 'identity_changed'

    (choice,) = client.get(url, headers=HEADERS).get_json()['data']
    monkeypatch.setattr(settings.general, 'minimum_score_sports', 90)
    response = client.post(url, headers=HEADERS, json={'provider': 'fake', 'subtitle': choice['subtitle']})
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id'] and not isolated_jobs._run_job(job)
    assert job.error['reason'] == 'below_minimum_score'


def test_sportarr_search_all_intersects_the_explicit_owner_with_the_filter(client, monkeypatch):
    from sportarr import workflows

    queued = []

    def queue(arr_instance_id):
        queued.append(arr_instance_id)
        return 91

    monkeypatch.setattr(workflows, 'wanted_search_missing_subtitles_sports', queue)
    for key in ('arr:4', 'source:1', 'source:removed'):
        response = client.post('/api/sports/wanted', headers=HEADERS, json={'arr_instance_id': 3},
                               query_string={'sources': 1, 'source_key[]': key})
        assert response.status_code == 200 and response.get_json()['job_id'] is None
    assert queued == []
    response = client.post('/api/sports/wanted', headers=HEADERS, json={'arr_instance_id': 3},
                           query_string=[('sources', 1), ('source_key[]', 'source:1'), ('source_key[]', 'arr:3')])
    assert response.status_code == 202 and response.get_json()['job_id'] == 91
    assert queued == [3]


def test_a_sports_identity_edit_during_download_refuses_publication(
        client, arr_library, writes, isolated_jobs, tmp_path, monkeypatch):
    import sqlalchemy as sa
    from media_sources import repository
    from subtitles import manual
    from subliminal_patch.score import MAX_SCORES

    _, _, dated, _ = _events(arr_library, tmp_path)
    path = Path(repository.get_item(dated, arr_library).path)
    candidate = pipeline.FakeSubtitle(score=MAX_SCORES['movie'])
    candidate.matches = {'title', 'year'}
    candidate.page_link = 'https://example.invalid/release'
    corrections = []

    def search(videos, languages, pool, **kwargs):
        (recording,) = videos
        return {recording: [candidate]}

    def edit_during_download(subtitles, pool):
        (subtitle,) = subtitles
        assert subtitle.source_context.title == 'Race Day Race'
        correction = client.patch(f'/api/sources/items/{dated}', headers=HEADERS, json={
            'title': 'Race Day', 'part_name': 'Qualifying',
        })
        assert correction.status_code == 200
        corrections.append(correction.get_json()['part_name'])

    monkeypatch.setattr(manual, 'list_all_subtitles', search)
    monkeypatch.setattr(manual, 'compute_score', lambda *args, **kwargs: (candidate.score, candidate.score))
    monkeypatch.setattr(manual, 'download_subtitles', edit_during_download)
    url = f'/api/sources/items/{dated}/manual'
    (choice,) = client.get(url, headers=HEADERS).get_json()['data']
    response = client.post(url, headers=HEADERS, json={'provider': 'fake', 'subtitle': choice['subtitle']})
    job = isolated_jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert not isolated_jobs._run_job(job)
    assert corrections == ['Qualifying'] and job.error['reason'] == 'identity_changed'
    assert not path.with_suffix('.en.srt').exists()
    assert writes == []
    assert arr_library.scalar(sa.select(sa.func.count()).select_from(repository.History)) == 0
