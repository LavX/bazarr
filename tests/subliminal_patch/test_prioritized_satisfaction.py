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
                        lambda self, name, video, languages, **kwargs: subtitles_by_provider.get(name, []))
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


# A listing a person can stop. A manual search lists the providers one after another, and a slow one can take
# half a minute to answer or time out, so a stop that was only read once the loop returned never ended it.

class _Stop(Exception):
    """What the caller's stop check names as its reason when the person pressed Stop."""


def _stop():
    """What the caller's stop check raises to end the listing: ``ListingStopped`` carrying its reason."""
    from subliminal_patch.core import ListingStopped

    return ListingStopped(_Stop())


def _slow_pool(monkeypatch, durations, calls):
    """A pool whose providers each take as long as ``durations`` says, and that records who was asked."""
    import time

    def list_provider(self, name, video, languages, **kwargs):
        calls.append(name)
        time.sleep(durations[name])
        return []

    monkeypatch.setattr(SZProviderPool, 'list_subtitles_provider', list_provider)
    return SZProviderPool(providers=list(durations))


def test_a_stop_between_providers_asks_no_later_provider(monkeypatch, episode, language):
    from subliminal_patch.core import ListingStopped

    calls = []
    pool = _slow_pool(monkeypatch, {'first': 0.05, 'second': 0.05, 'third': 0.05}, calls)
    stopped = []

    def cancel():
        if calls:
            stopped.append(True)
            raise _stop()

    with pytest.raises(ListingStopped) as stop:
        pool.list_subtitles_prioritized(episode, {language}, exhaustive=True,
                                        provider_order=['first', 'second', 'third'], cancel=cancel)

    assert isinstance(stop.value.original, _Stop)
    assert calls == ['first']


def test_a_stop_ends_the_listing_while_one_provider_is_still_answering(monkeypatch, episode, language):
    """The worst case: the stop arrives while a provider's request is in flight. The listing ends within the
    poll, the slow request is left to finish on its own, and it is the only thing left running."""
    import threading
    import time
    from subliminal_patch.core import ListingStopped

    calls = []
    pool = _slow_pool(monkeypatch, {'slow': 2.0, 'never': 0.01}, calls)
    pressed = time.monotonic() + 0.2

    def cancel():
        if time.monotonic() >= pressed:
            raise _stop()

    started = time.monotonic()
    with pytest.raises(ListingStopped):
        pool.list_subtitles_prioritized(episode, {language}, exhaustive=True,
                                        provider_order=['slow', 'never'], cancel=cancel)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert calls == ['slow']
    workers = [thread for thread in threading.enumerate() if thread.name.startswith('listing-')]
    assert len(workers) == 1 and workers[0].daemon
    workers[0].join(5)
    assert not workers[0].is_alive()


def test_a_stop_check_that_fails_for_its_own_reasons_does_not_end_the_listing(monkeypatch, episode, language):
    """A check that cannot read its answer (a database that is locked for a moment) has not been told to stop.
    The listing carries on and asks again at the next poll."""
    import time

    calls = []
    pool = _slow_pool(monkeypatch, {'first': 0.5, 'second': 0.01}, calls)
    checks = []

    def cancel():
        checks.append(time.monotonic())
        if len(checks) == 1:
            raise OSError('database is locked')

    pool.list_subtitles_prioritized(episode, {language}, exhaustive=True,
                                    provider_order=['first', 'second'], cancel=cancel)

    assert calls == ['first', 'second']
    assert len(checks) >= 3, 'the check was not asked again after it failed'


def test_a_listing_nobody_stops_returns_what_the_providers_gave(monkeypatch, episode, language):
    first = _Subtitle('first', {'series', 'year'}, language)
    pool = _pool_listing(monkeypatch, {'first': [first], 'second': []})
    checks = []

    listed = pool.list_subtitles_prioritized(episode, {language}, exhaustive=True,
                                             provider_order=['first', 'second'], cancel=lambda: checks.append(1))

    assert listed == [first]
    assert len(checks) >= 2


def test_a_provider_error_reaches_the_caller_of_a_stoppable_listing(monkeypatch, episode, language):
    def broken(self, name, video, languages, **kwargs):
        raise RuntimeError('provider blew up')

    monkeypatch.setattr(SZProviderPool, 'list_subtitles_provider', broken)
    pool = SZProviderPool(providers=['first'])

    with pytest.raises(RuntimeError, match='provider blew up'):
        pool.list_subtitles_prioritized(episode, {language}, exhaustive=True, provider_order=['first'],
                                        cancel=lambda: None)
