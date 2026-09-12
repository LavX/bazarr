"""Prepare canonical physical ownership outside short publication transactions."""

import ast
import json
import os

from sqlalchemy import select, text
from app.ownership_revision import ownership_revision, verify_ownership_protection

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


class SportsOutputNamespace:
    """A complete outside-lock ownership snapshot with a constant-size recheck."""

    def __init__(self, context, session, *, read_path=None):
        self.context = context
        verify_ownership_protection(session)
        self.configuration = _configuration()
        self.revision = ownership_revision(session)
        if read_path is not None:
            validate_read_path(context, read_path)
        self.folder = _physical(
            os.path.dirname(read_path)
            if read_path is not None
            else get_target_folder(context.mapped_path, create=False)
            or os.path.dirname(context.mapped_path)
        )
        self.stem = _stem(context.mapped_path)
        instances = {owner: (kind, raw) for owner, kind, raw in _instance_rows(session)}
        self.mappings = {}
        self.conflict = None
        for table, media_type in MEDIA_TABLES:
            for local_id, owner, path, subtitles in session.execute(
                select(table.id, table.arr_instance_id, table.path, table.subtitles)
            ):
                if (
                    media_type == "sports"
                    and local_id == context.event_id
                    and owner == context.arr_instance_id
                ):
                    continue
                if (media_type, owner) not in self.mappings:
                    raw = instances.get(owner, (None, None))[1]
                    self.mappings[media_type, owner] = (
                        read_sports_mappings(raw)
                        if media_type == "sports"
                        else _native_mapping(raw, media_type)
                    )
                if path:
                    mapped = self._mapped(path, media_type, owner)
                    if _overlaps(self.stem, _stem(mapped)):
                        folders = {os.path.dirname(mapped)}
                        folder = get_target_folder(mapped, create=False)
                        if folder:
                            folders.add(folder)
                        if any(_physical(folder) == self.folder for folder in folders):
                            self.conflict = (
                                "Subtitle destination is ambiguous between media owners"
                            )
                try:
                    recorded = ast.literal_eval(subtitles or "[]")
                    if not isinstance(recorded, (list, tuple)):
                        raise ValueError()
                    for item in recorded:
                        if not isinstance(item, (list, tuple)) or len(item) < 2:
                            raise ValueError()
                        subtitle_path = item[1]
                        if subtitle_path is None:
                            continue
                        if not isinstance(subtitle_path, str):
                            raise ValueError()
                        mapped = self._mapped(subtitle_path, media_type, owner)
                        # Do not touch unrelated mounts, even for encoded names.
                        if (
                            _overlaps(self.stem, _stem(mapped))
                            and _physical(os.path.dirname(mapped)) == self.folder
                        ):
                            self.conflict = (
                                "Subtitle destination is recorded for another owner"
                            )
                except (SyntaxError, TypeError, ValueError):
                    self.conflict = "Subtitle destination ownership record is invalid"
        self.validate(session)

    def _mapped(self, path, media_type, owner):
        mapping = self.mappings[media_type, owner]
        return (
            apply_sports_mapping(path, mapping)
            if media_type == "sports"
            else _apply_mapping(path, mapping, False)
        )

    def validate(self, session):
        if (
            self.revision != ownership_revision(session)
            or self.configuration != _configuration()
        ):
            raise ValueError("Subtitle destination ownership changed. Please retry.")
        if self.conflict:
            raise ValueError(self.conflict)
