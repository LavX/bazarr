# coding=utf-8
"""Lingarr as a legacy engine of the translation runtime.

Lingarr is not a Provider Hub plugin and has no worker. It stays what it was: a bridge the in-core service
``services/lingarr_translator.py`` talks to, configured by the two settings it always used
(``translator.lingarr_url`` and the write-only ``translator.lingarr_token``). Nothing is migrated: the
runtime only gives it an engine instance, so it can be listed, chosen as the engine or the fallback and
configured from the Translation page like any other engine.

What exists today, in one place:

* ``LINGARR_FIELDS`` is its small schema: an address (a destination, so the consent rules apply to it) and a
  token (a secret, so it never comes out).
* ``configured`` is true once the person has set an address: a value other than the shipped default, an
  address saved from the page, or an install that already ran Lingarr (it was the selected translator).
* ``legacy_engine_for`` is the seam dispatch reads: a job whose engine instance has a plugin id this returns an
  engine for does not go to a worker. It runs the in-core service named by ``translator_type`` through
  ``TranslatorFactory.create_translator``, as the old path did, with no connections, no cost and history rows
  that name the plugin id ``lingarr``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

LINGARR_PLUGIN_ID = "lingarr"
# The page's names for the two fields. The values live in the settings Lingarr always used.
URL_KEY = "url"
TOKEN_KEY = "token"
URL_SETTING = "lingarr_url"
TOKEN_SETTING = "lingarr_token"
DEFAULT_URL = "http://lingarr:9876"
URL_LIMIT = 2048
# Kept in the engine instance's settings once the person has saved an address from the page, so the shipped
# default address does not count as set up until they have.
SAVED_FLAG = "address_saved"
DESCRIPTION = "Sends each job to your own Lingarr service."
# A token shorter than this shows only that it is set, like every other secret.
LAST_FOUR_MIN_LENGTH = 16

# The schema the Translation page draws: the same shape a manifest's fields take.
LINGARR_FIELDS: tuple[Mapping[str, Any], ...] = (
    {"key": URL_KEY, "type": "text", "label": "Lingarr URL", "required": True, "destination": True,
     "placeholder": DEFAULT_URL, "help": "The address Bazarr+ reaches your Lingarr at."},
    {"key": TOKEN_KEY, "type": "secret", "label": "API token", "help": "Only when your Lingarr asks for one."},
)


@dataclass(frozen=True)
class LegacyEngine:
    """An engine served by code in Bazarr+ itself instead of by a plugin worker."""

    plugin_id: str
    name: str
    # The type ``TranslatorFactory.create_translator`` builds the service for.
    translator_type: str


LEGACY_ENGINES: Mapping[str, LegacyEngine] = {
    LINGARR_PLUGIN_ID: LegacyEngine(LINGARR_PLUGIN_ID, "Lingarr", "lingarr")}


def legacy_engine_for(plugin_id: str | None) -> LegacyEngine | None:
    """The in-core engine a plugin id names, or None for an ordinary plugin."""
    return LEGACY_ENGINES.get(plugin_id or "")


class ConfigLingarr:
    """The Lingarr address and token as they live in config.yaml. Tests swap this for an in-memory double."""

    def lock(self):
        from discover.metadata import CONFIG_LOCK

        return CONFIG_LOCK

    def url(self) -> str:
        from app.config import settings

        value = getattr(settings.translator, URL_SETTING, "")
        return value.strip() if isinstance(value, str) else ""

    def selected(self) -> bool:
        """Whether this install already ran Lingarr: it was the translator the old settings page selected."""
        from app.config import settings

        return getattr(settings.translator, "translator_type", "") == "lingarr"

    def token(self) -> str:
        """The stored token, for the length and last characters a response may say. Never sent out whole."""
        from app.config import settings

        value = getattr(settings.translator, TOKEN_SETTING, "")
        return value.strip() if isinstance(value, str) else ""

    def set(self, url: str, token: str | None) -> bool:
        """Write the address, and the token unless it is None. False, with the live values put back, on failure."""
        from app.config import settings, write_config

        section = settings.translator
        previous = (section.lingarr_url, section.lingarr_token)
        try:
            section.lingarr_url = url
            if token is not None:
                section.lingarr_token = token
            saved = write_config()
        except Exception:
            logger.exception("Could not save the Lingarr settings")
            saved = False
        if saved is not True:
            section.lingarr_url, section.lingarr_token = previous
            return False
        return True


def configured(config: ConfigLingarr | None = None, instance_settings: Mapping[str, Any] | None = None) -> bool:
    """Whether Lingarr may serve a job: the person has set its address. The token is optional, as it always was.

    The shipped default address alone does not count, because nothing has been set up yet. An address that
    differs from it, one saved from the page (``instance_settings`` carries the flag) and an install that
    already used Lingarr do.
    """
    config = config or ConfigLingarr()
    url = config.url()
    if not url:
        return False
    if url != DEFAULT_URL:
        return True
    return bool((instance_settings or {}).get(SAVED_FLAG)) or config.selected()
