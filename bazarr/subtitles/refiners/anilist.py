# coding=utf-8
# fmt: off

import logging
import time  # noqa: F401
import requests
from collections import namedtuple  # noqa: F401
from datetime import timedelta

from app.config import settings
from subliminal import Episode, region, __short_version__

logger = logging.getLogger(__name__)
refined_providers = {'jimaku'}


INDEXED_ID_TAGS = ("anidb_id", "imdb_id")


def build_series_index(anime_list):
    """{tag: {str(id): anilist_id}} for the tags lookups actually use.

    First entry wins, matching the old code, which scanned in order and took
    the first match. Entries carrying no AniList id are not indexed: they could
    never have produced an answer.
    """
    index = {tag: {} for tag in INDEXED_ID_TAGS}
    for entry in anime_list:
        anilist_id = entry.get("anilist_id")
        if not anilist_id:
            continue
        for tag in INDEXED_ID_TAGS:
            value = entry.get(tag)
            if value is not None:
                index[tag].setdefault(str(value), anilist_id)
    return index


class AniListClient(object):    
    def __init__(self, session=None, timeout=10):
        self.session = session or requests.Session()
        self.session.timeout = timeout
        self.session.headers['Content-Type'] = 'application/json'
        self.session.headers['User-Agent'] = 'Subliminal/%s' % __short_version__
    
    def fetch_series_mappings(self):
        r = self.session.get(
            'https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json'
        )

        r.raise_for_status()
        return r.json()

    @region.cache_on_arguments(expiration_time=timedelta(days=1).total_seconds())
    def get_series_index(self):
        """The two id maps, which is all any lookup here consults.

        The whole anime list used to be what was cached. The region is file
        backed, so it unpickled roughly 43,000 dicts on every call, scanned them
        linearly, took one match and discarded the rest: about 30 MB of
        short-lived objects per refined search. The index is a fraction of that
        and turns the scan into a hash lookup.
        """
        return build_series_index(self.fetch_series_mappings())

    def get_series_id(self, candidate_id_name, candidate_id_value):
        tag_map = {
            "series_anidb_id": "anidb_id",
            "imdb_id": "imdb_id"
        }
        mapped_tag = tag_map.get(candidate_id_name, candidate_id_name)

        anilist_id = self.get_series_index().get(mapped_tag, {}).get(str(candidate_id_value))
        if anilist_id is None:
            logger.debug(f"Could not find corresponding AniList ID with '{mapped_tag}': {candidate_id_value}")  # noqa: G004
            return None

        logger.debug(f"Based on '{mapped_tag}': '{candidate_id_value}', anime-list matched AniList ID {anilist_id}")  # noqa: G004
        return anilist_id


def refine_from_anilist(path, video):
    # Safety checks
    if isinstance(video, Episode):
        if not video.series_anidb_id:
            return

    if refined_providers.intersection(settings.general.enabled_providers) and video.anilist_id is None:
        refine_anilist_ids(video)


def refine_anilist_ids(video):
    anilist_client = AniListClient()
    
    if isinstance(video, Episode):
        candidate_id_name = "series_anidb_id"
    else:
        candidate_id_name = "imdb_id"
        
    candidate_id_value = getattr(video, candidate_id_name, None)
    if not candidate_id_value:
        logger.error(f"Found no value for property {candidate_id_name} of video.")  # noqa: G004
        return video
    
    anilist_id = anilist_client.get_series_id(candidate_id_name, candidate_id_value)
    if not anilist_id:
        return video

    video.anilist_id = anilist_id
