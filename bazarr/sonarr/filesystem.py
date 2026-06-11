# coding=utf-8

import requests
import logging

from app.config import settings, get_ssl_verify
from sonarr.http_session import sonarr_session
from sonarr.info import sonarr_headers, url_api_sonarr


def browse_sonarr_filesystem(path='#', arr_client=None):
    if path == '#':
        path = ''
    suffix = f"filesystem?path={path}&allowFoldersWithoutTrailingSlashes=true&includeFiles=false"
    try:
        if arr_client is not None:
            r = arr_client.get(f"/api/v3/{suffix}")
        else:
            r = sonarr_session().get(f"{url_api_sonarr()}{suffix}", timeout=int(settings.sonarr.http_timeout),
                                     verify=get_ssl_verify('sonarr'), headers=sonarr_headers(settings.sonarr.apikey))
        r.raise_for_status()
    except requests.exceptions.HTTPError:
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

    return r.json()
