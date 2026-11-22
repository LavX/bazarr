# coding=utf-8
"""The service kind: plugins that run something other plugins need, such as FlareSolverr.

A service ``provides`` capabilities (``flaresolverr.v1``). A plugin that ``requires`` one gets the address of
a service for the length of each call, through a lease (see ``provider_hub.leases``), and never keeps it. Where the
service runs is one of two things:

* a service the user runs and points Bazarr+ at: a remote binding, an address with a Test, a health status and an
  "External" label. Bazarr+ never starts, stops or upgrades it, and it is not a plugin at all;
* a service plugin Bazarr+ runs itself, a managed container behind its authenticating front. That tier is dormant
  here: nothing in this kind starts a container, and a daemon runtime is offered only where the Docker
  runtime is switched on (``BAZARR_MANAGED_CONTAINERS``). What starts one registers it in ``leases``.

A service is switched off, removed or revoked live. The detach closes every lease the service gave out, so no
token outlives it, and stops what it runs.
"""
from __future__ import annotations

import logging

from . import ATTACH_ON_RESTART, DETACH_LIVE, SERVICE, Kind, register_kind

logger = logging.getLogger(__name__)

SERVICE_KIND_API = "bazarr.service.v1"


class ServiceKind(Kind):
    name = SERVICE
    kind_apis = frozenset({SERVICE_KIND_API})
    # A new release starts at the next restart. A service that is switched off, removed or revoked stops now.
    attach_policy = ATTACH_ON_RESTART
    detach_policy = DETACH_LIVE
    noun = "service"

    def detach(self, plugin_id: str, *, withdraw: bool, grace: float | None = None,
               release_key: str | None = None):
        """Nothing new is leased from the service, the leases it gave out are closed and what it runs is stopped.

        ``grace`` does not apply: a lease on a service that is going away is not left to run out, the way a call
        of a provider may be. The calls that held one fail on their next request to it. The returned thread ends
        when what the service ran has stopped."""
        from .. import leases

        return leases.detach_service(plugin_id)

    def validate_manifest(self, manifest, trusted: bool):
        from ..manifest import ManifestValidationError, validate_plugin_bundle
        from ..plugin_manifest import parse_plugin_manifest

        validated = validate_plugin_bundle(manifest)
        if validated.kind != SERVICE or validated.api_version != SERVICE_KIND_API:
            raise ManifestValidationError(f"this is not a {SERVICE_KIND_API} manifest")
        if not parse_plugin_manifest(manifest).provides:
            raise ManifestValidationError("a service must provide at least one capability, such as flaresolverr.v1")
        return validated


SERVICE_KIND = register_kind(ServiceKind())
