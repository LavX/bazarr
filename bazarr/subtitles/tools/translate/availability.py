# coding=utf-8

"""Whether the configured translator can do anything at all.

A language profile can ask for a language to be translated from another. Every
queue path that can hand a language to the translator asks this first: the
engine used to be found out only inside the job, after the language had already
been kept away from the provider search, so a switched off or half-configured
translator meant every scan queued a job that died and the language stayed
missing forever. The answer never raises, because a configuration that cannot
even be read is just another unavailable translator with the reason why.
"""

import logging

from typing import NamedTuple

from app.config import settings
from .services.encryption import validate_encryption_key


class TranslationAvailability(NamedTuple):
    """One answer to whether a translation can run, and why when it cannot."""
    available: bool
    reason: str


# "No translator" reaches storage under several spellings: the installer writes
# the literal string 'none', a value cast from nothing at boot lands on 'None',
# and an empty picker can leave an empty string. They all mean the same thing.
NO_TRANSLATOR_SPELLINGS = (None, '', 'None', 'none')


# The engines that translate with an AI model. Google Translate (the shipped
# default) and Lingarr are not on the list: the first needs no setup and the
# second is a bridge to whatever its own service runs.
AI_TRANSLATOR_TYPES = ('openrouter', 'gemini')


def translation_available(translator_type=None):
    """The configured translator's answer to "can you translate", with a reason.

    Every rule mirrors what the engine needs to run, not what the factory can
    build: a Google translation needs no credential, the AI services need their
    own URL and key, and anything the factory does not know is not a translator.

    ``translator_type`` asks about one named engine rather than the configured
    one. The subtitle editor translates through the AI Subtitle Translator
    service whatever the settings page has selected, so it asks about that
    engine and gets the answer that engine's own branch would give.
    """
    try:
        if translator_type is None:
            translator_type = settings.translator.translator_type
        if translator_type in NO_TRANSLATOR_SPELLINGS:
            return TranslationAvailability(False, 'No translator is configured')
        if translator_type == 'google_translate':
            return TranslationAvailability(True, '')
        if translator_type == 'gemini':
            keys = settings.translator.gemini_keys
            if not any(isinstance(key, str) and key.strip() for key in (keys or [])):
                return TranslationAvailability(False, 'The Gemini engine has no API key configured')
            return TranslationAvailability(True, '')
        if translator_type == 'lingarr':
            if not str(settings.translator.lingarr_url or '').strip():
                return TranslationAvailability(False, 'The Lingarr engine has no URL configured')
            return TranslationAvailability(True, '')
        if translator_type == 'openrouter':
            # The service URL is the one thing Bazarr cannot supply itself. The
            # OpenRouter API key is not: the sidecar can hold its own. An
            # encryption key that is not exactly 64 hexadecimal characters fails
            # every job the client sends, before any request, so the gate
            # refuses those shapes with the client's own rule. A blank
            # encryption key is the sidecar's documented encryption-off mode and
            # stays available; a well-formed key that does not match the
            # sidecar's is a runtime refusal for the failure record, not
            # something the gate can know.
            if not str(settings.translator.openrouter_url or '').strip():
                return TranslationAvailability(False, 'The AI Subtitle Translator service URL is not configured')
            encryption_key = settings.translator.openrouter_encryption_key
            if encryption_key and (not isinstance(encryption_key, str)
                                   or not validate_encryption_key(encryption_key)):
                return TranslationAvailability(
                    False,
                    'The AI Subtitle Translator encryption key must be exactly 64 hexadecimal characters')
            return TranslationAvailability(True, '')
        return TranslationAvailability(False, 'Unknown translator type')
    except Exception as e:
        logging.error('BAZARR cannot read the translator configuration, so nothing can be translated: %s', e)
        return TranslationAvailability(False, f'The translator configuration cannot be read: {e}')


def ai_translator_configured():
    """Whether an AI translator is set up and the gate would let it run.

    Onboarding offers AI translation to installs that have not set it up. That
    is a narrower question than whether anything can translate, because the
    shipped Google engine always can. So the answer is the gate's own, asked
    for the configured engine, and only for an engine that uses an AI model.
    It never reads the OpenRouter API key itself: the sidecar can hold its own,
    and the gate already knows what a usable setup needs.
    """
    try:
        translator_type = settings.translator.translator_type
    except Exception as e:
        logging.error('BAZARR cannot read the translator configuration: %s', e)
        return False
    return translator_type in AI_TRANSLATOR_TYPES and translation_available(translator_type).available
