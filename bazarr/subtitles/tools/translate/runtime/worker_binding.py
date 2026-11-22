# coding=utf-8
"""Binds the dispatcher to the real translator workers.

``workers.bind(PoolWorkers(...))`` is the one call that turns the dispatcher's port from "no worker" into the worker
pool. This module makes it, once, at boot, and only while the ``translator.runtime_v2`` switch is on
(``bind_when_enabled``, called from init.py). With the switch off nothing here runs and the port stays unbound.

What a worker is. Any installed plugin of the translator kind is served the same way, so the AI Subtitle Translator
and the Google Translate plugin share this binding: its interpreter and bundle are the ones the Plugin Hub built and
verified for the release that is active now, it runs the vendored plugin SDK's runner in isolated mode through the sandbox
launcher, and it gets the host calls the dispatcher answers (reserve, usage, checkpoint). The interpreter, bundle and port
set are read again at every start, so a new release or a changed connection is what the next worker starts with.

What a worker may reach. Its filesystem is its bundle, the SDK, the system libraries and two folders of its own
(``HOME`` and ``TMPDIR``); ``config.yaml`` and the database are outside every grant, and so is ``/dev/shm``. Its
network is DNS, ports 80 and 443, the ports of the origins its manifest declares and the ports of the addresses its
enabled connections use, never Bazarr+'s own port. A host without Landlock runs it with the same limits and no ruleset,
which the Plugin Hub already reports as "declared".

Ports. A worker is started with the port set it had at that moment. ``PoolWorkers`` compares the set with the one the
connections need now before each translation and, when they differ, retires the worker once the jobs it holds are done.
A short ``test`` or ``options`` request runs in a worker of its own that admits the port its draft names, so a draft never
waits for a restart and a probe that overruns never ends a worker that holds translations. That worker has its own pool
key and its own writable folders under ``run/<plugin>/call/<id>``, with their own size budgets, and the folders are removed
when the call ends: what a probe writes can neither refuse another call nor count against the folders of the worker that
holds translations.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from . import workers

logger = logging.getLogger(__name__)

KIND = "translator"
# A file in a plugin's folder that says its data was kept on purpose when the plugin was removed.
KEPT_MARKER = ".kept"
DEFAULT_PORTS = frozenset({80, 443})
_DEFAULT_PORT_OF = {"http": 80, "https": 443}
_PLUGIN_ID = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,63}\Z")
_ENTRY_PART = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_CLASS_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_CALL_ID = re.compile(r"\A[0-9a-f]{12}\Z")
MIB = 1024 * 1024
# What the plugin may leave in its own folders. A translator keeps a few small files at most.
DATA_BUDGET_BYTES = 64 * MIB
TEMP_BUDGET_BYTES = 128 * MIB


def ports_of(origins: Iterable[str]) -> frozenset[int]:
    """The TCP ports a set of origins or addresses names, the default of the scheme where none is written.

    Anything that is not a plain http or https address (the stand-in for a value that could not be read, for one)
    names no port.
    """
    ports: set[int] = set()
    for origin in origins:
        try:
            parts = urlsplit(str(origin))
            scheme = parts.scheme.lower()
            port = parts.port or _DEFAULT_PORT_OF.get(scheme)
        except ValueError:
            continue
        if scheme in _DEFAULT_PORT_OF and parts.hostname and port and 1 <= port <= 65535:
            ports.add(port)
    return frozenset(ports)


@dataclass(frozen=True)
class Resolved:
    """What a worker of one plugin starts from, as the Plugin Hub has it now."""

    plugin_id: str
    interpreter: Path
    bundle: Path
    entry: str
    manifest: Mapping[str, Any]


class TranslatorWorkers:
    """Builds the worker definitions for translator plugins.

    Everything that reaches outside this module is a parameter, so tests supply a state, a store and folders of their
    own: ``installation_for`` (plugin id to its running installation, or None), ``store_factory``, ``hub_dir``,
    ``config_dir`` and ``reserved_ports`` (callables, read at every use).
    """

    def __init__(self, *, installation_for: Callable[[str], Any] | None = None,
                 store_factory: Callable[[], Any] | None = None, hub_dir: Callable[[], Path] | None = None,
                 config_dir: Callable[[], str] | None = None, reserved_ports: Callable[[], Iterable[int]] | None = None,
                 sdk_root: Path | None = None, require_sandbox: bool = False, probe: Any = None,
                 known_ids: Callable[[], set[str]] | None = None):
        self._installation_for = installation_for or _running_installation
        self._known_ids = known_ids or _installed_ids
        self._store_factory = store_factory or _default_store
        self._hub_dir = hub_dir or _hub_dir
        self._config_dir = config_dir or _config_dir
        self._reserved_ports = reserved_ports or _reserved_ports
        self._sdk_root = sdk_root
        self._require_sandbox = require_sandbox
        self._probe = probe

    # ------------------------------------------------------------------------------------------ what is installed
    def resolve(self, plugin_id: str) -> Resolved | None:
        """The release that is active now, with the interpreter and bundle the Plugin Hub vouches for, or None when
        the plugin is not installed, was held back at boot or has nothing it can run. Whether it is switched on is
        decided before a request gets here (the gate and the settings service)."""
        from provider_hub.venv import validated_bundle_path, validated_interpreter

        if not isinstance(plugin_id, str) or not _PLUGIN_ID.match(plugin_id):
            return None
        installation = self._installation_for(plugin_id)
        if installation is None or not installation.active_path or not installation.python_path:
            return None
        manifest = installation.manifest if isinstance(installation.manifest, Mapping) else {}
        module, name = manifest.get("entry_module"), manifest.get("entry_class")
        if (not isinstance(module, str) or not isinstance(name, str) or not _ENTRY_PART.match(module)
                or not _CLASS_NAME.match(name)):
            return None
        hub = self._hub_dir()
        bundle = validated_bundle_path(hub, installation.active_path)
        interpreter = validated_interpreter(hub, installation.python_path)
        if bundle is None or interpreter is None:
            return None
        return Resolved(plugin_id, Path(interpreter), Path(bundle), f"{module}:{name}", manifest)

    def _manifest(self, plugin_id: str) -> Mapping[str, Any] | None:
        installation = self._installation_for(plugin_id)
        manifest = None if installation is None else installation.manifest
        return manifest if isinstance(manifest, Mapping) else None

    # ---------------------------------------------------------------------------------------------------- ports
    def _fixed_ports(self, manifest: Mapping[str, Any]) -> frozenset[int]:
        """The ports of the origins the plugin's manifest declares for its own code."""
        from provider_hub.plugin_manifest import parse_plugin_manifest

        return ports_of(parse_plugin_manifest(dict(manifest)).all_endpoints)

    def ports_for(self, plugin_id: str) -> frozenset[int]:
        """The ports the plugin's long-lived worker is admitted to connect to: DNS and the web ports, the origins its
        manifest declares and the addresses of the enabled connections it serves (and of its own enabled engines).

        Never Bazarr+'s own port: a connection to it is refused by the sandbox like any port that is not listed.
        """
        from .destinations import effective_destinations, schema_for_backend, schema_for_engine

        manifest = self._manifest(plugin_id)
        if manifest is None:
            return frozenset(DEFAULT_PORTS - set(self._reserved_ports()))
        found: set[int] = set(DEFAULT_PORTS)
        found |= self._fixed_ports(manifest)
        store = self._store_factory()
        for instance in store.list_instances():
            if not instance.enabled:
                continue
            if instance.plugin_id == plugin_id:
                found |= ports_of(effective_destinations(schema_for_engine(manifest), store.settings_of(instance)))
            for connection in store.list_connections(instance.id):
                if not connection.enabled or (connection.plugin_id or instance.plugin_id) != plugin_id:
                    continue
                schema = schema_for_backend(manifest, connection.backend_type)
                found |= ports_of(effective_destinations(schema, store.settings_of(connection)))
        return frozenset(found - set(self._reserved_ports()))

    def draft_ports(self, plugin_id: str, params: Mapping[str, Any]) -> frozenset[int]:
        """The ports a one-call worker needs for a ``test`` or ``options`` request: DNS and the web ports, the origins
        the manifest declares, and the addresses the draft in the request names."""
        from .destinations import effective_destinations, schema_for_backend, schema_for_engine

        manifest = self._manifest(plugin_id)
        found: set[int] = set(DEFAULT_PORTS)
        config = params.get("config") if isinstance(params, Mapping) else None
        if manifest is not None and isinstance(config, Mapping):
            found |= self._fixed_ports(manifest)
            settings = config.get("settings")
            if isinstance(settings, Mapping):
                found |= ports_of(effective_destinations(schema_for_engine(manifest), settings))
            connection = config.get("connection")
            if isinstance(connection, Mapping) and isinstance(connection.get("type"), str):
                values = connection.get("settings")
                schema = schema_for_backend(manifest, connection["type"])
                found |= ports_of(effective_destinations(schema, values if isinstance(values, Mapping) else {}))
        return frozenset(found - set(self._reserved_ports()))

    # ----------------------------------------------------------------------------------------------- definitions
    def definition_for(self, plugin_id: str, *, ports: Callable[[], frozenset[int]] | None = None,
                       key: str | None = None, call_id: str | None = None, cap_key: str | None = None):
        """The pool definition of the plugin's worker, or None when nothing can be run for it now.

        ``ports`` says which ports each start admits (default: ``ports_for`` the plugin), ``key`` names the pool
        entry (default: the plugin id) and ``call_id`` puts the worker's writable folders, and their budgets, in a
        folder of one short call instead of the plugin's own. ``cap_key`` is the name the pool counts workers that
        were ended without whole-tree proof under, so the cap is the plugin's however many keys its workers have.
        """
        from provider_hub.runtime.pool import FolderBudget, WorkerDefinition, launcher_spawn

        from .host_calls import host_calls

        if self.resolve(plugin_id) is None:
            return None
        data, temp = self._folders(plugin_id, call_id)
        wanted_ports = ports or (lambda: self.ports_for(plugin_id))
        return WorkerDefinition(
            key or plugin_id, kind=KIND, cap_key=cap_key,
            spawn=launcher_spawn(lambda: self._spec(plugin_id, wanted_ports(), call_id), probe=self._probe,
                                 require_sandbox=self._require_sandbox),
            host_calls=host_calls(),
            budgets=(FolderBudget(str(data), DATA_BUDGET_BYTES, "the plugin's data folder"),
                     FolderBudget(str(temp), TEMP_BUDGET_BYTES, "the plugin's temp folder")))

    def one_call_for(self, plugin_id: str, params: Mapping[str, Any], call_id: str):
        """The definition of the worker a ``test`` or ``options`` request runs in: it admits the draft's ports, and has
        a pool key and writable folders of its own."""
        if not _CALL_ID.match(call_id):
            return None
        draft = self.draft_ports(plugin_id, params)
        # Own key and folders per call; one cap for all of a plugin's short calls, apart from its long-lived worker's.
        return self.definition_for(plugin_id, ports=lambda: draft, key=f"{plugin_id}{workers.ONE_CALL_SUFFIX}:{call_id}",
                                   call_id=call_id, cap_key=plugin_id + workers.ONE_CALL_SUFFIX)

    def one_call_done(self, plugin_id: str, call_id: str) -> None:
        """Remove the folders a short request's worker wrote in. Only the call's own folder is touched, and a link in
        it is removed as a link, never followed."""
        if not _CALL_ID.match(call_id) or not _PLUGIN_ID.match(plugin_id):
            return
        shutil.rmtree(self._folders(plugin_id, call_id)[0].parent, ignore_errors=True)

    def installed(self, plugin_id: str) -> bool:
        """Whether the Plugin Hub has the plugin at all: running, held back, switched off, staged or being removed.
        Raises when the state cannot be read, so that nothing is taken for gone."""
        return plugin_id in self._known_ids()

    def plugin_removed(self, plugin_id: str) -> None:
        """Remove the plugin's writable folders. The caller has made sure nothing of the plugin is running."""
        if _PLUGIN_ID.match(plugin_id):
            shutil.rmtree(Path(self._hub_dir()) / "run" / plugin_id, ignore_errors=True)

    def sweep_orphans(self) -> list[str]:
        """Remove the writable folders of plugins that are not installed at all, left by a removal that was saved
        after the process that made it had stopped clearing. Every row of the Plugin Hub's inventory counts as
        installed, whatever its state: a plugin that is held back, switched off or only staged keeps its data. When the
        inventory cannot be read nothing is removed. Returns the plugin ids whose folders went."""
        run = Path(self._hub_dir()) / "run"
        removed: list[str] = []
        try:
            known = set(self._known_ids())
            children = sorted(run.iterdir())
        except OSError:
            return removed
        except Exception:
            logger.warning("The installed translators could not be read, so no folder was removed", exc_info=True)
            return removed
        for child in children:
            if child.is_symlink() or not child.is_dir() or not _PLUGIN_ID.match(child.name):
                continue
            if child.name in known or (child / KEPT_MARKER).exists():
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
        return removed

    def _folders(self, plugin_id: str, call_id: str | None = None) -> tuple[Path, Path]:
        base = Path(self._hub_dir()) / "run" / plugin_id
        if call_id is not None:
            base = base / "call" / call_id
        return base / "data", base / "tmp"

    def _spec(self, plugin_id: str, ports: Iterable[int], call_id: str | None = None):
        """One start's launch specification, built from what is installed at this moment."""
        from provider_hub.runtime.environment import worker_environment
        from provider_hub.sandbox.policy import PolicyError, RuntimeSpec, SandboxPolicy

        resolved = self.resolve(plugin_id)
        if resolved is None:
            raise PolicyError("translator %s has no release that can run" % plugin_id)
        data, temp = self._folders(plugin_id, call_id)
        for folder in (data, temp):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = Path(self._sdk_root or _sdk_root())
        config = Path(self._config_dir())
        policy = SandboxPolicy.for_interpreter(
            resolved.interpreter, read_only=[resolved.bundle, root], read_write=[data, temp],
            connect_ports=ports, reserved_ports=self._reserved_ports(),
            protected_paths=[config / "config", config / "db"])
        env = worker_environment(
            KIND, home=data, tmpdir=temp, path_prepend=[resolved.interpreter.parent],
            extra={"BAZARR_PLUGIN_DATA_DIR": os.fspath(data), "BAZARR_PLUGIN_TEMP_DIR": os.fspath(temp)})
        return RuntimeSpec(
            interpreter=os.fspath(resolved.interpreter),
            args=("-I", "-B", os.fspath(root / "bazarr_plugin_sdk" / "_bootstrap.py"), "--entry", resolved.entry,
                  "--path", os.fspath(resolved.bundle)),
            cwd=os.fspath(resolved.bundle), env=env, policy=policy)


# ------------------------------------------------------------------------------------------------ real sources
def _installed_ids() -> set[str]:
    """Every plugin the Plugin Hub's inventory has a row for, read strictly: a document that cannot be read raises."""
    from provider_hub.state import load_state_strict

    installations = load_state_strict().get("installations")
    if not isinstance(installations, dict):
        raise ValueError("the Plugin Hub inventory has no installations")
    return {str(plugin_id) for plugin_id in installations}



def _running_installation(plugin_id: str):
    from provider_hub.kinds import TRANSLATOR
    from provider_hub.state import running_installations

    for installation in running_installations(kind=TRANSLATOR):
        if installation.provider_id == plugin_id:
            return installation
    return None


def _default_store():
    from app.database import database

    from .store import TranslatorStore

    return TranslatorStore(database)


def _sdk_root() -> Path:
    """The folder that holds the vendored plugin SDK package."""
    import provider_hub

    return Path(provider_hub.__file__).resolve().parent / "sdk_dist"


def _hub_dir() -> Path:
    from provider_hub.state import provider_hub_dir

    return Path(provider_hub_dir())


def _config_dir() -> str:
    from app.get_args import args

    return args.config_dir


def _reserved_ports() -> frozenset[int]:
    """Bazarr+'s own port: never in a worker's set."""
    from app.config import settings

    try:
        return frozenset({int(settings.general.port)})
    except (AttributeError, TypeError, ValueError):
        return frozenset()


# ------------------------------------------------------------------------------------------------------ binding
_lock = threading.Lock()
_pool = None
_port: workers.PoolWorkers | None = None
_definitions: TranslatorWorkers | None = None
# Plugins removed whose folders are still to be cleared, and the timer that looks again.
_pending_removal: set[str] = set()
_removal_timer: threading.Timer | None = None
REMOVAL_RECHECK_SECONDS = 10.0


def bind(pool, definitions: TranslatorWorkers | None = None) -> workers.PoolWorkers:
    """Bind the dispatcher to ``pool``. Returns the port that was bound."""
    global _pool, _port, _definitions
    definitions = definitions or TranslatorWorkers()
    port = workers.PoolWorkers(pool, definitions.definition_for, one_call_for=definitions.one_call_for,
                               one_call_done=definitions.one_call_done, ports_for=definitions.ports_for)
    with _lock:
        _pool, _port, _definitions = pool, port, definitions
    workers.bind(port)
    return port


def bind_when_enabled() -> bool:
    """Bind the real workers when the runtime switch is on, once. True when a binding is in place.

    Reads the switch itself and touches no table, so it runs at boot before the database is migrated.
    """
    from . import runtime_v2_flag

    if not runtime_v2_flag():
        return False
    with _lock:
        if _port is not None:
            return True
    from provider_hub.runtime.pool import WorkerPool

    try:
        pool = WorkerPool(config_dir=_config_dir())
    except Exception:
        logger.exception("Could not start the translator worker pool")
        return False
    bind(pool)
    try:
        sweep_removed_plugins()
    except Exception:
        logger.warning("Could not clear the folders of removed translators", exc_info=True)
    logger.debug("The translation runtime is bound to its translator workers")
    return True


def plugin_changed(plugin_id: str) -> None:
    """A new release of the plugin is active, or it was turned off or removed: its worker is retired once the jobs it
    holds are done, and the next start reads what is installed then. Nothing happens while no binding is in place."""
    with _lock:
        port = _port
    if port is not None:
        port.retire(plugin_id)


def plugin_detached(plugin_id: str, grace: float | None = None) -> "list[threading.Thread]":
    """The plugin was removed or revoked and its release is taken out of use now: nothing new is sent to its
    worker, the jobs it holds finish (or are cut after ``grace`` seconds), and the worker goes. Returns the
    threads that end when nothing of it runs. Empty while no binding is in place."""
    with _lock:
        port = _port
    if port is None:
        return []
    return port.detach(plugin_id, grace=grace)


def _set_kept(plugin_id: str, kept: bool) -> None:
    """Mark the plugin's writable folders as kept on purpose, or take the mark off. Works whether or not a binding
    is in place, since the mark is read by a later start, which may be bound when this process was not. A plugin
    with no folder has nothing to mark."""
    with _lock:
        definitions = _definitions
    folder = (definitions._hub_dir() if definitions is not None else _hub_dir()) / "run" / plugin_id
    if not _PLUGIN_ID.match(plugin_id) or folder.is_symlink():
        return
    marker = folder / KEPT_MARKER
    try:
        if kept:
            if folder.is_dir():
                marker.touch()
        else:
            marker.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not %s the mark on the folders of translator %s", "set" if kept else "clear", plugin_id,
                       exc_info=True)


def discard_folders(plugin_id: str) -> None:
    """Delete the plugin's writable folders now, mark and all. For data that was kept and is not wanted any more, with
    no worker of the plugin running because it is not installed. Works whether or not a binding is in place. A folder
    that cannot be deleted raises, since the caller must not go on as if it were gone."""
    with _lock:
        definitions = _definitions
    folder = (definitions._hub_dir() if definitions is not None else _hub_dir()) / "run" / plugin_id
    if _PLUGIN_ID.match(plugin_id) and not folder.is_symlink():
        try:
            shutil.rmtree(folder)
        except FileNotFoundError:
            pass


def plugin_installed(plugin_id: str) -> None:
    """The plugin was installed, which ends whatever an earlier removal said about its folders: they are in use
    again, and a removal made later decides afresh."""
    _set_kept(plugin_id, False)


def plugin_removed(plugin_id: str) -> None:
    """The plugin was removed and its data is not kept: any earlier mark that kept it is taken off at once, its worker
    is retired once its jobs are done, and its writable folders are cleared once nothing of it is running and the
    Plugin Hub no longer has it (a removal of an active plugin waits for a restart, and the sweep after that start
    clears them). Until then it is looked at every few seconds, and a start finds whatever is left. Nothing else
    happens while no binding is in place."""
    _set_kept(plugin_id, False)
    with _lock:
        port = _port
    if port is None:
        return
    port.retire(plugin_id)
    with _lock:
        _pending_removal.add(plugin_id)
    _sweep_removed()


def plugin_kept(plugin_id: str) -> None:
    """The plugin was removed and its data is kept: its folders are marked so that no sweep clears them, and its
    worker is retired once its jobs are done."""
    with _lock:
        port = _port
        # A removal asked for earlier is overtaken by this answer.
        _pending_removal.discard(plugin_id)
    _set_kept(plugin_id, True)
    if port is not None:
        port.retire(plugin_id)


def _sweep_removed(from_timer: bool = False) -> None:
    global _removal_timer
    with _lock:
        port, definitions, pending = _port, _definitions, set(_pending_removal)
        if from_timer:
            _removal_timer = None
    if port is None or definitions is None:
        return
    cleared = set()
    for plugin_id in pending:
        try:
            if definitions.installed(plugin_id):
                # A removal of a plugin that is running is staged until the next start, and the sweep after that
                # start clears it: nothing here changes before then, so there is nothing to look at again.
                cleared.add(plugin_id)
            elif port.idle(plugin_id):
                definitions.plugin_removed(plugin_id)
                cleared.add(plugin_id)
        except Exception:
            logger.warning("Could not clear the folders of translator %s", plugin_id, exc_info=True)
    with _lock:
        _pending_removal.difference_update(cleared)
        if _pending_removal and _port is port and _removal_timer is None:
            _removal_timer = threading.Timer(REMOVAL_RECHECK_SECONDS, _sweep_removed, args=(True,))
            _removal_timer.daemon = True
            _removal_timer.start()


def sweep_removed_plugins() -> list[str]:
    """Clear the folders of translators that are not installed any more. Run after the staged installations have been
    activated at start, which is when a removal made in the last run is finished. Nothing happens while no binding is in
    place."""
    with _lock:
        definitions = _definitions
    if definitions is None:
        return []
    removed = definitions.sweep_orphans()
    for plugin_id in removed:
        logger.info("Cleared the folders of translator %s, which is no longer installed", plugin_id)
    return removed


def shutdown() -> None:
    """Stop every translator worker and unbind. Safe to call more than once, or when nothing was bound."""
    global _pool, _port, _definitions, _removal_timer
    with _lock:
        pool, port = _pool, _port
        _pool = _port = _definitions = None
        timer, _removal_timer = _removal_timer, None
        _pending_removal.clear()
    if timer is not None:
        timer.cancel()
    if port is None:
        return
    workers.unbind()
    try:
        pool.close()
    except Exception:
        logger.warning("The translator worker pool did not close cleanly", exc_info=True)
