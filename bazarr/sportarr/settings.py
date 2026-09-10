"""Sportarr options: global config with an optional per-instance override.

Three layers resolve in order, lowest first:

1. The dynaconf validator default in ``app/config.py``. That is the only place
   a default is written down; this module deliberately keeps no copy.
2. The global ``settings.sportarr.*`` / ``settings.general.*_sports`` value.
   This is where an operator normally works, and it is what the Connections and
   Scheduler settings pages write.
3. The owning instance's ``options.sports_settings`` blob, an optional override
   for the operator running a second Sportarr server that needs to differ.

``get_sports_settings`` is the single read path, so no caller learns which layer
answered. There is no cache: the blob is read from the instance row the caller
already holds, so an edited override takes effect without an invalidation hook.
"""

import json

# Resolved key -> (config section, config key). Also the allow-list of override
# fields: validate_sports_settings rejects anything not named here.
GLOBAL_SOURCES = {
    "sports_sync": ("sportarr", "sports_sync"),
    "full_update": ("sportarr", "full_update"),
    "full_update_day": ("sportarr", "full_update_day"),
    "full_update_hour": ("sportarr", "full_update_hour"),
    "only_monitored": ("sportarr", "only_monitored"),
    "sync_only_monitored_leagues": ("sportarr", "sync_only_monitored_leagues"),
    "sync_only_monitored_events": ("sportarr", "sync_only_monitored_events"),
    "excluded_tags": ("sportarr", "excluded_tags"),
    "excluded_sports": ("sportarr", "excluded_sports"),
    "search_on_sync": ("sportarr", "search_on_sync"),
    "use_ffprobe_cache": ("sportarr", "use_ffprobe_cache"),
    "minimum_score": ("general", "minimum_score_sports"),
    "wanted_search_frequency": ("general", "wanted_search_frequency_sports"),
}

_LIST_KEYS = frozenset({"excluded_tags", "excluded_sports"})

_BOOL_KEYS = frozenset({
    "only_monitored",
    "sync_only_monitored_leagues",
    "sync_only_monitored_events",
    "search_on_sync",
    "use_ffprobe_cache",
})

# key -> (inclusive low, inclusive high or None). Mirrors the validator bounds.
_BOUNDS = {
    "sports_sync": (1, None),
    "full_update_day": (0, 6),
    "full_update_hour": (0, 23),
    "minimum_score": (1, 100),
    "wanted_search_frequency": (1, 876000),
}


def validate_sports_settings(blob):
    if blob is None:
        return {}
    if not isinstance(blob, dict):
        raise ValueError("sports_settings must be an object")
    for key, value in blob.items():
        if key not in GLOBAL_SOURCES:
            raise ValueError(f"Unknown sports_settings field: {key}")
        if key == "full_update":
            valid = value in ("Manually", "Daily", "Weekly")
        elif key in _LIST_KEYS:
            valid = isinstance(value, list) and all(isinstance(x, str) for x in value)
        elif key in _BOOL_KEYS:
            valid = type(value) is bool
        else:
            low, high = _BOUNDS[key]
            valid = (
                type(value) is int and value >= low and (high is None or value <= high)
            )
        if not valid:
            raise ValueError(f"Invalid sports_settings.{key}")
    return dict(blob)


def _options(raw):
    try:
        result = json.loads(raw or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def global_sports_settings():
    """The global layer, with no instance override applied.

    Lists are copied out of dynaconf's own container types so a caller mutating
    the result cannot write back into the live config.
    """
    from app.config import settings

    resolved = {}
    for key, (section, name) in GLOBAL_SOURCES.items():
        value = getattr(getattr(settings, section), name)
        resolved[key] = list(value) if key in _LIST_KEYS else value
    return resolved


def read_sports_overrides(instance):
    """The instance's stored override blob, with nothing merged in.

    The API serves this rather than the resolved values, because the settings
    UI reads a present key as "overridden" and an absent one as "inherited".
    """
    if instance.kind != 'sportarr':
        raise ValueError('Sports settings require a Sportarr instance')
    return validate_sports_settings(_options(instance.options).get('sports_settings'))


def get_sports_settings(instance):
    if instance.kind != 'sportarr':
        raise ValueError('Sports settings require a Sportarr instance')
    override = validate_sports_settings(_options(instance.options).get('sports_settings'))
    return global_sports_settings() | override


def merge_sports_settings(options_json, blob):
    """Return an ``options`` JSON string with sports_settings set to ``blob``,
    preserving any other key already in options.

    The submitted blob is the complete set of overrides, not an addition to
    what is stored. Unioning it with the previous value meant an override could
    never go back to inheriting the global: the UI drops a key when the
    operator switches that override off, and the union put it straight back.
    An empty blob removes the block entirely, so clearing the last override
    works too. Same contract as merge_subtitle_settings_into_options.
    """
    options = _options(options_json)
    validated = validate_sports_settings(blob)
    if validated:
        options['sports_settings'] = validated
    else:
        options.pop('sports_settings', None)
    return json.dumps(options)
