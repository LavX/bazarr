# coding=utf-8

import requests
import logging

from app.config import settings, get_ssl_verify
from sonarr.http_session import sonarr_session
from sonarr.info import sonarr_headers, url_api_sonarr


def browse_sonarr_filesystem(path='#'):
    if path == '#':
        path = ''
    url_sonarr_api_filesystem = (f"{url_api_sonarr()}filesystem?path={path}&allowFoldersWithoutTrailingSlashes=true&"
                                 f"includeFiles=false")
    try:
        r = sonarr_session().get(url_sonarr_api_filesystem, timeout=int(settings.sonarr.http_timeout), verify=get_ssl_verify('sonarr'),
                                 headers=sonarr_headers(settings.sonarr.apikey))
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
