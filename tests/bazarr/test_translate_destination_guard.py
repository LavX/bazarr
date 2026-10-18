# coding=utf-8
"""A queued translation never replaces a subtitle that landed while it waited.

An auto path queues a translation because the target language looks missing,
and captures the destination's versions at that moment. A provider subtitle
that lands at that destination afterwards is the better one: the write refuses
to publish over it, the job reports a skip instead of a failure, and nothing
holds the next scan back.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def _empty_record():
    from subtitles.tools.translate.failure_record import clear_failed_translations

    clear_failed_translations()
    yield
    clear_failed_translations()


def _run_translation_job(monkeypatch, tmp_path, translator, expectation):
    """Run one movies translation job and report what it did.

    Returns the translate function's result, the job names it recorded and the
    factory calls, so a test can tell a skip from a failure from a success.
    """
    from subzero.language import Language

    from subtitles.tools.translate import main as translate_main

    video = tmp_path / 'fixture.mkv'
    source = tmp_path / 'fixture.en.srt'
    source.write_text('source')
    factory_calls = []

    def _create(*args, **kwargs):
        factory_calls.append(kwargs)
        return translator

    monkeypatch.setattr(translate_main, 'validate_translation_params', lambda *a, **kw: None)
    monkeypatch.setattr(translate_main, 'convert_language_codes',
                        lambda to_lang, _forced, _hi: (Language('hun'), to_lang))
    monkeypatch.setattr(translate_main, 'alpha3_from_alpha2', lambda code: 'hun')
    monkeypatch.setattr(translate_main, 'translated_destination_path',
                        lambda video_path, to_lang, forced, hi: str(tmp_path / 'fixture.hu.srt'))
    monkeypatch.setattr(translate_main, 'activity', Mock())
    monkeypatch.setattr(translate_main, 'jobs_queue', SimpleNamespace(
        get_job_name=lambda job_id: 'Translating Fixture (EN to HU)',
        update_job_name=lambda **kwargs: renames.append(kwargs['new_job_name'])))
    monkeypatch.setattr(translate_main.TranslatorFactory, 'create_translator', staticmethod(_create))
    monkeypatch.setattr('api.subtitles.subtitles.postprocess_subtitles', lambda *a, **kw: None)
    monkeypatch.setattr('subtitles.tools.combine.main.try_combine_for_video', lambda **kw: None)

    renames = []
    result = translate_main.translate_subtitles_file(
        video_path=str(video), source_srt_file=str(source), from_lang='en', to_lang='hu',
        forced=False, hi=False, media_type='movies', sonarr_series_id=None, sonarr_episode_id=None,
        radarr_id=30, metadata=None, job_id=1, arr_instance_id=1,
        expected_destination_versions=expectation,
    )
    return result, renames, factory_calls


def _failed_hold():
    from subtitles.tools.translate.failure_record import translation_recently_failed

    return translation_recently_failed(1, 'movies', 30, 'en', 'hu', False, False)


def test_a_subtitle_that_landed_after_queueing_is_left_alone(monkeypatch, tmp_path):
    """The reported race: the queueing sighting saw no target file, one landed
    before the job ran. The job does no work and says so."""
    from subtitles.tools.subsync_engines import capture_subtitle_versions

    destination = tmp_path / 'fixture.hu.srt'
    expectation = capture_subtitle_versions(str(tmp_path / 'fixture.mkv'), (str(destination),))
    destination.write_text('the provider subtitle that landed first')
    translator = Mock()

    result, renames, factory_calls = _run_translation_job(monkeypatch, tmp_path, translator, expectation)

    assert result is None
    assert factory_calls == []
    translator.translate.assert_not_called()
    assert renames == ['Skipped Fixture (EN to HU)']
    assert destination.read_text() == 'the provider subtitle that landed first'
    assert not _failed_hold()


def test_the_write_refusal_mid_run_is_also_a_skip(monkeypatch, tmp_path):
    """A file that lands while the AI work runs is refused at publish time;
    that refusal is the same skip, not a job the user files as broken."""
    from subtitles.tools.subsync_engines import SubtitleSourceChanged, capture_subtitle_versions

    destination = tmp_path / 'fixture.hu.srt'
    expectation = capture_subtitle_versions(str(tmp_path / 'fixture.mkv'), (str(destination),))
    translator = Mock()
    translator.translate.side_effect = SubtitleSourceChanged('Queued subtitle input or destination changed')

    result, renames, factory_calls = _run_translation_job(monkeypatch, tmp_path, translator, expectation)

    assert result is None
    assert len(factory_calls) == 1
    assert renames == ['Skipped Fixture (EN to HU)']
    assert not _failed_hold()


def test_a_service_wrapped_refusal_is_still_a_skip(monkeypatch, tmp_path):
    """OpenRouter wraps the write guard's refusal in its own service error;
    the job has to see through the wrapper."""
    from subtitles.tools.subsync_engines import SubtitleSourceChanged, capture_subtitle_versions
    from subtitles.tools.translate.services.openrouter_translator import TranslationServiceError

    destination = tmp_path / 'fixture.hu.srt'
    expectation = capture_subtitle_versions(str(tmp_path / 'fixture.mkv'), (str(destination),))
    translator = Mock()

    def _refuse(job_id=None):
        raise TranslationServiceError('AI translation failed: Queued subtitle input or destination changed') \
            from SubtitleSourceChanged('Queued subtitle input or destination changed')

    translator.translate.side_effect = _refuse

    result, renames, _ = _run_translation_job(monkeypatch, tmp_path, translator, expectation)

    assert result is None
    assert renames == ['Skipped Fixture (EN to HU)']
    assert not _failed_hold()


def test_an_ordinary_failure_still_fails_the_job(monkeypatch, tmp_path):
    """The skip branch must not swallow real failures: the failure record and
    the raised error are the contract every other failure keeps."""
    from subtitles.tools.subsync_engines import capture_subtitle_versions

    destination = tmp_path / 'fixture.hu.srt'
    expectation = capture_subtitle_versions(str(tmp_path / 'fixture.mkv'), (str(destination),))
    translator = Mock()
    translator.translate.side_effect = ValueError('the provider answered 500')

    with pytest.raises(ValueError):
        _run_translation_job(monkeypatch, tmp_path, translator, expectation)

    assert _failed_hold()


def test_a_manual_translation_is_not_turned_into_a_skip(monkeypatch, tmp_path):
    """Without the auto path's capture there is no skip to report: a manual
    translation that cannot write is a failure like any other."""
    from subtitles.tools.subsync_engines import SubtitleSourceChanged

    translator = Mock()
    translator.translate.side_effect = SubtitleSourceChanged(
        'Queued subtitle input or destination changed')

    with pytest.raises(SubtitleSourceChanged):
        _run_translation_job(monkeypatch, tmp_path, translator, ())

    assert _failed_hold()


def test_an_unchanged_destination_translates(monkeypatch, tmp_path):
    """The guard only fires on a destination that moved: the ordinary queued
    translation, with nothing landing in between, runs as before."""
    from subtitles.tools.subsync_engines import capture_subtitle_versions

    destination = tmp_path / 'fixture.hu.srt'
    expectation = capture_subtitle_versions(str(tmp_path / 'fixture.mkv'), (str(destination),))
    translator = Mock()
    translator.translate.return_value = True
    translator.partial_error = None

    result, renames, factory_calls = _run_translation_job(monkeypatch, tmp_path, translator, expectation)

    assert result is True
    assert renames == ['Translated Fixture (EN to HU)']
    # The capture the auto path made reaches the service, which hands it to
    # the write as the versions it expects at publish.
    assert factory_calls[0]['expected_destination_versions'] == expectation


def test_the_write_guard_refuses_a_destination_that_appeared(tmp_path):
    """The mechanism itself: a captured destination that changed since the
    capture is refused before the block body runs, and the file stays."""
    from subtitles.tools.subsync_engines import (SubtitleSourceChanged, capture_subtitle_versions,
                                                 staged_subtitle_write)

    video = tmp_path / 'video.mkv'
    destination = tmp_path / 'video.hu.srt'
    expectation = capture_subtitle_versions(str(video), (str(destination),))
    destination.write_text('the provider subtitle that landed first')
    ran = []

    with pytest.raises(SubtitleSourceChanged):
        with staged_subtitle_write(str(video), str(destination),
                                   expected_versions=expectation):
            ran.append(True)

    assert ran == []
    assert destination.read_text() == 'the provider subtitle that landed first'


def test_the_write_kwargs_carry_the_queueing_capture(tmp_path):
    from subtitles.tools.subsync_engines import capture_subtitle_versions
    from subtitles.tools.translate.core.translator_utils import translation_write_kwargs

    destination = tmp_path / 'video.hu.srt'
    expectation = capture_subtitle_versions(str(tmp_path / 'video.mkv'), (str(destination),))
    translator = SimpleNamespace(expected_destination_versions=expectation)

    assert translation_write_kwargs(translator, 1) == {'expected_versions': expectation}


def test_a_translation_without_a_capture_keeps_its_write_open(tmp_path):
    """Manual paths queue nothing but the request, and a manual translation
    may replace what sits at its destination."""
    from subtitles.tools.translate.core.translator_utils import translation_write_kwargs

    assert translation_write_kwargs(SimpleNamespace(), 1) == {}
    assert translation_write_kwargs(SimpleNamespace(expected_destination_versions=()), 1) == {}
