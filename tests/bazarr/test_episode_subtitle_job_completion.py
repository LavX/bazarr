"""Episode actions return the job whose completion changes subtitle history."""

import pytest
from flask import Flask


@pytest.fixture
def private_queue(monkeypatch):
    from app import jobs_queue as jobs_module

    class NoThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(jobs_module, 'Thread', NoThread)
    monkeypatch.setattr(jobs_module, 'event_stream', lambda **kwargs: None)
    return jobs_module.JobsQueue()


@pytest.fixture
def episode_queue(private_queue, schema_session, monkeypatch):
    from api.episodes import blacklist
    from app.database import TableEpisodes, TableShows
    from subtitles.mass_download import series as downloader
    from utilities import job_dedupe
    from subtitles.tools import delete_ownership

    schema_session.add(TableShows(id=501, sonarrSeriesId=42, arr_instance_id=1,
                                 path='/tv/show', title='Show', tags='[]'))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=9001, series_id=501, sonarrSeriesId=42, sonarrEpisodeId=100,
                                    arr_instance_id=1, path='/tv/episode.mkv', title='Episode', season=1, episode=1,
                                    subtitles=repr([['en', '/tv/episode.en.srt', 1]])))
    schema_session.flush()

    queue = private_queue
    monkeypatch.setattr(downloader, 'jobs_queue', queue)
    monkeypatch.setattr(job_dedupe, 'jobs_queue', queue)
    monkeypatch.setattr(downloader, 'database', schema_session)
    monkeypatch.setattr(blacklist, 'database', schema_session)
    monkeypatch.setattr(blacklist, 'blacklist_log', lambda **kwargs: None)
    monkeypatch.setattr(blacklist, 'delete_subtitles', lambda **kwargs: True)
    monkeypatch.setattr(blacklist, 'event_stream', lambda **kwargs: None)
    monkeypatch.setattr(delete_ownership.path_mappings, 'path_replace_instance', lambda value, *args: value)
    return queue, downloader


def _start_job(queue):
    job = queue.jobs_pending_queue.popleft()
    job.status = 'running'
    job.kwargs['job_id'] = job.job_id
    queue.jobs_running_queue.append(job)


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_exclude_returns_the_webhook_search_job_already_in_the_queue(episode_queue, status):
    from api.episodes import blacklist

    queue, downloader = episode_queue
    existing_id = downloader.episode_download_subtitles(no=100, arr_instance_id=1)
    if status == 'running':
        _start_job(queue)

    app = Flask(__name__)
    with app.test_request_context('/api/episodes/blacklist?seriesid=42&episodeid=100&provider=p&subs_id=s&language=en'
                                 '&subtitles_path=/tv/episode.en.srt&arr_instance_id=1', method='POST'):
        result = blacklist.EpisodesBlacklist.post.__wrapped__(blacklist.EpisodesBlacklist())

    assert isinstance(existing_id, int)
    assert result == ({'job_id': existing_id}, 200)
    assert len(queue.jobs_pending_queue) + len(queue.jobs_running_queue) == 1


@pytest.mark.parametrize('status', [None, 'pending', 'running'])
def test_download_returns_the_new_or_existing_job_to_watch(episode_queue, status):
    from api.episodes import episodes_subtitles

    queue, downloader = episode_queue
    if status:
        existing_id = downloader.episode_download_specific_subtitles(
            42, 100, 'en', 'False', 'False', arr_instance_id=1)
        if status == 'running':
            _start_job(queue)

    app = Flask(__name__)
    with app.test_request_context('/api/episodes/subtitles?seriesid=42&episodeid=100&language=en'
                                 '&forced=false&hi=false&arr_instance_id=1', method='PATCH'):
        result = episodes_subtitles.EpisodesSubtitles.patch.__wrapped__(episodes_subtitles.EpisodesSubtitles())

    active = list(queue.jobs_pending_queue) + list(queue.jobs_running_queue)
    assert len(active) == 1
    assert result == ({'job_id': active[0].job_id}, 202)
    assert active[0].kwargs['arr_instance_id'] == 1
    if status:
        assert result[0]['job_id'] == existing_id


@pytest.mark.parametrize('kind', ['missing', 'specific'])
def test_search_does_not_reuse_a_job_owned_by_another_instance(episode_queue, kind):
    queue, downloader = episode_queue

    def enqueue(instance_id):
        if kind == 'missing':
            return downloader.episode_download_subtitles(no=100, arr_instance_id=instance_id)
        return downloader.episode_download_specific_subtitles(42, 100, 'en', 'False', 'False',
                                                              arr_instance_id=instance_id)

    first = enqueue(1)
    second = enqueue(2)
    assert first != second
    assert enqueue(1) == first
    assert enqueue(2) == second
    assert len(queue.jobs_pending_queue) == 2


@pytest.mark.parametrize('status', [None, 'pending', 'running'])
def test_manual_download_returns_the_new_or_existing_job(private_queue, monkeypatch, status):
    from api.providers import providers_episodes
    from subtitles import manual

    monkeypatch.setattr(manual, 'jobs_queue', private_queue)
    app = Flask(__name__)

    def request_download():
        with app.test_request_context('/api/providers/episodes', method='POST', query_string={
            'seriesid': 42, 'episodeid': 100, 'arr_instance_id': 1,
            'hi': 'false', 'forced': 'false', 'original_format': 'false',
            'provider': 'p', 'subtitle': 's',
        }):
            return providers_episodes.ProviderEpisodes.post.__wrapped__(providers_episodes.ProviderEpisodes())

    if status:
        existing = request_download()[0]['job_id']
        if status == 'running':
            _start_job(private_queue)

    result = request_download()
    active = list(private_queue.jobs_pending_queue) + list(private_queue.jobs_running_queue)
    assert len(active) == 1
    assert result == ({'job_id': active[0].job_id}, 202)
    assert active[0].kwargs['arr_instance_id'] == 1
    if status:
        assert result[0]['job_id'] == existing


@pytest.fixture
def subtitle_tool_request(episode_queue, schema_session, monkeypatch, tmp_path):
    from api.subtitles import subtitles as endpoint
    from app.database import TableEpisodes
    from subtitles import sync
    from subtitles.tools import mods
    from subtitles.tools.translate import main as translate

    queue, _ = episode_queue
    episode = schema_session.get(TableEpisodes, 9001)
    episode.path = str(tmp_path / 'episode.mkv')
    episode.subtitles = repr([['en', str(tmp_path / 'episode.en.srt'), 1]])
    schema_session.flush()
    (tmp_path / 'episode.mkv').touch()
    (tmp_path / 'episode.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n')

    monkeypatch.setattr(endpoint, 'database', schema_session)
    monkeypatch.setattr(endpoint, 'alpha3_from_alpha2', lambda code: {'en': 'eng', 'cs': 'ces'}.get(code))
    monkeypatch.setattr(endpoint.path_mappings, 'path_replace_instance', lambda path, *args: path)
    for module in (sync, mods, translate):
        monkeypatch.setattr(module, 'jobs_queue', queue)
    monkeypatch.setattr(translate, 'get_title', lambda *args, **kwargs: 'Show')
    app = Flask(__name__)

    def request_tool(action, media_type='episode'):
        with app.test_request_context('/api/subtitles', method='PATCH', query_string={
            'action': action, 'type': media_type, 'id': 100, 'arr_instance_id': 1,
            'language': 'cs', 'from_language': 'en', 'path': str(tmp_path / 'episode.en.srt'),
            'hi': 'false', 'forced': 'false',
        }):
            return endpoint.Subtitles.patch.__wrapped__(endpoint.Subtitles())

    return queue, request_tool


@pytest.mark.parametrize('action', ['sync', 'translate', 'remove_HI'])
def test_subtitle_tools_expose_the_native_queued_job(subtitle_tool_request, action):
    queue, request_tool = subtitle_tool_request
    result = request_tool(action)

    assert len(queue.jobs_pending_queue) == 1
    job = queue.jobs_pending_queue[0]
    assert isinstance(job.job_id, int)
    assert result == ({'job_id': job.job_id}, 202)
    assert job.kwargs['arr_instance_id'] == 1


@pytest.mark.parametrize('action', ['sync', 'translate', 'remove_HI'])
def test_movie_tools_keep_their_existing_http_response(subtitle_tool_request, schema_session, action):
    from app.database import TableEpisodes, TableMovies

    queue, request_tool = subtitle_tool_request
    episode = schema_session.get(TableEpisodes, 9001)
    schema_session.add(TableMovies(id=8001, radarrId=100, tmdbId=100, arr_instance_id=1,
                                  title='Movie', path=episode.path, subtitles=episode.subtitles))
    schema_session.flush()

    result = request_tool(action, media_type='movie')
    assert len(queue.jobs_pending_queue) == 1
    job = queue.jobs_pending_queue[0]
    if action == 'remove_HI':
        assert result == ({'job_id': job.job_id}, 202)
    else:
        assert result == ('', 204)


@pytest.mark.parametrize('action', ['translate', 'remove_HI'])
@pytest.mark.parametrize('status', ['pending', 'running'])
def test_subtitle_tool_reuses_the_active_job(subtitle_tool_request, action, status):
    queue, request_tool = subtitle_tool_request
    existing = request_tool(action)[0]['job_id']
    if status == 'running':
        _start_job(queue)

    assert request_tool(action) == ({'job_id': existing}, 202)
    assert len(queue.jobs_pending_queue) + len(queue.jobs_running_queue) == 1


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_sync_can_return_an_active_job_id_without_changing_internal_outcome_contract(
        private_queue, monkeypatch, status):
    from subtitles import sync

    monkeypatch.setattr(sync, 'jobs_queue', private_queue)

    def enqueue(return_job_id):
        return sync.sync_subtitles(video_path='/tv/episode.mkv', srt_path='/tv/episode.en.srt',
                                   srt_lang='en', hi=False, forced=False, percent_score=0,
                                   force_sync=True, return_job_id=return_job_id)

    existing = enqueue(True)
    assert isinstance(existing, int) and existing > 0
    if status == 'running':
        _start_job(private_queue)
    assert enqueue(True) == existing
    assert len(private_queue.jobs_pending_queue) + len(private_queue.jobs_running_queue) == 1
    assert enqueue(False) is False


@pytest.mark.parametrize('status', [None, 'pending', 'running'])
@pytest.mark.parametrize('action', ['scan-disk', 'search-missing'])
def test_series_action_returns_the_new_or_existing_job(episode_queue, monkeypatch, status, action):
    from api.series import series as endpoint
    from subtitles.indexer import series as indexer

    queue, downloader = episode_queue
    monkeypatch.setattr(indexer, 'jobs_queue', queue)
    if status:
        if action == 'scan-disk':
            existing = indexer.series_scan_disk(42, arr_instance_id=1)
        else:
            existing = downloader.series_download_subtitles(42, arr_instance_id=1)
        if status == 'running':
            _start_job(queue)

    app = Flask(__name__)
    with app.test_request_context('/api/series', method='PATCH', query_string={
        'seriesid': 42, 'arr_instance_id': 1, 'action': action,
    }):
        result = endpoint.Series.patch.__wrapped__(endpoint.Series())

    active = list(queue.jobs_pending_queue) + list(queue.jobs_running_queue)
    assert len(active) == 1
    assert result == ({'job_id': active[0].job_id}, 202)
    if status:
        assert result[0]['job_id'] == existing


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_queue_reuse_preserves_default_deduplication_and_owner_scope(private_queue, status):
    def enqueue(instance_id, return_existing=False):
        return private_queue.feed_jobs_pending_queue(
            'Search', 'subtitles.mass_download.series', 'episode_download_subtitles',
            kwargs={'no': 100, 'arr_instance_id': instance_id}, return_existing=return_existing,
        )

    first = enqueue(1)
    if status == 'running':
        _start_job(private_queue)
    assert enqueue(1) is False
    assert enqueue(1, return_existing=True) == first
    second = enqueue(2, return_existing=True)
    assert second != first
    assert enqueue(2, return_existing=True) == second
    assert len(private_queue.jobs_pending_queue) + len(private_queue.jobs_running_queue) == 2


@pytest.mark.parametrize('selection', ['series', 'episodes', 'mixed'])
def test_search_batch_finishes_each_series_once_before_publishing_completion(
        private_queue, schema_session, monkeypatch, tmp_path, selection):
    from api.subtitles import batch
    from app.database import TableEpisodes, TableShows
    from subtitles import mass_operations
    from subtitles.mass_download import series

    items = []
    expected_work = []
    for owner in (1, 2):
        folder = tmp_path / str(owner)
        folder.mkdir()
        schema_session.add(TableShows(id=500 + owner, sonarrSeriesId=42, arr_instance_id=owner,
                                     path=str(folder), title='Show', tags='[]'))
        schema_session.flush()
        if selection in ('series', 'mixed'):
            items.append({'type': 'series', 'sonarrSeriesId': 42, 'arr_instance_id': owner})
        for number in (100, 101):
            video = folder / f'episode-{number}.mkv'
            video.touch()
            schema_session.add(TableEpisodes(id=owner * 1000 + number, series_id=500 + owner,
                                            sonarrSeriesId=42, sonarrEpisodeId=number, arr_instance_id=owner,
                                            path=str(video), title='Episode', season=1, episode=number,
                                            missing_subtitles="['en']", subtitles='[]', audio_language='[]'))
            expected_work.append((owner, str(video)))
            if selection in ('episodes', 'mixed'):
                items.append({'type': 'episode', 'sonarrSeriesId': 42, 'sonarrEpisodeId': number,
                              'arr_instance_id': owner})
    schema_session.flush()
    for module in (batch, mass_operations, series):
        monkeypatch.setattr(module, 'jobs_queue', private_queue)
    monkeypatch.setattr(series, 'database', schema_session)
    monkeypatch.setattr(series, 'get_providers', lambda: ['provider'])
    monkeypatch.setattr(series, 'get_exclusion_clause', lambda media_type: [])
    monkeypatch.setattr(series, 'get_audio_profile_languages', lambda audio_language: [])
    monkeypatch.setattr(series.path_mappings, 'path_replace_instance', lambda path, *args: path)

    completed_work = []

    def search(path, *args, job_id, arr_instance_id, **kwargs):
        assert job_id == parent.job_id
        assert parent.status == 'running'
        assert parent.job_name == original_name
        assert parent.progress_max == len(items)
        assert not private_queue.jobs_pending_queue
        completed_work.append((arr_instance_id, path))
        # Nothing was found, so the subtitles stay missing on every pass.
        return iter([])

    monkeypatch.setattr(series, 'generate_subtitles', search)
    app = Flask(__name__)
    with app.test_request_context('/api/subtitles/batch', method='POST', json={
        'items': items, 'action': 'search-missing',
    }):
        response, status = batch.BatchOperation.post.__wrapped__(batch.BatchOperation())
    assert status == 200
    parent = private_queue._reserve_next_job()
    original_name = parent.job_name
    assert parent.job_id == response['job_id']

    assert private_queue._run_job(parent) is True
    assert sorted(completed_work) == sorted(expected_work)
    assert parent.status == 'completed'
    assert parent.job_name == original_name
    assert parent.progress_max == len(items)
    assert parent.job_returned_value == {'queued': 2, 'skipped': len(items) - 2, 'errors': []}
    assert not private_queue.jobs_pending_queue
