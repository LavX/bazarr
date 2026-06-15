# coding=utf-8

import logging

from app.config import settings, get_ssl_verify
from radarr.http_session import radarr_session
from radarr.info import radarr_headers, url_api_radarr


def notify_radarr(radarr_id, arr_client=None):
    try:
        data = {
            'name': 'RescanMovie',
            'movieId': int(radarr_id)
        }
        if arr_client is not None:
            arr_client.post("/api/v3/command", json=data)
        else:
            radarr_session().post(f"{url_api_radarr()}command", json=data, timeout=int(settings.radarr.http_timeout),
                                  verify=get_ssl_verify('radarr'), headers=radarr_headers(settings.radarr.apikey))
    except Exception:
        logging.exception('BAZARR cannot notify Radarr')
