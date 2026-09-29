# coding=utf-8
"""Resolve the indexed subtitle a user asked to delete or blacklist.

The episode and movie endpoints take a subtitle path from the caller. That path
only selects an entry of the owning media row's own subtitle index; the file
that is removed is always that entry's path mapped through the owner, so a
request cannot name another item's subtitle, or any other file the process can
write, and have it deleted.
"""
import ast
import os
from dataclasses import dataclass
from typing import Callable

from app.database import TableEpisodes, TableMovies, database, select
from arr_instances.resolution import scoped
from utilities.path_mappings import path_mappings


class SubtitleDeletionError(ValueError):
    """A refused deletion, carrying the HTTP status the endpoint answers with."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OwnedSubtitle:
    row: object
    # The indexed path as stored, which is what delete_subtitles expects.
    stored_path: str
    # The owner's mapped video and subtitle paths on this machine.
    media_path: str
    local_path: str
    # Asks the same question again; delete_subtitles calls it under the
    # subtitle write locks, immediately before the unlink.
    revalidate: Callable


_MEDIA = {
    'series': ('Episode', TableEpisodes, TableEpisodes.sonarrEpisodeId),
    'movie': ('Movie', TableMovies, TableMovies.radarrId),
}


# Also the answer when History offers Exclude on a download that was deleted
# or upgraded since, so it says why rather than only refusing.
_NOT_CURRENT = "Subtitle is not one of this {}'s current subtitles"


def _normalized(path):
    return os.path.normcase(os.path.normpath(path))


def _index_entries(subtitles):
    try:
        entries = ast.literal_eval(subtitles or '[]')
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry[1] for entry in entries
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and isinstance(entry[1], str) and entry[1]]


def resolve_subtitle_for_deletion(media_type, media_id, subtitle_path, arr_instance_id=None, *, session=None):
    """Find the indexed subtitle ``subtitle_path`` names on this media item.

    Raises SubtitleDeletionError with 404 when the item does not exist, 409 when
    no owner was given and the upstream id exists in more than one instance,
    and 403 when the path is not one of the item's indexed subtitles. The path
    may be given as stored, as mapped through the owning instance, or as mapped
    through the global table the media pages display paths with.
    """
    session = database if session is None else session
    label, table, upstream_id = _MEDIA[media_type]
    columns = [table.id, table.path, table.arr_instance_id, table.subtitles]
    if media_type == 'series':
        columns.append(table.sonarrSeriesId)
    rows = session.execute(scoped(select(*columns).where(upstream_id == media_id),
                                  table.arr_instance_id, arr_instance_id).limit(2)).all()
    if not rows:
        raise SubtitleDeletionError(f'{label} not found', 404)
    if len(rows) > 1:
        raise SubtitleDeletionError(f'This {label.lower()} exists in more than one instance; '
                                    'specify arr_instance_id', 409)
    row = rows[0]
    if not isinstance(subtitle_path, str) or not subtitle_path:
        raise SubtitleDeletionError(_NOT_CURRENT.format(label.lower()), 403)

    owner = row.arr_instance_id
    shown = path_mappings.path_replace_movie if media_type == 'movie' else path_mappings.path_replace
    wanted = _normalized(subtitle_path)
    for stored_path in _index_entries(row.subtitles):
        local_path = path_mappings.path_replace_instance(stored_path, owner, media_type)
        # The removed file is the mapped entry, so it has to be a real location
        # on this machine and not something resolved against the working
        # directory.
        if not local_path or not os.path.isabs(local_path):
            continue
        if wanted not in {_normalized(stored_path), _normalized(local_path), _normalized(shown(stored_path))}:
            continue
        media_path = path_mappings.path_replace_instance(row.path, owner, media_type)
        pinned = (row.id, row.path, owner, stored_path, local_path, media_path)

        def revalidate():
            try:
                current = resolve_subtitle_for_deletion(media_type, media_id, stored_path, owner,
                                                        session=session)
            except SubtitleDeletionError:
                current = None
            if current is None or (current.row.id, current.row.path, current.row.arr_instance_id,
                                   current.stored_path, current.local_path, current.media_path) != pinned:
                raise SubtitleDeletionError('Subtitle ownership changed before deletion', 409)

        return OwnedSubtitle(row, stored_path, media_path, local_path, revalidate)
    raise SubtitleDeletionError(_NOT_CURRENT.format(label.lower()), 403)
