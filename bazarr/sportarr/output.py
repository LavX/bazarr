"""Prepare canonical physical ownership outside short publication transactions."""

import ast
import json
import os
from bisect import bisect_left, insort
from threading import RLock
from weakref import WeakKeyDictionary

from sqlalchemy import select, text
from app.ownership_revision import ownership_revision, ownership_token, verify_ownership_protection

from app.config import settings
from app.database import (
    TableArrInstances,
    TableEpisodes,
    TableMovies,
    TableSportsEvents,
)
from utilities.helper import get_target_folder
from utilities.path_mappings import (
    _apply_mapping,
    apply_sports_mapping,
    read_sports_mappings,
    global_sports_mappings,
    path_mappings,
)


MEDIA_TABLES = (
    (TableEpisodes, "episode"),
    (TableMovies, "movie"),
    (TableSportsEvents, "sports"),
)


def output_policy(context):
    folder = get_target_folder(context.mapped_path, create=False) or os.path.dirname(
        context.mapped_path
    )
    return (
        settings.general.subfolder,
        settings.general.subfolder_custom,
        _physical(folder),
    )


def _physical(path):
    return os.path.normcase(os.path.realpath(path))


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0].lower()


def _overlaps(first, second):
    return (
        first == second
        or first.startswith(second + ".")
        or second.startswith(first + ".")
    )


def validate_output_path(context, path):
    folder = get_target_folder(context.mapped_path, create=False) or os.path.dirname(
        context.mapped_path
    )
    if _physical(os.path.dirname(path)) != _physical(folder) or not (
        _stem(path) == _stem(context.mapped_path)
        or _stem(path).startswith(_stem(context.mapped_path) + ".")
    ):
        raise ValueError("Sports subtitle destination does not belong to its event")


def lock_output_owners(session, owner):
    if session.get_bind().dialect.name == "postgresql":
        # One nonwaiting statement in fixed order. An import or competing lock
        # fails this short transaction instead of retaining a partial lock set.
        names = [
            table.__table__.name
            for table in (
                TableArrInstances,
                TableEpisodes,
                TableMovies,
                TableSportsEvents,
            )
        ]
        session.execute(
            text("LOCK TABLE " + ", ".join(names) + " IN SHARE MODE NOWAIT")
        )

    if (
        session.execute(
            select(TableArrInstances.id)
            .where(TableArrInstances.id == owner)
            .with_for_update(nowait=True)
        ).scalar_one_or_none()
        is None
    ):
        raise ValueError("Subtitle destination owner no longer exists")


def _instance_rows(session):
    return tuple(
        session.execute(
            select(
                TableArrInstances.id,
                TableArrInstances.kind,
                TableArrInstances.path_mappings,
            ).order_by(TableArrInstances.id)
        ).all()
    )


def _native_mapping(raw, media_type):
    fallback = (
        path_mappings.path_mapping_movies
        if media_type == "movie"
        else path_mappings.path_mapping_series
    )
    try:
        parsed = json.loads(raw) if raw else None
        if isinstance(parsed, dict):
            parsed = parsed.get("movies" if media_type == "movie" else "series")
        if isinstance(parsed, list):
            return [
                pair
                for pair in parsed
                if isinstance(pair, (list, tuple))
                and len(pair) >= 2
                and all(isinstance(value, str) and value for value in pair[:2])
                and pair[0] != pair[1]
            ]
    except (TypeError, ValueError):
        pass
    return fallback


def _configuration():
    # Pure in-memory values. Physical resolution belongs to preparation.
    return (
        settings.general.subfolder,
        settings.general.subfolder_custom,
        repr(path_mappings.path_mapping_series),
        repr(path_mappings.path_mapping_movies),
        repr(global_sports_mappings()),
    )


def validate_read_path(context, path):
    """Accept the same video-side and configured roots as subtitle discovery."""
    folders = {os.path.dirname(context.mapped_path)}
    folder = get_target_folder(context.mapped_path, create=False)
    if folder:
        folders.add(folder)
    if not (
        _stem(path) == _stem(context.mapped_path)
        or _stem(path).startswith(_stem(context.mapped_path) + ".")
    ) or _physical(os.path.dirname(path)) not in {
        _physical(folder) for folder in folders
    }:
        raise ValueError("Sports subtitle is outside its indexed discovery roots")


class _OwnershipSnapshot:
    def __init__(self, session, configuration, generation, revision):
        self.configuration, self.generation, self.revision = configuration, generation, revision
        self.instances = {owner: (kind, raw) for owner, kind, raw in _instance_rows(session)}
        self.rows, self.by_stem, self.stems, self.invalid = {}, {}, [], set()
        for table, media_type in MEDIA_TABLES:
            for row in session.execute(select(table.id, table.arr_instance_id, table.path, table.subtitles)):
                self.replace(media_type, row)

    def replace(self, media_type, row):
        local_id, owner, path, subtitles = row
        identity = (media_type, local_id)
        raw = self.instances.get(owner, (None, None))[1]
        mapping = read_sports_mappings(raw) if media_type == 'sports' else _native_mapping(raw, media_type)

        def mapped(path):
            return (apply_sports_mapping(path, mapping) if media_type == 'sports'
                    else _apply_mapping(path, mapping, False))

        entries = set()
        if path:
            video = mapped(path)
            folders = {os.path.dirname(video)}
            folder = get_target_folder(video, create=False)
            if folder:
                folders.add(folder)
            entries.update((_stem(video), folder, False, owner) for folder in folders)
        self.invalid.discard(identity)
        try:
            recorded = ast.literal_eval(subtitles or '[]')
            if not isinstance(recorded, (list, tuple)):
                raise ValueError()
            for item in recorded:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    raise ValueError()
                if item[1] is None:
                    continue
                if not isinstance(item[1], str):
                    raise ValueError()
                subtitle = mapped(item[1])
                entries.add((_stem(subtitle), os.path.dirname(subtitle), True, owner))
        except (SyntaxError, TypeError, ValueError):
            self.invalid.add(identity)
        previous = self.rows.get(identity, set())
        for stem, folder, recorded, old_owner in previous - entries:
            self.by_stem[stem].remove((identity, folder, recorded, old_owner))
        for stem, folder, recorded, new_owner in entries - previous:
            if stem not in self.by_stem:
                self.by_stem[stem] = set()
                insort(self.stems, stem)
            self.by_stem[stem].add((identity, folder, recorded, new_owner))
        for stem in {entry[0] for entry in previous - entries}:
            if not self.by_stem[stem]:
                del self.by_stem[stem]
                self.stems.pop(bisect_left(self.stems, stem))
        self.rows[identity] = entries

    def conflict(self, context, stem, folder):
        own = ('sports', context.event_id)
        if self.invalid - {own}:
            return 'Subtitle destination ownership record is invalid'
        candidates = {stem}
        candidates.update(stem[:index] for index, char in enumerate(stem) if char == '.')
        start = bisect_left(self.stems, stem + '.')
        while start < len(self.stems) and self.stems[start].startswith(stem + '.'):
            candidates.add(self.stems[start])
            start += 1
        for candidate in candidates:
            for identity, candidate_folder, recorded, owner in self.by_stem.get(candidate, ()):
                if identity == own and owner == context.arr_instance_id:
                    continue
                # Resolve plausible folders now, never cache filesystem aliases.
                if _physical(candidate_folder) == folder:
                    return ('Subtitle destination is recorded for another owner' if recorded
                            else 'Subtitle destination is ambiguous between media owners')
        return None


_snapshots = WeakKeyDictionary()
_snapshot_lock = RLock()


def _snapshot(session, configuration, revision):
    from sportarr.db import needs_sports_transaction

    bind = session.get_bind()
    # Do not let a snapshot containing uncommitted writes escape its transaction.
    cacheable = needs_sports_transaction(session)
    with _snapshot_lock:
        generation = session.execute(text(
            "SELECT revision FROM subtitle_ownership_changes WHERE table_name='generation' AND row_id=0"
        )).scalar_one_or_none()
        if generation is None:
            raise ValueError('Subtitle ownership generation is unavailable')
        cached = _snapshots.get(bind) if cacheable else None
        try:
            changes = []
            if (cached is not None and cached.configuration == configuration
                    and cached.generation == generation and cached.revision <= revision):
                changes = session.execute(text(
                    'SELECT table_name, row_id FROM subtitle_ownership_changes WHERE revision > :revision'
                ), {'revision': cached.revision}).all()
                if any(name in ('*', 'arr_instances') for name, _ in changes):
                    cached = None
            else:
                cached = None
            if cached is None:
                cached = _OwnershipSnapshot(session, configuration, generation, revision)
            else:
                for table, media_type in MEDIA_TABLES:
                    ids = [row_id for name, row_id in changes if name == table.__tablename__]
                    if ids:
                        for row in session.execute(select(table.id, table.arr_instance_id, table.path,
                                                          table.subtitles).where(table.id.in_(ids))):
                            cached.replace(media_type, row)
            if revision != ownership_revision(session) or configuration != _configuration():
                raise ValueError('Subtitle destination ownership changed. Please retry.')
            cached.revision = revision
            if cacheable:
                _snapshots[bind] = cached
            return cached
        except BaseException:
            if cacheable:
                _snapshots.pop(bind, None)
            raise


class SportsOutputNamespace:
    """Versioned ownership preparation with a constant-size publication recheck."""

    def __init__(self, context, session, *, read_path=None):
        self.context = context
        verify_ownership_protection(session)
        self.configuration = _configuration()
        self.revision = ownership_revision(session)
        if read_path is not None:
            validate_read_path(context, read_path)
        self.folder = _physical(
            os.path.dirname(read_path) if read_path is not None
            else get_target_folder(context.mapped_path, create=False) or os.path.dirname(context.mapped_path)
        )
        self.stem = _stem(context.mapped_path)
        snapshot = _snapshot(session, self.configuration, self.revision)
        self.generation = snapshot.generation
        self.conflict = snapshot.conflict(context, self.stem, self.folder)
        self.validate(session)

    def validate(self, session):
        if (self.revision, self.generation) != ownership_token(session) or self.configuration != _configuration():
            raise ValueError('Subtitle destination ownership changed. Please retry.')
        if self.conflict:
            raise ValueError(self.conflict)
