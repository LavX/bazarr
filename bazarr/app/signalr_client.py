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
from app.database import TableShows, TableMovies, database, select
from app.jobs_queue import jobs_queue  # noqa: F401

from .config import settings
from .scheduler import scheduler
from .get_args import args  # noqa: F401

patch_signalrcore_stop()

sonarr_queue = deque()
radarr_queue = deque()

last_series_event_data = None
last_episode_event_data = None
last_movie_event_data = None

SIGNALR_ACTIVE_STATES = {0, 1, 2}


def _signalr_transport_state_value(connection):
    transport = getattr(connection, "transport", None)
    if transport is None:
        return None

    state = getattr(transport, "state", None)
    return getattr(state, "value", state)


def _signalr_connection_active(connection):
    return _signalr_transport_state_value(connection) in SIGNALR_ACTIVE_STATES


class SonarrSignalrClient:
    def __init__(self):
        super(SonarrSignalrClient, self).__init__()
        self.apikey_sonarr = None
        self.connection = None
        self.connected = False

    def start(self):
        if not get_sonarr_info.supports_signalr_core():
            logging.warning(
                'BAZARR requires Sonarr v4 or newer for the SignalR feed. '
                'Current Sonarr version is %s, Sonarr live updates are disabled.',
                get_sonarr_info.version(),
            )
            self.connected = False
            event_stream(type='badges')
            return

        self.configure()
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
            scheduler.execute_job_now(taskid="update_series")

    def on_reconnect_handler(self):
        self.connected = False
        event_stream(type='badges')
        logging.error('BAZARR SignalR client for Sonarr connection as been lost. Trying to reconnect...')

    def configure(self):
        self.apikey_sonarr = settings.sonarr.apikey
        self.connection = build_signalr_connection(
            f"{url_sonarr()}/signalr/messages?access_token={self.apikey_sonarr}",
            HEADERS,
        )
        self.connection.on_open(self.on_connect_handler)
        self.connection.on_reconnect(self.on_reconnect_handler)
        self.connection.on_close(lambda: logging.debug('BAZARR SignalR client for Sonarr is disconnected.'))
        self.connection.on_error(self.exception_handler)
        self.connection.on("receiveMessage", feed_queue)


class RadarrSignalrClient:
    def __init__(self):
        super(RadarrSignalrClient, self).__init__()
        self.apikey_radarr = None
        self.connection = None
        self.connected = False

    def start(self):
        self.configure()
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
            scheduler.execute_job_now(taskid="update_movies")

    def on_reconnect_handler(self):
        self.connected = False
        event_stream(type='badges')
        logging.error('BAZARR SignalR client for Radarr connection as been lost. Trying to reconnect...')

    def configure(self):
        self.apikey_radarr = settings.radarr.apikey
        self.connection = build_signalr_connection(
            f"{url_radarr()}/signalr/messages?access_token={self.apikey_radarr}",
            HEADERS,
        )
        self.connection.on_open(self.on_connect_handler)
        self.connection.on_reconnect(self.on_reconnect_handler)
        self.connection.on_close(lambda: logging.debug('BAZARR SignalR client for Radarr is disconnected.'))
        self.connection.on_error(self.exception_handler)
        self.connection.on("receiveMessage", feed_queue)


def dispatcher(data):
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
                        select(TableShows.title, TableShows.year)
                        .where(TableShows.sonarrSeriesId == data['body']['resource']['seriesId'])) \
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
                        select(TableMovies.title, TableMovies.year)
                        .where(TableMovies.radarrId == media_id)) \
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
                sync_episodes(series_id=media_id, defer_search=settings.sonarr.defer_search_signalr, is_signalr=True)
            else:
                update_one_series(series_id=media_id, action=action, is_signalr=True)
        elif topic == 'episode':
            logging.debug(f'Event received from Sonarr for episode: {series_title} ({series_year}) - '  # noqa: G004
                          f'S{season_number:0>2}E{episode_number:0>2} - {episode_title}')
            sync_one_episode(episode_id=media_id, defer_search=settings.sonarr.defer_search_signalr, is_signalr=True)
        elif topic == 'movie':
            logging.debug(f'Event received from Radarr for movie: {movie_title} ({movie_year})')  # noqa: G004
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


def feed_queue(data):
    # some sonarr version sends events as a list of a single dict, we make it a dict
    if isinstance(data, list) and len(data):
        data = data[0]

    if isinstance(data, dict) and 'name' in data and data['name'] in ['series', 'episode', 'movie']:
        # filter out some keys to reduce the size of the event data dictionary and prevent similar events from being
        # added to the queue
        data = filter_nested_dict(data)

        # check if event is duplicate from the previous one
        if data['name'] == 'series':
            global last_series_event_data
            if data == last_series_event_data:
                return
            else:
                last_series_event_data = data
        elif data['name'] == 'episode':
            global last_episode_event_data
            if data == last_episode_event_data:
                return
            else:
                last_episode_event_data = data
        elif data['name'] == 'movie':
            global last_movie_event_data
            if data == last_movie_event_data:
                return
            else:
                last_movie_event_data = data

        # if data is a dict and contain an event for series, episode or movie, we add it to the event queue
        if isinstance(data, dict) and 'name' in data:
            if data['name'] in ['series', 'episode']:
                sonarr_queue.append(data)
            elif data['name'] == 'movie':
                radarr_queue.append(data)


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

# instantiate SignalR clients
sonarr_signalr_client = SonarrSignalrClient()
radarr_signalr_client = RadarrSignalrClient()
