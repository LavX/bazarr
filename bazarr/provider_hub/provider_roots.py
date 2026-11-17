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
import shutil
import sys
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


def binary_folders() -> list[str]:
    """The folders that hold the media tools the plugins run, by where the files really are.

    ``ffmpeg``, ``ffprobe`` and ``mediainfo`` are found through ``PATH`` or, for a download, in the
    folder Bazarr+ keeps them in; either can be a link to a file elsewhere, and the plugin
    executes the path the host hands it. The folder of each file after links is granted.
    """
    folders: dict[str, None] = {}
    for name in ("ffmpeg", "ffprobe", "mediainfo"):
        candidates = []
        try:
            from utilities.binaries import get_binary

            candidates.append(get_binary(name))
        except Exception:
            logger.debug("Provider Hub: could not look up %s", name, exc_info=True)
        candidates.append(shutil.which(name))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and os.path.isfile(candidate):
                folder = os.path.dirname(os.path.realpath(candidate))
                if folder and folder != "/":
                    folders[folder] = None
    return list(folders)


def own_ports() -> list[int]:
    """Every TCP port Bazarr+ answers on, whichever way it was started.

    The server binds ``--port`` when it is given and ``general.port`` otherwise, and falls back to
    6767 when that port is taken, so the setting alone is not the port. The docker supervisor starts
    the backend on a port of its own (``--port``) and keeps ``general.port`` for its front listener.
    All of them are listed: the port the running server really bound (its own record, and the
    socket's), the ``--port`` argument, and the setting.
    """
    found: dict[int, None] = {}

    def add(value):
        try:
            port = int(value)
        except (TypeError, ValueError):
            return
        if 1 <= port <= 65535:
            found[port] = None

    server_module = sys.modules.get("app.server")
    server = getattr(server_module, "webserver", None)
    if server is not None:
        add(getattr(server, "port", None))
        bound = getattr(server, "server", None)
        add(getattr(bound, "effective_port", None))
        for entry in getattr(bound, "_map", None) or ():
            add(getattr(entry, "effective_port", None))
    try:
        from app.get_args import args

        add(getattr(args, "port", None))
    except Exception:
        logger.debug("Provider Hub: could not read the --port argument", exc_info=True)
    try:
        from app.config import settings

        add(settings.general.port)
    except Exception:
        logger.debug("Provider Hub: could not read general.port", exc_info=True)
    return list(found)


def authentication_off() -> bool:
    """True while no login is set, which leaves Bazarr+'s own API open to anything that can reach its port."""
    try:
        from app.config import settings

        return str(settings.auth.type or "").strip().lower() in ("", "none")
    except Exception:
        return False


def backup_folders() -> list[Path]:
    """The folder the backups are written to, which holds copies of ``config.yaml`` and the database."""
    try:
        from app.config import settings

        folder = str(settings.backup.folder or "").strip()
    except Exception:
        logger.debug("Provider Hub: could not read the backup folder", exc_info=True)
        return []
    return [Path(os.path.abspath(folder))] if folder else []
