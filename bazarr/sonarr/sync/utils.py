# coding=utf-8

import requests
import logging

from app.config import settings, get_ssl_verify
from sonarr.http_session import sonarr_session
from sonarr.info import sonarr_headers, url_api_sonarr


def _api_get(suffix, v3_path, apikey_sonarr, arr_client):
    """GET a Sonarr API path. With no client, the exact legacy call
    (url_api_sonarr() + suffix via the shared session); with an ArrClient, route
    through it. Identical URL/headers/timeout/verify for the default instance.
    """
    if arr_client is not None:
        return arr_client.get(v3_path)
    return sonarr_session().get(
        f"{url_api_sonarr()}{suffix}",
        timeout=int(settings.sonarr.http_timeout),
        verify=get_ssl_verify('sonarr'),
        headers=sonarr_headers(apikey_sonarr),
    )


def get_profile_list():
    return []


def get_tags(arr_client=None):
    apikey_sonarr = settings.sonarr.apikey
    tagsDict = []

    try:
        tagsDict = _api_get("tag", "/api/v3/tag", apikey_sonarr, arr_client)
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get tags from Sonarr. Connection Error.")
        return []
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get tags from Sonarr. Timeout Error.")
        return []
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get tags from Sonarr.")
        return []
    else:
        return tagsDict.json()


def get_series_from_sonarr_api(apikey_sonarr, sonarr_series_id=None, arr_client=None):
    suffix = f"series/{sonarr_series_id if sonarr_series_id else ''}"
    try:
        r = _api_get(suffix, f"/api/v3/{suffix}", apikey_sonarr, arr_client)
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code:
            raise requests.exceptions.HTTPError
        logging.exception("BAZARR Error trying to get series from Sonarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get series from Sonarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get series from Sonarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get series from Sonarr.")
        return
    except Exception as e:
        logging.exception(f"Exception raised while getting series from Sonarr API: {e}")  # noqa: G004
        return
    else:
        if r.status_code == 200:
            result = r.json()
            if isinstance(result, dict):
                return [result]
            else:
                return r.json()
        else:
            return


def get_episodes_from_sonarr_api(apikey_sonarr, series_id=None, episode_id=None, arr_client=None):
    if series_id:
        suffix = f"episode?seriesId={series_id}&includeEpisodeFile=true"
    elif episode_id:
        suffix = f"episode/{episode_id}"
    else:
        return

    try:
        r = _api_get(suffix, f"/api/v3/{suffix}", apikey_sonarr, arr_client)
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get episodes from Sonarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get episodes from Sonarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get episodes from Sonarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get episodes from Sonarr.")
        return
    except Exception as e:
        logging.exception(f"Exception raised while getting episodes from Sonarr API: {e}")  # noqa: G004
        return
    else:
        if r.status_code == 200:
            return r.json()
        else:
            return


def get_episodesFiles_from_sonarr_api(apikey_sonarr, series_id=None, episode_file_id=None, arr_client=None):
    if series_id:
        suffix = f"episodeFile?seriesId={series_id}"
    elif episode_file_id:
        suffix = f"episodeFile/{episode_file_id}"
    else:
        return

    try:
        r = _api_get(suffix, f"/api/v3/{suffix}", apikey_sonarr, arr_client)
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get episodeFiles from Sonarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get episodeFiles from Sonarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get episodeFiles from Sonarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get episodeFiles from Sonarr.")
        return
    except Exception as e:
        logging.exception(f"Exception raised while getting episodes from Sonarr API: {e}")  # noqa: G004
        return
    else:
        if r.status_code == 200:
            return r.json()
        else:
            return


def get_history_from_sonarr_api(apikey_sonarr, episode_id, arr_client=None):
    suffix = f"history?eventType=1&episodeId={episode_id}"

    try:
        r = _api_get(suffix, f"/api/v3/{suffix}", apikey_sonarr, arr_client)
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get history from Sonarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get history from Sonarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get history from Sonarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get history from Sonarr.")
        return
    except Exception as e:
        logging.exception(f"Exception raised while getting history from Sonarr API: {e}")  # noqa: G004
        return
    else:
        if r.status_code == 200:
            return r.json()
        else:
            return
