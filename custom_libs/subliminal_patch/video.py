# coding=utf-8

from __future__ import absolute_import
import os

from subliminal.video import Video as Video_


class Video(Video_):
    is_special = False
    fps = None
    plexapi_metadata = None
    hints = None
    season_fully_aired = None
    audio_languages = None
    external_subtitle_languages = None
    info_url = None
    absolute_episode = None

    def __init__(
        self,
        name,
        source=None,
        release_group=None,
        resolution=None,
        streaming_service=None,
        video_codec=None,
        audio_codec=None,
        frame_rate=None,
        duration=None,
        imdb_id=None,
        tmdb_id=None,
        hashes=None,
        size=None,
        subtitles=None,
        subtitle_languages=None,
        audio_languages=None,
        title=None,
        year=None,
        country=None,
        use_ctime=True,
        edition=None,
        other=None,
        info_url=None,
        series_anidb_id=None,
        series_anidb_episode_id=None,
        series_anidb_season_episode_offset=None,
        anilist_id=None,
        absolute_episode=None,
        **kwargs
    ):
        super(Video, self).__init__(
            name,
            source=source,
            release_group=release_group,
            resolution=resolution,
            streaming_service=streaming_service,
            video_codec=video_codec,
            audio_codec=audio_codec,
            frame_rate=frame_rate,
            duration=duration,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            hashes=hashes,
            size=size,
            subtitles=subtitles,
            title=title,
            year=year,
            country=country,
            use_ctime=use_ctime,
        )
        self.original_name = os.path.basename(name)
        self.plexapi_metadata = {}
        self.hints = {}
        self._subtitle_languages = set(subtitle_languages or [])
        self.audio_languages = audio_languages or set()
        self.external_subtitle_languages = set()
        self.streaming_service = streaming_service
        self.edition = edition
        self.original_path = name
        self.other = other
        self.info_url = info_url
        self.absolute_episode = absolute_episode
        self.series_anidb_series_id = series_anidb_id
        self.series_anidb_id = series_anidb_id
        self.series_anidb_episode_id = series_anidb_episode_id
        self.series_anidb_season_episode_offset = series_anidb_season_episode_offset
        self.anilist_id = anilist_id

    @property
    def subtitle_languages(self):
        self._subtitle_languages.update(subtitle.language for subtitle in self.subtitles)
        return self._subtitle_languages

    @subtitle_languages.setter
    def subtitle_languages(self, languages):
        self._subtitle_languages = set(languages or [])
