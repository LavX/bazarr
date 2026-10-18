# coding=utf-8
"""One-time backfill: represent the existing single-instance Sonarr/Radarr
scalar config as the default arr_instances rows, and stamp existing owned rows
with their arr_instance_id (#156).

Idempotent and non-destructive: a kind is backfilled only when no default
instance of that kind exists yet AND there is something to own (use_sonarr /
use_radarr enabled, or any row with no owner yet). A manually created default
is never replaced. Once an instance has been deleted (a retired id is on
record), the ownerless rows may be straggler writes of the deleted server
rather than legacy rows, so they are quarantined: left ownerless instead of
stamped onto the surviving instance, and never stamped onto an instance
rebuilt from the stored scalar settings, which describe the deleted server.
Runs at startup after migrations.
"""
import logging

from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database import (
    TableArrInstanceRetiredIds,
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

from media_servers.repository import atomic

from .repository import ArrInstanceRepository

logger = logging.getLogger(__name__)

# Tables whose rows are owned by a Sonarr / Radarr instance.
_SONARR_TABLES = (
    TableShows, TableEpisodes, TableHistory, TableBlacklist, TableShowsRootfolder,
)
_RADARR_TABLES = (
    TableMovies, TableHistoryMovie, TableBlacklistMovie, TableMoviesRootfolder,
)

# The upstream-id column of every stamped table whose unique index scopes its
# rows to one instance (ux_table_episodes_instance_upstream_id and its siblings
# on shows, movies and the two rootfolder tables). Only these tables can fail a
# stamp, and the row-group retry below keys on this column: the one its unique
# index is built on, present before and after the cutover alike, unlike the
# local PK, which is still NULL on every row while the cutover migration runs
# this backfill. The history and blacklist tables carry no unique index on the
# owner, so their one-shot stamp cannot collide.
_UPSTREAM_ID_COLUMNS = {
    TableShows: "sonarrSeriesId",
    TableEpisodes: "sonarrEpisodeId",
    TableMovies: "radarrId",
    TableShowsRootfolder: "upstream_rootfolder_id",
    TableMoviesRootfolder: "upstream_rootfolder_id",
}


def _has_retired_ids(session):
    # Whether an instance has ever been deleted: the retired-id table keeps
    # every removed id. It names no kind, so one retirement anywhere is all the
    # discrimination there is and both kinds read the same answer: with none on
    # record the ownerless rows are the legacy cutover's or a crashed create's
    # resume leftovers, while after one they may be orphans of the deleted
    # instance or of its straggler writers. The table is read only when it is
    # there, the way _unused_id reads it: this backfill also runs from the
    # cutover migration, before the migration that creates it.
    existing = inspect(session.connection()).get_table_names()
    if TableArrInstanceRetiredIds.__tablename__ not in existing:
        return False
    return session.execute(
        select(TableArrInstanceRetiredIds.id).limit(1)).first() is not None


def _has_ownerless_rows(session, tables):
    # Only rows with no owner yet: the legacy rows this backfill exists to
    # stamp. A row naming an instance that is gone was written after that
    # instance was deleted, and the stamp below never touches it, so it is no
    # reason to rebuild an instance from the scalar config.
    # Select a single column (not the full entity): a full-entity select loads
    # ORM objects into the identity map keyed by the PK, and during the cutover
    # migration the local-id PK is still NULL on every row, which corrupts the
    # identity map and breaks the bulk stamp below. A scalar existence check is
    # also cheaper.
    for model in tables:
        if session.execute(select(model.arr_instance_id)
                           .where(model.arr_instance_id.is_(None)).limit(1)).first() is not None:
            return True
    return False


def _count_ownerless_rows(session, tables):
    # The same rows _has_ownerless_rows tests for, counted: the quarantine
    # lines below name how many rows stay ownerless. A scalar count loads no
    # entity into the identity map, for the reason on _has_ownerless_rows.
    total = 0
    for model in tables:
        total += session.execute(
            select(func.count()).select_from(model)
            .where(model.arr_instance_id.is_(None))).scalar_one()
    return total


def _stamp_ownerless(session, table, instance_id, where, params=None):
    result = session.execute(
        text(f"UPDATE {table} "
             f"SET arr_instance_id = :iid WHERE {where}"),
        {"iid": instance_id, **(params or {})},
    )
    return result.rowcount or 0


def _stamp_null_group(session, table, column, instance_id):
    # The NULL-upstream group cannot key its retry on the upstream id, and one
    # statement for the whole group would quarantine every NULL row on one
    # collision: shows and movies carry more unique indexes scoped to the
    # instance (their path, a movie's tmdbId), so a row whose path the
    # instance already owns rejects the entire group statement. Stamp the
    # NULL rows one by one, keyed on the path every stamped table carries, so
    # a collision quarantines only the row that collided. A NULL path can
    # only sit on a table with no path or tmdbId index (episodes and the two
    # rootfolder tables), where nothing can collide, so those rows keep one
    # shared statement.
    stamped = 0
    paths = session.execute(text(
        f'SELECT DISTINCT "path" FROM {table} '
        'WHERE arr_instance_id IS NULL '
        f'AND "{column}" IS NULL '
        'ORDER BY "path"')).scalars().all()
    for path in paths:
        if path is None:
            where = (f'arr_instance_id IS NULL AND "{column}" IS NULL '
                     'AND "path" IS NULL')
        else:
            where = (f'arr_instance_id IS NULL AND "{column}" IS NULL '
                     'AND "path" = :path')
        try:
            with atomic(session):
                stamped += _stamp_ownerless(
                    session, table, instance_id, where,
                    None if path is None else {"path": path})
        except IntegrityError:
            logger.info(
                "Quarantined an ownerless %s row (%s = NULL, path = %s): a "
                "unique index covers it with a row instance id=%s already "
                "owns, so the row stays ownerless",
                table, column, path, instance_id)
    return stamped


def _stamp_table(session, model, instance_id):
    # One table's stamp, isolated from every colliding row. The bulk stamp runs
    # first, in a savepoint of its own; when the unique index rejects it, the
    # savepoint is rolled back and the table is retried row-group by row-group,
    # keyed on the column of its unique index, so the groups that fit stamp and
    # only the colliding one stays ownerless, logged.
    table = model.__tablename__
    try:
        with atomic(session):
            return _stamp_ownerless(session, table, instance_id,
                                    "arr_instance_id IS NULL")
    except IntegrityError:
        column = _UPSTREAM_ID_COLUMNS.get(model)
        if column is None:
            # No unique index scopes this table's rows to an instance, so a
            # collision here is not one this backfill can isolate; let it
            # surface as it always did.
            raise
    stamped = 0
    values = session.execute(text(
        f'SELECT DISTINCT "{column}" FROM {table} '
        'WHERE arr_instance_id IS NULL '
        f'ORDER BY "{column}"')).scalars().all()
    for value in values:
        # Every retry keeps the ownerless filter: a retry matching only the
        # group's upstream id would also update rows other owners already
        # hold, reassigning them or colliding with them and leaving a stampable
        # orphan quarantined.
        if value is None:
            # The NULL group has no upstream id to share, so its rows cannot
            # be stamped together without one collision quarantining them all.
            stamped += _stamp_null_group(session, table, column, instance_id)
            continue
        where = f'arr_instance_id IS NULL AND "{column}" = :upstream'
        try:
            with atomic(session):
                stamped += _stamp_ownerless(
                    session, table, instance_id, where, {"upstream": value})
        except IntegrityError:
            logger.info(
                "Quarantined an ownerless %s row (%s = %s): a unique index "
                "covers it with a row instance id=%s already owns, so the row "
                "stays ownerless",
                table, column, value, instance_id)
    return stamped


def _stamp_null_rows(session, tables, instance_id):
    # Stamp via raw SQL keyed on the table name, not an ORM bulk update: the ORM
    # update synchronizes the session by the model PK, which during the cutover
    # migration is the still-NULL local id (the cutover populates it AFTER this
    # bootstrap), leaving rows unstamped. Raw SQL is PK-agnostic and behaves
    # identically at runtime (post-cutover). Table and column names are trusted
    # ORM constants.
    # One colliding row must not abort the backfill: an ownerless row can carry
    # an upstream id (or, on shows and movies, a path or tmdbId) the instance
    # already owns, the same series or movie tracked by both the default and a
    # deleted secondary, or a reused external id. Each table's stamp runs inside
    # its own savepoint (atomic() also opens the real transaction PostgreSQL
    # demands before one), and a table whose bulk stamp collides is retried
    # row-group by row-group so that only the colliding rows stay ownerless.
    stamped = 0
    for model in tables:
        stamped += _stamp_table(session, model, instance_id)
    return stamped


def _backfill_kind(session, repo, kind, scalar, use_flag, tables, has_retired_ids):
    existing = repo.list(kind=kind)
    if existing:
        # An instance already exists - do NOT recreate it (a demoted/disabled
        # instance must not be resurrected into a duplicate). But the create +
        # stamp is not atomic and the create commits immediately under AUTOCOMMIT,
        # so a crash between them leaves owned rows NULL forever if we just skip.
        # Resume the stamp when this is still a single-instance kind (the backfill
        # scenario), where stamping every orphaned row to the lone instance is
        # unambiguous. Key on the single instance itself, NOT get_default: a
        # partial crash (or the user disabling the only instance) can leave it
        # non-default/disabled, and get_default would then return None and strand
        # the orphans. With 2+ instances a NULL row's owner is genuinely unknown,
        # so leave it (scoped reads + the delete-guard handle it).
        # That resume is only unambiguous while no instance was ever deleted:
        # once one was, an ownerless row may be a straggler write of the deleted
        # instance (an exclusion whose episode row is already gone, a one-item
        # sync past its default check), and stamping it onto the lone survivor
        # would put the deleted server's media under the surviving one, under
        # its paths and credentials. With a retirement on record the rows stay
        # ownerless, and one line says how many and why.
        if len(existing) == 1:
            owner = existing[0]
            if has_retired_ids:
                orphaned = _count_ownerless_rows(session, tables)
                if orphaned:
                    logger.info(
                        "Quarantined %s ownerless %s row(s): an instance has been "
                        "deleted, so they stay ownerless rather than being stamped "
                        "onto instance id=%s",
                        orphaned, kind, owner.id)
                return {"created": False, "reason": "instance already exists",
                        "stamped": 0, "quarantined": orphaned}
            stamped = _stamp_null_rows(session, tables, owner.id)
            if stamped:
                logger.info(
                    "Resumed %s backfill: stamped %s orphaned rows onto instance id=%s",
                    kind, stamped, owner.id)
            return {"created": False, "reason": "instance already exists",
                    "stamped": stamped}
        return {"created": False, "reason": "instance already exists"}
    if not use_flag and not _has_ownerless_rows(session, tables):
        return {"created": False, "reason": "nothing to own"}
    if has_retired_ids:
        # A kind with no instance left and a retirement on record cannot be
        # rebuilt from the scalar settings: the retirement is deployment-wide
        # (it may be this kind's own or the other kind's), and the settings may
        # describe a deleted server, so creating from them would resurrect it,
        # with every ownerless row stamped onto the copy. Only the legacy
        # cutover (no retirement on record) builds an instance here.
        orphaned = _count_ownerless_rows(session, tables)
        logger.info(
            "Refused to rebuild the %s instance from the stored connection "
            "settings: the deployment has a retired instance id on record, so "
            "the settings may describe a deleted server; %s ownerless row(s) "
            "stay ownerless",
            kind, orphaned)
        return {"created": False, "reason": "deployment has a retired instance id",
                "quarantined": orphaned}

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

    stamped = _stamp_null_rows(session, tables, instance.id)
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
    has_retired_ids = _has_retired_ids(session)
    return {
        "sonarr": _backfill_kind(
            session, repo, "sonarr", settings.sonarr,
            bool(settings.general.use_sonarr), _SONARR_TABLES, has_retired_ids),
        "radarr": _backfill_kind(
            session, repo, "radarr", settings.radarr,
            bool(settings.general.use_radarr), _RADARR_TABLES, has_retired_ids),
    }
