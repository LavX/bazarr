# coding=utf-8
"""Can this Bazarr+ run a catalog entry, and which entry is the one to use.

An entry is compatible when this host defines its ``kind_api``, meets its
``min_host_version`` (full semantic-version order, so 2.8.0-rc.1 is below
2.8.0), runs on one of its ``platforms`` and offers its runtime. Selection
picks the highest compatible version among the entries a caller is allowed
to use.

The catalog listing, the install paths and the update and autoinstall selection
call this module. An entry it refuses is listed as blocked, cannot be installed
and is not chosen as an update. An entry that needs a runtime outside
AVAILABLE_RUNTIMES is refused on every host.
"""
from __future__ import annotations

import glob
import logging
import os
import platform
import sys

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from secret_store.scrub import scrub_text

from .manifest import ManifestValidationError
from .plugin_manifest import ANY_PLATFORM, PYTHON_BUNDLE, STDIO_WORKER, PluginManifest, parse_plugin_manifest
from .versions import host_version_key, version_key

logger = logging.getLogger(__name__)

# The runtimes this host can start. New runtimes are added here once the host can start them.
# An entry that needs a runtime not listed here is refused on every host.
AVAILABLE_RUNTIMES = frozenset({(PYTHON_BUNDLE, STDIO_WORKER)})

# The most characters of validator or failure text that is recorded or shown.
REFUSAL_TEXT_MAX_CHARS = 200


def refusal_text(text: Any) -> str:
    """Text for a record or a listing: scrubbed first, then cut.

    The cut comes second so a credential is masked whole before any of it can
    fall at the boundary, where a half of it would no longer match a pattern.
    """
    return scrub_text(str(text))[:REFUSAL_TEXT_MAX_CHARS]


@dataclass(frozen=True)
class HostFacts:
    version: str | None
    platform: str
    runtimes: frozenset[tuple[str, str]]
    python_tag: str = f"cp{sys.version_info.major}{sys.version_info.minor}"
    # Whether Bazarr+ runs in a container. Only the oci gate in evaluate reads this flag.
    in_container: bool = False


def _machine() -> str:
    machine = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
        "armv8l": "aarch64",
        "armv7": "armv7l",
        "i386": "i686",
        "x86": "i686",
    }.get(machine, machine)


def host_platform() -> str:
    machine = _machine()
    if sys.platform.startswith("linux"):
        libc, _version = platform.libc_ver()
        musl = libc != "glibc" and bool(glob.glob("/lib/ld-musl-*.so.1"))
        return f"linux-musl-{machine}" if musl else f"linux-{machine}"
    if sys.platform == "win32":
        return f"windows-{machine}"
    if sys.platform == "darwin":
        return "macos-arm64" if machine == "aarch64" else f"macos-{machine}"
    return f"{sys.platform}-{machine}"


def running_in_container() -> bool:
    """Whether Bazarr+ runs inside a container (Docker, Podman or containerd)."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            cgroups = handle.read()
    except OSError:
        return False
    return any(marker in cgroups for marker in ("docker", "containerd", "kubepods", "libpod"))


def current_host() -> HostFacts:
    return HostFacts(
        version=os.environ.get("BAZARR_VERSION") or None,
        platform=host_platform(),
        runtimes=AVAILABLE_RUNTIMES,
        in_container=running_in_container(),
    )


_REPORTED_UNVERSIONED_HOST = False


def host_meets(host_version: str | None, min_host_version: str | None) -> bool:
    global _REPORTED_UNVERSIONED_HOST
    if not min_host_version:
        return True
    host_key = host_version_key(host_version)
    if host_key is None:
        # A source checkout with no VERSION file. There is nothing to compare,
        # and refusing every entry would make such a checkout useless.
        if not _REPORTED_UNVERSIONED_HOST:
            _REPORTED_UNVERSIONED_HOST = True
            logger.info("Bazarr+ has no version, so plugin minimum versions are not checked")
        return True
    return host_key >= version_key(min_host_version)


def evaluate(plugin: PluginManifest, host: HostFacts) -> tuple[bool, str | None]:
    from .kinds import kind_for_api

    kind = kind_for_api(plugin.kind_api)
    if kind is None or kind.name != plugin.kind:
        return False, f"this Bazarr+ does not support {plugin.kind_api}"
    if not host_meets(host.version, plugin.min_host_version):
        return False, f"needs Bazarr+ {plugin.min_host_version} or later"
    if ANY_PLATFORM not in plugin.platforms and host.platform not in plugin.platforms:
        return False, f"not available for {host.platform}"
    if plugin.python_tags and "py3" not in plugin.python_tags and host.python_tag not in plugin.python_tags:
        return False, f"not available for Python {host.python_tag}"
    if (plugin.artifact, plugin.process) not in host.runtimes:
        return False, f"needs the {plugin.artifact} {plugin.process} runtime, which this Bazarr+ does not provide"
    # Reached only for an oci runtime that the host offers.
    if plugin.artifact == "oci" and not host.in_container:
        return False, "a managed container needs Bazarr+ itself to run in a container"
    return True, None


def entry_compatibility(entry: dict[str, Any], host: HostFacts) -> tuple[bool, str | None]:
    """Compatibility of a stored catalog entry, re-evaluated for this host.

    The stored manifest is evaluated against this host on every call, so a host
    that was upgraded since the catalog was refreshed, or that installs from the
    cached catalog during an upgrade, gets its own answer.
    """
    try:
        plugin = parse_plugin_manifest(entry.get("manifest"))
    except ManifestValidationError as error:
        return False, refusal_text(error)
    return evaluate(plugin, host)


def entry_plugin_id(entry: dict[str, Any]) -> str:
    return str(entry.get("provider_id") or "")


def select_entry(
    entries: Iterable[dict[str, Any]],
    plugin_id: str,
    *,
    usable: Callable[[dict[str, Any]], bool],
    source_id: str | None = None,
    above_version: Any = None,
) -> dict[str, Any] | None:
    """The highest version of ``plugin_id`` among the entries ``usable`` accepts.

    ``source_id`` limits the choice to one catalog source, which is how an
    installed plugin stays bound to the source it came from. ``above_version``
    keeps only strictly newer versions, which is what an update needs.
    """
    floor = version_key(above_version) if above_version is not None else None
    best: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    for entry in entries:
        if not isinstance(entry, dict) or entry_plugin_id(entry) != plugin_id:
            continue
        if source_id is not None and entry.get("source") != source_id:
            continue
        key = version_key((entry.get("manifest") or {}).get("version") or entry.get("version"))
        if key is None or (floor is not None and key <= floor):
            continue
        if not usable(entry):
            continue
        if best is None or key > best[0]:
            best = (key, entry)
    return best[1] if best else None
