# coding=utf-8
"""Bounded, independent native refresh workers and saved connection revisions."""

import logging
import os
from dataclasses import dataclass, field, replace
from threading import Condition, RLock, Thread

from . import resolution
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
    # How much scannable work this destination has been handed. A library scan
    # covers what was published before it, and nothing after it.
    publications: int = 0


def _coalescer(scanned, publications):
    """One scan of a library per drain, until new work arrives for it.

    A library scan is recursive, so a single one serves every file the library
    holds. Without this, a bulk mod or a season pack that lands each of its
    targets on the library rung asks the same library to rescan itself once per
    video, up to the whole pending queue.

    A publication that arrives after a scan was submitted is not covered by it,
    so the count of publications the destination has taken is part of the key.
    That errs towards one scan too many rather than one too few, which is the
    side to err on: a suppressed scan the file needed would never be asked for
    again.
    """
    def already_requested(library):
        key = str(library)
        if scanned.get(key) == publications:
            return True
        scanned[key] = publications
        return False
    return already_requested


def _refusal_code(error):
    """The refusal code an error carries, whatever generation raised it.

    This module binds ``MediaServerError`` when it is imported and builds its
    clients lazily, at the first refresh. Nothing makes those two moments share
    a module generation: test isolation evicts and re-imports these modules,
    and an identically named class from another generation is a different
    object, which ``except MediaServerError`` does not catch. Every generation
    agrees on the code, so that is what is matched, and a refusal keeps its
    meaning instead of collapsing into internal_error.
    """
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


def _misses_on(call, codes):
    """Read a refusal this rung declared as "not here", not as a failure.

    One client method predates the ladder and answers "no such item" by
    raising. Translating that lives here, at the adapter boundary, so the
    walk itself never has to know one destination's vocabulary.
    """
    def attempt():
        try:
            return call()
        except Exception as error:
            if _refusal_code(error) in codes:
                return None
            raise
    return attempt


def _relocated(event):
    """Where a retained unsupported publication's subtitle sits now, if it moved.

    The warning names a path the user is told to fix, and moving that file
    publishes nothing. Replaying the recorded path would answer the same
    refusal forever, so the retained event has to be re-derived against the
    filesystem instead: the same name beside the video, gone from where it was
    recorded, is the move the warning asked for. Anything else is not, and
    keeps the warning.
    """
    if event is None:
        return None
    try:
        video, subtitle = _media_path(event.video_path), _media_path(event.subtitle_path)
    except MediaServerError:
        return None
    if subtitle.parent == video.parent:
        return None
    moved = video.parent / subtitle.name
    if os.path.isfile(event.subtitle_path) or not os.path.isfile(str(moved)):
        return None
    return replace(event, subtitle_path=str(moved))


def _metadata(event):
    return resolution.media_metadata(event.media_type, event.video_path, event.arr_instance_id)


def _client(server, snapshot):
    if server == "emby":
        from emby.client import EmbyClient
        return EmbyClient(snapshot.url, snapshot.apikey, snapshot.verify_ssl)
    from silo.client import SiloClient
    return SiloClient(snapshot.url, snapshot.apikey, snapshot.verify_ssl)


class RefreshDispatcher:
    PENDING_LIMIT = 128

    def __init__(self, configuration, *, client_factory=_client, metadata_factory=_metadata):
        self.configuration = configuration
        self.client_factory = client_factory
        self.metadata_factory = metadata_factory
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
                        state.publications += 1
                elif len(state.targets) < self.PENDING_LIMIT:
                    target = state.targets[key] = _Target(event)
                    state.publications += 1
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

    def _refresh(self, instance_id, revision, snapshot, event, coalesce):
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
            if server == "silo":
                libraries = client.get_libraries()
                library = next((row for row in libraries if row["id"].lstrip("0") ==
                                mapped["library_id"].lstrip("0")), None)
                if (library is None or library["type"] != ("movies" if event.media_type == "movie" else "series")
                        or not any(_media_path(mapped["path"]).is_relative_to(_media_path(root))
                                   for root in library["paths"])):
                    raise MediaServerError("library_invalid")
                guard()
            resolved = resolution.walk(self._ladder(client, server, event, mapped, guard, coalesce))
            guard()
            if resolved is None:
                # Every rung this destination has missed, so it holds no such file.
                raise MediaServerError("item_missing")
            _rung, result, expected = resolved
            if result != {"status": expected}:
                raise MediaServerError("invalid_response")
            return expected

    def _ladder(self, client, server, event, mapped, guard, coalesce):
        """The rungs this destination declared it can climb, in ladder order.

        The identifiers come out of the rows Bazarr already wrote, read here
        rather than carried through every publication callback, and the rungs
        that need them disappear when they cannot be read.
        """
        supported = set(getattr(client, "REFRESH_STEPS", ()) or ())
        metadata = (self.metadata_factory(event)
                    if supported & {resolution.PROVIDER_ID, resolution.TITLE_YEAR} else None)
        if metadata is None:
            supported -= {resolution.PROVIDER_ID, resolution.TITLE_YEAR}
        if server == "emby":
            # item_missing is Emby saying it holds no such file, which is a
            # miss; item_ambiguous and every strict acceptance refusal are not,
            # and must not be laundered into a refresh of something broader.
            available = {
                resolution.PROVIDER_ID: (lambda: client.refresh_by_provider_id(
                    event.media_type, metadata, ensure_current=guard), "requested"),
                resolution.TITLE_YEAR: (lambda: client.refresh_by_title_year(
                    event.media_type, metadata, mapped["path"], ensure_current=guard), "requested"),
                resolution.PATH: (_misses_on(lambda: client.refresh_item(
                    event.media_type, mapped["path"], ensure_current=guard), ("item_missing",)), "requested"),
                resolution.LIBRARY: (lambda: client.refresh_library(
                    event.media_type, mapped["path"], ensure_current=guard,
                    coalesce=coalesce), "requested"),
            }
        else:
            # Silo's file rung decides its own miss and answers with nothing,
            # because only Silo can tell the documented 400 for a path it cannot
            # place from the conflicts, rate limits and refused event-stream
            # handshakes it folds into the same code. A scan that ran and fell
            # short, and every refusal, is a failure and keeps its own code.
            available = {
                resolution.PATH: (lambda: client.refresh_file(
                    mapped["library_id"], mapped["path"], ensure_current=guard), "confirmed"),
                resolution.LIBRARY: (lambda: client.refresh_library(
                    mapped["library_id"], ensure_current=guard, coalesce=coalesce), "requested"),
            }
        return [(name, *available[name]) for name in resolution.CHAIN
                if name in supported and name in available]

    def _run(self, server, state):
        # One drain, one scan per library. The memo dies with this worker, so a
        # later pass asks again rather than inheriting a stale suppression.
        scanned = {}
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
                publications = state.publications
                target.ready = False
                target.error_code = None
                state.state = "pending"
            try:
                result = self._refresh(server, revision, snapshot, event,
                                       _coalescer(scanned, publications))
                error_code = None
            except Exception as error:
                # Classified by the code, not the class: see _refusal_code.
                code = _refusal_code(error)
                result = "unconfirmed"
                error_code = code if code in _ERROR_CODES else "internal_error"
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
                    moved = _relocated(target.unsupported_event)
                    if moved is not None:
                        # The subtitle now sits where Silo reads it, so the
                        # warning has real work again rather than a path the
                        # user already fixed. Queue that work instead of
                        # counting it as covered by the scan just finished.
                        target.event, target.unsupported_event = moved, None
                        target.generation += 1
                        target.ready = True
                    elif target.unsupported_event is None:
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
                # Retry is the button the warning tells the user to press after
                # moving the subtitle, so this is where the recorded path has to
                # be re-derived rather than replayed.
                moved = _relocated(target.unsupported_event)
                if moved is not None:
                    target.event, target.unsupported_event = moved, None
                    target.generation += 1
                    target.error_code = None
                target.ready = True
            # Retry is the user asking again. A library scan submitted before
            # they pressed it is not the answer to it, so a pass still running
            # must not treat one as already covered.
            state.publications += 1
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
