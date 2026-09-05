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
    MIGRATED_BUILT_IN_PROVIDER_IDS,
    RETIRED_BUILT_IN_PROVIDER_IDS,
    can_shadow_built_in_provider,
    validation_built_in_provider_ids,
)
from .protocol import candidate_from_worker, language_to_payload, video_to_payload, worker_download_to_content
from .state import active_installations
from .worker import ProviderWorkerClient, WorkerError, worker_command

logger = logging.getLogger(__name__)

_REGISTERED_PROVIDER_HUB_IDS: set[str] = set()
_MAX_WORKER_REQUEST_TIMEOUT = 86400.0
_HOST_TIMEOUT_MARGIN_SECONDS = 30.0
_DEFAULT_GLOBAL_WORKER_TIMEOUT = 120.0


def _coerce_timeout(value):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return None
    return timeout


def _global_worker_timeout():
    """Backstop host-side worker deadline from settings, with a defensive fallback.

    Runs host-side, so app.config is importable. Falls back to the built-in
    default if settings are unavailable or the value is missing/invalid."""
    try:
        from app.config import settings
        value = _coerce_timeout(getattr(settings.general, "provider_hub_worker_timeout", None))
    except Exception:
        return _DEFAULT_GLOBAL_WORKER_TIMEOUT
    return value or _DEFAULT_GLOBAL_WORKER_TIMEOUT


class HubProxyProvider(Provider):
    provider_name = "providerhub"
    languages = set()
    video_types = (Episode, Movie)
    subtitle_class = None

    def __init__(self, timeout=None, worker_client=None, **config):
        # An explicit per-instance timeout override is optional; when absent the
        # global default backstop (see _request_timeout) applies instead of a
        # hardcoded value.
        self.timeout = _coerce_timeout(timeout)
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

    def _request_timeout(self):
        # The host deadline must never fire before the worker's own per-operation
        # timeout, or a long transcription is killed regardless of the user's
        # settings (issue: WhisperAI and 30 second worker timeout). Derive the
        # deadline from every timeout the plugin config declares. A margin is
        # added on top so a single-operation worker reaches its own timeout and
        # returns a clean error before the host read-wall kills the subprocess.
        # (A multi-phase worker, e.g. whisper's extract-then-transcribe, can
        # still exceed the host wall; each phase is bounded by the worker's own
        # per-phase timeout, so this is an accepted limitation, not a hard-kill
        # regression of the 30s bug.) The plugin-declared value is clamped to the
        # maximum BEFORE the margin is added so the margin survives at the cap,
        # then floored by the global default backstop.
        declared = []
        explicit = _coerce_timeout(self.timeout)
        if explicit is not None:
            declared.append(explicit)
        for key in ("worker_timeout", "timeout_seconds", "timeout"):
            value = _coerce_timeout(self.config.get(key))
            if value is not None:
                declared.append(value)
        for key, raw in self.config.items():
            if key.endswith("_timeout_seconds"):
                value = _coerce_timeout(raw)
                if value is not None:
                    declared.append(value)

        global_default = _global_worker_timeout()
        if not declared:
            return global_default
        base = min(max(declared), _MAX_WORKER_REQUEST_TIMEOUT)
        return max(base + _HOST_TIMEOUT_MARGIN_SECONDS, global_default)

    def list_subtitles(self, video, languages):
        timeout = self._request_timeout()
        request = {
            "provider": self.provider_name,
            "config": self.config,
            "video": video_to_payload(video),
            "languages": [language_to_payload(item) for item in languages],
        }
        result = self._worker().request("search", request, timeout=timeout)
        subtitles = []
        for item in result.payload.get("candidates", []):
            subtitle = candidate_from_worker(self.provider_name, item)
            # Request context belongs to this candidate, separate from provider
            # display metadata and opaque payloads used for scoring and download.
            subtitle._requested_archive_context = {
                "season": request["video"]["season"],
                "episode": request["video"]["episode"],
            }
            subtitles.append(subtitle)
        return subtitles

    def download_subtitle(self, subtitle):
        timeout = self._request_timeout()
        request = {
            "provider": self.provider_name,
            "provider_payload": subtitle.provider_payload,
            "language": language_to_payload(subtitle.language),
            "config": self.config,
        }
        result = self._worker().request("download", request, timeout=timeout)

        def _select_member_cb(members):
            context = getattr(subtitle, "_requested_archive_context", {})
            response = self._worker().select_archive_member(
                {
                    "provider": self.provider_name,
                    "provider_payload": subtitle.provider_payload,
                    "language": language_to_payload(subtitle.language),
                    "members": members,
                    "season": context.get("season", getattr(subtitle, "season", None)),
                    "episode": context.get("episode", getattr(subtitle, "episode", None)),
                    "config": self.config,
                },
                timeout=timeout,
            )
            return response.payload

        worker_download_to_content(subtitle, result.payload, select_member_cb=_select_member_cb)
        return True


def _languages_from_manifest(manifest):
    import logging
    from subzero.language import Language

    languages = set()
    for code in manifest.languages:
        try:
            languages.add(Language.fromietf(code))
        except Exception:
            try:
                languages.add(Language(code))
            except Exception:
                # A single code babelfish doesn't know (e.g. "cnr") must not drop the
                # whole provider during registration: skip it, keep the valid ones.
                logging.getLogger(__name__).warning(
                    "Provider Hub: skipping unsupported language %r in manifest for %s",
                    code, getattr(manifest, "provider_id", "?"),
                )
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
    # Always treat migrated built-in ids as built-in for the shadow gate, even once a
    # hub provider has registered one: otherwise, after the trusted migration registers
    # (adding the id to _REGISTERED_PROVIDER_HUB_IDS and dropping it from the dynamic
    # set), a later UNTRUSTED install of the same id would no longer count as shadowing
    # a built-in and could silently replace the migrated provider.
    #
    # Retired built-in ids are included for the mirror-image reason: their module is
    # gone, so the dynamic set no longer carries them and the gate would otherwise let
    # any plugin claim the freed id.
    built_in_provider_ids = (
        (set(provider_registry.names()) - _REGISTERED_PROVIDER_HUB_IDS)
        | MIGRATED_BUILT_IN_PROVIDER_IDS
        | RETIRED_BUILT_IN_PROVIDER_IDS
    )
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

        try:
            provider_cls = _make_provider_class(manifest, installation=installation)
        except Exception:
            # Build the proxy class before touching the registry. If this fails for a
            # provider that shadows a built-in, the built-in must stay in place rather
            # than be left deleted (provider_registry.register overwrites in place, so
            # no explicit delete is needed on the success path).
            logger.exception("Skipping Provider Hub provider %s because its proxy class could not be built", provider_id)
            continue
        provider_registry.register(manifest.provider_id, provider_cls)
        _REGISTERED_PROVIDER_HUB_IDS.add(manifest.provider_id)
        registered.append(manifest.provider_id)

    return registered
