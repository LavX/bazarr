# coding=utf-8
"""Auto-blacklisting a release a provider demands be excluded.

A sports video is a Movie for provider compatibility, so providers report its
media_type as "movie", while the id dict subliminal builds carries only the
Sonarr and Radarr keys and every one of them is None for a sports search. The
movie branch ran anyway and wrote a blacklist row with a null radarrId: it
blacklisted nothing and left a junk entry on the movie Excluded page.
"""

from types import SimpleNamespace

import pytest

from subzero.language import Language


@pytest.fixture
def blacklist_calls(monkeypatch):
    from app import get_providers

    calls = []
    monkeypatch.setattr(get_providers, 'blacklist_log',
                        lambda *args: calls.append(('series',) + args))
    monkeypatch.setattr(get_providers, 'blacklist_log_movie',
                        lambda *args: calls.append(('movie',) + args))
    return calls


@pytest.fixture
def sports_blacklist_calls(monkeypatch):
    from sportarr import history

    calls = []
    monkeypatch.setattr(history, 'blacklist_log_sports',
                        lambda *args: calls.append(args))
    return calls


def _exception(media_type):
    return SimpleNamespace(id='release-1', media_type=media_type)


def test_a_movie_release_is_blacklisted(blacklist_calls):
    from app.get_providers import _handle_mgb

    _handle_mgb('provider', _exception('movie'),
                {'radarrId': 7, 'sonarrSeriesId': None, 'sonarrEpisodeId': None},
                Language('eng'))
    # blacklist_log_movie(radarr_id, provider, subs_id, language)
    assert blacklist_calls == [('movie', 7, 'provider', 'release-1', 'en')]


def test_an_episode_release_is_blacklisted(blacklist_calls):
    from app.get_providers import _handle_mgb

    _handle_mgb('provider', _exception('series'),
                {'radarrId': None, 'sonarrSeriesId': 3, 'sonarrEpisodeId': 9},
                Language('eng'))
    # blacklist_log(series_id, episode_id, provider, subs_id, language)
    assert blacklist_calls == [('series', 3, 9, 'provider', 'release-1', 'en')]


def test_a_sports_search_without_a_context_records_nothing(blacklist_calls):
    """Every id is None, and media_type is "movie" because the video is one.

    Without the video's sports context nothing can be attributed: the callback
    must not invent an event. The context only travels on a real sports video.
    """
    from app.get_providers import _handle_mgb

    _handle_mgb('provider', _exception('movie'),
                {'radarrId': None, 'sonarrSeriesId': None, 'sonarrEpisodeId': None},
                Language('eng'))
    assert blacklist_calls == []


def test_a_sports_search_records_to_its_event_when_the_context_travels(
        blacklist_calls, sports_blacklist_calls):
    """The sports video carries its SportsEventContext (event_id, league_id,
    arr_instance_id), and the pool threads it into the throttle callback. The
    release the provider demanded blacklisting is then excluded for exactly
    that event and owner, not dropped."""
    from app.get_providers import _handle_mgb
    from types import SimpleNamespace

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    _handle_mgb('provider', _exception('movie'),
                {'radarrId': None, 'sonarrSeriesId': None, 'sonarrEpisodeId': None},
                Language('eng'), sports_context=context)
    # blacklist_log_sports(context, provider, subs_id, language)
    assert blacklist_calls == []
    assert sports_blacklist_calls == [(context, 'provider', 'release-1', 'en')]


def test_the_sports_blacklist_context_wins_over_any_media_ids(
        blacklist_calls, sports_blacklist_calls):
    """A sports video can surface a media_type a provider misreads as series;
    the owned context is the attribution, so it is checked before the media
    branches and no junk series row is written."""
    from app.get_providers import _handle_mgb
    from types import SimpleNamespace

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    _handle_mgb('provider', _exception('series'),
                {'radarrId': None, 'sonarrSeriesId': 3, 'sonarrEpisodeId': 9},
                Language('eng'), sports_context=context)
    assert blacklist_calls == []
    assert sports_blacklist_calls[0][1:] == ('provider', 'release-1', 'en')


def test_a_sports_blacklist_row_is_owned_by_its_event_and_instance(
        schema_session, monkeypatch):
    """The write is the same shape the owned exclusion flow uses, so the
    excluded page and the search-side filter see the same (provider, subs_id,
    owner) pair on the next search."""
    from types import SimpleNamespace

    from app.database import (TableArrInstances, TableBlacklistSports,
                              TableSportsEvents, TableSportsLeagues)
    from app.get_providers import _handle_mgb
    from subzero.language import Language
    from sqlalchemy import select as sa_select
    from sportarr import history

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s',
        port=1867, enabled=1))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=7, arr_instance_id=42, sportarrLeagueId=1, title='League'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=11, arr_instance_id=42, league_id=7, sportarrEventId=2, file_id=3,
        path='/sports/race.mkv', title='Race',
        audio_language='[]', subtitles='[]', missing_subtitles='[]',
        failedAttempts='[]'))
    schema_session.commit()

    monkeypatch.setattr(history, 'database', schema_session)
    monkeypatch.setattr(history, 'notify', lambda *args: None)

    _handle_mgb('provider', _exception('movie'),
                {'radarrId': None, 'sonarrSeriesId': None, 'sonarrEpisodeId': None},
                Language('eng'),
                sports_context=SimpleNamespace(
                    event_id=11, league_id=7, arr_instance_id=42))

    rows = schema_session.execute(
        sa_select(TableBlacklistSports)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.event_id, row.league_id, row.arr_instance_id) == (11, 7, 42)
    assert (row.provider, row.subs_id, row.language) == (
        'provider', 'release-1', 'en')


def test_the_pool_threads_the_video_sports_context_to_the_callback():
    """The video reaches the throttle callback as sports_context both when a
    release is listed and when it is downloaded. getattr keeps the default
    None for series and movies, so their behaviour is byte-identical."""
    import inspect

    from subliminal_patch import core

    provider_source = inspect.getsource(core.SZProviderPool.list_subtitles_provider)
    assert "sports_context=getattr(video, 'sports_context', None)" in provider_source
    assert "s.sports_context = getattr(video, 'sports_context', None)" in provider_source

    download_source = inspect.getsource(core.SZProviderPool.download_subtitle)
    assert "sports_context=getattr(subtitle, 'sports_context', None)" in download_source


def test_a_partial_episode_id_records_nothing(blacklist_calls):
    """Half an episode key is not a key: the old check only tested for the
    presence of the dict entries, which subliminal always sets, so a None pair
    passed it."""
    from app.get_providers import _handle_mgb

    _handle_mgb('provider', _exception('series'),
                {'radarrId': None, 'sonarrSeriesId': 3, 'sonarrEpisodeId': None},
                Language('eng'))
    assert blacklist_calls == []


def test_the_language_modifier_travels_with_the_release(blacklist_calls):
    from app.get_providers import _handle_mgb

    hi = Language.rebuild(Language('eng'), hi=True)
    _handle_mgb('provider', _exception('movie'),
                {'radarrId': 7, 'sonarrSeriesId': None, 'sonarrEpisodeId': None}, hi)
    assert blacklist_calls[0][-1] == 'en:hi'
