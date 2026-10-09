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


class TranslationAvailability(NamedTuple):
    """One answer to whether a translation can run, and why when it cannot."""
    available: bool
    reason: str


# "No translator" reaches storage under several spellings: the installer writes
# the literal string 'none', a value cast from nothing at boot lands on 'None',
# and an empty picker can leave an empty string. They all mean the same thing.
NO_TRANSLATOR_SPELLINGS = (None, '', 'None', 'none')


def translation_available():
    """The configured translator's answer to "can you translate", with a reason.

    Every rule mirrors what the engine needs to run, not what the factory can
    build: a Google translation needs no credential, the AI services need their
    own URL and key, and anything the factory does not know is not a translator.
    """
    try:
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
            if not str(settings.translator.openrouter_url or '').strip():
                return TranslationAvailability(False, 'The AI Subtitle Translator service URL is not configured')
            if not str(settings.translator.openrouter_api_key or '').strip():
                return TranslationAvailability(False, 'The AI Subtitle Translator API key is not configured')
            return TranslationAvailability(True, '')
        return TranslationAvailability(False, 'Unknown translator type')
    except Exception as e:
        logging.error('BAZARR cannot read the translator configuration, so nothing can be translated: %s', e)
        return TranslationAvailability(False, f'The translator configuration cannot be read: {e}')
