# coding=utf-8
"""Bounded, independent native refresh workers and saved connection revisions."""

import logging
from dataclasses import dataclass, field, replace
from threading import Condition, RLock, Thread

from .events import SubtitleMutation
from .http import MediaServerError
from .paths import _media_path, map_media_path

_SERVERS = ("emby", "silo")
_OPERATIONS = {"download", "upload", "delete", "sync", "translate", "combine", "edit"}
_ERROR_CODES = {
    "configuration_changed", "connection_disabled", "connection_error", "internal_error", "invalid_response",
    "invalid_url", "invalid_verify_ssl", "missing_credentials", "unauthorized", "forbidden", "not_found",
    "request_rejected", "redirect_denied", "response_too_large", "timeout", "tls_error", "item_missing",
    "item_ambiguous", "path_invalid", "mapping_invalid", "mapping_missing", "mapping_ambiguous",
    "library_missing", "library_invalid", "sidecar_unsupported", "observation_incomplete", "stream_disconnected",
    "scan_incomplete", "scan_failed", "scan_cancelled", "queue_overflow",
}


class NativeConfiguration:
    """Publish saved destinations under the request authorization lock."""

    def __init__(self, settings, *, snapshots=(), blocked_kinds=()):
        self.lock = RLock()
        self.blocked_kinds = frozenset(blocked_kinds)
        self.snapshots = {snapshot.id: snapshot for snapshot in snapshots}
        self.masters = {kind: getattr(settings.general, 'use_' + kind) is True for kind in _SERVERS}

    def publish(self, snapshot):
        with self.lock:
            if snapshot.kind in self.blocked_kinds:
                return
            previous = self.snapshots.get(snapshot.id)
            revision = previous.revision + 1 if previous else snapshot.revision
            self.snapshots[snapshot.id] = replace(snapshot, revision=revision,
                                                  master_enabled=self.masters[snapshot.kind])

    def delete(self, instance_id):
        with self.lock:
            self.snapshots.pop(instance_id, None)

    def publish_masters(self, settings):
        with self.lock:
            for kind in _SERVERS:
                enabled = getattr(settings.general, 'use_' + kind) is True
                if self.masters[kind] == enabled:
                    continue
                self.masters[kind] = enabled
                for instance_id, snapshot in list(self.snapshots.items()):
                    if snapshot.kind == kind:
                        self.snapshots[instance_id] = replace(snapshot, master_enabled=enabled,
                                                             revision=snapshot.revision + 1)

    def list(self):
        with self.lock:
            return tuple(self.snapshots.values())

    def read(self, instance_id):
        with self.lock:
            snapshot = self.snapshots.get(instance_id)
            if snapshot is None:
                raise MediaServerError('not_found')
            return snapshot.revision, snapshot, False

    def ensure_current(self, instance_id, revision):
        with self.lock:
            snapshot = self.snapshots.get(instance_id)
            if snapshot is None or snapshot.revision != revision or not snapshot.enabled:
                raise MediaServerError('configuration_changed')


@dataclass
class _Target:
    event: SubtitleMutation
    # One representative is enough to retain the warning without a path backlog.
    unsupported_event: SubtitleMutation | None = None
    generation: int = 1
    ready: bool = True
    error_code: str | None = None


@dataclass
class _ServerState:
    revision: int = 0
    targets: dict = field(default_factory=dict)
    worker: Thread | None = None
    state: str = "idle"
    error_code: str | None = None
    overflow: bool = False


def _client(server, snapshot):
    if server == "emby":
        from emby.client import EmbyClient
        return EmbyClient(snapshot.url, snapshot.apikey, snapshot.verify_ssl)
    from silo.client import SiloClient
    return SiloClient(snapshot.url, snapshot.apikey, snapshot.verify_ssl)


class RefreshDispatcher:
    PENDING_LIMIT = 128

    def __init__(self, configuration, *, client_factory=_client):
        self.configuration = configuration
        self.client_factory = client_factory
        self.condition = Condition(RLock())
        self.servers = {}

    def _connection(self, server, state):
        revision, snapshot, changing = self.configuration.read(server)
        if state.revision != revision:
            state.revision = revision
            state.state = 'unconfirmed' if state.targets else 'idle'
            state.error_code = 'configuration_changed' if state.targets else None
            for target in state.targets.values():
                target.error_code = 'configuration_changed'
        return revision, snapshot, changing

    def _server(self, server):
        revision, _snapshot, _changing = self.configuration.read(server)
        return self.servers.setdefault(server, _ServerState(revision=revision))

    def _prune_deleted(self):
        live = {snapshot.id for snapshot in self.configuration.list()}
        for instance_id in tuple(self.servers):
            if instance_id not in live:
                del self.servers[instance_id]

    @property
    def worker_count(self):
        with self.condition:
            return sum(state.worker is not None for state in self.servers.values())

    def notify(self, event):
        media_type = {"series": "episode", "movies": "movie"}.get(event.media_type, event.media_type)
        if media_type not in {"movie", "episode"} or event.operation not in _OPERATIONS:
            return
        event = SubtitleMutation(media_type, str(_media_path(event.video_path)), event.subtitle_path,
                                 event.operation, event.arr_instance_id)
        key = (media_type, _media_path(event.video_path), event.arr_instance_id)
        with self.condition, self.configuration.lock:
            self._prune_deleted()
            for snapshot in self.configuration.list():
                server = snapshot.id
                state = self._server(server)
                if snapshot.kind == "emby" and event.operation not in {"download", "upload"}:
                    continue
                _revision, snapshot, changing = self._connection(server, state)
                if not snapshot.enabled:
                    continue
                try:
                    map_media_path(event.video_path, snapshot.mappings(), require_library=snapshot.kind == 'silo')
                except MediaServerError as error:
                    if error.code == 'mapping_missing':
                        continue
                unsupported = False
                if snapshot.kind == 'silo':
                    try:
                        unsupported = _media_path(event.subtitle_path).parent != _media_path(event.video_path).parent
                    except MediaServerError:
                        # Keep invalid paths for the worker's normal validation.
                        pass
                if key in state.targets:
                    target = state.targets[key]
                    if not unsupported:
                        target.event = event
                        target.generation += 1
                        target.ready = True
                elif len(state.targets) < self.PENDING_LIMIT:
                    target = state.targets[key] = _Target(event)
                else:
                    state.overflow = True
                    continue
                if unsupported:
                    target.unsupported_event = target.unsupported_event or event
                if changing:
                    state.state, state.error_code = "unconfirmed", "configuration_changed"
                else:
                    self._start(server, state)

    def _start(self, server, state):
        if state.worker is None and any(target.ready for target in state.targets.values()):
            state.state, state.error_code = 'pending', None
            kind = self.configuration.read(server)[1].kind
            state.worker = Thread(target=self._run, args=(server, state),
                                  name=kind + "-subtitle-refresh", daemon=True)
            state.worker.start()

    def _refresh(self, instance_id, revision, snapshot, event):
        server = snapshot.kind
        from subtitles.tools.subsync_engines import subtitle_write_locks

        def guard():
            self.configuration.ensure_current(instance_id, revision)
        guard()
        if snapshot.configuration_error:
            raise MediaServerError(snapshot.configuration_error)
        # Publication callbacks run before local cleanup releases these locks.
        # Wait for that cleanup, then release before any native network work.
        with subtitle_write_locks(event.video_path, event.subtitle_path):
            pass
        guard()
        mapped = map_media_path(event.video_path, snapshot.mappings(), require_library=server == "silo")
        if server == "silo" and _media_path(event.subtitle_path).parent != _media_path(event.video_path).parent:
            raise MediaServerError("sidecar_unsupported")
        with self.client_factory(server, snapshot) as client:
            guard()
            if server == "emby":
                result = client.refresh_item(event.media_type, mapped["path"], ensure_current=guard)
            else:
                libraries = client.get_libraries()
                library = next((row for row in libraries if row["id"].lstrip("0") ==
                                mapped["library_id"].lstrip("0")), None)
                if (library is None or library["type"] != ("movies" if event.media_type == "movie" else "series")
                        or not any(_media_path(mapped["path"]).is_relative_to(_media_path(root))
                                   for root in library["paths"])):
                    raise MediaServerError("library_invalid")
                guard()
                result = client.refresh_file(mapped["library_id"], mapped["path"], ensure_current=guard)
            guard()
            expected = "requested" if server == "emby" else "confirmed"
            if result != {"status": expected}:
                raise MediaServerError("invalid_response")
            return expected

    def _run(self, server, state):
        while True:
            with self.condition:
                try:
                    revision, snapshot, changing = self._connection(server, state)
                except MediaServerError:
                    state.worker = None
                    if self.servers.get(server) is state:
                        del self.servers[server]
                    self.condition.notify_all()
                    return
                target_pair = next(((key, value) for key, value in state.targets.items() if value.ready), None)
                if target_pair is None or changing or not snapshot.enabled:
                    if state.targets:
                        state.state = 'unconfirmed'
                        state.error_code = next((target.error_code for target in state.targets.values()
                                                 if target.error_code), 'configuration_changed')
                    if state.targets and (changing or not snapshot.enabled):
                        state.state = "unconfirmed"
                        state.error_code = "configuration_changed" if changing else "connection_disabled"
                    state.worker = None
                    self.condition.notify_all()
                    return
                key, target = target_pair
                event, generation = target.event, target.generation
                target.ready = False
                target.error_code = None
                state.state = "pending"
            try:
                result = self._refresh(server, revision, snapshot, event)
                error_code = None
            except MediaServerError as error:
                result = "unconfirmed"
                error_code = error.code if error.code in _ERROR_CODES else "internal_error"
            except Exception:
                result, error_code = "unconfirmed", "internal_error"
            with self.condition:
                if self.servers.get(server) is not state:
                    state.worker = None
                    self.condition.notify_all()
                    return
                try:
                    self.configuration.ensure_current(server, revision)
                except MediaServerError:
                    result, error_code = "unconfirmed", "configuration_changed"
                if not error_code and state.targets[key].generation == generation:
                    if target.unsupported_event is None:
                        del state.targets[key]
                    else:
                        # The scan completed eligible work only. Retry the
                        # retained unsupported publication without claiming it
                        # was covered by this scan.
                        target.event = target.unsupported_event
                        result, error_code = 'unconfirmed', 'sidecar_unsupported'
                        target.error_code = error_code
                elif error_code:
                    state.targets[key].error_code = error_code
                state.state, state.error_code = result, error_code
                self.condition.notify_all()

    def status(self, server):
        with self.condition:
            state = self._server(server)
            _revision, snapshot, changing = self._connection(server, state)
            error = state.error_code or snapshot.configuration_error
            status = "unconfirmed" if snapshot.configuration_error else state.state
            if any(target.unsupported_event is not None for target in state.targets.values()):
                error = error or 'sidecar_unsupported'
                if status in {'idle', 'requested', 'confirmed'}:
                    status = 'unconfirmed'
            if state.targets and (changing or not snapshot.enabled):
                status = "unconfirmed"
                error = "configuration_changed" if changing else "connection_disabled"
            return {"pending": len(state.targets), "state": status,
                    "error_code": "queue_overflow" if state.overflow else error}

    def retry(self, server):
        with self.condition:
            state = self._server(server)
            _revision, snapshot, changing = self._connection(server, state)
            if changing or not snapshot.enabled:
                return 0
            for target in state.targets.values():
                target.ready = True
            self._start(server, state)
            return len(state.targets)

    def wait_idle(self, timeout, *, server=None):
        with self.condition:
            states = [self._server(server)] if server else list(self.servers.values())
            return self.condition.wait_for(lambda: all(state.worker is None for state in states), timeout)


_singleton_lock = RLock()
_configuration = None
_dispatcher = None


def get_native_configuration():
    global _configuration
    with _singleton_lock:
        if _configuration is None:
            from app.config import settings
            from app.database import database, TableMediaServerImports
            from .repository import MediaServerInstanceRepository
            kinds = [kind for kind in _SERVERS if database.get(TableMediaServerImports, kind) is not None]
            snapshots = MediaServerInstanceRepository(database).snapshots(settings, kinds=kinds)
            _configuration = NativeConfiguration(settings, snapshots=snapshots,
                                                 blocked_kinds=set(_SERVERS) - set(kinds))
        return _configuration


def _get_dispatcher():
    global _dispatcher
    with _singleton_lock:
        if _dispatcher is None:
            _dispatcher = RefreshDispatcher(get_native_configuration())
        return _dispatcher


def notify_subtitle_mutation(event: SubtitleMutation) -> None:
    try:
        _get_dispatcher().notify(event)
    except Exception:
        logging.warning("BAZARR native subtitle refresh could not be queued")


def get_refresh_status(server: str) -> dict:
    return _get_dispatcher().status(server)


def retry_pending_refreshes(server: str) -> int:
    return _get_dispatcher().retry(server)
