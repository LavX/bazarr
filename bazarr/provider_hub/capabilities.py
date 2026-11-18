# coding=utf-8
"""What a plugin can ask the host for, and what the host hands it.

A plugin names the capabilities it needs in ``requires``. The older catalog flags
``flaresolverr`` and ``anti_captcha`` still count as the same declarations. Each
capability the host knows is delivered one of two ways:

* ``url``: the host puts an address in the plugin's configuration under the key its
  kind names in ``capability_delivery`` (FlareSolverr).
* ``call``: the plugin asks the host over the worker's own pipes while it serves a
  request, and the host does the work (the captcha solver). The credentials stay with
  the host and the worker needs no network path back to Bazarr+. See ``worker.py``.

Nothing here raises into a caller. A capability that cannot be bound is reported as
not ready, with a reason, and the search goes on without it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import endpoints
from .kinds import get_kind, kind_of

logger = logging.getLogger(__name__)

FLARESOLVERR = endpoints.FLARESOLVERR_CAPABILITY
HOST_CAPTCHA = "host.captcha.v1"

DELIVERY_URL = "url"
DELIVERY_CALL = "call"

SOURCE_SETTING = "setting"
SOURCE_ENV = "env"
SOURCE_HOST = "host"
SOURCE_NONE = "none"

_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\.v[1-9][0-9]*$")

# Capability id to how it is delivered. A capability a plugin requires that is not here is
# someone else's to provide (another plugin) and is not bound by the host.
CAPABILITIES: Mapping[str, str] = {
    FLARESOLVERR: DELIVERY_URL,
    HOST_CAPTCHA: DELIVERY_CALL,
}

# Catalog manifests that predate ``requires`` say the same thing with a flag.
_LEGACY_FLAGS = {"flaresolverr": FLARESOLVERR, "anti_captcha": HOST_CAPTCHA}


@dataclass(frozen=True)
class Binding:
    capability: str
    # Where the answer came from: "setting", "env", "host" or "none".
    source: str
    ready: bool
    # A short, fixed sentence for the card when the capability is not ready.
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "source": self.source, "ready": self.ready, "reason": self.reason}


def _manifest_of(installation) -> dict:
    manifest = installation.get("manifest") if isinstance(installation, dict) else getattr(installation, "manifest", None)
    return manifest if isinstance(manifest, dict) else {}


def declared_capabilities(manifest) -> tuple[str, ...]:
    """The capabilities the host binds that a manifest declares, in a fixed order."""
    if not isinstance(manifest, dict):
        return ()
    found = set()
    requires = manifest.get("requires")
    if isinstance(requires, (list, tuple)):
        for item in requires:
            if isinstance(item, str) and _CAPABILITY_RE.match(item) and item in CAPABILITIES:
                found.add(item)
    for flag, capability in _LEGACY_FLAGS.items():
        if manifest.get(flag) is True:
            found.add(capability)
    return tuple(sorted(found))


def _trusted(installation) -> bool:
    value = installation.get("trusted") if isinstance(installation, dict) else getattr(installation, "trusted", False)
    return value is True


def _eligible(capability: str, installation) -> bool:
    """Whether the host may hand this capability to the plugin at all.

    FlareSolverr fetches any address it is given and a captcha service spends the user's
    balance, so neither goes to a plugin that merely has a field of the right name.
    """
    if capability == FLARESOLVERR:
        return endpoints.may_receive_flaresolverr(installation)
    if capability == HOST_CAPTCHA:
        return _trusted(installation) and capability in declared_capabilities(_manifest_of(installation))
    return False


def resolve(capability: str, installation, settings=None) -> Binding:
    """Whether and how the host can supply ``capability`` to this plugin. Never raises."""
    try:
        if not _eligible(capability, installation):
            return Binding(capability, SOURCE_NONE, False, "This capability is only offered to trusted plugins.")
        if capability == FLARESOLVERR:
            url, source = endpoints.flaresolverr_url_source(settings)
            if not url:
                return Binding(capability, SOURCE_NONE, False, "No FlareSolverr address is set.")
            return Binding(capability, source, True)
        if capability == HOST_CAPTCHA:
            from . import captcha

            if captcha.configured_vendor(settings) is None:
                return Binding(capability, SOURCE_NONE, False, "No captcha service is set up.")
            return Binding(capability, SOURCE_HOST, True)
    except Exception:
        logger.exception("Could not resolve %s", capability)
    return Binding(capability, SOURCE_NONE, False, "This capability is not available.")


def binding_report(installation, settings=None) -> list[dict[str, Any]]:
    """One entry per declared capability, for the plugin's card."""
    declared = declared_capabilities(_manifest_of(installation))
    return [resolve(capability, installation, settings).as_dict() for capability in declared]


def deliver(config: dict, properties: dict, installation, settings) -> None:
    """Hand each ``url`` capability to the plugin through its configuration.

    Runs after the stored config, the schema defaults and the plugin's own settings
    section are merged, so a value the user set for the provider is never replaced. With
    no address to give, the key is set to an empty string, so that a pool holding an older
    address drops it. A plugin whose schema has no such key, or that may not receive the
    capability, is left as it is.
    """
    try:
        delivery = get_kind(kind_of(installation)).capability_delivery
    except Exception:
        return
    for capability, (key, shape) in delivery.items():
        if shape != DELIVERY_URL or key not in properties:
            continue
        if config.get(key) not in (None, "", [], {}):
            continue
        if not _eligible(capability, installation):
            continue
        # Assigned even when there is nothing to give: a persistent pool merges the new
        # config over the one it holds, so an absent key would keep the old address.
        config[key] = endpoints.resolve_flaresolverr_url(settings) if capability == FLARESOLVERR else ""


def host_call_handlers(installation) -> dict[str, Callable[[dict, float], dict]]:
    """The functions that answer a worker's calls, for the ``call`` capabilities it may use.

    Looked up when a worker is built. The solver re-reads the settings on every call, so a
    key the user changes applies to the next call without a new worker.
    """
    handlers: dict[str, Callable[[dict, float], dict]] = {}
    if _eligible(HOST_CAPTCHA, installation):
        from . import captcha

        handlers[HOST_CAPTCHA] = captcha.solve_call
    return handlers
