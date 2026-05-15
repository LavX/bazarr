# coding=utf-8
from __future__ import absolute_import
import base64
import codecs
import logging
import os
import subliminal
import zlib
from subliminal import __short_version__
from subliminal.refiners.omdb import OMDBClient, refine as refine_orig, Episode, Movie
from subliminal_patch.http import TimeoutSession

logger = logging.getLogger(__name__)


def _resolve_omdb_apikey():
    """Resolve the OMDB API key, in priority order:

      1. Dynaconf settings.omdb.apikey - the user-facing settings input.
      2. OMDB_API_KEY env var - for operators who prefer config via env.
      3. U1pfT01EQl9LRVk env var - upstream's obfuscated envelope
         (base16 -> zlib -> rot13 -> base64 -> split 'x'), kept for
         compatibility with anyone who baked one in. Upstream decodes
         this with Python-2-only `.decode('base64')`, which crashes on
         Python 3; we reimplement the same shape in Python 3 terms.

    Returns the api key string, or None if nothing is configured /
    the envelope is malformed.
    """
    try:
        from app.config import settings
        apikey = (settings.omdb.apikey or "").strip()
        if apikey:
            return apikey
    except Exception:
        pass
    plain = os.environ.get("OMDB_API_KEY")
    if plain:
        return plain.strip()
    envelope = os.environ.get("U1pfT01EQl9LRVk")
    if not envelope:
        return None
    try:
        decompressed = zlib.decompress(base64.b16decode(envelope))
        rot13ed = codecs.decode(decompressed.decode("utf-8"), "rot_13")
        decoded = base64.b64decode(rot13ed).decode("utf-8")
        return decoded.split("x")[0]
    except Exception as e:
        logger.debug("OMDB envelope decode failed: %s", e)
        return None


class SZOMDBClient(OMDBClient):
    def __init__(self, version=1, session=None, headers=None, timeout=10):
        if not session:
            session = TimeoutSession(timeout=timeout)
        super(SZOMDBClient, self).__init__(version=version, session=session, headers=headers, timeout=timeout)

    def get_params(self, params):
        apikey = _resolve_omdb_apikey()
        if not apikey:
            raise RuntimeError(
                "OMDB refiner unavailable: set OMDB_API_KEY or U1pfT01EQl9LRVk"
            )
        self.session.params['apikey'] = apikey
        return dict(self.session.params, **params)

    def get(self, id=None, title=None, type=None, year=None, plot='short', tomatoes=False):
        # build the params
        params = {}
        if id:
            params['i'] = id
        if title:
            params['t'] = title
        if not params:
            raise ValueError('At least id or title is required')
        params['type'] = type
        params['y'] = year
        params['plot'] = plot
        params['tomatoes'] = tomatoes

        # perform the request
        r = self.session.get(self.base_url, params=self.get_params(params))
        r.raise_for_status()

        # get the response as json
        j = r.json()

        # check response status
        if j['Response'] == 'False':
            return None

        return j

    def search(self, title, type=None, year=None, page=1, is_movie=None):
        if type is None and is_movie is not None:
            type = "movie" if is_movie else "series"

        # build the params
        params = {'s': title, 'type': type, 'y': year, 'page': page}

        # perform the request
        r = self.session.get(self.base_url, params=self.get_params(params))
        r.raise_for_status()

        # get the response as json
        j = r.json()

        # check response status
        if j['Response'] == 'False':
            return None

        return j


def refine(video, **kwargs):
    refine_orig(video, **kwargs)
    if isinstance(video, Episode) and video.series_imdb_id:
        video.series_imdb_id = video.series_imdb_id.strip()
    elif isinstance(video, Movie) and video.imdb_id:
        video.imdb_id = video.imdb_id.strip()


omdb_client = SZOMDBClient(headers={'User-Agent': 'Subliminal/%s' % __short_version__})
subliminal.refiners.omdb.omdb_client = omdb_client
