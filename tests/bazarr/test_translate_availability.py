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
        openrouter_encryption_key='',
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


# A placeholder 64-hex fixture built from a repeated fragment, exactly the
# shape the client's own validator accepts.
_HEX_KEY = '0123456789abcdef' * 4


def test_openrouter_without_a_service_url_is_unavailable(monkeypatch):
    availability = _availability(monkeypatch, 'openrouter',
                                  openrouter_encryption_key=_HEX_KEY, openrouter_api_key='fixture-key')

    assert availability.translation_available() == (
        False, 'The AI Subtitle Translator service URL is not configured')


def test_openrouter_with_a_url_and_no_api_key_is_available(monkeypatch):
    """The sidecar can hold its own OpenRouter key, so a Bazarr-side API key is
    not something the gate can require: refusing here told sidecar-key users
    translation was unavailable while their translations worked."""
    availability = _availability(
        monkeypatch, 'openrouter', openrouter_url='http://subtitle-translator:8765')

    assert availability.translation_available() == (True, '')


def test_openrouter_with_a_sidecar_key_and_no_api_key_is_available(monkeypatch):
    availability = _availability(
        monkeypatch, 'openrouter',
        openrouter_url='http://subtitle-translator:8765', openrouter_encryption_key=_HEX_KEY)

    assert availability.translation_available() == (True, '')


def test_openrouter_with_a_blank_encryption_key_is_available(monkeypatch):
    """The documented encryption-off sidecar mode: the field is left empty and
    the sidecar checks no auth token, so a blank key is a configuration the
    gate has no business refusing. A runtime refusal is the failure record's
    answer, not the gate's."""
    availability = _availability(
        monkeypatch, 'openrouter', openrouter_url='http://subtitle-translator:8765',
        openrouter_encryption_key='')

    assert availability.translation_available() == (True, '')


@pytest.mark.parametrize('key', [
    'zz',
    '0123456789abcdef',
    '0' * 63 + ' ',
    ' ' + '0' * 63,
    5 * '0123456789abcdef' + 'g',
    '0' * 64 + '\n',
], ids=['not-hex', 'too-short', 'trailing-space', 'leading-space', 'non-hex-character',
        'trailing-newline'])
def test_openrouter_with_a_malformed_encryption_key_is_unavailable(monkeypatch, key):
    """The client's encrypt_api_key raises on every job for a key that is not
    exactly 64 hexadecimal characters, so the gate refuses the same shapes,
    with the same rule the client applies. A trailing newline counts: the
    shared rule is a full match, not 64 characters followed by anything
    `$` accepts before a line end."""
    availability = _availability(
        monkeypatch, 'openrouter',
        openrouter_url='http://subtitle-translator:8765', openrouter_encryption_key=key)

    assert availability.translation_available() == (
        False, 'The AI Subtitle Translator encryption key must be exactly 64 hexadecimal characters')


def test_openrouter_with_an_uppercase_hex_key_is_available(monkeypatch):
    availability = _availability(
        monkeypatch, 'openrouter',
        openrouter_url='http://subtitle-translator:8765',
        openrouter_encryption_key='0123456789ABCDEF' * 4)

    assert availability.translation_available() == (True, '')


def test_a_named_engine_is_answered_whatever_is_configured(monkeypatch):
    """The subtitle editor uses the AI Subtitle Translator service whatever the
    settings page has selected, so it asks about that engine by name and the
    configured type is not read."""
    availability = _availability(monkeypatch, 'none', openrouter_url='http://subtitle-translator:8765')

    answer = availability.translation_available('openrouter')

    assert answer == (True, '')
    assert availability.translation_available() == (False, 'No translator is configured')


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
