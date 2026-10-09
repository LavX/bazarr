# coding=utf-8

import logging
import re
import time
import threading

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


def _has_instances(kind):
    """Whether the kind has any instance, enabled or not. False if the
    registry can't be read yet."""
    try:
        return bool(ArrInstanceRepository(database).list(kind))
    except Exception:
        return False

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


def _stop_connection_quietly(connection):
    try:
        connection.stop()
    except Exception:
        pass


# The feed URL carries the arr API key as access_token, and an error page from
# the arr or a proxy in front of it can echo that URL back.
_ACCESS_TOKEN_RE = re.compile(r'(access_token)(?:=|%3D)[^&\s\'"]+', re.IGNORECASE)


def _start_error_summary(error):
    # One line: a refused websocket upgrade carries the raw HTTP response.
    summary = ' '.join(f'{type(error).__name__}: {error}'.split())
    return _ACCESS_TOKEN_RE.sub(r'\1=(removed)', summary)


def _start_error_key(error):
    # Tells a repeat of the last failure from a new one. Only the first line
    # counts: the raw HTTP response of a refused upgrade has a Date header that
    # changes on every attempt.
    lines = str(error).splitlines()
    return type(error), lines[0] if lines else ''


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


class _SignalrClientLifecycle:
    """Start and stop bookkeeping shared by the Sonarr and Radarr clients.

    Every start() and stop() moves the client to a new generation. A start()
    whose generation is no longer current was stopped, or superseded by a later
    start() of the same client, so its retry loops return instead of retrying
    beside the new one. The lock orders a generation change against configure(),
    so a retired start() never replaces the connection a newer one is using.
    """
    arr_name = None

    def __init__(self):
        super(_SignalrClientLifecycle, self).__init__()
        self._lifecycle_lock = threading.Lock()
        self._generation = 0
        # Left unstarted on purpose: its kind is switched on, and every one of
        # its instances is disabled. Nothing is expected to connect, so not
        # being connected is no disconnection.
        self.idle = False

    def _begin_generation(self):
        with self._lifecycle_lock:
            self._generation += 1
            return self._generation

    def _is_current(self, generation):
        return self._generation == generation

    def _instance_gone(self):
        if self.arr_instance_id is None:
            return False
        try:
            return client_for_instance(database, self.arr_instance_id) is None
        except Exception:
            # The registry could not be read; the instance may well still be
            # there, so keep retrying rather than give up on it.
            return False

    def _keep_retrying(self, generation):
        if not self._is_current(generation):
            return False
        # A per-instance client whose instance was deleted or disabled would
        # otherwise retry a server that is gone every 5s forever (a transiently
        # unreachable instance still resolves a client, so it keeps retrying).
        if self._instance_gone():
            logging.info('BAZARR %s instance %s is gone or disabled; '
                         'not starting its SignalR feed.', self.arr_name, self.arr_instance_id)
            self.connected = False
            return False
        return True

    def _current_only(self, handler):
        """Wrap a connection callback so it runs only while the start() that
        built the connection is current. configure() runs under the lifecycle
        lock, so the generation read here is that start()'s. A retired
        connection can still finish its handshake after a stop() or a newer
        start(), and must not change ``connected`` then.

        The wrapper takes no lock: signalrcore runs the callbacks on the
        connection's receive thread, and configure() joins that thread, with
        the lock held, when it closes the previous connection.
        """
        generation = self._generation

        def callback():
            if self._is_current(generation):
                handler()

        return callback

    def _configure_if_current(self, generation):
        """Build this start()'s connection, or return None when it has been
        stopped or superseded, or configure() bailed (instance deleted/disabled).
        """
        with self._lifecycle_lock:
            if not self._is_current(generation):
                return None
            self.configure()
            return self.connection

    def _connect(self, generation, connection):
        logging.info('BAZARR trying to connect to %s SignalR feed...', self.arr_name)
        last_failure = None
        while self._is_current(generation):
            try:
                started = connection.start()
            except Exception as e:
                # An arr that is down fails signalrcore's urllib negotiation with
                # URLError, a proxy answering with a page instead of JSON fails
                # it with ValueError, and a refused websocket upgrade raises
                # SocketHandshakeError. Letting any of them escape ends the feed
                # thread until the next restart.
                failure = _start_error_key(e)
                # Warn once for each new reason, so the log says why the feed
                # is down without repeating it every 5s.
                level = logging.DEBUG if failure == last_failure else logging.WARNING
                last_failure = failure
                logging.log(level, 'BAZARR cannot connect to %s SignalR feed yet: %s',
                            self.arr_name, _start_error_summary(e))
                # Once negotiation succeeds, signalrcore marks the new transport
                # connecting before it opens the socket, so a refused websocket
                # leaves a transport behind that looks active. Stop it so the
                # retry starts from a disconnected transport.
                _stop_connection_quietly(connection)
                if not self._keep_retrying(generation):
                    break
                time.sleep(5)
                continue
            if _signalr_connection_active(connection):
                break
            if not started:
                time.sleep(5)
        if not self._is_current(generation):
            # A stop() or a newer start() landed while this connection was
            # starting. Close it so no feed keeps running without an owner.
            _stop_connection_quietly(connection)

    def stop(self):
        with self._lifecycle_lock:
            self._generation += 1
            self.connected = False
            connection = self.connection
        if connection is None:
            return
        logging.info('BAZARR SignalR client for %s is now disconnected.', self.arr_name)
        connection.stop()


class SonarrSignalrClient(_SignalrClientLifecycle):
    arr_name = 'Sonarr'

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
        generation = self._begin_generation()
        supports_signalr, sonarr_version = self._support_state()
        if supports_signalr is None:
            logging.warning(
                'BAZARR cannot confirm Sonarr version yet. '
                'Retrying before starting the Sonarr SignalR feed.'
            )
        while supports_signalr is None:
            if not self._keep_retrying(generation):
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

        connection = self._configure_if_current(generation)
        if connection is None:
            return
        self._connect(generation, connection)

    def restart(self):
        if self.connection:
            if _signalr_connection_active(self.connection):
                self.stop()
        if settings.general.use_sonarr and not self.idle:
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
        # signalrcore reconnect thread is not orphaned. This closes the
        # connection directly: stop() would retire the start() calling us.
        if self.connection is not None:
            _stop_connection_quietly(self.connection)
        self.connection = build_signalr_connection(
            f"{base_url}/signalr/messages?access_token={self.apikey_sonarr}",
            HEADERS,
        )
        self.connection.on_open(self._current_only(self.on_connect_handler))
        self.connection.on_reconnect(self._current_only(self.on_reconnect_handler))
        self.connection.on_close(lambda: logging.debug('BAZARR SignalR client for Sonarr is disconnected.'))
        self.connection.on_error(self.exception_handler)
        self.connection.on("receiveMessage", lambda data: feed_queue(data, self.arr_instance_id))


class RadarrSignalrClient(_SignalrClientLifecycle):
    arr_name = 'Radarr'

    def __init__(self, arr_instance_id=None):
        super(RadarrSignalrClient, self).__init__()
        # arr_instance_id None == the legacy scalar/default path (byte-identical).
        self.arr_instance_id = arr_instance_id
        self.apikey_radarr = None
        self.connection = None
        self.connected = False

    def start(self):
        generation = self._begin_generation()
        connection = self._configure_if_current(generation)
        if connection is None:
            return
        self._connect(generation, connection)

    def restart(self):
        if self.connection:
            if _signalr_connection_active(self.connection):
                self.stop()
        if settings.general.use_radarr and not self.idle:
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
            _stop_connection_quietly(self.connection)
        self.connection = build_signalr_connection(
            f"{base_url}/signalr/messages?access_token={self.apikey_radarr}",
            HEADERS,
        )
        self.connection.on_open(self._current_only(self.on_connect_handler))
        self.connection.on_reconnect(self._current_only(self.on_reconnect_handler))
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
                # sync_episodes also serves the bulk sync, so this caller makes
                # the check update_one_series and sync_one_episode make.
                if arr_instance_id is None and resolution.skip_unscoped_sync(
                        database, 'sonarr', settings.general.use_sonarr,
                        f'the episodes of series {media_id}'):
                    return
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
        # Formatted by logging, which reports a failure to format rather than
        # raising it, so nothing can escape from this handler.
        logging.debug('BAZARR an exception occurred while parsing SignalR feed: %r', e)
    except BaseException:
        # Nothing an event raises may end the thread that consumes the feed,
        # not even an exception outside Exception.
        pass
    finally:
        event_stream(type='badges')


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
    return ((sonarr_signalr_client.connected or sonarr_signalr_client.idle)
            and all(c.connected for c in _sonarr_signalr_clients))


def all_radarr_signalr_connected():
    """Radarr counterpart of :func:`all_sonarr_signalr_connected`."""
    return ((radarr_signalr_client.connected or radarr_signalr_client.idle)
            and all(c.connected for c in _radarr_signalr_clients))


def _stop_clients_for_kind(singleton, extra_list):
    """Stop the singleton and every extra client of a kind, then forget the
    extras.

    Every client is stopped regardless of transport state. A client mid-reconnect
    is not in an active state but its signalrcore auto-reconnect thread
    (max_attempts=None) keeps feeding receiveMessage events tagged with the old
    arr_instance_id forever unless we stop() it, and a client still retrying an
    arr that is down only leaves its retry loop once stopped. stop() is
    None/already-stopped tolerant, so this is safe.
    """
    for client in [singleton, *extra_list]:
        try:
            client.stop()
        except Exception:
            pass
    extra_list.clear()


def _start_clients_for_kind(kind, singleton, extra_list, client_cls):
    """Start one SignalR client per enabled instance of a kind.

    The singleton always binds to the FIRST enabled instance's id (including
    when there is exactly one), so the live feed connects with that instance's
    saved host/key, the dispatcher routes its events through the per-instance
    path, and on-connect "sync on live" triggers that instance's
    ``update_*_<id>`` scheduler job - which is the only sync job the scheduler
    now registers (the scalar Host form was removed, so the scalar config is
    stale, #156). With more than one instance, each remaining instance gets one
    extra tagged client. Only when the kind has no instance at all does the
    singleton fall back to the scalar/default path (arr_instance_id None).
    When it has instances and every one is disabled, nothing starts: the
    scalar settings mirror the last default, which may since have been deleted,
    and the scheduler registers no sync for the kind either. The singleton is
    marked idle then, so it reads as nothing to watch rather than a feed that
    is down, and its restart() leaves it stopped.
    Like the scheduler fan-out, new instances are picked up on (re)start.
    """
    instances = _enabled_instances(kind)
    _stop_clients_for_kind(singleton, extra_list)

    singleton.idle = not instances and _has_instances(kind)
    if singleton.idle:
        return []
    if len(instances) >= 1:
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
    if settings.general.use_sonarr:
        start_sonarr_signalr()
    else:
        _stop_clients_for_kind(sonarr_signalr_client, _sonarr_signalr_clients)


def restart_radarr_signalr():
    """Stop every Radarr client and re-fan-out (used on settings/instance change)."""
    if settings.general.use_radarr:
        start_radarr_signalr()
    else:
        _stop_clients_for_kind(radarr_signalr_client, _radarr_signalr_clients)
