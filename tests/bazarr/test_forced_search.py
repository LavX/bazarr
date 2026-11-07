# coding=utf-8

"""The forced duplicate search of one episode.

It lists every provider with outcomes, downloads per language through the pool,
and ends each language as downloaded, finished, cutoff_met or open. The
provider statuses are the ones the listing reports, so the finish rule is
tested by feeding scripted results through ``on_outcome``.
"""

from types import SimpleNamespace

import pytest

from subliminal_patch.core import ListingStopped, ProviderSearchResult
from subzero.language import Language

pytestmark = pytest.mark.usefixtures('languages_loaded')


def _episode(failed_attempts='[]', profile_id=1):
    return SimpleNamespace(
        arr_instance_id=1,
        audio_language="['English']",
        failedAttempts=failed_attempts,
        path="/series/fixture/s01e01.mkv",
        profileId=profile_id,
        sceneName="Fixture.Scene",
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        subtitles="[['en', None, 100]]",
        title="Fixture Series",
    )


def _profile_language(language='en', hi='False', forced='False'):
    return {'language': language, 'hi': hi, 'forced': forced}


def _candidate(provider, language='eng'):
    return SimpleNamespace(provider_name=provider, language=Language(language))


class _Video:
    arr_instance_id = None


class FakePool:
    def __init__(self, providers=('alpha', 'beta')):
        self.providers = list(providers)
        self.discarded_providers = set()
        self.language_hook = None
        self.downloads = []
        self.behaviour = lambda pool, candidates, language: []
        self.fallbacks = []
        self.fallback = lambda candidates, language: []

    def download_best_subtitles(self, candidates, video, languages, **kwargs):
        self.downloads.append((list(candidates), set(languages), kwargs))
        return self.behaviour(self, candidates, next(iter(languages)))

    def download_fallback_subtitles(self, candidates, video, languages, **kwargs):
        self.fallbacks.append((list(candidates), set(languages), kwargs))
        return self.fallback(candidates, next(iter(languages)))


@pytest.fixture
def run(monkeypatch):
    """A harness around ``forced._run`` with every outside dependency replaced."""
    import subtitles.pool as pool_module
    import subtitles.tools.mods as mods_module
    import subtitles.wanted.forced as forced

    state = SimpleNamespace(
        forced=forced,
        pool=FakePool(),
        outcomes=[],
        candidates=[],
        items=[_profile_language('en')],
        throttled={},
        supports=lambda name, language: True,
        cutoff=lambda episode: False,
        listed=[],
        recorded=[],
        not_found=[],
        saved=[],
        stop=None,
    )
    video = _Video()
    state.video = video

    def list_all(videos, languages, pool, min_score=0, cancel=None, on_outcome=None):
        state.listed.append(set(languages))
        for outcome in state.outcomes:
            on_outcome(outcome)
        return {videos[0]: list(state.candidates)}

    monkeypatch.setattr(pool_module, '_update_pool', lambda *args, **kwargs: False)
    monkeypatch.setattr(mods_module, 'get_subzero_mods', lambda arr_instance_id: [])
    monkeypatch.setattr(forced, 'list_all_subtitles', list_all)
    monkeypatch.setattr(forced, '_get_pool', lambda media_type, profile_id: state.pool)
    monkeypatch.setattr(forced, 'get_providers', lambda: ['alpha', 'beta'])
    monkeypatch.setattr(forced, 'get_video', lambda *args, **kwargs: state.video)
    monkeypatch.setattr(forced, 'desired_subtitles_for_profile', lambda profile_id, audio: list(state.items))
    monkeypatch.setattr(forced, 'get_profiles_list', lambda profile_id: {'originalFormat': 'False'})
    monkeypatch.setattr(forced, 'get_audio_profile_languages', lambda audio: [{'name': 'English'}])
    monkeypatch.setattr(forced, 'snapshot_throttled_providers',
                        lambda: {'providers': [{'provider': name, 'reason': reason}
                                               for name, reason in state.throttled.items()]})
    monkeypatch.setattr(forced, 'provider_is_throttled', lambda name: name in state.throttled)
    monkeypatch.setattr(forced, '_provider_supports',
                        lambda pool, name, video, language: state.supports(name, language))
    monkeypatch.setattr(forced, 'external_cutoff_met', lambda episode: state.cutoff(episode))
    monkeypatch.setattr(forced.path_mappings, 'path_replace', lambda value: value)
    monkeypatch.setattr(forced.path_mappings, 'path_replace_instance', lambda value, instance, media: value)
    monkeypatch.setattr(forced, '_stop_check', lambda cancel: state.stop)

    def save(video, subtitles, episode, video_path, language, audio_language, max_score, job_id, subz_mods):
        state.saved.append(list(subtitles))
        return [SimpleNamespace(message='saved')]

    monkeypatch.setattr(forced, '_save_and_process', save)
    monkeypatch.setattr(forced, '_record_download', lambda episode, result: state.recorded.append(result))
    monkeypatch.setattr(forced, '_write_not_found', lambda episode, attempts: state.not_found.append(attempts))

    def go(episode=None, **kwargs):
        return forced.run_forced_search(episode or _episode(), **kwargs)

    state.go = go
    return state


def _result(provider, status='empty', reason=None):
    return ProviderSearchResult(provider, status=status, reason=reason)


def _statuses(result):
    return {item.language: item.status for item in result.languages}


def test_language_list_is_every_profile_language_after_the_audio_rules(run):
    run.items = [_profile_language('en'), _profile_language('hu', hi='True'), _profile_language('de', forced='True')]
    run.outcomes = [_result('alpha')]

    result = run.go()

    assert [item.language for item in result.languages] == ['en', 'hu:hi', 'de:forced']
    assert len(run.listed) == 1 and len(run.listed[0]) == 3


def test_an_episode_without_a_profile_has_nothing_to_search(run):
    result = run.go(_episode(profile_id=None))

    assert result.languages == []
    assert result.complete
    assert run.listed == []


@pytest.mark.parametrize('outcomes, expected', [
    # One provider answered with nothing and none failed.
    ([_result('alpha')], 'finished'),
    ([_result('alpha'), _result('beta', 'success')], 'finished'),
    # A provider that does not handle the media or the language is not a failure.
    ([_result('alpha'), _result('beta', 'skipped', 'unsupported_language')], 'finished'),
    ([_result('alpha'), _result('beta', 'skipped', 'unsupported_media')], 'finished'),
    ([_result('alpha'), _result('beta', 'skipped', 'excluded_language')], 'finished'),
    # A busy provider is not an answer and not a failure: the request waits.
    ([_result('alpha'), _result('beta', 'skipped', 'provider_busy')], 'open'),
    ([_result('alpha'), _result('beta', 'skipped', 'provider_excluded')], 'open'),
    ([_result('alpha'), _result('beta', 'cooldown', 'cooldown')], 'open'),
    ([_result('alpha'), _result('beta', 'timeout', 'timeout')], 'open'),
    ([_result('alpha'), _result('beta', 'unreachable', 'unreachable')], 'open'),
    ([_result('alpha'), _result('beta', 'error', 'error')], 'open'),
    # Nobody answered.
    ([], 'open'),
    ([_result('alpha', 'timeout')], 'open'),
])
def test_finish_rule(run, outcomes, expected):
    run.outcomes = outcomes

    result = run.go()

    assert _statuses(result) == {'en': expected}
    assert bool(run.not_found) == (expected == 'finished')
    assert result.complete == (expected == 'finished')


def test_a_finished_language_writes_one_not_found_record_per_language(run):
    run.items = [_profile_language('en'), _profile_language('hu')]
    run.outcomes = [_result('alpha')]

    result = run.go(_episode(failed_attempts='[]'))

    assert _statuses(result) == {'en': 'finished', 'hu': 'finished'}
    # The second record builds on the first, so both languages are in the final string.
    assert len(run.not_found) == 2
    assert "'en'" in run.not_found[-1] and "'hu'" in run.not_found[-1]


@pytest.mark.parametrize('status', ['authentication_required', 'setup_required'])
def test_a_credential_failure_is_neither_an_answer_nor_a_hold(run, status):
    run.outcomes = [_result('alpha'), _result('beta', status, status)]

    assert _statuses(run.go()) == {'en': 'finished'}


def test_a_credential_failure_alone_is_not_an_answer(run):
    run.outcomes = [_result('beta', 'authentication_required', 'authentication_required')]

    result = run.go()

    assert result.languages[0].status == 'open'
    assert result.languages[0].reason == 'no_provider_answered'


def test_a_provider_excluded_for_a_credential_backoff_is_ignored(run):
    run.throttled = {'beta': 'AuthenticationError'}
    run.outcomes = [_result('alpha'), _result('beta', 'skipped', 'provider_excluded')]

    assert _statuses(run.go()) == {'en': 'finished'}


def test_a_credential_backoff_at_start_is_ignored(run):
    run.throttled = {'beta': 'ConfigurationError'}
    run.outcomes = [_result('alpha')]

    assert _statuses(run.go()) == {'en': 'finished'}


def test_a_provider_throttled_at_start_holds_the_request(run):
    run.throttled = {'beta': 'TooManyRequests'}
    run.outcomes = [_result('alpha')]

    result = run.go()

    assert result.languages[0].status == 'open'
    assert 'beta:throttled' in result.languages[0].reason


def test_a_throttled_provider_that_does_not_support_the_language_does_not_hold(run):
    run.throttled = {'beta': 'TooManyRequests'}
    run.outcomes = [_result('alpha')]
    run.supports = lambda name, language: name != 'beta'

    assert _statuses(run.go()) == {'en': 'finished'}


def test_a_failure_of_a_provider_that_does_not_support_a_language_holds_only_the_others(run):
    run.items = [_profile_language('en'), _profile_language('hu')]
    run.outcomes = [_result('alpha'), _result('beta', 'timeout')]
    # beta handles Hungarian only.
    run.supports = lambda name, language: not (name == 'beta' and language.alpha3 == 'eng')

    assert _statuses(run.go()) == {'en': 'finished', 'hu': 'open'}


def test_the_embedded_provider_never_counts(run):
    run.outcomes = [_result('alpha'), _result('embeddedsubtitles', 'error')]

    assert _statuses(run.go()) == {'en': 'finished'}


def test_the_embedded_provider_answer_is_not_an_answer(run):
    run.outcomes = [_result('embeddedsubtitles', 'success')]

    assert _statuses(run.go()) == {'en': 'open'}


def test_embedded_provider_candidates_are_never_downloaded(run):
    embedded = _candidate('embeddedsubtitles')
    external = _candidate('alpha')
    run.candidates = [embedded, external]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [external]

    result = run.go()

    offered, languages, _ = run.pool.downloads[0]
    assert offered == [external]
    assert result.downloaded == ['en']


def test_a_downloaded_language_is_recorded_and_writes_no_not_found(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [candidate]

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded'}
    assert run.saved == [[candidate]]
    assert len(run.recorded) == 1
    assert run.not_found == []


def test_a_download_that_cannot_be_saved_keeps_the_language_open(run, monkeypatch):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [candidate]
    monkeypatch.setattr(run.forced, '_save_and_process', lambda *args, **kwargs: [])

    result = run.go()

    assert result.languages[0].status == 'open'
    assert result.languages[0].reason == 'save_failed'
    assert run.not_found == []


def test_a_skipped_download_holds_the_request(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]

    def behaviour(pool, candidates, language):
        candidate.skip_reason = 'provider_busy'
        return []

    run.pool.behaviour = behaviour

    result = run.go()

    assert result.languages[0].status == 'open'
    assert 'alpha:provider_busy' in result.languages[0].reason
    assert run.not_found == []


def test_a_provider_discarded_during_the_download_holds_the_request(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success'), _result('beta')]

    def behaviour(pool, candidates, language):
        pool.discarded_providers.add('alpha')
        return []

    run.pool.behaviour = behaviour

    result = run.go()

    assert result.languages[0].status == 'open'
    assert 'alpha:lost_during_download' in result.languages[0].reason


def test_a_provider_throttled_during_the_download_holds_the_request(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success'), _result('beta')]

    def behaviour(pool, candidates, language):
        run.throttled = {'alpha': 'TooManyRequests'}
        return []

    run.pool.behaviour = behaviour

    assert _statuses(run.go()) == {'en': 'open'}


def test_a_rejected_candidate_counts_as_an_answer(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]

    def behaviour(pool, candidates, language):
        candidate.rejected_reason = 'not a subtitle'
        return []

    run.pool.behaviour = behaviour

    result = run.go()

    assert _statuses(result) == {'en': 'finished'}
    assert len(run.not_found) == 1


def test_a_language_only_downloads_from_its_own_candidates_per_call(run):
    run.items = [_profile_language('en'), _profile_language('hu')]
    english = _candidate('alpha', 'eng')
    hungarian = _candidate('alpha', 'hun')
    run.candidates = [english, hungarian]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [candidates[0]]

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded', 'hu': 'downloaded'}
    assert [languages for _, languages, _ in run.pool.downloads] == [
        {Language('eng')}, {Language('hun')}]
    assert [offered for offered, _, _ in run.pool.downloads] == [[english], [hungarian]]


def test_a_cutoff_already_met_by_external_subtitles_searches_nothing(run):
    run.items = [_profile_language('en'), _profile_language('hu')]
    run.cutoff = lambda episode: True

    result = run.go()

    assert _statuses(result) == {'en': 'cutoff_met', 'hu': 'cutoff_met'}
    assert result.complete
    assert run.listed == []
    assert run.not_found == []


def test_a_cutoff_met_by_an_earlier_download_stops_the_remaining_languages(run):
    run.items = [_profile_language('en'), _profile_language('hu')]
    candidate = _candidate('alpha', 'eng')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [candidate]
    # The first check, before anything is listed, finds the cutoff unmet. The saved download then meets it.
    run.cutoff = lambda episode: bool(run.recorded)

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded', 'hu': 'cutoff_met'}
    assert len(run.pool.downloads) == 1
    assert run.not_found == []
    assert result.complete


def test_no_provider_in_the_pool_keeps_the_request(run, monkeypatch):
    monkeypatch.setattr(run.forced, 'get_providers', lambda: None)

    result = run.go()

    assert result.languages[0].status == 'open'
    assert result.languages[0].reason == 'no_providers'
    assert run.listed == []


def test_an_empty_pool_keeps_the_request(run):
    run.pool.providers = []

    assert _statuses(run.go()) == {'en': 'open'}


def test_a_video_that_cannot_be_read_keeps_the_request(run, monkeypatch):
    monkeypatch.setattr(run.forced, 'get_video', lambda *args, **kwargs: None)

    result = run.go()

    assert result.languages[0].reason == 'no_video'
    assert run.listed == []


def test_whisper_fallback_is_only_allowed_after_a_clean_answer(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', True)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', True)
    run.outcomes = [_result('alpha'), _result('beta', 'timeout')]
    run.go()
    assert run.pool.fallbacks == []

    run.outcomes = [_result('alpha')]
    run.go()
    assert len(run.pool.fallbacks) == 1


def test_whisper_fallback_does_not_stand_in_for_a_skipped_candidate(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', True)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', True)
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]

    def behaviour(pool, candidates, language):
        candidate.skip_reason = 'provider_busy'
        return []

    run.pool.behaviour = behaviour
    run.pool.fallback = lambda candidates, language: [_candidate('whisperai')]

    result = run.go()

    assert run.pool.fallbacks == []
    assert result.languages[0].status == 'open'
    assert run.saved == []


def test_a_whisper_fallback_that_delivers_is_a_download(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', True)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', True)
    whisper = _candidate('whisperai')
    run.outcomes = [_result('alpha')]
    run.pool.fallback = lambda candidates, language: [whisper]

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded'}
    assert run.saved == [[whisper]]


def test_the_translate_from_rule_is_not_consulted(run, monkeypatch):
    """The forced branch searches providers for every language: a profile's translate-from never replaces that."""
    queued = []
    monkeypatch.setattr('subtitles.tools.translate.main.translate_subtitles_file',
                        lambda **kwargs: queued.append(kwargs))
    run.items = [dict(_profile_language('hu'), translate_from='en')]
    run.outcomes = [_result('alpha')]

    result = run.go()

    assert queued == []
    assert len(run.listed) == 1 and Language('hun') in run.listed[0]
    assert _statuses(result) == {'hu': 'finished'}


def test_stop_during_the_listing_propagates_and_leaves_nothing_behind(run, monkeypatch):
    from app.jobs_queue import JobCancelled

    stopped = JobCancelled(job_id=7)

    def list_all(*args, **kwargs):
        raise ListingStopped(stopped)

    monkeypatch.setattr(run.forced, 'list_all_subtitles', list_all)

    with pytest.raises(JobCancelled) as raised:
        run.go(job_id=7)

    assert raised.value is stopped
    assert run.not_found == [] and run.recorded == []


def test_stop_between_languages_propagates(run):
    from app.jobs_queue import JobCancelled

    run.items = [_profile_language('en'), _profile_language('hu')]
    run.candidates = [_candidate('alpha', 'eng'), _candidate('alpha', 'hun')]
    run.outcomes = [_result('alpha', 'success')]
    calls = []

    def stop():
        calls.append(1)
        if len(run.not_found) == 1:
            raise ListingStopped(JobCancelled(job_id=7))

    run.stop = stop

    with pytest.raises(JobCancelled):
        run.go(job_id=7)

    assert len(run.not_found) == 1


def test_a_listing_error_propagates(run, monkeypatch):
    def list_all(*args, **kwargs):
        raise RuntimeError('listing failed')

    monkeypatch.setattr(run.forced, 'list_all_subtitles', list_all)

    with pytest.raises(RuntimeError):
        run.go()

    assert run.not_found == []


def test_stop_during_a_download_for_this_job_propagates(run):
    from app.jobs_queue import JobCancelled

    run.candidates = [_candidate('alpha')]
    run.outcomes = [_result('alpha', 'success')]

    def behaviour(pool, candidates, language):
        raise JobCancelled(job_id=7)

    run.pool.behaviour = behaviour

    with pytest.raises(JobCancelled):
        run.go(job_id=7)


def test_complete_means_no_language_is_open():
    from subtitles.wanted.forced import ForcedSearchResult, LanguageResult

    assert ForcedSearchResult([LanguageResult('en', 'downloaded'), LanguageResult('hu', 'finished'),
                               LanguageResult('de', 'cutoff_met')]).complete
    assert not ForcedSearchResult([LanguageResult('en', 'downloaded'), LanguageResult('hu', 'open')]).complete


# The pieces that read the real profile, the real stored subtitles and the real listing.


def test_the_listing_does_not_drop_a_language_the_video_already_has_an_embedded_track_for(monkeypatch):
    """``list_all_subtitles`` asks for ``languages - video.subtitle_languages``.

    Nothing in the app's video pipeline fills ``subtitle_languages`` (the metadata refiner has its embedded
    detection removed), so the language whose embedded track is broken is still listed.
    """
    import subliminal_patch.core_persistent as persistent
    from subliminal_patch.core import Episode

    video = Episode('/series/fixture/s01e01.mkv', 'Fixture Series', 1, 1)
    asked = []

    def list_provider(pool, name, video, languages, cancel):
        asked.append((name, set(languages)))
        return ProviderSearchResult(name)

    monkeypatch.setattr(persistent, '_list_provider_with_outcome', list_provider)
    pool = SimpleNamespace(providers=['alpha'], discarded_providers=set())
    outcomes = []

    persistent.list_all_subtitles([video], {Language('eng'), Language('hun')}, pool, on_outcome=outcomes.append)

    assert video.subtitle_languages == set()
    assert asked == [('alpha', {Language('eng'), Language('hun')})]
    assert [outcome.provider for outcome in outcomes] == ['alpha']


def test_external_cutoff_ignores_embedded_tracks(monkeypatch):
    import subtitles.indexer.series as indexer
    import subtitles.wanted.forced as forced

    monkeypatch.setattr(indexer, 'get_profile_cutoff',
                        lambda profile_id: [{'language': 'en', 'forced': 'False', 'hi': 'False'}])
    monkeypatch.setattr(indexer, 'get_audio_profile_languages', lambda audio: [])

    embedded_only = "[['en', None, None]]"
    monkeypatch.setattr(forced, '_external_subtitles_text', lambda episode: embedded_only)
    assert forced.external_cutoff_met(_episode()) is False

    external = "[['en', '/series/fixture/s01e01.en.srt', 100]]"
    monkeypatch.setattr(forced, '_external_subtitles_text', lambda episode: external)
    assert forced.external_cutoff_met(_episode()) is True

    monkeypatch.setattr(forced, '_external_subtitles_text', lambda episode: None)
    assert forced.external_cutoff_met(_episode()) is False


def test_external_cutoff_counts_hearing_impaired_as_good_as_normal(monkeypatch):
    import subtitles.indexer.series as indexer
    import subtitles.wanted.forced as forced

    monkeypatch.setattr(indexer, 'get_profile_cutoff',
                        lambda profile_id: [{'language': 'en', 'forced': 'False', 'hi': 'False'}])
    monkeypatch.setattr(indexer, 'get_audio_profile_languages', lambda audio: [])
    monkeypatch.setattr(forced, '_external_subtitles_text',
                        lambda episode: "[['en:hi', '/series/fixture/s01e01.en.hi.srt', 100]]")

    assert forced.external_cutoff_met(_episode()) is True


def test_forced_search_languages_follow_the_audio_rules(monkeypatch):
    import subtitles.indexer.series as indexer
    import subtitles.wanted.forced as forced

    profile = {'items': [
        {'language': 'en', 'forced': 'False', 'hi': 'False', 'audio_exclude': 'True'},
        {'language': 'hu', 'forced': 'False', 'hi': 'True'},
        {'language': 'de', 'forced': 'True', 'hi': 'False', 'audio_only_include': 'True'},
        {'language': 'fr', 'forced': 'False', 'hi': 'False'},
    ]}
    monkeypatch.setattr(indexer, 'get_profiles_list', lambda profile_id: profile)
    monkeypatch.setattr(indexer, 'get_audio_profile_languages',
                        lambda audio: [{'name': 'English', 'code2': 'en', 'code3': 'eng'}])

    pairs = forced.forced_search_languages(_episode())

    # English audio excludes the English subtitle, and German is only wanted for German audio.
    assert pairs == [('hu:hi', ('hu', 'True', 'False')), ('fr', ('fr', 'False', 'False'))]


def test_the_support_check_reads_the_provider_class(monkeypatch):
    import subtitles.wanted.forced as forced

    class Alpha:
        languages = {Language('eng')}

        @classmethod
        def check(cls, video):
            return True

    class Video:
        @classmethod
        def check(cls, video):
            return False

    class Equals:
        def check_set(self, languages):
            return languages

    pool = SimpleNamespace(language_hook=None, lang_equals=Equals())
    monkeypatch.setattr(forced, 'provider_registry', {'alpha': Alpha, 'video': Video})

    assert forced._provider_supports(pool, 'alpha', object(), Language('eng')) is True
    assert forced._provider_supports(pool, 'alpha', object(), Language('hun')) is False
    assert forced._provider_supports(pool, 'video', object(), Language('eng')) is False

    pool.language_hook = lambda name: {Language('eng')}
    assert forced._provider_supports(pool, 'alpha', object(), Language('eng')) is False


def test_stop_of_another_job_during_a_download_keeps_that_language_open(run):
    from app.jobs_queue import JobCancelled

    run.candidates = [_candidate('alpha')]
    run.outcomes = [_result('alpha', 'success')]

    def behaviour(pool, candidates, language):
        raise JobCancelled(job_id=99)

    run.pool.behaviour = behaviour

    result = run.go(job_id=7)

    assert _statuses(result) == {'en': 'open'}
    assert run.not_found == []


def test_a_credential_loss_during_the_download_does_not_hold(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success'), _result('beta')]

    def behaviour(pool, candidates, language):
        pool.discarded_providers.add('alpha')
        run.throttled = {'alpha': 'AuthenticationError'}
        return []

    run.pool.behaviour = behaviour

    assert _statuses(run.go()) == {'en': 'finished'}


def test_a_skipped_download_of_a_credential_blocked_provider_does_not_hold(run):
    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]

    def behaviour(pool, candidates, language):
        candidate.skip_reason = 'provider_excluded'
        run.throttled = {'alpha': 'ConfigurationError'}
        return []

    run.pool.behaviour = behaviour

    assert _statuses(run.go()) == {'en': 'finished'}


def test_provider_priority_tries_providers_in_the_pools_order(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_provider_priority', True)
    beta = _candidate('beta')
    alpha = _candidate('alpha')
    run.candidates = [beta, alpha]
    run.outcomes = [_result('alpha', 'success'), _result('beta', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: []

    run.go()
    assert [offered for offered, _, _ in run.pool.downloads] == [[alpha], [beta]]

    run.pool.downloads.clear()
    run.pool.behaviour = lambda pool, candidates, language: candidates[:1]
    result = run.go()
    assert [offered for offered, _, _ in run.pool.downloads] == [[alpha]]
    assert _statuses(result) == {'en': 'downloaded'}


def test_without_provider_priority_the_best_score_wins_across_providers(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_provider_priority', False)
    beta = _candidate('beta')
    alpha = _candidate('alpha')
    run.candidates = [beta, alpha]
    run.outcomes = [_result('alpha', 'success'), _result('beta', 'success')]

    run.go()

    assert [offered for offered, _, _ in run.pool.downloads] == [[beta, alpha]]


def test_a_language_no_provider_applies_to_is_open_with_its_own_reason(run):
    run.outcomes = [_result('alpha', 'skipped', 'unsupported_language')]

    result = run.go()

    assert result.languages[0].status == 'open'
    assert result.languages[0].reason == 'no_applicable_provider'
    assert run.not_found == []


def test_only_the_embedded_provider_answering_is_no_applicable_provider(run):
    run.outcomes = [_result('embeddedsubtitles', 'success')]

    assert run.go().languages[0].reason == 'no_applicable_provider'


def test_a_credential_failure_alone_still_has_an_applicable_provider(run):
    run.outcomes = [_result('alpha', 'authentication_required')]

    assert run.go().languages[0].reason == 'no_provider_answered'


def test_the_subtitle_cache_is_synced_after_a_run(run, monkeypatch):
    synced = []
    monkeypatch.setattr(run.forced, '_sync_cache', lambda: synced.append(1))
    run.outcomes = [_result('alpha')]

    run.go()

    assert synced == [1]


def _saving(monkeypatch, forced, language, on_disk):
    """Run ``_save_and_process`` with the writing and processing replaced."""
    import contextlib

    cleared = []
    subtitle = SimpleNamespace(format='srt', storage_path='/series/fixture/s01e01.en.srt', language=language,
                               matches={'hash', 'series'})

    monkeypatch.setattr(forced, 'get_target_folder', lambda path: None)
    monkeypatch.setattr(forced, 'subtitle_write_locks', lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(forced, 'save_subtitles',
                        lambda *args, **kwargs: (kwargs['write_subtitle'].keywords['written_paths'].append(
                            subtitle.storage_path), [subtitle])[1])
    monkeypatch.setattr(forced.os.path, 'isfile', lambda path: on_disk)
    monkeypatch.setattr(forced, 'process_subtitle', lambda **kwargs: SimpleNamespace(message='saved'))
    monkeypatch.setattr(forced, 'clear_mismatch_for_video',
                        lambda video, media_type, lang, arr_instance_id=None: cleared.append(lang))
    video = SimpleNamespace(original_path='/series/fixture/s01e01.mkv')
    processed = forced._save_and_process(video, [subtitle], _episode(), '/series/fixture/s01e01.mkv', language,
                                         'English', 100, None, [])
    return processed, cleared


def test_a_saved_subtitle_clears_the_recorded_mismatch(monkeypatch):
    import subtitles.wanted.forced as forced

    processed, cleared = _saving(monkeypatch, forced, Language('eng'), on_disk=True)

    assert len(processed) == 1
    assert cleared == [Language('eng')]


def test_a_subtitle_that_never_reached_disk_keeps_the_mismatch(monkeypatch):
    import subtitles.wanted.forced as forced

    processed, cleared = _saving(monkeypatch, forced, Language('eng'), on_disk=False)

    assert processed == []
    assert cleared == []


def test_a_stop_during_the_download_saves_and_records_nothing(run):
    from app.jobs_queue import JobCancelled

    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]
    pressed = []

    def stop():
        if pressed:
            raise ListingStopped(JobCancelled(job_id=7))

    def behaviour(pool, candidates, language):
        # Stop is pressed while the downloader runs; the downloader itself does not notice.
        pressed.append(1)
        return [candidate]

    run.stop = stop
    run.pool.behaviour = behaviour

    with pytest.raises(JobCancelled):
        run.go(job_id=7)

    assert run.saved == [] and run.recorded == [] and run.not_found == []


def test_a_stop_during_the_fallback_saves_and_records_nothing(run, monkeypatch):
    from app.jobs_queue import JobCancelled

    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', True)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', True)
    run.outcomes = [_result('alpha')]
    pressed = []

    def stop():
        if pressed:
            raise ListingStopped(JobCancelled(job_id=7))

    def fallback(candidates, language):
        pressed.append(1)
        return [_candidate('whisperai')]

    run.stop = stop
    run.pool.fallback = fallback

    with pytest.raises(JobCancelled):
        run.go(job_id=7)

    assert run.saved == [] and run.recorded == []


def test_a_stop_pressed_while_the_last_download_is_recorded_does_not_return_complete(run, monkeypatch):
    from app.jobs_queue import JobCancelled

    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [candidate]
    pressed = []

    def stop():
        if pressed:
            raise ListingStopped(JobCancelled(job_id=7))

    run.stop = stop
    monkeypatch.setattr(run.forced, '_record_download', lambda episode, result: pressed.append(1))

    with pytest.raises(JobCancelled):
        run.go(job_id=7)


def test_the_video_and_the_save_use_the_episodes_instance_mapping(run, monkeypatch):
    seen = {}
    monkeypatch.setattr(run.forced.path_mappings, 'path_replace', lambda value: '/global' + value)
    monkeypatch.setattr(run.forced.path_mappings, 'path_replace_instance',
                        lambda value, instance, media: f'/instance-{instance}-{media}' + value)

    def get_video(path, *args, **kwargs):
        seen['video'] = path
        return run.video

    def save(video, subtitles, episode, video_path, *args):
        seen['save'] = video_path
        return [SimpleNamespace(message='saved')]

    candidate = _candidate('alpha')
    run.candidates = [candidate]
    run.outcomes = [_result('alpha', 'success')]
    run.pool.behaviour = lambda pool, candidates, language: [candidate]
    monkeypatch.setattr(run.forced, 'get_video', get_video)
    monkeypatch.setattr(run.forced, '_save_and_process', save)

    run.go()

    expected = '/instance-1-episode/series/fixture/s01e01.mkv'
    assert seen == {'video': expected, 'save': expected}


def test_the_series_whisper_switch_gates_the_fallback(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', True)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', False)
    run.outcomes = [_result('alpha')]
    run.go()
    assert run.pool.fallbacks == []

    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', True)
    run.go()
    assert len(run.pool.fallbacks) == 1

    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', False)
    run.pool.fallbacks.clear()
    run.go()
    assert run.pool.fallbacks == []


def test_the_callers_fallback_permission_overrides_the_settings(run, monkeypatch):
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', False)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', False)
    run.outcomes = [_result('alpha')]

    run.go(fallback_allowed=True)
    assert len(run.pool.fallbacks) == 1

    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback', True)
    monkeypatch.setattr(run.forced.settings.general, 'use_whisper_fallback_series', True)
    run.pool.fallbacks.clear()
    run.go(fallback_allowed=False)
    assert run.pool.fallbacks == []


# Real candidate selection: the pool's own download_best_subtitles scores and picks.


class _Candidate:
    hash_verifiable = False
    hearing_impaired_verifiable = True
    release_info = 'Fixture.S01E01'
    uploader = None
    page_link = None

    def __init__(self, provider, language, hearing_impaired=False, subtitle_id=None):
        self.provider_name = provider
        self.language = language
        self.hearing_impaired = hearing_impaired
        self.id = subtitle_id or f'{provider}-{language}-{hearing_impaired}'
        self.matches = set()

    def get_matches(self, video):
        matches = {'series', 'season', 'episode', 'year', 'release_group', 'source', 'resolution'}
        if self.hearing_impaired:
            matches.add('hearing_impaired')
        return matches

    def __repr__(self):
        return self.id


def _real_pool(downloaded):
    from subliminal_patch.core import SZProviderPool

    pool = object.__new__(SZProviderPool)
    pool.providers = ['alpha']
    pool.discarded_providers = set()
    pool.language_hook = None

    def download_subtitle(subtitle):
        downloaded.append(subtitle)
        return True

    pool.download_subtitle = download_subtitle
    return pool


def _episode_video():
    from subliminal_patch.core import Episode

    return Episode('/series/fixture/s01e01.mkv', 'Fixture Series', 1, 1, source='Web', release_group='GRP')


def test_a_plain_candidate_does_not_satisfy_the_forced_language(run):
    downloaded = []
    run.pool = _real_pool(downloaded)
    run.video = _episode_video()
    run.items = [_profile_language('en'), _profile_language('en', forced='True')]
    plain = _Candidate('alpha', Language('eng'))
    forced_candidate = _Candidate('alpha', Language('eng', forced=True))
    run.candidates = [plain, forced_candidate]
    run.outcomes = [_result('alpha', 'success')]

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded', 'en:forced': 'downloaded'}
    assert run.saved == [[plain], [forced_candidate]]


def test_only_a_plain_candidate_leaves_the_forced_language_to_finish_empty(run):
    downloaded = []
    run.pool = _real_pool(downloaded)
    run.video = _episode_video()
    run.items = [_profile_language('en'), _profile_language('en', forced='True')]
    plain = _Candidate('alpha', Language('eng'))
    run.candidates = [plain]
    run.outcomes = [_result('alpha', 'success')]

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded', 'en:forced': 'finished'}
    assert run.saved == [[plain]]


def test_a_forced_candidate_does_not_satisfy_the_plain_language(run):
    run.pool = _real_pool([])
    run.video = _episode_video()
    run.items = [_profile_language('en')]
    run.candidates = [_Candidate('alpha', Language('eng', forced=True))]
    run.outcomes = [_result('alpha', 'success')]

    assert _statuses(run.go()) == {'en': 'finished'}
    assert run.saved == []


def test_a_hearing_impaired_language_only_takes_a_hearing_impaired_candidate(run):
    run.pool = _real_pool([])
    run.video = _episode_video()
    run.items = [_profile_language('en', hi='True')]
    plain = _Candidate('alpha', Language('eng'))
    run.candidates = [plain]
    run.outcomes = [_result('alpha', 'success')]

    assert _statuses(run.go()) == {'en:hi': 'finished'}
    assert run.saved == []

    impaired = _Candidate('alpha', Language('eng'), hearing_impaired=True)
    run.candidates = [plain, impaired]
    result = run.go()

    assert _statuses(result) == {'en:hi': 'downloaded'}
    assert run.saved[-1] == [impaired]


def test_a_mixed_profile_enforces_hearing_impaired_for_its_own_language_only(run):
    run.pool = _real_pool([])
    run.video = _episode_video()
    run.items = [_profile_language('en'), _profile_language('hu', hi='True')]
    english = _Candidate('alpha', Language('eng'))
    hungarian_plain = _Candidate('alpha', Language('hun'))
    run.candidates = [english, hungarian_plain]
    run.outcomes = [_result('alpha', 'success')]

    result = run.go()

    assert _statuses(result) == {'en': 'downloaded', 'hu:hi': 'finished'}
    assert run.saved == [[english]]
