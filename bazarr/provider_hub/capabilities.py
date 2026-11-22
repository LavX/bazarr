# coding=utf-8
"""What a plugin can ask the host for, and what the host hands it.

A plugin names the capabilities it needs in ``requires``. The older catalog flags
``flaresolverr`` and ``anti_captcha`` still count as the same declarations. Each
capability the host knows is delivered one of two ways:

* ``url``: the host reserves the plugin's configuration key its kind names in
  ``capability_delivery`` (FlareSolverr) and fills it for the length of each call, with
  the address of a lease (``CallLeases``, ``leases``). The address is never stored in the
  configuration a pool holds.
* ``call``: the plugin asks the host over the worker's own pipes while it serves a
  request, and the host does the work (the captcha solver). The credentials stay with
  the host and the worker needs no network path back to Bazarr+. See ``worker.py``.

Nothing here raises into a caller. A capability that cannot be bound is reported as
not ready, with a reason, and the search goes on without it.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import endpoints, leases, service_health
from .kinds import SUBTITLE_PROVIDER, get_kind, kind_of

logger = logging.getLogger(__name__)

FLARESOLVERR = endpoints.FLARESOLVERR_CAPABILITY
HOST_CAPTCHA = "host.captcha.v1"

DELIVERY_URL = "url"
DELIVERY_CALL = "call"

SOURCE_SETTING = "setting"
SOURCE_ENV = "env"
SOURCE_HOST = "host"
SOURCE_MANAGED = leases.SOURCE_MANAGED
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
    # Where the answer came from: "setting" or "env" (a service the user pointed Bazarr+ at), "managed" (one Bazarr+
    # runs), "host" or "none".
    source: str
    ready: bool
    # A short, fixed sentence for the card when the capability is not ready.
    reason: str = ""
    # A service someone else runs: the card says so, and shows the last health result.
    external: bool = False
    health: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        found = {"capability": self.capability, "source": self.source, "ready": self.ready, "reason": self.reason}
        if self.external:
            found["external"] = True
        if self.health is not None:
            found["health"] = dict(self.health)
        return found


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
            # An address the user typed wins over a service Bazarr+ runs: detection and installs suggest, they never
            # take over.
            url, source = endpoints.flaresolverr_url_source(settings)
            if url:
                return Binding(capability, source, True, external=True, health=service_health.last(capability, url))
            managed = leases.managed_service_for(capability)
            if managed is not None:
                if managed.ready():
                    return Binding(capability, SOURCE_MANAGED, True)
                return Binding(capability, SOURCE_MANAGED, False, "The managed FlareSolverr is not running yet.")
            return Binding(capability, SOURCE_NONE, False, "No FlareSolverr address is set.")
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


def ready_url_fields(installation, fields: set[str], settings=None) -> set[str]:
    """Which of these configuration fields can be filled by a service lease."""
    delivery = get_kind(kind_of(installation)).capability_delivery
    return {key for capability, (key, shape) in delivery.items()
            if shape == DELIVERY_URL and key in fields and resolve(capability, installation, settings).ready}


def deliver(config: dict, properties: dict, installation, settings) -> None:
    """Reserve the key of each ``url`` capability in the plugin's configuration.

    Runs after the stored config, the schema defaults and the plugin's own settings
    section are merged, so a value the user set for the provider is never replaced. The
    host does not write an address here: it is filled for each call from a lease
    (``CallLeases``), so one that is leased cannot outlive its call and one that was
    changed applies at once. The key is set to an empty string, so that a pool holding an
    address from before drops it. A plugin whose schema has no such key, or that may not
    receive the capability, is left as it is.
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
        # Assigned even though there is nothing to put here: a persistent pool merges the new config over the one
        # it holds, so an absent key would keep an old address.
        config[key] = ""


def _read_installations() -> dict[str, Any]:
    try:
        from . import state

        installations = state.load_state().get("installations")
        return installations if isinstance(installations, dict) else {}
    except Exception as error:
        logger.warning("Could not read the plugin installations (%s)", type(error).__name__)
        return {}


def live_row(provider_id, *, installations: Mapping[str, Any] | None = None) -> dict | None:
    """The installation in force, from a fresh read or the snapshot held by one provider call."""
    try:
        if installations is None:
            installations = _read_installations()
        row = installations.get(str(provider_id))
    except Exception:
        logger.exception("Could not read the record of %s", provider_id)
        return None
    if not isinstance(row, dict):
        return None
    # A release that was detached (removed, revoked or held back) is in force no longer, though its row still
    # says what it said: a worker that is finishing its calls gets nothing more from the host.
    if row.get("revoked") or row.get("blocked") or row.get("state") == "removed":
        return None
    return row


def live_trust(provider_id) -> bool:
    """Whether the installation is trusted right now, read from the stored record. An unreadable record, or none,
    is not trust."""
    row = live_row(provider_id)
    return row is not None and row.get("trusted") is True


def host_call_handlers(installation) -> dict[str, Callable[[dict, float, threading.Event], dict]]:
    """The functions that answer a worker's calls, for the ``call`` capabilities it may use.

    Built when a worker is made, for a plugin that declares the capability and is trusted then. Each call
    asks again, against the stored record, and is refused once the plugin has lost its trust; the solver
    re-reads the settings as well, so a key the user changes applies to the next call.
    """
    handlers: dict[str, Callable[[dict, float, threading.Event], dict]] = {}
    if _eligible(HOST_CAPTCHA, installation):
        from . import captcha

        provider_id = installation.get("provider_id") if isinstance(installation, dict) else getattr(
            installation, "provider_id", None)

        def trusted_now() -> bool:
            return live_trust(provider_id)

        def solve(payload, timeout, cancelled):
            return captcha.solve_call(payload, timeout, cancelled, allowed=trusted_now)

        handlers[HOST_CAPTCHA] = solve
    return handlers


def open_lease(capability: str, installation, settings=None, *, owner: str,
               installations: Mapping[str, Any] | None = None) -> leases.Lease | None:
    """A lease on the service that provides ``capability``, for one holder, or None. Never raises.

    The same rules as ``resolve``: the plugin must be one the host may hand the capability to, an address the user
    typed wins, and a service Bazarr+ runs comes next. None means the call goes on without the service, as it did
    before there was one. Only the kind of a failure is logged, because an address or a token may be in its text.
    """
    try:
        if capability not in CAPABILITIES or CAPABILITIES[capability] != DELIVERY_URL:
            return None
        if not _eligible(capability, installation):
            return None
        if capability == FLARESOLVERR:
            url, source = endpoints.flaresolverr_url_source(settings)
            if url:
                service_health.note_use(capability, url)
                return leases.DirectLease(capability, owner, url, source)
            return leases.open_managed(capability, owner, installations=installations)
    except Exception as error:
        logger.warning("Could not lease %s (%s)", capability, type(error).__name__)
    return None


def _offered(capability: str, settings=None) -> bool:
    """Whether there is any service to lease for ``capability`` right now, judged without reading the state."""
    try:
        if capability == FLARESOLVERR and endpoints.flaresolverr_url_source(settings)[0]:
            return True
        return leases.has_managed(capability)
    except Exception:
        return False


class CallLeases:
    """The leases one provider call holds. The call asks for its configuration through ``config`` and ends with ``close``.

    A lease is opened the first time a request needs the address, and shared by the requests of the same call (a
    download is a request and then a selection inside the archive). ``close`` gives them all back, and runs whatever
    way the call ended. A provider with nothing to lease pays for nothing: no state is read and no lease is made.
    """

    def __init__(self, provider_id: str, manifest: dict, wanted: "list[tuple[str, str]]", owner: str, settings=None):
        self._provider_id = provider_id
        self._manifest = manifest
        self._wanted = wanted
        self._owner = owner
        self._settings = settings
        self._leases: dict[str, leases.Lease | None] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._installations: dict[str, Any] | None = None

    @classmethod
    def for_provider(cls, provider_id: str, manifest, owner: str, settings=None) -> "CallLeases":
        """The scope for a call of a provider running ``manifest`` (the validated manifest of its release), held by
        ``owner``. A manifest that names no key to fill, or none at all, gives a scope that does nothing."""
        raw = getattr(manifest, "raw", None)
        if not isinstance(raw, dict):
            return cls(provider_id, {}, [], owner, settings)
        schema = raw.get("config_schema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            return cls(provider_id, raw, [], owner, settings)
        try:
            delivery = get_kind(SUBTITLE_PROVIDER).capability_delivery
        except Exception:
            return cls(provider_id, raw, [], owner, settings)
        wanted = [(capability, key) for capability, (key, shape) in delivery.items()
                  if shape == DELIVERY_URL and key in properties]
        return cls(provider_id, raw, wanted, owner, settings)

    def _lease_for(self, capability: str) -> leases.Lease | None:
        with self._lock:
            if self._closed:
                return None
            if capability in self._leases:
                return self._leases[capability]
            if not _offered(capability, self._settings):
                # No typed address or registered service: no lease and no state read.
                self._leases[capability] = None
                return None
            if self._installations is None:
                # Read once for both the provider and the managed service it may lease.
                self._installations = _read_installations()
            installations = self._installations
        row = live_row(self._provider_id, installations=installations)
        # Use the declarations of the release being run and the trust from the current snapshot.
        lease = None
        if row is not None:
            installation = {
                "provider_id": self._provider_id, "manifest": self._manifest, "trusted": row.get("trusted") is True}
            lease = open_lease(capability, installation, self._settings, owner=self._owner, installations=installations)
        with self._lock:
            if self._closed or capability in self._leases:
                if lease is not None:
                    lease.close()
                return self._leases.get(capability)
            self._leases[capability] = lease
        return lease

    def config(self, config: dict) -> dict:
        """The configuration of a request: ``config`` with each key the host fills set to the address of a lease.

        A key the user gave a value is left as it is. With no lease to give, the key is left empty and the provider
        works without the service."""
        if not self._wanted or self._closed:
            return config
        filled = None
        for capability, key in self._wanted:
            if config.get(key) not in (None, "", [], {}):
                continue
            lease = self._lease_for(capability)
            if lease is None:
                continue
            try:
                address = lease.url()
            except Exception as error:
                logger.warning("The %s lease of %s could not give an address (%s)", capability,
                               self._provider_id, type(error).__name__)
                continue
            if filled is None:
                filled = dict(config)
            filled[key] = address
        return config if filled is None else filled

    def close(self) -> None:
        """Give every lease back. Safe to call twice, and after the leases were closed from elsewhere."""
        with self._lock:
            self._closed = True
            held = [lease for lease in self._leases.values() if lease is not None]
            self._leases = {}
            self._installations = None
        for lease in held:
            lease.close()
