# coding=utf-8

import logging

from app.config import settings, get_ssl_verify
from sonarr.http_session import sonarr_session
from sonarr.info import sonarr_headers, url_api_sonarr


def notify_sonarr(sonarr_series_id):
    try:
        url = f"{url_api_sonarr()}command"
        data = {
            'name': 'RescanSeries',
            'seriesId': int(sonarr_series_id)
        }
        sonarr_session().post(url, json=data, timeout=int(settings.sonarr.http_timeout), verify=get_ssl_verify('sonarr'),
                              headers=sonarr_headers(settings.sonarr.apikey))
    except Exception:
        logging.exception('BAZARR cannot notify Sonarr')
