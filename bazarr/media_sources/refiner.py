# coding=utf-8
"""Fill a provider video from a source item's context instead of from the arr tables.

The registered database, ffprobe and arr-history refiners look the file up in the arr tables by path, so for an
item an arr also holds they would read the arr row. The source arm of ``get_video`` skips them and applies this.
"""
import os

from subliminal import Episode, Movie

from .context import SourceItemContext


def refine_from_source(path, video, source: SourceItemContext):
    if isinstance(video, Episode):
        if source.group_title:
            video.series = source.group_title
        if source.season is not None:
            video.season = int(source.season)
        if source.episode is not None:
            video.episode = int(source.episode)
        video.title = source.title
        # Only refine year as a fallback.
        if not video.year and source.year:
            video.year = source.year
        if source.series_tvdb_id:
            video.series_tvdb_id = int(source.series_tvdb_id)
        if source.series_imdb_id and not video.series_imdb_id:
            video.series_imdb_id = source.series_imdb_id
    elif isinstance(video, Movie):
        if source.kind == 'sports_event':
            # What get_sports_video does: the event's own title and year, never a movie id.
            video.title = source.title
            if source.year:
                video.year = source.year
            video.imdb_id = None
            try:
                video.size = os.path.getsize(path)
            except OSError:
                pass
        else:
            video.title = source.title
            if not video.year and source.year:
                video.year = source.year
            if source.imdb_id and not video.imdb_id:
                video.imdb_id = source.imdb_id
            if source.tmdb_id and not video.tmdb_id:
                video.tmdb_id = source.tmdb_id
    return video
