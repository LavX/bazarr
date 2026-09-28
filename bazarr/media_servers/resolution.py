# coding=utf-8
"""One ordered ladder for turning a publication into a server item.

Jellyfin's inline refresh climbs identifiers first, falls back to a title and
year, and finally refreshes the whole library. Native destinations climb the
same ladder, with one rung added: the exact file path, which Jellyfin never had
and which is the only evidence that proves a single file. Every destination
publishes the rungs it can actually climb, so a capability that a server does
not have is absent rather than faked.

The added rung also moves the weakest one. Jellyfin had nothing better than a
title, so it asked for one early; here the exact path sits above it, because
binding the wrong item is worse than resolving nothing at all: the walk stops
at the first rung that answers and reports the publication as requested.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass

PROVIDER_ID = 'provider_id'
TITLE_YEAR = 'title_year'
PATH = 'path'
LIBRARY = 'library'

# A provider id first, because it names the item on its own and that is what
# survives a wrong path mapping. The exact path next, because it is the only
# proof of one file. A title and a year after it, because two metadata fields
# are a guess and a guess must not pre-empt proof. The library last, because it
# is the broadest thing we can ask a server to do.
CHAIN = (PROVIDER_ID, PATH, TITLE_YEAR, LIBRARY)


@dataclass(frozen=True)
class MediaMetadata:
    """What Bazarr already recorded about the published video."""

    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: int | None = None
    title: str | None = None
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    def provider_ids(self, media_type):
        """Provider pairs in Jellyfin's precedence, as 'prov'/'id' tuples.

        An episode carries no identifier of its own: the ids Bazarr stores
        belong to the series, so these locate the series and the season and
        episode numbers pick the file underneath it.
        """
        pairs = [('imdb', self.imdb_id),
                 ('tmdb', self.tmdb_id) if media_type == 'movie' else ('tvdb', self.tvdb_id)]
        return [(name, str(value)) for name, value in pairs if value not in (None, '')]

    def locatable(self, media_type):
        """Whether an identifier or title match can reach the published file.

        A series is the item its identifiers name, so it needs no numbers. The
        refresh path never asks about one: a publication is always a file.
        """
        if media_type in ('movie', 'series'):
            return True
        return self.season is not None and self.episode is not None


def walk(steps):
    """The first rung that resolves the item, or None when none of them does.

    ``steps`` are ``(name, call, expected)`` in ladder order, and a rung
    misses by returning None. Anything a rung raises stops the walk, so a
    destination's strict acceptance rules keep refusing rather than being
    laundered into a broader refresh of something else.

    Ordering is all this decides. Which refusal counts as a miss, and what to
    call a ladder that reached the end, belong to the adapter boundary that
    knows those servers, so nothing here raises.
    """
    for name, call, expected in steps:
        result = call()
        if result is not None:
            return name, result, expected
    return None


@contextmanager
def _reader():
    """A read session owned by this call, never left on the worker thread.

    The scoped session is keyed by thread and nothing tears it down for a
    refresh worker, so a refresh takes its own session and closes it instead
    of leaving a connection behind for every worker that ever ran.
    """
    from app.database import session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _year(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def media_metadata(media_type, video_path, arr_instance_id):
    """Read the identifiers Bazarr already stores for this published video.

    Resolved here rather than threaded through every publication callback:
    each of those callers wrote these rows in the first place, and the ids the
    subtitles API hands Jellyfin come from exactly this query. ``video_path``
    is Bazarr-visible, so it is mapped back to the path the arr recorded.

    Returns None whenever the answer would be a guess: no row, a row that no
    longer joins to its show, more than one owner at that path, or any failure
    at all reading the database. The caller then climbs a lower rung.
    """
    try:
        from app.database import TableEpisodes, TableMovies, TableShows, select
        from arr_instances.resolution import scoped
        from utilities.path_mappings import path_mappings

        movie = media_type == 'movie'
        stored = path_mappings.path_replace_reverse_instance(
            video_path, arr_instance_id, 'movie' if movie else 'series')
        if movie:
            statement = scoped(
                select(TableMovies.imdbId, TableMovies.tmdbId, TableMovies.title, TableMovies.year)
                .where(TableMovies.path == stored),
                TableMovies.arr_instance_id, arr_instance_id)
        else:
            statement = scoped(
                select(TableEpisodes.season, TableEpisodes.episode, TableShows.imdbId,
                       TableShows.tvdbId, TableShows.title, TableShows.year)
                .join(TableShows).where(TableEpisodes.path == stored),
                TableEpisodes.arr_instance_id, arr_instance_id)
        with _reader() as session:
            rows = session.execute(statement.limit(2)).all()
    except Exception:
        logging.debug('BAZARR could not read media identifiers for a native refresh', exc_info=True)
        return None
    if len(rows) != 1:
        return None
    row = rows[0]
    if movie:
        return MediaMetadata(imdb_id=row.imdbId, tmdb_id=row.tmdbId,
                             title=row.title, year=_year(row.year))
    return MediaMetadata(imdb_id=row.imdbId, tvdb_id=row.tvdbId, title=row.title,
                         year=_year(row.year), season=row.season, episode=row.episode)


@dataclass(frozen=True)
class LibraryRow:
    """One library row, as a media server would be asked about it."""

    metadata: MediaMetadata
    # Bazarr-visible, the same space a publication's video_path is in, so the
    # destination path mappings apply to it unchanged. None when the stored
    # path could not be mapped, which only rules out the path-keyed kinds.
    video_path: str | None
    arr_instance_id: int | None


# Which arr path mapping a row's stored path goes through.
_PATH_TYPES = {'movie': 'movie', 'episode': 'series', 'series': 'series', 'sports': 'sports'}


def _visible_path(path, arr_instance_id, media_type):
    if not path:
        return None
    try:
        from utilities.path_mappings import path_mappings
        return path_mappings.path_replace_instance(path, arr_instance_id, _PATH_TYPES[media_type])
    except Exception:
        # A sports owner that is gone or disabled raises rather than degrading.
        logging.debug('BAZARR could not map a library row path for a media server link', exc_info=True)
        return None


def media_metadata_for(media_type, local_id):
    """What Bazarr stores about one library row, read by its own primary key.

    The link path starts from a row, not from a published file, so nothing is
    turned back into a path and reverse-mapped: the row is read by id and its
    stored path goes forward through the owning arr instance's mapping.
    Returns a LibraryRow, or None for an unknown type, a row that does not
    exist, or any failure reading the database.
    """
    if media_type not in _PATH_TYPES or type(local_id) is not int or local_id <= 0:
        return None
    try:
        from app.database import TableEpisodes, TableMovies, TableShows, TableSportsEvents, select
        with _reader() as session:
            if media_type == 'movie':
                row = session.execute(
                    select(TableMovies.imdbId, TableMovies.tmdbId, TableMovies.title, TableMovies.year,
                           TableMovies.path, TableMovies.arr_instance_id)
                    .where(TableMovies.id == local_id)).first()
                metadata = row and MediaMetadata(imdb_id=row.imdbId, tmdb_id=row.tmdbId,
                                                 title=row.title, year=_year(row.year))
            elif media_type == 'episode':
                row = session.execute(
                    select(TableEpisodes.season, TableEpisodes.episode, TableEpisodes.path,
                           TableEpisodes.arr_instance_id, TableShows.imdbId, TableShows.tvdbId,
                           TableShows.title, TableShows.year)
                    .join(TableShows, TableShows.id == TableEpisodes.series_id)
                    .where(TableEpisodes.id == local_id)).first()
                metadata = row and MediaMetadata(imdb_id=row.imdbId, tvdb_id=row.tvdbId, title=row.title,
                                                 year=_year(row.year), season=row.season,
                                                 episode=row.episode)
            elif media_type == 'series':
                row = session.execute(
                    select(TableShows.imdbId, TableShows.tvdbId, TableShows.title, TableShows.year,
                           TableShows.path, TableShows.arr_instance_id)
                    .where(TableShows.id == local_id)).first()
                metadata = row and MediaMetadata(imdb_id=row.imdbId, tvdb_id=row.tvdbId,
                                                 title=row.title, year=_year(row.year))
            else:
                # A sports event has no provider identity, only its own title
                # and the numbers Sportarr gave it.
                row = session.execute(
                    select(TableSportsEvents.title, TableSportsEvents.season, TableSportsEvents.episode,
                           TableSportsEvents.path, TableSportsEvents.arr_instance_id)
                    .where(TableSportsEvents.id == local_id)).first()
                metadata = row and MediaMetadata(title=row.title, season=row.season, episode=row.episode)
    except Exception:
        logging.debug('BAZARR could not read a library row for a media server link', exc_info=True)
        return None
    if row is None:
        return None
    return LibraryRow(metadata, _visible_path(row.path, row.arr_instance_id, media_type), row.arr_instance_id)
