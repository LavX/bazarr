# coding=utf-8
"""The one object the shared pipeline is given for a source item."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select, text

from . import repository
from .db import source_transaction


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
    group_id: Optional[int] = None
    created_at: Optional[datetime] = None
    source_created_at: Optional[datetime] = None
    source_name: Optional[str] = None

    def same_item(self, current):
        """SQLite may reuse the integer id of a deleted item."""
        current_id = current.item_id if isinstance(current, SourceItemContext) else current.id
        return self.item_id == current_id and self.created_at == current.created_at

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


def source_item_title(source: SourceItemContext) -> str:
    """Describe a source file with its show or league when available."""
    parts = [source.group_title] if source.group_title else []
    if source.kind == 'episode' and source.season is not None and source.episode is not None:
        parts.append(f'S{source.season:02d}E{source.episode:02d}')
    if source.title:
        parts.append(source.title)
    return ' - '.join(parts)


def resolve_item(item_id, target=None) -> SourceItemContext:
    """Build the context from the item's row and its group, or raise ``SourceItemGone``."""
    item = repository.get_item(item_id, target)
    if item is None:
        raise SourceItemGone(item_id)
    return _context_for_item(item, target)


def _context_for_item(item, target):
    group = repository.get_group(item.group_id, target) if item.group_id is not None else None
    library = repository.get_library(item.library_id, target)
    source = repository.get_source(item.source_id, target)
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
        series_identity_source=group.identity_source if is_episode and group is not None else 'filename',
        group_id=item.group_id, created_at=item.created_at,
        source_created_at=source.created_at if source is not None else None,
        source_name=source.name if source is not None else None)



def resolve_same_item(source, target=None):
    """Resolve a captured item only while its row, owner and path still match."""
    try:
        current = resolve_item(source.item_id, target)
    except SourceItemGone:
        return None
    return _same_context(source, current)


def _same_context(source, current):
    if not source.same_item(current) or (source.source_id, source.library_id, source.path, source.kind) != (
            current.source_id, current.library_id, current.path, current.kind):
        return None
    return current


@contextmanager
def source_item_publication(source, *, shared=False):
    """Keep the captured item stable through an atomic subtitle publication."""
    with source_transaction() as connection:
        postgres = connection.dialect.name == 'postgresql'
        shared_postgres = shared and postgres
        item = repository.get_item(source.item_id, connection, for_update=True, nowait=shared_postgres)
        if item is not None and shared_postgres:
            # The row lock alone cannot prevent a source, Arr or Sportarr
            # writer from admitting another owner in the shared folder.
            # Take the item lock first and leave any busy writer free to finish.
            connection.execute(text('LOCK TABLE table_source_items, table_episodes, table_movies, '
                                    'table_sports_events, arr_instances IN SHARE MODE NOWAIT'))
        if item is not None and postgres:
            # Keep the enabled owner and library stable through the final eligibility check and file writes.
            for table, row_id in ((repository.Sources, item.source_id), (repository.Libraries, item.library_id)):
                connection.execute(select(table.c.id).where(table.c.id == row_id).with_for_update(read=True, nowait=True))
        current = _same_context(source, _context_for_item(item, connection)) if item is not None else None
        yield current, connection
