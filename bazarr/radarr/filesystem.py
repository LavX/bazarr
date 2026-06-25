# coding=utf-8

import requests
import logging

from app.config import settings, get_ssl_verify
from radarr.http_session import radarr_session
from radarr.info import radarr_headers, url_api_radarr


def browse_radarr_filesystem(path='#', arr_client=None):
    if path == '#':
        path = ''

    suffix = f"filesystem?path={path}&allowFoldersWithoutTrailingSlashes=true&includeFiles=false"
    try:
        if arr_client is not None:
            r = arr_client.get(f"/api/v3/{suffix}")
        else:
            r = radarr_session().get(f"{url_api_radarr()}{suffix}", timeout=int(settings.radarr.http_timeout),
                                     verify=get_ssl_verify('radarr'), headers=radarr_headers(settings.radarr.apikey))
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get series from Radarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get series from Radarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get series from Radarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get series from Radarr.")
        return

    return r.json()
