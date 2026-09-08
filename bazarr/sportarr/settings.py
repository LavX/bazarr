"""Validated Sportarr options stored exclusively on the owning instance."""

from copy import deepcopy
import json

DEFAULTS = {
    "sync_interval": 60,
    "full_scan": "Daily",
    "full_scan_day": 6,
    "full_scan_hour": 4,
    "only_monitored": False,
    "sync_only_monitored_leagues": False,
    "sync_only_monitored_events": False,
    "excluded_tags": [],
    "excluded_sports": [],
    "search_on_sync": True,
    "use_ffprobe_cache": True,
    "minimum_score": 70,
    "wanted_search_frequency": 6,
}


def validate_sports_settings(blob):
    if blob is None:
        return {}
    if not isinstance(blob, dict):
        raise ValueError("sports_settings must be an object")
    for key, value in blob.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown sports_settings field: {key}")
        if key == "full_scan":
            valid = value in ("Manually", "Daily", "Weekly")
        elif key in ("excluded_tags", "excluded_sports"):
            valid = isinstance(value, list) and all(isinstance(x, str) for x in value)
        elif type(DEFAULTS[key]) is bool:
            valid = type(value) is bool
        else:
            bounds = {
                "sync_interval": (1, None),
                "full_scan_day": (0, 6),
                "full_scan_hour": (0, 23),
                "minimum_score": (1, 100),
                "wanted_search_frequency": (1, 876000),
            }
            low, high = bounds[key]
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


def get_sports_settings(instance):
    if instance.kind != 'sportarr':
        raise ValueError('Sports settings require a Sportarr instance')
    return deepcopy(DEFAULTS) | validate_sports_settings(_options(instance.options).get('sports_settings'))


def merge_sports_settings(options_json, blob):
    options = _options(options_json)
    previous = validate_sports_settings(options.get('sports_settings'))
    options['sports_settings'] = previous | validate_sports_settings(blob)
    return json.dumps(options)
