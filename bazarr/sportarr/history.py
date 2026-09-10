"""Owner-scoped history, release exclusions and proven artifact deletion."""

import json
import os
import hashlib
import shutil
import tempfile
import logging
from types import SimpleNamespace

from datetime import datetime

from sqlalchemy import delete, func, insert, select

from app.database import (
    database,
    TableArrInstances,
    TableBlacklistSports,
    TableHistorySports,
    TableSportsEvents,
)
from sportarr.artifacts import (
    artifact_video_matches,
    capture_artifact,
    capture_replacement_state,
    validate_artifact_stat,
)
from sportarr.connection import check_cancelled
from sportarr.db import sports_transaction, SportsTransactionOutcome
from sportarr.identity import resolve_event_in_session
from sportarr.subtitles import (
    candidate_signature,
    sports_file_publication,
    sports_history,
)
from sportarr.pagination import validate_page
from sportarr.sync.leagues import notify, require_sportarr
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings
from utilities.pretty_date import pretty_date


def list_records(
    session,
    kind,
    arr_instance_id=None,
    event_id=None,
    start=0,
    length=100,
    language=None,
    provider=None,
    action=None,
):
    limit = validate_page(start, length)
    table = TableHistorySports if kind == "history" else TableBlacklistSports
    query = (
        select(table, TableSportsEvents.title)
        .join(
            TableSportsEvents,
            (table.event_id == TableSportsEvents.id)
            & (table.arr_instance_id == TableSportsEvents.arr_instance_id),
        )
        .join(TableArrInstances, table.arr_instance_id == TableArrInstances.id)
        .where(TableArrInstances.kind == "sportarr", TableArrInstances.enabled == 1)
    )
    if arr_instance_id is not None:
        require_sportarr(session, arr_instance_id)
        query = query.where(table.arr_instance_id == arr_instance_id)
    if event_id is not None:
        resolve_event_in_session(session, event_id, arr_instance_id)
        query = query.where(table.event_id == event_id)
    if language:
        query = query.where(table.language == language)
    if provider:
        query = query.where(table.provider == provider)
    if action is not None and kind == "history":
        query = query.where(table.action == action)
    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar_one()
    data = []
    for row, title in session.execute(
        query.order_by(table.timestamp.desc(), table.id.desc())
        .offset(start)
        .limit(limit)
    ):
        item = row.to_dict()
        item.pop("artifact", None)
        # The same pair the episodes and movies history endpoints send: a
        # relative form for the column and the exact date for its popover. The
        # raw ISO string went out under both names, so the sports table printed
        # a machine timestamp where the other two read "2 hours ago".
        if row.timestamp:
            item["timestamp"] = pretty_date(row.timestamp)
            item["parsed_timestamp"] = row.timestamp.strftime("%x %X")
        else:
            item["timestamp"] = None
            item["parsed_timestamp"] = None
        data.append(item | {"title": title})
    return {"data": data, "total": total}


def sports_history_log(action, event_id, arr_instance_id, result):
    """Record a non-provider sports history entry, such as a deletion.

    sports_history writes the provider-download shape and reads fields a
    ProcessSubtitlesResult does not carry (subs_id, matched, not_matched), so a
    deletion needs its own small writer rather than a contorted call into that
    one. Mirrors history_log_movie, which exists for the same reason.
    """
    from sportarr.identity import resolve_event_in_session

    context = resolve_event_in_session(database, event_id, arr_instance_id)
    database.execute(
        insert(TableHistorySports).values(
            event_id=context.event_id,
            league_id=context.league_id,
            arr_instance_id=context.arr_instance_id,
            action=action,
            timestamp=datetime.now(),
            description=result.message,
            # ProcessSubtitlesResult renames its constructor arguments: the
            # reversed paths land on .path and .subs_path, and the language
            # arrives already carrying its :hi or :forced modifier.
            video_path=result.path,
            language=result.language_code,
            provider=result.provider,
            score=result.score,
            subs_id=result.subs_id,
            subtitles_path=result.subs_path,
        )
    )
    database.commit()


def remove_blacklist(session, entry_id, arr_instance_id):
    with sports_transaction(session) as transaction:
        require_sportarr(transaction, arr_instance_id)
        removed = transaction.execute(
            delete(TableBlacklistSports).where(
                TableBlacklistSports.id == entry_id,
                TableBlacklistSports.arr_instance_id == arr_instance_id,
            )
        )
    notify([])
    return removed.rowcount == 1


def clear_blacklist(session, arr_instance_id):
    with sports_transaction(session) as transaction:
        require_sportarr(transaction, arr_instance_id)
        result = transaction.execute(
            delete(TableBlacklistSports).where(
                TableBlacklistSports.arr_instance_id == arr_instance_id
            )
        )
    notify([])
    return result.rowcount


def _prepare_recovery(path, proof, cancel):
    # Copy and verify outside the writer transaction, retaining the original until publication.
    descriptor, backup = tempfile.mkstemp(
        prefix=".bazarr-sports-recovery-", dir=os.path.dirname(path)
    )
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as target, open(path, "rb") as source:
            while block := source.read(1024 * 1024):
                check_cancelled(cancel)
                target.write(block)
                digest.update(block)
            target.flush()
            os.fsync(target.fileno())
        shutil.copystat(path, backup)
        if digest.hexdigest() != json.loads(proof)["sha256"]:
            raise ValueError(
                "Sports subtitle changed while preparing deletion recovery"
            )
        validate_artifact_stat(proof, path)
        check_cancelled(cancel)
        return backup
    except BaseException:
        os.unlink(backup)
        raise


def _restore_recovery(path, backup):
    try:
        # Exclusive installation cannot replace a new file, including a dangling symlink.
        os.link(backup, path)
        if not os.path.samefile(backup, path):
            raise OSError("Restored subtitle identity could not be confirmed")
    except OSError as exc:
        raise OSError(
            f"Sports subtitle recovery could not be confirmed; recovery file retained at {backup}"
        ) from exc
    os.unlink(backup)


def _deletion_committed(record, outcome):
    # Only an acknowledged rollback of the owned transaction authorizes restoration.
    if outcome.rollback_confirmed:
        return False
    if record is None:
        return None
    try:
        with database.get_bind().connect() as connection:
            row = (
                connection.execute(
                    select(TableHistorySports.__table__).where(
                        TableHistorySports.id == record["id"]
                    )
                )
                .mappings()
                .first()
            )
        if row is not None and dict(row) == record:
            return True
    except Exception:
        logging.exception("Could not determine sports subtitle deletion commit outcome")
    return None


def _refresh_after_deletion(context):
    from subtitles.indexer.sports import store_subtitles_sports

    try:
        # Finish accounting for committed disk state even when its caller has stopped.
        store_subtitles_sports(context.event_id, context.arr_instance_id)
    finally:
        notify([context.event_id])


def blacklist_history(history_id, arr_instance_id, *, cancel=None, job_id=None):
    from sportarr.automatic import search_event
    from subtitles.tools.subsync_engines import subtitle_write_locks, subtitle_mutation
    from utilities.helper import get_target_folder

    check_cancelled(cancel)
    row = database.execute(
        select(TableHistorySports)
        .where(
            TableHistorySports.id == history_id,
            TableHistorySports.arr_instance_id == arr_instance_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        row is None
        or row.action not in (1, 2, 3)
        or not row.provider
        or not row.subs_id
    ):
        raise ValueError("A provider download history entry for this owner is required")
    record = row.to_dict()
    context = resolve_event_in_session(database, row.event_id, arr_instance_id)
    signature = candidate_signature(context)
    instance = require_sportarr(database, arr_instance_id)
    path = (
        apply_sports_mapping(
            row.subtitles_path, read_sports_mappings(instance.path_mappings)
        )
        if row.subtitles_path
        else None
    )
    destination = os.path.join(
        get_target_folder(context.mapped_path, create=False)
        or os.path.dirname(context.mapped_path),
        ".destination",
    )
    status = "preserved"
    replacement_state = None
    with subtitle_write_locks(
        context.mapped_path, destination, path or context.mapped_path, cancel=cancel
    ):
        proof = None
        if path and record["artifact"]:
            try:
                proof = capture_artifact(context, path, signature, cancel)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                check_cancelled(cancel)
            if proof != record["artifact"]:
                proof = None
        if (
            path
            and not os.path.lexists(path)
            and artifact_video_matches(record["artifact"], path, signature)
        ):
            status = "absent"
        backup = _prepare_recovery(path, proof, cancel) if proof is not None else None
        deletion_record = None
        mutation_started = False
        transaction_outcome = SportsTransactionOutcome()
        try:
            with sports_file_publication(
                context, signature, cancel, outcome=transaction_outcome
            ) as (
                session,
                validate,
            ):
                current = session.execute(
                    select(TableHistorySports)
                    .where(
                        TableHistorySports.id == history_id,
                        TableHistorySports.arr_instance_id == arr_instance_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one_or_none()
                if current is None or current.to_dict() != record:
                    raise ValueError("Sports history changed before exclusion")
                exists = session.execute(
                    select(TableBlacklistSports.id).where(
                        TableBlacklistSports.arr_instance_id == arr_instance_id,
                        TableBlacklistSports.provider == row.provider,
                        TableBlacklistSports.subs_id == row.subs_id,
                    )
                ).first()
                if not exists:
                    session.add(
                        TableBlacklistSports(
                            event_id=context.event_id,
                            league_id=context.league_id,
                            arr_instance_id=arr_instance_id,
                            provider=row.provider,
                            subs_id=row.subs_id,
                            language=row.language,
                        )
                    )
                if proof is not None:
                    validate_artifact_stat(proof, path)
                    validate()
                    with subtitle_mutation(context.mapped_path, path):
                        mutation_started = True
                        os.remove(path)
                    status = "deleted"
                    result = SimpleNamespace(
                        message="Deleted blacklisted sports subtitle",
                        path=context.original_path,
                        language_code=row.language,
                        provider=row.provider,
                        score=row.score,
                        subs_id=row.subs_id,
                        subs_path=row.subtitles_path,
                        matched=[],
                        not_matched=[],
                    )
                    deleted = sports_history(session, context, result, action=0)
                    session.flush()
                    deletion_record = deleted.to_dict()
        except BaseException:
            if backup is not None:
                committed = (
                    _deletion_committed(deletion_record, transaction_outcome)
                    if mutation_started
                    else False
                )
                if committed is None:
                    raise OSError(
                        f"Sports deletion commit outcome is uncertain; recovery file retained at {backup}"
                    ) from None
                if committed:
                    os.unlink(backup)
                    _refresh_after_deletion(context)
                elif mutation_started and not os.path.lexists(path):
                    _restore_recovery(path, backup)
                elif mutation_started:
                    # An unlink may succeed and then raise. Preserve a subsequently installed path.
                    _restore_recovery(path, backup)
                else:
                    os.unlink(backup)
            raise
        if backup is not None:
            try:
                os.unlink(backup)
            finally:
                _refresh_after_deletion(context)
        if status != "preserved":
            check_cancelled(cancel)
            replacement_state = capture_replacement_state(context, signature)
    notify([context.event_id])
    if status == "preserved":
        replacement = {
            "status": "skipped",
            "message": "File preserved because its current artifact could not be proven",
            "downloads": 0,
        }
    else:
        replacement = search_event(
            context.event_id,
            arr_instance_id,
            language=row.language,
            cancel=cancel,
            job_id=job_id,
            replacement_state=replacement_state,
        )
    return {
        "file_status": status,
        "message": "Release excluded for this Sportarr instance",
        "replacement": replacement,
    }
