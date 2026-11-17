# coding=utf-8
"""Where a subtitle provider worker may read: the media library and the folders it runs programs from.

The provider sandbox profile is an allowlist. Besides the system paths, the interpreter and the plugin's
own bundle, a worker is given the media root folders (it probes and extracts from media files) and the
directories on ``PATH`` (``ffmpeg`` and ``ffprobe`` are found there), nothing else. The configuration
folder, the backup folder, home folders and every other service's data stay out of reach.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def media_root_folders() -> list[str]:
    """Every media root folder Bazarr+ knows, as this host sees it. Folders that do not exist are left out.

    Read from the Sonarr, Radarr and Sportarr root folders of every instance, each also through the
    path mappings that apply to its instance, because the stored path is the one the arr application
    reports. Raises when the folders cannot be read: a profile built without them would deny every
    media file to a plugin that needs one, and that is better said than guessed.
    """
    from app.database import (
        TableMoviesRootfolder,
        TableShowsRootfolder,
        TableSportsLeaguesRootfolder,
        database,
        select,
    )
    from utilities.path_mappings import path_mappings

    found: dict[str, None] = {}

    def add(path):
        if isinstance(path, str) and path.strip() and os.path.isdir(path):
            found[os.path.normpath(path)] = None

    for table, kind in ((TableShowsRootfolder, "series"), (TableMoviesRootfolder, "movie")):
        for instance_id, path in database.execute(select(table.arr_instance_id, table.path)).all():
            add(path)
            try:
                if instance_id is not None:
                    add(path_mappings.path_replace_instance(path, instance_id, kind))
                add(path_mappings.path_replace(path) if kind == "series" else path_mappings.path_replace_movie(path))
            except Exception:
                logger.debug("Provider Hub: no path mapping applied to the root folder %s", path, exc_info=True)
    for instance_id, path in database.execute(
        select(TableSportsLeaguesRootfolder.arr_instance_id, TableSportsLeaguesRootfolder.path)
    ).all():
        add(path)
        try:
            add(path_mappings.path_replace_instance(path, instance_id, "sports"))
        except Exception:
            logger.debug("Provider Hub: no path mapping applied to the sports root folder %s", path, exc_info=True)
    return list(found)


def program_folders(path_value: str | None = None) -> list[str]:
    """The absolute, existing directories on ``PATH`` (default: this process's), in order."""
    value = os.environ.get("PATH", "") if path_value is None else path_value
    folders: dict[str, None] = {}
    for entry in value.split(os.pathsep):
        if entry.startswith("/") and entry != "/" and os.path.isdir(entry):
            folders[os.path.normpath(entry)] = None
    return list(folders)


def backup_folders() -> list[Path]:
    """The folder the backups are written to, which holds copies of ``config.yaml`` and the database."""
    try:
        from app.config import settings

        folder = str(settings.backup.folder or "").strip()
    except Exception:
        logger.debug("Provider Hub: could not read the backup folder", exc_info=True)
        return []
    return [Path(os.path.abspath(folder))] if folder else []
