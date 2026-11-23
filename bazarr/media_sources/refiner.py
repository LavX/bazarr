# coding=utf-8
"""Fill a provider video from a source item's context instead of from the arr tables.

The registered database, ffprobe and arr-history refiners look the file up in the arr tables by path, so for an
item an arr also holds they would read the arr row. The source arm of ``get_video`` skips them and applies this.
"""
import os

from subliminal import Episode, Movie

from subtitles.refiners.ffprobe import refine_from_metadata

from . import repository
from .analysis import cached_metadata, local_stat, streams
from .context import SourceItemContext


def refine_from_source(path, video, source: SourceItemContext):
    """Apply confirmed source identities and fill gaps with the scanner's parse."""
    authoritative = source.identity_source in ('user', 'media_server', 'tmdb')
    if isinstance(video, Episode):
        if source.group_title:
            video.series = source.group_title
        # An unresolved or multi-episode parse has no single episode number.
        video.season = source.season
        video.episode = source.episode
        video.title = source.title
        if authoritative and source.year:
            video.year = source.year
        elif source.series_year and (source.series_identity_source != 'filename' or not video.year):
            video.year = source.series_year
        elif not video.year:
            video.year = source.year
        if source.tvdb_id or authoritative:
            video.tvdb_id = source.tvdb_id
        if source.imdb_id or authoritative:
            video.imdb_id = source.imdb_id
        if source.tmdb_id or authoritative:
            video.tmdb_id = int(source.tmdb_id) if source.tmdb_id else None
        if source.series_tvdb_id:
            video.series_tvdb_id = int(source.series_tvdb_id)
        if source.series_imdb_id:
            video.series_imdb_id = source.series_imdb_id
        if source.series_tmdb_id:
            video.series_tmdb_id = int(source.series_tmdb_id)
    elif isinstance(video, Movie):
        if source.kind == 'sports_event':
            # What get_sports_video does: the event's own title and year, never a movie id.
            video.title = source.title
            video.year = source.year
            video.imdb_id = None
            video.tmdb_id = None
            try:
                video.size = os.path.getsize(path)
            except OSError:
                pass
        else:
            video.title = source.title
            if source.year and (authoritative or not video.year):
                video.year = source.year
            if source.imdb_id or authoritative:
                video.imdb_id = source.imdb_id
            if source.tmdb_id or authoritative:
                video.tmdb_id = int(source.tmdb_id) if source.tmdb_id else None
    stat = local_stat(path)
    if stat is not None:
        item = repository.get_item(source.item_id)
        if item is not None and item.path == path:
            data = cached_metadata(item, stat)
            refine_from_metadata(video, data)
            duration = streams(data).get('duration')
            if duration is not None:
                video.duration = duration.total_seconds() if hasattr(duration, 'total_seconds') else duration
    return video
