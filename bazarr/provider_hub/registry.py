# coding=utf-8
from __future__ import annotations

import logging
import json
import sys

from pathlib import Path

from subliminal.video import Episode, Movie
from subliminal_patch.extensions import provider_registry
from subliminal_patch.providers import Provider

from .manifest import ManifestValidationError, validate_manifest
from .migration import (
    can_shadow_built_in_provider,
    validation_built_in_provider_ids,
)
from .protocol import candidate_from_worker, language_to_payload, video_to_payload, worker_download_to_content
from .state import active_installations
from .worker import ProviderWorkerClient, WorkerError, worker_command

logger = logging.getLogger(__name__)

_REGISTERED_PROVIDER_HUB_IDS: set[str] = set()


class HubProxyProvider(Provider):
    provider_name = "providerhub"
    languages = set()
    video_types = (Episode, Movie)
    subtitle_class = None

    def __init__(self, timeout=30, worker_client=None, **config):
        self.timeout = int(timeout)
        self.worker_client = worker_client or getattr(self.__class__, "worker_client", None)
        self.config = config

    def initialize(self):
        return True

    def terminate(self):
        if self.worker_client:
            self.worker_client.stop()

    @classmethod
    def check(cls, video):
        return isinstance(video, cls.video_types)

    def _worker(self):
        if self.worker_client:
            return self.worker_client
        bundle_path = getattr(self.__class__, "bundle_path", None)
        python_path = getattr(self.__class__, "python_path", None) or sys.executable
        manifest_json = getattr(self.__class__, "manifest_json", None)
        if bundle_path and manifest_json:
            runner = Path(__file__).with_name("worker_runner.py")
            self.worker_client = ProviderWorkerClient(
                worker_command(python_path, runner),
                cwd=bundle_path,
                env={
                    "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle_path),
                    "BAZARR_PROVIDER_HUB_MANIFEST": manifest_json,
                },
            )
            return self.worker_client
        raise WorkerError("Provider Hub worker is not configured")

    def list_subtitles(self, video, languages):
        request = {
            "provider": self.provider_name,
            "config": self.config,
            "video": video_to_payload(video),
            "languages": [language_to_payload(item) for item in languages],
        }
        result = self._worker().request("search", request, timeout=self.timeout)
        return [
            candidate_from_worker(self.provider_name, item)
            for item in result.payload.get("candidates", [])
        ]

    def download_subtitle(self, subtitle):
        request = {
            "provider": self.provider_name,
            "provider_payload": subtitle.provider_payload,
            "language": language_to_payload(subtitle.language),
            "config": self.config,
        }
        result = self._worker().request("download", request, timeout=self.timeout)
        worker_download_to_content(subtitle, result.payload)
        return True


def _languages_from_manifest(manifest):
    from subzero.language import Language

    languages = set()
    for code in manifest.languages:
        try:
            languages.add(Language.fromietf(code))
        except Exception:
            languages.add(Language(code))
    return languages


def _make_provider_class(manifest, worker_client=None, installation=None):
    attrs = {
        "provider_name": manifest.provider_id,
        "languages": _languages_from_manifest(manifest),
        "video_types": tuple(
            item for media in manifest.supported_media
            for item in ((Movie,) if media == "movie" else (Episode,))
        ),
        "manifest": manifest,
        "manifest_json": json.dumps(manifest.raw),
    }
    if worker_client is not None:
        attrs["worker_client"] = worker_client
    if installation is not None:
        if installation.active_path:
            attrs["bundle_path"] = installation.active_path
        if getattr(installation, "python_path", None):
            attrs["python_path"] = installation.python_path
    return type(f"{manifest.provider_id.title().replace('_', '')}HubProvider", (HubProxyProvider,), attrs)


def register_active_provider_classes(installations=None) -> list[str]:
    registered = []
    built_in_provider_ids = set(provider_registry.names()) - _REGISTERED_PROVIDER_HUB_IDS
    installations = installations if installations is not None else active_installations()

    for installation in installations:
        provider_id = installation.provider_id
        trusted = bool(getattr(installation, "trusted", False))
        shadows_builtin = provider_id in built_in_provider_ids
        if shadows_builtin and not can_shadow_built_in_provider(provider_id, trusted):
            logger.warning("Skipping Provider Hub provider %s because it shadows a built-in provider", provider_id)
            continue
        try:
            manifest = validate_manifest(
                installation.manifest,
                built_in_provider_ids=validation_built_in_provider_ids(
                    provider_id,
                    built_in_provider_ids,
                    trusted,
                ),
            )
        except ManifestValidationError:
            logger.exception("Skipping invalid Provider Hub manifest for %s", provider_id)
            continue

        if shadows_builtin and manifest.provider_id in provider_registry:
            del provider_registry[manifest.provider_id]
        provider_registry.register(manifest.provider_id, _make_provider_class(manifest, installation=installation))
        _REGISTERED_PROVIDER_HUB_IDS.add(manifest.provider_id)
        registered.append(manifest.provider_id)

    return registered
