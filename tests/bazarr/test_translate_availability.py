# coding=utf-8

"""Whether the configured translator can do anything at all.

A language profile can ask for a language to be translated from another. The
queue paths used to find out only inside the job, after the language had
already been kept away from the provider search, so a switched off or
half-configured translator meant every scan queued a job that died and the
language stayed missing forever. The availability gate answers before anything
is queued, for every engine and every spelling of off.
"""

import logging
from types import SimpleNamespace

import pytest


def _settings(translator_type, **overrides):
    translator = SimpleNamespace(
        translator_type=translator_type,
        gemini_keys=[],
        lingarr_url='',
        openrouter_url='',
        openrouter_api_key='',
    )
    for name, value in overrides.items():
        setattr(translator, name, value)
    return SimpleNamespace(translator=translator)


class _ExplodingTranslator:
    @property
    def translator_type(self):
        raise RuntimeError('fixture: the setting cannot be read')


def _availability(monkeypatch, translator_type, **overrides):
    from subtitles.tools.translate import availability

    monkeypatch.setattr(availability, 'settings', _settings(translator_type, **overrides))
    return availability


@pytest.mark.parametrize('translator_type', [None, '', 'None', 'none'])
def test_every_spelling_of_off_is_unavailable_with_the_no_translator_reason(
        monkeypatch, translator_type):
    availability = _availability(monkeypatch, translator_type)

    assert availability.translation_available() == (False, 'No translator is configured')


def test_google_translate_needs_no_credential(monkeypatch):
    assert _availability(monkeypatch, 'google_translate').translation_available() == (True, '')


@pytest.mark.parametrize('keys', [[], [''], ['', '   ']], ids=['no-key', 'blank-key', 'blank-keys'])
def test_gemini_without_a_usable_key_is_unavailable(monkeypatch, keys):
    availability = _availability(monkeypatch, 'gemini', gemini_keys=keys)

    assert availability.translation_available() == (
        False, 'The Gemini engine has no API key configured')


def test_gemini_with_a_configured_key_is_available(monkeypatch):
    availability = _availability(monkeypatch, 'gemini', gemini_keys=['fixture-key'])

    assert availability.translation_available() == (True, '')


def test_lingarr_without_a_url_is_unavailable(monkeypatch):
    availability = _availability(monkeypatch, 'lingarr')

    assert availability.translation_available() == (False, 'The Lingarr engine has no URL configured')


def test_lingarr_with_a_url_is_available(monkeypatch):
    availability = _availability(monkeypatch, 'lingarr', lingarr_url='http://lingarr:9876')

    assert availability.translation_available() == (True, '')


def test_openrouter_without_a_service_url_is_unavailable(monkeypatch):
    availability = _availability(monkeypatch, 'openrouter', openrouter_api_key='fixture-key')

    assert availability.translation_available() == (
        False, 'The AI Subtitle Translator service URL is not configured')


def test_openrouter_without_an_api_key_is_unavailable(monkeypatch):
    availability = _availability(
        monkeypatch, 'openrouter', openrouter_url='http://subtitle-translator:8765')

    assert availability.translation_available() == (
        False, 'The AI Subtitle Translator API key is not configured')


def test_openrouter_with_a_url_and_a_key_is_available(monkeypatch):
    availability = _availability(
        monkeypatch, 'openrouter',
        openrouter_url='http://subtitle-translator:8765', openrouter_api_key='fixture-key')

    assert availability.translation_available() == (True, '')


@pytest.mark.parametrize('translator_type', ['google', 'lingar', 'Google', 'open_router'])
def test_a_type_the_factory_does_not_know_is_unavailable(monkeypatch, translator_type):
    availability = _availability(monkeypatch, translator_type)

    assert availability.translation_available() == (False, 'Unknown translator type')


def test_an_error_reading_the_configuration_is_unavailable_and_logged_once(monkeypatch, caplog):
    from subtitles.tools.translate import availability

    monkeypatch.setattr(
        availability, 'settings', SimpleNamespace(translator=_ExplodingTranslator()))

    with caplog.at_level(logging.ERROR):
        answer = availability.translation_available()

    assert answer.available is False
    assert 'fixture: the setting cannot be read' in answer.reason
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1


@pytest.mark.parametrize('stored, expected', [
    (None, 'none'),
    ('', 'none'),
    ('None', 'none'),
    ('none', 'none'),
    ('google_translate', 'google_translate'),
    ('openrouter', 'openrouter'),
    (5, '5'),
], ids=['null', 'empty', 'str-cast-null', 'installer-off', 'google', 'openrouter', 'non-string'])
def test_the_boot_normalization_writes_one_spelling_of_off(stored, expected):
    from app.config import normalize_translator_type

    assert normalize_translator_type(stored) == expected
