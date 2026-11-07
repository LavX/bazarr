# coding=utf-8
"""Which runs consume a forced-search request, and when the request is dropped.

The scheduled wanted search, "Search All" and the series "Search" hand a flagged episode to the forced
search. The Sonarr and Plex webhooks, the Sonarr sync and the blacklist API call
``episode_download_subtitles`` without ``forced`` and must leave the request alone. The request is
dropped only when the search reports every language as answered.
"""

import ast
import datetime
import pathlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import insert, select

pytestmark = pytest.mark.usefixtures('languages_loaded')

REQUESTED_AT = datetime.datetime(2026, 11, 1, 12, 0, 0)
BAZARR = pathlib.Path(__file__).resolve().parents[2] / 'bazarr'


def _monitored_only():
    from app.database import TableEpisodes, TableShows

    return [TableEpisodes.monitored == 'True', TableShows.monitored == 'True']


def _seed_library(session, root):
    """One show, five episodes.

    101 flagged, nothing missing (the case a request exists for)
    102 flagged and also missing English
    103 not flagged, missing English
    104 flagged but unmonitored
    105 not flagged, nothing missing
    """
    from app.database import TableEpisodes, TableShows

    session.execute(insert(TableShows).values(
        id=1, sonarrSeriesId=10, arr_instance_id=1, path=str(root), title='Show', monitored='True', tags='[]',
        seriesType='standard', profileId=None))
    rows = [
        (101, 1, REQUESTED_AT, '[]', 'True'),
        (102, 2, REQUESTED_AT, "['en']", 'True'),
        (103, 3, None, "['en']", 'True'),
        (104, 4, REQUESTED_AT, '[]', 'False'),
        (105, 5, None, '[]', 'True'),
    ]
    for local_id, number, flag, missing, monitored in rows:
        episode_file = root / f's01e0{number}.mkv'
        episode_file.write_bytes(b'')
        session.execute(insert(TableEpisodes).values(
            id=local_id, series_id=1, sonarrSeriesId=10, sonarrEpisodeId=local_id, arr_instance_id=1,
            path=str(episode_file), title=f'Episode {number}', season=1, episode=number,
            audio_language="['English']", sceneName='Scene', missing_subtitles=missing, failedAttempts='[]',
            subtitles='[]', monitored=monitored, forced_search_requested_at=flag))
    session.commit()


def _flag(session, episode_id):
    from app.database import TableEpisodes

    session.expire_all()
    return session.execute(select(TableEpisodes.forced_search_requested_at)
                           .where(TableEpisodes.sonarrEpisodeId == episode_id)).scalar()


class _RecordingDatabase:
    """Passes everything to the session and keeps the compiled statements."""

    def __init__(self, session):
        self.session = session
        self.statements = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement.compile(compile_kwargs={'literal_binds': True})))
        return self.session.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.session, name)


@pytest.fixture
def library(schema_session, tmp_path, monkeypatch):
    _seed_library(schema_session, tmp_path)
    return schema_session


@pytest.fixture
def wanted(library, monkeypatch):
    import subtitles.wanted.series as module

    recording = _RecordingDatabase(library)
    monkeypatch.setattr(module, 'database', recording)
    monkeypatch.setattr(module, 'get_exclusion_clause', lambda kind: _monitored_only())
    monkeypatch.setattr(module, 'get_providers', lambda: ['provider'])
    monkeypatch.setattr(module, 'evaluate_translation_gate', lambda: True)
    monkeypatch.setattr(module, 'activity', Mock())
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    monkeypatch.setattr(module.jobs_queue, 'update_job_name', lambda **kwargs: None)
    searched = []
    monkeypatch.setattr(module, 'wanted_download_subtitles',
                        lambda episode_id, **kwargs: searched.append((episode_id, kwargs)))
    return SimpleNamespace(module=module, database=recording, searched=searched)


def test_wanted_run_adds_flagged_episodes_the_missing_query_does_not_return(wanted):
    wanted.module.wanted_search_missing_subtitles_series(job_id=1)

    # 101 has nothing missing and is flagged: only the second query finds it. 104 is unmonitored and stays out.
    assert sorted(episode_id for episode_id, _ in wanted.searched) == [101, 102, 103]


def test_wanted_missing_query_is_untouched_and_the_flag_query_is_separate(wanted, library):
    from app.database import TableEpisodes, TableShows
    from sqlalchemy import select as sql_select
    import operator
    from functools import reduce

    wanted.module.wanted_search_missing_subtitles_series(job_id=1)

    conditions = [TableEpisodes.missing_subtitles.is_not(None), TableEpisodes.missing_subtitles != '[]']
    conditions += _monitored_only()
    expected = str(sql_select(TableEpisodes.sonarrSeriesId,
                              TableEpisodes.sonarrEpisodeId,
                              TableEpisodes.arr_instance_id,
                              TableShows.tags,
                              TableEpisodes.monitored,
                              TableShows.title,
                              TableEpisodes.season,
                              TableEpisodes.episode,
                              TableEpisodes.title.label('episodeTitle'),
                              TableShows.seriesType)
                   .select_from(TableEpisodes)
                   .join(TableShows)
                   .where(reduce(operator.and_, conditions))
                   .compile(compile_kwargs={'literal_binds': True}))
    first, second = wanted.database.statements[:2]
    assert first == expected
    assert 'forced_search_requested_at' not in first
    assert 'forced_search_requested_at IS NOT NULL' in second


def test_wanted_run_lists_an_episode_once_when_both_queries_return_it(wanted):
    wanted.module.wanted_search_missing_subtitles_series(job_id=1)

    ids = [episode_id for episode_id, _ in wanted.searched]
    assert ids.count(102) == 1


def test_unflagged_library_searches_exactly_the_missing_episodes(wanted, library):
    from app.database import TableEpisodes
    from sqlalchemy import update

    library.execute(update(TableEpisodes).values(forced_search_requested_at=None))
    library.commit()

    wanted.module.wanted_search_missing_subtitles_series(job_id=1)

    assert sorted(episode_id for episode_id, _ in wanted.searched) == [102, 103]


def _wanted_episode(flag):
    return SimpleNamespace(
        arr_instance_id=1, audio_language="['English']", failedAttempts='[]', missing_subtitles="['en']",
        path='/series/show/s01e01.mkv', profileId=1, sceneName='Scene', sonarrEpisodeId=20, sonarrSeriesId=10,
        subtitles='[]', title='Show', forced_search_requested_at=flag)


def test_wanted_episode_with_a_request_takes_the_forced_search_only(monkeypatch):
    import subtitles.wanted.series as module

    forced = Mock()
    normal = Mock(return_value=[])
    monkeypatch.setattr(module, 'run_flagged_search', forced)
    monkeypatch.setattr(module, 'generate_subtitles', normal)
    monkeypatch.setattr(module.path_mappings, 'path_replace', lambda value: value)

    module._wanted_episode(_wanted_episode(REQUESTED_AT), ['provider'], job_id=5)

    forced.assert_called_once()
    assert forced.call_args.kwargs == {'job_id': 5}
    normal.assert_not_called()


def test_wanted_episode_without_a_request_searches_as_before(monkeypatch):
    import subtitles.wanted.series as module

    forced = Mock()
    normal = Mock(return_value=[])
    monkeypatch.setattr(module, 'run_flagged_search', forced)
    monkeypatch.setattr(module, 'generate_subtitles', normal)
    monkeypatch.setattr(module, 'get_audio_profile_languages', lambda value: [])
    monkeypatch.setattr(module, 'get_profiles_list', lambda profile_id: None)
    monkeypatch.setattr(module, 'is_search_active', lambda desired_language, attempt_string: True)
    monkeypatch.setattr(module, 'updateFailedAttempts', lambda desired_language, attempt_string: '[]')
    monkeypatch.setattr(module, 'database', Mock())
    monkeypatch.setattr(module.path_mappings, 'path_replace', lambda value: value)

    module._wanted_episode(_wanted_episode(None), ['provider'])

    forced.assert_not_called()
    normal.assert_called_once()


def test_forced_episode_ignores_the_adaptive_window(monkeypatch):
    import subtitles.wanted.series as module

    window = Mock(return_value=False)
    monkeypatch.setattr(module, 'is_search_active', window)
    monkeypatch.setattr(module, 'run_flagged_search', Mock())

    module._wanted_episode(_wanted_episode(REQUESTED_AT), ['provider'])

    window.assert_not_called()


@pytest.fixture
def mass(library, tmp_path, monkeypatch):
    import subtitles.mass_download.series as module

    monkeypatch.setattr(module, 'database', library)
    monkeypatch.setattr(module, 'get_exclusion_clause', lambda kind: _monitored_only())
    monkeypatch.setattr(module, 'get_providers', lambda: ['provider'])
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    monkeypatch.setattr(module.jobs_queue, 'update_job_name', lambda **kwargs: None)
    monkeypatch.setattr(module.path_mappings, 'path_replace_instance',
                        lambda path, arr_instance_id, media_type: path)
    return module


def test_series_search_passes_forced_only_for_flagged_episodes(mass, monkeypatch):
    calls = []
    monkeypatch.setattr(mass, 'episode_download_subtitles', lambda **kwargs: calls.append(kwargs))

    mass.series_download_subtitles(10, job_id=1)

    assert {call['no']: call['forced'] for call in calls} == {101: True, 102: True, 103: False}


def test_series_search_of_an_unflagged_show_is_unchanged(mass, library, monkeypatch):
    from app.database import TableEpisodes
    from sqlalchemy import update

    library.execute(update(TableEpisodes).values(forced_search_requested_at=None))
    library.commit()
    calls = []
    monkeypatch.setattr(mass, 'episode_download_subtitles', lambda **kwargs: calls.append(kwargs))

    mass.series_download_subtitles(10, job_id=1)

    assert [(call['no'], call['forced']) for call in calls] == [(102, False), (103, False)]


def _prepare_episode_search(mass, monkeypatch, *, providers=('provider',)):
    forced = Mock(return_value=SimpleNamespace(complete=True, downloaded=[]))
    normal = Mock(return_value=[])
    monkeypatch.setattr(mass, 'run_flagged_search', forced)
    monkeypatch.setattr(mass, 'generate_subtitles', normal)
    monkeypatch.setattr(mass, 'get_providers', lambda: list(providers))
    monkeypatch.setattr(mass, 'get_audio_profile_languages', lambda value: [])
    return forced, normal


def test_episode_search_without_forced_leaves_a_request_alone(mass, library, monkeypatch):
    forced, normal = _prepare_episode_search(mass, monkeypatch)

    mass.episode_download_subtitles(no=102, job_id=1, job_sub_function=True)

    forced.assert_not_called()
    normal.assert_called_once()
    assert _flag(library, 102) == REQUESTED_AT


def test_episode_search_with_forced_takes_the_forced_search(mass, monkeypatch):
    forced, normal = _prepare_episode_search(mass, monkeypatch)

    mass.episode_download_subtitles(no=101, job_id=1, job_sub_function=True, forced=True)

    forced.assert_called_once()
    assert forced.call_args.kwargs == {'job_id': 1}
    normal.assert_not_called()


def test_episode_search_with_forced_but_no_request_searches_normally(mass, monkeypatch):
    forced, normal = _prepare_episode_search(mass, monkeypatch)

    mass.episode_download_subtitles(no=103, job_id=1, job_sub_function=True, forced=True)

    forced.assert_not_called()
    normal.assert_called_once()


def test_empty_pool_does_not_consume_the_request(mass, library, monkeypatch):
    forced, normal = _prepare_episode_search(mass, monkeypatch, providers=())

    mass.episode_download_subtitles(no=101, job_id=1, job_sub_function=True, forced=True)

    forced.assert_not_called()
    normal.assert_not_called()
    assert _flag(library, 101) == REQUESTED_AT


def test_unmonitored_flagged_episode_is_not_searched_and_keeps_the_request(mass, library, monkeypatch):
    forced, normal = _prepare_episode_search(mass, monkeypatch)

    mass.episode_download_subtitles(no=104, job_id=1, job_sub_function=True, forced=True)

    forced.assert_not_called()
    assert _flag(library, 104) == REQUESTED_AT


def test_default_is_off():
    import inspect
    import subtitles.mass_download.series as mass_series

    assert inspect.signature(mass_series.episode_download_subtitles).parameters['forced'].default is False


@pytest.mark.parametrize('relative', [
    'api/webhooks/sonarr.py',
    'api/webhooks/plex.py',
    'api/episodes/blacklist.py',
    'sonarr/sync/episodes.py',
])
def test_other_callers_do_not_ask_for_the_forced_search(relative):
    tree = ast.parse((BAZARR / relative).read_text())
    found = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', getattr(node.func, 'attr', None))
            if name == 'episode_download_subtitles':
                found += 1
                assert 'forced' not in {keyword.arg for keyword in node.keywords}, relative
        elif isinstance(node, ast.keyword) and node.arg == 'func' \
                and isinstance(node.value, ast.Constant) and node.value.value == 'episode_download_subtitles':
            found += 1
    assert found, f'{relative} no longer calls episode_download_subtitles; update this test'
    if relative == 'sonarr/sync/episodes.py':
        # The sync queues the job by name with kwargs: neither carries `forced`.
        assert "'forced'" not in (BAZARR / relative).read_text().split('episode_download_subtitles', 1)[1][:400]


@pytest.fixture
def flag_runs(library, monkeypatch):
    import subtitles.wanted.forced_flag as module

    monkeypatch.setattr(module, 'database', library)
    monkeypatch.setattr(module, 'event_stream', Mock())
    return module


def _flagged_episode(flag=REQUESTED_AT, episode_id=101):
    return SimpleNamespace(arr_instance_id=1, sonarrEpisodeId=episode_id, sonarrSeriesId=10,
                           path='/series/show/s01e01.mkv', forced_search_requested_at=flag)


def _result(*statuses):
    from subtitles.wanted.forced import ForcedSearchResult, LanguageResult

    return ForcedSearchResult([LanguageResult(f'l{i}', status) for i, status in enumerate(statuses)])


def test_complete_result_clears_the_request(flag_runs, library, monkeypatch):
    monkeypatch.setattr(flag_runs, 'run_forced_search', lambda *args, **kwargs: _result('downloaded', 'finished'))

    result = flag_runs.run_flagged_search(_flagged_episode(), job_id=3)

    assert result.complete
    assert _flag(library, 101) is None
    flag_runs.event_stream.assert_called_once_with(type='series', action='update', payload=10)


def test_incomplete_result_keeps_the_request(flag_runs, library, monkeypatch):
    monkeypatch.setattr(flag_runs, 'run_forced_search', lambda *args, **kwargs: _result('downloaded', 'open'))

    result = flag_runs.run_flagged_search(_flagged_episode(), job_id=3)

    assert not result.complete
    assert _flag(library, 101) == REQUESTED_AT


def test_cutoff_met_counts_as_complete(flag_runs, library, monkeypatch):
    monkeypatch.setattr(flag_runs, 'run_forced_search', lambda *args, **kwargs: _result('cutoff_met'))

    flag_runs.run_flagged_search(_flagged_episode(), job_id=3)

    assert _flag(library, 101) is None


def test_error_keeps_the_request_and_does_not_raise(flag_runs, library, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('provider exploded')

    monkeypatch.setattr(flag_runs, 'run_forced_search', broken)

    assert flag_runs.run_flagged_search(_flagged_episode(), job_id=3) is None
    assert _flag(library, 101) == REQUESTED_AT


def test_stop_propagates_and_keeps_the_request(flag_runs, library, monkeypatch):
    from app.jobs_queue import JobCancelled

    def stopped(*args, **kwargs):
        raise JobCancelled('stopped', job_id=3)

    monkeypatch.setattr(flag_runs, 'run_forced_search', stopped)

    with pytest.raises(JobCancelled):
        flag_runs.run_flagged_search(_flagged_episode(), job_id=3)
    assert _flag(library, 101) == REQUESTED_AT


def test_a_newer_request_made_during_the_run_stays(flag_runs, library, monkeypatch):
    # The run started from an older request than the one now stored.
    older = REQUESTED_AT - datetime.timedelta(hours=1)
    monkeypatch.setattr(flag_runs, 'run_forced_search', lambda *args, **kwargs: _result('downloaded'))

    flag_runs.run_flagged_search(_flagged_episode(flag=older), job_id=3)

    assert _flag(library, 101) == REQUESTED_AT


def test_the_search_gets_the_jobs_stop_signal(flag_runs, monkeypatch):
    seen = {}

    def forced_search(episode, job_id=None, cancel=None):
        seen.update(job_id=job_id, cancel=cancel)
        return _result('downloaded')

    monkeypatch.setattr(flag_runs, 'run_forced_search', forced_search)

    flag_runs.run_flagged_search(_flagged_episode(), job_id=3)

    assert seen['job_id'] == 3
    assert seen['cancel'].job_id == 3


def test_a_stopped_episode_search_reaches_the_series_loop(mass, library, monkeypatch):
    from app.jobs_queue import JobCancelled

    def stopped(*args, **kwargs):
        raise JobCancelled('stopped', job_id=1)

    monkeypatch.setattr(mass, 'run_flagged_search', stopped)
    monkeypatch.setattr(mass, 'generate_subtitles', Mock(return_value=[]))
    monkeypatch.setattr(mass, 'get_audio_profile_languages', lambda value: [])

    with pytest.raises(JobCancelled):
        mass.series_download_subtitles(10, job_id=1)
    assert _flag(library, 101) == REQUESTED_AT


def test_a_stop_set_during_the_last_download_keeps_the_request(flag_runs, library, monkeypatch):
    from app.jobs_queue import JobCancelled

    state = SimpleNamespace(stopped=False)

    class Stop:
        def __init__(self, job_id):
            self.job_id = job_id

        def is_set(self):
            if state.stopped:
                raise JobCancelled('stopped', job_id=self.job_id)
            return False

    def search_that_is_stopped_at_the_end(*args, **kwargs):
        # The search finishes cleanly, but Stop arrived while its last download was running.
        state.stopped = True
        return _result('downloaded')

    monkeypatch.setattr(flag_runs, '_JobStop', Stop)
    monkeypatch.setattr(flag_runs, 'run_forced_search', search_that_is_stopped_at_the_end)

    with pytest.raises(JobCancelled):
        flag_runs.run_flagged_search(_flagged_episode(), job_id=3)
    assert _flag(library, 101) == REQUESTED_AT
