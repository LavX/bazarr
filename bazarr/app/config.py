# coding=utf-8

import hashlib  # noqa: F401
import os
import ast
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime

import configparser
import yaml
import platform

from urllib.parse import quote_plus
from utilities.binaries import BinaryNotFound, get_binary
from literals import EXIT_VALIDATION_ERROR
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
    if ip_string == "*":
        return True
    try:
        ip_address(ip_string)
        return True
    except ValueError:
        return False


def validate_tags(tags):
    if not tags:
        return True

    return all(re.match(r"^[a-z0-9_-]+$", item) for item in tags)


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
        raise ValidationError(
            f"Executable '{value}' not found in search path. Please install before making this selection."
        )
    return True


validators = [
    # general section
    Validator(
        "general.flask_secret_key",
        must_exist=True,
        default=hexlify(os.urandom(16)).decode(),
        is_type_of=str,
    ),
    # Master key for the bazarr.secrets crypto module (encrypts every
    # user-visible credential at rest). Default empty so the key is
    # generated lazily on first read - matches the pattern of the legacy
    # plex.encryption_key. Never round-trips through the API.
    Validator(
        "general.secrets_encryption_key", must_exist=True, default="", is_type_of=str
    ),
    Validator(
        "general.ip",
        must_exist=True,
        default="*",
        is_type_of=str,
        condition=validate_ip_address,
    ),
    Validator(
        "general.port", must_exist=True, default=6767, is_type_of=int, gte=1, lte=65535
    ),
    Validator(
        "general.hostname", must_exist=True, default=platform.node(), is_type_of=str
    ),
    Validator("general.base_url", must_exist=True, default="", is_type_of=str),
    Validator(
        "general.instance_name",
        must_exist=True,
        default="Bazarr+",
        is_type_of=str,
        apply_default_on_none=True,
    ),
    Validator("general.path_mappings", must_exist=True, default=[], is_type_of=list),
    Validator("general.debug", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "general.branch",
        must_exist=True,
        default="master",
        is_type_of=str,
        is_in=["master", "development"],
    ),
    Validator("general.auto_update", must_exist=True, default=True, is_type_of=bool),
    Validator(
        "general.single_language", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.minimum_score",
        must_exist=True,
        default=80,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator("general.use_scenename", must_exist=True, default=True, is_type_of=bool),
    Validator(
        "general.use_postprocessing", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.postprocessing_cmd", must_exist=True, default="", is_type_of=str
    ),
    Validator(
        "general.postprocessing_threshold",
        must_exist=True,
        default=90,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator(
        "general.use_postprocessing_threshold",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "general.postprocessing_threshold_movie",
        must_exist=True,
        default=70,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator(
        "general.use_postprocessing_threshold_movie",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    # External webhook integration
    Validator(
        "general.use_external_webhook", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.external_webhook_url", must_exist=True, default="", is_type_of=str
    ),
    Validator(
        "general.external_webhook_username", must_exist=True, default="", is_type_of=str
    ),
    Validator(
        "general.external_webhook_password", must_exist=True, default="", is_type_of=str
    ),
    Validator("general.use_sonarr", must_exist=True, default=False, is_type_of=bool),
    Validator("general.use_radarr", must_exist=True, default=False, is_type_of=bool),
    Validator("general.use_plex", must_exist=True, default=False, is_type_of=bool),
    Validator("general.use_jellyfin", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "general.path_mappings_movie", must_exist=True, default=[], is_type_of=list
    ),
    Validator(
        "general.serie_tag_enabled", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.movie_tag_enabled", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.remove_profile_tags",
        must_exist=True,
        default=[],
        is_type_of=list,
        condition=validate_tags,
    ),
    Validator(
        "general.serie_default_enabled", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.serie_default_profile",
        must_exist=True,
        default="",
        is_type_of=(int, str),
    ),
    Validator(
        "general.movie_default_enabled", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.movie_default_profile",
        must_exist=True,
        default="",
        is_type_of=(int, str),
    ),
    Validator(
        "general.page_size",
        must_exist=True,
        default=25,
        is_type_of=int,
        is_in=[25, 50, 100, 250, 500, 1000],
    ),
    Validator(
        "general.theme",
        must_exist=True,
        default="auto",
        is_type_of=str,
        is_in=["auto", "light", "dark"],
    ),
    Validator(
        "general.show_live_badge", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "general.minimum_score_movie",
        must_exist=True,
        default=70,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator(
        "general.use_embedded_subs", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "general.embedded_subs_show_desired",
        must_exist=True,
        default=True,
        is_type_of=bool,
    ),
    Validator("general.utf8_encode", must_exist=True, default=True, is_type_of=bool),
    Validator(
        "general.ignore_pgs_subs", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.ignore_vobsub_subs", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.ignore_ass_subs", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.adaptive_searching", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "general.adaptive_searching_delay",
        must_exist=True,
        default="3w",
        is_type_of=str,
        is_in=["1w", "2w", "3w", "4w"],
    ),
    Validator(
        "general.adaptive_searching_delta",
        must_exist=True,
        default="1w",
        is_type_of=str,
        is_in=["3d", "1w", "2w", "3w", "4w"],
    ),
    Validator(
        "general.adaptive_searching_max_age",
        must_exist=True,
        default="",
        is_type_of=str,
        is_in=["", "1m", "3m", "6m", "1y", "2y"],
    ),
    Validator(
        "general.enabled_providers", must_exist=True, default=[], is_type_of=list
    ),
    Validator(
        "general.provider_priorities", must_exist=True, default={}, is_type_of=dict
    ),
    Validator(
        "general.provider_languages", must_exist=True, default={}, is_type_of=dict
    ),
    Validator(
        "general.use_provider_priority", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "general.enabled_integrations", must_exist=True, default=[], is_type_of=list
    ),
    Validator("general.multithreading", must_exist=True, default=True, is_type_of=bool),
    Validator("general.chmod_enabled", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "general.enable_strm_support", must_exist=True, default=False, is_type_of=bool
    ),
    Validator("general.chmod", must_exist=True, default="0640", is_type_of=str),
    Validator("general.subfolder", must_exist=True, default="current", is_type_of=str),
    Validator("general.subfolder_custom", must_exist=True, default="", is_type_of=str),
    Validator(
        "general.use_whisper_fallback", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "general.use_whisper_fallback_series",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator("general.upgrade_subs", must_exist=True, default=True, is_type_of=bool),
    Validator(
        "general.upgrade_frequency",
        must_exist=True,
        default=12,
        is_type_of=int,
        is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS],
    ),
    Validator(
        "general.days_to_upgrade_subs",
        must_exist=True,
        default=7,
        is_type_of=int,
        gte=0,
        lte=30,
    ),
    Validator("general.upgrade_manual", must_exist=True, default=True, is_type_of=bool),
    Validator(
        "general.anti_captcha_provider",
        must_exist=True,
        default=None,
        is_type_of=(NoneType, str),
        is_in=[None, "anti-captcha", "death-by-captcha"],
    ),
    Validator(
        "general.wanted_search_frequency",
        must_exist=True,
        default=6,
        is_type_of=int,
        is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS],
    ),
    Validator(
        "general.wanted_search_frequency_movie",
        must_exist=True,
        default=6,
        is_type_of=int,
        is_in=[6, 12, 24, 168, ONE_HUNDRED_YEARS_IN_HOURS],
    ),
    Validator("general.subzero_mods", must_exist=True, default="", is_type_of=str),
    Validator(
        "general.dont_notify_manual_actions",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "general.notify_if_nothing_is_missing_for_signalr_event",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "general.hi_extension",
        must_exist=True,
        default="hi",
        is_type_of=str,
        is_in=["hi", "cc", "sdh"],
    ),
    Validator(
        "general.embedded_subtitles_parser",
        must_exist=True,
        default="ffprobe",
        is_type_of=str,
        is_in=["ffprobe", "mediainfo"],
        condition=check_parser_binary,
    ),
    Validator(
        "general.default_und_audio_lang", must_exist=True, default="", is_type_of=str
    ),
    Validator(
        "general.default_und_embedded_subtitles_lang",
        must_exist=True,
        default="",
        is_type_of=str,
    ),
    Validator(
        "general.parse_embedded_audio_track",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator("general.skip_hashing", must_exist=True, default=False, is_type_of=bool),
    Validator("general.language_equals", must_exist=True, default=[], is_type_of=list),
    Validator(
        "general.concurrent_jobs",
        must_exist=True,
        default=4 if os.cpu_count() >= 4 else os.cpu_count(),
        is_type_of=int,
    ),
    # log section
    Validator(
        "log.include_filter", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "log.exclude_filter", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator("log.ignore_case", must_exist=True, default=False, is_type_of=bool),
    Validator("log.use_regex", must_exist=True, default=False, is_type_of=bool),
    # auth section
    Validator(
        "auth.apikey",
        must_exist=True,
        default=hexlify(os.urandom(16)).decode(),
        is_type_of=str,
    ),
    Validator(
        "auth.type",
        must_exist=True,
        default=None,
        is_type_of=(NoneType, str),
        is_in=[None, "basic", "form"],
    ),
    Validator("auth.username", must_exist=True, default="", is_type_of=str, cast=str),
    Validator("auth.password", must_exist=True, default="", is_type_of=str, cast=str),
    # cors section
    Validator("cors.enabled", must_exist=True, default=False, is_type_of=bool),
    # backup section
    Validator(
        "backup.folder",
        must_exist=True,
        default=os.path.join(args.config_dir, "backup"),
        is_type_of=str,
    ),
    Validator("backup.retention", must_exist=True, default=31, is_type_of=int, gte=0),
    Validator(
        "backup.frequency",
        must_exist=True,
        default="Weekly",
        is_type_of=str,
        is_in=["Manually", "Daily", "Weekly"],
    ),
    Validator("backup.day", must_exist=True, default=6, is_type_of=int, gte=0, lte=6),
    Validator("backup.hour", must_exist=True, default=3, is_type_of=int, gte=0, lte=23),
    # translating section
    Validator(
        "translator.min_source_score",
        must_exist=True,
        default=90,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator(
        "translator.default_score", must_exist=True, default=50, is_type_of=int, gte=0
    ),
    Validator("translator.gemini_keys", must_exist=True, default=[], is_type_of=list),
    Validator(
        "translator.gemini_model",
        must_exist=True,
        default="gemini-2.0-flash",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "translator.gemini_batch_size",
        must_exist=True,
        default=300,
        is_type_of=int,
        gte=1,
    ),
    Validator(
        "translator.translator_info", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "translator.translator_type",
        must_exist=True,
        default="google_translate",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "translator.lingarr_url",
        must_exist=True,
        default="http://lingarr:9876",
        is_type_of=str,
    ),
    Validator(
        "translator.openrouter_url",
        must_exist=True,
        default="http://subtitle-translator:8765",
        is_type_of=str,
    ),
    Validator(
        "translator.openrouter_api_key",
        must_exist=True,
        default="",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "translator.openrouter_model",
        must_exist=True,
        default="google/gemini-2.5-flash-preview-05-20",
        is_type_of=str,
    ),
    Validator(
        "translator.openrouter_temperature",
        must_exist=True,
        default=0.3,
        is_type_of=float,
    ),
    Validator(
        "translator.openrouter_max_concurrent",
        must_exist=True,
        default=2,
        is_type_of=int,
        gte=1,
        lte=10,
    ),
    Validator(
        "translator.openrouter_reasoning",
        must_exist=True,
        default="disabled",
        is_type_of=str,
        is_in=["disabled", "low", "medium", "high"],
    ),
    Validator(
        "translator.openrouter_parallel_batches",
        must_exist=True,
        default=4,
        is_type_of=int,
        gte=1,
        lte=8,
    ),
    Validator(
        "translator.openrouter_encryption_key",
        must_exist=True,
        default="",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "translator.lingarr_token",
        must_exist=True,
        default="",
        is_type_of=str,
        cast=str,
    ),
    # sonarr section
    Validator("sonarr.ip", must_exist=True, default="127.0.0.1", is_type_of=str),
    Validator(
        "sonarr.port", must_exist=True, default=8989, is_type_of=int, gte=1, lte=65535
    ),
    Validator("sonarr.base_url", must_exist=True, default="/", is_type_of=str),
    Validator("sonarr.ssl", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "sonarr.http_timeout",
        must_exist=True,
        default=60,
        is_type_of=int,
        is_in=[60, 120, 180, 240, 300, 600],
    ),
    Validator("sonarr.apikey", must_exist=True, default="", is_type_of=str),
    Validator(
        "sonarr.full_update",
        must_exist=True,
        default="Daily",
        is_type_of=str,
        is_in=["Manually", "Daily", "Weekly"],
    ),
    Validator(
        "sonarr.full_update_day",
        must_exist=True,
        default=6,
        is_type_of=int,
        gte=0,
        lte=6,
    ),
    Validator(
        "sonarr.full_update_hour",
        must_exist=True,
        default=4,
        is_type_of=int,
        gte=0,
        lte=23,
    ),
    Validator("sonarr.only_monitored", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "sonarr.series_sync_on_live", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "sonarr.series_sync",
        must_exist=True,
        default=60,
        is_type_of=int,
        is_in=[15, 60, 180, 360, 720, 1440, 10080, ONE_HUNDRED_YEARS_IN_MINUTES],
    ),
    Validator(
        "sonarr.excluded_tags",
        must_exist=True,
        default=[],
        is_type_of=list,
        condition=validate_tags,
    ),
    Validator(
        "sonarr.excluded_series_types", must_exist=True, default=[], is_type_of=list
    ),
    Validator(
        "sonarr.use_ffprobe_cache", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "sonarr.exclude_season_zero", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "sonarr.defer_search_signalr", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "sonarr.sync_only_monitored_series",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "sonarr.sync_only_monitored_episodes",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator("sonarr.verify_ssl", must_exist=True, default=False, is_type_of=bool),
    # radarr section
    Validator("radarr.ip", must_exist=True, default="127.0.0.1", is_type_of=str),
    Validator(
        "radarr.port", must_exist=True, default=7878, is_type_of=int, gte=1, lte=65535
    ),
    Validator("radarr.base_url", must_exist=True, default="/", is_type_of=str),
    Validator("radarr.ssl", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "radarr.http_timeout",
        must_exist=True,
        default=60,
        is_type_of=int,
        is_in=[60, 120, 180, 240, 300, 600],
    ),
    Validator("radarr.apikey", must_exist=True, default="", is_type_of=str),
    Validator(
        "radarr.full_update",
        must_exist=True,
        default="Daily",
        is_type_of=str,
        is_in=["Manually", "Daily", "Weekly"],
    ),
    Validator(
        "radarr.full_update_day",
        must_exist=True,
        default=6,
        is_type_of=int,
        gte=0,
        lte=6,
    ),
    Validator(
        "radarr.full_update_hour",
        must_exist=True,
        default=4,
        is_type_of=int,
        gte=0,
        lte=23,
    ),
    Validator("radarr.only_monitored", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "radarr.movies_sync_on_live", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "radarr.movies_sync",
        must_exist=True,
        default=60,
        is_type_of=int,
        is_in=[15, 60, 180, 360, 720, 1440, 10080, ONE_HUNDRED_YEARS_IN_MINUTES],
    ),
    Validator(
        "radarr.excluded_tags",
        must_exist=True,
        default=[],
        is_type_of=list,
        condition=validate_tags,
    ),
    Validator(
        "radarr.use_ffprobe_cache", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "radarr.defer_search_signalr", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "radarr.sync_only_monitored_movies",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator("radarr.verify_ssl", must_exist=True, default=False, is_type_of=bool),
    # plex section
    Validator("plex.ip", must_exist=True, default="127.0.0.1", is_type_of=str),
    Validator(
        "plex.port", must_exist=True, default=32400, is_type_of=int, gte=1, lte=65535
    ),
    Validator("plex.ssl", must_exist=True, default=False, is_type_of=bool),
    Validator("plex.apikey", must_exist=True, default="", is_type_of=str),
    Validator(
        "plex.movie_library", must_exist=True, default=[], is_type_of=(str, list)
    ),
    Validator(
        "plex.series_library", must_exist=True, default=[], is_type_of=(str, list)
    ),
    Validator("plex.movie_library_ids", must_exist=True, default=[], is_type_of=list),
    Validator("plex.series_library_ids", must_exist=True, default=[], is_type_of=list),
    Validator("plex.set_movie_added", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "plex.set_episode_added", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "plex.update_movie_library", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "plex.update_series_library", must_exist=True, default=False, is_type_of=bool
    ),
    # OAuth fields
    Validator("plex.token", must_exist=True, default="", is_type_of=str),
    Validator("plex.username", must_exist=True, default="", is_type_of=str),
    Validator("plex.email", must_exist=True, default="", is_type_of=str),
    Validator("plex.user_id", must_exist=True, default="", is_type_of=(int, str)),
    Validator(
        "plex.auth_method",
        must_exist=True,
        default="apikey",
        is_type_of=str,
        is_in=["apikey", "oauth"],
    ),
    Validator("plex.encryption_key", must_exist=True, default="", is_type_of=str),
    Validator("plex.verify_ssl", must_exist=True, default=False, is_type_of=bool),
    Validator("plex.server_machine_id", must_exist=True, default="", is_type_of=str),
    Validator("plex.server_name", must_exist=True, default="", is_type_of=str),
    Validator("plex.server_url", must_exist=True, default="", is_type_of=str),
    Validator("plex.server_local", must_exist=True, default=False, is_type_of=bool),
    # Migration fields
    Validator(
        "plex.migration_attempted", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "plex.migration_successful", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "plex.migration_timestamp",
        must_exist=True,
        default="",
        is_type_of=(int, float, str),
    ),
    Validator(
        "plex.disable_auto_migration", must_exist=True, default=False, is_type_of=bool
    ),
    Validator("plex.client_identifier", must_exist=True, default="", is_type_of=str),
    # jellyfin section
    Validator("jellyfin.url", must_exist=True, default="", is_type_of=str),
    Validator("jellyfin.apikey", must_exist=True, default="", is_type_of=str),
    Validator("jellyfin.movie_library", must_exist=True, default=[], is_type_of=list),
    Validator("jellyfin.series_library", must_exist=True, default=[], is_type_of=list),
    Validator(
        "jellyfin.movie_library_ids", must_exist=True, default=[], is_type_of=list
    ),
    Validator(
        "jellyfin.series_library_ids", must_exist=True, default=[], is_type_of=list
    ),
    Validator(
        "jellyfin.update_movie_library", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "jellyfin.update_series_library",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "jellyfin.refresh_method",
        must_exist=True,
        default="immediate",
        is_type_of=str,
        is_in=["immediate", "async"],
    ),
    # Default to verifying TLS like sonarr/radarr/plex; users with self-signed
    # homelab certs can flip this off explicitly. Matches feedback_codeql memory.
    Validator("jellyfin.verify_ssl", must_exist=True, default=True, is_type_of=bool),
    # proxy section
    Validator(
        "proxy.type",
        must_exist=True,
        default=None,
        is_type_of=(NoneType, str),
        is_in=[None, "socks5", "socks5h", "http"],
    ),
    Validator("proxy.url", must_exist=True, default="", is_type_of=str),
    Validator("proxy.port", must_exist=True, default="", is_type_of=(str, int)),
    Validator("proxy.username", must_exist=True, default="", is_type_of=str, cast=str),
    Validator("proxy.password", must_exist=True, default="", is_type_of=str, cast=str),
    Validator(
        "proxy.exclude",
        must_exist=True,
        default=["localhost", "127.0.0.1"],
        is_type_of=list,
    ),
    # opensubtitles.org section
    Validator(
        "opensubtitles.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "opensubtitles.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "opensubtitles.use_tag_search", must_exist=True, default=False, is_type_of=bool
    ),
    Validator("opensubtitles.vip", must_exist=True, default=False, is_type_of=bool),
    Validator("opensubtitles.ssl", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "opensubtitles.timeout", must_exist=True, default=15, is_type_of=int, gte=1
    ),
    Validator(
        "opensubtitles.skip_wrong_fps", must_exist=True, default=False, is_type_of=bool
    ),
    # Web scraper mode - always enabled (OpenSubtitles.org login no longer available)
    Validator(
        "opensubtitles.use_web_scraper", must_exist=True, default=True, is_type_of=bool
    ),
    # Scraper URL - can be set via OPENSUBTITLES_SCRAPER_URL environment variable
    Validator(
        "opensubtitles.scraper_service_url",
        must_exist=True,
        default=os.environ.get("OPENSUBTITLES_SCRAPER_URL", "http://localhost:8000"),
        is_type_of=str,
    ),
    # opensubtitles.com section
    Validator(
        "opensubtitlescom.username",
        must_exist=True,
        default="",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "opensubtitlescom.password",
        must_exist=True,
        default="",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "opensubtitlescom.use_hash", must_exist=True, default=True, is_type_of=bool
    ),
    Validator(
        "opensubtitlescom.include_ai_translated",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "opensubtitlescom.include_machine_translated",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    # napiprojekt section
    Validator(
        "napiprojekt.only_authors", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "napiprojekt.only_real_names", must_exist=True, default=False, is_type_of=bool
    ),
    # addic7ed section
    Validator(
        "addic7ed.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "addic7ed.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator("addic7ed.cookies", must_exist=True, default="", is_type_of=str),
    Validator("addic7ed.user_agent", must_exist=True, default="", is_type_of=str),
    Validator("addic7ed.vip", must_exist=True, default=False, is_type_of=bool),
    # animetosho section
    Validator(
        "animetosho.search_threshold",
        must_exist=True,
        default=6,
        is_type_of=int,
        gte=1,
        lte=15,
    ),
    Validator(
        "animetosho.anidb_api_client",
        must_exist=True,
        default="",
        is_type_of=str,
        cast=str,
    ),
    Validator(
        "animetosho.anidb_api_client_ver",
        must_exist=True,
        default=1,
        is_type_of=int,
        gte=1,
        lte=9,
    ),
    # avistaz section
    Validator("avistaz.cookies", must_exist=True, default="", is_type_of=str),
    Validator("avistaz.user_agent", must_exist=True, default="", is_type_of=str),
    # cinemaz section
    Validator("cinemaz.cookies", must_exist=True, default="", is_type_of=str),
    Validator("cinemaz.user_agent", must_exist=True, default="", is_type_of=str),
    # podnapisi section
    Validator("podnapisi.verify_ssl", must_exist=True, default=True, is_type_of=bool),
    # subf2m section
    Validator("subf2m.verify_ssl", must_exist=True, default=True, is_type_of=bool),
    Validator("subf2m.user_agent", must_exist=True, default="", is_type_of=str),
    # hdbits section
    Validator("hdbits.username", must_exist=True, default="", is_type_of=str, cast=str),
    Validator("hdbits.passkey", must_exist=True, default="", is_type_of=str, cast=str),
    # whisperai section
    Validator(
        "whisperai.endpoint",
        must_exist=True,
        default="http://127.0.0.1:9000",
        is_type_of=str,
    ),
    Validator("whisperai.response", must_exist=True, default=5, is_type_of=int, gte=1),
    Validator(
        "whisperai.timeout", must_exist=True, default=3600, is_type_of=int, gte=1
    ),
    Validator(
        "whisperai.pass_video_name", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "whisperai.loglevel",
        must_exist=True,
        default="INFO",
        is_type_of=str,
        is_in=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    ),
    # legendasdivx section
    Validator(
        "legendasdivx.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "legendasdivx.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "legendasdivx.skip_wrong_fps", must_exist=True, default=False, is_type_of=bool
    ),
    # legendasnet section
    Validator(
        "legendasnet.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "legendasnet.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # pipocas section
    Validator(
        "pipocas.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "pipocas.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # ktuvit section
    Validator("ktuvit.email", must_exist=True, default="", is_type_of=str),
    Validator(
        "ktuvit.hashed_password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # xsubs section
    Validator("xsubs.username", must_exist=True, default="", is_type_of=str, cast=str),
    Validator("xsubs.password", must_exist=True, default="", is_type_of=str, cast=str),
    # assrt section
    Validator("assrt.token", must_exist=True, default="", is_type_of=str, cast=str),
    # anticaptcha section
    Validator(
        "anticaptcha.anti_captcha_key", must_exist=True, default="", is_type_of=str
    ),
    # deathbycaptcha section
    Validator(
        "deathbycaptcha.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "deathbycaptcha.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # napisy24 section
    Validator(
        "napisy24.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "napisy24.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # betaseries section
    Validator(
        "betaseries.token", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # jimaku section
    Validator("jimaku.api_key", must_exist=True, default="", is_type_of=str),
    Validator(
        "jimaku.enable_name_search_fallback",
        must_exist=True,
        default=True,
        is_type_of=bool,
    ),
    Validator(
        "jimaku.enable_archives_download",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator("jimaku.enable_ai_subs", must_exist=True, default=False, is_type_of=bool),
    # titlovi section
    Validator(
        "titlovi.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "titlovi.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # titulky section
    Validator(
        "titulky.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "titulky.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator("titulky.approved_only", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "titulky.skip_wrong_fps", must_exist=True, default=False, is_type_of=bool
    ),
    # embeddedsubtitles section
    Validator(
        "embeddedsubtitles.included_codecs",
        must_exist=True,
        default=[],
        is_type_of=list,
    ),
    Validator(
        "embeddedsubtitles.hi_fallback", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "embeddedsubtitles.timeout", must_exist=True, default=600, is_type_of=int, gte=1
    ),
    Validator(
        "embeddedsubtitles.unknown_as_fallback",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "embeddedsubtitles.fallback_lang",
        must_exist=True,
        default="en",
        is_type_of=str,
        cast=str,
    ),
    # karagarga section
    Validator(
        "karagarga.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "karagarga.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "karagarga.f_username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "karagarga.f_password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    # subdl section
    Validator("subdl.api_key", must_exist=True, default="", is_type_of=str, cast=str),
    Validator("subdl.anime_mode", must_exist=True, default=False, is_type_of=bool),
    # turkcealtyaziorg section
    Validator("turkcealtyaziorg.cookies", must_exist=True, default="", is_type_of=str),
    Validator(
        "turkcealtyaziorg.user_agent", must_exist=True, default="", is_type_of=str
    ),
    # subsync section
    Validator("subsync.use_subsync", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "subsync.use_subsync_threshold", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "subsync.subsync_threshold",
        must_exist=True,
        default=90,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator(
        "subsync.use_subsync_movie_threshold",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator(
        "subsync.subsync_movie_threshold",
        must_exist=True,
        default=70,
        is_type_of=int,
        gte=0,
        lte=100,
    ),
    Validator("subsync.debug", must_exist=True, default=False, is_type_of=bool),
    Validator("subsync.force_audio", must_exist=True, default=False, is_type_of=bool),
    Validator(
        "subsync.use_original_language", must_exist=True, default=False, is_type_of=bool
    ),
    Validator(
        "subsync.auto_use_original_language",
        must_exist=True,
        default=False,
        is_type_of=bool,
    ),
    Validator("subsync.checker", must_exist=True, default={}, is_type_of=dict),
    Validator(
        "subsync.checker.blacklisted_providers",
        must_exist=True,
        default=[],
        is_type_of=list,
    ),
    Validator(
        "subsync.checker.blacklisted_languages",
        must_exist=True,
        default=[],
        is_type_of=list,
    ),
    Validator(
        "subsync.no_fix_framerate", must_exist=True, default=True, is_type_of=bool
    ),
    Validator("subsync.gss", must_exist=True, default=True, is_type_of=bool),
    Validator(
        "subsync.max_offset_seconds",
        must_exist=True,
        default=60,
        is_type_of=int,
        is_in=[60, 120, 300, 600],
    ),
    Validator(
        "subsync.enabled_engines",
        must_exist=True,
        default=["ffsubsync", "autosubsync", "alass"],
        is_type_of=list,
    ),
    Validator(
        "subsync.output_mode",
        must_exist=True,
        default="overwrite",
        is_type_of=str,
        is_in=["overwrite", "keep_all"],
    ),
    # postgresql section
    Validator("postgresql.enabled", must_exist=True, default=False, is_type_of=bool),
    Validator("postgresql.host", must_exist=True, default="localhost", is_type_of=str),
    Validator(
        "postgresql.port",
        must_exist=True,
        default=5432,
        is_type_of=int,
        gte=1,
        lte=65535,
    ),
    Validator("postgresql.database", must_exist=True, default="", is_type_of=str),
    Validator(
        "postgresql.username", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator(
        "postgresql.password", must_exist=True, default="", is_type_of=str, cast=str
    ),
    Validator("postgresql.url", must_exist=True, default="", is_type_of=str, cast=str),
    # anidb section
    Validator("anidb.api_client", must_exist=True, default="", is_type_of=str),
    Validator("anidb.api_client_ver", must_exist=True, default=1, is_type_of=int),
    # subsource section
    Validator("subsource.apikey", must_exist=True, default="", is_type_of=str),
    # subsarr section
    Validator("subsarr.base_url", must_exist=True, default="", is_type_of=str),
    # subx section
    Validator("subx.api_key", must_exist=True, default="", is_type_of=str),
    # subsro section
    Validator("subsro.api_key", must_exist=True, default="", is_type_of=str, cast=str),
    # compat_endpoint section
    # NOTE: secret length enforcement happens at blueprint registration via
    # boot_hmac_selftest (see bazarr/compat/auth.py). Do NOT add len_min
    # validators here that fire on the "Save" path -- they would reject the
    # first save that flips enabled=True (secrets still empty at that moment
    # because auto-generation runs at next boot, not at save time).
    Validator("compat_endpoint.enabled", default=False, cast=bool),
    Validator("compat_endpoint.consent", default=False, cast=bool),
    Validator("compat_endpoint.token", default="", cast=str),
    Validator("compat_endpoint.jwt_secret", default="", cast=str),
    Validator("compat_endpoint.file_id_secret", default="", cast=str),
    Validator(
        "compat_endpoint.cache_ttl_seconds", default=1800, cast=int, gte=60, lte=86400
    ),
    Validator(
        "compat_endpoint.cache_ttl_partial_seconds",
        default=300,
        cast=int,
        gte=30,
        lte=3600,
    ),
    Validator(
        "compat_endpoint.search_timeout_seconds", default=20, cast=int, gte=5, lte=120
    ),
    # per_provider_timeout is not a user-facing knob: it's derived as
    # 60% of the wall timeout inside _do_fanout. The log-label threshold
    # should scale with the wall, not be tuned independently.
    # Compat fanout shares one process-wide bounded ThreadPoolExecutor
    # so repeated timed-out searches cannot keep spawning provider
    # threads. fanout_max_workers caps total background provider
    # threads; max_concurrent_fanouts caps simultaneous fanouts so a
    # burst of requests cannot drain the pool with abandoned work.
    Validator(
        "compat_endpoint.fanout_max_workers", default=32, cast=int, gte=4, lte=256
    ),
    Validator(
        "compat_endpoint.max_concurrent_fanouts", default=4, cast=int, gte=1, lte=16
    ),
    Validator(
        "compat_endpoint.file_id_ttl_seconds",
        default=3600,
        cast=int,
        gte=300,
        lte=86400,
    ),
    Validator(
        "compat_endpoint.stream_token_ttl_seconds",
        default=300,
        cast=int,
        gte=60,
        lte=3600,
    ),
    Validator(
        "compat_endpoint.jwt_ttl_seconds", default=86400, cast=int, gte=3600, lte=604800
    ),
    Validator(
        "compat_endpoint.downloads_per_window",
        default=1000,
        cast=int,
        gte=1,
        lte=1000000,
    ),
    Validator(
        "compat_endpoint.downloads_window_seconds",
        default=86400,
        cast=int,
        gte=60,
        lte=2592000,
    ),
    Validator("compat_endpoint.serve_local_subs", default=True, is_type_of=bool),
    # OMDB: optional title/year resolution for movies that aren't in the
    # local library. Free tier at omdbapi.com (1000 req/day). Empty = skip.
    Validator("omdb.apikey", default="", cast=str),
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
    with open(os.path.join(os.path.dirname(config_file), "config.yaml"), "w") as file:
        yaml.dump(output_dict, file)
    os.replace(config_file, f"{config_file}.old")


config_yaml_file = os.path.join(args.config_dir, "config", "config.yaml")
config_ini_file = os.path.join(args.config_dir, "config", "config.ini")
if os.path.exists(config_ini_file) and not os.path.exists(config_yaml_file):
    convert_ini_to_yaml(config_ini_file)
elif not os.path.exists(config_yaml_file):
    if not os.path.isdir(os.path.dirname(config_yaml_file)):
        os.makedirs(os.path.dirname(config_yaml_file))
    open(config_yaml_file, mode="w").close()

if os.path.exists(config_yaml_file):
    os.environ["BAZARR_CONFIGURED"] = "1"

settings = Dynaconf(
    settings_file=config_yaml_file,
    core_loaders=["YAML"],
    apply_default_on_none=True,
)

settings.validators.register(*validators)

failed_validator = True
while failed_validator:
    try:
        settings.validators.validate_all()
        failed_validator = False
    except ValidationError as e:
        current_validator_details = e.details[0][0]
        logging.error(f"Validator failed for {current_validator_details.names[0]}: {e}")  # noqa: G004
        if (
            hasattr(current_validator_details, "default")
            and current_validator_details.default is not empty
        ):
            old_value = settings.get(current_validator_details.names[0], "undefined")
            settings[current_validator_details.names[0]] = (
                current_validator_details.default
            )
            logging.warning(
                f"VALIDATOR RESET: {current_validator_details.names[0]} from '{old_value}' to '{current_validator_details.default}'"
            )  # noqa: G004
        else:
            logging.critical(
                f"Value for {current_validator_details.names[0]} doesn't pass validation and there's no "  # noqa: G004
                f"default value. This issue must be reported to and fixed by the development team. "
                f"Bazarr won't work until it's been fixed."
            )
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

# Legacy Plex encryption (URLSafeSerializer + plex.encryption_key) used a
# different at-rest format with no marker prefix. If we hand its
# ciphertext to decrypt_settings_in_place, the marker check fails and the
# bytes get treated as plaintext - the next save would re-encrypt them
# under the unified key and the user's Plex creds would be unrecoverable.
# Migrate FIRST so the rest of the pipeline only sees plaintext.
migrate_legacy_plex_encryption(settings)

# Detect plaintext credentials BEFORE decrypt_settings_in_place runs - it
# is a passthrough on plaintext, so afterwards the in-memory state and
# the on-disk state match for plaintext fields and write_config's
# comparison would skip the rewrite. Capture the "needs migration" bit
# now and force a write below.
_force_first_save_migration = has_plaintext_secrets_on_disk(settings)

decrypt_settings_in_place(settings)


def write_config():
    # On-disk shape compared in plaintext form: encrypt_secret is non-
    # deterministic (per-payload salt + timestamp), so naive ciphertext
    # comparison would always diff and rewrite config.yaml on every save.
    global _force_first_save_migration
    in_memory_plaintext = {k.lower(): v for k, v in settings.as_dict().items()}
    on_disk_dict = {
        k.lower(): v
        for k, v in Dynaconf(
            settings_file=config_yaml_file,
            core_loaders=["YAML"],
        )
        .as_dict()
        .items()
    }
    on_disk_plaintext = decrypt_settings_dict(on_disk_dict)

    if in_memory_plaintext == on_disk_plaintext and not _force_first_save_migration:
        logging.debug(
            "Nothing changed when comparing to config file. Skipping write to file."
        )
        return

    forced_migration = _force_first_save_migration
    if forced_migration:
        logging.info(
            "secret_store: forcing config rewrite to encrypt plaintext credentials on disk"
        )

    try:
        write(
            settings_path=config_yaml_file + ".tmp",
            settings_data=encrypt_settings_dict(in_memory_plaintext),
            merge=False,
        )
    except Exception as error:
        logging.exception(
            f"Exception raised while trying to save temporary settings file: {error}"
        )  # noqa: G004
    else:
        try:
            move(config_yaml_file + ".tmp", config_yaml_file)
        except Exception as error:
            logging.exception(
                f"Exception raised while trying to overwrite settings file with temporary settings "  # noqa: G004
                f"file: {error}"
            )
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


base_url = settings.general.base_url.rstrip("/")

array_keys = [
    "excluded_tags",
    "exclude",
    "included_codecs",
    "subzero_mods",
    "excluded_series_types",
    "enabled_providers",
    "enabled_integrations",
    "gemini_keys",
    "path_mappings",
    "path_mappings_movie",
    "remove_profile_tags",
    "language_equals",
    "blacklisted_languages",
    "blacklisted_providers",
    "movie_library",
    "series_library",
    "movie_library_ids",
    "series_library_ids",
]

empty_values = ["", "None", "null", "undefined", None, []]

str_keys = [
    "chmod",
    "log_include_filter",
    "log_exclude_filter",
    "password",
    "f_password",
    "hashed_password",
]

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
if hasattr(settings.embeddedsubtitles, "unknown_as_english"):
    if settings.embeddedsubtitles.unknown_as_english:
        settings.embeddedsubtitles.unknown_as_fallback = True
        settings.embeddedsubtitles.fallback_lang = "en"
    del settings.embeddedsubtitles.unknown_as_english

# delete custom scores sections since we don't use this anymore
if hasattr(settings, "series_scores"):
    settings.unset("SERIES_SCORES")
if hasattr(settings, "movie_scores"):
    settings.unset("MOVIE_SCORES")

# backward compatibility: migrate gemini_key to gemini_keys
if hasattr(settings.translator, "gemini_key"):
    legacy_key = str(settings.translator.gemini_key).strip()
    if legacy_key and not settings.translator.gemini_keys:
        settings.translator.gemini_keys = [legacy_key]
    del settings.translator.gemini_key

# save updated settings to file
write_config()


def get_settings():
    # API serializer for /api/system/settings. SYSTEM_SECRETS are masked
    # with '***' (key still present so the wire shape is stable, value
    # hidden); USER_VISIBLE_SECRETS pass through unchanged because the
    # in-memory settings already hold their decrypted plaintext.
    from secret_store import is_system_secret  # noqa: PLC0415, RUF100

    settings_to_return = {}
    for k, v in settings.as_dict().items():
        if isinstance(v, dict):
            k = k.lower()
            settings_to_return[k] = dict()
            for subk, subv in v.items():
                full_path = f"{k}.{subk.lower()}"
                if is_system_secret(full_path):
                    # Keep empty values literally empty so the UI can
                    # distinguish "not configured" from "configured but
                    # hidden". Non-empty system secrets get the mask.
                    settings_to_return[k][subk] = "***" if subv else subv
                    continue
                if subv in empty_values and subk.lower() in array_keys:
                    settings_to_return[k].update({subk: []})
                elif subk == "subzero_mods":
                    settings_to_return[k].update({subk: get_array_from(subv)})
                else:
                    settings_to_return[k].update({subk: subv})
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
            raise ValidationError(
                f"Include filter: invalid regular expression: {settings.log.include_filter}"
            )
        try:
            re.compile(settings.log.exclude_filter)
        except Exception:
            raise ValidationError(
                f"Exclude filter: invalid regular expression: {settings.log.exclude_filter}"
            )


def _settings_mapping(parent, key):
    try:
        mapping = parent[key]
    except KeyError:
        parent[key] = {}
        mapping = parent[key]
    if mapping is None:
        parent[key] = {}
        mapping = parent[key]
    return mapping


def save_settings(settings_items):
    configure_debug = False
    configure_captcha = False
    update_schedule = False
    sonarr_changed = False
    radarr_changed = False
    update_path_map = False
    configure_proxy = False
    exclusion_updated = False
    sonarr_exclusion_updated = False
    radarr_exclusion_updated = False
    use_embedded_subs_changed = False
    undefined_audio_track_default_changed = False
    undefined_subtitles_track_default_changed = False
    audio_tracks_parsing_changed = False
    adaptive_searching_max_age_changed = False
    reset_providers = False
    reset_fanout_pool = False
    reset_compat_pool = False

    # Subzero Mods
    update_subzero = False
    subzero_mods = get_array_from(settings.general.subzero_mods)

    if len(subzero_mods) == 1 and subzero_mods[0] == "":
        subzero_mods = []

    for key, value in settings_items:
        settings_keys = key.split("-")

        # Make sure that text based form values aren't passed as list
        if (
            isinstance(value, list)
            and len(value) == 1
            and settings_keys[-1] not in array_keys
        ):
            value = value[0]
            if value in empty_values and value != "":
                value = None

        # try to cast string as integer or float
        if isinstance(value, str) and settings_keys[-1] not in str_keys:
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass

        # Make sure empty language list are stored correctly
        if settings_keys[-1] in array_keys and value[0] in empty_values:
            value = []

        # Handle path mappings settings since they are array in array
        if settings_keys[-1] in ["path_mappings", "path_mappings_movie"]:
            value = [x.split(",") for x in value if isinstance(x, str)]

        if value == "true":
            value = True
        elif value == "false":
            value = False

        # Handle JSON strings for dict settings
        if settings_keys[-1] in [
            "provider_priorities",
            "provider_languages",
        ] and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass

        if key in [
            "settings-general-use_embedded_subs",
            "settings-general-ignore_pgs_subs",
            "settings-general-ignore_vobsub_subs",
            "settings-general-ignore_ass_subs",
        ]:
            use_embedded_subs_changed = True

        if key == "settings-general-adaptive_searching_max_age":
            if value != settings.general.adaptive_searching_max_age:
                adaptive_searching_max_age_changed = True

        if key == "settings-general-default_und_audio_lang":
            undefined_audio_track_default_changed = True

        if key == "settings-general-parse_embedded_audio_track":
            audio_tracks_parsing_changed = True

        if key == "settings-general-default_und_embedded_subtitles_lang":
            undefined_subtitles_track_default_changed = True

        if key in [
            "settings-general-base_url",
            "settings-sonarr-base_url",
            "settings-radarr-base_url",
        ]:
            value = base_url_slash_cleaner(value)

        if key == "settings-general-instance_name" and value == "":
            value = None

        if key == "settings-auth-password":
            if value != settings.auth.password and value is not None:
                from utilities.helper import hash_password

                value = hash_password(value)

        if key == "settings-general-debug":
            configure_debug = True

        if key == "settings-general-hi_extension":
            os.environ["SZ_HI_EXTENSION"] = value or ""

        if key in [
            "settings-general-anti_captcha_provider",
            "settings-anticaptcha-anti_captcha_key",
            "settings-deathbycaptcha-username",
            "settings-deathbycaptcha-password",
        ]:
            configure_captcha = True

        if key in [
            "update_schedule",
            "settings-general-use_sonarr",
            "settings-general-use_radarr",
            "settings-general-auto_update",
            "settings-general-upgrade_subs",
            "settings-sonarr-series_sync",
            "settings-radarr-movies_sync",
            "settings-sonarr-full_update",
            "settings-sonarr-full_update_day",
            "settings-sonarr-full_update_hour",
            "settings-radarr-full_update",
            "settings-radarr-full_update_day",
            "settings-radarr-full_update_hour",
            "settings-general-wanted_search_frequency",
            "settings-general-wanted_search_frequency_movie",
            "settings-general-upgrade_frequency",
            "settings-backup-frequency",
            "settings-backup-day",
            "settings-backup-hour",
        ]:
            update_schedule = True

        if key in [
            "settings-general-use_sonarr",
            "settings-sonarr-ip",
            "settings-sonarr-port",
            "settings-sonarr-base_url",
            "settings-sonarr-ssl",
            "settings-sonarr-apikey",
        ]:
            sonarr_changed = True

        if key in [
            "settings-general-use_radarr",
            "settings-radarr-ip",
            "settings-radarr-port",
            "settings-radarr-base_url",
            "settings-radarr-ssl",
            "settings-radarr-apikey",
        ]:
            radarr_changed = True

        if key in [
            "settings-general-path_mappings",
            "settings-general-path_mappings_movie",
        ]:
            update_path_map = True

        if key in [
            "settings-proxy-type",
            "settings-proxy-url",
            "settings-proxy-port",
            "settings-proxy-username",
            "settings-proxy-password",
        ]:
            configure_proxy = True

        if key in [
            "settings-sonarr-excluded_tags",
            "settings-sonarr-only_monitored",
            "settings-sonarr-excluded_series_types",
            "settings-sonarr-exclude_season_zero",
            "settings-radarr-excluded_tags",
            "settings-radarr-only_monitored",
        ]:
            exclusion_updated = True

        if key in [
            "settings-sonarr-excluded_tags",
            "settings-sonarr-only_monitored",
            "settings-sonarr-excluded_series_types",
            "settings-sonarr-exclude_season_zero",
        ]:
            sonarr_exclusion_updated = True

        if key in ["settings-radarr-excluded_tags", "settings-radarr-only_monitored"]:
            radarr_exclusion_updated = True

        if key == "settings-addic7ed-username":
            if value != settings.addic7ed.username:
                reset_providers = True
                region.delete("addic7ed_data")
        elif key == "settings-addic7ed-password":
            if value != settings.addic7ed.password:
                reset_providers = True
                region.delete("addic7ed_data")

        if key == "settings-legendasdivx-username":
            if value != settings.legendasdivx.username:
                reset_providers = True
                region.delete("legendasdivx_cookies2")
        elif key == "settings-legendasdivx-password":
            if value != settings.legendasdivx.password:
                reset_providers = True
                region.delete("legendasdivx_cookies2")

        if key == "settings-opensubtitles-username":
            if key != settings.opensubtitles.username:
                reset_providers = True
                region.delete("os_token")
        elif key == "settings-opensubtitles-password":
            if key != settings.opensubtitles.password:
                reset_providers = True
                region.delete("os_token")
        elif key == "settings-opensubtitles-use_web_scraper":
            if key != settings.opensubtitles.use_web_scraper:
                reset_providers = True
                region.delete("os_token")  # Clear any cached tokens
        elif key == "settings-opensubtitles-scraper_service_url":
            if key != settings.opensubtitles.scraper_service_url:
                reset_providers = True

        if key == "settings-opensubtitlescom-username":
            if value != settings.opensubtitlescom.username:
                reset_providers = True
                region.delete("oscom_token")
        elif key == "settings-opensubtitlescom-password":
            if value != settings.opensubtitlescom.password:
                reset_providers = True
                region.delete("oscom_token")

        if key == "settings-titlovi-username":
            if value != settings.titlovi.username:
                reset_providers = True
                region.delete("titlovi_token")
        elif key == "settings-titlovi-password":
            if value != settings.titlovi.password:
                reset_providers = True
                region.delete("titlovi_token")

        if key == "settings-subsource-apikey":
            if value != settings.subsource.apikey:
                reset_providers = True

        if key in (
            "settings-general-enabled_providers",
            "settings-general-provider_languages",
        ):
            # Defer the reset until AFTER all values in this batch are
            # written, same reasoning as reset_fanout_pool below.
            # provider_languages changes affect per-provider exclusions
            # which alter compat cache keys and provider pool behavior.
            reset_compat_pool = True

        if key in (
            "settings-compat_endpoint-fanout_max_workers",
            "settings-compat_endpoint-max_concurrent_fanouts",
        ):
            # Defer the reset until AFTER all values in this batch are
            # written. Resetting in-loop opens a window where a
            # concurrent compat request could re-init the shared
            # executor by reading the OLD value before the assignment
            # at the bottom of the loop body lands.
            reset_fanout_pool = True

        if reset_providers:
            from .get_providers import reset_throttled_providers

            reset_throttled_providers(only_auth_or_conf_error=True)
            # Defer the compat-pool reset for the same race reason as
            # the fanout pool above. Resetting here, before the
            # settings[...] = value assignment lands, would let a
            # concurrent /compat request re-init the provider pool
            # with stale provider settings or credentials.
            reset_compat_pool = True

        if settings_keys[0] == "settings":
            if len(settings_keys) == 3:
                _settings_mapping(settings, settings_keys[1])[settings_keys[2]] = value
            elif len(settings_keys) == 4:
                section = _settings_mapping(settings, settings_keys[1])
                _settings_mapping(section, settings_keys[2])[settings_keys[3]] = value

        if settings_keys[0] == "subzero":
            mod = settings_keys[1]
            if mod in subzero_mods and not value:
                subzero_mods.remove(mod)
            elif value:
                subzero_mods.append(mod)

            # Handle color
            if mod == "color":
                previous = None
                for exist_mod in subzero_mods:
                    if exist_mod.startswith("color"):
                        previous = exist_mod
                        break
                if previous is not None:
                    subzero_mods.remove(previous)
                if value not in empty_values:
                    subzero_mods.append(value)

            update_subzero = True

    if (
        use_embedded_subs_changed
        or undefined_audio_track_default_changed
        or adaptive_searching_max_age_changed
    ):
        from .scheduler import scheduler
        from subtitles.indexer.series import list_missing_subtitles
        from subtitles.indexer.movies import list_missing_subtitles_movies

        if settings.general.use_sonarr:
            list_missing_subtitles()
        if settings.general.use_radarr:
            list_missing_subtitles_movies()

    if undefined_subtitles_track_default_changed:
        from .scheduler import scheduler
        from subtitles.indexer.series import series_full_scan_subtitles
        from subtitles.indexer.movies import movies_full_scan_subtitles

        if settings.general.use_sonarr:
            series_full_scan_subtitles(use_cache=True)
        if settings.general.use_radarr:
            movies_full_scan_subtitles(use_cache=True)

    if audio_tracks_parsing_changed:
        from .scheduler import scheduler

        if settings.general.use_sonarr:
            from sonarr.sync.series import update_series

            update_series()
        if settings.general.use_radarr:
            from radarr.sync.movies import update_movies

            update_movies()

    if update_subzero:
        settings.general.subzero_mods = ",".join(subzero_mods)

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

    try:
        settings.validators.validate()
        validate_log_regex()
    except ValidationError:
        # Re-decrypt after reload: settings.reload() pulls the on-disk
        # ciphertext back into the live Dynaconf object, so without this
        # second pass downstream code would see `enc:v1:` strings for
        # API keys, auth credentials, provider passwords, and compat
        # tokens until the next process restart. Codex P1.
        settings.reload()
        migrate_legacy_plex_encryption(settings)
        decrypt_settings_in_place(settings)
        raise
    else:
        write_config()

        # Set the configured state based on config.yaml file existence
        from .database import database, update, System

        database.execute(update(System).values(configured=1))

        # Reconfigure Bazarr to reflect changes
        if configure_debug:
            from .logger import configure_logging

            configure_logging(settings.general.debug or args.debug)

        if configure_captcha:
            configure_captcha_func()

        if update_schedule:
            from .scheduler import scheduler
            from .event_handler import event_stream

            scheduler.update_configurable_tasks()
            event_stream(type="task")

        if sonarr_changed:
            from .signalr_client import sonarr_signalr_client

            try:
                sonarr_signalr_client.restart()
            except Exception:
                pass

        if radarr_changed:
            from .signalr_client import radarr_signalr_client

            try:
                radarr_signalr_client.restart()
            except Exception:
                pass

        if update_path_map:
            from utilities.path_mappings import path_mappings

            path_mappings.update()

        if configure_proxy:
            configure_proxy_func()

        if exclusion_updated:
            from .event_handler import event_stream

            event_stream(type="badges")
            if sonarr_exclusion_updated:
                event_stream(type="reset-episode-wanted")
            if radarr_exclusion_updated:
                event_stream(type="reset-movie-wanted")


def get_array_from(property):
    if property:
        if "[" in property:
            return ast.literal_eval(property)
        elif "," in property:
            return property.split(",")
        else:
            return [property]
    else:
        return []


def configure_captcha_func():
    # set anti-captcha provider and key
    if (
        settings.general.anti_captcha_provider == "anti-captcha"
        and settings.anticaptcha.anti_captcha_key != ""
    ):
        os.environ["ANTICAPTCHA_CLASS"] = "AntiCaptchaProxyLess"
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(
            settings.anticaptcha.anti_captcha_key
        )
    elif (
        settings.general.anti_captcha_provider == "death-by-captcha"
        and settings.deathbycaptcha.username != ""
        and settings.deathbycaptcha.password != ""
    ):
        os.environ["ANTICAPTCHA_CLASS"] = "DeathByCaptchaProxyLess"
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(
            ":".join(
                {settings.deathbycaptcha.username, settings.deathbycaptcha.password}
            )
        )
    else:
        os.environ["ANTICAPTCHA_CLASS"] = ""


def configure_proxy_func():
    if settings.proxy.type:
        if settings.proxy.username != "" and settings.proxy.password != "":
            proxy = (
                f"{settings.proxy.type}://{quote_plus(settings.proxy.username)}:"
                f"{quote_plus(settings.proxy.password)}@{settings.proxy.url}:{settings.proxy.port}"
            )
        else:
            proxy = (
                f"{settings.proxy.type}://{settings.proxy.url}:{settings.proxy.port}"
            )
        os.environ["HTTP_PROXY"] = str(proxy)
        os.environ["HTTPS_PROXY"] = str(proxy)
        exclude = ",".join(settings.proxy.exclude)
        os.environ["NO_PROXY"] = exclude


_SSL_VERIFY_SERVICES = frozenset({"sonarr", "radarr", "plex"})


def get_ssl_verify(service):
    """Return the verify parameter for requests calls to a service."""
    if service not in _SSL_VERIFY_SERVICES:
        raise ValueError(f"Unknown service for SSL verify: {service}")
    return settings.get(f"{service}.verify_ssl", False)


def sync_checker(subtitle):
    "This function can be extended with settings. It only takes a Subtitle argument"

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
def migrate_plex_config():
    # Generate encryption key if not exists or is empty
    existing_key = settings.plex.get("encryption_key")
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

        auth_method = settings.plex.get("auth_method", "apikey")
        api_key = settings.plex.get("apikey", "")

        # Only migrate if:
        # 1. Currently using API key method
        # 2. Has an API key configured (not empty/None)
        # 3. Plex is actually enabled in general settings
        if not settings.general.get("use_plex", False):
            return

        if auth_method != "apikey" or not api_key or api_key.strip() == "":
            return

        # Check if already migrated (has OAuth token)
        if settings.plex.get("token"):
            logging.debug("OAuth token already exists, skipping migration")
            return

        # We have determined a migration is needed, now log and proceed
        logging.info(
            "OAuth migration - user has API key configuration that needs upgrading"
        )

        # Check if migration is disabled (for emergency rollback)
        if settings.plex.get("disable_auto_migration", False):
            logging.info("auto-migration disabled, skipping")
            return

        # Create backup of current configuration
        backup_config = {
            "auth_method": auth_method,
            "apikey": api_key,
            "apikey_encrypted": settings.plex.get("apikey_encrypted", False),
            "ip": settings.plex.get("ip", "127.0.0.1"),
            "port": settings.plex.get("port", 32400),
            "ssl": settings.plex.get("ssl", False),
            "migration_attempted": True,
            "migration_timestamp": datetime.now().isoformat() + "_backup",
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

        encryption_key = get_or_create_encryption_key(settings.plex, "encryption_key")
        token_manager = TokenManager(encryption_key)

        # Handle both encrypted and plain text API keys
        try:
            if settings.plex.get("apikey_encrypted", False):
                decrypted_api_key = token_manager.decrypt(api_key)
            else:
                decrypted_api_key = api_key
        except Exception as e:
            logging.error(f"Failed to decrypt API key for migration: {e}")  # noqa: G004
            return

        # Use API key to fetch user data from Plex with retry logic
        import requests

        headers = {"X-Plex-Token": decrypted_api_key, "Accept": "application/json"}

        # Get user account info with retries
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                user_response = requests.get(
                    "https://plex.tv/api/v2/user", headers=headers, timeout=10
                )

                if user_response.status_code == 429:  # Rate limited
                    logging.warning(
                        f"Rate limited by Plex API, attempt {attempt + 1}/{max_retries}"
                    )  # noqa: G004
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                        continue
                    else:
                        logging.error(
                            "Migration failed due to rate limiting, will retry later"
                        )
                        return

                user_response.raise_for_status()
                user_data = user_response.json()

                username = user_data.get("username", "")
                email = user_data.get("email", "")
                user_id = str(user_data.get("id", ""))
                break

            except requests.exceptions.Timeout:
                logging.warning(
                    f"Timeout getting user data, attempt {attempt + 1}/{max_retries}"
                )  # noqa: G004
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
                servers_response = requests.get(
                    "https://plex.tv/pms/resources",
                    headers=headers,
                    params={"includeHttps": "1", "includeRelay": "1"},
                    timeout=10,
                )

                if servers_response.status_code == 429:  # Rate limited
                    logging.warning(
                        f"Rate limited getting servers, attempt {attempt + 1}/{max_retries}"
                    )  # noqa: G004
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (attempt + 1))
                        continue
                    else:
                        logging.error(
                            "Migration failed due to rate limiting, will retry later"
                        )
                        return

                servers_response.raise_for_status()

                # Parse response - could be JSON or XML
                content_type = servers_response.headers.get("content-type", "")
                servers = []

                if "application/json" in content_type:
                    resources_data = servers_response.json()
                    for device in resources_data:
                        if (
                            isinstance(device, dict)
                            and device.get("provides") == "server"
                            and device.get("owned")
                        ):
                            server = {
                                "name": device.get("name", ""),
                                "machineIdentifier": device.get("clientIdentifier", ""),
                                "connections": [],
                            }

                            for conn in device.get("connections", []):
                                server["connections"].append(
                                    {
                                        "uri": conn.get("uri", ""),
                                        "local": conn.get("local", False),
                                    }
                                )

                            servers.append(server)

                elif "application/xml" in content_type or "text/xml" in content_type:
                    # Parse XML response
                    import xml.etree.ElementTree as ET

                    root = ET.fromstring(servers_response.text)

                    for device in root.findall("Device"):
                        if (
                            device.get("provides") == "server"
                            and device.get("owned") == "1"
                        ):
                            server = {
                                "name": device.get("name", ""),
                                "machineIdentifier": device.get("clientIdentifier", ""),
                                "connections": [],
                            }

                            # Get connections directly from the XML
                            for conn in device.findall("Connection"):
                                server["connections"].append(
                                    {
                                        "uri": conn.get("uri", ""),
                                        "local": conn.get("local") == "1",
                                    }
                                )

                            servers.append(server)
                else:
                    logging.error(f"Unexpected response format: {content_type}")  # noqa: G004
                    return

                break

            except requests.exceptions.Timeout:
                logging.warning(
                    f"Timeout getting servers, attempt {attempt + 1}/{max_retries}"
                )  # noqa: G004
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
        current_ip = settings.plex.get("ip", "127.0.0.1")
        current_port = settings.plex.get("port", 32400)
        current_ssl = settings.plex.get("ssl", False)
        current_url = (
            f"{'https' if current_ssl else 'http'}://{current_ip}:{current_port}"
        )

        selected_server = None
        selected_connection = None

        # Try to match current server configuration
        for server in servers:
            for connection in server["connections"]:
                if connection["uri"] == current_url:
                    selected_server = server
                    selected_connection = connection
                    break
            if selected_server:
                break

        # If no exact match, try to find the first available local server
        if not selected_server and servers:
            for server in servers:
                for connection in server["connections"]:
                    if connection.get("local", False):
                        selected_server = server
                        selected_connection = connection
                        break
                if selected_server:
                    break

        # If still no match, use the first server
        if not selected_server and servers:
            selected_server = servers[0]
            if selected_server["connections"]:
                selected_connection = selected_server["connections"][0]

        if not selected_server or not selected_connection:
            logging.warning("No suitable Plex server found for migration")
            return

        # Encrypt the API key as OAuth token (they're the same thing)
        encrypted_token = token_manager.encrypt(decrypted_api_key)

        # Validate OAuth configuration BEFORE making any changes
        oauth_config = {
            "auth_method": "oauth",
            "token": encrypted_token,
            "username": username,
            "email": email,
            "user_id": user_id,
            "server_machine_id": selected_server["machineIdentifier"],
            "server_name": selected_server["name"],
            "server_url": selected_connection["uri"],
            "server_local": selected_connection.get("local", False),
        }

        # Test OAuth configuration before committing
        logging.info("Testing OAuth configuration before applying changes...")
        test_success = False

        try:
            # Temporarily apply OAuth settings in memory only
            original_auth_method = settings.plex.auth_method
            original_token = settings.plex.token

            settings.plex.auth_method = oauth_config["auth_method"]
            settings.plex.token = oauth_config["token"]
            settings.plex.server_machine_id = oauth_config["server_machine_id"]
            settings.plex.server_name = oauth_config["server_name"]
            settings.plex.server_url = oauth_config["server_url"]
            settings.plex.server_local = oauth_config["server_local"]

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

        logging.info(
            "OAuth configuration validated successfully, proceeding with migration"
        )

        # Now safely apply the OAuth configuration
        settings.plex.auth_method = oauth_config["auth_method"]
        settings.plex.token = oauth_config["token"]
        settings.plex.username = oauth_config["username"]
        settings.plex.email = oauth_config["email"]
        settings.plex.user_id = oauth_config["user_id"]
        settings.plex.server_machine_id = oauth_config["server_machine_id"]
        settings.plex.server_name = oauth_config["server_name"]
        settings.plex.server_url = oauth_config["server_url"]
        settings.plex.server_local = oauth_config["server_local"]

        # Mark migration as successful and disable auto-migration
        settings.plex.migration_successful = True
        # Create human-readable timestamp: YYYYMMDD_HHMMSS_randomstring
        random_suffix = secrets.token_hex(4)  # 8 character random string
        settings.plex.migration_timestamp = (
            f"{datetime.now().isoformat()}_{random_suffix}"
        )
        settings.plex.disable_auto_migration = True

        # Clean up legacy manual configuration fields (no longer needed with OAuth)
        settings.plex.ip = ""
        settings.plex.port = 32400  # Reset to default
        settings.plex.ssl = False  # Reset to default

        # Save configuration with OAuth settings
        write_config()

        logging.info(f"Migrated Plex configuration to OAuth for user '{username}'")  # noqa: G004
        logging.info(
            f"Selected server: {selected_server['name']} ({selected_connection['uri']})"
        )  # noqa: G004
        logging.info("Legacy manual configuration fields cleared (ip, port, ssl)")

        # Final validation test
        try:
            test_server = get_plex_server()
            test_server.account()  # Test connection
            logging.info("Migration validated - OAuth connection successful")

            # Only now permanently remove API key
            settings.plex.apikey = ""
            settings.plex.apikey_encrypted = False
            write_config()
            logging.info(
                "Legacy API key permanently removed after successful OAuth migration"
            )

        except Exception as e:
            logging.error(f"Final OAuth validation failed: {e}")  # noqa: G004

            # Restore backup configuration
            logging.info("Restoring backup configuration...")
            settings.plex.auth_method = backup_config["auth_method"]
            settings.plex.apikey = backup_config["apikey"]
            settings.plex.apikey_encrypted = backup_config["apikey_encrypted"]
            settings.plex.ip = backup_config["ip"]
            settings.plex.port = backup_config["port"]
            settings.plex.ssl = backup_config["ssl"]

            # Clear OAuth settings and restore legacy manual config
            settings.plex.token = ""
            settings.plex.username = ""
            settings.plex.email = ""
            settings.plex.user_id = ""
            settings.plex.server_machine_id = ""
            settings.plex.server_name = ""
            settings.plex.server_url = ""
            settings.plex.server_local = False
            settings.plex.migration_successful = False
            settings.plex.disable_auto_migration = False  # Allow retry

            write_config()

            # Test the rollback
            try:
                test_server = get_plex_server()
                test_server.account()  # Test connection with legacy settings
                logging.info("Rollback successful - legacy API key connection restored")
                logging.error(
                    "OAuth migration failed but legacy configuration is working. Please configure OAuth manually through the GUI."
                )
            except Exception as rollback_error:
                logging.error(f"Rollback validation also failed: {rollback_error}")  # noqa: G004
                logging.error(
                    "CRITICAL: Manual intervention required. Please reset Plex settings."
                )

    except Exception as e:
        logging.error(f"Unexpected error during Plex OAuth migration: {e}")  # noqa: G004
        # Keep existing configuration intact


def cleanup_legacy_oauth_config():
    """
    Clean up legacy manual configuration fields when using OAuth.
    These fields (ip, port, ssl) are not used with OAuth since server_url contains everything.
    """
    if settings.plex.get("auth_method") != "oauth":
        return

    # Check if any legacy values exist
    has_legacy_ip = bool(settings.plex.get("ip", "").strip())
    has_legacy_ssl = settings.plex.get("ssl", False) == True  # noqa: E712
    has_legacy_port = settings.plex.get("port", 32400) != 32400

    # Only disable auto-migration if migration was actually successful
    migration_successful = settings.plex.get("migration_successful", False)
    auto_migration_enabled = not settings.plex.get("disable_auto_migration", False)
    should_disable_auto_migration = migration_successful and auto_migration_enabled

    if (
        has_legacy_ip
        or has_legacy_ssl
        or has_legacy_port
        or should_disable_auto_migration
    ):
        logging.info("Cleaning up OAuth configuration")

        # Clear legacy manual config fields (not needed with OAuth)
        if has_legacy_ip or has_legacy_ssl or has_legacy_port:
            settings.plex.ip = ""
            settings.plex.port = 32400  # Reset to default
            settings.plex.ssl = False  # Reset to default
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
            logging.info(
                f"Migrated plex.movie_library from string to list: {old_value}"
            )  # noqa: G004
            changed = True
        else:
            settings.plex.movie_library = []
            changed = True

    # Migrate series library
    if isinstance(settings.plex.series_library, str):
        old_value = settings.plex.series_library
        if old_value:  # Only migrate if not empty
            settings.plex.series_library = [old_value]
            logging.info(
                f"Migrated plex.series_library from string to list: {old_value}"
            )  # noqa: G004
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
    if settings.general.use_plex and settings.plex.get("auth_method") == "oauth":
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
