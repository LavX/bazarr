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

from sqlalchemy import select, text

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
    # Select a single column (not the full entity): a full-entity select loads
    # ORM objects into the identity map keyed by the PK, and during the cutover
    # migration the local-id PK is still NULL on every row, which corrupts the
    # identity map and breaks the bulk stamp below. A scalar existence check is
    # also cheaper.
    for model in tables:
        if session.execute(select(model.arr_instance_id).limit(1)).first() is not None:
            return True
    return False


def _backfill_kind(session, repo, kind, scalar, use_flag, tables):
    # Skip if ANY instance of this kind already exists (not just a default):
    # once the user manages instances, a demoted/disabled instance must not be
    # silently resurrected into a duplicate on the next startup.
    if repo.list(kind=kind):
        return {"created": False, "reason": "instance already exists"}
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
        # Stamp via raw SQL keyed on the table name, not an ORM bulk update:
        # the ORM update synchronizes the session by the model PK, which during
        # the cutover migration is the still-NULL local id (the cutover
        # populates it AFTER this bootstrap), leaving rows unstamped. Raw SQL is
        # PK-agnostic and behaves identically at runtime (post-cutover).
        # Table name is a trusted ORM constant, not user input.
        result = session.execute(
            text(f"UPDATE {model.__tablename__} "
                 "SET arr_instance_id = :iid WHERE arr_instance_id IS NULL"),
            {"iid": instance.id},
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
