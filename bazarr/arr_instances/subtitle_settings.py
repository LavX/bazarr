# coding=utf-8
"""Per-instance subtitle settings stored in ``arr_instances.options``.

A defined subset of the global subtitle settings (post-processing, audio sync
thresholds/engines, subzero mods) may be overridden per Sonarr/Radarr instance.
Overrides live under ``options["subtitle_settings"][<section>][<key>]``; an
absent key inherits the global value. See
https://github.com/LavX/bazarr/issues/227
"""
import json

SUBTITLE_SETTINGS_KEY = "subtitle_settings"


def _is_bool(value):
    return isinstance(value, bool)


def _is_str(value):
    return isinstance(value, str)


def _is_percent(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def _is_offset(value):
    return isinstance(value, int) and not isinstance(value, bool) and value in (60, 120, 300, 600)


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_engine_list(value):
    if not isinstance(value, list):
        return False
    from subtitles.tools.subsync_engines import SYNC_ENGINES
    return all(isinstance(item, str) and item in SYNC_ENGINES for item in value)


# section -> key -> (validator, human-readable constraint). The allowed set
# mirrors the per-instance candidates in the design (issue #227); anything not
# listed here is rejected so a typo or a not-allowed key never silently sticks.
ALLOWED = {
    "general": {
        "use_postprocessing": (_is_bool, "a boolean"),
        "postprocessing_cmd": (_is_str, "a string"),
        "use_postprocessing_threshold": (_is_bool, "a boolean"),
        "postprocessing_threshold": (_is_percent, "an integer between 0 and 100"),
        "use_postprocessing_threshold_movie": (_is_bool, "a boolean"),
        "postprocessing_threshold_movie": (_is_percent, "an integer between 0 and 100"),
        "subzero_mods": (_is_str_list, "a list of strings"),
        "subzero_mods_keep_lyrics": (_is_bool, "a boolean"),
    },
    "subsync": {
        "use_subsync": (_is_bool, "a boolean"),
        "use_subsync_threshold": (_is_bool, "a boolean"),
        "subsync_threshold": (_is_percent, "an integer between 0 and 100"),
        "use_subsync_movie_threshold": (_is_bool, "a boolean"),
        "subsync_movie_threshold": (_is_percent, "an integer between 0 and 100"),
        "enabled_engines": (_is_engine_list, "a list of known sync engine ids"),
        "max_offset_seconds": (_is_offset, "one of 60, 120, 300, 600"),
    },
}


def validate_subtitle_settings(blob):
    """Validate a per-instance subtitle_settings override blob.

    Returns a cleaned ``{section: {key: value}}`` dict containing only the
    recognised overrides (empty sections dropped). Raises ``ValueError`` on an
    unknown section/key or an out-of-range value so the API returns 400 rather
    than persisting garbage. The global config validators cannot be reused
    (e.g. there is no engine-enum check there), so the constraints are enforced
    here explicitly.
    """
    if blob is None:
        return {}
    if not isinstance(blob, dict):
        raise ValueError("subtitle_settings must be an object")
    cleaned = {}
    for section, items in blob.items():
        if section not in ALLOWED:
            raise ValueError(f"unknown subtitle settings section: {section}")
        if not isinstance(items, dict):
            raise ValueError(f"subtitle_settings.{section} must be an object")
        allowed_keys = ALLOWED[section]
        section_out = {}
        for key, value in items.items():
            if key not in allowed_keys:
                raise ValueError(f"setting not allowed per instance: {section}.{key}")
            validator, constraint = allowed_keys[key]
            if not validator(value):
                raise ValueError(f"{section}.{key} must be {constraint}")
            section_out[key] = value
        if section_out:
            cleaned[section] = section_out
    return cleaned


def read_subtitle_settings(options_json):
    """Parse the subtitle_settings blob out of an instance ``options`` JSON
    string. Returns {} for null/blank/malformed input or when the key is absent."""
    if not options_json:
        return {}
    try:
        options = json.loads(options_json)
    except (ValueError, TypeError):
        return {}
    if isinstance(options, dict) and isinstance(options.get(SUBTITLE_SETTINGS_KEY), dict):
        return options[SUBTITLE_SETTINGS_KEY]
    return {}


def merge_subtitle_settings_into_options(options_json, blob):
    """Return an ``options`` JSON string with subtitle_settings set to ``blob``,
    preserving any other keys already in options. An empty blob removes the
    subtitle_settings key (and returns None if nothing else remains), so a
    cleared override does not leave a stale entry behind."""
    options = {}
    if options_json:
        try:
            existing = json.loads(options_json)
            if isinstance(existing, dict):
                options = existing
        except (ValueError, TypeError):
            options = {}
    if blob:
        options[SUBTITLE_SETTINGS_KEY] = blob
    else:
        options.pop(SUBTITLE_SETTINGS_KEY, None)
    return json.dumps(options) if options else None
