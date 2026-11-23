"""Mixed browse, search and detail as served to the Series and Movies pages."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from media_source_helpers import add_group, add_item, add_profile, add_source
import test_media_sources_no_change as arr_fixtures

HEADERS = arr_fixtures.HEADERS
arr_library = arr_fixtures.arr_library
client = arr_fixtures.client
everything_uses_the_test_database = arr_fixtures.everything_uses_the_test_database


@pytest.fixture
def source_library(arr_library, tmp_path):
    from media_sources import repository

    library = arr_library
    movie_source, movie_library, movies = add_source(library, tmp_path, name='Local Movies', profile_id=1)
    show_source, show_library, shows = add_source(
        library, tmp_path, name='Local Series', folder='Shows', content_kind='series', profile_id=1)
    extra_source, extra_library, extra = add_source(library, tmp_path, name='Other Movies', folder='Other')
    movie = add_item(library, movie_source, movie_library, movies / 'Arr Movie.mkv', title='Arr Movie',
                     year='2026', monitored=0, poster='https://images.example.test/movie.png',
                     subtitles='[["en", "/source/Arr Movie.en.srt", 57, "subrip"]]', missing_subtitles='["en"]')
    show = add_group(library, show_source, show_library, title='Arr Show', key=str(shows / 'Arr Show'),
                     year='2026', poster='https://images.example.test/show.png')
    other_show = add_group(library, show_source, show_library, title='Other Show', identity_state='unmatched')
    episode_two = add_item(library, show_source, show_library, shows / 'two.mkv', kind='episode', group_id=show,
                           title='Episode two', season=1, episode=2)
    episode_one = add_item(library, show_source, show_library, shows / 'one.mkv', kind='episode', group_id=show,
                           title='Episode one', season=1, episode=1, missing_subtitles='["en"]')
    add_item(library, show_source, show_library, shows / 'other.mkv', kind='episode', group_id=other_show,
             title='Other episode', season=1, episode=1, missing_subtitles='["en"]')
    add_item(library, movie_source, movie_library, movies / 'Mystery.mkv', title='Mystery',
             identity_state='ambiguous')
    extra_movie = add_item(library, extra_source, extra_library, extra / 'Zulu.mkv', title='Zulu',
                           subtitles='[]', missing_subtitles='[]')
    # A stale cross-library relationship must not make another owner's row
    # visible through this show's detail or count as one of its episodes.
    wrong_source, wrong_library, wrong = add_source(
        library, tmp_path, name='Other Series', folder='Different', content_kind='series', profile_id=1)
    wrong_show = add_group(library, wrong_source, wrong_library, title='Different Show')
    wrong_episode = add_item(library, wrong_source, wrong_library, wrong / 'wrong.mkv',
                             kind='episode', group_id=wrong_show, season=1, episode=1, missing_subtitles='["en"]')
    repository.update_item(wrong_episode, library, group_id=show)
    return SimpleNamespace(
        movie=movie, show=show, movie_source=movie_source, movie_library=movie_library,
        show_source=show_source, show_library=show_library, episode_one=episode_one, episode_two=episode_two,
        other_show=other_show, extra_movie=extra_movie, movie_path=str(movies / 'Arr Movie.mkv'),
    )


def _get(client, path):
    response = client.get(path, headers=HEADERS)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


@pytest.mark.parametrize('endpoint, arr_owner', [('movies', 2), ('series', 1)])
def test_browse_orders_and_pages_colliding_local_ids(client, source_library, endpoint, arr_owner):
    path = f'/api/{endpoint}?sources=1'
    mixed = _get(client, path)
    assert mixed['total'] == 4
    first = _get(client, path + '&start=0&length=1')['data'][0]
    second = _get(client, path + '&start=1&length=1')['data'][0]
    assert (first['origin'], first['id'], first['source_key']) == ('arr', 1, f'arr:{arr_owner}')
    assert (second['origin'], second['id']) == ('source', 1)
    assert second['arr_instance_id'] is None and second['profileId'] == 1
    assert second['needs_attention'] is False
    assert _get(client, path + '&id[]=1')['data'] == _get(client, f'/api/{endpoint}?id[]=1')['data']
    assert _get(client, path + '&source_key[]=source:removed') == {'data': [], 'total': 0}
    assert _get(client, path + f'&source_key[]=arr:{arr_owner}')['total'] == 1
    key = second['source_key']
    assert _get(client, path + f'&id[]=1&source_key[]={key}') == {'data': [], 'total': 0}
    combined = _get(client, path + f'&source_key[]=arr:{arr_owner}&source_key[]={key}')
    assert {row['source_key'] for row in combined['data']} == {f'arr:{arr_owner}', key}
    assert client.get(path + '&source_key[]=invalid', headers=HEADERS).status_code == 400
    if endpoint == 'movies':
        assert not second['monitored'] and second['radarrId'] is None
        assert second['path'] == source_library.movie_path
        assert second['poster'] == 'https://images.example.test/movie.png'
        assert second['missing_subtitles'][0]['code2'] == 'en'
    else:
        assert second['sonarrSeriesId'] is None
        assert second['episodeFileCount'] == 2 and second['episodeMissingCount'] == 1


def test_attention_is_filterable_and_uses_inherited_profiles(client, source_library):
    movies = _get(client, '/api/movies?sources=1&needs_attention=1&length=1')
    assert movies['total'] == 2
    assert [row['title'] for row in movies['data']] == ['Mystery']
    rows = _get(client, '/api/movies?sources=1&needs_attention=1')['data']
    assert {row['title']: row['profileId'] for row in rows} == {'Mystery': 1, 'Zulu': None}
    assert [row['title'] for row in _get(client, '/api/series?sources=1&needs_attention=1')['data']] == ['Other Show']


def test_owner_filter_applies_on_arr_only_legacy_path(client):
    original = client.get('/api/movies', headers=HEADERS).data
    assert client.get('/api/movies?sources=1', headers=HEADERS).data == original
    assert _get(client, '/api/movies?source_key[]=arr:2')['total'] == 1
    assert _get(client, '/api/movies?source_key[]=arr:1') == {'data': [], 'total': 0}
    assert _get(client, '/api/movies?sources=1&source_key[]=source:2') == {'data': [], 'total': 0}


def test_mixed_scores_never_use_an_arr_score_for_a_source_with_the_same_id(client, source_library, monkeypatch):
    from api.movies import movies
    from api.series.series import Series

    def film_scores(session, subtitles, *args, **kwargs):
        assert set(subtitles) == {1}
        return {1: 75}

    def series_scores(ids):
        assert ids == [1]
        return {1: 70}

    monkeypatch.setattr(movies, 'lowest_subtitle_scores', film_scores)
    monkeypatch.setattr(Series, '_series_lowest_subtitle_scores', staticmethod(series_scores))
    for endpoint, score in (('movies', 75), ('series', 70)):
        rows = _get(client, f'/api/{endpoint}?sources=1&scores=1')['data']
        assert [(row['origin'], row['lowest_subtitle_score']) for row in rows[:2]] == [('arr', score), ('source', None)]


def test_global_search_returns_kind_qualified_source_destinations(client, source_library):
    path = '/api/system/searches?query=arr'
    legacy = _get(client, path)
    assert len(legacy) == 3 and all('origin' not in item for item in legacy)
    mixed = _get(client, path + '&sources=1')
    assert [item for item in mixed if item.get('origin') != 'source'] == legacy
    found = {item['source_kind']: (item['id'], item['detail_url'])
             for item in mixed if item.get('origin') == 'source'}
    assert found == {'show': (1, '/series/source/1'), 'movie': (1, '/movies/source/1')}


def test_source_detail_profiles_and_episodes_keep_owner_scope(client, source_library, arr_library, monkeypatch):
    from app.database import TableMovies, TableShows
    from media_sources import indexer, repository, service

    add_profile(arr_library, [('hu', 'False', 'False')], profile_id=2)
    queued, events = [], []
    monkeypatch.setattr(service, 'queue_missing_subtitles_recalculation', lambda **kwargs: queued.append(kwargs))
    monkeypatch.setattr('app.event_handler.socketio',
                        SimpleNamespace(server=object(), emit=lambda name, event: events.append((name, event))))
    movie = f'/api/sources/items/{source_library.movie}?kind=movie'
    show = f'/api/sources/groups/{source_library.show}?kind=show'
    movie_detail = _get(client, movie)
    assert movie_detail['effective_profile_id'] == 1
    assert movie_detail['subtitles'][0] == ['en', '/source/Arr Movie.en.srt', 57, 'subrip']
    assert movie_detail['subtitle_details'][0]['path'] == '/source/Arr Movie.en.srt'
    assert movie_detail['subtitle_details'][0]['code2'] == 'en'
    assert movie_detail['missing_subtitles'] == ['en']
    assert movie_detail['missing_subtitle_details'][0]['code2'] == 'en'
    assert _get(client, show)['effective_profile_id'] == 1
    assert client.get(movie.replace('kind=movie', 'kind=episode'), headers=HEADERS).status_code == 404
    assert client.patch(show.replace('kind=show', 'kind=league'), json={'profile_id': 2},
                        headers=HEADERS).status_code == 404
    listing = _get(client, f'/api/sources/items?group_id={source_library.show}&kind=episode&length=-1')
    assert listing['total'] == 2
    assert [row['id'] for row in listing['data']] == [source_library.episode_one, source_library.episode_two]
    assert listing['data'][0]['missing_subtitle_details'][0]['code2'] == 'en'
    for path in (movie, show):
        changed = client.patch(path, json={'profile_id': 2}, headers=HEADERS)
        assert changed.status_code == 200
        assert changed.get_json()['effective_profile_id'] == 2
    assert queued == [{'source_scope': {'item_id': source_library.movie}},
                      {'source_scope': {'group_id': source_library.show}}]
    indexer.recalculate_missing_subtitles(item_id=source_library.movie)
    assert events == [('data', {'type': 'source-item', 'action': 'update', 'payload': source_library.movie})]
    assert repository.get_item(source_library.movie, arr_library).missing_subtitles == "['hu']"
    assert repository.get_item(source_library.extra_movie, arr_library).missing_subtitles == '[]'
    assert arr_library.scalar(sa.select(TableMovies.profileId).where(TableMovies.id == 1)) == 1
    assert arr_library.scalar(sa.select(TableShows.profileId).where(TableShows.id == 1)) == 1
    cleared = client.patch(movie, json={'profile_id': None}, headers=HEADERS)
    assert cleared.status_code == 200 and cleared.get_json()['effective_profile_id'] == 1


def test_group_search_restricts_queued_work_to_eligible_episodes(client, source_library, monkeypatch):
    from media_sources import repository, service

    found, queued = [], []

    def feed(job_name, module, func, **options):
        queued.append((module, func, options['kwargs']))
        return 17

    monkeypatch.setattr(service.jobs_queue, 'feed_jobs_pending_queue', feed)
    monkeypatch.setattr(service.jobs_queue, 'raise_if_cancelled', lambda *args: None)
    monkeypatch.setattr(service.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    monkeypatch.setattr(service, 'search_wanted_item',
                        lambda item_id, created_at, job_id, **kwargs: found.append((item_id, job_id, kwargs['group_id'])))
    endpoint = f'/api/sources/groups/{source_library.show}/search'
    response = client.post(endpoint, headers=HEADERS)
    assert response.status_code == 202 and response.get_json()['job_id'] == 17
    group = repository.get_group(source_library.show)
    key = (group.source_id, group.library_id, group.created_at.isoformat())
    module, func, kwargs = queued[0]
    assert (module, func) == ('media_sources.service', 'search_source_group')
    assert (kwargs['group_id'], kwargs['expected_group']) == (group.id, key)
    service.search_source_group(**{**kwargs, 'job_id': 17})
    assert found == [(source_library.episode_one, 17, group.id)]
    repository.update_library(group.library_id, enabled=0)
    assert client.post(endpoint, headers=HEADERS).status_code == 409
