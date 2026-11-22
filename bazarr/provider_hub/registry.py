# coding=utf-8
from __future__ import annotations

import contextlib
import itertools
import logging
import json
import sys
import threading
import weakref
import time

from dataclasses import dataclass
from pathlib import Path

from subliminal.video import Episode, Movie
from subliminal_patch.extensions import provider_registry
from subliminal_patch.providers import Provider

from .bundle import verify_bundle_tree
from .manifest import ManifestValidationError, validate_manifest
from .migration import (
    MIGRATED_BUILT_IN_PROVIDER_IDS,
    RETIRED_BUILT_IN_PROVIDER_IDS,
    can_shadow_built_in_provider,
    validation_built_in_provider_ids,
)
from .protocol import candidate_from_worker, language_to_payload, video_to_payload, worker_download_to_content
from .state import (
    OFFICIAL_CATALOG_REPO, OFFICIAL_CATALOG_SOURCE_ID, active_installations, blocked_installations, provider_hub_dir,
)
from . import leases, runtime_status
from .env_registry import env_dir_of
from .venv import hold_environment, validated_bundle_path, validated_interpreter
from .worker import ProviderWorkerClient, release_when_gone, RequestNotAdmitted, WorkerBusy, WorkerError, worker_command

logger = logging.getLogger(__name__)

_REGISTERED_PROVIDER_HUB_IDS: set[str] = set()
# Held for every change that puts a plugin's class into the provider registry or takes it out, so a class that
# replaces a built-in is saved in the same step, and a detach gives the built-in its place back in one step.
_REGISTRY_GUARD = threading.RLock()
# The built-in class a plugin of the same id replaced, by provider id, kept so that detaching the plugin restores it.
_SHADOWED_BUILT_INS: dict[str, type] = {}
# Provider ids a plugin was detached from while Bazarr+ ran and nothing has taken the place of: no pool serves them,
# and the pools' admission check says so, so a download that names one is refused quietly.
_WITHDRAWN_PROVIDER_IDS: set[str] = set()
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


def _consume_runtime_events(provider_id, result, request_generation,
                            request_configuration_generation):
    result_generation = getattr(result, "status_generation", None)
    result_configuration_generation = getattr(
        result, "status_configuration_generation", None
    )
    if (
        type(result_generation) is int
        and type(result_configuration_generation) is int
        and result_configuration_generation == request_configuration_generation
    ):
        request_generation = result_generation
    runtime_status.consume(
        provider_id,
        getattr(result, "events", []),
        expected_generation=request_generation,
    )


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


# Guards the retired mark and the operation count of every proxy. The sections
# are a few assignments, so one lock for all of them is enough.
_PROXY_LOCK = threading.Lock()
# Signalled each time an admitted operation ends, for the drain that waits on them.
_PROXY_OPERATIONS_ENDED = threading.Condition(_PROXY_LOCK)


# Names each proxy instance as the holder of the leases its calls take, so a drained instance gives back exactly those.
_LEASE_OWNERS = itertools.count(1)

# The workers that hold a host capability, by provider id, so a withdrawn trust can take it back at once.
_HOST_CALL_CLIENTS: dict[str, "weakref.WeakSet"] = {}


def revoke_untrusted_host_calls() -> None:
    """Take the host capabilities back from every running worker whose plugin is no longer trusted."""
    from .capabilities import live_trust

    with _PROXY_LOCK:
        held = {provider_id: list(clients) for provider_id, clients in _HOST_CALL_CLIENTS.items()}
    for provider_id, clients in held.items():
        if clients and not live_trust(provider_id):
            for client in clients:
                client.revoke_host_calls()


class HubProxyProvider(Provider):
    provider_name = "providerhub"
    languages = set()
    video_types = (Episode, Movie)
    subtitle_class = None
    # Served through the distribution hub. For a hearing-impaired request the
    # host searches the base language unless the provider lists that variant;
    # see _hub_search_languages in custom_libs/subliminal_patch/core.py.
    hub_proxy = True
    #: Set by the pool that owns this instance: a callable answering whether
    #: the pool may still call this provider. Searches and downloads recheck it
    #: once they get their turn on the worker; None sends them ungated.
    admit = None
    #: Set once the pool has dropped this instance for a replacement. A retired
    #: proxy admits no new operation and never starts a worker.
    _retired = False
    _active_operations = 0
    #: Set by whichever path stops this proxy's worker first, so only one does.
    _stopping = False
    #: Names this instance as the holder of the service leases its calls take.
    _lease_owner = None

    def __init__(self, timeout=None, worker_client=None, **config):
        # An explicit per-instance timeout override is optional; when absent the
        # global default backstop (see _request_timeout) applies instead of a
        # hardcoded value.
        self.timeout = _coerce_timeout(timeout)
        self.worker_client = worker_client or getattr(self.__class__, "worker_client", None)
        self.config = config

    def initialize(self):
        return True

    def retire(self) -> None:
        """Refuse every operation that has not been admitted yet."""
        with _PROXY_LOCK:
            self._retired = True

    def _shutdown_worker(self) -> bool:
        """Stop the worker, if no other path has claimed that. True when this call did the stopping.

        The last admitted call of a retired proxy and the drain both reach for
        the stop; claiming it atomically leaves one owner, and stopping never
        starts a process.
        """
        with _PROXY_LOCK:
            if self._stopping:
                return False
            self._stopping = True
        client = self.worker_client
        if client:
            client.stop()
        return True

    def _call_leases(self):
        """What this call may lease, for the manifest of the release this instance runs. Never raises."""
        from .capabilities import CallLeases

        try:
            with _PROXY_LOCK:
                if self._lease_owner is None:
                    self._lease_owner = f"{self.provider_name}#{next(_LEASE_OWNERS)}"
                owner = self._lease_owner
            return CallLeases.for_provider(self.provider_name, getattr(self.__class__, "manifest", None), owner)
        except Exception:
            logger.exception("Provider Hub: could not prepare the service leases of %s", self.provider_name)
            return CallLeases(self.provider_name, {}, [], "")

    @contextlib.contextmanager
    def _operation(self):
        """One admitted call: it holds the environment against cleanup for as long as it runs.

        A retired proxy refuses it, so the caller asks the pool for the current
        instance. When the last admitted call of a retired proxy ends, the worker
        it may have started is stopped before the hold is released.

        What it yields is the call's service leases: the request's configuration comes from ``call.config`` and the
        leases are given back when the call ends, whatever way it ends.
        """
        from subliminal_patch.core import ProviderRetiredError

        with _PROXY_LOCK:
            if self._retired:
                raise ProviderRetiredError(self.provider_name)
            env_dir = _instance_environment(self)
            release = hold_environment(env_dir) if env_dir else (lambda: None)
            self._active_operations += 1
        call = None
        try:
            call = self._call_leases()
            yield call
        finally:
            if call is not None:
                call.close()
            with _PROXY_LOCK:
                self._active_operations -= 1
                last_of_retired = self._retired and self._active_operations == 0
                _PROXY_OPERATIONS_ENDED.notify_all()
            try:
                if last_of_retired:
                    self._terminate_holding()
            except Exception:
                # The drain holds the environment until the worker has exited,
                # so a failed stop here keeps it pinned rather than freeing it.
                logger.exception("Provider Hub: could not stop the worker of a replaced %s", self.provider_name)
            finally:
                release()

    def terminate(self):
        self._terminate_holding()

    def _terminate_holding(self, release=None):
        """The one way a proxy's worker is ended.

        The proxy is retired, so nothing new starts on it, its worker is
        stopped, and the environment it runs from stays pinned until the worker
        and everything it started are gone: ``release`` (a hold the caller took
        when it detached the proxy) or one taken here. A survivor keeps the pin
        and is released later by a watcher, so a pool that drops the instance
        never frees an environment that is still in use.
        """
        if release is None:
            env_dir = _instance_environment(self)
            release = hold_environment(env_dir) if env_dir else (lambda: None)
        self.retire()
        try:
            self._shutdown_worker()
        except Exception:
            logger.exception("Provider Hub: could not stop the worker of %s", self.provider_name)
        finally:
            # A call that was cut never reached its own release. Nothing of an ended instance keeps a service.
            if self._lease_owner is not None:
                leases.release_owner(self._lease_owner)
        release_when_gone(self.worker_client, release, self.provider_name)

    def outcome_recorded(self):
        """Called by the pool once the outcome of a call is recorded, so the
        next queued request rechecks admission against it."""
        recorded = getattr(self.worker_client, "outcome_recorded", None)
        if recorded is not None:
            recorded()

    def _source_media_permission(self):
        """The manifest schema through which this release can request local media."""
        manifest = getattr(self.__class__, "manifest", None)
        if manifest is None:
            return None
        if (manifest.schema_version == 1 and self.provider_name == "embeddedsubtitles"
                and manifest.source_repo.casefold() == OFFICIAL_CATALOG_REPO.casefold()):
            return 1
        if manifest.schema_version == 2 and "media_read" in (manifest.raw.get("permissions") or ()):
            return 2
        return None

    def _source_media(self, context):
        """Resolve the live installation's permission before looking up the source file."""
        from .capabilities import live_row
        from .provider_roots import source_media_file

        permission = self._source_media_permission()
        if permission is None:
            return None
        manifest = self.__class__.manifest
        row = live_row(self.provider_name)
        active_manifest = row.get("manifest") if row else None
        if not isinstance(active_manifest, dict) or row.get("enabled") is False:
            return None
        if str(active_manifest.get("bundle_sha256", "")).lower() != manifest.bundle_sha256:
            return None
        if permission == 1 and (row.get("origin") != "catalog" or row.get("source_id") != OFFICIAL_CATALOG_SOURCE_ID
                                or not can_shadow_built_in_provider(self.provider_name, row.get("trusted") is True)):
            return None
        if permission == 2:
            permissions = active_manifest.get("permissions")
            if not isinstance(permissions, list) or "media_read" not in permissions:
                return None
        return source_media_file(context)

    def _gated_request(self, op, request, timeout, *, source_context=None, consume_source_reply=None):
        # The worker cannot raise what the pool catches, so its two refusals
        # are translated here. Imported late: some suites stub subliminal_patch.
        # Source authorization is resolved after the request gets its turn.
        options = {}
        if source_context is not None and self._source_media_permission() is not None:
            options["source_media"] = lambda: self._source_media(source_context)
            if consume_source_reply is not None:
                options["consume_source_reply"] = consume_source_reply
        if self.admit is None:
            return self._worker().request(op, request, timeout=timeout, **options)
        try:
            return self._worker().request(op, request, timeout=timeout, admit=self.admit, **options)
        except RequestNotAdmitted as error:
            from subliminal_patch.core import ProviderExcludedWhileQueuedError
            raise ProviderExcludedWhileQueuedError(self.provider_name) from error
        except WorkerBusy as error:
            from subliminal_patch.core import ProviderBusyError
            raise ProviderBusyError(self.provider_name) from error

    @classmethod
    def check(cls, video):
        return isinstance(video, cls.video_types)

    def _worker(self):
        # Check, create and publish in one step under the lock that guards the
        # stop and the retirement: two first calls share one client, and a proxy
        # that is stopping or retired never creates one (the caller moves to the
        # current instance), so the client a stop reaches is the only one there is.
        with _PROXY_LOCK:
            if self._stopping or (self._retired and not self.worker_client):
                from subliminal_patch.core import ProviderRetiredError

                raise ProviderRetiredError(self.provider_name)
            if self.worker_client:
                return self.worker_client
            bundle_path = getattr(self.__class__, "bundle_path", None)
            python_path = getattr(self.__class__, "python_path", None) or sys.executable
            manifest_json = getattr(self.__class__, "manifest_json", None)
            if not (bundle_path and manifest_json):
                raise WorkerError("Provider Hub worker is not configured")
            runner = Path(__file__).with_name("worker_runner.py")
            from types import SimpleNamespace

            from .capabilities import host_call_handlers, live_trust

            # What the plugin may ask the host for while it serves a request. The grant follows the
            # host's own trust record of the installation, never the manifest's word for itself.
            host_calls = host_call_handlers(SimpleNamespace(
                provider_id=self.provider_name,
                manifest=getattr(getattr(self.__class__, "manifest", None), "raw", None),
                trusted=live_trust(self.provider_name)))
            self.worker_client = ProviderWorkerClient(
                worker_command(python_path, runner),
                cwd=bundle_path,
                env={
                    "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle_path),
                    "BAZARR_PROVIDER_HUB_MANIFEST": manifest_json,
                    "BAZARR_HOST_CALLS": json.dumps(sorted(host_calls)),
                },
                provider_id=self.provider_name,
                release=getattr(self.__class__, "release_key", None),
                before_start=self._verify_bundle_before_start,
                host_calls=host_calls,
            )
            if host_calls:
                _HOST_CALL_CLIENTS.setdefault(self.provider_name, weakref.WeakSet()).add(self.worker_client)
            return self.worker_client

    def _verify_bundle_before_start(self) -> None:
        """Every process the proxy starts runs code that still matches its manifest.

        Runs before the first start and before each respawn. A bundle that no
        longer matches flags the installation for repair and starts nothing.
        """
        manifest = getattr(self.__class__, "manifest", None)
        bundle_path = getattr(self.__class__, "bundle_path", None)
        if manifest is None or not bundle_path:
            return
        try:
            verify_bundle_tree(manifest, bundle_path)
        except Exception as error:
            try:
                from .env_registry import mark_needs_repair

                mark_needs_repair(self.provider_name)
            except Exception:
                logger.exception("Provider Hub: could not flag %s for repair", self.provider_name)
            raise WorkerError(f"the bundle of {self.provider_name} does not match its manifest: {error}") from error

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
        status_generation = runtime_status.generation(self.provider_name)
        status_configuration_generation = runtime_status.configuration_generation(
            self.provider_name
        )
        request = {
            "provider": self.provider_name,
            "config": self.config,
            "video": video_to_payload(video),
            "languages": [language_to_payload(item) for item in languages],
        }
        with self._operation() as call:
            request["config"] = call.config(self.config)
            result = self._gated_request("search", request, timeout, source_context=getattr(video, "source_context", None))
        _consume_runtime_events(
            self.provider_name,
            result,
            status_generation,
            status_configuration_generation,
        )
        subtitles = []
        for item in result.payload.get("candidates", []):
            subtitle = candidate_from_worker(self.provider_name, item)
            subtitle.source_context = getattr(video, "source_context", None)
            # Request context belongs to this candidate, separate from provider
            # display metadata and opaque payloads used for scoring and download.
            subtitle._requested_archive_context = {
                "season": request["video"]["season"],
                "episode": request["video"]["episode"],
                "absolute_episode": request["video"]["absolute_episode"],
            }
            subtitles.append(subtitle)
        return subtitles

    def download_subtitle(self, subtitle):
        # The archive member selection inside the download talks to the same
        # worker, so the whole download is one admitted operation.
        with self._operation() as call:
            return self._download_subtitle(subtitle, call)

    def _download_subtitle(self, subtitle, call=None):
        timeout = self._request_timeout()
        status_generation = runtime_status.generation(self.provider_name)
        status_configuration_generation = runtime_status.configuration_generation(
            self.provider_name
        )
        request = {
            "provider": self.provider_name,
            "provider_payload": subtitle.provider_payload,
            "language": language_to_payload(subtitle.language),
            "config": call.config(self.config) if call is not None else self.config,
        }

        def build_selector_payload(members):
            context = getattr(subtitle, "_requested_archive_context", {})
            selector_payload = {
                "provider": self.provider_name,
                "provider_payload": subtitle.provider_payload,
                "language": language_to_payload(subtitle.language),
                "members": members,
                "season": context.get("season", getattr(subtitle, "season", None)),
                "episode": context.get("episode", getattr(subtitle, "episode", None)),
                "config": request["config"],
            }
            if "absolute_episode" in context:
                selector_payload["absolute_episode"] = context["absolute_episode"]
            elif hasattr(subtitle, "absolute_episode"):
                selector_payload["absolute_episode"] = subtitle.absolute_episode
            return selector_payload

        def consume(result, select_member):
            _consume_runtime_events(
                self.provider_name,
                result,
                status_generation,
                status_configuration_generation,
            )
            worker_download_to_content(
                subtitle, result.payload,
                select_member_cb=lambda members: select_member(build_selector_payload(members), timeout=timeout).payload,
            )

        source_context = getattr(subtitle, "source_context", None)
        if source_context is not None and self._source_media_permission() is not None:
            self._gated_request("download", request, timeout, source_context=source_context, consume_source_reply=consume)
        else:
            result = self._gated_request("download", request, timeout)
            consume(result, lambda payload, timeout: self._worker().select_archive_member(payload, timeout=timeout))
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
        # The release this class serves, as its workers are keyed.
        "release_key": getattr(manifest, "bundle_sha256", None),
    }
    if worker_client is not None:
        attrs["worker_client"] = worker_client
    if installation is not None:
        if installation.active_path:
            bundle = validated_bundle_path(provider_hub_dir(), installation.active_path)
            if bundle is None:
                raise ValueError(f"the bundle {installation.active_path} is not one of the hub's bundles")
            attrs["bundle_path"] = str(bundle)
        if getattr(installation, "python_path", None):
            interpreter = validated_interpreter(provider_hub_dir(), installation.python_path)
            if interpreter is None:
                # Never run a plugin from an interpreter the hub did not build.
                raise ValueError(f"the interpreter {installation.python_path} is not one of the hub's environments")
            # The validated path is the one that is run, not the stored string.
            attrs["python_path"] = str(interpreter)
    return type(f"{manifest.provider_id.title().replace('_', '')}HubProvider", (HubProxyProvider,), attrs)


# Providers an installation exists for but whose class could not be registered the
# last time they were tried: a manifest or interpreter or bundle that was refused.
# Startup must not read "not registered" as "gone" for these: their ids stay in
# the provider lists and their settings stay where they are, until a repair or
# an operator settles it.
_REGISTRATION_INCOMPLETE: set[str] = set()
_REGISTRATION_INCOMPLETE_LOCK = threading.Lock()


def registration_incomplete() -> frozenset[str]:
    with _REGISTRATION_INCOMPLETE_LOCK:
        return frozenset(_REGISTRATION_INCOMPLETE)


def _note_registration(provider_id: str, complete: bool) -> None:
    with _REGISTRATION_INCOMPLETE_LOCK:
        if complete:
            _REGISTRATION_INCOMPLETE.discard(provider_id)
        else:
            _REGISTRATION_INCOMPLETE.add(provider_id)


def settings_cleanup_plan(
    existing_providers,
    enabled_providers,
    disabled_providers,
    retired_provider_ids,
    has_section,
    incomplete=None,
):
    """What startup may drop from the settings, given what registered.

    Returns ``(enabled, disabled, stale_sections)``. A provider whose
    registration was refused (``incomplete``) keeps its id in both lists and its
    settings section: a refusal can be temporary, and the section holds its
    credentials. ``incomplete=None`` means the set could not be determined, which
    drops nothing at all.
    """
    if incomplete is None:
        return list(enabled_providers), list(disabled_providers), []
    keep = set(existing_providers) | set(incomplete)
    stale = sorted(
        provider_id for provider_id in retired_provider_ids
        if provider_id not in existing_providers and provider_id not in incomplete and has_section(provider_id)
    )
    return (
        [item for item in enabled_providers if item in keep],
        [item for item in disabled_providers if item in keep],
        stale,
    )


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
    if installations is None:
        installations = active_installations()
        # A provider held back at boot is installed, only not started: it counts as a registration that was
        # refused, so startup keeps its entries in the provider lists and its saved settings.
        for held_back in blocked_installations():
            _note_registration(held_back.provider_id, False)

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
            _note_registration(provider_id, False)
            continue

        try:
            provider_cls = _make_provider_class(manifest, installation=installation)
        except Exception:
            # Build the proxy class before touching the registry. If this fails for a
            # provider that shadows a built-in, the built-in must stay in place rather
            # than be left deleted (provider_registry.register overwrites in place, so
            # no explicit delete is needed on the success path).
            logger.exception("Skipping Provider Hub provider %s because its proxy class could not be built", provider_id)
            _note_registration(provider_id, False)
            try:
                # A path the hub did not build is something to repair, not to run.
                from .env_registry import mark_needs_repair

                mark_needs_repair(provider_id)
            except Exception:
                logger.exception("Provider Hub: could not flag %s for repair", provider_id)
            continue
        with _REGISTRY_GUARD:
            replaced = provider_registry[manifest.provider_id]
            if replaced is not None and not issubclass(replaced, HubProxyProvider):
                # A built-in provider this plugin takes the id of: kept, so detaching the plugin restores it.
                _SHADOWED_BUILT_INS[manifest.provider_id] = replaced
            provider_registry.register(manifest.provider_id, provider_cls)
            _REGISTERED_PROVIDER_HUB_IDS.add(manifest.provider_id)
            _WITHDRAWN_PROVIDER_IDS.discard(manifest.provider_id)
        _note_registration(provider_id, True)
        registered.append(manifest.provider_id)

    return registered


@dataclass(frozen=True)
class Unregistered:
    """What taking a plugin's class out of the provider registry did."""

    # A plugin class was in the registry and is gone.
    removed: bool = False
    # A built-in provider the plugin had replaced is registered again.
    restored_built_in: bool = False


def unregister_provider_class(provider_id: str) -> Unregistered:
    """Take a plugin's class out of the provider registry. Searches that start afterwards do not see it.

    A plugin that took over a built-in provider's id gives it back: the class it replaced was saved when it
    registered, and goes back in the same step, under the lock registration takes, so there is no moment at
    which the id names neither. A registry that holds no plugin class under the id (a built-in, or nothing) is
    left as it is. A search that already holds the class it looked up goes on with it: the pools drop their
    instances separately (``retire_provider_instances``).
    """
    with _REGISTRY_GUARD:
        current = provider_registry[provider_id]
        removed = current is not None and issubclass(current, HubProxyProvider) and provider_registry.unregister(
            provider_id, expected=current) is not None
        built_in = _SHADOWED_BUILT_INS.pop(provider_id, None)
        restored = False
        if removed:
            _REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
            if built_in is not None and provider_registry[provider_id] is None:
                provider_registry.register(provider_id, built_in)
                restored = True
            else:
                _WITHDRAWN_PROVIDER_IDS.add(provider_id)
        return Unregistered(removed=removed, restored_built_in=restored)


def is_withdrawn(provider_id: str) -> bool:
    """Whether a plugin was detached from this provider id while Bazarr+ ran, and nothing serves it now."""
    with _REGISTRY_GUARD:
        return provider_id in _WITHDRAWN_PROVIDER_IDS and provider_registry[provider_id] is None


def release_provider_id(provider_id: str) -> int:
    """A plugin's class left the registry, and the id may name a built-in again: hand the id over clean.

    Every pool that is alive gets the settings the built-in is given in place of what it held under the id (read
    now, after the plugin's configuration stopped being handed out), so a call that reaches the id before the next
    settings read builds the built-in from its own settings. A throttle or discard the plugin earned is dropped:
    the built-in starts with a clean record. Returns how many pools held something under the id.
    """
    own: dict = {}
    try:
        from app.get_providers import forget_provider_throttle, get_providers_auth

        own = dict(get_providers_auth().get(provider_id) or {})
        forget_provider_throttle(provider_id)
    except Exception:
        logger.exception("Provider Hub: could not read the settings of %s for the pools", provider_id)
    try:
        from subliminal_patch.provider_health import get_tracker

        get_tracker().forget(provider_id)
    except Exception:
        logger.exception("Provider Hub: could not clear the health record of %s", provider_id)
    try:
        from subliminal_patch.core import live_provider_pools
    except ImportError:
        return 0
    forgot = 0
    for pool in live_provider_pools():
        try:
            if provider_id in pool.provider_configs:
                forgot += 1
            pool.forget_provider_config(provider_id, own)
        except Exception:
            logger.exception("Provider Hub: could not clear what a pool holds for %s", provider_id)
    _retire_built_in_instances(provider_id, live_provider_pools)
    return forgot


def _retire_built_in_instances(provider_id: str, live_provider_pools) -> None:
    """Drop every instance of a built-in that a pool made for ``provider_id`` before the pools were released.

    The registry gives the built-in back one step before the pools are told, and a pool that builds in between holds
    an instance made from the plugin's configuration. While the plugin held the id no built-in instance could exist, so
    whatever is there now was made in that window. In-progress builds are made to start over (the provider generation
    moves), and the next call builds the built-in from its own settings.
    """
    from subliminal_patch.core import bump_provider_generation

    bump_provider_generation(provider_id)
    for pool in live_provider_pools():
        try:
            stale = pool.detach_provider(provider_id, lambda cached: not isinstance(cached, HubProxyProvider))
            if stale is not None:
                try:
                    stale.terminate()
                except Exception:
                    logger.exception("Provider Hub: could not end a built-in %s made before its pools were released",
                                     provider_id)
        except Exception:
            logger.exception("Provider Hub: could not retire a built-in %s of a pool", provider_id)


def is_plugin_class(provider_id: str) -> bool:
    """Whether the provider registry holds no class under the id, or holds a plugin's. A plugin's configuration is
    handed only to a plugin's class: when a built-in holds the id (the plugin was detached, or its registration was
    refused) it is built from its own settings."""
    current = provider_registry[provider_id]
    return current is None or issubclass(current, HubProxyProvider) or bool(getattr(current, "hub_proxy", False))


def revoke_host_calls_of(provider_id: str) -> int:
    """Take the host capabilities back from every running worker of one plugin, for the calls that follow.

    A worker that is finishing what it was given keeps what it has already sent, but cannot ask the host for
    anything more (a captcha solved with the user's key). Returns how many workers were told."""
    with _PROXY_LOCK:
        clients = list(_HOST_CALL_CLIENTS.get(provider_id, ()))
    for client in clients:
        client.revoke_host_calls()
    return len(clients)


def _instance_environment(instance) -> str | None:
    client = getattr(instance, "worker_client", None)
    command = getattr(client, "command", None)
    python_path = command[0] if command else getattr(type(instance), "python_path", None)
    return env_dir_of(python_path) if python_path else None


def _wait_for_operations(instance, grace: float | None = None) -> bool:
    """Block until every call admitted before the retirement has ended.

    Without ``grace`` there is no point at which it gives up: a call that outlives its expected time is logged
    once and still waited for, because stopping the worker under it, or releasing the environment it runs from,
    would cut a call that is still legitimately working (a download is a search-queue wait, a download and an
    archive selection in one operation). With ``grace`` (a release that must stop, a revoked one) it waits at
    most that many seconds and answers False when calls are still running. True when none is.
    """
    if grace is not None:
        deadline = time.monotonic() + max(0.0, grace)
        with _PROXY_OPERATIONS_ENDED:
            while getattr(instance, "_active_operations", 0) > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                _PROXY_OPERATIONS_ENDED.wait(left)
        return True
    try:
        limit = float(instance._request_timeout()) + _HOST_TIMEOUT_MARGIN_SECONDS
    except Exception:
        limit = _MAX_WORKER_REQUEST_TIMEOUT + _HOST_TIMEOUT_MARGIN_SECONDS
    deadline = time.monotonic() + limit
    warned = False
    with _PROXY_OPERATIONS_ENDED:
        while getattr(instance, "_active_operations", 0) > 0:
            left = deadline - time.monotonic()
            if left <= 0 and not warned:
                warned = True
                logger.warning(
                    "Provider Hub: a replaced %s still has a call running after %.0fs; "
                    "its worker and environment are kept until it ends",
                    getattr(instance, "provider_name", "?"), limit,
                )
            _PROXY_OPERATIONS_ENDED.wait(None if warned else max(left, 0.0))
    return True


def _drain_instance(instance, release, grace: float | None = None) -> None:
    """Let the instance finish what it was admitted for, then end its worker through the one termination path.

    A call admitted before the retirement may still be on its way to the worker,
    or between the two requests of a download. Stopping first would fail it as
    busy or cut it between them, so the stop waits for those calls. ``release``
    is the hold taken at detach time; it is given up only once the worker and
    its process group are confirmed gone. With ``grace`` the wait is bounded: calls still
    running when it ends are cut by killing the worker, and fail as a worker that went away.
    """
    finished = True
    try:
        finished = _wait_for_operations(instance, grace)
    except Exception:
        logger.exception("Provider Hub: could not wait for the calls of a replaced %s", getattr(instance, "provider_name", "?"))
    if not finished:
        client = getattr(instance, "worker_client", None)
        cancel = getattr(client, "cancel", None)
        logger.warning("Provider Hub: cutting the calls %s still had running after %.0fs",
                       getattr(instance, "provider_name", "?"), grace)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                logger.exception("Provider Hub: could not cut the calls of %s", getattr(instance, "provider_name", "?"))
    instance._terminate_holding(release)


class RetirementIncomplete(RuntimeError):
    """A cached instance could not be retired, so its environment may still be in use."""


def retire_provider_instances(provider_id: str, grace: float | None = None) -> list[threading.Thread]:
    """Replace the instances every pool has cached for ``provider_id``.

    Used after the environment a provider runs from changed, and when a plugin is detached. Each pool drops its
    cached instance at once, so the next request builds one from the registered
    class (or finds none, for a plugin that left the registry) and runs in the new environment. The dropped
    instance is drained in the
    background: it finishes the request it is serving, then its worker stops. With ``grace`` the
    calls it still has after that many seconds are cut. Its
    environment is held against cleanup until that worker has exited. Returns the
    drain threads.
    """
    try:
        from subliminal_patch.core import bump_provider_generation, live_provider_pools
    except ImportError:
        # Without the pool module there are no pools and nothing cached.
        return []
    # First, so a pool that is building an instance of the old class right
    # now sees the change before it stores it, and builds again.
    bump_provider_generation(provider_id)
    failures: list[Exception] = []
    drains: list[threading.Thread] = []
    for pool in live_provider_pools():
        try:
            # The look at the cache and the detach are one step under the pool's
            # store lock: a constructor that is storing an instance right now is
            # either finished (and found here) or sees the new generation.
            instance = pool.detach_provider(provider_id, lambda cached: isinstance(cached, HubProxyProvider))
            if instance is None:
                continue
            env_dir = _instance_environment(instance)
            release = hold_environment(env_dir) if env_dir else (lambda: None)
            drain = threading.Thread(
                target=_drain_instance, args=(instance, release, grace), name=f"hub-drain-{provider_id}", daemon=True
            )
            drain.start()
            drains.append(drain)
        except Exception as error:
            logger.exception("Provider Hub: could not retire the cached %s of a pool", provider_id)
            failures.append(error)
    if failures:
        raise RetirementIncomplete(
            f"{len(failures)} cached instance(s) of {provider_id} could not be retired"
        ) from failures[0]
    return drains
