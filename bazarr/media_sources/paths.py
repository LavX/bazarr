# coding=utf-8
"""Checking a folder before it becomes a library: what refuses it, and what only deserves a warning."""
import logging
import os
from dataclasses import dataclass, field

import sqlalchemy as sa

from . import identity, repository
from .kinds import Problem, get_kind


@dataclass
class PathCheck:
    path: object
    errors: list = field(default_factory=list)     # Problem
    warnings: list = field(default_factory=list)   # str

    @property
    def ok(self):
        return not self.errors

    def as_dict(self):
        return {'path': self.path, 'ok': self.ok, 'errors': [{'code': error.code, 'message': error.message}
                                                              for error in self.errors],
                'warnings': list(self.warnings)}


class _Draft:
    def __init__(self, locator, content_kind):
        self.locator = locator
        self.content_kind = content_kind


def normalize(path):
    """The form a folder path is stored in. Anything that is not a usable string is returned as it is, for the
    kind to refuse."""
    if isinstance(path, str) and path.strip() and '\x00' not in path:
        return repository.normalize_path(path)
    return path


def _arr_root_folders(target=None):
    """``[(instance name, Bazarr-visible root folder)]`` for the root folders of every arr instance."""
    from app.database import (TableArrInstances, TableMoviesRootfolder, TableShowsRootfolder,
                              TableSportsLeaguesRootfolder, database)
    session = database if target is None else target
    instances = {row.id: row for row in session.execute(sa.select(TableArrInstances)).scalars().all()}
    roots = []
    for model in (TableMoviesRootfolder, TableShowsRootfolder, TableSportsLeaguesRootfolder):
        for arr_instance_id, path in session.execute(sa.select(model.arr_instance_id, model.path)).all():
            instance = instances.get(arr_instance_id)
            if instance is None or not path:
                continue
            mapper = identity._mapper_for(instance)
            roots.append((instance.name, mapper.forward(path) if mapper is not None else path))
    return roots


def validate_path(path, content_kind='movie', *, source_kind='folder', target=None, exclude_library_id=None,
                  include_warnings=True):
    """Everything the Connections page says about a folder before it is added: the refusals (absolute, existing,
    readable, not ``/``, not overlapping Bazarr+'s configuration folder or a root of another library of any
    source) and the warnings (an arr root folder, a folder that is not writable)."""
    check = PathCheck(path=normalize(path))
    kind = get_kind(source_kind)
    check.errors.extend(kind.validate_library(None, _Draft(check.path, content_kind)))
    if check.errors:
        return check
    clash = repository.overlapping_root(check.path, target, exclude_library_id=exclude_library_id)
    if clash is not None:
        check.errors.append(Problem(
            'overlap', f"This folder overlaps {clash[1]}, a library of the media source {clash[0]}. A folder can "
                       f"belong to one library only: use a folder that neither contains nor sits inside another "
                       f"library's."))
        return check
    if not include_warnings:
        return check
    try:
        for name, root in _arr_root_folders(target):
            normal = os.path.normcase(os.path.normpath(root))
            mine = os.path.normcase(os.path.normpath(check.path))
            if mine == normal or mine.startswith(normal.rstrip(os.sep) + os.sep) \
                    or normal.startswith(mine.rstrip(os.sep) + os.sep):
                check.warnings.append(
                    f"This folder overlaps a root folder of {name}. Files {name} manages are handled by it, and "
                    f"the same files here are marked as managed by it.")
    except Exception:
        logging.exception('BAZARR could not compare a new library folder with the arr root folders')
    if not os.access(check.path, os.W_OK):
        check.warnings.append('This folder is not writable. Subtitles are written next to the media, or into the '
                              'configured subfolder.')
    return check


class ServerRoots:
    """Map one library's remote paths using only its own saved roots."""

    def __init__(self, roots):
        self.rows = [dict(local_path=root.server_path, remote_path=root.local_path,
                          library_id=root.local_path) for root in roots]

    def resolve(self, server_path):
        from media_servers.http import MediaServerError
        from media_servers.paths import map_media_path

        resolved = map_media_path(server_path, self.rows)
        path, root = resolved['path'], resolved['library_id']
        real_root = os.path.realpath(root)
        try:
            if os.path.commonpath((real_root, os.path.realpath(path))) != real_root:
                raise MediaServerError('path_invalid')
        except ValueError:
            raise MediaServerError('path_invalid') from None
        return path, root
