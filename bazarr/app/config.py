# coding=utf-8

import hashlib  # noqa: F401
import os
import ast
import contextlib
import json
import logging
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from typing import NamedTuple

import configparser
import yaml
import platform

from urllib.parse import quote_plus
from utilities.binaries import BinaryNotFound, get_binary
from literals import (EXIT_VALIDATION_ERROR, LOG_BACKUP_COUNT_DEFAULT, LOG_BACKUP_COUNT_MAX, LOG_BACKUP_COUNT_MIN,
                      LOG_MAX_FILE_SIZE_MB_DEFAULT, LOG_MAX_FILE_SIZE_MB_MAX, LOG_MAX_FILE_SIZE_MB_MIN)
from utilities.central import stop_bazarr
from subliminal.cache import region
from dynaconf import Dynaconf, Validator as OriginalValidator
from dynaconf.loaders.yaml_loader import write
from dynaconf.validator import ValidationError
from dynaconf.utils.functional import empty
from ipaddress import ip_address
from binascii import hexlify
from types import MappingProxyType
from shutil import move

from .get_args import args

NoneType = type(None)


def base_url_slash_cleaner(uri):
    while "//" in uri:
        uri = uri.replace("//", "/")
    return uri


def validate_ip_address(ip_string):
    if ip_string == '*':
        return True
    try:
        ip_address(ip_string)
        return True
    except ValueError:
        return False

def is_not_bool(value):
    # is_type_of=int accepts True and False, and a form value of 'true' is saved as True.
    return not isinstance(value, bool)


def validate_tags(tags):
    if not tags:
        return True

    return all(re.match( r'^[a-z0-9_-]+$', item) for item in tags)


def normalize_openrouter_provider_order(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValidationError('OpenRouter providers must be a list of at most 20 provider slugs.')
    normalized = []
    for provider in value:
        if not isinstance(provider, str):
            raise ValidationError('OpenRouter provider slugs must be text.')
        provider = provider.strip().lower()
        if len(provider) > 160:
            raise ValidationError('OpenRouter provider slugs must be at most 160 characters.')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*', provider):
            raise ValidationError('OpenRouter providers must contain nonempty provider or endpoint slugs, '
                                  'such as deepinfra or parasail/fp8.')
        if provider not in normalized:
            normalized.append(provider)
    return normalized


# What a new install gets, and what an install that predates the setting gets instead.
# smartfast needs AI Subtitle Translator 2.0.0; throughput is served by every version, so
# an upgrade is never moved onto a routing its translator might refuse.
DEFAULT_PROVIDER_ROUTING = 'smartfast'
UPGRADED_PROVIDER_ROUTING = 'throughput'
# Every routing the selector can store. Shared by the validator's is_in and the boot-time
# normalization below so the two can never drift apart (the translation service module keeps
# its own mirror, but a value has to satisfy this set before it is ever persisted).
PROVIDER_ROUTING_VALUES = ('throughput', 'nitro', 'price', 'floor', 'latency', 'default', 'smartfast', 'custom')


def normalize_stored_provider_routing(stored_routing):
    """The routing an existing config should run with, given its stored value.

    A value we understand is kept verbatim. A missing value (``None``) and any value
    outside ``PROVIDER_ROUTING_VALUES`` are configs we do not understand, and both land
    on the plain sort every translator serves rather than on the shipped default, which
    refuses outright ahead of AI Subtitle Translator 2.0.0. A value we failed to parse is
    not evidence about which translator version is running, so it must not select a mode
    that can refuse.
    """
    if stored_routing in PROVIDER_ROUTING_VALUES:
        return stored_routing
    return UPGRADED_PROVIDER_ROUTING


def normalize_translator_type(stored_translator_type):
    """The translator an existing config should run with, given its stored value.

    "No translator" reaches storage under several spellings: the installer writes
    the literal string ``none``, a value cast from nothing lands on ``'None'``,
    and an emptied picker can leave an empty string. They are all one setting,
    so they are all kept under the one spelling the rest of Bazarr reads, and
    anything else is kept verbatim for the availability gate to answer for.
    """
    if stored_translator_type is None or stored_translator_type in ('', 'None'):
        return 'none'
    return stored_translator_type if isinstance(stored_translator_type, str) else str(stored_translator_type)


def migrate_upgrade_subtitle_toggles(settings, existing_config) -> bool:
    """Split the combined manual/translated upgrade toggle.

    New installs get both off via validator defaults. Existing configs keep
    whatever they already stored for ``upgrade_manual``, or the old default
    (on) if the key was missing. Translated upgrades start off and are never
    copied from the old combined value.
    """
    if settings.get('general.upgrade_translated') is not None:
        return False
    settings['general.upgrade_translated'] = False
    if existing_config and settings.get('general.upgrade_manual') is None:
        settings['general.upgrade_manual'] = True
    return True


# Discover's search deadline. The default rose from 20 to 40 seconds, but every install
# that ran before that has the old default saved, because startup writes every default
# into config.yaml.
OLD_DEFAULT_SEARCH_TIMEOUT_SECONDS = 20
DEFAULT_SEARCH_TIMEOUT_SECONDS = 40


def migrate_search_timeout_default(settings) -> bool:
    """Move an existing config off the old 20 second search deadline, once.

    A saved 20 cannot be told apart from a 20 the user typed, so the move runs a
    single time and records ``compat_endpoint.search_timeout_migrated``. A user who
    sets 20 again afterwards keeps it, and any other saved value is left alone. A new
    install never needs this: the validator defaults give it 40 and the marker.
    Returns True when the saved deadline was changed.
    """
    if settings.get('compat_endpoint.search_timeout_migrated') is True:
        return False
    settings['compat_endpoint.search_timeout_migrated'] = True
    if settings.get('compat_endpoint.search_timeout_seconds') != OLD_DEFAULT_SEARCH_TIMEOUT_SECONDS:
        return False
    settings['compat_endpoint.search_timeout_seconds'] = DEFAULT_SEARCH_TIMEOUT_SECONDS
    logging.info("Discover search deadline was the old %d second default; raised it to the new default of "
                 "%d seconds. Change it under Distribution Hub > Settings > Global search timeout.",
                 OLD_DEFAULT_SEARCH_TIMEOUT_SECONDS, DEFAULT_SEARCH_TIMEOUT_SECONDS)
    return True


ONE_HUNDRED_YEARS_IN_MINUTES = 52560000
ONE_HUNDRED_YEARS_IN_HOURS = 876000


class Validator(OriginalValidator):
    # Give the ability to personalize messages sent by the original dynasync Validator class.
    default_messages = MappingProxyType(
        {
            "must_exist_true": "{name} is required",
            "must_exist_false": "{name} cannot exists",
            "condition": "{name} invalid for {function}({value})",
            "operations": "{name} must {operation} {op_value} but it is {value}",
            "combined": "combined validators failed {errors}",
        }
    )


def check_parser_binary(value):
    try:
        get_binary(value)
    except BinaryNotFound:
        raise ValidationError(f"Executable '{value}' not found in search path. Please install before making this selection.")
    return True


def _allowed_hosts_from_config(value):
    """The comma-separated form of general.allowed_hosts, as a list.

    A config.yaml edited by hand names one hostname most naturally as a
    scalar, and the validator would otherwise reset that to an empty list,
    so the scalar is read as a one-entry list the same way the environment
    variable is.
    """
    if isinstance(value, str):
        return [entry.strip() for entry in value.split(',') if entry.strip()]
    return value


validators = [
    # general section
    Validator('general.flask_secret_key', must_exist=True, default=hexlify(os.urandom(16)).decode(),
              is_type_of=str),
    # Master key for the bazarr.secrets crypto module (encrypts every
    # user-visible credential at rest). Default empty so the key is
    # generated lazily on first read - matches the pattern of the legacy
    # plex.encryption_key. Never round-trips through the API.
    Validator('general.secrets_encryption_key', must_exist=True, default='', is_type_of=str),
    Validator('general.ip', must_exist=True, default='*', is_type_of=str, condition=validate_ip_address),
    Validator('general.port', must_exist=True, default=6767, is_type_of=int, gte=1, lte=65535),
    # Waitress worker threads. The old inherited constant was 100, a 25x
    # multiple of waitress's own default that dominated the process's thread
    # count and resident memory; see the default's justification in server.py
    # (Socket.IO long-polling parks one worker per open browser tab).
    Validator('general.web_server_threads', must_exist=True, default=32, is_type_of=int, gte=4, lte=100),
    Validator('general.hostname', must_exist=True, default=platform.node(), is_type_of=str),
    # The one address whose X-Forwarded-* headers waitress may believe. The
    # default covers only the in-container supervisor hop, so a reverse proxy
    # running in another container is not trusted until it is named here:
    # without that the request scheme reads as http behind HTTPS, and every
    # client shares one rate-limit bucket because remote_addr is the proxy's
    # address. Empty means trust nothing.
    #
    # Deliberately a single address rather than a list: waitress compares the
    # peer against this value exactly (proxy_headers.py), so a comma-separated
    # list matches no one and would silently trust less than the old hardcoded
    # default did.
    Validator('general.trusted_proxy', must_exist=True, default='127.0.0.1', is_type_of=str, cast=str),
    # Hostnames the request-host check answers while no login is configured:
    # exact names, *.example.com for a domain, or a lone '*' to answer every
    # hostname (the health page names that switch). The default set (IP
    # literals, localhost, single-label names and local-only suffixes) always
    # applies; entries here add to it. The cast accepts the comma-separated
    # string a hand edit writes, as _allowed_hosts_from_config does.
    Validator('general.allowed_hosts', must_exist=True, default=[], is_type_of=list,
              cast=_allowed_hosts_from_config),
    Validator('general.base_url', must_exist=True, default='', is_type_of=str),
    Validator('general.instance_name', must_exist=True, default='Bazarr+', is_type_of=str,
              apply_default_on_none=True),
    Validator('general.path_mappings', must_exist=True, default=[], is_type_of=list),
    Validator('general.debug', must_exist=True, default=False, is_type_of=bool),
    Validator('general.branch', must_exist=True, default='master', is_type_of=str,
              is_in=['master', 'development']),
    Validator('general.auto_update', must_exist=True, default=True, is_type_of=bool),
    Validator('general.single_language', must_exist=True, default=False, is_type_of=bool),
    Validator('general.minimum_score', must_exist=True, default=80, is_type_of=int, gte=0, lte=100),
    Validator('general.use_scenename', must_exist=True, default=True, is_type_of=bool),
    # Off by default: it notifies, and an unasked-for notification stream is
    # worse than the problem it reports.
    Validator('general.detect_release_type_mismatch', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_postprocessing', must_exist=True, default=False, is_type_of=bool),
    Validator('general.postprocessing_cmd', must_exist=True, default='', is_type_of=str),
    Validator('general.postprocessing_threshold', must_exist=True, default=90, is_type_of=int, gte=0, lte=100),
    Validator('general.use_postprocessing_threshold', must_exist=True, default=False, is_type_of=bool),
    Validator('general.postprocessing_threshold_movie', must_exist=True, default=70, is_type_of=int, gte=0,
              lte=100),
    Validator('general.use_postprocessing_threshold_movie', must_exist=True, default=False, is_type_of=bool),
    # External webhook integration
    Validator('general.use_external_webhook', must_exist=True, default=False, is_type_of=bool),
    Validator('general.external_webhook_url', must_exist=True, default='', is_type_of=str),
    Validator('general.external_webhook_username', must_exist=True, default='', is_type_of=str),
    Validator('general.external_webhook_password', must_exist=True, default='', is_type_of=str),
    Validator('general.use_sonarr', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_radarr', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_sportarr', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_plex', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_jellyfin', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_emby', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_silo', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_seerr', must_exist=True, default=False, is_type_of=bool),
    # Set True once the first-run onboarding wizard is completed or skipped, so it
    # never auto-triggers again. Defaults False on a fresh install.
    Validator('general.setup_complete', must_exist=True, default=False, is_type_of=bool),
    Validator('general.path_mappings_movie', must_exist=True, default=[], is_type_of=list),
    Validator('general.path_mappings_sports', must_exist=True, default=[], is_type_of=list),
    Validator('general.serie_tag_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.movie_tag_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.sports_tag_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.remove_profile_tags', must_exist=True, default=[], is_type_of=list, condition=validate_tags),
    Validator('general.serie_default_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.serie_default_profile', must_exist=True, default='', is_type_of=(int, str)),
    Validator('general.movie_default_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.movie_default_profile', must_exist=True, default='', is_type_of=(int, str)),
    Validator('general.sports_default_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.sports_default_profile', must_exist=True, default='', is_type_of=(int, str)),
    Validator('general.page_size', must_exist=True, default=25, is_type_of=int,
              is_in=[25, 50, 100, 250, 500, 1000]),
    Validator('general.theme', must_exist=True, default='auto', is_type_of=str,
              is_in=['auto', 'light', 'dark']),
    Validator('general.show_live_badge', must_exist=True, default=True, is_type_of=bool),
    Validator('general.minimum_score_movie', must_exist=True, default=70, is_type_of=int, gte=0, lte=100),
    Validator('general.minimum_score_sports', must_exist=True, default=70, is_type_of=int, gte=0, lte=100),
    Validator('general.use_embedded_subs', must_exist=True, default=True, is_type_of=bool),
    Validator('general.embedded_subs_show_desired', must_exist=True, default=True, is_type_of=bool),
    Validator('general.mass_embedded_extract_workers', must_exist=True, default=2, is_type_of=int, gte=1, lte=4),
    Validator('general.utf8_encode', must_exist=True, default=True, is_type_of=bool),
    Validator('general.ignore_pgs_subs', must_exist=True, default=False, is_type_of=bool),
    Validator('general.ignore_vobsub_subs', must_exist=True, default=False, is_type_of=bool),
    Validator('general.ignore_ass_subs', must_exist=True, default=False, is_type_of=bool),
    Validator('general.adaptive_searching', must_exist=True, default=True, is_type_of=bool),
    Validator('general.adaptive_searching_delay', must_exist=True, default='3w', is_type_of=str,
              is_in=['1w', '2w', '3w', '4w']),
    Validator('general.adaptive_searching_delta', must_exist=True, default='1w', is_type_of=str,
              is_in=['3d', '1w', '2w', '3w', '4w']),
    Validator('general.adaptive_searching_max_age', must_exist=True, default='', is_type_of=str,
              is_in=['', '1m', '3m', '6m', '1y', '2y']),
    Validator('general.enabled_providers', must_exist=True, default=[], is_type_of=list),
    Validator('general.provider_priorities', must_exist=True, default={}, is_type_of=dict),
    Validator('general.provider_languages', must_exist=True, default={}, is_type_of=dict),
    Validator('general.provider_score_modifiers', must_exist=True, default={}, is_type_of=dict),
    Validator('general.ai_translated_score_penalty', must_exist=True, default=0, is_type_of=int, gte=0, lte=100),
    Validator('general.use_provider_priority', must_exist=True, default=True, is_type_of=bool),
    Validator('general.enabled_integrations', must_exist=True, default=[], is_type_of=list),
    Validator('general.multithreading', must_exist=True, default=True, is_type_of=bool),
    Validator('general.chmod_enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('general.enable_strm_support', must_exist=True, default=False, is_type_of=bool),
    Validator('general.provider_hub_auto_install', must_exist=True, default=False, is_type_of=bool),
    Validator('general.provider_hub_worker_timeout', must_exist=True, default=120, is_type_of=int,
              gte=30, lte=86400),
    Validator('general.chmod', must_exist=True, default='0640', is_type_of=str),
    Validator('general.subfolder', must_exist=True, default='current', is_type_of=str),
    Validator('general.subfolder_custom', must_exist=True, default='', is_type_of=str),
    Validator('general.use_whisper_fallback', must_exist=True, default=False, is_type_of=bool),
    Validator('general.use_whisper_fallback_series', must_exist=True, default=False, is_type_of=bool),
    Validator('general.upgrade_subs', must_exist=True, default=True, is_type_of=bool),
    Validator('general.upgrade_frequency', must_exist=True, default=12, is_type_of=int,
              is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS]),
    Validator('general.days_to_upgrade_subs', must_exist=True, default=7, is_type_of=int, gte=0, lte=30),
    Validator('general.upgrade_manual', must_exist=True, default=False, is_type_of=bool),
    Validator('general.upgrade_translated', must_exist=True, default=False, is_type_of=bool),
    Validator('general.anti_captcha_provider', must_exist=True, default=None, is_type_of=(NoneType, str),
              is_in=[None, 'anti-captcha', 'death-by-captcha', 'captchaai']),
    Validator('general.wanted_search_frequency', must_exist=True, default=6, is_type_of=int, 
              is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS]),
    Validator('general.wanted_search_frequency_movie', must_exist=True, default=6, is_type_of=int,
              is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS]),
    Validator('general.wanted_search_frequency_sports', must_exist=True, default=6, is_type_of=int,
              is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS]),
    Validator('general.subzero_mods', must_exist=True, default='', is_type_of=str),
    Validator('general.subzero_mods_keep_lyrics', must_exist=True, default=False, is_type_of=bool),
    Validator('general.dont_notify_manual_actions', must_exist=True, default=False, is_type_of=bool),
    Validator('general.notify_if_nothing_is_missing_for_signalr_event', must_exist=True, default=False, is_type_of=bool),
    Validator('general.hi_extension', must_exist=True, default='hi', is_type_of=str, is_in=['hi', 'cc', 'sdh']),
    Validator('general.embedded_subtitles_parser', must_exist=True, default='ffprobe', is_type_of=str,
              is_in=['ffprobe', 'mediainfo'], condition=check_parser_binary),
    Validator('general.default_und_audio_lang', must_exist=True, default='', is_type_of=str),
    Validator('general.default_und_embedded_subtitles_lang', must_exist=True, default='', is_type_of=str),
    Validator('general.parse_embedded_audio_track', must_exist=True, default=False, is_type_of=bool),
    Validator('general.skip_hashing', must_exist=True, default=False, is_type_of=bool),
    Validator('general.language_equals', must_exist=True, default=[], is_type_of=list),
    Validator('general.concurrent_jobs', must_exist=True, default=4 if os.cpu_count() >= 4 else os.cpu_count(),
              is_type_of=int),

    # log section
    Validator('log.include_filter', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('log.exclude_filter', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('log.ignore_case', must_exist=True, default=False, is_type_of=bool),
    Validator('log.use_regex', must_exist=True, default=False, is_type_of=bool),
    # Logger names whose third-party level ceiling general.debug is allowed to
    # lift. Empty by default: socketio.server and apscheduler at DEBUG are most
    # of a debug-mode log, so asking for them back is opt-in.
    Validator('log.verbose_loggers', must_exist=True, default=[], is_type_of=list),
    # The log folder's ceiling is max_file_size_mb * (backup_count + 1): the
    # live file rolls at midnight or at this size, whichever comes first, and
    # this many rolled files are kept. A backup count of 0 would mean "never
    # delete" to the handler, so the floor is 1.
    Validator('log.max_file_size_mb', must_exist=True, default=LOG_MAX_FILE_SIZE_MB_DEFAULT, is_type_of=int,
              gte=LOG_MAX_FILE_SIZE_MB_MIN, lte=LOG_MAX_FILE_SIZE_MB_MAX, condition=is_not_bool),
    Validator('log.backup_count', must_exist=True, default=LOG_BACKUP_COUNT_DEFAULT, is_type_of=int,
              gte=LOG_BACKUP_COUNT_MIN, lte=LOG_BACKUP_COUNT_MAX, condition=is_not_bool),

    # auth section
    Validator('auth.apikey', must_exist=True, default=hexlify(os.urandom(16)).decode(), is_type_of=str),
    Validator('auth.type', must_exist=True, default=None, is_type_of=(NoneType, str),
              is_in=[None, 'basic', 'form']),
    Validator('auth.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('auth.password', must_exist=True, default='', is_type_of=str, cast=str),
    # How long a signed-in browser may sit idle before it has to sign in
    # again. The window slides forward on each request, so an active browser
    # stays signed in. Flask's cookie carried no expiry at all, so a session
    # lasted until the browser was closed, or forever in one that restores
    # tabs.
    Validator('auth.session_lifetime_days', must_exist=True, default=30, is_type_of=int, gte=1, lte=365),
    # Secure flag policy for the session cookie. 'auto' follows the scheme of
    # the request the cookie is set on, which is what a plain-http LAN install
    # needs; 'always' is the right answer behind an HTTPS reverse proxy that
    # this instance cannot detect (see general.trusted_proxy).
    Validator('auth.cookie_secure', must_exist=True, default='auto', is_type_of=str,
              is_in=['auto', 'always', 'never']),
    # Which batch of signed session cookies is still honoured. Logout, a
    # password change and an auth-type change advance it, and a cookie issued
    # under an earlier number stops working. Server-owned: the settings read
    # hides it and the settings save refuses it.
    Validator('auth.session_generation', must_exist=True, default=0, is_type_of=int, gte=0,
              condition=is_not_bool),

    # cors section
    Validator('cors.enabled', must_exist=True, default=False, is_type_of=bool),

    # backup section
    Validator('backup.folder', must_exist=True, default=os.path.join(args.config_dir, 'backup'),
              is_type_of=str),
    Validator('backup.retention', must_exist=True, default=31, is_type_of=int, gte=0),
    Validator('backup.frequency', must_exist=True, default='Weekly', is_type_of=str,
              is_in=['Manually', 'Daily', 'Weekly']),
    Validator('backup.day', must_exist=True, default=6, is_type_of=int, gte=0, lte=6),
    Validator('backup.hour', must_exist=True, default=3, is_type_of=int, gte=0, lte=23),

    # translating section
    Validator('translator.min_source_score', must_exist=True, default=90, is_type_of=int, gte=0, lte=100),
    Validator('translator.default_score', must_exist=True, default=50, is_type_of=int, gte=0),
    Validator('translator.gemini_keys', must_exist=True, default=[], is_type_of=list),
    Validator('translator.gemini_model', must_exist=True, default='gemini-2.0-flash', is_type_of=str, cast=str),
    Validator('translator.gemini_batch_size', must_exist=True, default=300, is_type_of=int, gte=1),
    Validator('translator.translator_info', must_exist=True, default=True, is_type_of=bool),
    Validator('translator.translator_type', must_exist=True, default='google_translate', is_type_of=str,
              cast=normalize_translator_type),
    Validator('translator.lingarr_url', must_exist=True, default='http://lingarr:9876', is_type_of=str),
    Validator('translator.openrouter_url', must_exist=True, default='http://subtitle-translator:8765', is_type_of=str),
    Validator('translator.openrouter_api_key', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('translator.openrouter_model', must_exist=True, default='google/gemini-2.5-flash-lite', is_type_of=str),
    Validator('translator.openrouter_temperature', must_exist=True, default=0.3, is_type_of=float),
    Validator('translator.openrouter_max_concurrent', must_exist=True, default=2, is_type_of=int, gte=1, lte=10),
    Validator('translator.openrouter_reasoning', must_exist=True, default='disabled', is_type_of=str,
              is_in=['disabled', 'low', 'medium', 'high']),
    Validator('translator.openrouter_parallel_batches', must_exist=True, default=4, is_type_of=int, gte=1, lte=8),
    # Which OpenRouter provider serves the model: smartfast (the default, where the sidecar
    # weighs speed against price itself, per model and per session), throughput (the sidecar's
    # historical default, fastest and often needlessly expensive), nitro/floor (OpenRouter's
    # slug shortcuts, which also unlock the priority/flex tiers), price, latency, OpenRouter's
    # own load balancing, or explicit provider selection.
    #
    # smartfast needs AI Subtitle Translator 2.0.0. A new install pointed at an older one is
    # told to update rather than being routed some other way behind the user's back.
    Validator('translator.openrouter_provider_routing', must_exist=True,
              default=DEFAULT_PROVIDER_ROUTING, is_type_of=str,
              is_in=list(PROVIDER_ROUTING_VALUES)),
    Validator('translator.openrouter_provider_order', must_exist=True, default=[], is_type_of=list,
              cast=normalize_openrouter_provider_order),
    Validator('translator.openrouter_encryption_key', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('translator.lingarr_token', must_exist=True, default='', is_type_of=str, cast=str),

    # sonarr section
    Validator('sonarr.ip', must_exist=True, default='127.0.0.1', is_type_of=str),
    Validator('sonarr.port', must_exist=True, default=8989, is_type_of=int, gte=1, lte=65535),
    Validator('sonarr.base_url', must_exist=True, default='/', is_type_of=str),
    Validator('sonarr.ssl', must_exist=True, default=False, is_type_of=bool),
    # Mirrored from the default arr instance, which accepts any whole number of
    # seconds from 1 up. A narrower rule here rejected every settings save.
    Validator('sonarr.http_timeout', must_exist=True, default=60, is_type_of=int, gte=1,
              condition=is_not_bool),
    Validator('sonarr.apikey', must_exist=True, default='', is_type_of=str),
    Validator('sonarr.full_update', must_exist=True, default='Daily', is_type_of=str,
              is_in=['Manually', 'Daily', 'Weekly']),
    Validator('sonarr.full_update_day', must_exist=True, default=6, is_type_of=int, gte=0, lte=6),
    Validator('sonarr.full_update_hour', must_exist=True, default=4, is_type_of=int, gte=0, lte=23),
    Validator('sonarr.only_monitored', must_exist=True, default=False, is_type_of=bool),
    Validator('sonarr.series_sync_on_live', must_exist=True, default=True, is_type_of=bool),
    Validator('sonarr.series_sync', must_exist=True, default=60, is_type_of=int,
              is_in=[15, 60, 180, 360, 720, 1440, 10080, ONE_HUNDRED_YEARS_IN_MINUTES]),
    Validator('sonarr.excluded_tags', must_exist=True, default=[], is_type_of=list, condition=validate_tags),
    Validator('sonarr.excluded_series_types', must_exist=True, default=[], is_type_of=list),
    Validator('sonarr.use_ffprobe_cache', must_exist=True, default=True, is_type_of=bool),
    Validator('sonarr.exclude_season_zero', must_exist=True, default=False, is_type_of=bool),
    Validator('sonarr.defer_search_signalr', must_exist=True, default=False, is_type_of=bool),
    Validator('sonarr.sync_only_monitored_series', must_exist=True, default=False, is_type_of=bool),
    Validator('sonarr.sync_only_monitored_episodes', must_exist=True, default=False, is_type_of=bool),
    Validator('sonarr.verify_ssl', must_exist=True, default=False, is_type_of=bool),

    # radarr section
    Validator('radarr.ip', must_exist=True, default='127.0.0.1', is_type_of=str),
    Validator('radarr.port', must_exist=True, default=7878, is_type_of=int, gte=1, lte=65535),
    Validator('radarr.base_url', must_exist=True, default='/', is_type_of=str),
    Validator('radarr.ssl', must_exist=True, default=False, is_type_of=bool),
    Validator('radarr.http_timeout', must_exist=True, default=60, is_type_of=int, gte=1,
              condition=is_not_bool),
    Validator('radarr.apikey', must_exist=True, default='', is_type_of=str),
    Validator('radarr.full_update', must_exist=True, default='Daily', is_type_of=str,
              is_in=['Manually', 'Daily', 'Weekly']),
    Validator('radarr.full_update_day', must_exist=True, default=6, is_type_of=int, gte=0, lte=6),
    Validator('radarr.full_update_hour', must_exist=True, default=4, is_type_of=int, gte=0, lte=23),
    Validator('radarr.only_monitored', must_exist=True, default=False, is_type_of=bool),
    Validator('radarr.movies_sync_on_live', must_exist=True, default=True, is_type_of=bool),
    Validator('radarr.movies_sync', must_exist=True, default=60, is_type_of=int,
              is_in=[15, 60, 180, 360, 720, 1440, 10080, ONE_HUNDRED_YEARS_IN_MINUTES]),
    Validator('radarr.excluded_tags', must_exist=True, default=[], is_type_of=list, condition=validate_tags),
    Validator('radarr.use_ffprobe_cache', must_exist=True, default=True, is_type_of=bool),
    Validator('radarr.defer_search_signalr', must_exist=True, default=False, is_type_of=bool),
    Validator('radarr.sync_only_monitored_movies', must_exist=True, default=False, is_type_of=bool),
    Validator('radarr.verify_ssl', must_exist=True, default=False, is_type_of=bool),

    # sportarr section. Behavioural and schedule settings only: connection
    # details live exclusively in the arr_instances table, because Sportarr has
    # no single-instance compat path that reads scalars the way sonarr and
    # radarr do.
    Validator('sportarr.sports_sync', must_exist=True, default=60, is_type_of=int,
              is_in=[15, 60, 180, 360, 720, 1440, 10080, ONE_HUNDRED_YEARS_IN_MINUTES]),
    Validator('sportarr.full_update', must_exist=True, default='Daily', is_type_of=str,
              is_in=['Manually', 'Daily', 'Weekly']),
    Validator('sportarr.full_update_day', must_exist=True, default=6, is_type_of=int, gte=0, lte=6),
    Validator('sportarr.full_update_hour', must_exist=True, default=4, is_type_of=int, gte=0, lte=23),
    Validator('sportarr.only_monitored', must_exist=True, default=False, is_type_of=bool),
    Validator('sportarr.sync_only_monitored_leagues', must_exist=True, default=False, is_type_of=bool),
    Validator('sportarr.sync_only_monitored_events', must_exist=True, default=False, is_type_of=bool),
    Validator('sportarr.excluded_tags', must_exist=True, default=[], is_type_of=list, condition=validate_tags),
    Validator('sportarr.excluded_sports', must_exist=True, default=[], is_type_of=list),
    Validator('sportarr.search_on_sync', must_exist=True, default=True, is_type_of=bool),
    Validator('sportarr.use_ffprobe_cache', must_exist=True, default=True, is_type_of=bool),
    # Marker for the one-time enable-flag reconcile in app/database.py. Not a
    # user setting and not surfaced in the UI.
    Validator('sportarr.enable_reconciled', must_exist=True, default=False, is_type_of=bool),

    # plex section
    Validator('plex.ip', must_exist=True, default='127.0.0.1', is_type_of=str),
    Validator('plex.port', must_exist=True, default=32400, is_type_of=int, gte=1, lte=65535),
    Validator('plex.ssl', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.apikey', must_exist=True, default='', is_type_of=str),
    Validator('plex.movie_library', must_exist=True, default=[], is_type_of=(str, list)),
    Validator('plex.series_library', must_exist=True, default=[], is_type_of=(str, list)),
    Validator('plex.movie_library_ids', must_exist=True, default=[], is_type_of=list),
    Validator('plex.series_library_ids', must_exist=True, default=[], is_type_of=list),
    Validator('plex.sports_library', must_exist=True, default=[], is_type_of=(str, list)),
    Validator('plex.sports_library_ids', must_exist=True, default=[], is_type_of=list),
    Validator('plex.set_movie_added', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.set_episode_added', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.update_movie_library', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.update_series_library', must_exist=True, default=False, is_type_of=bool),
    # OAuth fields
    Validator('plex.token', must_exist=True, default='', is_type_of=str),
    Validator('plex.username', must_exist=True, default='', is_type_of=str),
    Validator('plex.email', must_exist=True, default='', is_type_of=str),
    Validator('plex.user_id', must_exist=True, default='', is_type_of=(int, str)),
    Validator('plex.auth_method', must_exist=True, default='apikey', is_type_of=str, is_in=['apikey', 'oauth']),
    Validator('plex.encryption_key', must_exist=True, default='', is_type_of=str),
    Validator('plex.verify_ssl', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.server_machine_id', must_exist=True, default='', is_type_of=str),
    Validator('plex.server_name', must_exist=True, default='', is_type_of=str),
    Validator('plex.server_url', must_exist=True, default='', is_type_of=str),
    Validator('plex.server_local', must_exist=True, default=False, is_type_of=bool),
    # Migration fields
    Validator('plex.migration_attempted', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.migration_successful', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.migration_timestamp', must_exist=True, default='', is_type_of=(int, float, str)),
    Validator('plex.disable_auto_migration', must_exist=True, default=False, is_type_of=bool),
    Validator('plex.client_identifier', must_exist=True, default='', is_type_of=str),
    # The destination row this account owns. Recorded rather than inferred so a
    # second Plex instance added by hand is never taken over by the account.
    Validator('plex.instance_id', must_exist=True, default='', is_type_of=str),

    # emby section
    Validator('emby.url', must_exist=True, default='', is_type_of=str),
    Validator('emby.apikey', must_exist=True, default='', is_type_of=str),
    Validator('emby.verify_ssl', must_exist=True, default=True, is_type_of=bool),
    Validator('emby.path_mappings', must_exist=True, default=[], is_type_of=list),

    # silo section
    Validator('silo.url', must_exist=True, default='', is_type_of=str),
    Validator('silo.apikey', must_exist=True, default='', is_type_of=str),
    Validator('silo.verify_ssl', must_exist=True, default=True, is_type_of=bool),
    Validator('silo.path_mappings', must_exist=True, default=[], is_type_of=list),

    # jellyfin section
    Validator('jellyfin.url', must_exist=True, default='', is_type_of=str),
    Validator('jellyfin.apikey', must_exist=True, default='', is_type_of=str),
    Validator('jellyfin.movie_library', must_exist=True, default=[], is_type_of=list),
    Validator('jellyfin.series_library', must_exist=True, default=[], is_type_of=list),
    Validator('jellyfin.movie_library_ids', must_exist=True, default=[], is_type_of=list),
    Validator('jellyfin.series_library_ids', must_exist=True, default=[], is_type_of=list),
    Validator('jellyfin.sports_library', must_exist=True, default=[], is_type_of=list),
    Validator('jellyfin.sports_library_ids', must_exist=True, default=[], is_type_of=list),
    Validator('jellyfin.update_movie_library', must_exist=True, default=False, is_type_of=bool),
    Validator('jellyfin.update_series_library', must_exist=True, default=False, is_type_of=bool),
    Validator('jellyfin.refresh_method', must_exist=True, default='immediate', is_type_of=str,
              is_in=['immediate', 'async']),
    # Default to verifying TLS like sonarr/radarr/plex; users with self-signed
    # homelab certs can flip this off explicitly. Matches feedback_codeql memory.
    Validator('jellyfin.verify_ssl', must_exist=True, default=True, is_type_of=bool),

    # seerr section (Overseerr, Jellyseerr and Seerr share this API)
    Validator('seerr.url', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('seerr.apikey', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('seerr.verify_ssl', must_exist=True, default=True, is_type_of=bool),
    # Browser-facing base for links. Empty means Seerr's own applicationUrl,
    # then the API url above.
    Validator('seerr.external_url', must_exist=True, default='', is_type_of=str, cast=str),

    # proxy section
    Validator('proxy.type', must_exist=True, default=None, is_type_of=(NoneType, str),
              is_in=[None, 'socks5', 'socks5h', 'http']),
    Validator('proxy.url', must_exist=True, default='', is_type_of=str),
    Validator('proxy.port', must_exist=True, default='', is_type_of=(str, int)),
    Validator('proxy.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('proxy.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('proxy.exclude', must_exist=True, default=["localhost", "127.0.0.1"], is_type_of=list),

    # opensubtitles.org section
    Validator('opensubtitles.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('opensubtitles.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('opensubtitles.use_tag_search', must_exist=True, default=False, is_type_of=bool),
    Validator('opensubtitles.vip', must_exist=True, default=False, is_type_of=bool),
    Validator('opensubtitles.ssl', must_exist=True, default=False, is_type_of=bool),
    Validator('opensubtitles.timeout', must_exist=True, default=15, is_type_of=int, gte=1),
    Validator('opensubtitles.skip_wrong_fps', must_exist=True, default=False, is_type_of=bool),
    # Web scraper mode - always enabled (OpenSubtitles.org login no longer available)
    Validator('opensubtitles.use_web_scraper', must_exist=True,
              default=True,
              is_type_of=bool),
    # Scraper URL - can be set via OPENSUBTITLES_SCRAPER_URL environment variable
    Validator('opensubtitles.scraper_service_url', must_exist=True,
              default=os.environ.get('OPENSUBTITLES_SCRAPER_URL', 'http://localhost:8000'),
              is_type_of=str),


    # opensubtitles.com section
    Validator('opensubtitlescom.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('opensubtitlescom.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('opensubtitlescom.use_hash', must_exist=True, default=True, is_type_of=bool),
    Validator('opensubtitlescom.include_ai_translated', must_exist=True, default=False, is_type_of=bool),
    Validator('opensubtitlescom.include_machine_translated', must_exist=True, default=False, is_type_of=bool),

    # napiprojekt section
    Validator('napiprojekt.only_authors', must_exist=True, default=False, is_type_of=bool),
    Validator('napiprojekt.only_real_names', must_exist=True, default=False, is_type_of=bool),

    # addic7ed section
    Validator('addic7ed.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('addic7ed.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('addic7ed.cookies', must_exist=True, default='', is_type_of=str),
    Validator('addic7ed.user_agent', must_exist=True, default='', is_type_of=str),
    Validator('addic7ed.vip', must_exist=True, default=False, is_type_of=bool),

    # animetosho section
    Validator('animetosho.search_threshold', must_exist=True, default=6, is_type_of=int, gte=1, lte=15),
    Validator('animetosho.anidb_api_client', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('animetosho.anidb_api_client_ver', must_exist=True, default=1, is_type_of=int, gte=1, lte=9),

    # avistaz section
    Validator('avistaz.cookies', must_exist=True, default='', is_type_of=str),
    Validator('avistaz.user_agent', must_exist=True, default='', is_type_of=str),

    # cinemaz section
    Validator('cinemaz.cookies', must_exist=True, default='', is_type_of=str),
    Validator('cinemaz.user_agent', must_exist=True, default='', is_type_of=str),

    # subf2m section
    Validator('subf2m.verify_ssl', must_exist=True, default=True, is_type_of=bool),
    Validator('subf2m.user_agent', must_exist=True, default='', is_type_of=str),

    # hdbits section
    Validator('hdbits.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('hdbits.passkey', must_exist=True, default='', is_type_of=str, cast=str),

    # whisperai section
    Validator('whisperai.endpoint', must_exist=True, default='http://127.0.0.1:9000', is_type_of=str),
    Validator('whisperai.response', must_exist=True, default=5, is_type_of=int, gte=1),
    Validator('whisperai.timeout', must_exist=True, default=3600, is_type_of=int, gte=1),
    Validator('whisperai.pass_video_name', must_exist=True, default=False, is_type_of=bool),
    Validator('whisperai.loglevel', must_exist=True, default='INFO', is_type_of=str,
              is_in=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']),

    # legendasdivx section
    Validator('legendasdivx.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('legendasdivx.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('legendasdivx.skip_wrong_fps', must_exist=True, default=False, is_type_of=bool),

    # legendasnet section
    Validator('legendasnet.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('legendasnet.password', must_exist=True, default='', is_type_of=str, cast=str),

    # pipocas section
    Validator('pipocas.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('pipocas.password', must_exist=True, default='', is_type_of=str, cast=str),

    # ktuvit section
    Validator('ktuvit.email', must_exist=True, default='', is_type_of=str),
    Validator('ktuvit.hashed_password', must_exist=True, default='', is_type_of=str, cast=str),

    # assrt section
    Validator('assrt.token', must_exist=True, default='', is_type_of=str, cast=str),

    # anticaptcha section
    Validator('anticaptcha.anti_captcha_key', must_exist=True, default='', is_type_of=str),

    # deathbycaptcha section
    Validator('deathbycaptcha.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('deathbycaptcha.password', must_exist=True, default='', is_type_of=str, cast=str),

    # captchaai section
    Validator('captchaai.captchaai_key', must_exist=True, default='', is_type_of=str, cast=str),

    # napisy24 section
    Validator('napisy24.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('napisy24.password', must_exist=True, default='', is_type_of=str, cast=str),

    # betaseries section
    Validator('betaseries.token', must_exist=True, default='', is_type_of=str, cast=str),

    # jimaku section
    Validator('jimaku.api_key', must_exist=True, default='', is_type_of=str),
    Validator('jimaku.enable_name_search_fallback', must_exist=True, default=True, is_type_of=bool),
    Validator('jimaku.enable_archives_download', must_exist=True, default=False, is_type_of=bool),
    Validator('jimaku.enable_ai_subs', must_exist=True, default=False, is_type_of=bool),

    # titlovi section
    Validator('titlovi.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('titlovi.password', must_exist=True, default='', is_type_of=str, cast=str),

    # titulky section
    Validator('titulky.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('titulky.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('titulky.approved_only', must_exist=True, default=False, is_type_of=bool),
    Validator('titulky.skip_wrong_fps', must_exist=True, default=False, is_type_of=bool),

    # embeddedsubtitles section
    Validator('embeddedsubtitles.included_codecs', must_exist=True, default=[], is_type_of=list),
    Validator('embeddedsubtitles.hi_fallback', must_exist=True, default=False, is_type_of=bool),
    Validator('embeddedsubtitles.timeout', must_exist=True, default=600, is_type_of=int, gte=1),
    Validator('embeddedsubtitles.unknown_as_fallback', must_exist=True, default=False, is_type_of=bool),
    Validator('embeddedsubtitles.fallback_lang', must_exist=True, default='en', is_type_of=str, cast=str),

    # karagarga section
    Validator('karagarga.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('karagarga.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('karagarga.f_username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('karagarga.f_password', must_exist=True, default='', is_type_of=str, cast=str),

    # subdl section
    Validator('subdl.api_key', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('subdl.anime_mode', must_exist=True, default=False, is_type_of=bool),

    # turkcealtyaziorg section
    Validator('turkcealtyaziorg.cookies', must_exist=True, default='', is_type_of=str),
    Validator('turkcealtyaziorg.user_agent', must_exist=True, default='', is_type_of=str),

    # subsync section
    Validator('subsync.use_subsync', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.use_subsync_threshold', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.subsync_threshold', must_exist=True, default=90, is_type_of=int, gte=0, lte=100),
    Validator('subsync.use_subsync_movie_threshold', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.subsync_movie_threshold', must_exist=True, default=70, is_type_of=int, gte=0, lte=100),
    Validator('subsync.debug', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.force_audio', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.use_original_language', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.auto_use_original_language', must_exist=True, default=False, is_type_of=bool),
    Validator('subsync.checker', must_exist=True, default={}, is_type_of=dict),
    Validator('subsync.checker.blacklisted_providers', must_exist=True, default=[], is_type_of=list),
    Validator('subsync.checker.blacklisted_languages', must_exist=True, default=[], is_type_of=list),
    Validator('subsync.no_fix_framerate', must_exist=True, default=True, is_type_of=bool),
    Validator('subsync.gss', must_exist=True, default=True, is_type_of=bool),
    Validator('subsync.max_offset_seconds', must_exist=True, default=60, is_type_of=int,
              is_in=[60, 120, 300, 600]),
    Validator('subsync.enabled_engines', must_exist=True, default=['ffsubsync', 'autosubsync', 'alass'],
              is_type_of=list),
    Validator('subsync.output_mode', must_exist=True, default='overwrite', is_type_of=str,
              is_in=['overwrite', 'keep_all']),

    # ocr section. Where Tesseract language packs are kept; empty means the
    # tessdata folder in the config directory.
    Validator('ocr.tessdata_dir', must_exist=True, default='', is_type_of=str, cast=str),

    # postgresql section
    Validator('postgresql.enabled', must_exist=True, default=False, is_type_of=bool),
    Validator('postgresql.host', must_exist=True, default='localhost', is_type_of=str),
    Validator('postgresql.port', must_exist=True, default=5432, is_type_of=int, gte=1, lte=65535),
    Validator('postgresql.database', must_exist=True, default='', is_type_of=str),
    Validator('postgresql.username', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('postgresql.password', must_exist=True, default='', is_type_of=str, cast=str),
    Validator('postgresql.url', must_exist=True, default='', is_type_of=str, cast=str),

    # anidb section
    Validator('anidb.api_client', must_exist=True, default='', is_type_of=str),
    Validator('anidb.api_client_ver', must_exist=True, default=1, is_type_of=int),

    # subsource section
    Validator('subsource.apikey', must_exist=True, default='', is_type_of=str),

    # subsarr section
    Validator('subsarr.base_url', must_exist=True, default='', is_type_of=str),

    # subx section
    Validator('subx.api_key', must_exist=True, default='', is_type_of=str),
    
    # subsro section
    Validator('subsro.api_key', must_exist=True, default='', is_type_of=str, cast=str),

    # compat_endpoint section
    # NOTE: secret length enforcement happens at blueprint registration via
    # boot_hmac_selftest (see bazarr/compat/auth.py). Do NOT add len_min
    # validators here that fire on the "Save" path -- they would reject the
    # first save that flips enabled=True (secrets still empty at that moment
    # because auto-generation runs at next boot, not at save time).
    Validator('compat_endpoint.enabled', default=False, cast=bool),
    Validator('compat_endpoint.consent', default=False, cast=bool),
    Validator('compat_endpoint.token', default='', cast=str),
    Validator('compat_endpoint.jwt_secret', default='', cast=str),
    Validator('compat_endpoint.file_id_secret', default='', cast=str),
    Validator('compat_endpoint.cache_ttl_seconds',
              default=1800, cast=int, gte=60, lte=86400),
    Validator('compat_endpoint.cache_ttl_partial_seconds',
              default=300, cast=int, gte=30, lte=3600),
    Validator('compat_endpoint.search_timeout_seconds',
              default=DEFAULT_SEARCH_TIMEOUT_SECONDS, cast=int, gte=5, lte=120),
    # Set once migrate_search_timeout_default has looked at an existing config. A new
    # install starts with it set, since it already has the current default.
    Validator('compat_endpoint.search_timeout_migrated',
              default=True, is_type_of=bool),
    # per_provider_timeout is not a user-facing knob: it's derived as
    # 60% of the wall timeout inside _do_fanout. The log-label threshold
    # should scale with the wall, not be tuned independently.
    # Compat fanout shares one process-wide bounded ThreadPoolExecutor
    # so repeated timed-out searches cannot keep spawning provider
    # threads. fanout_max_workers caps total background provider
    # threads; max_concurrent_fanouts caps simultaneous fanouts so a
    # burst of requests cannot drain the pool with abandoned work.
    Validator('compat_endpoint.fanout_max_workers',
              default=32, cast=int, gte=4, lte=256),
    Validator('compat_endpoint.max_concurrent_fanouts',
              default=4, cast=int, gte=1, lte=16),
    Validator('compat_endpoint.file_id_ttl_seconds',
              default=3600, cast=int, gte=300, lte=86400),
    Validator('compat_endpoint.stream_token_ttl_seconds',
              default=300, cast=int, gte=60, lte=3600),
    Validator('compat_endpoint.jwt_ttl_seconds',
              default=86400, cast=int, gte=3600, lte=604800),
    Validator('compat_endpoint.downloads_per_window',
              default=1000, cast=int, gte=1, lte=1000000),
    Validator('compat_endpoint.downloads_window_seconds',
              default=86400, cast=int, gte=60, lte=2592000),
    Validator('compat_endpoint.serve_local_subs',
              default=True, is_type_of=bool),
    # Distribution Hub: tiered rate limiting. `tiers` is an operator-editable
    # dict merged over the built-in presets (see compat/tiers.py). A limit of
    # 0 means unlimited for that window. `default_tier` is applied to new keys.
    Validator('compat_endpoint.default_tier', default='free', cast=str),
    Validator('compat_endpoint.tiers', default={}, is_type_of=dict),
    Validator('compat_endpoint.search_rate_limit_enabled',
              default=True, is_type_of=bool),
    Validator('compat_endpoint.usage_retention_days',
              default=400, cast=int, gte=35, lte=1000),
    # OMDB: optional title/year resolution for movies that aren't in the
    # local library. Free tier at omdbapi.com (1000 req/day). Empty = skip.
    Validator('omdb.apikey', default='', cast=str),
    # Validate new credential input before assignment, never interpolate a stored secret.
    Validator('discover.tmdb_access_token', default=''),
    Validator('discover.locale', default='en-US'),
    Validator('general.metadata_language', default=''),
]


def convert_ini_to_yaml(config_file):
    config_object = configparser.RawConfigParser()
    file = open(config_file, "r")
    config_object.read_file(file)
    output_dict = dict()
    sections = config_object.sections()
    for section in sections:
        items = config_object.items(section)
        output_dict[section] = dict()
        for item in items:
            try:
                output_dict[section].update({item[0]: ast.literal_eval(item[1])})
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                output_dict[section].update({item[0]: item[1]})
    with open(os.path.join(os.path.dirname(config_file), 'config.yaml'), 'w') as file:
        yaml.dump(output_dict, file)
    os.replace(config_file, f'{config_file}.old')


config_yaml_file = os.path.join(args.config_dir, 'config', 'config.yaml')
config_ini_file = os.path.join(args.config_dir, 'config', 'config.ini')
if os.path.exists(config_ini_file) and not os.path.exists(config_yaml_file):
    convert_ini_to_yaml(config_ini_file)
elif not os.path.exists(config_yaml_file):
    if not os.path.isdir(os.path.dirname(config_yaml_file)):
        os.makedirs(os.path.dirname(config_yaml_file))
    open(config_yaml_file, mode='w').close()

if os.path.exists(config_yaml_file):
    os.environ['BAZARR_CONFIGURED'] = '1'

settings = Dynaconf(
    settings_file=config_yaml_file,
    core_loaders=['YAML'],
    apply_default_on_none=True,
)

settings.validators.register(*validators)

# An install that predates the setting, or whose stored value we cannot make sense of,
# keeps a sort every translator serves rather than being moved onto the new default.
#
# The validator default is written for a NEW install. Applying it to an upgrade would
# move an install that has been translating happily onto a routing its translator may not
# implement, and smartfast refuses rather than degrades, so the first symptom would be
# every translation failing on a service the user never had reason to touch. The setting
# first shipped in v2.6.2, so every config written before that lacks the key entirely and
# would otherwise be indistinguishable from a fresh one. A stored value outside the set is
# the same kind of config we do not understand, and the validation loop below would reset
# it to the default (smartfast) just the same; normalizing it first prevents that, because
# a value we failed to parse is not evidence about which translator version is running.
#
# A brand new install is the empty file created just above; anything with content in it
# is an existing config, and existing configs keep the sort they can already be served.
if os.path.getsize(config_yaml_file) > 0:
    stored_routing = settings.get('translator.openrouter_provider_routing')
    if stored_routing not in PROVIDER_ROUTING_VALUES:
        settings['translator.openrouter_provider_routing'] = normalize_stored_provider_routing(stored_routing)
        logging.info("Existing configuration has no usable OpenRouter provider routing (%r); keeping %s, "
                     "which every AI Subtitle Translator version serves.", stored_routing,
                     UPGRADED_PROVIDER_ROUTING)
    # "No translator" has reached storage under several spellings, and they have
    # to be folded onto the one spelling here, before the validation loop below
    # resets a stored null onto the shipped default instead of off. A value that
    # is merely absent is a fresh install and keeps getting that default.
    if settings.exists('translator.translator_type'):
        stored_translator_type = settings.get('translator.translator_type')
        normalized_translator_type = normalize_translator_type(stored_translator_type)
        if normalized_translator_type != stored_translator_type:
            settings['translator.translator_type'] = normalized_translator_type
            logging.info("Existing configuration has no usable translator type (%r); keeping the "
                         "translator switched off.", stored_translator_type)
    migrate_upgrade_subtitle_toggles(settings, existing_config=True)
    migrate_search_timeout_default(settings)

failed_validator = True
while failed_validator:
    try:
        settings.validators.validate_all()
        failed_validator = False
    except ValidationError as e:
        current_validator_details = e.details[0][0]
        logging.error(f"Validator failed for {current_validator_details.names[0]}: {e}")  # noqa: G004
        if hasattr(current_validator_details, 'default') and current_validator_details.default is not empty:
            old_value = settings.get(current_validator_details.names[0], 'undefined')
            settings[current_validator_details.names[0]] = current_validator_details.default
            logging.warning(f"VALIDATOR RESET: {current_validator_details.names[0]} from '{old_value}' to '{current_validator_details.default}'")  # noqa: G004
        else:
            logging.critical(f"Value for {current_validator_details.names[0]} doesn't pass validation and there's no "  # noqa: G004
                             f"default value. This issue must be reported to and fixed by the development team. "
                             f"Bazarr won't work until it's been fixed.")
            stop_bazarr(EXIT_VALIDATION_ERROR)

# Decrypt every USER_VISIBLE_SECRET in the live settings object so the
# rest of bazarr reads plaintext credentials. On a freshly-upgraded
# instance the values are still plaintext (no marker prefix) and this
# call is a no-op; on subsequent boots the values come from disk as
# ciphertext and get decrypted in place. The complementary encryption
# happens inside write_config() before the dict hits config.yaml.
from secret_store import (  # noqa: E402
    decrypt_settings_dict,
    decrypt_settings_in_place,
    encrypt_settings_dict,
    has_plaintext_secrets_on_disk,
    migrate_legacy_plex_encryption,
)
from secret_store.migration import (LAST_FOUR_MIN_LENGTH, clear_decrypt_failure_path,  # noqa: E402
                                    decrypt_failure_paths, note_replaced_secret)
from secret_store.registry import (SECRET_DESCRIPTOR_KEY, HubSecretRegistrationError,  # noqa: E402
                                   at_rest_secret_paths, manifest_boolean_secret_paths)

# Legacy Plex encryption (URLSafeSerializer + plex.encryption_key) used a
# different at-rest format with no marker prefix. If we hand its
# ciphertext to decrypt_settings_in_place, the marker check fails and the
# bytes get treated as plaintext - the next save would re-encrypt them
# under the unified key and the user's Plex creds would be unrecoverable.
# Migrate FIRST so the rest of the pipeline only sees plaintext.
migrate_legacy_plex_encryption(settings)

# Register the Provider Hub plugin secret paths before the boot secret
# pass: the plaintext detection and the in-place decrypt below have to
# walk them, or a plugin credential stays in the clear (and one left by a
# removed plugin never decrypts). Reads state.json directly; the service
# layer is not up while settings load.
try:
    from secret_store.registry import load_plugin_secret_paths
    load_plugin_secret_paths(settings)
except HubSecretRegistrationError:
    # No covering of the plaintext plugin leftovers could be vouched for,
    # so the boot does not complete while they stay readable. The broad
    # catch below is for the failures the durable registrations survive.
    raise
except Exception:
    logging.exception("Unable to load the Provider Hub plugin secret registry")

# Detect plaintext credentials BEFORE decrypt_settings_in_place runs - it
# is a passthrough on plaintext, so afterwards the in-memory state and
# the on-disk state match for plaintext fields and write_config's
# comparison would skip the rewrite. Capture the "needs migration" bit
# now and force a write below.
_force_first_save_migration = has_plaintext_secrets_on_disk(settings)

decrypt_settings_in_place(settings)

# A stored API key that will not decrypt is the one failure Bazarr can and
# must repair itself: the key is Bazarr's own, the SPA sends it on every
# request, and withheld as empty it locks the UI and the API out entirely,
# with the remedy (a health issue the API serves) unreachable. Mint a fresh
# key with the same generator the auth.apikey validator's default uses, end
# the failure, and raise a health notice saying the key was replaced and
# that the tools holding the old one must be updated. The boot write below
# persists the fresh key encrypted, and its failure refuses the boot: an
# unsaved key would serve this boot only, and a restart would mint another
# while the stored one stays undecryptable. An empty key is NOT
# regenerated: an install that stores an empty key keeps it, exactly as
# before.
_replaced_undecryptable_api_key = "auth.apikey" in decrypt_failure_paths()
if _replaced_undecryptable_api_key:
    settings.auth.apikey = hexlify(os.urandom(16)).decode()
    clear_decrypt_failure_path("auth.apikey")
    note_replaced_secret(
        "auth.apikey",
        "The stored API key could not be decrypted and was replaced. Update the "
        "tools and scripts that use the API key."
    )
    logging.warning("The stored API key could not be decrypted and was replaced; "
                    "update the tools that use it.")


class MetadataPersistenceError(Exception):
    """The requested metadata configuration could not be persisted."""


class SettingsFollowupError(Exception):
    """Settings were persisted, but subsequent application work failed."""


class MetadataFollowupError(SettingsFollowupError):
    """Metadata settings were persisted, but subsequent application work failed."""


def write_config(*, strict_metadata=False):
    from discover.metadata import CONFIG_LOCK
    from secret_store.scrub import refresh_secret_values
    try:
        with CONFIG_LOCK:
            return _write_config(strict_metadata=strict_metadata)
    finally:
        # Whatever the outcome, the registry follows the live store. A
        # successful save entered or rotated a credential; a failed one is
        # rolled back by the caller after this returns (and
        # restore_persisted_settings refreshes again there). A save whose
        # follow-up failed still wrote the credential, so the refresh cannot
        # hang off the return value alone. Outside the lock: the refresh reads
        # Provider Hub's state as well, and a settings save must not wait on
        # the hub's write lock to finish.
        refresh_secret_values(settings)


# Why the last write_config call failed, when it failed: write_config answers
# True/False, so the save path re-attaches this to its refusal instead of the
# caller learning only that a save did not happen.
_last_write_error: Exception | None = None


def _write_config(*, strict_metadata=False):
    from secret_store.crypto import mark_master_key_persisted
    # On-disk shape compared in plaintext form: encrypt_secret is non-
    # deterministic (per-payload salt + timestamp), so naive ciphertext
    # comparison would always diff and rewrite config.yaml on every save.
    global _force_first_save_migration
    global _last_write_error
    in_memory_plaintext = {k.lower(): v for k, v in settings.as_dict().items()}
    on_disk_dict = {
        k.lower(): v for k, v in Dynaconf(
            settings_file=config_yaml_file,
            core_loaders=['YAML'],
        ).as_dict().items()
    }
    on_disk_plaintext = decrypt_settings_dict(on_disk_dict)

    if in_memory_plaintext == on_disk_plaintext and not _force_first_save_migration:
        logging.debug("Nothing changed when comparing to config file. Skipping write to file.")
        mark_master_key_persisted(in_memory_plaintext.get("general", {}).get("secrets_encryption_key"))
        _last_write_error = None
        return True

    forced_migration = _force_first_save_migration
    if forced_migration:
        logging.info("secret_store: forcing config rewrite to encrypt plaintext credentials on disk")

    try:
        encrypted_payload = encrypt_settings_dict(in_memory_plaintext)
        # A credential whose decryption failed at boot is withheld as an
        # empty value in the live settings; keep the on-disk ciphertext for
        # it so this save does not overwrite recoverable bytes, unless the
        # snapshot carries a re-entered value.
        from secret_store.migration import preserve_recoverable_ciphertexts
        preserve_recoverable_ciphertexts(encrypted_payload, in_memory_plaintext, on_disk_dict)
        write(settings_path=config_yaml_file + '.tmp',
              settings_data=encrypted_payload,
              merge=False)
    except Exception as error:
        _last_write_error = error
        if strict_metadata:
            raise MetadataPersistenceError("Metadata settings could not be saved. Try again.") from None
        logging.exception(f"Exception raised while trying to save temporary settings file: {error}")  # noqa: G004
        return False
    else:
        try:
            move(config_yaml_file + '.tmp', config_yaml_file)
        except Exception as error:
            _last_write_error = error
            if strict_metadata:
                raise MetadataPersistenceError("Metadata settings could not be saved. Try again.") from None
            logging.exception(f"Exception raised while trying to overwrite settings file with temporary settings "  # noqa: G004
                              f"file: {error}")
            return False
        else:
            # Only clear the forced-migration flag once the new
            # encrypted config is durably in place. Clearing it on the
            # logging branch above would silently swallow the migration
            # if write() or move() failed (disk full, permission, I/O):
            # the next write_config() would short-circuit on the
            # plaintext-equality check at the top of this function and
            # never retry, leaving credentials unencrypted on disk.
            if forced_migration:
                _force_first_save_migration = False
            _last_write_error = None
            mark_master_key_persisted(encrypted_payload.get("general", {}).get("secrets_encryption_key"))
            # The write landed: a failed scalar path whose live value is
            # non-empty was replaced by this write, so its failure is over
            # no matter which writer set the value. Without this, only the
            # settings API's save ended a failure, and a writer that set a
            # credential directly kept the health issue asking for a
            # re-entry that had already happened.
            from secret_store.migration import clear_replaced_secret_paths
            clear_replaced_secret_paths(in_memory_plaintext)
            return True


# OpenRouter retired these ids, including Bazarr's default and documented recommendation.
# Users who never chose a model would otherwise get a model-not-found error.
RETIRED_OPENROUTER_MODELS = {
    'google/gemini-2.5-flash-preview-05-20': 'google/gemini-2.5-flash',
    'google/gemini-2.5-flash-lite-preview-06-17': 'google/gemini-2.5-flash-lite',
    'google/gemini-2.5-flash-lite-preview-09-2025': 'google/gemini-2.5-flash-lite',
    'google/gemini-3-pro-preview': 'google/gemini-3.1-pro-preview',
}


def migrate_retired_openrouter_model(settings) -> bool:
    current_model = settings.translator.openrouter_model.strip()
    replacement = RETIRED_OPENROUTER_MODELS.get(current_model)
    if replacement is None:
        return False
    settings.translator.openrouter_model = replacement
    logging.warning(f'Replaced retired OpenRouter model {current_model} with {replacement}')  # noqa: G004
    return True


def remove_settings_section(name):
    """Drop a whole top-level section from the live settings, for good.

    Dynaconf keeps a section added by a set() without a loader name, which is also
    what an item or attribute assignment on the settings does, as a default of its
    own. unset() skips a default unless forced, and a forced unset still leaves it
    for the next reload() to put back, so the default is dropped as well.
    """
    key = name.upper()
    settings.unset(key, force=True)
    settings.__core__.config.defaults.pop(key, None)


base_url = settings.general.base_url.rstrip('/')

array_keys = ['excluded_tags',
              'exclude',
              'allowed_hosts',
              'included_codecs',
              'subzero_mods',
              'excluded_series_types',
              'enabled_providers',
              'enabled_integrations',
              'enabled_engines',
              'gemini_keys',
              'openrouter_provider_order',
              'path_mappings',
              'path_mappings_movie',
              'path_mappings_sports',
              'excluded_sports',
              'remove_profile_tags',
              'language_equals',
              'verbose_loggers',
              'blacklisted_languages',
              'blacklisted_providers',
              'movie_library',
              'series_library',
              'movie_library_ids',
              'series_library_ids',
              'sports_library',
              'sports_library_ids']

empty_values = ['', 'None', 'null', 'undefined', None, []]

str_keys = ['chmod', 'log_include_filter', 'log_exclude_filter', 'password', 'f_password', 'hashed_password']

# Increase Sonarr and Radarr sync interval since we now use SignalR feed to update in real time
if settings.sonarr.series_sync < 15:
    settings.sonarr.series_sync = 60
if settings.radarr.movies_sync < 15:
    settings.radarr.movies_sync = 60

# Make sure to get of double slashes in base_url
settings.general.base_url = base_url_slash_cleaner(uri=settings.general.base_url)
settings.sonarr.base_url = base_url_slash_cleaner(uri=settings.sonarr.base_url)
settings.radarr.base_url = base_url_slash_cleaner(uri=settings.radarr.base_url)

# increase delay between searches to reduce impact on providers
if settings.general.wanted_search_frequency == 3:
    settings.general.wanted_search_frequency = 6
if settings.general.wanted_search_frequency_movie == 3:
    settings.general.wanted_search_frequency_movie = 6

# backward compatibility embeddedsubtitles provider
if hasattr(settings.embeddedsubtitles, 'unknown_as_english'):
    if settings.embeddedsubtitles.unknown_as_english:
        settings.embeddedsubtitles.unknown_as_fallback = True
        settings.embeddedsubtitles.fallback_lang = 'en'
    del settings.embeddedsubtitles.unknown_as_english

# delete custom scores sections since we don't use this anymore
if hasattr(settings, 'series_scores'):
    remove_settings_section('series_scores')
if hasattr(settings, 'movie_scores'):
    remove_settings_section('movie_scores')

# backward compatibility: migrate gemini_key to gemini_keys
if hasattr(settings.translator, 'gemini_key'):
    legacy_key = str(settings.translator.gemini_key).strip()
    if legacy_key and not settings.translator.gemini_keys:
        settings.translator.gemini_keys = [legacy_key]
    del settings.translator.gemini_key

migrate_retired_openrouter_model(settings)

# save updated settings to file. The boot write is what carries the
# encryption migration to disk, so while a plaintext credential remains
# there its failure is not survivable: write_config keeps the migration
# pending on failure, and the boot refuses instead of completing with the
# credential readable in the clear. The next boot retries the rewrite once
# the write works. The same write persists a replacement API key minted
# for one that could not be decrypted, and that failure is not survivable
# either: unwritten, this boot would serve a key nothing on disk holds,
# and a restart would mint another while the stored one stays
# undecryptable, so that boot refuses too.
if not write_config():
    if _force_first_save_migration:
        raise RuntimeError(
            "The settings file could not be rewritten, so the plaintext "
            "credentials on disk could not be encrypted; startup does not "
            "complete while they remain readable in the clear"
        ) from _last_write_error
    if _replaced_undecryptable_api_key:
        raise RuntimeError(
            "The settings file could not be rewritten, so the replacement "
            "API key minted for the stored one that could not be decrypted "
            "was not persisted; startup does not complete while the stored "
            "API key remains undecryptable"
        ) from _last_write_error

# Plugin credentials held in Provider Hub's state.json encrypt only under a
# master key that is durably saved: the boot write above just put this
# process's key on disk, and encrypting before that (under a key that exists
# only in memory) would commit ciphertext a failed write could leave with no
# key that ever opens it. The migration raises when plaintext credentials
# remain but cannot be encrypted, so a boot does not complete while they are
# still readable in the clear.
from secret_store.registry import migrate_installation_config_secrets  # noqa: E402

migrate_installation_config_secrets()

# Register the live credential values before anything can log, store or serve
# them, and rewrite what an earlier version already left in state.json. The
# scrub runs after the migration above for the same reason the migration runs
# after the boot write: the credential containers are ciphertext by now, so a
# scrub cannot mistake a stored credential for a leaked one.
from secret_store.scrub import refresh_secret_values, scrub_persisted_secrets  # noqa: E402

refresh_secret_values(settings)
scrub_persisted_secrets()


# What the settings read shows in place of a stored login password, and what a
# save may send back to mean "keep the stored one".
AUTH_PASSWORD_MASK = '***'


def get_settings():
    from discover.metadata import CONFIG_LOCK
    with CONFIG_LOCK:
        return _get_settings()


def _get_settings():
    # API serializer for /api/system/settings. SYSTEM_SECRETS are masked
    # with '***' (key still present so the wire shape is stable, value
    # hidden); USER_VISIBLE_SECRETS pass through unchanged because the
    # in-memory settings already hold their decrypted plaintext.
    # Registered Provider Hub plugin secrets are write-only: an empty value
    # plus a per-section descriptor that says one is configured and, for a
    # long secret, how it ends.
    from secret_store import is_system_secret, is_write_only_secret  # noqa: PLC0415, RUF100
    from secret_store.migration import decrypt_failure_paths
    from secret_store.registry import (IMPORT_ONLY_SECTIONS,
                                        hidden_leftover_paths, is_plugin_secret)
    hidden_paths = hidden_leftover_paths()
    settings_to_return = {}
    for k, v in settings.as_dict().items():
        if isinstance(v, dict):
            k = k.lower()
            if k in IMPORT_ONLY_SECTIONS:
                continue
            settings_to_return[k] = dict()
            # The write-only descriptor map for this section. The key is not a
            # valid plugin config-schema field name (manifest validation
            # rejects a leading underscore), so it can never collide with a
            # real field.
            secret_descriptors = {}
            for subk, subv in v.items():
                full_path = f"{k}.{subk.lower()}"
                if full_path == SESSION_GENERATION_PATH:
                    continue
                if is_write_only_secret(full_path):
                    continue
                if is_plugin_secret(full_path):
                    if full_path in decrypt_failure_paths():
                        # The stored value would not decrypt at boot: say the
                        # credential is there but must be re-entered, and give
                        # nothing away.
                        settings_to_return[k][subk] = ""
                        secret_descriptors[subk.lower()] = {"configured": True,
                                                            "decrypt_failed": True}
                        continue
                    if isinstance(subv, str) and subv:
                        settings_to_return[k][subk] = ""
                        descriptor = {"configured": True}
                        if len(subv) >= LAST_FOUR_MIN_LENGTH:
                            descriptor["last_four"] = subv[-4:]
                        secret_descriptors[subk.lower()] = descriptor
                        continue
                    if isinstance(subv, (list, dict)) and not subv:
                        # An empty collection an earlier write left at a
                        # secret path reads exactly like an empty string:
                        # the field is not a configured credential, and the
                        # payload never carries the raw collection.
                        settings_to_return[k][subk] = ""
                        continue
                    if subv not in (None, ""):
                        # A non-string value in a secret slot (an older save
                        # coerced a numeric-looking token): it is a stored
                        # credential all the same, so it is withheld too,
                        # with no last four to show for it.
                        settings_to_return[k][subk] = ""
                        secret_descriptors[subk.lower()] = {"configured": True}
                        continue
                    # An empty or missing value is not a configured secret;
                    # it flows through the normal handling below.
                if full_path in hidden_paths:
                    # The leftover scan could not run and the fallback could
                    # not register this path safely either, so the value is
                    # withheld for this boot rather than handed out: the next
                    # boot with the scan working covers it for real.
                    settings_to_return[k][subk] = ""
                    continue
                if k == "discover" and subk.lower() in {"tmdb_configured", "tmdb_token_stored",
                                                        "metadata_revision"}:
                    continue
                if full_path == 'auth.password':
                    # The stored login password is a one-way hash, and a hash
                    # handed to anyone holding the API key is a password they
                    # can test offline with no throttle. Masked like the
                    # system secrets; empty stays empty.
                    settings_to_return[k][subk] = AUTH_PASSWORD_MASK if subv else subv
                    continue
                if is_system_secret(full_path):
                    # Keep empty values literally empty so the UI can
                    # distinguish "not configured" from "configured but
                    # hidden". Non-empty system secrets get the mask.
                    settings_to_return[k][subk] = '***' if subv else subv
                    continue
                if subv in empty_values and subk.lower() in array_keys:
                    settings_to_return[k].update({subk: []})
                elif subk == 'subzero_mods':
                    settings_to_return[k].update({subk: get_array_from(subv)})
                else:
                    settings_to_return[k].update({subk: subv})
            if secret_descriptors:
                settings_to_return[k][SECRET_DESCRIPTOR_KEY] = secret_descriptors
    from discover.metadata import configuration, reader_token_stored
    metadata_config = configuration()
    # Two separate facts: metadata works at all, which the built-in key makes
    # true everywhere, and whether the reader saved a key of their own, which
    # is the only one that can be removed.
    settings_to_return.setdefault("discover", {}).update({
        "tmdb_configured": bool(metadata_config.token), "tmdb_token_stored": reader_token_stored(),
        "metadata_revision": metadata_config.revision})
    settings_to_return.setdefault("general", {})["metadata_language"] = metadata_config.locale
    return settings_to_return


def validate_log_regex():
    # handle bug in dynaconf that changes strings to numbers, so change them back to str
    if not isinstance(settings.log.include_filter, str):
        settings.log.include_filter = str(settings.log.include_filter)
    if not isinstance(settings.log.exclude_filter, str):
        settings.log.exclude_filter = str(settings.log.exclude_filter)

    if settings.log.use_regex:
        # compile any regular expressions specified to see if they are valid
        # if invalid, tell the user which one
        try:
            re.compile(settings.log.include_filter)
        except Exception:
            raise ValidationError(f"Include filter: invalid regular expression: {settings.log.include_filter}")
        try:
            re.compile(settings.log.exclude_filter)
        except Exception:
            raise ValidationError(f"Exclude filter: invalid regular expression: {settings.log.exclude_filter}")


def _settings_mapping(parent, key):
    try:
        mapping = parent[key]
    except KeyError:
        mapping = None
    if mapping is None:
        if parent is settings:
            # Named, because Dynaconf keeps a section added by an unnamed set() as a
            # default of its own: reload() then keeps it, so a refused save that
            # created it would not be undone, and unset() skips it unless forced.
            settings.set(key, {}, loader_identifier='settings_save')
        else:
            parent[key] = {}
        mapping = parent[key]
    return mapping


def _settings_value(parent, keys):
    value = parent
    for key in keys:
        if value is None:
            return None
        if hasattr(value, "get"):
            value = value.get(key)
        else:
            value = getattr(value, key, None)
    return value


def _active_provider_hub_provider_ids():
    try:
        state_module = sys.modules.get("provider_hub.state")
        if state_module is None:
            from provider_hub import state as state_module
        load_state = getattr(state_module, "load_state", None)
        if callable(load_state):
            state = load_state()
            return {
                str(item.get("provider_id") or provider_id)
                for provider_id, item in (state.get("installations") or {}).items()
                if isinstance(item, dict)
                and item.get("active_version")
                and item.get("state") != "removed"
            }

        active_installations = getattr(state_module, "active_installations")
        return {
            str(provider_id)
            for provider_id in (
                getattr(installation, "provider_id", None)
                for installation in active_installations()
            )
            if provider_id
        }
    except Exception:
        logging.exception("Unable to inspect active Provider Hub providers")
        return set()


def _translator_field(settings_items, name):
    """The value a request carries for ``settings-translator-<name>``, or None.

    Matched on the whole key rather than its last segment. The settings store underneath
    is case-insensitive, so the name is compared case-insensitively, but the section is
    not: a key naming some other section must never be read as, or written to, this one.
    """
    for key, value in settings_items:
        parts = key.split('-')
        if len(parts) == 3 and parts[0] == 'settings' and parts[1] == 'translator' and parts[2].lower() == name:
            return value
    return None


def _is_provider_order_key(key):
    """True for settings-translator-openrouter_provider_order in any casing of the name."""
    parts = key.split('-')
    return (len(parts) == 3 and parts[0] == 'settings' and parts[1] == 'translator'
            and parts[2].lower() == 'openrouter_provider_order')


def _require_provider_order_for_custom_routing(settings_items):
    """Refuse custom OpenRouter routing that names no provider.

    The routing and the provider list arrive in the same request and only mean anything
    together. Stored apart, custom with an empty list saves cleanly and then fails every
    translation, and by that point the only signal is a failed job.

    Only a request that actually carries one of the two keys is checked. Reading the pair
    off stored settings for every save made one bad translator config reject saves on
    every other settings page, which is a worse failure than the one being prevented and
    lands on a page that cannot fix it.
    """
    submitted_routing = _translator_field(settings_items, 'openrouter_provider_routing')
    submitted_order = _translator_field(settings_items, 'openrouter_provider_order')
    if submitted_routing is None and submitted_order is None:
        return
    routing = submitted_routing
    if routing is None:
        routing = getattr(settings.translator, 'openrouter_provider_routing', '')
    if isinstance(routing, list):
        routing = routing[0] if routing else ''
    if str(routing).lower() != 'custom':
        return
    order = submitted_order
    if order is None:
        order = getattr(settings.translator, 'openrouter_provider_order', [])
    if not order:
        raise ValidationError('OpenRouter custom routing requires at least one provider slug. '
                              'Choose a provider, or pick another routing option.')


def _validate_auth_password(settings_items):
    """Refuse a new login password that is the mask the settings read hands out.

    The mask stands for "keep the stored password": a save that sends it back
    leaves the password as it is. With a password stored, that is the only
    reading it can have, so a caller who really wants the password to be the
    mask text gets the old one kept. With none stored there is nothing to keep,
    and hashing the mask would set a password that later saves could never
    resend as itself, so it is refused instead.
    """
    if settings.auth.password:
        return
    for key, value in settings_items:
        if key.lower() != 'settings-auth-password':
            continue
        submitted = value[0] if isinstance(value, list) and len(value) == 1 else value
        if submitted == AUTH_PASSWORD_MASK:
            raise ValidationError(f'The password cannot be {AUTH_PASSWORD_MASK}: that text stands for '
                                  'the stored password. Choose a different password.')


def _validate_postprocessing_command(settings_items):
    """Refuse a post-processing command that relies on a shell.

    Commands run as an argument list without a shell, so a pipe, redirect or
    variable would reach the program as a literal argument. A request that
    sets the command is always checked, because an enabled instance can
    inherit the global command while the global toggle is off, and a command
    that cannot run should not be stored at all. A request that turns
    post-processing on is checked against the stored command, so a command
    stored before this rule never blocks saves on other pages or switching it
    off; the health check reports that one instead. Whenever the submitted
    toggle turns post-processing on the commands stored per instance are
    checked too, whether or not the same request also sets a global command:
    an override saved while the instance toggle was off becomes effective the
    moment the switch is on.
    """
    submitted = {key.lower(): value[0] if isinstance(value, list) and value else value
                 for key, value in settings_items}
    command = submitted.get('settings-general-postprocessing_cmd')
    submitted_toggle = submitted.get('settings-general-use_postprocessing')
    if command is None and submitted_toggle is None:
        return
    sets_command = command is not None
    if command is None:
        command = settings.general.postprocessing_cmd
    enabled = settings.general.use_postprocessing if submitted_toggle is None \
        else str(submitted_toggle).lower() == 'true'
    if not sets_command and not enabled:
        return
    from utilities.post_processing import parse_postprocessing_command
    try:
        parse_postprocessing_command(str(command))
    except ValueError as error:
        raise ValidationError(f'Post-processing command: {error}.') from None
    # The submitted toggle decides the instance checks, not the absence of a
    # command: the toggle can arrive together with a valid new global command,
    # and the stored instance commands become effective with the switch either
    # way, so a shell-dependent one must not survive into the enabled
    # configuration behind that new command.
    turns_post_processing_on = submitted_toggle is not None and enabled
    if not turns_post_processing_on:
        return
    from app.database import database, TableArrInstances, select
    from arr_instances.subtitle_settings import read_subtitle_settings
    instances = database.execute(
        select(TableArrInstances.name, TableArrInstances.options)
        .where(TableArrInstances.enabled == 1)).all()
    for instance in instances:
        own = read_subtitle_settings(instance.options).get('general', {}).get('postprocessing_cmd')
        if not isinstance(own, str) or not own.strip():
            continue
        try:
            parse_postprocessing_command(own)
        except ValueError as error:
            raise ValidationError(f"Post-processing command for {instance.name}: {error}. Turn that "
                                  f"instance's post-processing off or fix its command first.") from None


def validate_metadata_settings(settings_items):
    from discover.metadata import validate_token
    allowed = {"settings-discover-tmdb_access_token", "settings-discover-locale",
               "settings-general-metadata_language"}
    seen = set()
    for key, values in settings_items:
        if not key.lower().startswith("settings-discover-") and key.lower() != "settings-general-metadata_language":
            continue
        if key not in allowed or key in seen:
            raise ValidationError("Invalid Discover setting.")
        seen.add(key)
        if not isinstance(values, list) or len(values) != 1:
            raise ValidationError("Invalid TMDB access token." if key.endswith("tmdb_access_token")
                                  else "Invalid metadata language.")
        if key.endswith("tmdb_access_token"):
            try:
                validate_token(values[0])
            except ValueError:
                raise ValidationError("Invalid TMDB access token.") from None
        elif not isinstance(values[0], str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", values[0]):
            raise ValidationError("Invalid metadata language.")
class ReloadedSettings(NamedTuple):
    """What a reload from disk found in the file, as opposed to what memory then holds.

    `generation` is the session generation the file carries (None when it is not
    a readable integer) and `login` the login identity it carries.
    """
    generation: int | None
    login: tuple


def restore_persisted_settings():
    """Return the live settings object to what is actually saved on disk.

    Returns the ReloadedSettings the file held.

    Re-decrypt after reload: settings.reload() pulls the on-disk ciphertext back
    into the live Dynaconf object, so without this second pass downstream code
    would see `enc:v1:` strings for API keys, auth credentials, provider
    passwords, and compat tokens until the next process restart.

    The session generation is the one value a reload never moves backwards:
    see app.auth.generation.never_going_backwards.
    """
    from app.auth.generation import current_generation, never_going_backwards
    with never_going_backwards():
        settings.reload()
        migrate_legacy_plex_encryption(settings)
        decrypt_settings_in_place(settings)
        # What the file itself held, read before the generation is put back to
        # the larger of the two: a refused login-change save decides on this
        # whether its login reached the disk.
        reloaded = ReloadedSettings(generation=current_generation(), login=_auth_identity())
    # The registry follows the live store: a refused save registered the
    # credential it proposed, and this is where those values are put back as
    # they were. Without this the restored credential would escape scrubbing
    # until the next settings write or boot.
    from secret_store.scrub import refresh_secret_values
    refresh_secret_values(settings)
    return reloaded


_native_settings_save_lock = threading.RLock()

# What a written save could not finish for the provider logins it changed: the
# cached logins still to clear, and whether the credential throttles still need
# their reset. Saving the same login again finds nothing changed, so the next
# written save retries these, whatever it changes. Saves run one at a time.
_unfinished_login_resets = {'caches': set(), 'throttles': False}


class _FollowUps:
    """Runs what a written save sets off, so that one step failing stops no other.

    The save is on disk by then and every step acts on it by itself: a failed login
    reset must not leave a library job unqueued, which saving the same values again
    would not queue either. Once all have run, the first failure is raised and any
    others are logged.
    """

    def __init__(self):
        self.failures = []

    @contextlib.contextmanager
    def step(self):
        try:
            yield
        except Exception as error:
            self.failures.append(error)

    def raise_first(self):
        for error in self.failures[1:]:
            logging.error('Settings were saved, but applying them also failed', exc_info=error)
        if self.failures:
            raise self.failures[0]


# Every kind's master switch, so flipping one republishes that kind's saved
# snapshots. A kind missing from here keeps refreshing after the user turned it
# off, until the next restart.
NATIVE_MASTER_KEYS = {'settings-general-use_' + kind
                      for kind in ('emby', 'jellyfin', 'plex', 'silo')}

# What a settings save can change about the Plex account: the credential it
# authenticates with, or the addresses its selection points at. Such a save
# is an account transition, and it is serialized with every account handler
# and every verification's write phase under the account lock, rather than
# only the row sync that follows it. The addresses are serialized for the
# verification's write too: it rechecks them under the lock right before it
# clears or rewrites them, so a save landing between the two would otherwise
# be wiped from the older answer's snapshot.
PLEX_ACCOUNT_KEYS = {'settings-plex-auth_method', 'settings-plex-token', 'settings-plex-apikey',
                     'settings-plex-server_url', 'settings-plex-server_connections'}


def _save_settings_with_native(settings_items, *, strict_metadata=False, on_persisted=None):
    """Apply the media-server master-switch handling around a settings save."""
    with _native_settings_save_lock:
        if not any(key in NATIVE_MASTER_KEYS for key, _value in settings_items):
            return _save_settings(settings_items, strict_metadata=strict_metadata,
                                  on_persisted=on_persisted)
        from media_servers.dispatcher import get_native_configuration
        native = get_native_configuration()
        with native.lock:
            try:
                return _save_settings(settings_items, native, strict_metadata=strict_metadata,
                                      on_persisted=on_persisted)
            finally:
                for kind, enabled in native.masters.items():
                    _settings_mapping(settings, 'general')['use_' + kind] = enabled


def _canonical_settings_key(key):
    """The one spelling of a settings-<section>-<field> name, or None.

    The store underneath is case-insensitive, so two spellings of one name
    reach the same stored key while each carries its own value past every
    check that reads only the first of them.
    """
    if not isinstance(key, str):
        return None
    parts = key.split('-')
    if len(parts) != 3 or parts[0] != 'settings':
        return None
    return f'settings-{parts[1].lower()}-{parts[2].lower()}'


def _refuse_repeated_settings_items(items):
    """Each settings-<section>-<field> name once, in its canonical spelling.

    The writer applies submitted items in order, so two spellings of one name
    would leave the last value in the store after every check had read only
    the first. A save that submits the same name twice is refused instead of
    silently preferring one of the values.
    """
    seen = set()
    for index, (key, values) in enumerate(items):
        canonical = _canonical_settings_key(key)
        if canonical is None:
            continue
        if canonical in seen:
            raise ValidationError('Each setting can only be submitted once')
        seen.add(canonical)
        items[index] = canonical, values
    return items


def _refuse_unverified_scalar_arr_destination(settings_items):
    """The destination rule for the scalar Sonarr and Radarr settings.

    The scalar connection settings mirror the kind's default instance, its key
    included, and the paths that still read them (the status probe, the cover
    proxy, the scalar filesystem, rootfolder and notify calls) send that
    mirrored key to the address they build. A save that moves one of those
    settings moves a stored key, so it is refused unless the save types the key
    again, the same rule the instance's own edits follow. The refusal protects
    a key that is still retained: a save that clears the stored key moves the
    address with it, and with no key stored or left there is nothing the moved
    address could take anywhere. Turning certificate verification off counts
    as a move; turning it back on only hardens the connection.
    """
    for kind in ('sonarr', 'radarr'):
        submitted = {}
        for key, values in settings_items:
            parts = key.split('-') if isinstance(key, str) else []
            if len(parts) == 3 and parts[:2] == ['settings', kind]:
                values = values if isinstance(values, list) else [values]
                submitted.setdefault(parts[2].lower(), []).extend(values)
        if not any(field in submitted
                   for field in ('ip', 'port', 'base_url', 'ssl', 'verify_ssl')):
            continue
        typed = any(isinstance(value, str) and value.strip()
                    for value in (_plex_writer_value('apikey', [item])
                                  for item in submitted.get('apikey', [])))
        if typed:
            # The key typed again is all the move needs.
            continue
        if 'apikey' in submitted:
            # The writer stores the submitted value, so a submitted key with
            # no content, in any of the writer's empty spellings, clears the
            # stored one: the save leaves no key for the moved address to
            # send, and nothing is retained to protect.
            continue
        section = getattr(settings, kind, None)
        if section is None:
            continue
        stored_key = section.get('apikey', '') or ''
        if not (isinstance(stored_key, str) and stored_key.strip()):
            # No key is retained, so there is nothing a moved address
            # could take with it.
            continue
        changed = False
        for field, fallback in (('ip', '127.0.0.1'),
                                ('port', 8989 if kind == 'sonarr' else 7878),
                                ('base_url', '/'), ('ssl', False)):
            if field not in submitted:
                continue
            value = _plex_writer_value(field, submitted[field])
            stored = section.get(field, fallback)
            if stored is None:
                stored = fallback
            if value is None:
                # An empty spelling clears the stored value, and the cleared
                # field falls back to the default the client applies, so the
                # destination the moved key is sent to is the default one: a
                # move whenever the stored value was not that default already.
                value = fallback
            if value != stored:
                changed = True
        if 'verify_ssl' in submitted:
            verify_ssl = _plex_writer_value('verify_ssl', submitted['verify_ssl'])
            if section.get('verify_ssl', False) and not verify_ssl:
                # Turning verification off hands the mirrored key to whatever
                # answers in place of the server.
                changed = True
        if changed:
            raise ValidationError('Re-enter the key to use a different address')


def _usable_plugin_manifest(candidate):
    """Whether an installed row's manifest document declares its config fields.

    A manifest that carries none of the structure the declarations are read
    from, a config_schema carrying its properties and a string list of
    secret fields, is not a plugin whose fields can be vouched for:
    ``manifest: {}`` declares nothing, so neither the fields formatted as a
    uri nor the secrets can be known. Such a document is unusable the same way
    a missing one is, never a plugin without secrets; only a document that
    declares its fields, however few, says what its section's values are.
    A config_schema that carries its properties declares them with or without
    the ``type: object`` marker the manifest validator stamps on a schema at
    install time: an installed row whose stored schema came without the
    marker, an older write or another writer leaving it out, still says what
    its section's values are, so the declarations are read from it rather than
    the row being handed to the fail-closed rule a structureless document
    gets.
    """
    if not isinstance(candidate, dict):
        return False
    schema = candidate.get("config_schema")
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    if any(not isinstance(field, str) or not isinstance(attribute, dict)
           for field, attribute in properties.items()):
        return False
    secrets = candidate.get("secret_fields", [])
    return isinstance(secrets, list) and all(isinstance(field, str) for field in secrets)


def _installed_provider_hub_manifest_fields():
    """What each installed plugin's manifests declare about its config fields.

    Returns (declared, unreadable): declared maps provider_id to the config
    fields its manifests format as a uri, the fields they declare
    write-only, the default destinations a cleared field falls back to, and
    the installation's own config store, and unreadable holds the providers
    with an installed row whose manifest data could not be read. Every
    installed state is read, not only the active ones, and the declarations
    are the union of the active and the staged manifest, because a pending
    update swaps the manifest in at activation and the token a save keeps
    now travels to the addresses the staged one declares from then on. A
    row whose manifests could not be read keeps its own config store in the
    mapping too, with nothing declared beside it, so the Hub's own save path
    can tell a change from nothing while the fields that would need re-entry
    stay unknowable. Returns (None, None) when the state itself cannot be
    read, a failed read, a state module with no reader, a state that is not
    a mapping or an installations value that is not one: no section's plugin
    data can then be vouched for, not even whether it has a plugin. A
    document whose root is not an object is one of those unreadable states,
    read through the strict reader rather than as an empty installation
    inventory.
    """
    try:
        state_module = sys.modules.get("provider_hub.state")
        if state_module is None:
            from provider_hub import state as state_module
        # The strict reader tells a malformed state document, whose root is
        # not an object, from an empty installation inventory: the plain
        # reader answers a default for a malformed document, and the move
        # would be checked against an installation list that no longer
        # exists. A state module that only carries the plain reader still
        # serves it, the same reader it always had.
        load_state = getattr(state_module, "load_state_strict", None)
        if not callable(load_state):
            load_state = getattr(state_module, "load_state", None)
        if not callable(load_state):
            # A state module with no reader is the state being unreadable,
            # not the state saying nothing is installed: no section's plugin
            # data can be vouched for.
            return None, None
        state = load_state()
        if not isinstance(state, dict):
            # A state that is not a mapping is the state being unreadable, not
            # the state saying nothing is installed: no section's plugin data
            # can be vouched for.
            return None, None
        installations = state.get("installations")
        if not isinstance(installations, dict):
            # An installations value that is not a mapping, a list, an empty
            # string or a null among them, is the state being unreadable
            # rather than a state with nothing installed: the provider rows
            # that collection held cannot be vouched away.
            return None, None
        declared = {}
        unreadable = set()
        for provider_key, item in installations.items():
            if not isinstance(item, dict):
                # A row that is not a dictionary is an installed provider
                # whose data cannot be read, not a section with no plugin:
                # it joins the unreadable set under its mapping key, so the
                # save rule checks its section no differently than a row
                # whose manifest data failed to read.
                unreadable.add(str(provider_key).lower())
                continue
            provider_id = str(item.get("provider_id") or provider_key).lower()
            hub_config = item.get("config") if isinstance(item.get("config"), dict) else {}
            manifests = [candidate for candidate in (item.get("manifest"), item.get("staged_manifest"))
                         if _usable_plugin_manifest(candidate)]
            if not manifests:
                # A row without a usable manifest, neither one that declares
                # its fields nor one the structure check can read, is an
                # installed plugin whose declared fields cannot be known, not
                # a plugin without any: its section is handed to the save rule
                # as unreadable, with only its own config store kept so a
                # submission that changes nothing can still be told from one.
                unreadable.add(provider_id)
                declared[provider_id] = (set(), set(), {}, hub_config)
                continue
            uri_fields = set()
            secrets = set()
            defaults = {}
            for manifest in manifests:
                # The declarations are the union of the active and the staged
                # manifest, because a pending update swaps the manifest in at
                # activation and the token a save keeps now travels to the
                # addresses the staged one declares from then on.
                schema = manifest.get("config_schema")
                properties = schema.get("properties") if isinstance(schema, dict) else None
                secrets.update(field.lower() for field in manifest.get("secret_fields", [])
                               if isinstance(field, str))
                if isinstance(properties, dict):
                    for field, attribute in properties.items():
                        if not isinstance(field, str) or not isinstance(attribute, dict):
                            continue
                        if attribute.get("format") == "uri":
                            uri_fields.add(field.lower())
                        # A field may be declared a secret either way, in
                        # secret_fields or with a secret attribute on its
                        # property, a spelling the manifest validation
                        # tolerates. Only an exact true counts, so no truthy
                        # string stands in for it.
                        if attribute.get("secret") is True:
                            secrets.add(field.lower())
                        if attribute.get("default") is not None:
                            # The default destination a cleared or empty field
                            # falls back to, the fallback the provider reader
                            # applies; a pending update can still swap it, so
                            # every manifest's default is kept.
                            field_defaults = defaults.setdefault(field.lower(), [])
                            if attribute["default"] not in field_defaults:
                                field_defaults.append(attribute["default"])
            hub_config = item.get("config") if isinstance(item.get("config"), dict) else {}
            declared[provider_id] = (uri_fields, secrets, defaults, hub_config)
        return declared, unreadable
    except Exception:
        logging.exception("Unable to inspect installed Provider Hub providers")
        return None, None


def _refuse_unverified_plugin_destination(settings_items):
    """The destination rule for plugin settings.

    A plugin keeps its configuration in two stores, the settings section a
    settings save writes and the installation's own config the Hub's update
    path writes, and its stored key reaches the effective configuration
    across both on every search, so a save that moves one of its destination
    fields is refused unless the moved address receives a credential the
    same submission typed, the same rule the media server settings follow. A
    destination field is one named url, host or endpoint, one ending in _url
    or _host, or
    one a manifest formats as a uri, the active or the staged one; the
    secrets are the fields the manifests declare, in secret_fields or with a
    secret attribute on the property. Every installed plugin counts, whatever
    its state, because one that is inactive or waiting for its restart still
    holds its stored key, and its section still moves. The plugin data needed
    to check a section that cannot be read, a row without usable manifest
    data or a state that cannot be read at all, refuses the move outright,
    with no re-entry escape: the fields that would need re-entry cannot be
    known, so any moved field is refused rather than read as a
    provider with no secrets. A section Bazarr reserves for itself is vouched
    for without the state instead, so the proxy url or a base_url moves as
    freely as ever, but a provider's section is never one of them, not even
    one a settings validator shares its name with, because a plugin owns its
    section's credentials even when the data that would name them cannot be
    read. A section with no installed row stays unmanaged, because an
    uninstalled section has no plugin to send a retained key to.
    """
    submitted = {}
    for key, values in settings_items:
        parts = key.split('-') if isinstance(key, str) else []
        if len(parts) != 3 or parts[0] != 'settings':
            continue
        values = values if isinstance(values, list) else [values]
        submitted.setdefault(parts[1], {}).setdefault(parts[2], []).extend(values)
    if not submitted:
        return
    declared, unreadable = _installed_provider_hub_manifest_fields()
    own_sections = set()
    if declared is None:
        try:
            from provider_hub.manifest import reserved_settings_sections
            own_sections = reserved_settings_sections()
        except Exception:
            # The reserved sections could not be derived, so no section can be
            # vouched for without the state: the exemption stays empty and
            # every key-shaped destination move is refused, the fail-closed
            # direction. A section wrongly refused can move again once the
            # settings it needs are readable; a stored key wrongly followed to
            # a new address cannot be taken back.
            own_sections = set()
    for provider_id, fields in submitted.items():
        if declared is None and provider_id.lower() in own_sections:
            # A section Bazarr reserves for itself needs no plugin data to be
            # vouched for: the reserved sections are Bazarr's own with the
            # provider-owned ids taken out, the same notion the pool overlay
            # uses, so no provider's section is vouched for by a settings
            # validator it shares a name with.
            continue
        unchecked = declared is None or provider_id in unreadable
        if unchecked:
            # The plugin data needed to check this section could not be read,
            # so the fields that would need re-entry cannot be known: any
            # field change is an uncertain one and refused rather than read as
            # a provider with no secrets, until the declarations can be
            # recovered. A field named like a destination is one, and a field
            # the manifest formats as a uri is one, and only the manifest can
            # tell which, so nothing that moves is allowed through,
            # key-shaped or not, while a stored value the save does not clear
            # could be the credential a moved destination would take with it:
            # it sits in the section or in the installation's own config, and
            # a state that cannot be read at all leaves that second store
            # unknowable, so a retained credential cannot be ruled out there
            # either. With nothing retained in either store there is nothing a
            # moved address could take anywhere, the same answer a section
            # with no stored key gives.
            section = settings.get(provider_id, {}) if hasattr(settings, 'get') else {}
            hub_config = (None if declared is None
                          else declared.get(provider_id, (set(), set(), {}, {}))[3])
            retained = (any(value not in (None, '') for value in section.values())
                        or hub_config is None
                        or any(value not in (None, '') for value in hub_config.values()))
            if retained:
                for field, values in fields.items():
                    value = _plex_writer_value(field, values)
                    stored = section.get(field, '')
                    if value is None:
                        # A field submitted as one of the writer's empty spellings
                        # clears the stored address, and with the plugin data
                        # unreadable the default the cleared field falls back to
                        # cannot be checked, so a cleared address is refused the
                        # same way a moved one is: the stored key would follow
                        # whichever address the fallback selects.
                        if stored not in (None, ''):
                            raise ValidationError('The provider data needed to check this '
                                                  'address change could not be read, so the '
                                                  'address was not changed')
                    elif value != stored:
                        raise ValidationError('The provider data needed to check this '
                                              'address change could not be read, so the '
                                              'address was not changed')
            continue
        uri_fields, secrets, defaults, hub_config = declared.get(
            provider_id, (set(), set(), {}, {}))
        submitted_values = {field: _plex_writer_value(field, values)
                            for field, values in fields.items()}
        _refuse_plugin_destination_move(
            submitted_values, uri_fields, secrets, defaults,
            settings.get(provider_id, {}) if hasattr(settings, 'get') else {},
            hub_config,
            the_save_writes_the_settings_section=True)


def _plugin_destination_reach(settings_value, hub_present, hub_value, fallbacks):
    """The addresses a plugin's key can reach for one field, the way the
    provider reader builds the effective configuration: the settings
    section's value wins, the installation's own config answers next, and
    the manifest's default applies only while the key is absent from that
    config, the presence the reader itself keys on, so an empty value sitting
    in it blocks the default rather than falling back to it, and a sentinel
    clear that empties the section reaches the default rather than nothing.
    More than one address while a pending update can still swap the default
    a clear reaches; one value for a settled configuration.
    """
    if settings_value not in (None, ''):
        return {settings_value}
    if hub_present:
        return {hub_value}
    reached = {value for value in fallbacks if value not in (None, '')}
    return reached or {''}


def _credential_present(value):
    """Whether a stored or submitted value is a credential someone holds."""
    return isinstance(value, str) and value.strip()


def _refuse_plugin_destination_move(submitted, uri_fields, secrets, defaults,
                                    section, hub_config,
                                    *, the_save_writes_the_settings_section):
    """The one destination rule a plugin's two config stores and two save
    paths follow.

    A plugin keeps its configuration in two stores, the settings section a
    settings save writes and the installation's own config the Hub's update
    path writes, and the provider reader sends the stored key to the effective
    configuration across both, so a save that moves one of its destination
    fields is refused unless the moved address receives a credential this
    submission typed. The credential the moved address receives is the
    effective post-save one, the settings section's value first and the
    installation's own config next, the same order the provider reader reads
    them in: a typed key in the store this save replaces is not the one that
    stays effective when the other store holds one, so the rule checks what
    the reader would send rather than what the submission carried. The
    comparison reads the effective destination after the fallback the reader
    applies, sentinel clears included, so a clear that reaches a default the
    stored address never had is a move the comparison sees, and it reads the
    presence of the installation's own key the way the reader does, so an
    empty value sitting in it is a present one that blocks the default rather
    than an absent one that falls back to it. The two save paths differ only
    in which of the two stores the submission replaces,
    which is what the flag says; everything else is one rule, so the paths
    cannot drift apart again.
    """
    destinations = [field for field in submitted
                    if field in ('url', 'host', 'endpoint')
                    or field.endswith('_url') or field.endswith('_host')
                    or field in uri_fields]
    if not destinations or not secrets:
        return
    moved = False
    for field in destinations:
        value = submitted[field]
        hub_value = hub_config.get(field)
        fallbacks = defaults.get(field, ())
        hub_present = field in hub_config
        before = _plugin_destination_reach(section.get(field, ''), hub_present, hub_value,
                                            fallbacks)
        if the_save_writes_the_settings_section:
            after = _plugin_destination_reach(value, hub_present, hub_value, fallbacks)
        else:
            # The Hub's save writes the installation's own config, where a
            # submitted key is present whatever it carries: the presence the
            # reader's default fallback keys on, so an empty submission
            # reaches the empty address rather than the default.
            after = _plugin_destination_reach(section.get(field, ''), True, value, fallbacks)
        if after != before:
            moved = True
    if not moved:
        return
    for secret in secrets:
        typed = submitted.get(secret)
        if the_save_writes_the_settings_section:
            # The section after the save carries the submitted value, or the
            # stored one when the submission leaves the field out.
            section_value = typed if secret in submitted else (section.get(secret, '') or '')
            hub_value = hub_config.get(secret, '') or ''
            typed_now = secret in submitted and _credential_present(typed)
        else:
            from provider_hub.service import SECRET_PLACEHOLDER
            if secret in submitted and typed != SECRET_PLACEHOLDER:
                hub_value = typed
                typed_now = _credential_present(typed)
            else:
                # The placeholder keeps the stored value, and an absent field
                # leaves it where it is: neither types anything now.
                hub_value = hub_config.get(secret, '') or ''
                typed_now = False
            section_value = section.get(secret, '') or ''
        if _credential_present(section_value):
            # The section's value is the one the moved address receives, and
            # it was typed in this submission only when this submission is
            # the settings save that typed it.
            typed_now = typed_now and the_save_writes_the_settings_section
        elif _credential_present(hub_value):
            # The installation's own config answers when the section holds
            # nothing, and it was typed in this submission only when this
            # submission is the Hub save that typed it.
            typed_now = typed_now and not the_save_writes_the_settings_section
        else:
            # No credential is retained anywhere, so the moved address
            # receives nothing that needs re-entering.
            continue
        if not typed_now:
            raise ValidationError('Re-enter the key to use a different address')


def refuse_unverified_provider_hub_config_move(provider_id, submitted):
    """The Hub's own save path for a plugin's config, the second of its two
    stores: the same destination rule the settings save runs, one rule both
    paths call, reading the effective configuration across both stores.
    Moving a declared destination field is refused while the plugin's stored
    key is retained in either store; plugin data that cannot be read refuses
    the move outright, the same fail-closed answer the settings save gives.
    The message shape is the same one the settings path returns, and each
    path keeps its own protocol for carrying it.
    """
    declared, unreadable = _installed_provider_hub_manifest_fields()
    if declared is None or provider_id in unreadable:
        # The plugin data needed to check this section could not be read, so
        # which fields are destinations cannot be known: while a stored value
        # the save does not clear could be the credential a moved destination
        # would take with it, every field change is an uncertain one and
        # refused, until the declarations can be recovered. The submission is
        # compared against the installation's own config where the row itself
        # could still be read, so a value that changes nothing is not refused;
        # a state that cannot be read at all leaves that store unknowable, so
        # there a retained credential cannot be ruled out and every submitted
        # value refuses. The placeholder keeps the stored value, so it
        # changes nothing either way, and with nothing retained in either of
        # the plugin's two stores there is nothing a moved address could take
        # anywhere.
        section = settings.get(provider_id, {}) if hasattr(settings, 'get') else {}
        stored_config = ({} if declared is None
                         else declared.get(provider_id, (set(), set(), {}, {}))[3])
        retained = (any(value not in (None, '') for value in section.values())
                    or declared is None
                    or any(value not in (None, '') for value in stored_config.values()))
        if retained:
            from provider_hub.service import SECRET_PLACEHOLDER
            for field, value in submitted.items():
                if value == SECRET_PLACEHOLDER:
                    continue
                if value != stored_config.get(field):
                    raise ValueError('The provider data needed to check this address change '
                                     'could not be read, so the address was not changed')
        return
    uri_fields, secrets, defaults, hub_config = declared.get(
        provider_id, (set(), set(), {}, {}))
    section = settings.get(provider_id, {}) if hasattr(settings, 'get') else {}
    try:
        _refuse_plugin_destination_move(
            submitted, uri_fields, secrets, defaults, section, hub_config,
            the_save_writes_the_settings_section=False)
    except ValidationError as error:
        raise ValueError(str(error)) from None


def _plex_destination_rule_items(settings_items):
    """The Plex destination fields a settings save submits, flattened.

    Keys are parsed exactly the way the settings writer parses them, including
    the case it compares and the case it does not, so a mixed-case name reaches
    the same section and field here as it does there. Returns {field: [values]}
    with every field lowercased, because the store underneath is case-insensitive.
    """
    submitted = {}
    for key, values in settings_items:
        if not isinstance(key, str):
            continue
        parts = key.split('-')
        if len(parts) != 3 or parts[0] != 'settings' or parts[1].lower() != 'plex':
            continue
        values = values if isinstance(values, list) else [values]
        submitted.setdefault(parts[2].lower(), []).extend(values)
    return submitted


def _plex_writer_value(field, values):
    """The value the settings writer would store, from one field's form values.

    Mirrors the writer's own coercions, so the rule compares what will really
    be written: a lone form value unwraps to a scalar, the writer's empty-value
    spellings become None, its number casts and its 'true' and 'false' apply,
    and only a string the writer keeps is compared as it is spelled.
    """
    if not values:
        return None
    value = values[0]
    if isinstance(value, str):
        if value in empty_values and value != '':
            return None
        if field not in str_keys:
            for cast in (int, float):
                try:
                    return cast(value)
                except ValueError:
                    continue
        if value == 'true':
            return True
        if value == 'false':
            return False
    return value


def _refuse_unverified_plex_destination(settings_items):
    """The Plex destination rule for settings saves.

    A stored Plex credential is never sent to an address the save changed.
    The sign-in token cannot be typed again, so a save that changes the
    selected server address is accepted only when plex.tv lists it for the
    selected server, exactly like the server picker, and what the save stores
    is the URI plex.tv listed rather than the submitted string; nothing is
    accepted while plex.tv cannot be reached. The listing is owed whenever
    the token stays stored or the save submits one, whatever method the save
    selects, because the method that reads the stored token can be switched
    back. A save that turns certificate verification off is refused outright
    while the sign-in token stays stored, whatever method the save switches
    to, because the token cannot be retyped and the method that reads it can
    be switched back. The stored API key travels to the host, port and TLS
    settings whatever method the save leaves in place, since a later save can
    switch the method back, so changing one of them needs the API key typed
    again, unless the save replaces or clears the stored key; turning
    certificate verification back on never needs it.

    The rule runs in two halves. The plex.tv round-trip and the credential
    and machine-id reads it needs stay before the write locks, because no
    lock may be held across it, and they capture what the listing verified:
    the credential it answered for, the machine id, and the addresses it
    listed. Every gate that reads the stored settings is the locked half,
    returned here for save_settings to run inside the account and config
    locks, where the checked settings and the written settings are one
    state: a save that paused between its check and its lock cannot land
    what its check never saw. Under the locks the captured verification is
    confirmed against the account as it is then stored, and the save is
    refused when it no longer matches, because the listing answered for a
    different account than the one the save would write beside.
    """
    submitted = _plex_destination_rule_items(settings_items)
    if not submitted:
        return None
    if 'instance_id' in submitted:
        # The row the account owns is recorded by the account itself, on every
        # transition and at startup; a save that types in an owner would point
        # the account's token at whichever row the request named.
        raise ValidationError('The Plex destination is chosen by the signed-in account, '
                              'not by this setting')

    def credential_retained(section, field):
        """Whether the save leaves this stored credential in place."""
        if field in submitted:
            # The writer stores the submitted value, so the stored credential
            # is replaced by anything, and cleared by an empty one.
            return False
        value = section.get(field, '') or ''
        return bool(value if isinstance(value, str) else '')

    def addresses_to_verify(section):
        """The submitted server addresses that differ from the stored ones."""
        changed = []
        for field in ('server_url', 'server_connections'):
            if field not in submitted:
                continue
            submitted_values = [value for value in submitted[field]
                                if isinstance(value, str) and value]
            stored_raw = section.get(field, []) if field == 'server_connections' \
                else section.get(field, '') or ''
            if isinstance(stored_raw, (list, tuple)):
                stored = [value for value in stored_raw if value]
            else:
                stored = [stored_raw] if isinstance(stored_raw, str) and stored_raw else []
            if sorted(submitted_values) != sorted(stored):
                changed.extend(submitted_values)
        return changed

    # The listing and the credential and machine-id reads it needs stay
    # before the locks, because no lock may be held across a plex.tv
    # round-trip. What it verified is carried into the locked half, which
    # confirms it there: a save that paused between this check and its lock
    # cannot land what this check never saw.
    section = settings.plex
    addresses = addresses_to_verify(section)
    token_submitted = 'token' in submitted and any(
        isinstance(value, str) and value.strip() for value in submitted['token'])
    verified = None
    if addresses and (credential_retained(section, 'token') or token_submitted):
        # The listing is owed whenever the sign-in token stays stored or the
        # save submits one, whatever method the save selects: the method that
        # reads the stored token can be switched back, and the token cannot
        # be typed again, so the address it would be sent to is verified now.
        from media_servers.plex_account import (SIGN_IN_AGAIN, remembered_listed)
        credential = (_plex_writer_value('token', submitted['token']) if token_submitted
                      else (section.get('token', '') or ''))
        machine_id = (_plex_writer_value('server_machine_id',
                                         submitted.get('server_machine_id'))
                      or section.get('server_machine_id', '') or '')
        from utilities.security_guards import normalized_origin
        listed = remembered_listed('oauth', credential, machine_id)
        if listed is SIGN_IN_AGAIN:
            raise ValidationError('Sign in to Plex to change the server address')
        if listed is None:
            raise ValidationError('Plex could not be reached to verify the server address')
        origins, uris = listed
        for address in addresses:
            if normalized_origin(address) not in origins:
                raise ValidationError('Plex does not list this address for the selected server')
        verified = (credential, machine_id)
        # Store the URI plex.tv listed for each connection, not the string
        # the request submitted.
        for index, (key, values) in enumerate(settings_items):
            parts = key.split('-') if isinstance(key, str) else []
            if (len(parts) != 3 or parts[0] != 'settings'
                    or parts[1].lower() != 'plex'
                    or parts[2].lower() not in ('server_url', 'server_connections')):
                continue
            if not isinstance(values, list):
                values = [values]
            listed_values = [uris[normalized_origin(value)] for value in values
                             if isinstance(value, str) and value
                             and normalized_origin(value) in uris]
            if listed_values:
                settings_items[index] = (key, listed_values)

    def refuse_under_the_write_locks():
        """The Plex gates that read the stored settings, under the write locks.

        The lock boundary is where the checked settings and the written
        settings are one state: a save that paused between its check and its
        lock cannot land what its check never saw, so every gate that reads
        the stored settings runs here, on the settings as they are now, and
        the verification captured before the locks is confirmed against the
        account as it is now stored.
        """
        section = settings.plex
        method = str(_plex_writer_value('auth_method', submitted.get('auth_method'))
                     or section.get('auth_method', 'apikey') or 'apikey').lower()

        # The token-condition listing gate. The listing is owed whenever the
        # sign-in token stays stored or the save submits one, whatever method
        # the save selects, and a token that arrived after this save's check
        # cannot have its addresses verified under the locks, where no
        # plex.tv round-trip may run. The verification captured before the
        # locks has to answer for the account as it is now stored instead,
        # and the save is refused when it does not: the listing answered for
        # a different credential than the one now stored, or for a server a
        # newer selection has since replaced, so the verification is stale
        # and the retry performs its own listing. An emptied machine id is
        # the account's own clear dropping its selection, not a newer
        # account: the address the save stores stays one plex.tv listed for
        # the same credential, and the next use asks again for the account
        # there now.
        addresses = addresses_to_verify(section)
        if addresses and (credential_retained(section, 'token') or token_submitted):
            credential = (_plex_writer_value('token', submitted['token']) if token_submitted
                          else (section.get('token', '') or ''))
            machine_id = (_plex_writer_value('server_machine_id',
                                             submitted.get('server_machine_id'))
                          or section.get('server_machine_id', '') or '')
            if (verified is None or credential != verified[0]
                    or (machine_id and machine_id != verified[1])):
                raise ValidationError('The Plex account changed while the server address was '
                                      'being verified. Try the save again.')

        # The sign-in token cannot be typed again, so the account server's
        # certificate verification cannot be turned off while it stays stored,
        # whatever method the save switches to: the method that reads it can be
        # switched back, and the stored token would travel unverified. The
        # submission is what says the field was sent at all: one of its empty
        # spellings becomes None, which is verification off just as 'false' is.
        if 'verify_ssl' in submitted:
            verify_ssl = _plex_writer_value('verify_ssl', submitted['verify_ssl'])
            if (section.get('verify_ssl', False) and not verify_ssl
                    and (method == 'oauth' or credential_retained(section, 'token'))):
                raise ValidationError('Sign in to Plex again to turn certificate verification '
                                      'off for the account server')

        # The stored API key is kept or retyped by the same save whatever method
        # the save leaves in place, so the host, port and TLS settings owe it
        # their re-entry whatever that method is: a later save can switch the
        # method back and the stored key would follow the moved address.
        key_changed = False
        for field, fallback in (('ip', ''), ('port', 32400), ('ssl', False)):
            if field not in submitted:
                continue
            value = _plex_writer_value(field, submitted[field])
            stored = section.get(field, fallback)
            if stored is None:
                stored = fallback
            if value is None:
                # An empty spelling clears the stored value, and the cleared
                # field falls back to the default the connection applies, so
                # the destination the stored key is sent to is the default one:
                # a move whenever the stored value was not that default
                # already, the same refusal a typed move gets.
                value = fallback
            if value != stored:
                key_changed = True
        if 'verify_ssl' in submitted:
            verify_ssl = _plex_writer_value('verify_ssl', submitted['verify_ssl'])
            if section.get('verify_ssl', False) and not verify_ssl:
                # Turning verification off hands the key to whatever answers in
                # place of the server; turning it back on only hardens the
                # connection and never needs the key again.
                key_changed = True
        if key_changed and credential_retained(section, 'apikey'):
            raise ValidationError('Re-enter the key to use a different address')

    return refuse_under_the_write_locks


def save_settings(settings_items):
    from media_servers.http import MediaServerError, parse_verify_ssl
    from secret_store.registry import IMPORT_ONLY_SECTIONS

    items = _refuse_repeated_settings_items(list(settings_items))
    validate_metadata_settings(items)
    # The Plex guard runs its plex.tv round-trip before any lock is taken,
    # because no lock may be held across it, and hands back the gates that
    # read the stored settings for the locked half of the rule below.
    plex_destination_lock_phase = _refuse_unverified_plex_destination(items)

    for index, (key, value) in enumerate(items):
        parts = key.lower().split('-')
        if len(parts) > 1 and parts[1] in IMPORT_ONLY_SECTIONS:
            raise ValidationError('Use media server instances to edit connection settings')
        if key in NATIVE_MASTER_KEYS:
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            try:
                items[index] = key, parse_verify_ssl(value)
            except MediaServerError:
                raise ValidationError('Invalid native media server master switch') from None

    from discover.metadata import CONFIG_LOCK, invalidate_metadata
    # A save that changes the method, the credential or the selected addresses
    # of the Plex account is an account transition, and every account
    # transition and every verification's write phase runs under the account
    # lock: the save takes it too, so an older verification can no longer
    # write the account or the selection a newer save just changed while its
    # own write phase holds the lock. It is entered before the config lock,
    # the order every account handler already writes the config in, so the
    # two locks never wait on each other the other way round.
    account_lock = contextlib.nullcontext()
    if any(key in PLEX_ACCOUNT_KEYS for key, _value in items):
        from media_servers.plex_account import ACCOUNT_LOCK
        account_lock = ACCOUNT_LOCK
    with account_lock, CONFIG_LOCK:
        # The destination checks read the stored settings, so they run inside
        # the write boundary, where the checked settings and the written
        # settings are one state: a save that paused between its check and
        # its lock cannot land what its check never saw, and a secret a
        # newer save stored inside that window is caught here rather than
        # left following the moved address. The Plex guard's locked half
        # confirms the verification its pre-lock round-trip captured against
        # the account as it is now stored.
        if plex_destination_lock_phase is not None:
            plex_destination_lock_phase()
        _refuse_unverified_scalar_arr_destination(items)
        _refuse_unverified_plugin_destination(items)
        metadata_submitted = any(key.startswith("settings-discover-") or key == "settings-general-metadata_language"
                                 for key, _ in items)
        changed = False
        if metadata_submitted:
            previous = dict(settings.discover)
            previous_language = settings.get("general.metadata_language", "")
            effective_language = previous_language
            effective = dict(previous)
            for key, values in items:
                if key == "settings-general-metadata_language":
                    effective_language = values[0]
                if key.startswith("settings-discover-"):
                    field = key.removeprefix("settings-discover-")
                    if field != "tmdb_access_token" or values[0] != "***":
                        effective[field] = values[0]
            changed = effective != previous or effective_language != previous_language
        persisted = False

        def settings_persisted():
            nonlocal persisted
            persisted = True
            if changed:
                invalidate_metadata()

        try:
            _save_settings_with_native(items, strict_metadata=changed, on_persisted=settings_persisted)
        except Exception:
            if persisted:
                # The save is on disk and only what follows it failed, so the
                # caller keeps what goes with a written save rather than taking
                # it for an unsaved one. The cause stays in the log, not in the
                # message the caller passes on.
                logging.exception("Settings were saved, but applying them failed")
                if changed:
                    raise MetadataFollowupError(
                        "Metadata settings were saved, but application refresh failed. Reload settings before retrying."
                    ) from None
                raise SettingsFollowupError(
                    "Settings were saved, but applying them failed. Reload settings before retrying."
                ) from None
            if metadata_submitted:
                settings.set("discover", previous)
                settings.set("general.metadata_language", previous_language)
            raise


# The server-owned counter behind session revocation. It is never read out by
# the settings API and never written by it: a client that could set it could
# bring a revoked cookie back to life.
SESSION_GENERATION_PATH = 'auth.session_generation'


def _auth_identity():
    """The login settings a session depends on: the mode, the account and its password."""
    return (settings.auth.type, settings.auth.username, settings.auth.password)


def _withdraw_login_advance(login_changed, advance, reloaded, identity_before):
    """A refused login-change save gives back the generation it advanced, if its login is not on disk.

    A refusal does not prove the file is untouched: the write can report failure
    after the new file is already in place (a rename that fell back to a copy and
    then failed on the file's mode). Then the new login is on disk, in force
    after the reload, and every earlier cookie has to stay revoked, so the
    advance stays. It is given back only when the reloaded file carries neither
    the advance nor a login other than the one before this save. `reloaded` is
    what restore_persisted_settings read from the file; None means a caller that
    has nothing to say about the disk, which is treated as a write that landed
    nothing.
    """
    if not login_changed:
        return
    if reloaded is not None:
        generation_landed = reloaded.generation is None or reloaded.generation >= advance.number
        login_landed = reloaded.login != identity_before
        if generation_landed or login_landed:
            logging.error('A settings save was refused but its login change had already reached the '
                          'disk. The new login is in force and session generation %s stays, so earlier '
                          'sessions remain signed out.', advance.number)
            return
    from app.auth.generation import withdraw_advance
    withdraw_advance(advance)


def _save_settings(settings_items, native_configuration=None, *, strict_metadata=False,
                   on_persisted=None):
    # Validate repeated form values before applying any changes, including the
    # single-value and empty-list representations used by the settings editor.
    #
    # The settings store underneath is case-insensitive, so a case variant of the name
    # would reach the same key while skipping this normalizer and leaving a second,
    # unvalidated copy in the config file. The name is therefore compared case
    # insensitively and rewritten to its canonical form, because every later step here
    # compares the last segment against array_keys and str_keys exactly.
    #
    # The section is compared exactly. Matching the name alone let a key naming any other
    # section be redirected into the translator's, which is a silent cross-section write.
    settings_items = [
        ('settings-translator-openrouter_provider_order',
         normalize_openrouter_provider_order([] if value == [''] else value))
        if _is_provider_order_key(key) else (key, value)
        for key, value in settings_items
    ]
    _require_provider_order_for_custom_routing(settings_items)
    _validate_postprocessing_command(settings_items)
    _validate_auth_password(settings_items)

    def _registered_credential(key):
        """Whether a submitted settings key names a registered at-rest
        credential. The dotted path the registry knows is built from the
        lowercased section and field, which is also the shape the settings
        store itself is case-insensitive about."""
        parts = key.split('-')
        return (parts[0] == 'settings' and len(parts) == 3
                and f"{parts[1].lower()}.{parts[2].lower()}" in at_rest_secret_paths())

    # The type of a credential's value is checked before anything is applied.
    # A repeated form key arrives as a list, and refusing it inside the loop
    # below would leave the items earlier in the same request already applied
    # in memory: the next unrelated save would write them, and the recoverable
    # ciphertext the refusal was protecting would be gone. The single-value
    # shape the loop unwraps is checked as unwrapped, and a list with more
    # than one element is refused here, so a refusal is always atomic: every
    # rejection a credential can cause happens before the first item of the
    # request is applied.
    for key, value in settings_items:
        if not _registered_credential(key):
            continue
        checked = value[0] if isinstance(value, list) and len(value) == 1 else value
        if not isinstance(checked, (str, type(None))):
            raise ValidationError(f'{key.split("-")[-1]} must be text')

    configure_debug = False
    configure_log_rotation = False
    hi_extension_changed = False
    configure_captcha = False
    update_schedule = False
    sonarr_changed = False
    radarr_changed = False
    sportarr_changed = False
    plex_account_changed = False
    update_path_map = False
    configure_proxy = False
    exclusion_updated = False
    sonarr_exclusion_updated = False
    radarr_exclusion_updated = False
    sportarr_exclusion_updated = False
    use_embedded_subs_changed = False
    embedded_subtitles_parser_changed = False
    undefined_audio_track_default_changed = False
    undefined_subtitles_track_default_changed = False
    audio_tracks_parsing_changed = False
    adaptive_searching_max_age_changed = False
    translator_settings_changed = False
    reset_providers = False
    provider_caches_to_clear = set()
    reset_fanout_pool = False
    reset_compat_pool = False
    invalidate_compat_cache = False
    active_provider_hub_provider_ids = None
    provider_hub_status_clears = set()
    clear_disabled_provider_hub_statuses = False

    # Who may sign in, as it stands before this save. A change to any of it
    # revokes the sessions signed under the old one, once the loop is done.
    auth_identity_before = _auth_identity()

    # Subzero Mods
    update_subzero = False
    subzero_mods = get_array_from(settings.general.subzero_mods)

    if len(subzero_mods) == 1 and subzero_mods[0] == '':
        subzero_mods = []

    for key, value in settings_items:

        settings_keys = key.split('-')

        if '.'.join(part.lower() for part in settings_keys[1:3]) == SESSION_GENERATION_PATH:
            continue

        if settings_keys[-1].startswith('__'):
            # The settings API's own descriptor key and anything else the
            # serializer reserves. A caller echoing one back must not be
            # able to write it into the config, where _get_settings would
            # hand it out again as if it were a setting.
            continue

        if key in {'settings-discover-tmdb_access_token', 'settings-discover-locale'}:
            if key.endswith('tmdb_access_token') and value[0] == '***':
                continue
            settings.discover[settings_keys[-1]] = value[0]
            continue

        # A registered credential is identified before any generic
        # normalization below: every conversion that follows exists for
        # ordinary settings and would mangle a credential. It keeps its
        # string shape all the way to disk; a value that is not text is
        # refused (the pre-validation pass already caught the shapes that
        # reach here as lists, this is the backstop inside the loop, on the
        # unwrapped value).
        registered_credential = _registered_credential(key)

        # Make sure that text based form values aren't passed as list. A
        # registered credential unwraps its single element independently of
        # array_keys: a secret named like an array-typed setting is still a
        # scalar credential, and leaving it wrapped would push it into the
        # generic array handling below that exists for ordinary settings.
        if isinstance(value, list) and len(value) == 1 and (
                registered_credential or settings_keys[-1] not in array_keys):
            value = value[0]
            if not registered_credential and value in empty_values and value != '':
                # A sentinel like 'null' or 'undefined' is an empty ordinary
                # setting. Typed into a credential it is the credential:
                # stored as the literal text, never converted into a clear.
                value = None

        if registered_credential and not isinstance(value, (str, type(None))):
            raise ValidationError(f'{settings_keys[2]} must be text')

        # try to cast string as integer or float
        if not registered_credential and isinstance(value, str) and settings_keys[-1] not in str_keys:
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass

        # Make sure empty language list are stored correctly
        if (not registered_credential
                and settings_keys[-1] in array_keys and settings_keys[-1] != 'openrouter_provider_order'
                and value and value[0] in empty_values):
            value = []

        # Handle path mappings settings since they are array in array
        if not registered_credential and settings_keys[-1] in ['path_mappings', 'path_mappings_movie', 'path_mappings_sports']:
            value = [x.split(',') for x in value if isinstance(x, str)]

        # A field the active manifest declares boolean is a toggle even
        # when a registered credential owns its name: the submitted text
        # "false" is the off state the worker reads, never a truthy string
        # stored under the secret path.
        boolean_toggle = (registered_credential
                          and f"{settings_keys[1].lower()}.{settings_keys[2].lower()}"
                          in manifest_boolean_secret_paths())
        if value == 'true' and (not registered_credential or boolean_toggle):
            value = True
        elif value == 'false' and (not registered_credential or boolean_toggle):
            value = False

        # Handle JSON strings for dict settings. A registered credential is
        # never parsed: a secret field whose name collides with a dict-typed
        # setting (a plugin secret named 'tiers' holding JSON text) is stored
        # as the typed text, or the parsed dict would ride past both the
        # encryption at rest and the API masking.
        if (not registered_credential and settings_keys[-1] in [
                'provider_priorities', 'provider_languages',
                'provider_score_modifiers', 'tiers'] and isinstance(value, str)):
            try:
                value = json.loads(value)
            except ValueError:
                pass

        if key in ['settings-general-use_embedded_subs', 'settings-general-ignore_pgs_subs',
                   'settings-general-ignore_vobsub_subs', 'settings-general-ignore_ass_subs']:
            use_embedded_subs_changed = True

        if key == 'settings-general-embedded_subtitles_parser':
            embedded_subtitles_parser_changed = value != settings.general.embedded_subtitles_parser

        # A hold the failed-translation record put on an item describes the
        # translator as it was, so any changed translator setting clears the
        # whole record and the next scan may offer those items again.
        if settings_keys[:2] == ['settings', 'translator'] and len(settings_keys) == 3 \
                and value != _settings_value(settings, settings_keys[1:]):
            translator_settings_changed = True

        if key == 'settings-general-adaptive_searching_max_age':
            if value != settings.general.adaptive_searching_max_age:
                adaptive_searching_max_age_changed = True

        if key == 'settings-general-default_und_audio_lang':
            undefined_audio_track_default_changed = True

        if key == 'settings-general-parse_embedded_audio_track':
            audio_tracks_parsing_changed = True

        if key == 'settings-general-default_und_embedded_subtitles_lang':
            undefined_subtitles_track_default_changed = True

        if key in ['settings-general-base_url', 'settings-sonarr-base_url', 'settings-radarr-base_url']:
            value = base_url_slash_cleaner(value)

        if key == 'settings-general-instance_name' and value == '':
            value = None

        if key == 'settings-auth-password':
            if value == AUTH_PASSWORD_MASK and settings.auth.password:
                # The settings read hands out the mask, never the hash, and a
                # client that sends back what it read means to keep the stored
                # password.
                value = settings.auth.password
            if value != settings.auth.password and value is not None:
                from utilities.helper import hash_password
                value = hash_password(value)

        if key == 'settings-general-debug':
            configure_debug = True

        if key in ('settings-log-max_file_size_mb', 'settings-log-backup_count'):
            configure_log_rotation = True

        if key == 'settings-general-hi_extension':
            hi_extension_changed = True

        if key in ['settings-general-anti_captcha_provider', 'settings-anticaptcha-anti_captcha_key',
                   'settings-deathbycaptcha-username', 'settings-deathbycaptcha-password',
                   'settings-captchaai-captchaai_key']:
            configure_captcha = True

        if key in ['update_schedule', 'settings-general-use_sonarr', 'settings-general-use_radarr',
                   'settings-general-use_sportarr',
                   'settings-general-auto_update', 'settings-general-upgrade_subs',
                   'settings-sonarr-series_sync', 'settings-radarr-movies_sync',
                   'settings-sportarr-sports_sync',
                   'settings-sonarr-full_update', 'settings-sonarr-full_update_day', 'settings-sonarr-full_update_hour',
                   'settings-radarr-full_update', 'settings-radarr-full_update_day', 'settings-radarr-full_update_hour',
                   'settings-sportarr-full_update', 'settings-sportarr-full_update_day',
                   'settings-sportarr-full_update_hour',
                   'settings-general-wanted_search_frequency', 'settings-general-wanted_search_frequency_movie',
                   'settings-general-wanted_search_frequency_sports',
                   'settings-general-upgrade_frequency', 'settings-backup-frequency', 'settings-backup-day',
                   'settings-backup-hour']:
            update_schedule = True

        if key in ['settings-general-use_sonarr', 'settings-sonarr-ip', 'settings-sonarr-port',
                   'settings-sonarr-base_url', 'settings-sonarr-ssl', 'settings-sonarr-apikey']:
            sonarr_changed = True

        if key in ('settings-plex-auth_method', 'settings-plex-token', 'settings-plex-apikey',
                   'settings-plex-server_url'):
            # Which credential the Plex account authenticates with, the
            # credential itself, or the address its selection points at: what
            # its destination row carries, so the row is re-synced once the
            # save is on disk below, exactly as every account transition
            # re-syncs it.
            plex_account_changed = True

        if key in ['settings-general-use_radarr', 'settings-radarr-ip', 'settings-radarr-port',
                   'settings-radarr-base_url', 'settings-radarr-ssl', 'settings-radarr-apikey']:
            radarr_changed = True

        # Sports has no scalar connection block: its instances live in
        # arr_instances and their own edits already refresh the runtime. The
        # master toggle is the one sports setting that does not, and without
        # this the event streams kept running after it was switched off, until
        # a restart or an unrelated instance edit happened to refresh them.
        if key == 'settings-general-use_sportarr':
            sportarr_changed = True

        if key in ['settings-general-path_mappings', 'settings-general-path_mappings_movie',
                   'settings-general-path_mappings_sports']:
            update_path_map = True

        if key in ['settings-proxy-type', 'settings-proxy-url', 'settings-proxy-port', 'settings-proxy-username',
                   'settings-proxy-password']:
            configure_proxy = True

        if key in ['settings-sonarr-excluded_tags', 'settings-sonarr-only_monitored',
                   'settings-sonarr-excluded_series_types', 'settings-sonarr-exclude_season_zero',
                   'settings-radarr-excluded_tags', 'settings-radarr-only_monitored',
                   'settings-sportarr-excluded_tags', 'settings-sportarr-excluded_sports',
                   'settings-sportarr-only_monitored']:
            exclusion_updated = True

        if key in ['settings-sportarr-excluded_tags', 'settings-sportarr-excluded_sports',
                   'settings-sportarr-only_monitored']:
            sportarr_exclusion_updated = True

        if key in ['settings-sonarr-excluded_tags', 'settings-sonarr-only_monitored',
                   'settings-sonarr-excluded_series_types', 'settings-sonarr-exclude_season_zero']:
            sonarr_exclusion_updated = True

        if key in ['settings-radarr-excluded_tags', 'settings-radarr-only_monitored']:
            radarr_exclusion_updated = True

        # The cached logins are cleared once the save has been written, below: a
        # refused save keeps the old credentials, and their logins with them.
        if key == 'settings-addic7ed-username':
            if value != settings.addic7ed.username:
                reset_providers = True
                provider_caches_to_clear.add('addic7ed_data')
        elif key == 'settings-addic7ed-password':
            if value != settings.addic7ed.password:
                reset_providers = True
                provider_caches_to_clear.add('addic7ed_data')

        if key == 'settings-legendasdivx-username':
            if value != settings.legendasdivx.username:
                reset_providers = True
                provider_caches_to_clear.add('legendasdivx_cookies2')
        elif key == 'settings-legendasdivx-password':
            if value != settings.legendasdivx.password:
                reset_providers = True
                provider_caches_to_clear.add('legendasdivx_cookies2')

        if key == 'settings-opensubtitles-username':
            if value != settings.opensubtitles.username:
                reset_providers = True
                provider_caches_to_clear.add('os_token')
        elif key == 'settings-opensubtitles-password':
            if value != settings.opensubtitles.password:
                reset_providers = True
                provider_caches_to_clear.add('os_token')
        elif key == 'settings-opensubtitles-use_web_scraper':
            if value != settings.opensubtitles.use_web_scraper:
                reset_providers = True
                provider_caches_to_clear.add('os_token')  # Clear any cached tokens
        elif key == 'settings-opensubtitles-scraper_service_url':
            if value != settings.opensubtitles.scraper_service_url:
                reset_providers = True


        if key == 'settings-opensubtitlescom-username':
            if value != settings.opensubtitlescom.username:
                reset_providers = True
                provider_caches_to_clear.add('oscom_token')
        elif key == 'settings-opensubtitlescom-password':
            if value != settings.opensubtitlescom.password:
                reset_providers = True
                provider_caches_to_clear.add('oscom_token')

        if key == 'settings-titlovi-username':
            if value != settings.titlovi.username:
                reset_providers = True
                provider_caches_to_clear.add('titlovi_token')
        elif key == 'settings-titlovi-password':
            if value != settings.titlovi.password:
                reset_providers = True
                provider_caches_to_clear.add('titlovi_token')

        if key == 'settings-subsource-apikey':
            if value != settings.subsource.apikey:
                reset_providers = True

        if key in ('settings-general-provider_score_modifiers',
                   'settings-general-ai_translated_score_penalty'):
            # A cached compat envelope carries the projected scores in it, so
            # an edited modifier would leave external clients on the old
            # numbers until the entry expires, up to a day later.
            invalidate_compat_cache = True

        if key in ('settings-general-enabled_providers',
                   'settings-general-provider_languages'):
            # Defer the reset until AFTER all values in this batch are
            # written, same reasoning as reset_fanout_pool below.
            # provider_languages changes affect per-provider exclusions
            # which alter compat cache keys and provider pool behavior.
            reset_compat_pool = True
            if key == 'settings-general-enabled_providers':
                clear_disabled_provider_hub_statuses = True

        if settings_keys[0] == 'settings' and len(settings_keys) >= 3:
            if active_provider_hub_provider_ids is None:
                active_provider_hub_provider_ids = _active_provider_hub_provider_ids()
            if settings_keys[1] in active_provider_hub_provider_ids:
                reset_compat_pool = True
                provider_hub_status_clears.add(settings_keys[1])

        if key in ('settings-compat_endpoint-enabled', 'settings-compat_endpoint-serve_local_subs'):
            update_schedule = True

        if key in ('settings-compat_endpoint-fanout_max_workers',
                   'settings-compat_endpoint-max_concurrent_fanouts'):
            # Defer the reset until AFTER all values in this batch are
            # written. Resetting in-loop opens a window where a
            # concurrent compat request could re-init the shared
            # executor by reading the OLD value before the assignment
            # at the bottom of the loop body lands.
            reset_fanout_pool = True

        if reset_providers:
            # Defer the compat-pool reset for the same race reason as
            # the fanout pool above. Resetting here, before the
            # settings[...] = value assignment lands, would let a
            # concurrent /compat request re-init the provider pool
            # with stale provider settings or credentials.
            reset_compat_pool = True

        if settings_keys[0] == 'settings':
            if len(settings_keys) == 3:
                # A save that carries a credential path ends its decrypt
                # failure, whatever it carries: a new value replaces the
                # credential and an explicit clear removes it, and from
                # here on neither the descriptor nor the preserve pass may
                # hold on to the old value. This runs before the write, so
                # a refused save's reload from disk re-adds the failure if
                # the stored ciphertext still will not decrypt.
                clear_decrypt_failure_path(f"{settings_keys[1].lower()}.{settings_keys[2].lower()}")
                _settings_mapping(settings, settings_keys[1])[settings_keys[2]] = value
            elif len(settings_keys) == 4:
                section = _settings_mapping(settings, settings_keys[1])
                try:
                    inner = section[settings_keys[2]]
                except KeyError:
                    inner = None
                # Stored whole, merged with what is there: a mapping read back
                # from the settings store can be a copy, and a value written
                # into a copy never reaches the store.
                merged = {} if inner is None else dict(inner)
                merged[settings_keys[3]] = value
                section[settings_keys[2]] = merged

        if settings_keys[0] == 'subzero':
            mod = settings_keys[1]
            if mod in subzero_mods and not value:
                subzero_mods.remove(mod)
            elif value:
                subzero_mods.append(mod)

            # Handle color
            if mod == 'color':
                previous = None
                for exist_mod in subzero_mods:
                    if exist_mod.startswith('color'):
                        previous = exist_mod
                        break
                if previous is not None:
                    subzero_mods.remove(previous)
                if value not in empty_values:
                    subzero_mods.append(value)

            update_subzero = True

    if update_subzero:
        settings.general.subzero_mods = ','.join(subzero_mods)

    login_changed = _auth_identity() != auth_identity_before
    session_was_in_force, advance = False, None

    try:
        settings.validators.validate()
        validate_log_regex()
    except ValidationError:
        restore_persisted_settings()
        raise
    else:
        if login_changed:
            # In memory only: the write below saves it with the rest of this
            # save. A save that is refused after this point takes its own
            # advance back (a reload cannot, it never moves the number
            # backwards). After validation, so a save that is refused for its
            # values never moves it.
            from app.auth.generation import advance_for_login_change
            session_was_in_force, advance = advance_for_login_change()

        if strict_metadata:
            try:
                saved = write_config(strict_metadata=True)
            except Exception:
                reloaded = restore_persisted_settings()
                _withdraw_login_advance(login_changed, advance, reloaded, auth_identity_before)
                raise MetadataPersistenceError("Metadata settings could not be saved. Try again.") from None
        else:
            saved = write_config()

        if saved is not True:
            # The request is refused, so none of it may stay applied. Every
            # submitted value is already on the live settings object by now,
            # and when a media-server master switch travelled with it the
            # caller's `finally` restores only those two switches: without this
            # the process would keep running values that reached no file, tell
            # the user they were saved, and revert them at the next restart.
            # Nothing about that is particular to a master switch, so the check
            # covers every save rather than only those. The cause write_config
            # swallowed (an encryption failure, a permission error) stays in
            # the chain so the refusal says what happened.
            reloaded = restore_persisted_settings()
            _withdraw_login_advance(login_changed, advance, reloaded, auth_identity_before)
            raise ValidationError('Unable to save settings to disk') from _last_write_error

        if login_changed:
            # The save is on disk, so the browser that made it carries on under
            # the new generation; every other browser has to sign in again.
            from app.auth.generation import keep_current_session_signed_in
            keep_current_session_signed_in(session_was_in_force, advance)

        # Before anything below can fail: the save is on disk now, and a failure
        # from here on must not be taken for an unsaved one, which would put the
        # previous master switches and metadata back into the live settings only.
        if native_configuration is not None:
            native_configuration.publish_masters(settings)

        # Each step from here on runs even when one before it failed.
        follow_ups = _FollowUps()

        if on_persisted is not None:
            with follow_ups.step():
                on_persisted()

        # Only now that the save has been written: naming new subtitles with the
        # hearing-impaired extension, clearing provider logins, resetting the
        # pools and queueing library-wide jobs all act on the submitted values,
        # and a job queued for a save that is then refused cannot be recalled.
        # The sports reindex carries the audio mode with it.
        if hi_extension_changed:
            with follow_ups.step():
                os.environ["SZ_HI_EXTENSION"] = settings.general.hi_extension or ""

        # Each one is recorded before it is tried and dropped once done, so one
        # that fails here, or was left by an earlier save, is tried again now.
        unfinished = _unfinished_login_resets
        if unfinished['caches'] or unfinished['throttles']:
            # Providers built since the earlier save can hold the login it could
            # not clear, so they are built again once it is.
            reset_compat_pool = True
        unfinished['caches'].update(provider_caches_to_clear)
        unfinished['throttles'] = unfinished['throttles'] or reset_providers
        for cache_key in sorted(unfinished['caches']):
            with follow_ups.step():
                region.delete(cache_key)
                unfinished['caches'].discard(cache_key)

        if unfinished['throttles']:
            with follow_ups.step():
                from .get_providers import reset_throttled_providers
                reset_throttled_providers(only_auth_or_conf_error=True)
                unfinished['throttles'] = False

        if reset_fanout_pool:
            # All in-loop assignments have committed by now, so the next
            # _get_pool() call will read the new sizing values.
            try:
                from subliminal_patch.core_persistent import reset_pool as _reset_fanout
                _reset_fanout()
            except Exception:
                pass

        if reset_compat_pool:
            # All in-loop assignments have committed by now, so the next
            # /compat request that constructs a pool sees the updated
            # provider list / credentials instead of the stale pre-save
            # values.
            try:
                from compat.service import reset_compat_pool as _reset_compat
                _reset_compat()
            except Exception:
                pass

        if invalidate_compat_cache:
            # Same reasoning: after the writes, so the next request rebuilds
            # against the new values rather than the ones being replaced.
            try:
                from compat.cache import invalidate_all as _invalidate_compat_cache
                _invalidate_compat_cache()
            except Exception:
                pass

        if undefined_subtitles_track_default_changed:
            if settings.general.use_sonarr:
                with follow_ups.step():
                    from subtitles.indexer.series import series_full_scan_subtitles
                    series_full_scan_subtitles(use_cache=True)
            if settings.general.use_radarr:
                with follow_ups.step():
                    from subtitles.indexer.movies import movies_full_scan_subtitles
                    movies_full_scan_subtitles(use_cache=True)

        if settings.general.use_sportarr and (undefined_subtitles_track_default_changed or
                                             use_embedded_subs_changed or audio_tracks_parsing_changed or
                                             embedded_subtitles_parser_changed):
            with follow_ups.step():
                from subtitles.indexer.sports import sports_full_scan_subtitles
                sports_full_scan_subtitles(refresh_audio=audio_tracks_parsing_changed,
                                          audio_mode=bool(settings.general.parse_embedded_audio_track)
                                          if audio_tracks_parsing_changed else None,
                                          audio_refresh_id=secrets.token_hex(16)
                                          if audio_tracks_parsing_changed or embedded_subtitles_parser_changed
                                          else None)

        if audio_tracks_parsing_changed:
            if settings.general.use_sonarr:
                with follow_ups.step():
                    from sonarr.sync.series import update_series
                    update_series()
            if settings.general.use_radarr:
                with follow_ups.step():
                    from radarr.sync.movies import update_movies
                    update_movies()

        if use_embedded_subs_changed or undefined_audio_track_default_changed or adaptive_searching_max_age_changed:
            # Queued rather than run here: this is inside the settings save
            # request, and a library-wide pass held it long enough for a proxy to
            # time out a save that had already been written. And only now that
            # it has been written: a save refused by validation or by the disk
            # changed nothing that needs recalculating.
            with follow_ups.step():
                from subtitles.indexer.missing_refresh import queue_missing_subtitles_recalculation
                queue_missing_subtitles_recalculation()

        if clear_disabled_provider_hub_statuses:
            with follow_ups.step():
                if active_provider_hub_provider_ids is None:
                    active_provider_hub_provider_ids = _active_provider_hub_provider_ids()
                configured = getattr(settings.general, 'enabled_providers', [])
                if isinstance(configured, str):
                    enabled_provider_ids = {
                        item.strip().strip("'\"")
                        for item in configured.strip().strip('[]').split(',')
                        if item.strip()
                    }
                elif isinstance(configured, (list, tuple, set)):
                    enabled_provider_ids = {str(item) for item in configured}
                else:
                    enabled_provider_ids = set()
                provider_hub_status_clears.update(
                    active_provider_hub_provider_ids - enabled_provider_ids)

        if provider_hub_status_clears:
            try:
                from provider_hub import runtime_status
                for provider_id in provider_hub_status_clears:
                    runtime_status.clear(provider_id)
            except Exception:
                logging.exception('Unable to clear stale Provider Hub runtime status')

        # Set the configured state based on config.yaml file existence
        with follow_ups.step():
            from .database import database, update, System
            database.execute(
                update(System)
                .values(configured=1))

        # Reconfigure Bazarr to reflect changes
        if configure_debug:
            with follow_ups.step():
                from .logger import configure_logging
                configure_logging(settings.general.debug or args.debug)

        if configure_log_rotation:
            # In place on the live handler, so the new ceiling holds from the
            # next record rather than from the next restart.
            with follow_ups.step():
                from .logger import apply_log_rotation_settings
                apply_log_rotation_settings()

        if configure_captcha:
            with follow_ups.step():
                configure_captcha_func()

        if update_schedule:
            with follow_ups.step():
                from .scheduler import scheduler
                from .event_handler import event_stream
                scheduler.update_configurable_tasks()
                event_stream(type='task')

        if sonarr_changed:
            # Restart every Sonarr SignalR client and re-fan-out (#156).
            from .signalr_client import restart_sonarr_signalr
            try:
                restart_sonarr_signalr()
            except Exception:
                pass

        if radarr_changed:
            from .signalr_client import restart_radarr_signalr
            try:
                restart_radarr_signalr()
            except Exception:
                pass

        if sportarr_changed:
            # Streams and jobs together: configure_sports_jobs is gated on the
            # same toggle, so leaving it out would stop the streams and leave
            # the scheduled sports jobs registered.
            from sportarr.scheduler import refresh_sports_runtime
            try:
                refresh_sports_runtime()
            except Exception:
                pass

        if plex_account_changed:
            with follow_ups.step():
                # A save that changed the method, the credential or the selected
                # address is an account transition for the row that carries it,
                # and only now that the save is on disk: a refused or unwritten
                # save changes nothing the row should follow.
                _sync_plex_destination()

        if update_path_map:
            with follow_ups.step():
                from utilities.path_mappings import path_mappings
                path_mappings.update()

        if configure_proxy:
            with follow_ups.step():
                configure_proxy_func()

        if translator_settings_changed:
            # The translator the record described a failure for may have been
            # fixed, switched or re-keyed by this save, so the hold it put on
            # those items has nothing left to describe.
            with follow_ups.step():
                from subtitles.tools.translate.failure_record import clear_failed_translations
                clear_failed_translations()

        if exclusion_updated:
            with follow_ups.step():
                from .event_handler import event_stream
                event_stream(type='badges')
                if sonarr_exclusion_updated:
                    event_stream(type='reset-episode-wanted')
                if radarr_exclusion_updated:
                    event_stream(type='reset-movie-wanted')
                # The sports wanted list is computed live against the exclusion
                # settings, so saving them has to invalidate the client's cached
                # sports rows. The 'sports' event is the one the socketio reducer
                # maps to the whole sports query root, wanted included.
                if sportarr_exclusion_updated:
                    event_stream(type='sports')

        follow_ups.raise_first()


def get_array_from(property):
    if property:
        if '[' in property:
            return ast.literal_eval(property)
        elif ',' in property:
            return property.split(',')
        else:
            return [property]
    else:
        return []


def configure_captcha_func():
    # set anti-captcha provider and key
    if settings.general.anti_captcha_provider == 'anti-captcha' and settings.anticaptcha.anti_captcha_key != "":
        os.environ["ANTICAPTCHA_CLASS"] = 'AntiCaptchaProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(settings.anticaptcha.anti_captcha_key)
    elif settings.general.anti_captcha_provider == 'death-by-captcha' and settings.deathbycaptcha.username != "" and \
            settings.deathbycaptcha.password != "":
        os.environ["ANTICAPTCHA_CLASS"] = 'DeathByCaptchaProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(':'.join(
            {settings.deathbycaptcha.username, settings.deathbycaptcha.password}))
    elif settings.general.anti_captcha_provider == 'captchaai' and settings.captchaai.captchaai_key != "":
        os.environ["ANTICAPTCHA_CLASS"] = 'CaptchaAIProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(settings.captchaai.captchaai_key)
    else:
        os.environ["ANTICAPTCHA_CLASS"] = ''
        # Clear the key too, or disabling the vendor leaves the previous
        # credential exported for the rest of the process lifetime.
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = ''


def configure_proxy_func():
    if settings.proxy.type:
        if settings.proxy.username != '' and settings.proxy.password != '':
            proxy = (f'{settings.proxy.type}://{quote_plus(settings.proxy.username)}:'
                     f'{quote_plus(settings.proxy.password)}@{settings.proxy.url}:{settings.proxy.port}')
        else:
            proxy = f'{settings.proxy.type}://{settings.proxy.url}:{settings.proxy.port}'
        os.environ['HTTP_PROXY'] = str(proxy)
        os.environ['HTTPS_PROXY'] = str(proxy)
        exclude = ','.join(settings.proxy.exclude)
        os.environ['NO_PROXY'] = exclude


_SSL_VERIFY_SERVICES = frozenset({'sonarr', 'radarr', 'plex'})


def get_ssl_verify(service):
    """Return the verify parameter for requests calls to a service."""
    if service not in _SSL_VERIFY_SERVICES:
        raise ValueError(f"Unknown service for SSL verify: {service}")
    return settings.get(f'{service}.verify_ssl', False)



def sync_checker(subtitle):
    " This function can be extended with settings. It only takes a Subtitle argument"

    logging.debug("Checker data [%s] for %s", settings.subsync.checker, subtitle)

    bl_providers = settings.subsync.checker.blacklisted_providers

    # TODO
    # bl_languages = settings.subsync.checker.blacklisted_languages

    verdicts = set()

    # You can add more inner checkers. The following is a verfy basic one for providers,
    # but you can make your own functions, etc to handle more complex stuff. You have
    # subtitle data to compare.

    verdicts.add(subtitle.provider_name not in bl_providers)

    met = False not in verdicts

    if met is True:
        logging.debug("BAZARR Sync checker passed.")
        return True
    else:
        logging.debug("BAZARR Sync checker not passed. Won't sync.")
        return False


# Plex OAuth Migration Functions
def _sync_plex_destination():
    """Keep the Plex destination row in step with an account change here.

    The automatic API-key-to-OAuth migration swaps which credential the account
    authenticates with, in both directions, and so does a settings save that
    changes the method, the token, the API key or the selected server address.
    A row still holding the other credential would refresh with one the
    account no longer has, and a row still holding the previous address would
    refresh the server the account no longer points at.
    """
    try:
        from media_servers.plex_account import sync_plex_account
        sync_plex_account()
    except Exception:
        logging.debug('Could not carry the migrated Plex account onto its destination',
                      exc_info=True)


def migrate_plex_config():
    # Generate encryption key if not exists or is empty
    existing_key = settings.plex.get('encryption_key')
    if not existing_key or existing_key.strip() == "":
        logging.debug("Generating new encryption key for Plex token storage")
        key = secrets.token_urlsafe(32)
        settings.plex.encryption_key = key
        write_config()
        logging.debug("Plex encryption key generated")
    
    # Check if user needs seamless migration from API key to OAuth
    migrate_apikey_to_oauth()


def migrate_apikey_to_oauth():
    """
    Seamlessly migrate users from API key authentication to OAuth.
    This preserves their existing configuration while enabling OAuth features.
    
    Safety features:
    - Creates backup before migration
    - Validates before committing changes
    - Implements graceful rollback on failure
    - Handles rate limiting and network issues
    - Delays startup to avoid race conditions
    """
    try:
        # Add startup delay to avoid race conditions with other Plex connections
        time.sleep(5)
        
        auth_method = settings.plex.get('auth_method', 'apikey')
        api_key = settings.plex.get('apikey', '')
        
        # Only migrate if:
        # 1. Currently using API key method
        # 2. Has an API key configured (not empty/None)
        # 3. Plex is actually enabled in general settings
        if not settings.general.get('use_plex', False):
            return
            
        if auth_method != 'apikey' or not api_key or api_key.strip() == '':
            return
            
        # Check if already migrated (has OAuth token)
        if settings.plex.get('token'):
            logging.debug("OAuth token already exists, skipping migration")
            return
            
        # We have determined a migration is needed, now log and proceed
        logging.info("OAuth migration - user has API key configuration that needs upgrading")
            
        # Check if migration is disabled (for emergency rollback)
        if settings.plex.get('disable_auto_migration', False):
            logging.info("auto-migration disabled, skipping")
            return
            
        # Create backup of current configuration
        backup_config = {
            'auth_method': auth_method,
            'apikey': api_key,
            'apikey_encrypted': settings.plex.get('apikey_encrypted', False),
            'ip': settings.plex.get('ip', '127.0.0.1'),
            'port': settings.plex.get('port', 32400),
            'ssl': settings.plex.get('ssl', False),
            'migration_attempted': True,
            'migration_timestamp': datetime.now().isoformat() + '_backup'
        }
        
        # Mark that migration was attempted (prevents retry loops)
        settings.plex.migration_attempted = True
        write_config()
            
        logging.info("Starting Plex OAuth migration, converting API key to OAuth...")
        
        # Add random delay to prevent thundering herd (0-30 seconds)
        import random
        delay = random.uniform(0, 30)
        logging.debug(f"Migration delay: {delay:.1f}s to prevent server overload")  # noqa: G004
        time.sleep(delay)
        
        # Decrypt the API key
        from api.plex.security import TokenManager, get_or_create_encryption_key
        encryption_key = get_or_create_encryption_key(settings.plex, 'encryption_key')
        token_manager = TokenManager(encryption_key)
        
        # Handle both encrypted and plain text API keys
        try:
            if settings.plex.get('apikey_encrypted', False):
                decrypted_api_key = token_manager.decrypt(api_key)
            else:
                decrypted_api_key = api_key
        except Exception as e:
            logging.error(f"Failed to decrypt API key for migration: {e}")  # noqa: G004
            return
            
        # Use API key to fetch user data from Plex with retry logic
        import requests
        headers = {
            'X-Plex-Token': decrypted_api_key,
            'Accept': 'application/json'
        }
        
        # Get user account info with retries
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                user_response = requests.get('https://plex.tv/api/v2/user', 
                                           headers=headers, timeout=10)
                
                if user_response.status_code == 429:  # Rate limited
                    logging.warning(f"Rate limited by Plex API, attempt {attempt + 1}/{max_retries}")  # noqa: G004
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                        continue
                    else:
                        logging.error("Migration failed due to rate limiting, will retry later")
                        return
                        
                user_response.raise_for_status()
                user_data = user_response.json()
                
                username = user_data.get('username', '')
                email = user_data.get('email', '')
                user_id = str(user_data.get('id', ''))
                break
                
            except requests.exceptions.Timeout:
                logging.warning(f"Timeout getting user data, attempt {attempt + 1}/{max_retries}")  # noqa: G004
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    logging.error("Migration failed due to timeouts, will retry later")
                    return
            except Exception as e:
                logging.error(f"Failed to fetch user data for migration: {e}")  # noqa: G004
                return
            
        # Get user's servers with retry logic
        for attempt in range(max_retries):
            try:
                servers_response = requests.get('https://plex.tv/pms/resources',
                                              headers=headers, 
                                              params={'includeHttps': '1', 'includeRelay': '1'},
                                              timeout=10)
                
                if servers_response.status_code == 429:  # Rate limited
                    logging.warning(f"Rate limited getting servers, attempt {attempt + 1}/{max_retries}")  # noqa: G004
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        logging.error("Migration failed due to rate limiting, will retry later")
                        return
                        
                servers_response.raise_for_status()
                
                # Parse response - could be JSON or XML
                content_type = servers_response.headers.get('content-type', '')
                servers = []
                
                if 'application/json' in content_type:
                    resources_data = servers_response.json()
                    for device in resources_data:
                        if isinstance(device, dict) and device.get('provides') == 'server' and device.get('owned'):
                            server = {
                                'name': device.get('name', ''),
                                'machineIdentifier': device.get('clientIdentifier', ''),
                                'connections': []
                            }
                            
                            for conn in device.get('connections', []):
                                server['connections'].append({
                                    'uri': conn.get('uri', ''),
                                    'local': conn.get('local', False)
                                })
                            
                            servers.append(server)
                
                elif 'application/xml' in content_type or 'text/xml' in content_type:
                    # Parse XML response
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(servers_response.text)
                    
                    for device in root.findall('Device'):
                        if device.get('provides') == 'server' and device.get('owned') == '1':
                            server = {
                                'name': device.get('name', ''),
                                'machineIdentifier': device.get('clientIdentifier', ''),
                                'connections': []
                            }
                            
                            # Get connections directly from the XML
                            for conn in device.findall('Connection'):
                                server['connections'].append({
                                    'uri': conn.get('uri', ''),
                                    'local': conn.get('local') == '1'
                                })
                            
                            servers.append(server)
                else:
                    logging.error(f"Unexpected response format: {content_type}")  # noqa: G004
                    return
                
                break
                
            except requests.exceptions.Timeout:
                logging.warning(f"Timeout getting servers, attempt {attempt + 1}/{max_retries}")  # noqa: G004
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    logging.error("Migration failed due to timeouts, will retry later")
                    return
            except Exception as e:
                logging.error(f"Failed to fetch servers for migration: {e}")  # noqa: G004
                return
            
        # Find the server that matches current manual configuration
        current_ip = settings.plex.get('ip', '127.0.0.1')
        current_port = settings.plex.get('port', 32400)
        current_ssl = settings.plex.get('ssl', False)
        current_url = f"{'https' if current_ssl else 'http'}://{current_ip}:{current_port}"
        
        selected_server = None
        selected_connection = None
        
        # Try to match current server configuration
        for server in servers:
            for connection in server['connections']:
                if connection['uri'] == current_url:
                    selected_server = server
                    selected_connection = connection
                    break
            if selected_server:
                break
                
        # If no exact match, try to find the first available local server
        if not selected_server and servers:
            for server in servers:
                for connection in server['connections']:
                    if connection.get('local', False):
                        selected_server = server
                        selected_connection = connection
                        break
                if selected_server:
                    break
                    
        # If still no match, use the first server
        if not selected_server and servers:
            selected_server = servers[0]
            if selected_server['connections']:
                selected_connection = selected_server['connections'][0]
                
        if not selected_server or not selected_connection:
            logging.warning("No suitable Plex server found for migration")
            return
            
        # Encrypt the API key as OAuth token (they're the same thing)
        encrypted_token = token_manager.encrypt(decrypted_api_key)
        
        # Validate OAuth configuration BEFORE making any changes
        oauth_config = {
            'auth_method': 'oauth',
            'token': encrypted_token,
            'username': username,
            'email': email,
            'user_id': user_id,
            'server_machine_id': selected_server['machineIdentifier'],
            'server_name': selected_server['name'],
            'server_url': selected_connection['uri'],
            'server_local': selected_connection.get('local', False)
        }
        
        # Test OAuth configuration before committing
        logging.info("Testing OAuth configuration before applying changes...")
        test_success = False
        
        try:
            # Temporarily apply OAuth settings in memory only
            original_auth_method = settings.plex.auth_method
            original_token = settings.plex.token
            
            settings.plex.auth_method = oauth_config['auth_method']
            settings.plex.token = oauth_config['token']
            settings.plex.server_machine_id = oauth_config['server_machine_id']
            settings.plex.server_name = oauth_config['server_name']
            settings.plex.server_url = oauth_config['server_url']
            settings.plex.server_local = oauth_config['server_local']
            
            # Test connection
            from plex.operations import get_plex_server
            test_server = get_plex_server()
            test_server.account()  # Test connection
            test_success = True
            
            # Restore original values temporarily
            settings.plex.auth_method = original_auth_method
            settings.plex.token = original_token
            
        except Exception as e:
            logging.error(f"OAuth pre-validation failed: {e}")  # noqa: G004
            # Restore original values
            settings.plex.auth_method = original_auth_method
            settings.plex.token = original_token
            return
            
        if not test_success:
            logging.error("OAuth configuration validation failed, aborting migration")
            return
            
        logging.info("OAuth configuration validated successfully, proceeding with migration")
        
        # Now safely apply the OAuth configuration
        settings.plex.auth_method = oauth_config['auth_method']
        settings.plex.token = oauth_config['token']
        settings.plex.username = oauth_config['username']
        settings.plex.email = oauth_config['email']
        settings.plex.user_id = oauth_config['user_id']
        settings.plex.server_machine_id = oauth_config['server_machine_id']
        settings.plex.server_name = oauth_config['server_name']
        settings.plex.server_url = oauth_config['server_url']
        settings.plex.server_local = oauth_config['server_local']
        
        # Mark migration as successful and disable auto-migration
        settings.plex.migration_successful = True
        # Create human-readable timestamp: YYYYMMDD_HHMMSS_randomstring
        random_suffix = secrets.token_hex(4)  # 8 character random string
        settings.plex.migration_timestamp = f"{datetime.now().isoformat()}_{random_suffix}"
        settings.plex.disable_auto_migration = True
        
        # Clean up legacy manual configuration fields (no longer needed with OAuth)
        settings.plex.ip = ''
        settings.plex.port = 32400  # Reset to default
        settings.plex.ssl = False   # Reset to default
        
        # Save configuration with OAuth settings
        write_config()
        
        logging.info(f"Migrated Plex configuration to OAuth for user '{username}'")  # noqa: G004
        logging.info(f"Selected server: {selected_server['name']} ({selected_connection['uri']})")  # noqa: G004
        logging.info("Legacy manual configuration fields cleared (ip, port, ssl)")
        
        # Final validation test
        try:
            test_server = get_plex_server()
            test_server.account()  # Test connection
            logging.info("Migration validated - OAuth connection successful")
            
            # Only now permanently remove API key
            settings.plex.apikey = ''
            settings.plex.apikey_encrypted = False
            write_config()
            # The account authenticates with the token now, so the destination
            # row must stop holding the API key that was just removed.
            _sync_plex_destination()
            logging.info("Legacy API key permanently removed after successful OAuth migration")
            
        except Exception as e:
            logging.error(f"Final OAuth validation failed: {e}")  # noqa: G004
            
            # Restore backup configuration
            logging.info("Restoring backup configuration...")
            settings.plex.auth_method = backup_config['auth_method']
            settings.plex.apikey = backup_config['apikey']
            settings.plex.apikey_encrypted = backup_config['apikey_encrypted']
            settings.plex.ip = backup_config['ip']
            settings.plex.port = backup_config['port']
            settings.plex.ssl = backup_config['ssl']
            
            # Clear OAuth settings and restore legacy manual config
            settings.plex.token = ''
            settings.plex.username = ''
            settings.plex.email = ''
            settings.plex.user_id = ''
            settings.plex.server_machine_id = ''
            settings.plex.server_name = ''
            settings.plex.server_url = ''
            settings.plex.server_local = False
            settings.plex.migration_successful = False
            settings.plex.disable_auto_migration = False  # Allow retry
            
            write_config()
            # Back on the API key, so the destination row goes back with it.
            _sync_plex_destination()
            
            # Test the rollback
            try:
                test_server = get_plex_server()
                test_server.account()  # Test connection with legacy settings
                logging.info("Rollback successful - legacy API key connection restored")
                logging.error("OAuth migration failed but legacy configuration is working. Please configure OAuth manually through the GUI.")
            except Exception as rollback_error:
                logging.error(f"Rollback validation also failed: {rollback_error}")  # noqa: G004
                logging.error("CRITICAL: Manual intervention required. Please reset Plex settings.")
            
    except Exception as e:
        logging.error(f"Unexpected error during Plex OAuth migration: {e}")  # noqa: G004
        # Keep existing configuration intact


def cleanup_legacy_oauth_config():
    """
    Clean up legacy manual configuration fields when using OAuth.
    These fields (ip, port, ssl) are not used with OAuth since server_url contains everything.
    """
    if settings.plex.get('auth_method') != 'oauth':
        return
        
    # Check if any legacy values exist
    has_legacy_ip = bool(settings.plex.get('ip', '').strip())
    has_legacy_ssl = settings.plex.get('ssl', False) == True  # noqa: E712
    has_legacy_port = settings.plex.get('port', 32400) != 32400
    
    # Only disable auto-migration if migration was actually successful
    migration_successful = settings.plex.get('migration_successful', False)
    auto_migration_enabled = not settings.plex.get('disable_auto_migration', False)
    should_disable_auto_migration = migration_successful and auto_migration_enabled
    
    if has_legacy_ip or has_legacy_ssl or has_legacy_port or should_disable_auto_migration:
        logging.info("Cleaning up OAuth configuration")
        
        # Clear legacy manual config fields (not needed with OAuth)
        if has_legacy_ip or has_legacy_ssl or has_legacy_port:
            settings.plex.ip = ''
            settings.plex.port = 32400  # Reset to default
            settings.plex.ssl = False   # Reset to default
            logging.info("Cleared legacy manual config fields (OAuth uses server_url)")
        
        # Disable auto-migration only if it was previously successful
        if should_disable_auto_migration:
            settings.plex.disable_auto_migration = True
            logging.info("Disabled auto-migration (previous migration was successful)")
            
        write_config()


def migrate_plex_library_to_list():
    """
    Migrate old single-string Plex library settings to new list format.
    This migration runs during app initialization to ensure backward compatibility.
    
    Converts:
    - plex.movie_library: string -> list
    - plex.series_library: string -> list
    
    Automatically saves configuration if changes are made.
    """
    changed = False
    
    # Migrate movie library
    if isinstance(settings.plex.movie_library, str):
        old_value = settings.plex.movie_library
        if old_value:  # Only migrate if not empty
            settings.plex.movie_library = [old_value]
            logging.info(f"Migrated plex.movie_library from string to list: {old_value}")  # noqa: G004
            changed = True
        else:
            settings.plex.movie_library = []
            changed = True
    
    # Migrate series library
    if isinstance(settings.plex.series_library, str):
        old_value = settings.plex.series_library
        if old_value:  # Only migrate if not empty
            settings.plex.series_library = [old_value]
            logging.info(f"Migrated plex.series_library from string to list: {old_value}")  # noqa: G004
            changed = True
        else:
            settings.plex.series_library = []
            changed = True
    
    if changed:
        write_config()
        logging.debug("Plex library migration completed successfully")


def initialize_plex():
    """
    Initialize Plex configuration on startup.
    Call this from your main application initialization.
    """
    # Run OAuth migration
    migrate_plex_config()
    
    # Run library multiselect migration
    migrate_plex_library_to_list()
    
    # Clean up legacy fields for existing OAuth configurations
    cleanup_legacy_oauth_config()
    
    # Start cache cleanup if OAuth is enabled
    if settings.general.use_plex and settings.plex.get('auth_method') == 'oauth':
        try:
            from api.plex.security import pin_cache
            
            def cleanup_task():
                while True:
                    time.sleep(300)  # 5 minutes
                    try:
                        pin_cache.cleanup_expired()
                    except Exception:
                        pass
            
            cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
            cleanup_thread.start()
            logging.info("Plex OAuth cache cleanup started")
        except ImportError:
            logging.warning("Plex OAuth cache cleanup - module not found")
    
    logging.debug("Plex configuration initialized")
