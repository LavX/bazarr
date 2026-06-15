# coding=utf-8

import logging

from app.config import settings, get_ssl_verify
from sonarr.http_session import sonarr_session
from sonarr.info import sonarr_headers, url_api_sonarr


def notify_sonarr(sonarr_series_id, arr_client=None):
    try:
        data = {
            'name': 'RescanSeries',
            'seriesId': int(sonarr_series_id)
        }
        if arr_client is not None:
            arr_client.post("/api/v3/command", json=data)
        else:
            sonarr_session().post(f"{url_api_sonarr()}command", json=data, timeout=int(settings.sonarr.http_timeout),
                                  verify=get_ssl_verify('sonarr'), headers=sonarr_headers(settings.sonarr.apikey))
    except Exception:
        logging.exception('BAZARR cannot notify Sonarr')
