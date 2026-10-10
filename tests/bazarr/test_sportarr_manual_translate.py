# coding=utf-8
"""Translating a sports subtitle from the manual toolbox.

The toolbox route accepted the request and returned 204, then the queued job
died on "An exact sports profile operation is required": translate_subtitles_file
refuses a sports translation without a bound profile operation, and the route
passed none, plus the media metadata row that a sports translation must not
carry. So the button reported success and nothing was ever written.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def use_sportarr(monkeypatch):
    """Only the tests that build an operation: importing the API with the
    switch on also starts the Sports scheduler, which needs a database."""
    from app.config import settings

    monkeypatch.setattr(settings.general, 'use_sportarr', True)


def test_the_route_binds_an_operation_and_drops_the_media_metadata():
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    branch = source[source.index("sports_operation = None"):source.index("except ValueError as exc:")]
    assert "manual_translation_operation(" in branch
    assert "metadata=None if media_type == \"sports\" else metadata" in branch
    assert "sports_operation=sports_operation" in branch


def test_the_operation_binds_its_target():
    """sports_write_kwargs compares the translation's target against the
    operation's before handing over a publication guard, so an operation bound
    without one is refused at write time exactly like no operation at all."""
    import inspect

    from sportarr.profile_hooks import manual_translation_operation, sports_write_kwargs

    binding = inspect.getsource(manual_translation_operation)
    assert "target=target" in binding
    assert "profile_item_language_code(" in binding
    guard = inspect.getsource(sports_write_kwargs)
    assert "target != operation.target" in guard


def test_a_target_the_profile_does_not_want_is_refused_with_a_reason(use_sportarr, monkeypatch):
    """A sports subtitle is only ever written for a language its profile asks
    for and does not yet have: the owned publication boundary is defined in
    those terms. Refusing here beats accepting and dropping it inside a job."""
    from sportarr import profile_hooks

    context = SimpleNamespace(mapped_path='/sports/race.mkv')
    monkeypatch.setattr('sportarr.identity.resolve_event_in_session',
                        lambda *a, **k: context)
    monkeypatch.setattr(profile_hooks, 'candidate_signature', lambda *a, **k: ('sig',))
    monkeypatch.setattr(profile_hooks, 'capture_profile_operation',
                        lambda *a, **k: object())
    monkeypatch.setattr(profile_hooks, 'missing_languages', lambda ctx: ['en'])

    with pytest.raises(ValueError, match='does not want a missing de'):
        profile_hooks.manual_translation_operation(1, 2, '/sports/race.hu.srt', 'de')


def test_a_wanted_target_binds(use_sportarr, monkeypatch):
    from sportarr import profile_hooks

    bound = {}

    class _Operation:
        def bind(self, sources, destination, **kwargs):
            bound.update(sources=sources, destination=destination, **kwargs)
            return self

    monkeypatch.setattr('sportarr.identity.resolve_event_in_session',
                        lambda *a, **k: SimpleNamespace(mapped_path='/sports/race.mkv'))
    monkeypatch.setattr(profile_hooks, 'candidate_signature', lambda *a, **k: ('sig',))
    monkeypatch.setattr(profile_hooks, 'capture_profile_operation',
                        lambda *a, **k: _Operation())
    monkeypatch.setattr(profile_hooks, 'missing_languages', lambda ctx: ['en'])
    monkeypatch.setattr(profile_hooks, 'translation_destination',
                        lambda *a, **k: '/sports/race.en.srt')

    profile_hooks.manual_translation_operation(
        1, 2, '/sports/race.hu.srt', 'en', from_language='hu')

    assert bound['sources'] == ('/sports/race.hu.srt',)
    assert bound['destination'] == '/sports/race.en.srt'
    assert bound['target'] == 'en'
    assert bound['source_language'] == 'hu'


def test_the_hi_and_forced_variants_are_distinct_targets(use_sportarr, monkeypatch):
    """A forced English file is a different subtitle from the plain one, and
    the profile tracks them separately, so the target has to carry the
    modifier or a forced request would bind the plain language's slot."""
    from sportarr import profile_hooks

    seen = []

    class _Operation:
        def bind(self, sources, destination, **kwargs):
            seen.append(kwargs['target'])
            return self

    monkeypatch.setattr('sportarr.identity.resolve_event_in_session',
                        lambda *a, **k: SimpleNamespace(mapped_path='/sports/race.mkv'))
    monkeypatch.setattr(profile_hooks, 'candidate_signature', lambda *a, **k: ('sig',))
    monkeypatch.setattr(profile_hooks, 'capture_profile_operation',
                        lambda *a, **k: _Operation())
    monkeypatch.setattr(profile_hooks, 'missing_languages',
                        lambda ctx: ['en:forced', 'en:hi'])
    monkeypatch.setattr(profile_hooks, 'translation_destination',
                        lambda *a, **k: '/sports/race.en.srt')

    profile_hooks.manual_translation_operation(1, 2, '/s.hu.srt', 'en', forced=True)
    profile_hooks.manual_translation_operation(1, 2, '/s.hu.srt', 'en', hi=True)
    assert seen == ['en:forced', 'en:hi']


def test_a_manual_translation_is_refused_while_use_sportarr_is_off(monkeypatch):
    """Turning the switch off leaves the instance row enabled. A stale page or
    a direct request must still not start a sports translation."""
    from app.config import settings
    from sportarr import profile_hooks

    def unreachable(*args, **kwargs):
        raise AssertionError('resolved the event with Use Sportarr off')

    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    monkeypatch.setattr('sportarr.identity.resolve_event_in_session', unreachable)

    with pytest.raises(ValueError, match='Sportarr is turned off'):
        profile_hooks.manual_translation_operation(1, 2, '/sports/race.hu.srt', 'de')


@pytest.mark.parametrize("forced,hi,target,expected_name", [
    (False, False, "ea", "race.es-419.srt"),
    (True, False, "ea:forced", "race.es-419.forced.srt"),
    (False, True, "ea:hi", "race.es-419.hi.srt"),
])
def test_a_sports_translation_binds_and_writes_the_regional_tag_destination(
        use_sportarr, monkeypatch, tmp_path, forced, hi, target, expected_name):
    """The sports write guard refuses a translation whose computed
    destination disagrees with the one its operation bound, so both sides
    have to resolve the same name. A translation targeting Spanish (Latino)
    binds, computes and publishes the .es-419 destination, for the plain,
    forced and HI spellings alike. translation_destination is deliberately
    not patched: the binding runs the real destination calculation."""
    from app.config import settings
    from languages import get_languages
    from sportarr import profile_hooks
    from subtitles.tools.translate import main as translate_main

    video = tmp_path / "race.mkv"
    source = tmp_path / "race.hu.srt"

    monkeypatch.setattr(settings.general, 'subfolder', 'current')
    monkeypatch.setattr(get_languages, 'languages_dict', [
        {"code2": "hu", "code3": "hun", "name": "Hungarian", "code3b": "hun"},
        {"code2": "ea", "code3": "spl", "name": "Spanish (Latino)", "code3b": "spl"},
    ], raising=False)

    context = SimpleNamespace(mapped_path=str(video), event_id=1, arr_instance_id=2)

    class _Operation:
        def __init__(self):
            self.context = context
            self.sources = ()
            self.destination = None
            self.versions = ()

        def validate(self, *args, **kwargs):
            return None

        def bind(self, sources, destination, **kwargs):
            self.sources = sources
            self.destination = destination
            self.__dict__.update(kwargs)
            return self

    monkeypatch.setattr('sportarr.identity.resolve_event_in_session', lambda *a, **k: context)
    monkeypatch.setattr(profile_hooks, 'candidate_signature', lambda *a, **k: ('sig',))
    monkeypatch.setattr(profile_hooks, 'capture_profile_operation', lambda *a, **k: _Operation())
    monkeypatch.setattr(profile_hooks, 'missing_languages', lambda ctx: [target])

    operation = profile_hooks.manual_translation_operation(
        1, 2, str(source), 'ea', from_language='hu', forced=forced, hi=hi)

    seen = {}
    translator = MagicMock()
    translator.translate.return_value = True

    def _create(kind, **kwargs):
        seen.update(kwargs)
        return translator

    monkeypatch.setattr(translate_main, 'validate_translation_params', lambda *a, **kw: None)
    monkeypatch.setattr(translate_main.TranslatorFactory, 'create_translator', staticmethod(_create))

    with patch("subtitles.tools.combine.main.try_combine_for_video", lambda **kw: None):
        translate_main.translate_subtitles_file(
            video_path=str(video), source_srt_file=str(source),
            from_lang='hu', to_lang='ea', forced=forced, hi=hi,
            media_type='sports', sonarr_series_id=None, sonarr_episode_id=None,
            radarr_id=None, metadata=None, job_id='job-1', arr_instance_id=2,
            sports_operation=operation)

    # The guard hands over the publication kwargs only when the write path's
    # destination matches the one the operation bound.
    write_translator = SimpleNamespace(
        sports_operation=operation, cancel=None, orig_to_lang='ea', forced=forced, hi=hi,
        video_path=str(video), source_srt_file=str(source), dest_srt_file=seen['dest_srt_file'],
        media_type='sports', arr_instance_id=2, from_lang='hu',
        radarr_id=None, sonarr_series_id=None, sonarr_episode_id=None)
    write_kwargs = profile_hooks.sports_write_kwargs(write_translator, 'job-1')

    assert seen['dest_srt_file'] == operation.destination == str(tmp_path / expected_name), (
        "the write path and the bound operation must resolve the same "
        "regional-tagged destination")
    assert 'publication_guard' in write_kwargs
