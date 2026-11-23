# coding=utf-8
"""The one object the shared pipeline is given for a source item."""
from dataclasses import dataclass
from typing import Optional

from . import repository


class SourceItemGone(LookupError):
    """The item was deleted, or never existed. A search that outlives its item meets this at its last step."""


@dataclass(frozen=True)
class SourceItemContext:
    item_id: int
    kind: str                    # 'movie', 'episode' or 'sports_event'
    source_id: int
    library_id: int
    path: str                    # Bazarr-visible, as stored: never mapped, in either direction
    profile_id: Optional[int]
    title: str                   # for sports, the provider-facing title
    year: Optional[int]          # for sports, the date's year, else the season year
    group_title: Optional[str]   # the show or the league
    season: Optional[int]
    episode: Optional[int]       # the episode number, or the sports round
    event_date: Optional[str]
    imdb_id: Optional[str]       # the item's own: a movie's, or the episode's
    tmdb_id: Optional[str]
    tvdb_id: Optional[int]
    series_imdb_id: Optional[str]  # the show's, from the group; None for movies and sports
    series_tvdb_id: Optional[int]
    publication_type: str        # 'movie', 'episode' or 'sports'
    scene_name: Optional[str] = None
    audio_language: Optional[str] = None
    identity_source: str = 'filename'
    series_tmdb_id: Optional[str] = None
    series_year: Optional[int] = None
    series_identity_source: str = 'filename'
    pinned_file_identity: Optional[tuple[int, int]] = None  # device and inode from an explicit Discover copy

    @property
    def media_type(self):
        """What the pipeline's ``media_type`` keyword is for this item: series for an episode, movie otherwise."""
        return 'series' if self.kind == 'episode' else 'movie'


_PUBLICATION = {'movie': 'movie', 'episode': 'episode', 'sports_event': 'sports'}


def _year(value):
    try:
        return int(str(value)[:4]) if value else None
    except ValueError:
        return None


def _provider_title(item):
    """A sports event reaches providers as its title plus its part, so a qualifying and a race of one weekend
    are not the same title and year."""
    if item.kind == 'sports_event' and item.part_name:
        return f'{item.title} {item.part_name}'
    return item.title


def resolve_item(item_id, target=None) -> SourceItemContext:
    """Build the context from the item's row and its group, or raise ``SourceItemGone``."""
    item = repository.get_item(item_id, target)
    if item is None:
        raise SourceItemGone(item_id)
    group = repository.get_group(item.group_id, target) if item.group_id is not None else None
    library = repository.get_library(item.library_id, target)
    # An item's own profile wins, then its group's, then the library's. With none, it is never searched.
    profile_id = item.profile_id
    if profile_id is None and group is not None:
        profile_id = group.profile_id
    if profile_id is None and library is not None:
        profile_id = library.profile_id
    is_episode = item.kind == 'episode'
    # A sports event goes to providers with the year of its date, else its season year.
    year = (_year(item.event_date) or _year(item.year)) if item.kind == 'sports_event' else _year(item.year)
    return SourceItemContext(
        item_id=item.id, kind=item.kind, source_id=item.source_id, library_id=item.library_id, path=item.path,
        profile_id=profile_id, title=_provider_title(item),
        year=year,
        group_title=group.title if group is not None else None, season=item.season, episode=item.episode,
        event_date=item.event_date, imdb_id=item.imdb_id, tmdb_id=item.tmdb_id, tvdb_id=item.tvdb_id,
        series_imdb_id=group.imdb_id if is_episode and group is not None else None,
        series_tvdb_id=group.tvdb_id if is_episode and group is not None else None,
        publication_type=_PUBLICATION[item.kind], scene_name=item.scene_name,
        audio_language=item.audio_language, identity_source=item.identity_source,
        series_tmdb_id=group.tmdb_id if is_episode and group is not None else None,
        series_year=_year(group.year) if is_episode and group is not None else None,
        series_identity_source=group.identity_source if is_episode and group is not None else 'filename')
