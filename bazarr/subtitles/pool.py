# coding=utf-8
# fmt: off

import logging
import time

from inspect import getfullargspec

from radarr.blacklist import get_blacklist_movie
from sonarr.blacklist import get_blacklist
from app.get_providers import get_providers, get_providers_auth, provider_throttle, provider_pool, get_language_equals, \
    get_provider_language_hook, get_providers_sorted, provider_is_usable  # noqa: F401

from .utils import get_ban_list


# fmt: on
def _init_pool(media_type, profile_id=None, providers=None, context=None):
    pool = provider_pool()
    return pool(
        providers=providers or get_providers_sorted(),
        provider_configs=get_providers_auth(),
        blacklist=_blacklist(media_type, context),
        throttle_callback=provider_throttle,
        ban_list=get_ban_list(profile_id),
        language_hook=get_provider_language_hook(),
        language_equals=get_language_equals(),
        adoption_gate=provider_is_usable,
    )


def _blacklist(media_type, context=None):
    if media_type == 'sports':
        from sportarr.subtitles import get_blacklist_sports
        return get_blacklist_sports(context)
    return get_blacklist() if media_type == 'series' else get_blacklist_movie()


def _pool_key(media_type, profile_id, context):
    if media_type == 'sports':
        from sportarr.subtitles import validate_context
        validate_context(context)
        return f'sports_{context.arr_instance_id}_{profile_id or ""}'
    return f'{media_type}_{profile_id or ""}'


_pools = {}


def _get_pool(media_type, profile_id=None, context=None):
    try:
        return _pools[_pool_key(media_type, profile_id, context)]
    except KeyError:
        _update_pool(media_type, profile_id, context)

        return _pools[_pool_key(media_type, profile_id, context)]


def _update_pool(media_type, profile_id=None, context=None):
    pool_key = _pool_key(media_type, profile_id, context)
    logging.debug("BAZARR updating pool: %s", pool_key)

    # Init a new pool if not present
    if pool_key not in _pools:
        logging.debug("BAZARR pool not initialized: %s. Initializing", pool_key)
        _pools[pool_key] = _init_pool(media_type, profile_id, context=context)

    pool = _pools[pool_key]
    if pool is None:
        return False

    return pool.update(
        get_providers_sorted(),
        get_providers_auth(),
        _blacklist(media_type, context),
        get_ban_list(profile_id),
        get_language_equals(),
    )


def _pool_update(pool, media_type, profile_id=None, context=None):
    return pool.update(
        get_providers_sorted(),
        get_providers_auth(),
        _blacklist(media_type, context),
        get_ban_list(profile_id),
        get_language_equals(),
    )


def update_pools(f):
    """Decorator that ensures all pools are updated on each function run.
    It will detect any config changes in Bazarr"""

    def decorated(*args, **kwargs):
        logging.debug("BAZARR updating pools: %s", _pools)

        start = time.time()
        args_spec = getfullargspec(f).args

        try:
            profile_id = kwargs["profile_id"]
        except KeyError:
            try:
                profile_id = args[args_spec.index("profile_id")]
            except (ValueError, IndexError):
                profile_id = None

        try:
            media_type = kwargs["media_type"]
        except KeyError:
            try:
                media_type = args[args_spec.index("media_type")]
            except (ValueError, IndexError):
                media_type = None

        context = kwargs.get("context")
        updated = _update_pool(media_type, profile_id, context)

        if updated:
            logging.debug(
                "BAZARR pools update elapsed time: %sms",
                round((time.time() - start) * 1000, 2),
            )

        return f(*args, **kwargs)

    return decorated
