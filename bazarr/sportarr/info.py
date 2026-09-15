# coding=utf-8

import datetime
import logging

from dogpile.cache import make_region

from app.config import settings
from app.database import database
from arr_instances.repository import ArrInstanceRepository


region = make_region().configure('dogpile.cache.memory')


class GetSportarrInfo:
    @staticmethod
    def version():
        """Sportarr version for System, Status.

        Unlike Sonarr and Radarr this cannot read a scalar host/apikey: sports
        was born multi-instance and deliberately keeps its connection details
        only in arr_instances, so the version comes from the DEFAULT instance's
        client. Cached for 60 seconds like the other two, so opening the status
        page does not poll Sportarr on every render.
        """
        cached = region.get(
            "sportarr_version",
            expiration_time=datetime.timedelta(seconds=60).total_seconds(),
        )
        if cached and cached != 'unknown':
            region.set("sportarr_version", cached)
            return cached

        sportarr_version = ''
        if settings.general.use_sportarr:
            sportarr_version = 'unknown'
            try:
                from arr_instances.resolution import client_for_instance

                default = ArrInstanceRepository(database).get_default('sportarr')
                if default is not None:
                    client = client_for_instance(database, default.id, enabled_only=False)
                    if client is not None:
                        result = client.test_connection()
                        if result.get("ok") and result.get("version"):
                            sportarr_version = result["version"]
            except Exception:
                logging.debug('BAZARR cannot get Sportarr version')
        logging.debug('BAZARR got this Sportarr version from its API: %s', sportarr_version)
        region.set("sportarr_version", sportarr_version)
        return sportarr_version


get_sportarr_info = GetSportarrInfo()
