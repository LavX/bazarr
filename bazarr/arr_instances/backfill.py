# coding=utf-8
"""One-time backfill: represent the existing single-instance Sonarr/Radarr
scalar config as the default arr_instances rows, and stamp existing owned rows
with their arr_instance_id (#156).

Idempotent and non-destructive: a kind is backfilled only when no default
instance of that kind exists yet AND there is something to own (use_sonarr /
use_radarr enabled, or any existing owned row). A manually created default is
never replaced. Runs at startup after migrations.
"""
import logging

from sqlalchemy import select, update

from app.database import (
    TableBlacklist,
    TableBlacklistMovie,
    TableEpisodes,
    TableHistory,
    TableHistoryMovie,
    TableMovies,
    TableMoviesRootfolder,
    TableShows,
    TableShowsRootfolder,
)

from .repository import ArrInstanceRepository

logger = logging.getLogger(__name__)

# Tables whose rows are owned by a Sonarr / Radarr instance.
_SONARR_TABLES = (
    TableShows, TableEpisodes, TableHistory, TableBlacklist, TableShowsRootfolder,
)
_RADARR_TABLES = (
    TableMovies, TableHistoryMovie, TableBlacklistMovie, TableMoviesRootfolder,
)


def _has_any_rows(session, tables):
    for model in tables:
        if session.execute(select(model).limit(1)).first() is not None:
            return True
    return False


def _backfill_kind(session, repo, kind, scalar, use_flag, tables):
    if repo.get_default(kind) is not None:
        return {"created": False, "reason": "default already exists"}
    if not use_flag and not _has_any_rows(session, tables):
        return {"created": False, "reason": "nothing to own"}

    instance = repo.create(
        kind,
        kind.capitalize(),
        api_key=getattr(scalar, "apikey", "") or "",
        ip=getattr(scalar, "ip", "127.0.0.1") or "127.0.0.1",
        port=getattr(scalar, "port", None),
        base_url=getattr(scalar, "base_url", "/") or "/",
        ssl=bool(getattr(scalar, "ssl", False)),
        verify_ssl=bool(getattr(scalar, "verify_ssl", False)),
        http_timeout=getattr(scalar, "http_timeout", 60) or 60,
        enabled=True,
        is_default=True,
    )

    stamped = 0
    for model in tables:
        result = session.execute(
            update(model)
            .where(model.arr_instance_id.is_(None))
            .values(arr_instance_id=instance.id)
        )
        stamped += result.rowcount or 0

    logger.info(
        "Backfilled default %s instance (id=%s); stamped %s existing rows",
        kind, instance.id, stamped,
    )
    return {"created": True, "instance_id": instance.id, "stamped": stamped}


def backfill_default_instances(session, settings, repo=None):
    """Backfill default Sonarr/Radarr instances from scalar config.

    Returns a per-kind summary dict. Safe to call on every startup.
    """
    repo = repo or ArrInstanceRepository(session)
    return {
        "sonarr": _backfill_kind(
            session, repo, "sonarr", settings.sonarr,
            bool(settings.general.use_sonarr), _SONARR_TABLES),
        "radarr": _backfill_kind(
            session, repo, "radarr", settings.radarr,
            bool(settings.general.use_radarr), _RADARR_TABLES),
    }
