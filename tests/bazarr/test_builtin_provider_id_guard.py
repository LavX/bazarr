# coding=utf-8
"""Pin the built-in provider id set.

New subtitle providers ship as Provider Hub catalog plugins, never as built-ins.
A built-in id is reserved against every untrusted catalog plugin, and a new
Bazarr settings section is reserved against every plugin, so a provider module,
registry entry or settings section that lands here without anyone deciding it
should would silently take an id away from the catalog. Each check below reads
what the application itself reads and fails naming the id or file that moved.
"""
from pathlib import Path

import subliminal_patch.providers  # importing it registers every built-in module

# The pin. Shrinking it is how a built-in is retired (move the id to
# RETIRED_BUILT_IN_PROVIDER_IDS in provider_hub/migration.py); growing it means a
# provider was added as a built-in, which should be a catalog plugin instead.
PINNED_BUILT_IN_PROVIDER_IDS = frozenset({
    "addic7ed",
    "animekalesi",
    "animesubinfo",
    "animetosho",
    "assrt",
    "avistaz",
    "betaseries",
    "bsplayer",
    "cinemaz",
    "embeddedsubtitles",
    "gestdown",
    "greeksubs",
    "greeksubtitles",
    "hdbits",
    "jimaku",
    "karagarga",
    "ktuvit",
    "legendasdivx",
    "legendasnet",
    "napiprojekt",
    "napisy24",
    "nekur",
    "opensubtitles",
    "opensubtitlescom",
    "pipocas",
    "prijevodionline",
    "regielive",
    "shooter",
    "soustitreseu",
    "subclub",
    "subdl",
    "subf2m",
    "subs4free",
    "subs4series",
    "subsarr",
    "subsource",
    "subsro",
    "subssabbz",
    "subsunacs",
    "subsynchro",
    "subtis",
    "subtitrarinoi",
    "subtitriid",
    "subtitulamostv",
    "subx",
    "supersubtitles",
    "titlovi",
    "titrari",
    "titulky",
    "turkcealtyaziorg",
    "tvsubtitles",
    "whisperai",
    "wizdom",
    "yavkanet",
    "yifysubtitles",
    "zimuku",
})

# Modules in the providers directory that the registration loop imports but that
# define no provider of their own.
PINNED_PROVIDER_HELPER_MODULES = frozenset({
    "_agent_list",
    "avistaz_network",
    "opensubtitles_scraper",
})

# Files the registration loop in subliminal_patch/providers/__init__.py skips.
_REGISTRATION_SKIPPED_FILES = frozenset({"__init__.py", "mixins.py", "utils.py"})

# Bazarr's own settings sections, the ones reserved_settings_sections() keeps any
# plugin from claiming. A provider's settings section must not be added here: it
# belongs to the catalog plugin, which declares its own config_schema.
PINNED_RESERVED_SETTINGS_SECTIONS = frozenset({
    "anidb",
    "anticaptcha",
    "auth",
    "backup",
    "captchaai",
    "compat_endpoint",
    "cors",
    "deathbycaptcha",
    "discover",
    "emby",
    "general",
    "jellyfin",
    "log",
    "ocr",
    "omdb",
    "plex",
    "postgresql",
    "proxy",
    "radarr",
    "seerr",
    "silo",
    "sonarr",
    "sportarr",
    "subsync",
    "translator",
})

_CATALOG_HINT = (
    "New providers ship as catalog plugins, not built-ins. If this change is "
    "intended (a retirement, or a deliberate exception), update the pin in "
    "tests/bazarr/test_builtin_provider_id_guard.py."
)


def _diff_message(what, actual, pinned):
    added = sorted(set(actual) - set(pinned))
    removed = sorted(set(pinned) - set(actual))
    lines = [f"{what} no longer matches the pin."]
    if added:
        lines.append(f"  not on the pin: {', '.join(added)}")
    if removed:
        lines.append(f"  pinned but missing: {', '.join(removed)}")
    lines.append(_CATALOG_HINT)
    return "\n".join(lines)


def test_registry_holds_exactly_the_pinned_built_in_ids():
    # The dynamic half of the built-in set, computed as provider_hub's shadow gate
    # computes it: everything registered, minus the hub plugins that registered
    # themselves into the same registry.
    from provider_hub.registry import _REGISTERED_PROVIDER_HUB_IDS
    from subliminal_patch.extensions import provider_registry

    registered = set(provider_registry.names()) - _REGISTERED_PROVIDER_HUB_IDS

    assert registered == PINNED_BUILT_IN_PROVIDER_IDS, _diff_message(
        "subliminal_patch provider_registry", registered, PINNED_BUILT_IN_PROVIDER_IDS
    )


def test_hub_built_in_id_set_is_the_pin_plus_retired_ids():
    # The set the Provider Hub install and trust checks actually consult, which also
    # carries the migration allowlist. Retired ids are pinned by their own tests.
    from provider_hub.migration import RETIRED_BUILT_IN_PROVIDER_IDS
    from provider_hub.service import _built_in_provider_ids

    built_in = _built_in_provider_ids() - RETIRED_BUILT_IN_PROVIDER_IDS

    assert built_in == PINNED_BUILT_IN_PROVIDER_IDS, _diff_message(
        "provider_hub built-in provider ids", built_in, PINNED_BUILT_IN_PROVIDER_IDS
    )


def test_providers_directory_holds_only_pinned_modules():
    providers_dir = Path(subliminal_patch.providers.__file__).resolve().parent
    files = {
        path.name: path
        for path in providers_dir.iterdir()
        if path.suffix == ".py" and path.name not in _REGISTRATION_SKIPPED_FILES
    }
    pinned = PINNED_BUILT_IN_PROVIDER_IDS | PINNED_PROVIDER_HELPER_MODULES
    stems = {Path(name).stem for name in files}

    unexpected = sorted(str(files[name]) for name in files if Path(name).stem not in pinned)
    assert not unexpected, (
        "provider module file(s) not on the pin, each would register as a built-in:\n  "
        + "\n  ".join(unexpected)
        + "\n"
        + _CATALOG_HINT
    )
    assert stems == pinned, _diff_message(f"provider modules in {providers_dir}", stems, pinned)


def test_reserved_settings_sections_match_the_pin():
    # Read off the settings validators by the same function the manifest check uses,
    # so a new section, provider-shaped or not, is named here before it reserves an
    # id a catalog plugin already owns.
    from provider_hub.manifest import reserved_settings_sections

    reserved = reserved_settings_sections()

    assert reserved == PINNED_RESERVED_SETTINGS_SECTIONS, _diff_message(
        "settings validator sections reserved from plugins",
        reserved,
        PINNED_RESERVED_SETTINGS_SECTIONS,
    )
