# coding=utf-8
from __future__ import annotations

import contextlib
import logging
import json
import sys
import threading
import time

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
from .state import active_installations, provider_hub_dir
from . import runtime_status
from .env_registry import env_dir_of
from .venv import hold_environment, validated_bundle_path, validated_interpreter
from .worker import ProviderWorkerClient, release_when_gone, RequestNotAdmitted, WorkerBusy, WorkerError, worker_command

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

    @contextlib.contextmanager
    def _operation(self):
        """One admitted call: it holds the environment against cleanup for as long as it runs.

        A retired proxy refuses it, so the caller asks the pool for the current
        instance. When the last admitted call of a retired proxy ends, the worker
        it may have started is stopped before the hold is released.
        """
        from subliminal_patch.core import ProviderRetiredError

        with _PROXY_LOCK:
            if self._retired:
                raise ProviderRetiredError(self.provider_name)
            env_dir = _instance_environment(self)
            release = hold_environment(env_dir) if env_dir else (lambda: None)
            self._active_operations += 1
        try:
            yield
        finally:
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
        release_when_gone(self.worker_client, release, self.provider_name)

    def outcome_recorded(self):
        """Called by the pool once the outcome of a call is recorded, so the
        next queued request rechecks admission against it."""
        recorded = getattr(self.worker_client, "outcome_recorded", None)
        if recorded is not None:
            recorded()

    def _gated_request(self, op, request, timeout):
        # The worker cannot raise what the pool catches, so its two refusals
        # are translated here. Imported late: some suites stub subliminal_patch.
        if self.admit is None:
            return self._worker().request(op, request, timeout=timeout)
        try:
            return self._worker().request(op, request, timeout=timeout, admit=self.admit)
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
            self.worker_client = ProviderWorkerClient(
                worker_command(python_path, runner),
                cwd=bundle_path,
                env={
                    "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle_path),
                    "BAZARR_PROVIDER_HUB_MANIFEST": manifest_json,
                },
                provider_id=self.provider_name,
            )
            return self.worker_client

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
        with self._operation():
            result = self._gated_request("search", request, timeout)
        _consume_runtime_events(
            self.provider_name,
            result,
            status_generation,
            status_configuration_generation,
        )
        subtitles = []
        for item in result.payload.get("candidates", []):
            subtitle = candidate_from_worker(self.provider_name, item)
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
        with self._operation():
            return self._download_subtitle(subtitle)

    def _download_subtitle(self, subtitle):
        timeout = self._request_timeout()
        status_generation = runtime_status.generation(self.provider_name)
        status_configuration_generation = runtime_status.configuration_generation(
            self.provider_name
        )
        request = {
            "provider": self.provider_name,
            "provider_payload": subtitle.provider_payload,
            "language": language_to_payload(subtitle.language),
            "config": self.config,
        }
        result = self._gated_request("download", request, timeout)
        _consume_runtime_events(
            self.provider_name,
            result,
            status_generation,
            status_configuration_generation,
        )

        def _select_member_cb(members):
            context = getattr(subtitle, "_requested_archive_context", {})
            selector_payload = {
                "provider": self.provider_name,
                "provider_payload": subtitle.provider_payload,
                "language": language_to_payload(subtitle.language),
                "members": members,
                "season": context.get("season", getattr(subtitle, "season", None)),
                "episode": context.get("episode", getattr(subtitle, "episode", None)),
                "config": self.config,
            }
            if "absolute_episode" in context:
                selector_payload["absolute_episode"] = context["absolute_episode"]
            elif hasattr(subtitle, "absolute_episode"):
                selector_payload["absolute_episode"] = subtitle.absolute_episode
            response = self._worker().select_archive_member(
                selector_payload,
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
            try:
                # A path the hub did not build is something to repair, not to run.
                from .env_registry import mark_needs_repair

                mark_needs_repair(provider_id)
            except Exception:
                logger.exception("Provider Hub: could not flag %s for repair", provider_id)
            continue
        provider_registry.register(manifest.provider_id, provider_cls)
        _REGISTERED_PROVIDER_HUB_IDS.add(manifest.provider_id)
        registered.append(manifest.provider_id)

    return registered


def _instance_environment(instance) -> str | None:
    client = getattr(instance, "worker_client", None)
    command = getattr(client, "command", None)
    python_path = command[0] if command else getattr(type(instance), "python_path", None)
    return env_dir_of(python_path) if python_path else None


def _wait_for_operations(instance) -> None:
    """Block until every call admitted before the retirement has ended.

    There is no point at which it gives up: a call that outlives its expected
    time is logged once and still waited for, because stopping the worker under
    it, or releasing the environment it runs from, would cut a call that is still
    legitimately working (a download is a search-queue wait, a download and an
    archive selection in one operation).
    """
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


def _drain_instance(instance, release) -> None:
    """Let the instance finish what it was admitted for, then end its worker through the one termination path.

    A call admitted before the retirement may still be on its way to the worker,
    or between the two requests of a download. Stopping first would fail it as
    busy or cut it between them, so the stop waits for those calls. ``release``
    is the hold taken at detach time; it is given up only once the worker and
    its process group are confirmed gone.
    """
    try:
        _wait_for_operations(instance)
    except Exception:
        logger.exception("Provider Hub: could not wait for the calls of a replaced %s", getattr(instance, "provider_name", "?"))
    instance._terminate_holding(release)


class RetirementIncomplete(RuntimeError):
    """A cached instance could not be retired, so its environment may still be in use."""


def retire_provider_instances(provider_id: str) -> list[threading.Thread]:
    """Replace the instances every pool has cached for ``provider_id``.

    Used after the environment a provider runs from changed. Each pool drops its
    cached instance at once, so the next request builds one from the registered
    class and runs in the new environment. The dropped instance is drained in the
    background: it finishes the request it is serving, then its worker stops. Its
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
                target=_drain_instance, args=(instance, release), name=f"hub-drain-{provider_id}", daemon=True
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
