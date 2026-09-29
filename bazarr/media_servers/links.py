# coding=utf-8
"""Where a library row opens on each media server destination, read only.

A link needs an item id and an address a browser can reach. Every destination
answers the first in a few reads, so nothing is stored: each answer is cached
briefly and asked again after that. Which destinations a row could be on comes
from the saved destinations alone, with no network, so a menu can draw its rows
at once. Each row then resolves on its own request, on that destination's own
workers, so a slow server delays only its own rows.
"""

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from threading import Event, Lock
from urllib.parse import quote

from dogpile.cache import make_region

from utilities.locked_lru import LockedLRU
from .http import MediaServerError
from .instances import LIBRARY_KEYS, VALID_KINDS
from .paths import map_media_path
from .resolution import media_metadata_for

logger = logging.getLogger(__name__)

KIND_NAMES = {'emby': 'Emby', 'jellyfin': 'Jellyfin', 'plex': 'Plex', 'silo': 'Silo'}
MEDIA_TYPES = ('movie', 'series', 'episode', 'sports')
CACHE_SECONDS = 900
# Per request to the server, where the client lets it be set.
HTTP_TIMEOUT = (3, 4)
# The longest one lookup runs, from when it starts, whatever its client does.
# Plex may ask its agent online, and the pooled Plex client cannot be given a
# shorter timeout.
BUDGETS = {'emby': 8, 'jellyfin': 8, 'silo': 8, 'plex': 15}
# Whether a kind can pin a sports recording to one item of its own. A
# recording has no identifiers, so it is found by its file: Emby by the mapped
# path, Jellyfin by the file name. Silo's native API has no path lookup, and a
# recording it never matched carries a hashed id nothing here can derive.
# Plex files a show's date-named recordings of one day as one episode (four
# NFL games of one Sunday are one item, "Episode 09-20"), so no file name
# picks out an item of its own there. A kind that cannot pin a recording is
# not offered for sports at all, rather than drawn as a row that can only
# ever answer "Not found".
PINS_RECORDINGS = {'emby': True, 'jellyfin': True, 'plex': False, 'silo': False}
# Lookups in flight per destination. A lookup past its budget keeps its worker
# until the server lets go, so every destination has workers of its own: a
# server that hangs then holds only its own rows, never another server's.
SLOTS = 2

links_region = make_region(key_mangler=lambda key: key).configure(
    'dogpile.cache.memory', arguments={'cache_dict': LockedLRU(maxsize=2048)},
    expiration_time=CACHE_SECONDS)


class _Lane:
    """One destination's workers, and how many are still held past their budget."""

    def __init__(self, revision):
        self.revision = revision
        self.pool = ThreadPoolExecutor(max_workers=SLOTS, thread_name_prefix='media-server-link')
        self.lock = Lock()
        self.overdue = 0

    def settle(self, _future):
        with self.lock:
            self.overdue -= 1


_lanes = {}
_lanes_lock = Lock()


def _lane(snapshot):
    """The destination's lane. A saved edit starts a fresh one.

    A lookup still hanging on the old address says nothing about the new one.
    The old lane is dropped rather than shut down: its workers finish what
    they hold and exit once nothing refers to it.
    """
    with _lanes_lock:
        lane = _lanes.get(snapshot.id)
        if lane is None or lane.revision != snapshot.revision:
            lane = _lanes[snapshot.id] = _Lane(snapshot.revision)
        return lane


@dataclass(frozen=True)
class Target:
    snapshot: object
    display_name: str
    # The file on the server, for the kinds that map paths; None otherwise.
    mapped_path: str | None


def destinations(native):
    """Every destination a link can open, in a stable order, with the name it shows.

    That is the kind when there is one destination of it and the destination's
    own name when there are more, so two Emby servers never read the same.
    """
    usable = [snapshot for snapshot in native.list()
              if snapshot.enabled and not snapshot.configuration_error]
    counts = Counter(snapshot.kind for snapshot in usable)
    usable.sort(key=lambda snapshot: (VALID_KINDS.index(snapshot.kind), snapshot.name.lower(), snapshot.id))
    return [(snapshot, snapshot.name if counts[snapshot.kind] > 1 else KIND_NAMES[snapshot.kind])
            for snapshot in usable]


def candidates(row, media_type, native):
    """The destinations that can hold this row, decided without the network.

    Jellyfin and Plex are asked only in the libraries chosen on the instance,
    the scoping the refresh uses. Emby and Silo hold a file only where a path
    mapping covers it; a row no mapping covers is not on that server at all,
    which is different from a server that was asked and did not find it.
    A recording is offered only on the kinds that pin one by its file, see
    ``PINS_RECORDINGS``, so one whose path could not be mapped is on none.
    """
    if media_type == 'sports' and row.video_path is None:
        return []
    targets = []
    for snapshot, display_name in destinations(native):
        if media_type == 'sports' and not PINS_RECORDINGS[snapshot.kind]:
            continue
        mapped = None
        if snapshot.kind in LIBRARY_KEYS:
            if not snapshot.libraries(media_type):
                continue
        else:
            if row.video_path is None:
                continue
            try:
                mapped = map_media_path(row.video_path, snapshot.mappings(),
                                        require_library=snapshot.kind == 'silo')['path']
            except MediaServerError:
                continue
        targets.append(Target(snapshot, display_name, mapped))
    return targets


def _shown(snapshot, display_name):
    return {'instance_id': snapshot.id, 'kind': snapshot.kind, 'display_name': display_name}


def destination_rows(native=None):
    return [_shown(snapshot, display_name) for snapshot, display_name in destinations(native or _native())]


def candidate_rows(media_type, local_id, native=None):
    """The rows a menu draws for one library row, or None when there is no such row."""
    row = media_metadata_for(media_type, local_id)
    if row is None:
        return None
    return [_shown(target.snapshot, target.display_name)
            for target in candidates(row, media_type, native or _native())]


def resolve(instance_id, media_type, local_id, native=None):
    """One destination's answer for one row, or None when there is no such row.

    ``resolved`` carries the URL. ``not_found`` is a server that answered and
    does not hold the item, or a destination that cannot hold the row at all.
    ``unreachable`` is a server that did not answer in time or refused, and is
    the one answer never cached, so a server that comes back is asked again.
    """
    row = media_metadata_for(media_type, local_id)
    if row is None:
        return None
    target = next((target for target in candidates(row, media_type, native or _native())
                   if target.snapshot.id == instance_id), None)
    if target is None:
        return {'instance_id': instance_id, 'state': 'not_found', 'url': None}
    # The file the answer depends on. Jellyfin maps no path, but it finds a
    # recording by its file name, so a replaced recording is asked again.
    path = target.mapped_path or (row.video_path if media_type == 'sports' else None)
    key = f'{instance_id}:{target.snapshot.revision}:{media_type}:{local_id}:{path}'
    return links_region.get_or_create(key, lambda: _answer(target, media_type, row),
                                      should_cache_fn=lambda answer: answer['state'] != 'unreachable')


def _native():
    from .dispatcher import get_native_configuration
    return get_native_configuration()


def _answer(target, media_type, row):
    snapshot = target.snapshot
    answer = {'instance_id': snapshot.id, 'state': 'unreachable', 'url': None}
    budget = BUDGETS[snapshot.kind]
    lane = _lane(snapshot)
    with lane.lock:
        if lane.overdue >= SLOTS:
            # Every worker this destination has is still waiting on a server
            # that already missed its budget. The row would only queue behind
            # them, so it is answered now, and asked again the next time.
            logger.debug('BAZARR media server link lookup skipped: a %s destination is not answering',
                         snapshot.kind)
            return answer
    started = Event()

    def run():
        started.set()
        return _lookup(target, media_type, row)

    future = lane.pool.submit(run)
    # The budget is the lookup's own, counted from when it starts. Waiting for
    # a worker, behind this destination's other rows only, is bounded by one
    # budget as well, after which a lookup that never started is withdrawn.
    if not started.wait(budget) and future.cancel():
        logger.debug('BAZARR media server link lookup never started on a busy %s destination', snapshot.kind)
        return answer
    try:
        url = future.result(timeout=budget)
    except FutureTimeout:
        with lane.lock:
            lane.overdue += 1
        # Counted until the server lets go of the worker.
        future.add_done_callback(lane.settle)
        logger.debug('BAZARR media server link lookup timed out on a %s destination', snapshot.kind)
        return answer
    except Exception as error:
        logger.debug('BAZARR media server link lookup failed on a %s destination: %s',
                     snapshot.kind, getattr(error, 'code', None) or type(error).__name__)
        return answer
    if url is None:
        answer['state'] = 'not_found'
    else:
        answer.update(state='resolved', url=url)
    return answer


def _browser_base(snapshot):
    return (snapshot.external_url or snapshot.url).rstrip('/')


def emby_url(base, item_id, server_id):
    return f"{base}/web/index.html#!/item?id={quote(item_id, safe='')}&serverId={quote(server_id, safe='')}"


def jellyfin_url(base, item_id):
    # No serverId: the details route falls back to the server it is served
    # from, and a wrong id is worse than none.
    return f"{base}/web/#/details?id={quote(item_id, safe='')}"


def silo_url(base, content_id):
    return f"{base}/item/{quote(content_id, safe='')}"


def plex_web_base(snapshot):
    """Local Plex Web when a browser URL is saved, otherwise the hosted app."""
    return f'{snapshot.external_url.rstrip("/")}/web/index.html' if snapshot.external_url else None


def _emby_server_id(snapshot, client):
    return links_region.get_or_create(f'emby-server-id:{snapshot.id}:{snapshot.revision}', client.server_id)


def _lookup(target, media_type, row):
    """The URL on this destination, or None when it does not hold the item."""
    snapshot = target.snapshot
    if snapshot.kind == 'emby':
        from emby.client import EmbyClient
        with EmbyClient(snapshot.url, snapshot.apikey, snapshot.verify_ssl) as client:
            client.http.timeout = HTTP_TIMEOUT
            item_id = client.locate(media_type, row.metadata, target.mapped_path)
            if item_id is None:
                return None
            return emby_url(_browser_base(snapshot), item_id, _emby_server_id(snapshot, client))
    if snapshot.kind == 'jellyfin':
        from jellyfin.refresh import JellyfinRefreshClient
        with JellyfinRefreshClient(snapshot) as client:
            client.client.timeout = HTTP_TIMEOUT
            item_id = client.locate(media_type, row.metadata, row.video_path)
            return None if item_id is None else jellyfin_url(_browser_base(snapshot), item_id)
    if snapshot.kind == 'plex':
        from plex.refresh import PlexRefreshClient
        with PlexRefreshClient(snapshot) as client:
            item = client.locate(media_type, row.metadata)
            return None if item is None else item.getWebURL(base=plex_web_base(snapshot))
    from silo.client import SiloClient
    with SiloClient(snapshot.url, snapshot.apikey, snapshot.verify_ssl) as client:
        client.http.timeout = HTTP_TIMEOUT
        content_id = client.locate(media_type, row.metadata)
        return None if content_id is None else silo_url(_browser_base(snapshot), content_id)
