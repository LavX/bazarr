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
    """{tag: {str(id): anilist_id or None}} for the tags lookups actually use.

    First entry wins, which is what the old linear scan did when it took
    ``obj[0]``. That includes an entry carrying no AniList id: it still claims
    the key, with a value of None, because "the list has this id but no AniList
    id for it" is a different answer from "the list does not have this id", and
    the old code distinguished them. Dropping such entries would silently let a
    later duplicate answer in their place.

    Non-mapping elements are skipped rather than raising. The value is built
    inside the cached call, so one malformed element upstream would otherwise
    mean nothing is ever cached and the 3 MB list is refetched on every search.
    """
    index = {tag: {} for tag in INDEXED_ID_TAGS}
    for entry in anime_list:
        if not isinstance(entry, dict):
            continue
        anilist_id = entry.get("anilist_id") or None
        for tag in INDEXED_ID_TAGS:
            value = entry.get(tag)
            if value is not None:
                index[tag].setdefault(str(value), anilist_id)
    return index


class AniListClient(object):    
    def __init__(self, session=None, timeout=10):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.session.headers['Content-Type'] = 'application/json'
        self.session.headers['User-Agent'] = 'Subliminal/%s' % __short_version__
    
    def fetch_series_mappings(self):
        # Passed per request: requests ignores an attribute set on the Session,
        # so this call had no timeout at all and could hang a search
        # indefinitely behind the region's per-key mutex.
        r = self.session.get(
            'https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json',
            timeout=self.timeout,
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

        mapping = self.get_series_index().get(mapped_tag, {})
        key = str(candidate_id_value)
        if key not in mapping:
            logger.debug(f"Could not find corresponding AniList ID with '{mapped_tag}': {candidate_id_value}")  # noqa: G004
            return None

        anilist_id = mapping[key]
        if not anilist_id:
            # Matched an entry that simply has no AniList id. Kept distinct from
            # "no match" because it is the case worth telling someone about.
            logger.error("This entry does not have an AniList ID")
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
