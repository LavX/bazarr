# coding=utf-8

import logging
import time
import threading

from requests.exceptions import ConnectionError
from app.signalrcore_compat import build_signalr_connection, patch_signalrcore_stop
from collections import deque
from time import sleep

from constants import HEADERS
from app.event_handler import event_stream
from sonarr.sync.episodes import sync_episodes, sync_one_episode
from sonarr.sync.series import update_series, update_one_series  # noqa: F401
from radarr.sync.movies import update_movies, update_one_movie  # noqa: F401
from sonarr.info import get_sonarr_info, url_sonarr
from radarr.info import url_radarr
from sonarr.sync.episodes import sync_one_episode_for_instance
from sonarr.sync.series import update_one_series_for_instance
from radarr.sync.movies import update_one_movie_for_instance
from arr_instances.repository import ArrInstanceRepository
from arr_instances import resolution
from arr_instances.resolution import client_for_instance
from apscheduler.jobstores.base import JobLookupError
from app.database import TableShows, TableMovies, database, select
from app.jobs_queue import jobs_queue  # noqa: F401

from .config import settings
from .scheduler import scheduler
from .get_args import args  # noqa: F401

patch_signalrcore_stop()

sonarr_queue = deque()
radarr_queue = deque()

# Per-instance dedup caches keyed by arr_instance_id (None == the legacy
# single-instance / scalar path). The same event from two instances is no longer
# collapsed into one; identical repeats from one instance still are. (#156)
last_series_event_data = {}
last_episode_event_data = {}
last_movie_event_data = {}


def _enabled_instances(kind):
    """Enabled instances of a kind, or [] if the registry can't be read yet."""
    try:
        return ArrInstanceRepository(database).list(kind, enabled_only=True)
    except Exception:
        return []

SIGNALR_ACTIVE_STATES = {0, 1, 2}
UNKNOWN_SONARR_VERSION_VALUES = {"", "unknown", None}


def _signalr_transport_state_value(connection):
    transport = getattr(connection, "transport", None)
    if transport is None:
        return None

    state = getattr(transport, "state", None)
    return getattr(state, "value", state)


def _signalr_connection_active(connection):
    return _signalr_transport_state_value(connection) in SIGNALR_ACTIVE_STATES


def _sonarr_signalr_core_support_state():
    version = get_sonarr_info.version()
    if version in UNKNOWN_SONARR_VERSION_VALUES:
        return None, version
    return get_sonarr_info.supports_signalr_core(), version


def _version_supports_signalr_core(version):
    """True/False if ``version`` is a Sonarr v4+ string, None if unparseable.

    Mirrors GetSonarrInfo.semver()/supports_signalr_core(): the major component
    must be >= 4. Sonarr nightly/develop builds report e.g. "4.0.9.2421-develop"
    so we read the leading digits of each of the first three dotted segments.
    """
    if not isinstance(version, str) or version in UNKNOWN_SONARR_VERSION_VALUES:
        return None
    split_version = version.split('.')
    if len(split_version) < 3 or not all(split_version[i].isdigit() for i in range(3)):
        return None
    return int(split_version[0]) >= 4


def _instance_sonarr_signalr_core_support_state(arr_instance_id):
    """Per-instance counterpart to ``_sonarr_signalr_core_support_state`` (#156).

    Probes THIS instance's ``/api/v3/system/status`` rather than the scalar
    default ``get_sonarr_info``, so a secondary Sonarr decides whether to start
    its live feed based on its own server's version. Returns ``(supports, version)``
    where ``supports`` is None (retry) when the instance is unreachable, gone, or
    reports an unparseable version.
    """
    try:
        client = client_for_instance(database, arr_instance_id)
        if client is None:
            return None, "unknown"
        version = client.get('/api/v3/system/status').json().get('version')
    except Exception:
        logging.debug('BAZARR cannot get Sonarr version for instance %s', arr_instance_id)
        return None, "unknown"
    if not version:
        return None, "unknown"
    return _version_supports_signalr_core(version), version


class SonarrSignalrClient:
    def __init__(self, arr_instance_id=None):
        super(SonarrSignalrClient, self).__init__()
        # arr_instance_id None == the legacy scalar/default path (byte-identical:
        # scalar config, untagged events, the unsuffixed 'update_series' job).
        # When set, the client connects to that instance's server, tags its
        # events so the dispatcher scopes them, and triggers update_series_<id>.
        self.arr_instance_id = arr_instance_id
        self.apikey_sonarr = None
        self.connection = None
        self.connected = False

    def _support_state(self):
        # The scalar/default client keeps probing the shared get_sonarr_info
        # (byte-identical to the legacy path). A per-instance client probes ITS
        # own server's status so it never gates on the default Sonarr (#156).
        if self.arr_instance_id is None:
            return _sonarr_signalr_core_support_state()
        return _instance_sonarr_signalr_core_support_state(self.arr_instance_id)

    def start(self):
        supports_signalr, sonarr_version = self._support_state()
        if supports_signalr is None:
            logging.warning(
                'BAZARR cannot confirm Sonarr version yet. '
                'Retrying before starting the Sonarr SignalR feed.'
            )
        while supports_signalr is None:
            # Stop retrying if this per-instance client's instance was deleted or
            # disabled: otherwise the version probe re-runs every 5s forever for a
            # server that is gone (a transient-unreachable instance still resolves
            # a client, so it keeps retrying as before).
            if self.arr_instance_id is not None and \
                    client_for_instance(database, self.arr_instance_id) is None:
                logging.info('BAZARR Sonarr instance %s is gone or disabled; '
                             'not starting its SignalR feed.', self.arr_instance_id)
                self.connected = False
                return
            time.sleep(5)
            supports_signalr, sonarr_version = self._support_state()

        if not supports_signalr:
            logging.warning(
                'BAZARR requires Sonarr v4 or newer for the SignalR feed. '
                'Current Sonarr version is %s, Sonarr live updates are disabled.',
                sonarr_version,
            )
            self.connected = False
            event_stream(type='badges')
            return

        self.configure()
        if self.connection is None:
            # configure() bailed (instance deleted/disabled); nothing to connect.
            return
        logging.info('BAZARR trying to connect to Sonarr SignalR feed...')
        while not _signalr_connection_active(self.connection):
            try:
                started = self.connection.start()
            except ConnectionError:
                time.sleep(5)
                continue
            if not started and not _signalr_connection_active(self.connection):
                time.sleep(5)

    def stop(self):
        logging.info('BAZARR SignalR client for Sonarr is now disconnected.')
        if self.connection is None:
            return
        self.connection.stop()

    def restart(self):
        if self.connection:
            if _signalr_connection_active(self.connection):
                self.stop()
        if settings.general.use_sonarr:
            self.start()

    def exception_handler(self):
        sonarr_queue.clear()
        self.connected = False
        event_stream(type='badges')
        logging.error("BAZARR connection to Sonarr SignalR feed has failed. We'll try to reconnect.")
        self.restart()

    def on_connect_handler(self):
        self.connected = True
        event_stream(type='badges')
        logging.info('BAZARR SignalR client for Sonarr is connected and waiting for events.')
        if settings.sonarr.series_sync_on_live:
            # Match the scheduler fan-out: unsuffixed job for the single default
            # instance, per-instance job id when fanned out.
            taskid = "update_series" if self.arr_instance_id is None else f"update_series_{self.arr_instance_id}"
            try:
                scheduler.execute_job_now(taskid=taskid)
            except JobLookupError:
                # A per-instance job can be unregistered when this client connects
                # before the scheduler fan-out registered it (#156). Skip the
                # immediate sync; the scheduled job will run once registered.
                logging.warning('BAZARR SignalR connect could not trigger sync job %s yet '
                                '(not registered).', taskid)

    def on_reconnect_handler(self):
        self.connected = False
        event_stream(type='badges')
        logging.error('BAZARR SignalR client for Sonarr connection as been lost. Trying to reconnect...')

    def configure(self):
        # None -> scalar config (the default instance), byte-identical. Otherwise
        # resolve this instance's base URL + decrypted key from its saved row.
        if self.arr_instance_id is None:
            base_url = url_sonarr()
            self.apikey_sonarr = settings.sonarr.apikey
        else:
            client = client_for_instance(database, self.arr_instance_id)
            if client is None:
                # The instance was deleted/disabled between enumeration and now.
                # Bail without building a connection so the daemon thread does not
                # die on None.base_url() (#156).
                logging.warning('BAZARR Sonarr instance %s is gone; not starting its '
                                'SignalR feed.', self.arr_instance_id)
                self.connected = False
                return
            base_url = client.base_url()
            self.apikey_sonarr = client.api_key
        # Tear down any prior connection before overwriting it so a stale
        # signalrcore reconnect thread is not orphaned.
        if self.connection is not None:
            try:
                self.stop()
            except Exception:
                pass
        self.connection = build_signalr_connection(
            f"{base_url}/signalr/messages?access_token={self.apikey_sonarr}",
            HEADERS,
        )
        self.connection.on_open(self.on_connect_handler)
        self.connection.on_reconnect(self.on_reconnect_handler)
        self.connection.on_close(lambda: logging.debug('BAZARR SignalR client for Sonarr is disconnected.'))
        self.connection.on_error(self.exception_handler)
        self.connection.on("receiveMessage", lambda data: feed_queue(data, self.arr_instance_id))


class RadarrSignalrClient:
    def __init__(self, arr_instance_id=None):
        super(RadarrSignalrClient, self).__init__()
        # arr_instance_id None == the legacy scalar/default path (byte-identical).
        self.arr_instance_id = arr_instance_id
        self.apikey_radarr = None
        self.connection = None
        self.connected = False

    def start(self):
        self.configure()
        if self.connection is None:
            # configure() bailed (instance deleted/disabled); nothing to connect.
            return
        logging.info('BAZARR trying to connect to Radarr SignalR feed...')
        while not _signalr_connection_active(self.connection):
            try:
                started = self.connection.start()
            except ConnectionError:
                time.sleep(5)
                continue
            if not started and not _signalr_connection_active(self.connection):
                time.sleep(5)

    def stop(self):
        logging.info('BAZARR SignalR client for Radarr is now disconnected.')
        if self.connection is None:
            return
        self.connection.stop()

    def restart(self):
        if self.connection:
            if _signalr_connection_active(self.connection):
                self.stop()
        if settings.general.use_radarr:
            self.start()

    def exception_handler(self):
        radarr_queue.clear()
        self.connected = False
        event_stream(type='badges')
        logging.error("BAZARR connection to Radarr SignalR feed has failed. We'll try to reconnect.")
        self.restart()

    def on_connect_handler(self):
        self.connected = True
        event_stream(type='badges')
        logging.info('BAZARR SignalR client for Radarr is connected and waiting for events.')
        if settings.radarr.movies_sync_on_live:
            taskid = "update_movies" if self.arr_instance_id is None else f"update_movies_{self.arr_instance_id}"
            try:
                scheduler.execute_job_now(taskid=taskid)
            except JobLookupError:
                logging.warning('BAZARR SignalR connect could not trigger sync job %s yet '
                                '(not registered).', taskid)

    def on_reconnect_handler(self):
        self.connected = False
        event_stream(type='badges')
        logging.error('BAZARR SignalR client for Radarr connection as been lost. Trying to reconnect...')

    def configure(self):
        if self.arr_instance_id is None:
            base_url = url_radarr()
            self.apikey_radarr = settings.radarr.apikey
        else:
            client = client_for_instance(database, self.arr_instance_id)
            if client is None:
                logging.warning('BAZARR Radarr instance %s is gone; not starting its '
                                'SignalR feed.', self.arr_instance_id)
                self.connected = False
                return
            base_url = client.base_url()
            self.apikey_radarr = client.api_key
        if self.connection is not None:
            try:
                self.stop()
            except Exception:
                pass
        self.connection = build_signalr_connection(
            f"{base_url}/signalr/messages?access_token={self.apikey_radarr}",
            HEADERS,
        )
        self.connection.on_open(self.on_connect_handler)
        self.connection.on_reconnect(self.on_reconnect_handler)
        self.connection.on_close(lambda: logging.debug('BAZARR SignalR client for Radarr is disconnected.'))
        self.connection.on_error(self.exception_handler)
        self.connection.on("receiveMessage", lambda data: feed_queue(data, self.arr_instance_id))


def dispatcher(data):
    # The owning instance tagged by feed_queue (None == legacy/default, unscoped).
    arr_instance_id = data.get('_arr_instance_id') if isinstance(data, dict) else None
    try:
        series_title = series_year = episode_title = season_number = episode_number = movie_title = movie_year = None

        #
        try:
            episodesChanged = False
            topic = data['name']

            media_id = data['body']['resource']['id']
            action = data['body']['action']
            if topic == 'series':
                if 'episodesChanged' in data['body']['resource']:
                    episodesChanged = data['body']['resource']['episodesChanged']
                series_title = data['body']['resource']['title']
                series_year = data['body']['resource']['year']
            elif topic == 'episode':
                if 'series' in data['body']['resource']:
                    series_title = data['body']['resource']['series']['title']
                    series_year = data['body']['resource']['series']['year']
                else:
                    series_metadata = database.execute(
                        resolution.scoped(
                            select(TableShows.title, TableShows.year)
                            .where(TableShows.sonarrSeriesId == data['body']['resource']['seriesId']),
                            TableShows.arr_instance_id, arr_instance_id)) \
                        .first()
                    if series_metadata:
                        series_title = series_metadata.title
                        series_year = series_metadata.year
                episode_title = data['body']['resource']['title']
                season_number = data['body']['resource']['seasonNumber']
                episode_number = data['body']['resource']['episodeNumber']
            elif topic == 'movie':
                if action == 'deleted':
                    existing_movie_details = database.execute(
                        resolution.scoped(
                            select(TableMovies.title, TableMovies.year)
                            .where(TableMovies.radarrId == media_id),
                            TableMovies.arr_instance_id, arr_instance_id)) \
                        .first()
                    if existing_movie_details:
                        movie_title = existing_movie_details.title
                        movie_year = existing_movie_details.year
                    else:
                        return
                else:
                    movie_title = data['body']['resource']['title']
                    movie_year = data['body']['resource']['year']
        except KeyError:
            return

        if topic == 'series':
            logging.debug(f'Event received from Sonarr for series: {series_title} ({series_year})')  # noqa: G004
            if episodesChanged:
                # this will happen if a season's monitored status is changed.
                arr_client = client_for_instance(database, arr_instance_id) if arr_instance_id is not None else None
                sync_episodes(series_id=media_id, defer_search=settings.sonarr.defer_search_signalr, is_signalr=True,
                              arr_instance_id=arr_instance_id, arr_client=arr_client)
            elif arr_instance_id is not None:
                update_one_series_for_instance(arr_instance_id, media_id, action, is_signalr=True)
            else:
                update_one_series(series_id=media_id, action=action, is_signalr=True)
        elif topic == 'episode':
            logging.debug(f'Event received from Sonarr for episode: {series_title} ({series_year}) - '  # noqa: G004
                          f'S{season_number:0>2}E{episode_number:0>2} - {episode_title}')
            if arr_instance_id is not None:
                sync_one_episode_for_instance(arr_instance_id, media_id,
                                              defer_search=settings.sonarr.defer_search_signalr, is_signalr=True)
            else:
                sync_one_episode(episode_id=media_id, defer_search=settings.sonarr.defer_search_signalr,
                                 is_signalr=True)
        elif topic == 'movie':
            logging.debug(f'Event received from Radarr for movie: {movie_title} ({movie_year})')  # noqa: G004
            if arr_instance_id is not None:
                update_one_movie_for_instance(arr_instance_id, media_id, action,
                                              defer_search=settings.radarr.defer_search_signalr, is_signalr=True)
            else:
                update_one_movie(movie_id=media_id, action=action, defer_search=settings.radarr.defer_search_signalr,
                                 is_signalr=True)
    except Exception as e:
        logging.debug(f'BAZARR an exception occurred while parsing SignalR feed: {repr(e)}')  # noqa: G004
    finally:
        event_stream(type='badges')
        return


def filter_nested_dict(data: dict) -> dict:
    """
    Filters out specific keys from a nested dictionary structure, including any
    nested dictionaries or lists that may contain dictionaries.

    The function recursively processes the input dictionary to remove any key-value
    pairs where the key matches the specified keys to exclude. For lists, it will
    iterate through the items and apply the same filtering logic if the item is a
    dictionary.

    :param data: A dictionary that may contain nested dictionaries or lists. Values
                 that are dictionaries will be recursively filtered, and lists
                 within the dictionary will be traversed to check for and filter
                 nested dictionaries within them.
    :type data: dict
    :return: A dictionary where specified keys are removed, including from any
             nested dictionaries or dictionaries within lists.
    :rtype: dict
    """
    keys_to_remove = ['statistics']

    filtered_data = {}

    for key, value in data.items():
        if key not in keys_to_remove:
            if isinstance(value, dict):
                # Recursively filter nested dictionaries
                filtered_data[key] = filter_nested_dict(value)
            elif isinstance(value, list):
                # Handle lists that might contain dictionaries
                filtered_data[key] = [
                    filter_nested_dict(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                # Keep the value as is
                filtered_data[key] = value

    return filtered_data


def feed_queue(data, arr_instance_id=None):
    # some sonarr version sends events as a list of a single dict, we make it a dict
    if isinstance(data, list) and len(data):
        data = data[0]

    if isinstance(data, dict) and 'name' in data and data['name'] in ['series', 'episode', 'movie']:
        # filter out some keys to reduce the size of the event data dictionary and prevent similar events from being
        # added to the queue
        data = filter_nested_dict(data)
        name = data['name']

        # check if event is duplicate from the previous one FOR THIS INSTANCE
        # (#156): the same event arriving from two instances is processed once
        # per instance, while identical repeats from one instance are skipped.
        cache = {
            'series': last_series_event_data,
            'episode': last_episode_event_data,
            'movie': last_movie_event_data,
        }[name]
        if cache.get(arr_instance_id) == data:
            return
        cache[arr_instance_id] = data

        # tag the queued copy with the owning instance so the dispatcher can
        # scope it (None == legacy/default path, unscoped). The cache holds the
        # untagged event so dedup compares event content only.
        tagged = dict(data)
        tagged['_arr_instance_id'] = arr_instance_id
        if name in ['series', 'episode']:
            sonarr_queue.append(tagged)
        elif name == 'movie':
            radarr_queue.append(tagged)


def consume_queue(queue):
    # get events data from queues one at a time and dispatch it
    while True:
        try:
            data = queue.popleft()
        except IndexError:
            pass
        except (KeyboardInterrupt, SystemExit):
            break
        else:
            dispatcher(data)
        sleep(0.1)


# start both queues consuming threads
sonarr_queue_thread = threading.Thread(target=consume_queue, args=(sonarr_queue,))
sonarr_queue_thread.daemon = True
sonarr_queue_thread.start()
radarr_queue_thread = threading.Thread(target=consume_queue, args=(radarr_queue,))
radarr_queue_thread.daemon = True
radarr_queue_thread.start()

# instantiate SignalR clients. The module-level singletons remain the DEFAULT
# (scalar) clients that badges + config restart reference by name; the manager
# fans out additional per-instance clients when more than one instance exists.
sonarr_signalr_client = SonarrSignalrClient()
radarr_signalr_client = RadarrSignalrClient()

# Extra per-instance clients beyond the singleton (multi-instance mode, #156).
_sonarr_signalr_clients = []
_radarr_signalr_clients = []


def all_sonarr_signalr_connected():
    """True only when the scalar/default Sonarr client AND every per-instance
    extra client report connected. In multi-instance mode the badge must read
    LIVE only when every enabled feed is up, so a secondary instance whose feed
    is DOWN is not masked by the singleton's state (#156).
    """
    return (sonarr_signalr_client.connected
            and all(c.connected for c in _sonarr_signalr_clients))


def all_radarr_signalr_connected():
    """Radarr counterpart of :func:`all_sonarr_signalr_connected`."""
    return (radarr_signalr_client.connected
            and all(c.connected for c in _radarr_signalr_clients))


def _start_clients_for_kind(kind, singleton, extra_list, client_cls):
    """Start one SignalR client per enabled instance of a kind.

    A single enabled instance keeps the singleton on the scalar/default path
    (arr_instance_id None) -> byte-identical to the legacy behaviour. With more
    than one, the singleton handles the first instance and one extra client per
    remaining instance, each tagged so the dispatcher scopes its events and
    triggers that instance's update_*_<id> job. Like the scheduler fan-out, new
    instances are picked up on (re)start, not live.
    """
    instances = _enabled_instances(kind)
    # Stop EVERY previously-started extra before re-fanning out, regardless of
    # transport state. A client mid-reconnect is not in an active state but its
    # signalrcore auto-reconnect thread (max_attempts=None) keeps feeding
    # receiveMessage events tagged with the old arr_instance_id forever unless we
    # stop() it. stop() is None/already-stopped tolerant, so this is safe. (#156)
    for client in extra_list:
        try:
            client.stop()
        except Exception:
            pass
    extra_list.clear()

    if len(instances) > 1:
        singleton.arr_instance_id = instances[0].id
        clients = [singleton] + [client_cls(inst.id) for inst in instances[1:]]
        extra_list.extend(clients[1:])
    else:
        singleton.arr_instance_id = None
        clients = [singleton]

    for client in clients:
        thread = threading.Thread(target=client.start)
        thread.daemon = True
        thread.start()
    return clients


def start_sonarr_signalr():
    return _start_clients_for_kind('sonarr', sonarr_signalr_client, _sonarr_signalr_clients, SonarrSignalrClient)


def start_radarr_signalr():
    return _start_clients_for_kind('radarr', radarr_signalr_client, _radarr_signalr_clients, RadarrSignalrClient)


def restart_sonarr_signalr():
    """Stop every Sonarr client and re-fan-out (used on settings/instance change)."""
    if sonarr_signalr_client.connection and _signalr_connection_active(sonarr_signalr_client.connection):
        sonarr_signalr_client.stop()
    if settings.general.use_sonarr:
        start_sonarr_signalr()


def restart_radarr_signalr():
    """Stop every Radarr client and re-fan-out (used on settings/instance change)."""
    if radarr_signalr_client.connection and _signalr_connection_active(radarr_signalr_client.connection):
        radarr_signalr_client.stop()
    if settings.general.use_radarr:
        start_radarr_signalr()
