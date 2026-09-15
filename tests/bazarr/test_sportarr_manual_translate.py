# coding=utf-8
"""Translating a sports subtitle from the manual toolbox.

The toolbox route accepted the request and returned 204, then the queued job
died on "An exact sports profile operation is required": translate_subtitles_file
refuses a sports translation without a bound profile operation, and the route
passed none, plus the media metadata row that a sports translation must not
carry. So the button reported success and nothing was ever written.
"""

from types import SimpleNamespace

import pytest


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


def test_a_target_the_profile_does_not_want_is_refused_with_a_reason(monkeypatch):
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


def test_a_wanted_target_binds(monkeypatch):
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


def test_the_hi_and_forced_variants_are_distinct_targets(monkeypatch):
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
