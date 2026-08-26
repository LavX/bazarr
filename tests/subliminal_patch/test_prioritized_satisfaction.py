# coding=utf-8
"""The prioritized listing's early exit has to mean what the download means.

``list_subtitles_prioritized`` stops querying providers once every requested
language is "satisfied", and it decided that on score alone.
``download_best_subtitles`` then throws out an episode candidate that does not
carry season, episode and either series or an imdb id, so a candidate could end
the search and be rejected straight afterwards, leaving nothing downloaded and
the remaining providers never asked.

A per-provider score modifier widens the gap between the two checks, which is
what brought it to light.
"""
import pytest

from subliminal_patch.core import SZProviderPool
from subliminal_patch.score import MAX_SCORES


class _Subtitle:
    hash_verifiable = False

    def __init__(self, provider_name, matches, language):
        self.provider_name = provider_name
        self._matches = matches
        self.language = language
        self.hearing_impaired = False

    def get_matches(self, video):
        return set(self._matches)

    def __repr__(self):
        return f'<_Subtitle {self.provider_name} {sorted(self._matches)}>'


@pytest.fixture
def episode():
    from subliminal_patch.core import Episode

    return Episode('/m/Show.S01E01.mkv', 'Show', 1, 1)


@pytest.fixture
def language():
    from babelfish import Language

    return Language('eng')


def _pool_listing(monkeypatch, subtitles_by_provider):
    """An SZProviderPool that hands back the given results, provider by provider.

    The method is patched on the class because list_subtitles_prioritized calls
    it unbound, so an instance attribute would never be reached.
    """
    monkeypatch.setattr(SZProviderPool, 'list_subtitles_provider',
                        lambda self, name, video, languages: subtitles_by_provider.get(name, []))
    return SZProviderPool(providers=list(subtitles_by_provider))


def test_an_episode_candidate_missing_season_and_episode_does_not_end_the_search(
        monkeypatch, episode, language):
    """It scores well on series and year alone, and the download path will
    refuse it, so it must not stop the second provider being asked."""
    weak = _Subtitle('first', {'series', 'year'}, language)
    good = _Subtitle('second', {'series', 'year', 'season', 'episode'}, language)

    pool = _pool_listing(monkeypatch, {'first': [weak], 'second': [good]})

    listed = pool.list_subtitles_prioritized(
        episode, {language}, min_score=1, provider_order=['first', 'second'])

    assert good in listed


def test_a_complete_episode_candidate_still_ends_the_search(
        monkeypatch, episode, language):
    """The early exit is the point of the prioritized path; it has to survive."""
    good = _Subtitle('first', {'series', 'year', 'season', 'episode'}, language)
    later = _Subtitle('second', {'series', 'year', 'season', 'episode'}, language)

    pool = _pool_listing(monkeypatch, {'first': [good], 'second': [later]})

    listed = pool.list_subtitles_prioritized(
        episode, {language}, min_score=1, provider_order=['first', 'second'])

    assert good in listed
    assert later not in listed


def test_a_modifier_cannot_satisfy_a_language_with_an_invalid_candidate(
        monkeypatch, episode, language):
    """The case that exposed it: a positive modifier lifts a series-and-year
    candidate over an eighty percent threshold, the search stops, and then the
    download path rejects it."""
    from subliminal_patch.score import compute_score

    weak = _Subtitle('whisperai', {'series', 'year'}, language)
    good = _Subtitle('second', {'series', 'year', 'season', 'episode'}, language)

    pool = _pool_listing(monkeypatch, {'whisperai': [weak], 'second': [good]})
    monkeypatch.setattr(type(compute_score), 'modifier',
                        staticmethod(lambda provider: 20 if provider == 'whisperai' else 0))

    listed = pool.list_subtitles_prioritized(
        episode, {language},
        min_score=round(MAX_SCORES['episode'] * 0.8),
        provider_order=['whisperai', 'second'])

    assert good in listed


def test_a_movie_candidate_is_judged_on_score_alone(monkeypatch, language):
    """The series/season/episode requirement is an episode rule. A movie has
    no equivalent in the download path, so nothing extra is imposed here."""
    from subliminal_patch.core import Movie

    movie = Movie('/m/Film.2020.mkv', 'Film', year=2020)
    candidate = _Subtitle('first', {'title', 'year'}, language)
    later = _Subtitle('second', {'title', 'year'}, language)

    pool = _pool_listing(monkeypatch, {'first': [candidate], 'second': [later]})

    listed = pool.list_subtitles_prioritized(
        movie, {language}, min_score=1, provider_order=['first', 'second'])

    assert candidate in listed
    assert later not in listed
