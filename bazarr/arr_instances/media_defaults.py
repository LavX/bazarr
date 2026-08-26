# coding=utf-8
"""Per-instance default language profile stored in ``arr_instances.options``.

The global "default language profile for newly synced media"
(``settings.general.serie_default_enabled`` / ``serie_default_profile`` and the
movie equivalents) applies one profile to every instance. Someone running a
separate anime Sonarr next to a standard one needs a different default per
instance, so an instance may override the pair under
``options["media_defaults"]``.

This is a SIBLING of ``options["subtitle_settings"]``, deliberately not a
section inside it: a language profile is not a subtitle setting, and the two
blobs are validated, read and merged independently of one another.

Three states, and only three:

* no ``media_defaults`` key at all      -> inherit the global default
* ``{"default_enabled": False}``        -> assign no profile on this instance
* ``{"default_enabled": True, "default_profile": N}`` -> assign profile N

Absence is the whole contract. An instance without the block resolves to the
global value, so a single-instance install behaves exactly as it did before any
of this existed. Resolving an override where none is set would silently move
profile assignment for every such install on its next sync, which is the
failure this module is shaped to make impossible.

Precedence, highest first: a Sonarr/Radarr tag matching a language profile's
``tag`` (applied by the sync parsers when Series Tag / Movie Tag is enabled),
then this instance default, then the global default.
"""
import json

MEDIA_DEFAULTS_KEY = "media_defaults"


def _is_bool(value):
    return isinstance(value, bool)


def _is_profile_id(value):
    # bool is an int subclass; a True here would sail through as profile 1.
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value > 0)


ALLOWED = {
    "default_enabled": (_is_bool, "a boolean"),
    "default_profile": (_is_profile_id, "a positive language profile id or null"),
}


def validate_media_defaults(blob, known_profile_ids=None):
    """Validate a per-instance media_defaults override blob.

    Returns a cleaned dict holding only the recognised keys. Raises
    ``ValueError`` on an unknown key, a bad value, an incomplete override, or a
    profile id that is not in ``known_profile_ids`` (when the caller supplies
    the set), so the API answers 400 rather than persisting a dangling
    ``profileId`` that the sync would later have to defend against.

    An empty result means "no override": the instance inherits the global
    default. A disabled override drops any ``default_profile`` it was sent, so a
    stale id never lingers in the stored blob.
    """
    if blob is None:
        return {}
    if not isinstance(blob, dict):
        raise ValueError("media_defaults must be an object")

    cleaned = {}
    for key, value in blob.items():
        if key not in ALLOWED:
            raise ValueError(f"setting not allowed per instance: media_defaults.{key}")
        validator, constraint = ALLOWED[key]
        if not validator(value):
            raise ValueError(f"media_defaults.{key} must be {constraint}")
        cleaned[key] = value

    if not cleaned:
        return {}
    if "default_enabled" not in cleaned:
        raise ValueError("media_defaults.default_enabled is required")

    if cleaned["default_enabled"] is True:
        profile = cleaned.get("default_profile")
        if profile is None:
            raise ValueError(
                "media_defaults.default_profile is required when default_enabled is true")
        if known_profile_ids is not None and profile not in known_profile_ids:
            raise ValueError(f"language profile {profile} does not exist")
    else:
        # "assign no profile" carries no profile id.
        cleaned.pop("default_profile", None)
    return cleaned


def read_media_defaults(options_json):
    """Parse the media_defaults blob out of an instance ``options`` JSON string.

    Returns {} for null/blank/malformed input or when the key is absent, which
    is the "inherit the global default" state."""
    if not options_json:
        return {}
    try:
        options = json.loads(options_json)
    except (ValueError, TypeError):
        return {}
    if isinstance(options, dict) and isinstance(options.get(MEDIA_DEFAULTS_KEY), dict):
        return options[MEDIA_DEFAULTS_KEY]
    return {}


def merge_media_defaults_into_options(options_json, blob):
    """Return an ``options`` JSON string with media_defaults set to ``blob``,
    preserving any other keys already in options (notably subtitle_settings).
    An empty blob removes the media_defaults key (and returns None if nothing
    else remains), so clearing the override leaves no stale entry that could
    still read as one."""
    options = {}
    if options_json:
        try:
            existing = json.loads(options_json)
            if isinstance(existing, dict):
                options = existing
        except (ValueError, TypeError):
            options = {}
    if blob:
        options[MEDIA_DEFAULTS_KEY] = blob
    else:
        options.pop(MEDIA_DEFAULTS_KEY, None)
    return json.dumps(options) if options else None


def global_default_profile(enabled, profile):
    """Reduce the global default_enabled/default_profile pair to a profile id.

    Reproduces exactly what the sync sites did inline before this module
    existed, including the empty-string sentinel the config stores when the
    setting is on but no profile was ever picked.
    """
    if enabled is not True:
        return None
    if profile == "" or profile is None:
        return None
    return profile


def instance_default_profile(blob):
    """Return ``(has_override, profile_id)`` for a media_defaults blob.

    ``has_override`` is False for anything that is not a real override, and the
    caller must then fall back to the global value. Nothing else in the blob
    counts: only an explicit ``default_enabled`` makes this instance's default
    its own.
    """
    if not isinstance(blob, dict) or "default_enabled" not in blob:
        return False, None
    if blob.get("default_enabled") is not True:
        return True, None
    profile = blob.get("default_profile")
    if profile in ("", None):
        return True, None
    return True, profile
