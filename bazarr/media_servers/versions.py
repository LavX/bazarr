# coding=utf-8
"""Media server versions for the status page, cached and refreshed off-request.

Nothing durable stores a media server version: the instance row has no column
for one, and the destination layer only learns a version while it is already
talking to the server. So the only source is a probe, and a probe on every
status render would poll every configured server every time the page is opened.

Instead the cache is filled from two sides. A connection test in the settings
UI records what it just saw, for free. Anything past its age is refreshed on a
small background pool while the request returns whatever it already holds, so
the status endpoint never waits on a media server, never blocks on a slow one,
and never fails because one is down. A server that has never answered reports
'checking' once; the page polls until it resolves.

Each entry also carries how many seconds its answer can stay as it is, so the
page knows when to come back. Zero means a probe is running right now and the
next reply may differ. Anything else is time left on a cached value that
nothing can change before it expires, which lets the page wait that long
instead of asking again into an unchanged reply.
"""

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# How long a recorded version stays authoritative. A media server version
# changes at most a few times a year, so this is deliberately far longer than
# the 60 seconds Sonarr and Radarr use: it is a label, not a health signal.
CACHE_SECONDS = 600

# A failure is retried far sooner than a success is re-read. A server that was
# down when the page was first opened should recover on the page, not ten
# minutes later, and a failed probe costs one bounded request.
FAILURE_SECONDS = 60

# Bounded on purpose. Plex's client carries its own generous timeout, so an
# unreachable Plex can hold a worker for a while. It holds a worker, not the
# request, and the pool refuses to grow past this.
_MAX_WORKERS = 4

_lock = threading.Lock()
# key -> (state, version, recorded monotonic time)
_recorded = {}
_inflight = set()
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix='media-server-version')


def _key(instance_id, revision):
    # The revision is part of the key, so editing a destination's URL or
    # credentials retires what the previous one reported instead of showing
    # the old server's version against the new one.
    return f'{instance_id}:{revision}'


def record(instance_id, revision, result):
    """Remember what a probe just learned about one destination."""
    if not isinstance(result, dict):
        return
    connected = result.get('success') is True
    version = result.get('version') if connected else ''
    if not isinstance(version, str):
        version = ''
    with _lock:
        _recorded[_key(instance_id, revision)] = (
            'connected' if connected else 'unreachable', version, time.monotonic())


def reset():
    """Drop everything recorded. For tests and for a settings reload."""
    with _lock:
        _recorded.clear()
        _inflight.clear()


def _probe(snapshot):
    from .service import _probe as probe_kind
    try:
        return probe_kind(snapshot.kind, snapshot.url, snapshot.apikey, bool(snapshot.verify_ssl), False)
    except Exception:
        logging.debug('BAZARR cannot get the %s version for destination %s', snapshot.kind, snapshot.id)
        return {'success': False}


def _run(snapshot, key):
    try:
        record(snapshot.id, snapshot.revision, _probe(snapshot))
    finally:
        with _lock:
            _inflight.discard(key)


def _schedule(snapshot, key):
    with _lock:
        if key in _inflight:
            return
        _inflight.add(key)
    try:
        _executor.submit(_run, snapshot, key)
    except RuntimeError:
        # The interpreter is shutting down. Nothing to report, nothing to fix.
        with _lock:
            _inflight.discard(key)


def statuses(session, settings, *, refresh=True):
    """One entry per destination that is actually in use, never a blank row.

    Disabled destinations, destinations whose kind is switched off and
    destinations that cannot be connected to at all are left out entirely: a
    row claiming a version for something Bazarr is not talking to reads as
    broken rather than absent.
    """
    from .repository import MediaServerInstanceRepository

    entries = []
    live = set()
    for snapshot in MediaServerInstanceRepository(session).snapshots(settings):
        key = _key(snapshot.id, snapshot.revision)
        live.add(key)
        if not snapshot.enabled or snapshot.configuration_error or not snapshot.apikey:
            continue
        with _lock:
            cached = _recorded.get(key)
        ttl = CACHE_SECONDS if cached and cached[0] == 'connected' else FAILURE_SECONDS
        if refresh and (cached is None or time.monotonic() - cached[2] >= ttl):
            _schedule(snapshot, key)
        state, version = (cached[0], cached[1]) if cached else ('checking', '')
        with _lock:
            probing = key in _inflight
        # Zero while a probe is in flight, because the state above is the old
        # answer and the new one lands in seconds. Otherwise the time left on
        # this entry: until it expires, every request returns exactly what
        # this one did, so there is nothing for the page to come back for.
        refresh_in = 0 if probing or cached is None else int(
            math.ceil(max(0.0, cached[2] + ttl - time.monotonic())))
        entries.append({'id': snapshot.id, 'kind': snapshot.kind, 'name': snapshot.name,
                        'state': state, 'version': version, 'refresh_in': refresh_in})
    with _lock:
        for stale in [key for key in _recorded if key not in live and key not in _inflight]:
            del _recorded[stale]
    return entries
