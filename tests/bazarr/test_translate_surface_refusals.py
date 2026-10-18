# coding=utf-8

"""Every surface that queues a translation asks the gate first, and answers.

The queue paths the wanted scans use were gated when the availability module
landed, but the actions a user presses were not: the manual translate action
queued a doomed job and answered 204, the mass and batch paths queued one doomed
job per item, the subtitle editor refused on the service URL alone, the health
page said nothing about profiles that translate from another language while
nothing can translate, and the system status carried no answer for the UI to
read. Each of those surfaces now refuses with the gate's reason before it
queues anything, and the health page and system status say what the gate says.
"""

import json
from types import SimpleNamespace

from flask import Flask

from app.database import TableEpisodes, TableLanguagesProfiles, TableMovies, TableShows

NO_TRANSLATOR_REASON = 'No translator is configured'
URL_REASON = 'The AI Subtitle Translator service URL is not configured'


def _gate_settings(translator_type='none', **translator_overrides):
    translator = dict(
        translator_type=translator_type,
        gemini_keys=[],
        lingarr_url='',
        openrouter_url='',
        openrouter_api_key='',
        openrouter_encryption_key='',
    )
    translator.update(translator_overrides)
    return SimpleNamespace(translator=SimpleNamespace(**translator))


def _close_the_gate(monkeypatch, reason=NO_TRANSLATOR_REASON):
    from subtitles.tools.translate import availability

    monkeypatch.setattr(availability, 'settings', _gate_settings())
    return reason


def _open_the_gate(monkeypatch):
    from subtitles.tools.translate import availability

    monkeypatch.setattr(availability, 'settings', _gate_settings('google_translate'))


# ------------------------------------------------------------- manual action


def _manual_translate_call(schema_session, monkeypatch, tmp_path):
    """Call the manual translate action the way the endpoint does, with every
    dependency the action reaches recorded so a test can tell a refusal that
    queued nothing from an answer that queued a job."""
    from api.subtitles import subtitles as api_mod

    monkeypatch.setattr(api_mod, 'database', schema_session)
    monkeypatch.setattr(api_mod, 'alpha3_from_alpha2', lambda code: 'eng')
    monkeypatch.setattr(api_mod.path_mappings, 'path_replace_instance',
                        lambda path, arr_instance_id, media_type: path)

    queued = []
    monkeypatch.setattr(api_mod, 'translate_subtitles_file',
                        lambda **kwargs: queued.append(kwargs))

    schema_session.add(TableShows(id=1, arr_instance_id=3, sonarrSeriesId=7,
                                  title='S', path='/tv/s', profileId=None))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=1, arr_instance_id=3, series_id=1, sonarrSeriesId=7,
                                     sonarrEpisodeId=42, title='E', path='/tv/s/e.mkv',
                                     season=1, episode=1))
    schema_session.add(TableMovies(id=2, arr_instance_id=3, radarrId=9, title='M',
                                  path='/movies/m.mkv', tmdbId='1'))
    schema_session.commit()

    def call(media_type='movie', id=9, from_language='en'):
        args = {
            'action': 'translate', 'language': 'hu', 'path': '', 'type': media_type,
            'id': id, 'arr_instance_id': 3, 'from_language': from_language,
            'forced': 'False', 'hi': 'False',
        }
        stub_self = SimpleNamespace(
            patch_request_parser=SimpleNamespace(parse_args=lambda: args))
        return api_mod.Subtitles.patch.__wrapped__(stub_self), queued

    return call


def test_the_manual_translate_action_refuses_with_the_reason_and_queues_nothing(
        schema_session, monkeypatch, tmp_path):
    reason = _close_the_gate(monkeypatch)
    call = _manual_translate_call(schema_session, monkeypatch, tmp_path)

    answer, queued = call()

    assert answer == (reason, 409)
    assert queued == [], 'a closed gate must not queue a translation job'


def test_the_manual_translate_action_still_queues_when_the_gate_is_open(
        schema_session, monkeypatch, tmp_path):
    _open_the_gate(monkeypatch)
    call = _manual_translate_call(schema_session, monkeypatch, tmp_path)

    answer, queued = call()

    assert answer == ('', 204)
    assert len(queued) == 1


def _seed_a_sports_event(schema_session):
    """Seed the sports row the manual action resolves, with its owner.

    The handler looks a sports event up through its enabled sportarr owner, so
    the fixture has to seed both: without them the 404 below the gate answers
    before the extractor is ever reached, and a test asserting the extractor
    never ran cannot fail, whatever the code does."""
    from app.database import (TableArrInstances, TableSportsEvents,
                              TableSportsLeagues)

    schema_session.add(TableArrInstances(id=3, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, port=1867, api_key='k'))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=3, sportarrLeagueId=7,
                                         title='Formula 1', path='/sports'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(id=9, arr_instance_id=3, league_id=51,
                                         sportarrEventId=9, file_id=71,
                                         path='/sports/race.mkv', title='Race'))
    schema_session.commit()


def test_the_manual_translate_action_refuses_before_the_embedded_extractor_runs(
        schema_session, monkeypatch, tmp_path):
    """A sports translate extracts its embedded track inside the request, into
    the persistent extracted_subs directory, and a closed gate also used to
    lose to the missing-file error, so the gate has to answer first."""
    reason = _close_the_gate(monkeypatch)
    call = _manual_translate_call(schema_session, monkeypatch, tmp_path)
    _seed_a_sports_event(schema_session)
    from api.subtitles import subtitles as api_mod
    extractions = []
    monkeypatch.setattr(api_mod, 'extract_embedded_subtitle',
                        lambda *args, **kwargs: extractions.append((args, kwargs)) or '')

    answer, queued = call(media_type='sports', id=9, from_language='en')

    assert answer == (reason, 409)
    assert extractions == [], 'a closed gate must not run the embedded extractor'
    assert queued == [], 'a closed gate must not queue a translation job'


def test_the_manual_translate_action_reaches_the_extractor_when_the_gate_is_open(
        schema_session, monkeypatch, tmp_path):
    """The other half of the refusal above, on the same seeded event: an open
    gate lets the request through to the extractor, so the closed-gate test's
    empty extraction list says the gate refused and not that the fixture
    answered 404 first."""
    _open_the_gate(monkeypatch)
    call = _manual_translate_call(schema_session, monkeypatch, tmp_path)
    _seed_a_sports_event(schema_session)
    from api.subtitles import subtitles as api_mod
    extractions = []
    monkeypatch.setattr(api_mod, 'extract_embedded_subtitle',
                        lambda *args, **kwargs: extractions.append((args, kwargs)) or '')

    answer, queued = call(media_type='sports', id=9, from_language='en')

    assert len(extractions) == 1, 'an open gate runs the embedded extractor once'
    assert answer == ('Could not extract embedded subtitle: codec may be bitmap '
                      '(PGS/VobSub) or the language track was not found', 400)
    assert queued == []


# --------------------------------------------------------------- batch action


def _batch_translate_call(monkeypatch):
    from api.subtitles import batch as batch_mod

    fed = []
    monkeypatch.setattr(batch_mod, 'jobs_queue', SimpleNamespace(
        feed_jobs_pending_queue=lambda **kwargs: fed.append(kwargs)))

    def call(action='translate', items=None):
        body = {
            'action': action,
            'items': items or [{'type': 'movie', 'radarrId': 9}],
            'options': {'from_lang': 'en', 'to_lang': 'hu'},
        }
        app = Flask(__name__)
        with app.test_request_context('/api/subtitles/batch', method='POST', json=body):
            return batch_mod.BatchOperation.post.__wrapped__(
                batch_mod.BatchOperation()), fed

    return call


def test_the_batch_translate_action_refuses_with_the_reason_before_queuing(monkeypatch):
    reason = _close_the_gate(monkeypatch)
    call = _batch_translate_call(monkeypatch)

    answer, fed = call()

    assert answer == ({'error': reason}, 409)
    assert fed == [], 'a closed gate must not queue a batch job'


def test_the_batch_translate_action_still_queues_when_the_gate_is_open(monkeypatch):
    _open_the_gate(monkeypatch)
    call = _batch_translate_call(monkeypatch)

    answer, fed = call()

    assert answer[1] == 200
    assert len(fed) == 1


def test_a_batch_of_another_action_is_not_refused_by_the_translate_gate(monkeypatch):
    """A closed translate gate only refuses translation: a batch sync still
    runs, exactly as it did before."""
    _close_the_gate(monkeypatch)
    call = _batch_translate_call(monkeypatch)

    answer, fed = call(action='sync')

    assert answer[1] == 200
    assert len(fed) == 1


# ------------------------------------------------------------- health issues


# The shape the settings page writes: api/system/settings.py stores the
# profile's items as a bare list.
_PROFILE_WITH_TRANSLATE_FROM = [
    {'id': 1, 'language': 'hu', 'forced': 'False', 'hi': 'False',
     'audio_exclude': 'False', 'audio_only_include': 'False',
     'translate_from': 'en'},
]
_PROFILE_WITHOUT_TRANSLATE_FROM = [
    {'id': 1, 'language': 'en', 'forced': 'False', 'hi': 'False',
     'audio_exclude': 'False', 'audio_only_include': 'False',
     'translate_from': None},
]


def _health_settings():
    return SimpleNamespace(general=SimpleNamespace(
        use_sonarr=False, use_radarr=False, use_sportarr=False,
        serie_default_enabled=False, serie_default_profile='',
        movie_default_enabled=False, movie_default_profile='',
        sports_default_enabled=False, sports_default_profile='',
        use_postprocessing=False, postprocessing_cmd='',
    ))


def _health_with_profiles(schema_session, monkeypatch):
    from utilities import health as health_mod

    monkeypatch.setattr(health_mod, 'database', schema_session)
    monkeypatch.setattr(health_mod, 'settings', _health_settings())

    schema_session.add(TableLanguagesProfiles(
        profileId=1, name='Translating profile', cutoff=None,
        items=json.dumps(_PROFILE_WITH_TRANSLATE_FROM)))
    schema_session.add(TableLanguagesProfiles(
        profileId=2, name='Plain profile', cutoff=None,
        items=json.dumps(_PROFILE_WITHOUT_TRANSLATE_FROM)))
    schema_session.commit()
    return health_mod


def test_a_closed_gate_names_each_profile_that_translates_from_another_language(
        schema_session, monkeypatch):
    reason = _close_the_gate(monkeypatch)
    health_mod = _health_with_profiles(schema_session, monkeypatch)

    issues = health_mod.get_health_issues()

    assert {'object': 'Translating profile',
            'issue': f'This languages profile translates some languages from another one, '
                     f'and translation is unavailable: {reason}. Those languages are '
                     f'searched on providers instead of being translated.'} in issues
    assert not any(issue['object'] == 'Plain profile' for issue in issues), (
        'a profile with no translate-from item is not affected by the gate')


def test_an_open_gate_raises_no_translate_from_health_issue(schema_session, monkeypatch):
    _open_the_gate(monkeypatch)
    health_mod = _health_with_profiles(schema_session, monkeypatch)

    assert not any('translation is unavailable' in issue['issue']
                   for issue in health_mod.get_health_issues())


def test_a_profile_stored_as_an_object_still_names_the_profile(schema_session, monkeypatch):
    """Nothing writes the object shape today: the settings page stores a bare
    list. The health page reads it tolerantly anyway, so a row from another
    writer still names its profile instead of quietly raising nothing."""
    reason = _close_the_gate(monkeypatch)
    from utilities import health as health_mod

    monkeypatch.setattr(health_mod, 'database', schema_session)
    monkeypatch.setattr(health_mod, 'settings', _health_settings())
    schema_session.add(TableLanguagesProfiles(
        profileId=1, name='Translating profile', cutoff=None,
        items=json.dumps({'items': _PROFILE_WITH_TRANSLATE_FROM})))
    schema_session.commit()

    issues = health_mod.get_health_issues()

    assert {'object': 'Translating profile',
            'issue': f'This languages profile translates some languages from another one, '
                     f'and translation is unavailable: {reason}. Those languages are '
                     f'searched on providers instead of being translated.'} in issues


# ------------------------------------------------------------- system status


def _system_status_call(monkeypatch):
    from api.system import status as status_mod

    monkeypatch.setattr(status_mod, 'get_sonarr_info', SimpleNamespace(version=lambda: 'unknown'))
    monkeypatch.setattr(status_mod, 'get_radarr_info', SimpleNamespace(version=lambda: 'unknown'))
    monkeypatch.setattr(status_mod, 'get_sportarr_info', SimpleNamespace(version=lambda: 'unknown'))
    monkeypatch.setattr(status_mod, 'media_server_statuses', lambda: [])
    return status_mod.SystemStatus.get.__wrapped__(status_mod.SystemStatus())


def test_the_system_status_carries_the_gate_answer_with_its_reason(monkeypatch):
    reason = _close_the_gate(monkeypatch)

    body = _system_status_call(monkeypatch)

    assert body['data']['translation_availability'] == {'available': False, 'reason': reason}


def test_the_system_status_carries_an_open_gate(monkeypatch):
    _open_the_gate(monkeypatch)

    body = _system_status_call(monkeypatch)

    assert body['data']['translation_availability'] == {'available': True, 'reason': ''}


def _editor_gate_settings(monkeypatch, translator_type, **overrides):
    from subtitles.tools.translate import availability

    monkeypatch.setattr(availability, 'settings', _gate_settings(translator_type, **overrides))


def test_the_system_status_answers_the_editor_about_its_own_engine(monkeypatch):
    """The editor translates through the AI Subtitle Translator service
    whatever the settings page selected, so the status carries it a second
    answer for that engine. A switched-off configured engine with a working
    service must not disable an operation the editor endpoint accepts."""
    _editor_gate_settings(monkeypatch, 'none',
                           openrouter_url='http://subtitle-translator:8765')

    body = _system_status_call(monkeypatch)

    assert body['data']['translation_availability'] == {
        'available': False, 'reason': 'No translator is configured'}
    assert body['data']['editor_translation_availability'] == {'available': True, 'reason': ''}


def test_the_editor_answer_is_closed_while_the_configured_engine_is_open(monkeypatch):
    """The other direction: a configured engine that needs no service URL can
    run while the editor's service has none, so the editor's controls must
    read their own answer and not enable an operation its endpoint refuses."""
    _editor_gate_settings(monkeypatch, 'google_translate')

    body = _system_status_call(monkeypatch)

    assert body['data']['translation_availability'] == {'available': True, 'reason': ''}
    assert body['data']['editor_translation_availability'] == {
        'available': False, 'reason': 'The AI Subtitle Translator service URL is not configured'}
