# coding=utf-8

import requests
import logging

from app.config import settings, get_ssl_verify
from radarr.http_session import radarr_session
from radarr.info import get_radarr_info, radarr_headers, url_api_radarr


def _api_get(suffix, v3_path, apikey_radarr, arr_client):
    """GET a Radarr API path. With no client, the legacy call
    (url_api_radarr() + suffix, version-aware base, via the shared session);
    with an ArrClient, route through it pinned to /api/v3. Identical for the
    default instance.
    """
    if arr_client is not None:
        return arr_client.get(v3_path)
    return radarr_session().get(
        f"{url_api_radarr()}{suffix}",
        timeout=int(settings.radarr.http_timeout),
        verify=get_ssl_verify('radarr'),
        headers=radarr_headers(apikey_radarr),
    )


def get_profile_list(arr_client=None):
    apikey_radarr = settings.radarr.apikey
    profiles_list = []

    suffix = f"{'quality' if url_api_radarr().endswith('v3/') else ''}profile"
    try:
        profiles_json = _api_get(suffix, "/api/v3/qualityprofile", apikey_radarr, arr_client)
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get profiles from Radarr. Connection Error.")
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get profiles from Radarr. Timeout Error.")
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get profiles from Radarr.")
    else:
        # Parsing data returned from radarr
        if get_radarr_info.is_legacy():
            for profile in profiles_json.json():
                if 'language' in profile:
                    profiles_list.append([profile['id'], profile['language'].capitalize()])  # noqa: PERF401
        else:
            for profile in profiles_json.json():
                if 'language' in profile and 'name' in profile['language']:
                    profiles_list.append([profile['id'], profile['language']['name'].capitalize()])  # noqa: PERF401

    return profiles_list


def get_tags(arr_client=None):
    apikey_radarr = settings.radarr.apikey
    tagsDict = []

    try:
        tagsDict = _api_get("tag", "/api/v3/tag", apikey_radarr, arr_client)
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get tags from Radarr. Connection Error.")
        return []
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get tags from Radarr. Timeout Error.")
        return []
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get tags from Radarr.")
        return []
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Exception while trying to get tags from Radarr.")
        return []
    else:
        try:
            return tagsDict.json()
        except Exception:
            return []


def get_movies_from_radarr_api(apikey_radarr, radarr_id=None, arr_client=None):
    suffix = f'movie{f"/{radarr_id}" if radarr_id else ""}'

    try:
        r = _api_get(suffix, f"/api/v3/{suffix}", apikey_radarr, arr_client)
        if r.status_code == 404:
            return
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get movies from Radarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get movies from Radarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get movies from Radarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get movies from Radarr.")
        return
    except Exception as e:
        logging.exception(f"Exception raised while getting movies from Radarr API: {e}")  # noqa: G004
        return
    else:
        if r.status_code == 200:
            return r.json()
        else:
            return


def get_history_from_radarr_api(apikey_radarr, movie_id, arr_client=None):
    suffix = f"history?eventType=1&movieIds={movie_id}"

    try:
        r = _api_get(suffix, f"/api/v3/{suffix}", apikey_radarr, arr_client)
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get history from Radarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get history from Radarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get history from Radarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get history from Radarr.")
        return
    except Exception as e:
        logging.exception(f"Exception raised while getting history from Radarr API: {e}")  # noqa: G004
        return
    else:
        if r.status_code == 200:
            return r.json()
        else:
            return
