"""Owned SSE readers with cancellable transports and coalesced repair work."""
from dataclasses import dataclass, field
import json
import logging
from threading import Event, Lock, Thread, current_thread
from time import monotonic

from sqlalchemy import select

from app.database import database, TableSportsEvents, TableSportsLeagues
from arr_instances.client import ArrClientFactory
from arr_instances.repository import ArrInstanceRepository
from sportarr.connection import check_cancelled, connection_identity
from sportarr.transport import OwnedHTTPTransport
from sportarr.parser import positive_id


@dataclass(frozen=True)
class SSEFrame:
    id: str | None
    event: str
    data: str


def parse_sse(lines):
    """Dispatch only complete frames, with multiline data and per-frame IDs."""
    event_id, event, data, size = None, 'message', [], 0
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode('utf-8')
        line = line.rstrip('\r\n')
        if not line:
            if data:
                yield SSEFrame(event_id, event, '\n'.join(data))
            event_id, event, data, size = None, 'message', [], 0
            continue
        size += len(line)
        if size > 1024 * 1024:
            raise ValueError('Sportarr stream frame is too large')
        if line.startswith(':'):
            continue
        name, _, value = line.partition(':')
        if value.startswith(' '):
            value = value[1:]
        if name == 'id' and '\x00' not in value:
            event_id = value
        elif name == 'event':
            event = value
        elif name == 'data':
            data.append(value)


@dataclass
class PendingWork:
    full: bool = False
    league_ids: set[int] = field(default_factory=set)
    event_ids: set[int] = field(default_factory=set)


def reconcile_work(owner, batch, *, cancel, expected_connection, http_get=None):
    """Resolve native IDs only within the stream's owner, then use local sync IDs."""
    from sportarr.sync.leagues import require_sportarr, update_sports_for_instance
    from sportarr.sync.events import sync_events
    check_cancelled(cancel)
    instance = require_sportarr(database, owner)
    if connection_identity(instance) != expected_connection:
        raise ValueError('Sportarr stream connection changed')
    kwargs = dict(cancel=cancel, expected_connection=expected_connection, http_get=http_get)
    if batch.full:
        update_sports_for_instance(owner, **kwargs)
        return
    upstream_leagues = set(batch.league_ids)
    client = ArrClientFactory().from_row(instance, http_get=http_get)
    for event_id in batch.event_ids:
        check_cancelled(cancel)
        found = database.execute(select(TableSportsLeagues.sportarrLeagueId).join(
            TableSportsEvents, (TableSportsEvents.league_id == TableSportsLeagues.id) &
            (TableSportsEvents.arr_instance_id == TableSportsLeagues.arr_instance_id)).where(
                TableSportsEvents.arr_instance_id == owner, TableSportsEvents.sportarrEventId == event_id)
            .distinct()).scalars().all()
        if found:
            upstream_leagues.update(found)
            continue
        try:
            response = client.get(f'/api/events/{event_id}')
            if response.status_code == 404:
                batch.full = True
                break
            if response.status_code != 200:
                raise ValueError()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get('id') != event_id or not positive_id(payload.get('leagueId')):
                raise ValueError()
        except Exception:
            raise ValueError('Could not resolve the Sportarr stream event') from None
        upstream_leagues.add(payload['leagueId'])
    local_leagues = database.execute(select(TableSportsLeagues.id, TableSportsLeagues.sportarrLeagueId).where(
        TableSportsLeagues.arr_instance_id == owner, TableSportsLeagues.sportarrLeagueId.in_(upstream_leagues))).all()
    if batch.full or len(local_leagues) != len(upstream_leagues):
        update_sports_for_instance(owner, **kwargs)
    else:
        for league_id, _ in local_leagues:
            sync_events(league_id, owner, **kwargs)


def _release_database_session():
    database.remove()


class SportarrSSEClient:
    def __init__(self, owner, identity, client, *, transport_factory=OwnedHTTPTransport,
                 reconcile=reconcile_work, debounce=0.25, retry_min=1, retry_max=30):
        self.owner, self.identity, self.client = owner, identity, client
        self.transport_factory, self.reconcile = transport_factory, reconcile
        self.debounce, self.retry_min, self.retry_max = debounce, retry_min, retry_max
        self.last_event_id = None
        self.connected = False
        self._stop, self._wake = Event(), Event()
        self._lock = Lock()
        self._pending = PendingWork()
        self._transport = self._work_transport = None
        self._threads = []

    def start(self):
        if self._threads or self._stop.is_set():
            return
        self.enqueue(full=True)
        self._threads = [Thread(target=self._read, name=f'sportarr-stream-{self.owner}', daemon=True),
                         Thread(target=self._work, name=f'sportarr-repair-{self.owner}', daemon=True)]
        for thread in self._threads:
            thread.start()

    def enqueue(self, *, full=False, league_id=None, event_id=None):
        with self._lock:
            if self._stop.is_set():
                return False
            self._pending.full |= full
            if league_id is not None:
                self._pending.league_ids.add(league_id)
            if event_id is not None:
                self._pending.event_ids.add(event_id)
            if self._pending.full or len(self._pending.league_ids) + len(self._pending.event_ids) > 1000:
                self._pending = PendingWork(full=True)
            self._wake.set()
            return True

    def dispatch(self, frame):
        try:
            payload = json.loads(frame.data)
            if not isinstance(payload, dict):
                raise ValueError()
        except ValueError:
            return self.enqueue(full=True)
        cursor = frame.id
        if frame.event == 'stream.resync':
            accepted = self.enqueue(full=True)
            cursor = str(payload['lastId']) if positive_id(payload.get('lastId')) else None
        else:
            resource = payload.get('resourceType') or frame.event.partition('.')[0]
            if resource == 'league':
                accepted = self.enqueue(full=True)
            elif resource in ('event', 'file'):
                league_id, event_id = payload.get('leagueId'), payload.get('eventId')
                if positive_id(league_id):
                    accepted = self.enqueue(league_id=league_id)
                elif positive_id(event_id):
                    accepted = self.enqueue(event_id=event_id)
                else:
                    accepted = self.enqueue(full=True)
            else:
                accepted = self.enqueue(full=True)
        if accepted and cursor is not None:
            if cursor.isdecimal():
                # A replaced server can reset cursors. Repair was enqueued before
                # this acknowledgment; every new connection also queues repair.
                if self.last_event_id is not None and int(cursor) < int(self.last_event_id):
                    self.enqueue(full=True)
                self.last_event_id = cursor
            else:
                self.enqueue(full=True)
        return accepted

    def _opened(self):
        self.connected = True
        # Repair again after subscription to cover changes during startup repair.
        self.enqueue(full=True)

    def _read(self):
        delay = self.retry_min
        while not self._stop.is_set():
            transport = self.transport_factory()
            with self._lock:
                self._transport = transport
                stopped = self._stop.is_set()
            opened_at = monotonic()
            try:
                if stopped:
                    return
                for frame in parse_sse(transport.stream(self.client, self.last_event_id, self._opened)):
                    if self._stop.is_set():
                        break
                    self.dispatch(frame)
            except Exception:
                if not self._stop.is_set():
                    logging.debug('Sportarr stream disconnected for instance %s', self.owner)
            finally:
                self.connected = False
                transport.close()
                with self._lock:
                    self._transport = None
            if monotonic() - opened_at > 60:
                delay = self.retry_min
            if self._stop.wait(delay):
                break
            delay = min(self.retry_max, delay * 2)

    def _work(self):
        delay = self.retry_min
        try:
            while not self._stop.is_set():
                self._wake.wait()
                if self._stop.wait(self.debounce):
                    break
                transport = OwnedHTTPTransport()
                with self._lock:
                    batch, self._pending = self._pending, PendingWork()
                    self._wake.clear()
                    self._work_transport = transport
                try:
                    check_cancelled(self._stop)
                    self.reconcile(self.owner, batch, cancel=self._stop,
                                   expected_connection=self.identity, http_get=transport.get)
                    delay = self.retry_min
                except Exception:
                    if not self._stop.is_set():
                        logging.warning('Sportarr reconciliation will retry for instance %s', self.owner)
                        if not self._stop.wait(delay):
                            self.enqueue(full=True)
                        delay = min(self.retry_max, delay * 2)
                finally:
                    transport.close()
                    with self._lock:
                        self._work_transport = None
                    _release_database_session()
        finally:
            _release_database_session()

    def is_alive(self):
        return any(thread.is_alive() for thread in self._threads)

    def stop(self, timeout=6):
        self._stop.set()
        self._wake.set()
        with self._lock:
            transports = (self._transport, self._work_transport)
            self._pending = PendingWork()
        for transport in transports:
            if transport is not None:
                transport.close()
        deadline = monotonic() + timeout
        for thread in self._threads:
            if thread is not current_thread():
                thread.join(max(0, deadline - monotonic()))
        return not self.is_alive()


class SportarrClientManager:
    def __init__(self, client_factory=SportarrSSEClient):
        self.clients = {}
        self._lock = Lock()
        self._factory = client_factory
        self._shutdown = False

    def refresh(self, session):
        with self._lock:
            if self._shutdown:
                return
            instances = ArrInstanceRepository(session).list('sportarr', enabled_only=True)
            wanted = {row.id: row for row in instances}
            for owner, client in list(self.clients.items()):
                row = wanted.get(owner)
                if row is None or client.identity != connection_identity(row) or not client.is_alive():
                    if client.stop(timeout=35):
                        del self.clients[owner]
            for owner, row in wanted.items():
                if owner not in self.clients:
                    client = self._factory(owner, connection_identity(row), ArrClientFactory().from_row(row))
                    self.clients[owner] = client
                    client.start()

    def stop(self):
        with self._lock:
            self._shutdown = True
            for owner, client in list(self.clients.items()):
                if client.stop(timeout=35):
                    del self.clients[owner]


_manager = SportarrClientManager()


def refresh_sportarr_clients():
    from app.get_args import args
    if not args.no_signalr:
        _manager.refresh(database)


def stop_sportarr_clients():
    _manager.stop()
