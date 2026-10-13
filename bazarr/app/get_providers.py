# coding=utf-8

import math
import os
import json
import datetime
import logging
import subliminal_patch
import time
import socket
import requests
import threading
import traceback
import re

from zoneinfo import ZoneInfo
from requests import ConnectionError
from subzero.language import Language
from subliminal_patch.exceptions import (TooManyRequests, APIThrottled, ParseResponseError, IPAddressBlocked,
                                         MustGetBlacklisted, SearchLimitReached, ProviderError, ForbiddenError)
from subliminal.exceptions import DownloadLimitExceeded, ServiceUnavailable, AuthenticationError, ConfigurationError
from subliminal import region as subliminal_cache_region
from subliminal_patch.extensions import provider_registry
from subliminal_patch.score import ComputeScore

from app.get_args import args
from app.config import settings
from languages.get_languages import CustomLanguage
from app.event_handler import event_stream
from utilities.binaries import get_binary
from utilities.pretty_date import pretty_date
from radarr.blacklist import blacklist_log_movie
from sonarr.blacklist import blacklist_log

_TRACEBACK_RE = re.compile(r'File "(.*?providers[\\/].*?)", line (\d+)')
# The same identifier rule the search-failure path applies to the same field:
# a plugin supplies the remote class name, and the file formatter does not
# escape newlines, so a name that is not an identifier would split the one line
# across physical lines and let a forged record masquerade as a real one.
_REMOTE_CLASS_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_PROVIDER_HUB_REGISTRATION_DONE = False
# Plugin ids already reported for claiming a Bazarr settings section. The overlay
# below runs on every search and download, so without this the same install
# writes the same line for the life of the process.
_REPORTED_RESERVED_SECTION_PLUGINS = set()


def _decrypt_blocked_provider_ids():
    """Provider ids whose stored credentials will not decrypt.

    Carried through configuration, selection and admission, so a provider
    left without a usable credential is left out of searches completely
    instead of only out of the runtime config: without the selection and
    admission legs the pool rebuilt its config from the installation row
    and queried it anyway. Unavailable answers empty, because the pool has
    to be built either way and runtime_provider_configs already refuses
    the blocked providers their config."""
    try:
        from provider_hub.service import decrypt_blocked_provider_ids
        return decrypt_blocked_provider_ids()
    except Exception:
        logging.exception("Unable to load the Provider Hub decrypt-blocked providers")
        return set()


def _ensure_provider_hub_registered():
    global _PROVIDER_HUB_REGISTRATION_DONE
    if _PROVIDER_HUB_REGISTRATION_DONE:
        return

    try:
        from provider_hub.registry import register_active_provider_classes
        register_active_provider_classes()
    except Exception:
        logging.exception("Unable to register active Provider Hub providers")
    else:
        _PROVIDER_HUB_REGISTRATION_DONE = True


def time_until_midnight(timezone) -> datetime.timedelta:
    """
    Get timedelta until midnight.
    """
    now_in_tz = datetime.datetime.now(tz=timezone)
    midnight = now_in_tz.replace(hour=0, minute=0, second=0, microsecond=0) + \
               datetime.timedelta(days=1)
    return midnight - now_in_tz


# Titulky resets its download limits at the start of a new day from its perspective - the Europe/Prague timezone
# Needs to convert to offset-naive dt
def titulky_limit_reset_timedelta():
    return time_until_midnight(timezone=ZoneInfo('Europe/Prague'))


# LegendasDivx reset its searches limit at approximately midnight, Lisbon time, every day. We wait 1 more hours just
# to be sure.
def legendasdivx_limit_reset_timedelta():
    return time_until_midnight(timezone=ZoneInfo('Europe/Lisbon')) + datetime.timedelta(minutes=60)


VALID_THROTTLE_EXCEPTIONS = (TooManyRequests, DownloadLimitExceeded, ServiceUnavailable, APIThrottled,
                             ParseResponseError, IPAddressBlocked)
VALID_COUNT_EXCEPTIONS = ('TooManyRequests', 'ServiceUnavailable', 'APIThrottled', requests.exceptions.Timeout,
                          requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, socket.timeout)


def provider_throttle_map():
    return {
        "default": {
            TooManyRequests: (datetime.timedelta(hours=1), "1 hour"),
            DownloadLimitExceeded: (datetime.timedelta(hours=3), "3 hours"),
            ServiceUnavailable: (datetime.timedelta(minutes=20), "20 minutes"),
            APIThrottled: (datetime.timedelta(minutes=10), "10 minutes"),
            ParseResponseError: (datetime.timedelta(hours=6), "6 hours"),
            requests.exceptions.Timeout: (datetime.timedelta(hours=1), "1 hour"),
            socket.timeout: (datetime.timedelta(hours=1), "1 hour"),
            requests.exceptions.ConnectTimeout: (datetime.timedelta(hours=1), "1 hour"),
            requests.exceptions.ReadTimeout: (datetime.timedelta(hours=1), "1 hour"),
            ConfigurationError: (datetime.timedelta(hours=12), "12 hours"),
            PermissionError: (datetime.timedelta(hours=12), "12 hours"),
            requests.exceptions.ProxyError: (datetime.timedelta(hours=1), "1 hour"),
            AuthenticationError: (datetime.timedelta(hours=12), "12 hours"),
        },
        "opensubtitlescom": {
            TooManyRequests: (datetime.timedelta(minutes=1), "1 minute"),
            DownloadLimitExceeded: (datetime.timedelta(hours=6), "6 hours"),
            ProviderError: (datetime.timedelta(minutes=1), "1 minute"),
        },
        "addic7ed": {
            DownloadLimitExceeded: (datetime.timedelta(hours=3), "3 hours"),
            TooManyRequests: (datetime.timedelta(minutes=5), "5 minutes"),
            IPAddressBlocked: (datetime.timedelta(hours=1), "1 hours"),
        },
        "titlovi": {
            TooManyRequests: (datetime.timedelta(minutes=5), "5 minutes"),
        },
        "titrari": {
            TooManyRequests: (datetime.timedelta(minutes=10), "10 minutes"),
        },
        "titulky": {
            TooManyRequests: (datetime.timedelta(minutes=1), "1 minute"),
            DownloadLimitExceeded: (
                titulky_limit_reset_timedelta(),
                f"{titulky_limit_reset_timedelta().seconds // 3600 + 1} hours")
        },
        "legendasdivx": {
            TooManyRequests: (datetime.timedelta(hours=3), "3 hours"),
            DownloadLimitExceeded: (
                legendasdivx_limit_reset_timedelta(),
                f"{legendasdivx_limit_reset_timedelta().seconds // 3600 + 1} hours"),
            IPAddressBlocked: (
                legendasdivx_limit_reset_timedelta(),
                f"{legendasdivx_limit_reset_timedelta().seconds // 3600 + 1} hours"),
            SearchLimitReached: (
                legendasdivx_limit_reset_timedelta(),
                f"{legendasdivx_limit_reset_timedelta().seconds // 3600 + 1} hours"),
        },
        "whisperai": {
            ConnectionError: (datetime.timedelta(minutes=5), "5 minutes"),
        },
        "regielive": {
            APIThrottled: (datetime.timedelta(hours=1), "1 hour"),
            TooManyRequests: (datetime.timedelta(minutes=5), "5 minutes"),
            ProviderError: (datetime.timedelta(minutes=10), "10 minutes"),
        },
        "subsource": {
            APIThrottled: (datetime.timedelta(minutes=10), "10 minutes"),
            AuthenticationError: (datetime.timedelta(hours=1), "1 hour"),
            ForbiddenError: (datetime.timedelta(minutes=15), "15 minutes"),
            TooManyRequests: (datetime.timedelta(hours=1), "1 hour"),
        },
        "subdl": {
            ProviderError: (datetime.timedelta(hours=1), "1 hour"),
        }
    }


PROVIDERS_FORCED_OFF = ["addic7ed", "tvsubtitles", "legendasdivx", "napiprojekt", "shooter",
                        "supersubtitles", "titlovi", "assrt"]

throttle_count = {}
# The compat fanout records throttles from inside its provider futures, so two
# providers failing at once now reach this table concurrently, and both races
# that opens end the same way: an exception out of the provider's own error
# handler, which the fanout then reports in place of the failure it was
# recording. One is the staging file, which every writer moves through the same
# throttled_providers.dat.tmp path. The other is `tp` itself, which one thread
# can resize while another is serializing it. So the lock covers every mutation
# of the table and its persistence, not only the write. It is reentrant because
# those pairs call set_throttled_providers(), which takes it again.
_THROTTLE_LOCK = threading.RLock()

# Remote exception names the worker transport carries when a provider raised a
# timeout. The envelope keeps the class name but not the class, so a search
# outcome and this table disagree about the same failure unless both read it.
# These are exactly the names core.provider_search_failure calls a timeout.
_REMOTE_TIMEOUTS = {
    "Timeout": requests.exceptions.Timeout,
    "ConnectTimeout": requests.exceptions.ConnectTimeout,
    "ReadTimeout": requests.exceptions.ReadTimeout,
}


def provider_is_usable(name):
    """Adoption gate for the provider pools (see SZProviderPool.__getitem__):
    True only when the provider is enabled in settings, not currently
    throttled, and not blocked by a credential that will not decrypt, so a
    download naming a provider from a stale cached search cannot resurrect
    one the user disabled, the backoff excluded, or the secret store
    withheld the credential of."""
    if name not in settings.general.enabled_providers:
        return False
    reason, until, _ = tp.get(name, (None, None, None))
    if reason and until and datetime.datetime.now() < until:
        return False
    if name in _decrypt_blocked_provider_ids():
        return False
    return True


def provider_pool():
    if settings.general.multithreading:
        return subliminal_patch.core.SZAsyncProviderPool
    return subliminal_patch.core.SZProviderPool


def _lang_from_str(content: str):
    " Formats: es-MX en@hi es-MX@forced "
    extra_info = content.split("@")
    if len(extra_info) > 1:
        kwargs = {extra_info[-1]: True}
    else:
        kwargs = {}

    content = extra_info[0]

    try:
        code, country = content.split("-")
    except ValueError:
        lang = CustomLanguage.from_value(content)
        if lang is not None:
            lang = lang.subzero_language()
            return lang.rebuild(lang, **kwargs)

        code, country = content, None

    return subliminal_patch.core.Language(code, country, **kwargs)


def get_language_equals(settings_=None):
    settings_ = settings_ or settings

    equals = settings_.general.language_equals
    if not equals:
        return []

    items = []
    for equal in equals:
        try:
            from_, to_ = equal.split(":")
            from_, to_ = _lang_from_str(from_), _lang_from_str(to_)
        except Exception as error:
            logging.info("Invalid equal value: '%s' [%s]", equal, error)
        else:
            items.append((from_, to_))

    return items


def get_provider_language_exclusions(settings_=None):
    settings_ = settings_ or settings

    configured = getattr(settings_.general, "provider_languages", {}) or {}
    if isinstance(configured, str):
        try:
            configured = json.loads(configured)
        except ValueError:
            logging.info("Invalid provider language configuration: %r", configured)
            return {}

    if not isinstance(configured, dict):
        return {}

    exclusions = {}
    for provider, language_codes in configured.items():
        if not provider or not isinstance(language_codes, (list, tuple, set)):
            continue

        languages = set()
        for language_code in language_codes:
            if not isinstance(language_code, str) or not language_code.strip():
                continue
            try:
                languages.add(_lang_from_str(language_code.strip()))
            except Exception as error:
                logging.info("Invalid provider language value for %s: '%s' [%s]", provider, language_code, error)

        if languages:
            exclusions[str(provider)] = languages

    return exclusions


def get_provider_language_hook(settings_=None):
    settings_ = settings_ or settings

    def provider_language_hook(provider):
        return get_provider_language_exclusions(settings_).get(provider)

    return provider_language_hook


def get_providers():
    _ensure_provider_hub_registered()
    providers_list = []
    existing_providers = provider_registry.names()
    blocked = _decrypt_blocked_provider_ids()
    # Selection: a provider whose stored credential will not decrypt is
    # left out here, so no search asks the pool for it at all.
    providers = [x for x in settings.general.enabled_providers
                 if x in existing_providers and x not in blocked]
    for provider in providers:
        reason, until, throttle_desc = tp.get(provider, (None, None, None))
        providers_list.append(provider)

        if reason:
            now = datetime.datetime.now()
            if now < until:
                # INFO rather than DEBUG: a provider being skipped until a later
                # hour is a decision about what the reader gets, and it is the
                # line a report about a provider that "does nothing" needs.
                logging.info("Not using %s until %s, because of: %s", provider,
                             until.strftime("%y/%m/%d %H:%M"), reason)
                providers_list.remove(provider)
            else:
                # Decide again with the lock held. The read above is a snapshot,
                # and a provider future recording a fresh backoff between the
                # two would otherwise have this release it on the strength of
                # the deadline it has already replaced.
                with _THROTTLE_LOCK:
                    reason, until, throttle_desc = tp.get(provider, (None, None, None))
                    if reason and until and datetime.datetime.now() < until:
                        logging.info("Not using %s until %s, because of: %s", provider,
                                     until.strftime("%y/%m/%d %H:%M"), reason)
                        providers_list.remove(provider)
                    elif reason:
                        logging.info("Using %s again after %s, (disabled because: %s)", provider,
                                     throttle_desc, reason)
                        tp.pop(provider, None)
                        set_throttled_providers(tp)
        # if forced only is enabled: # fixme: Prepared for forced only implementation to remove providers with don't support forced only subtitles
        #     for provider in providers_list:
        #         if provider in PROVIDERS_FORCED_OFF:
        #             providers_list.remove(provider)

    if not providers_list:
        providers_list = None

    return providers_list


def get_enabled_providers():
    # return enabled provider including those who can be throttled
    if isinstance(settings.general.enabled_providers, list):
        return settings.general.enabled_providers
    else:
        return []


def get_providers_sorted():
    """Get enabled providers sorted by priority (lower = higher priority)"""
    providers_list = get_providers()
    if not providers_list:
        return None

    priorities = settings.general.provider_priorities or {}
    default_priority = priorities.get('default', 100)

    # Sort by priority (lower number = higher priority)
    sorted_providers = sorted(
        providers_list,
        key=lambda p: priorities.get(p, default_priority)
    )
    return sorted_providers


def get_provider_priority(provider_name):
    """Get priority for a specific provider"""
    priorities = settings.general.provider_priorities or {}
    return priorities.get(provider_name, priorities.get('default', 100))


def get_provider_score_modifier(provider_name):
    """The signed percentage to add to this provider's candidate scores.

    Read fresh on every scoring call rather than cached, so editing the setting
    takes effect on the next search without a restart. The value comes back
    from the browser as JSON, so anything that is not a number is treated as no
    modifier at all instead of taking the search down.
    """
    modifiers = settings.general.provider_score_modifiers or {}
    value = modifiers.get(provider_name, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    # NaN and the infinities pass the type check and then raise inside round().
    if not math.isfinite(value):
        return 0
    # The scale only goes to a hundred, and a finite value large enough to
    # overflow the multiplication would raise rather than degrade.
    return max(-100, min(100, value))


# Install the reader on the scorer class, not on the shared instance. Every
# native search path -- automatic download, the priority-ordered listing's
# early-exit, manual search -- goes through the shared one, but the compat
# endpoint builds its own ComputeScore to project scores for external clients,
# and that surface has to agree with the rest. staticmethod keeps it a plain
# function rather than binding self over the provider name.
def get_ai_translated_score_penalty():
    """Read the configured integer percentage-point penalty for each score."""
    value = getattr(settings.general, 'ai_translated_score_penalty', 0)
    return value if type(value) is int and 0 <= value <= 100 else 0


ComputeScore.modifier = staticmethod(get_provider_score_modifier)
ComputeScore.ai_translated_penalty = staticmethod(get_ai_translated_score_penalty)


_FFPROBE_BINARY = get_binary("ffprobe")
_FFMPEG_BINARY = get_binary("ffmpeg")


def get_providers_auth():
    provider_configs = {
        'addic7ed': {
            'username': settings.addic7ed.username,
            'password': settings.addic7ed.password,
            'cookies': settings.addic7ed.cookies,
            'user_agent': settings.addic7ed.user_agent,
            'is_vip': settings.addic7ed.vip,
        },
        'avistaz': {
            'cookies': settings.avistaz.cookies,
            'user_agent': settings.avistaz.user_agent,
        },
        'cinemaz': {
            'cookies': settings.cinemaz.cookies,
            'user_agent': settings.cinemaz.user_agent,
        },
        'opensubtitles': {
            'username': settings.opensubtitles.username,
            'password': settings.opensubtitles.password,
            'use_tag_search': settings.opensubtitles.use_tag_search,
            'only_foreign': False,  # fixme
            'also_foreign': False,  # fixme
            'is_vip': settings.opensubtitles.vip,
            'use_ssl': settings.opensubtitles.ssl,
            'timeout': int(settings.opensubtitles.timeout) or 15,
            'skip_wrong_fps': settings.opensubtitles.skip_wrong_fps,
            'use_web_scraper': settings.opensubtitles.use_web_scraper,
            'scraper_service_url': settings.opensubtitles.scraper_service_url,
        },
        'opensubtitlescom': {'username': settings.opensubtitlescom.username,
                             'password': settings.opensubtitlescom.password,
                             'use_hash': settings.opensubtitlescom.use_hash,
                             'include_ai_translated': settings.opensubtitlescom.include_ai_translated,
                             'include_machine_translated': settings.opensubtitlescom.include_machine_translated,
                             'api_key': 's38zmzVlW7IlYruWi7mHwDYl2SfMQoC1'
                             },
        'napiprojekt': {'only_authors': settings.napiprojekt.only_authors,
                        'only_real_names': settings.napiprojekt.only_real_names},
        'legendasdivx': {
            'username': settings.legendasdivx.username,
            'password': settings.legendasdivx.password,
            'skip_wrong_fps': settings.legendasdivx.skip_wrong_fps,
        },
        'legendasnet': {
            'username': settings.legendasnet.username,
            'password': settings.legendasnet.password,
        },
        'pipocas': {
            'username': settings.pipocas.username,
            'password': settings.pipocas.password,
        },
        'assrt': {
            'token': settings.assrt.token,
        },
        'napisy24': {
            'username': settings.napisy24.username,
            'password': settings.napisy24.password,
        },
        'betaseries': {'token': settings.betaseries.token},
        'titulky': {
            'username': settings.titulky.username,
            'password': settings.titulky.password,
            'approved_only': settings.titulky.approved_only,
            'skip_wrong_fps': settings.titulky.skip_wrong_fps,
        },
        'titlovi': {
            'username': settings.titlovi.username,
            'password': settings.titlovi.password,
        },
        'jimaku': {
            'api_key': settings.jimaku.api_key,
            'enable_name_search_fallback': settings.jimaku.enable_name_search_fallback,
            'enable_archives_download': settings.jimaku.enable_archives_download,
            'enable_ai_subs': settings.jimaku.enable_ai_subs,
        },
        'ktuvit': {
            'email': settings.ktuvit.email,
            'hashed_password': settings.ktuvit.hashed_password,
        },
        'embeddedsubtitles': {
            'included_codecs': settings.embeddedsubtitles.included_codecs,
            'hi_fallback': settings.embeddedsubtitles.hi_fallback,
            'cache_dir': os.path.join(args.config_dir, "cache"),
            'ffprobe_path': _FFPROBE_BINARY,
            'ffmpeg_path': _FFMPEG_BINARY,
            'timeout': settings.embeddedsubtitles.timeout,
            'unknown_as_fallback': settings.embeddedsubtitles.unknown_as_fallback,
            'fallback_lang': settings.embeddedsubtitles.fallback_lang,
        },
        'karagarga': {
            'username': settings.karagarga.username,
            'password': settings.karagarga.password,
            'f_username': settings.karagarga.f_username,
            'f_password': settings.karagarga.f_password,
        },
        'hdbits': {
            'username': settings.hdbits.username,
            'passkey': settings.hdbits.passkey,
        },
        'subf2m': {
            'verify_ssl': settings.subf2m.verify_ssl,
            'user_agent': settings.subf2m.user_agent,
        },
        'whisperai': {
            'endpoint': settings.whisperai.endpoint,
            'response': settings.whisperai.response,
            'timeout': settings.whisperai.timeout,
            'ffmpeg_path': _FFMPEG_BINARY,
            'loglevel': settings.whisperai.loglevel,
            'pass_video_name': settings.whisperai.pass_video_name,
        },
        "animetosho": {
            'search_threshold': settings.animetosho.search_threshold,
        },
        "subdl": {
            'api_key': settings.subdl.api_key,
            'anime_mode': settings.subdl.anime_mode,
        },
        'turkcealtyaziorg': {
            'cookies': settings.turkcealtyaziorg.cookies,
            'user_agent': settings.turkcealtyaziorg.user_agent,
        },
        'subsource': {
            'api_key': settings.subsource.apikey,
        },
        'subsarr': {
            'base_url': settings.subsarr.base_url,
        },
        'animesubinfo': {},
        'subx':
            {
                'api_key': settings.subx.api_key,
            },
        'subsro': {
            'api_key': settings.subsro.api_key,
        }
    }
    try:
        from provider_hub.service import runtime_provider_configs
        provider_configs.update(runtime_provider_configs())
    except Exception:
        logging.exception("Unable to load Provider Hub provider config")
    try:
        from provider_hub.manifest import reserved_settings_sections
        from provider_hub.state import running_installations
        reserved_sections = reserved_settings_sections()
        blocked = _decrypt_blocked_provider_ids()
        # The same active-version population the runtime configs admit, with
        # the active manifest: the class stays keyed off active_version until
        # a restart, so a pending update must not strip the section overlay,
        # or a provider admitted on its settings-section credential runs
        # without it. A staged schema's fields are not overlaid; the code
        # that is running is the active version's.
        for installation in running_installations():
            if installation.provider_id in blocked:
                # Configuration: a provider whose stored credential will not
                # decrypt is not rebuilt here from its installation row, or
                # the pool would query it anyway with the settings section's
                # values for config. No config, no worker request.
                continue
            manifest = installation.manifest
            schema = manifest.get("config_schema") if isinstance(manifest, dict) else {}
            properties = schema.get("properties") if isinstance(schema, dict) else {}
            if not isinstance(properties, dict):
                continue
            if installation.provider_id in reserved_sections:
                # The section named after this plugin is one of Bazarr's own, not
                # the plugin's. validate_manifest refuses such an id at install and
                # at every boot, so getting here means a manifest slipped past it,
                # and copying the section's keys into the worker config would hand
                # the plugin that section's credentials (sonarr.apikey, plex.token,
                # auth.password) on every search. Its own stored config and the
                # schema defaults still apply.
                if installation.provider_id not in _REPORTED_RESERVED_SECTION_PLUGINS:
                    _REPORTED_RESERVED_SECTION_PLUGINS.add(installation.provider_id)
                    logging.error("Refusing to read the %s settings section for Provider Hub plugin %s: "
                                  "that section belongs to Bazarr, not to the plugin",
                                  installation.provider_id, installation.provider_id)
                section = {}
            else:
                section = settings.get(installation.provider_id, {}) if hasattr(settings, "get") else {}
            config = dict(provider_configs.get(installation.provider_id, {}))
            for key, field in properties.items():
                if not isinstance(field, dict):
                    continue
                if "default" in field and key not in config:
                    config[key] = field["default"]
                value = section.get(key) if hasattr(section, "get") else getattr(section, key, None)
                if value not in (None, "", [], {}):
                    config[key] = value
            provider_configs[installation.provider_id] = config
    except Exception:
        logging.exception("Unable to load Provider Hub settings config")
    return provider_configs


def _handle_mgb(name, exception, ids, language, sports_context=None):
    if language.forced:
        language_str = f'{language.basename}:forced'
    elif language.hi:
        language_str = f'{language.basename}:hi'
    else:
        language_str = language.basename

    if sports_context is not None:
        # A sports search carries the event and its instance on the video, and
        # the pool threads them through to this callback. The release ids are
        # all None by construction, so this must come before the media
        # branches; the event's own table records the exclusion the same shape
        # the owned blacklist flow writes. Guarded: an attribution fault must
        # not take the whole provider search down with it.
        from sportarr.history import blacklist_log_sports
        try:
            blacklist_log_sports(
                sports_context, name, exception.id, language_str)
        except Exception:
            logging.exception(
                'BAZARR could not record the sports blacklist for %s '
                'release %s', name, exception.id)
        return

    if ids:
        # The id dict always carries all three media keys, filled with None
        # when the video does not have them, so membership never told these
        # branches apart. A sports search is already gone by here: its video
        # and every subtitle listed off it carry sports_context, which the
        # branch above returns on, so nothing below can be a recording.
        #
        # The dict also names the instance the video belongs to. The row keeps
        # that owner even when the item itself is gone by now, removed with its
        # instance's library while this search ran. With no owner, the next
        # startup gives the row to the kind's only instance, or builds one
        # from the stored connection settings when the kind has none left.
        #
        # The remaining null-id case is an episode or movie whose database
        # refiner did not resolve, which is routine on an instance with its own
        # path mappings because the refiner looks the row up through the GLOBAL
        # reverse mapping. get_blacklist() reads (provider, subs_id) with no
        # media scoping, so the row does suppress the bad release everywhere,
        # and dropping it left the corrupt subtitle to be re-downloaded and
        # re-rejected forever.
        #
        # Know what the row costs, because it is not a visible junk entry. Both
        # Excluded pages inner-join the local ids this row leaves NULL, so it
        # never lists, and the exclusion it applies is global. It is still
        # removable: blacklist_delete counts distinct owners for that one
        # (provider, subs_id) and refuses only when a NULL-owner row shares a
        # key with an owned one, which is not the ordinary case, so an unscoped
        # delete by the provider and release id the warning below prints will
        # clear it. That is the behaviour development has always had; it is
        # recorded here so the next reader weighing "tidy the Excluded page"
        # against "stop re-downloading a subtitle the provider rejected" knows
        # which way the trade runs.
        if exception.media_type == "series":
            if not (ids.get('sonarrSeriesId') and ids.get('sonarrEpisodeId')):
                logging.warning(
                    'BAZARR provider %s demanded a blacklist for %s on an episode that could not '
                    'be attributed; recording it unattributed so the release stays excluded.',
                    name, exception.id)
            blacklist_log(ids.get('sonarrSeriesId'), ids.get('sonarrEpisodeId'), name, exception.id,
                          language_str, arr_instance_id=ids.get('arr_instance_id'))
            return
        if not ids.get('radarrId'):
            logging.warning(
                'BAZARR provider %s demanded a blacklist for %s on a movie that could not be '
                'attributed; recording it unattributed so the release stays excluded.',
                name, exception.id)
        blacklist_log_movie(ids.get('radarrId'), name, exception.id, language_str,
                            arr_instance_id=ids.get('arr_instance_id'))


def _provider_failure_words(exception):
    """The reason in words for the one line a recorded backoff writes.

    One classifier, the pool handlers' own (subliminal_patch.core), so the
    line the reader sees cannot disagree with what the handler classified.
    An unexpected exception still earns its line, beside its ERROR record:
    the words just say what the backoff is for, and stay neutral about it:
    only the ERROR record can back the word "unexpected" up, and the
    handler, not this line, decides whether there is one."""
    from subliminal_patch.core import provider_failure_words
    return provider_failure_words(exception) or "error"


def _failure_class_name(exception):
    """The class the one line names: a plugin's own class when the envelope
    carries one the search path would also accept, the exception's own
    otherwise. A plugin's name that is not an identifier is refused rather
    than printed, falling back the same way provider_search_failure does."""
    code = getattr(exception, "code", None)
    if code == "timeout":
        return "Timeout"
    if code == "provider":
        remote_name = getattr(exception, "remote_class_name", None)
        if isinstance(remote_name, str) and remote_name:
            if _REMOTE_CLASS_NAME_RE.fullmatch(remote_name):
                return remote_name
            return "UnknownError"
    return type(exception).__name__


def provider_throttle(name, exception, ids=None, language=None, sports_context=None, wait=True,
                      operation=None, announce=True):
    """Record a provider failure in the throttle table.

    ``wait`` is the caller's promise about its own deadline. The count gate
    below pauses through the first few rate-limit events and only records the
    backoff on the fifth, which is right for a search that is about to retry
    the same provider in the same call: the pause is the retry's whole point,
    and a provider that recovers on the second attempt has earned no backoff.
    A bounded fanout does neither. The pause would be spent inside the wall the
    other providers are still living on, and there is no retry for it to buy,
    so the first rate limit is the only evidence there will be. Such a caller
    passes ``wait=False`` and the backoff is recorded at once, without a pause.

    ``operation`` names the path that failed, search or download, for the one
    line. ``announce`` is whether this call may write that line at WARNING: a
    download passes False for every attempt, because its one line is written
    once it stops retrying, naming the deadline actually in force. Returns
    whether a backoff was recorded; False is the caller's signal that there is
    nothing to announce later.
    """
    if isinstance(exception, MustGetBlacklisted) and isinstance(ids, dict) and isinstance(language, Language):
        _handle_mgb(name, exception, ids, language, sports_context)
        return False

    cls = getattr(exception, "__class__")
    cls_name = getattr(cls, "__name__")
    # A plugin worker's failure reaches us as a WorkerError: the transport
    # cannot carry the original exception class, only a code and, for a failure
    # the provider itself raised, the remote class name. Classifying by class
    # alone records "WorkerError" with the generic ten-minute default, so the
    # cooldown Discover reads back out of this table afterwards says "provider
    # cooldown" about what was plainly a timeout, and outranks the timeout
    # floor with a duration that has nothing to do with the cause.
    # core.provider_search_failure already reads both the hard-deadline code
    # and these remote names; the backoff has to agree with it or the two
    # disagree on screen.
    code = getattr(exception, "code", None)
    if code == "timeout":
        cls = requests.exceptions.Timeout
    elif code == "provider":
        cls = _REMOTE_TIMEOUTS.get(getattr(exception, "remote_class_name", None), cls)
    cls_name = cls.__name__
    if cls not in VALID_THROTTLE_EXCEPTIONS:
        for valid_cls in VALID_THROTTLE_EXCEPTIONS:
            if issubclass(cls, valid_cls):
                cls = valid_cls

    throttle_data = provider_throttle_map().get(name, provider_throttle_map()["default"]).get(cls, None) or \
                    provider_throttle_map()["default"].get(cls, None)

    if throttle_data:
        throttle_delta, throttle_description = throttle_data
    else:
        throttle_delta, throttle_description = datetime.timedelta(minutes=10), "10 minutes"

    # A provider that sent a Retry-After has said when it will answer again.
    # The map holds what this exception class is worth in general, which is a
    # floor, not a ceiling on what the provider asked for: a site asking for an
    # hour was otherwise re-queried after the class's ten minutes, since the
    # header only ever chose how long to pause between retries. Clamped the way
    # core.provider_search_failure clamps the same value.
    retry_after = getattr(exception, "retry_after", None)
    try:
        retry_after = max(1.0, min(86400.0, float(retry_after))) if retry_after else None
    except (TypeError, ValueError):
        retry_after = None
    if retry_after and datetime.timedelta(seconds=retry_after) > throttle_delta:
        throttle_delta = datetime.timedelta(seconds=retry_after)
        throttle_description = (f"{round(retry_after)} seconds" if retry_after < 120
                                else f"{round(retry_after / 60)} minutes")

    throttle_until = datetime.datetime.now() + throttle_delta

    recorded = False
    if cls_name not in VALID_COUNT_EXCEPTIONS or throttled_count(name, exception, wait=wait,
                                                                 operation=operation):
        if cls_name == 'ValueError' and isinstance(exception.args, tuple) and len(exception.args) and exception.args[
            0].startswith('unsupported pickle protocol'):
            for fn in subliminal_cache_region.backend.all_filenames:
                try:
                    os.remove(fn)
                except (IOError, OSError):
                    logging.debug("Couldn't remove cache file: %s", os.path.basename(fn))
            # A cache reset records no backoff, so there is nothing for a
            # caller to announce either.
        else:
            with _THROTTLE_LOCK:
                # Two searches can have the same provider in flight and compute
                # their deadlines before either gets here, so the second writer
                # is not necessarily the one with the most to say. Keep the
                # later deadline: an hour the provider asked for must not be
                # replaced by a generic ten minutes from the other request,
                # which would put Bazarr back on its door while it is still
                # refusing. The same rule the reader applies, on the way in.
                current = tp.get(name)
                if current and current[1] and current[1] > throttle_until:
                    cls_name, throttle_until, throttle_description = current
                tp[name] = (cls_name, throttle_until, throttle_description)
                set_throttled_providers(tp)

            trac_info = _get_traceback_info(exception)

            if announce:
                # The one line for a provider failure that takes the provider
                # out of rotation: what failed, in words, and when it is used
                # again. The backoff itself is unchanged; only the level and
                # the wording differ from the line this replaces.
                logging.warning("Provider %s %s: %s (%s). Not using it for %s, until %s. Exception info: %r",
                                name, f"{operation} failed" if operation else "failed",
                                _provider_failure_words(exception), _failure_class_name(exception),
                                throttle_description, throttle_until.strftime("%y/%m/%d %H:%M"), trac_info)
            else:
                logging.debug("Throttling %s for %s, until %s, because of: %s. Exception info: %r", name,
                              throttle_description, throttle_until.strftime("%y/%m/%d %H:%M"), cls_name, trac_info)
            recorded = True

    update_throttled_provider()
    return recorded


def announce_provider_throttle(name, exception, operation=None):
    """Write the one line for a backoff a download recorded across its retries.

    Every attempt recorded with announce=False, below the default filter, so
    a line per attempt would write three where the reader needs one. This
    reads the entry the last attempt left and names the deadline actually
    enforced, which the table already holds as the later of the attempts'
    deadlines. A caller whose calls recorded nothing never reaches this, and
    an entry that expired or was cleared writes nothing: there is no deadline
    in force to name."""
    with _THROTTLE_LOCK:
        entry = tp.get(name)
    _reason, until, throttle_description = entry or (None, None, None)
    if not until or datetime.datetime.now() >= until:
        return
    trac_info = _get_traceback_info(exception)
    logging.warning("Provider %s %s: %s (%s). Not using it for %s, until %s. Exception info: %r",
                    name, f"{operation} failed" if operation else "failed",
                    _provider_failure_words(exception), _failure_class_name(exception),
                    throttle_description, until.strftime("%y/%m/%d %H:%M"), trac_info)


def _get_traceback_info(exc: Exception):
    traceback_str = " ".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    clean_msg = str(exc).replace("\n", " ").strip()

    line_info = _TRACEBACK_RE.findall(traceback_str)

    # Value info max chars len is 100

    if not line_info:
        return clean_msg[:100]

    line_info = line_info[-1]
    file_, line = line_info

    extra = f"' ~ {os.path.basename(file_)}@{line}"[:90]
    message = f"'{clean_msg}"[:100 - len(extra)]

    return message + extra


def throttled_count(name, exception=None, wait=True, operation=None):
    global throttle_count
    if name in list(throttle_count.keys()):
        if 'count' in list(throttle_count[name].keys()):
            for key, value in throttle_count[name].items():
                if key == 'count':
                    value += 1
                    throttle_count[name]['count'] = value
        else:
            throttle_count[name] = {"count": 1, "time": (datetime.datetime.now() + datetime.timedelta(seconds=120))}

    else:
        throttle_count[name] = {"count": 1, "time": (datetime.datetime.now() + datetime.timedelta(seconds=120))}

    if throttle_count[name]['count'] >= 5:
        return True
    if throttle_count[name]['time'] <= datetime.datetime.now():
        throttle_count[name] = {"count": 1, "time": (datetime.datetime.now() + datetime.timedelta(seconds=120))}

    # Respect Retry-After from the exception if available, otherwise default to 5s
    wait_seconds = 5
    if exception and hasattr(exception, 'retry_after') and exception.retry_after:
        wait_seconds = max(1, min(exception.retry_after, 30))  # floor at 1s, cap at 30s

    if not wait:
        # No pause means no retry, so there is no second attempt for this
        # provider to redeem itself on. Record the backoff its exception class
        # earns now, or the caller re-asks a rate-limited provider on its very
        # next request and the per-provider cool-offs never apply to it.
        # Below the default filter: the recorded backoff's own line follows at
        # once and says everything this one would.
        logging.debug("Provider %s throttle count %s of 5, recorded without waiting", name,
                      throttle_count[name]['count'])
        return True

    # While the gate has not tripped, this is the one line for the event: the
    # pool still leaves the provider out until its next refresh, so the reader
    # has to be told, with the operation, the reason in words, the exception
    # class and that no backoff is recorded yet. The wait itself is the gate's
    # timing and does not change.
    logging.warning("Provider %s %s: %s (%s). Throttle count %s of 5, no backoff recorded yet; leaving the provider "
                    "out until the pool's next refresh",
                    name, f"{operation} failed" if operation else "failed",
                    _provider_failure_words(exception or Exception()), _failure_class_name(exception or Exception()),
                    throttle_count[name]['count'])
    time.sleep(wait_seconds)
    return False


def update_throttled_provider():
    existing_providers = provider_registry.names()
    providers_list = [x for x in settings.general.enabled_providers if x in existing_providers]

    # Held across the whole sweep: it both deletes from the table and hands the
    # table to the writer, so a recorder arriving mid-sweep would otherwise
    # resize a dict this loop is already walking.
    with _THROTTLE_LOCK:
        for provider in list(tp):
            if provider not in providers_list:
                tp.pop(provider, None)
                set_throttled_providers(tp)

            reason, until, throttle_desc = tp.get(provider, (None, None, None))

            if reason:
                now = datetime.datetime.now()
                if now < until:
                    pass
                else:
                    logging.info("Using %s again after %s, (disabled because: %s)", provider, throttle_desc, reason)
                    tp.pop(provider, None)
                    set_throttled_providers(tp)

                reason, until, throttle_desc = tp.get(provider, (None, None, None))

                if reason:
                    now = datetime.datetime.now()
                    if now >= until:
                        logging.info("Using %s again after %s, (disabled because: %s)", provider, throttle_desc,
                                     reason)
                        tp.pop(provider, None)
                        set_throttled_providers(tp)

    event_stream(type='badges')


def list_throttled_providers():
    update_throttled_provider()
    throttled_providers = []
    existing_providers = provider_registry.names()
    providers = [x for x in settings.general.enabled_providers if x in existing_providers]
    for provider in providers:
        reason, until, throttle_desc = tp.get(provider, (None, None, None))
        throttled_providers.append([provider, reason, pretty_date(until)])
    return throttled_providers


def snapshot_throttled_providers():
    """Read-only view of the in-memory throttle table.

    ``list_throttled_providers`` calls ``update_throttled_provider`` first,
    which deletes expired entries, rewrites throttled_providers.dat and emits a
    badge event. A status read must do none of that, so this filters expired
    entries in memory and writes nothing.

    An empty result means no throttle is recorded for an enabled provider. It is
    not evidence that the providers are healthy, and one throttled provider says
    nothing about the others.
    """
    now = datetime.datetime.now()
    enabled = list(settings.general.enabled_providers or [])
    providers = []
    for provider in enabled:
        reason, until, throttle_desc = tp.get(provider, (None, None, None))
        if not reason or not until or until <= now:
            continue
        providers.append({'provider': provider, 'reason': reason,
                          'until': until.isoformat(), 'description': throttle_desc})
    return {'providers': providers, 'enabled_count': len(enabled)}


# The throttle reasons a user can actually fix by correcting what they typed.
# Everything else in the table is the provider's own state, so a settings or
# Provider Hub edit has no business clearing it.
CREDENTIAL_THROTTLE_REASONS = ('AuthenticationError', 'ConfigurationError', 'PaymentRequired')


def reset_throttled_provider(name):
    """Forget one provider's credential throttle.

    A rejected password parks that provider for hours, and correcting it is
    what earns it its next attempt. Only that provider is touched, so the
    cool-offs the others are serving out stay where they are. Returns True when
    an entry was dropped, so a caller can log the difference.
    """
    with _THROTTLE_LOCK:
        reason = tp.get(name, (None, None, None))[0]
        if reason not in CREDENTIAL_THROTTLE_REASONS:
            return False
        tp.pop(name, None)
        set_throttled_providers(tp)
    event_stream(type='badges')
    logging.info('BAZARR throttle for %s has been reset (was: %s).', name, reason)
    return True


def reset_throttled_providers(only_auth_or_conf_error=False):
    with _THROTTLE_LOCK:
        for provider in list(tp):
            if only_auth_or_conf_error and tp[provider][0] not in CREDENTIAL_THROTTLE_REASONS:
                continue
            tp.pop(provider, None)
        set_throttled_providers(tp)
    update_throttled_provider()
    if only_auth_or_conf_error:
        logging.info('BAZARR throttled providers have been reset (only AuthenticationError, ConfigurationError and '
                     'PaymentRequired).')
    else:
        logging.info('BAZARR throttled providers have been reset.')


def _throttled_providers_path():
    return os.path.normpath(os.path.join(args.config_dir, 'config', 'throttled_providers.dat'))


def get_throttled_providers():
    providers = {}
    dat_path = _throttled_providers_path()
    try:
        if os.path.exists(dat_path):
            with open(dat_path, 'r') as handle:
                content = handle.read()
                if not content.strip():
                    return providers
                raw = json.loads(content)
                for name, val in raw.items():
                    cls_name, until_str, description = val
                    until = datetime.datetime.fromisoformat(until_str) if until_str else None
                    providers[name] = (cls_name, until, description)
    except (json.JSONDecodeError, KeyError, ValueError):
        logging.info("Migrating throttled_providers.dat from legacy format to JSON. Throttle state reset.")
        set_throttled_providers(providers)
    except Exception:
        logging.exception("Unexpected error reading throttled_providers.dat. Resetting.")
        set_throttled_providers(providers)
    return providers


def set_throttled_providers(data):
    if not isinstance(data, dict):
        raise TypeError(f"set_throttled_providers expects a dict, got {type(data).__name__}")
    dat_path = _throttled_providers_path()
    # Every writer stages through the one .tmp path, so two callers racing here
    # have the second replace a file the first has already moved, and the
    # FileNotFoundError surfaces out of whichever provider's error handler was
    # recording its failure. Reading `data` under the same lock also keeps this
    # iteration off a dict another recorder is writing to. The compat fanout
    # records from inside its provider futures, so both races are now reachable
    # from a single search.
    with _THROTTLE_LOCK:
        serializable = {}
        for name, val in data.items():
            cls_name, throttle_until, description = val
            serializable[name] = (
                cls_name,
                throttle_until.isoformat() if throttle_until else None,
                description
            )
        tmp_path = dat_path + '.tmp'
        with open(tmp_path, 'w') as handle:
            handle.write(json.dumps(serializable))
        os.replace(tmp_path, dat_path)


tp = get_throttled_providers()
if not isinstance(tp, dict):
    raise ValueError('tp should be a dict')
