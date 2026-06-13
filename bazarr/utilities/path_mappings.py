# coding=utf-8

import json
import logging
import re

from app.config import settings


def _apply_mapping(path, mapping, reverse):
    """Apply a single [remote, local] mapping list to ``path``.

    ``reverse`` swaps the direction (local->remote). Mirrors the in-class
    path_replace* loop exactly so per-instance mappings behave identically to
    the global ones; first matching pair wins.
    """
    if path is None:
        return None
    src_index, dst_index = (1, 0) if reverse else (0, 1)
    for path_mapping in mapping:
        if len(path_mapping) < 2 or path_mapping[0] == path_mapping[1]:
            continue
        if '' in path_mapping:
            continue
        if path_mapping[src_index] in path:
            path = path.replace(path_mapping[src_index], path_mapping[dst_index])
            if path.startswith('\\\\') or re.match(r'^[a-zA-Z]:\\', path):
                path = path.replace('/', '\\')
            elif path.startswith('/'):
                path = path.replace('\\', '/')
            break
    return path


def _instance_path_mapping(arr_instance_id, media_type):
    """Return the per-instance [remote, local] mapping list for ``arr_instance_id``
    (#156), or None when the instance has no path_mappings configured.

    The arr_instances ``path_mappings`` column is a JSON blob. Two shapes are
    accepted so single-purpose and split configs both work:

      * a bare list ``[[remote, local], ...]`` applied to both series + movies;
      * an object ``{"series": [...], "movies": [...]}`` (missing key => none).

    Returns None (caller falls back to the global mapping) on a missing
    instance, empty/unset column, or malformed JSON. ``media_type`` is
    'series'/'episode' or 'movie'/'movies'.
    """
    if arr_instance_id is None:
        return None
    try:
        from app.database import database
        from arr_instances.repository import ArrInstanceRepository

        row = ArrInstanceRepository(database).get(arr_instance_id)
        if row is None or not row.path_mappings:
            return None
        parsed = json.loads(row.path_mappings)
    except Exception:
        logging.debug('BAZARR could not load per-instance path_mappings for instance %s', arr_instance_id)
        return None

    if isinstance(parsed, dict):
        key = 'movies' if media_type in ('movie', 'movies') else 'series'
        mapping = parsed.get(key)
    else:
        mapping = parsed
    if not isinstance(mapping, list):
        return None
    return [m for m in mapping if isinstance(m, (list, tuple)) and len(m) >= 2 and m[0] != m[1]]


class PathMappings:
    def __init__(self):
        self.path_mapping_series = []
        self.path_mapping_movies = []

    def update(self):
        self.path_mapping_series = [x for x in settings.general.path_mappings if x[0] != x[1]]
        self.path_mapping_movies = [x for x in settings.general.path_mappings_movie if x[0] != x[1]]

    def path_replace_instance(self, path, arr_instance_id, media_type):
        """Path-replace ``path`` using the owning instance's per-instance
        path_mappings when configured (#156), else the global mapping.

        media_type is 'series'/'episode' or 'movie'/'movies'. This is the entry
        point to use wherever a media row carries an arr_instance_id, so the
        per-instance path_mappings column is no longer silently ignored.
        """
        mapping = _instance_path_mapping(arr_instance_id, media_type)
        if mapping is not None:
            return _apply_mapping(path, mapping, reverse=False)
        if media_type in ('movie', 'movies'):
            return self.path_replace_movie(path)
        return self.path_replace(path)

    def path_replace_reverse_instance(self, path, arr_instance_id, media_type):
        """Reverse of :meth:`path_replace_instance` (local->remote)."""
        mapping = _instance_path_mapping(arr_instance_id, media_type)
        if mapping is not None:
            return _apply_mapping(path, mapping, reverse=True)
        if media_type in ('movie', 'movies'):
            return self.path_replace_reverse_movie(path)
        return self.path_replace_reverse(path)

    def path_replace(self, path):
        if path is None:
            return None

        for path_mapping in self.path_mapping_series:
            if path_mapping[0] == path_mapping[1]:
                continue
            if '' in path_mapping:
                continue
            if path_mapping[0] in path:
                path = path.replace(path_mapping[0], path_mapping[1])
                if path.startswith('\\\\') or re.match(r'^[a-zA-Z]:\\', path):
                    path = path.replace('/', '\\')
                elif path.startswith('/'):
                    path = path.replace('\\', '/')
                break
        return path

    def path_replace_reverse(self, path):
        if path is None:
            return None

        for path_mapping in self.path_mapping_series:
            if path_mapping[0] == path_mapping[1]:
                continue
            if '' in path_mapping:
                continue
            if path_mapping[1] in path:
                path = path.replace(path_mapping[1], path_mapping[0])
                if path.startswith('\\\\') or re.match(r'^[a-zA-Z]:\\', path):
                    path = path.replace('/', '\\')
                elif path.startswith('/'):
                    path = path.replace('\\', '/')
                break
        return path

    def path_replace_movie(self, path):
        if path is None:
            return None

        for path_mapping in self.path_mapping_movies:
            if path_mapping[0] == path_mapping[1]:
                continue
            if '' in path_mapping:
                continue
            if path_mapping[0] in path:
                path = path.replace(path_mapping[0], path_mapping[1])
                if path.startswith('\\\\') or re.match(r'^[a-zA-Z]:\\', path):
                    path = path.replace('/', '\\')
                elif path.startswith('/'):
                    path = path.replace('\\', '/')
                break
        return path

    def path_replace_reverse_movie(self, path):
        if path is None:
            return None

        for path_mapping in self.path_mapping_movies:
            if path_mapping[0] == path_mapping[1]:
                continue
            if '' in path_mapping:
                continue
            if path_mapping[1] in path:
                path = path.replace(path_mapping[1], path_mapping[0])
                if path.startswith('\\\\') or re.match(r'^[a-zA-Z]:\\', path):
                    path = path.replace('/', '\\')
                elif path.startswith('/'):
                    path = path.replace('\\', '/')
                break
        return path


# NOTE (#156): the per-instance path_mappings column is now honoured via
# path_replace_instance / path_replace_reverse_instance, applied in
# api/subtitles/content.py (subtitle path, video-path fallback, and the
# re-anchor lookup). The many other path_replace* call sites across the
# subtitle pipeline (processing/upload/delete/indexer/sync) still use the
# GLOBAL mapping; migrating each to the per-instance variant requires threading
# the owning arr_instance_id to every site and is tracked as follow-up work.
# Prefer path_replace_instance wherever an arr_instance_id is readily available.
path_mappings = PathMappings()
