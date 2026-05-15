# coding=utf-8

from __future__ import absolute_import
import hashlib
import subliminal

if not hasattr(subliminal.exceptions, 'Unauthorized'):
    class Unauthorized(subliminal.exceptions.AuthenticationError):
        pass

    subliminal.exceptions.Unauthorized = Unauthorized

if not hasattr(subliminal.subtitle, 'sanitize'):
    subliminal.subtitle.sanitize = subliminal.utils.sanitize

if not hasattr(subliminal.cache, 'sha1_key_mangler'):
    def sha1_key_mangler(key):
        if isinstance(key, str):
            key = key.encode('utf-8')
        return hashlib.sha1(key).hexdigest()

    subliminal.cache.sha1_key_mangler = sha1_key_mangler

if '.strm' not in subliminal.video.VIDEO_EXTENSIONS:
    subliminal.video.VIDEO_EXTENSIONS = subliminal.video.VIDEO_EXTENSIONS + ('.strm',)

# patch subliminal's subtitle and provider base
from .subtitle import Subtitle, guess_matches
subliminal.subtitle.Subtitle = Subtitle
subliminal.subtitle.guess_matches = guess_matches
from .providers import Provider as Provider  # noqa: E402

from .core import scan_video, search_external_subtitles, list_all_subtitles, save_subtitles, refine, \
    download_best_subtitles  # noqa: E402
from .score import compute_score as compute_score  # noqa: E402
from .video import Video  # noqa: E402
from . import extensions as extensions  # noqa: E402
from . import http as http  # noqa: E402
from . import core as core  # noqa: E402

# patch subliminal's core functions
subliminal.scan_video = subliminal.core.scan_video = scan_video
subliminal.core.search_external_subtitles = search_external_subtitles
subliminal.save_subtitles = subliminal.core.save_subtitles = save_subtitles
subliminal.refine = subliminal.core.refine = refine
subliminal.download_best_subtitles = subliminal.core.download_best_subtitles = download_best_subtitles
subliminal.video.Video = subliminal.Video = Video
subliminal.video.Episode.__bases__ = (Video,)
subliminal.video.Movie.__bases__ = (Video,)

if not getattr(subliminal.video.Episode, '_bazarr_episode_init_patched', False):
    _episode_init = subliminal.video.Episode.__init__

    def episode_init(self, name, series, season, episodes=None, *args, episode=None, **kwargs):
        if episodes is None and episode is not None:
            episodes = episode
        _episode_init(self, name, series, season, episodes, *args, **kwargs)

    def get_episode(self):
        return min(self.episodes) if self.episodes else None

    def set_episode(self, value):
        self.episodes = [value] if value is not None else []

    subliminal.video.Episode.__init__ = episode_init
    subliminal.video.Episode.episode = property(get_episode, set_episode)
    subliminal.video.Episode._bazarr_episode_init_patched = True

# add our own list_all_subtitles
subliminal.list_all_subtitles = subliminal.core.list_all_subtitles = list_all_subtitles
