# coding=utf-8

import requests
import logging

from app.config import settings, get_ssl_verify
from radarr.http_session import radarr_session
from radarr.info import radarr_headers, url_api_radarr


def browse_radarr_filesystem(path='#'):
    if path == '#':
        path = ''

    url_radarr_api_filesystem = (f"{url_api_radarr()}filesystem?path={path}&allowFoldersWithoutTrailingSlashes=true&"
                                 f"includeFiles=false")
    try:
        r = radarr_session().get(url_radarr_api_filesystem, timeout=int(settings.radarr.http_timeout), verify=get_ssl_verify('radarr'),
                                 headers=radarr_headers(settings.radarr.apikey))
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
