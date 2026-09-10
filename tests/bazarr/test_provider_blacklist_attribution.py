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


def test_a_sports_search_records_nothing(blacklist_calls):
    """Every id is None, and media_type is "movie" because the video is one."""
    from app.get_providers import _handle_mgb

    _handle_mgb('provider', _exception('movie'),
                {'radarrId': None, 'sonarrSeriesId': None, 'sonarrEpisodeId': None},
                Language('eng'))
    assert blacklist_calls == []


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
