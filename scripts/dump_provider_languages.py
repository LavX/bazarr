#!/usr/bin/env python3
"""Dump per-provider supported languages to JSON.

Imports each provider class from custom_libs/subliminal_patch/providers/ and
emits a JSON map: { provider_id: [{ code: 'xxx', name: 'English' }, ...] }.

Run inside the bazarr container where all deps are installed.
"""

import importlib
import json
import os
import sys
import traceback
from pathlib import Path

# Adjust to the bazarr root inside the container
BAZARR_ROOT = Path(os.environ.get("BAZARR_ROOT", "/app"))
PROVIDERS_DIR = BAZARR_ROOT / "custom_libs" / "subliminal_patch" / "providers"
OUT_PATH = Path(os.environ.get("OUT_PATH", "/tmp/provider-languages.json"))

# Ensure bazarr libs are importable
for p in [
    str(BAZARR_ROOT / "custom_libs"),
    str(BAZARR_ROOT / "libs"),
    str(BAZARR_ROOT / "bazarr"),
    str(BAZARR_ROOT),
]:
    if p not in sys.path:
        sys.path.insert(0, p)


def language_to_dict(lang) -> dict:
    """Convert a babelfish.Language to a serialisable dict."""
    try:
        code = lang.alpha3
    except Exception:
        code = str(lang)
    country = getattr(lang, "country", None)
    country_code = country.alpha2 if country else None
    try:
        name = lang.name
    except Exception:
        name = code
    out = {"code": code, "name": name}
    if country_code:
        out["country"] = country_code
        out["display"] = f"{name} ({country_code})"
    else:
        out["display"] = name
    return out


def discover_provider_modules():
    for entry in sorted(PROVIDERS_DIR.iterdir()):
        if not entry.name.endswith(".py"):
            continue
        if entry.name.startswith("_"):
            continue
        yield entry.stem


def main():
    if not PROVIDERS_DIR.exists():
        print(f"providers dir not found: {PROVIDERS_DIR}", file=sys.stderr)
        sys.exit(1)

    result: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    for module_name in discover_provider_modules():
        full = f"subliminal_patch.providers.{module_name}"
        try:
            mod = importlib.import_module(full)
        except Exception as e:  # noqa: BLE001
            errors[module_name] = f"{type(e).__name__}: {e}"
            continue

        # Find provider classes in the module
        provider_classes = []
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if not isinstance(obj, type):
                continue
            if obj.__module__ != full:
                continue
            langs = getattr(obj, "languages", None)
            if langs is None:
                continue
            # Skip if it's clearly inherited and not a set/frozenset
            if not hasattr(langs, "__iter__"):
                continue
            provider_classes.append((attr, langs))

        if not provider_classes:
            continue

        # Take the largest language set among the classes in the module
        attr, langs = max(provider_classes, key=lambda x: len(list(x[1])))
        try:
            lang_dicts = [language_to_dict(l) for l in langs]
        except Exception as e:  # noqa: BLE001
            errors[module_name] = f"{type(e).__name__}: {e}"
            continue

        # Dedupe by display
        seen = set()
        unique = []
        for d in lang_dicts:
            key = d.get("display")
            if key and key not in seen:
                seen.add(key)
                unique.append(d)
        unique.sort(key=lambda d: d["display"])
        result[module_name] = unique

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"wrote {len(result)} providers to {OUT_PATH}")
    if errors:
        print(f"\n{len(errors)} providers failed to introspect:", file=sys.stderr)
        for k, v in sorted(errors.items()):
            print(f"  {k}: {v}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(2)
