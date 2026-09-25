"""Wanted, league and upgrade jobs with local event IDs and exact owners."""

import ast
import datetime
import logging
import time

from sqlalchemy import select, and_, or_, func, false, literal_column

from app.config import settings
from app.database import (
    database,
    TableArrInstances,
    TableHistorySports,
    TableSportsEvents,
    TableSportsLeagues,
    TableLanguagesProfiles,
)
from app.jobs_queue import jobs_queue, JobCancelled
from sportarr.automatic import eligibility, search_event
from sportarr.connection import check_cancelled
from sportarr.history import blacklist_history
from sportarr.identity import resolve_event_in_session
from sportarr.library import _event_query, _serialize_event, get_league, parse_stored_list
from sportarr.pagination import validate_page
from sportarr.sync.leagues import require_sportarr
from sportarr.errors import SportsNotFound


class SportsJobSignal:
    # How long an "is this instance still enabled" answer stays good for.
    # is_set() is called from tight wait loops (the ffprobe wait polls it, the
    # provider search wait polls it), and each call used to issue a SELECT plus
    # a job-progress update. A 60-second ffprobe cost roughly twelve thousand
    # of each, which is real contention against request threads on SQLite. An
    # instance disabled a second ago is still worth stopping for; a second of
    # staleness against a 120-second analysis is not worth the round trips.
    OWNER_CACHE_SECONDS = 1.0

    def __init__(self, owner, job_id=None, parent=None):
        self.owner, self.job_id, self.parent = owner, job_id, parent
        self._owner_checked_at = None
        self._owner_enabled = True

    def _owner_is_enabled(self):
        now = time.monotonic()
        if (self._owner_checked_at is not None
                and now - self._owner_checked_at < self.OWNER_CACHE_SECONDS):
            return self._owner_enabled
        enabled = database.execute(
            select(TableArrInstances.enabled).where(
                TableArrInstances.id == self.owner, TableArrInstances.kind == "sportarr"
            )
        ).scalar_one_or_none()
        self._owner_enabled = enabled == 1
        self._owner_checked_at = now
        return self._owner_enabled

    def is_set(self):
        if self.job_id:
            jobs_queue.update_job_progress(self.job_id)
        if self.parent is not None and self.parent.is_set():
            return True
        return not self._owner_is_enabled()

    def wait(self, timeout):
        if self.parent is not None:
            self.parent.wait(timeout)
        else:
            time.sleep(timeout)
        return self.is_set()


def require_sports_enabled():
    """Refuse new sports work while Use Sportarr is off.

    Turning the switch off leaves the instance rows enabled, and
    require_sportarr() only reads the row, because Local Hub lookup keeps
    serving enabled owners with the switch off. The scheduler, the event
    streams and the webhook already stop at the switch; this is the same stop
    for the searches and syncs the Sports pages queue.
    """
    if not settings.general.use_sportarr:
        raise ValueError("Sportarr is turned off. Turn on Use Sportarr in Settings first.")


def _wanted_query(session, arr_instance_id=None, league_id=None):
    """Compile owner/league rules once, then count and page event rows in SQL."""
    from sportarr.settings import get_sports_settings

    if arr_instance_id is not None:
        require_sportarr(session, arr_instance_id)
    if league_id is not None and get_league(session, league_id, arr_instance_id) is None:
        raise SportsNotFound("Sports league not found for this owner")
    owners_query = select(TableArrInstances).where(TableArrInstances.kind == "sportarr",
                                                   TableArrInstances.enabled == 1)
    if arr_instance_id is not None:
        owners_query = owners_query.where(TableArrInstances.id == arr_instance_id)
    owners = {row.id: get_sports_settings(row) for row in session.execute(
        owners_query.execution_options(populate_existing=True)).scalars()}
    # Tags are stored as Python list literals, including quote escaping. Parse
    # only the league dimension, never every missing file or substring matches.
    excluded = {owner: [] for owner in owners}
    tag_owners = [owner for owner, options in owners.items() if options['excluded_tags']]
    if tag_owners:
        for owner, local_id, tags in session.execute(select(
                TableSportsLeagues.arr_instance_id, TableSportsLeagues.id, TableSportsLeagues.tags)
                .where(TableSportsLeagues.arr_instance_id.in_(tag_owners))):
            if set(str(tag) for tag in ast.literal_eval(tags or "[]")) & set(owners[owner]['excluded_tags']):
                excluded[owner].append(local_id)
    conditions = []
    for owner, options in owners.items():
        condition = TableSportsEvents.arr_instance_id == owner
        if options['only_monitored']:
            condition = and_(condition, TableSportsEvents.monitored == "True", TableSportsLeagues.monitored == "True")
        if options['excluded_sports']:
            condition = and_(condition, or_(TableSportsLeagues.sport.is_(None),
                                            TableSportsLeagues.sport.not_in(options['excluded_sports'])))
        if excluded[owner]:
            # IDs come from typed database columns. Literal integers avoid a
            # database parameter ceiling for owners with many excluded leagues.
            condition = and_(condition, TableSportsLeagues.id.not_in(
                [literal_column(str(int(local_id))) for local_id in excluded[owner]]))
        conditions.append(condition)
    query = _event_query(arr_instance_id).where(
        or_(*conditions) if conditions else false(),
        TableSportsEvents.missing_subtitles.is_not(None), TableSportsEvents.missing_subtitles != "[]",
        TableSportsLeagues.profileId != 0,
        TableSportsLeagues.profileId.in_(select(TableLanguagesProfiles.profileId)),
    )
    return query.where(TableSportsEvents.league_id == league_id) if league_id is not None else query


def wanted_rows(session, arr_instance_id=None, league_id=None):
    query = _wanted_query(session, arr_instance_id, league_id)
    return [_serialize_event(row) for row in session.execute(query.order_by(TableSportsEvents.id))]


def list_wanted(session, arr_instance_id=None, start=0, length=100):
    limit = validate_page(start, length)
    query = _wanted_query(session, arr_instance_id)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    page = [_serialize_event(row) for row in session.execute(
        query.order_by(TableSportsEvents.id).offset(start).limit(limit))]
    from subtitles.mismatch import flagged_media_ids

    mismatched = flagged_media_ids(session, "sports", [row["id"] for row in page])
    for row in page:
        row["release_mismatch"] = row["id"] in mismatched
    return {"data": page, "total": total}


def wanted_badge(session):
    query = _wanted_query(session).with_only_columns(TableSportsEvents.missing_subtitles)
    return sum(len(parse_stored_list(missing)) for missing in session.execute(query).scalars())


def _run_events(rows, job_id, adaptive=False, language=None):
    outcomes = []
    failures = []
    from sportarr.notify import rescan_batch

    jobs_queue.update_job_progress(job_id, progress_max=len(rows) or 1)
    try:
        # One whole-library Sportarr rescan per affected owner per operation:
        # every published subtitle asks for one, and the batch merges them.
        with rescan_batch():
            for position, row in enumerate(rows, 1):
                signal = SportsJobSignal(row["arr_instance_id"], job_id)
                try:
                    check_cancelled(signal)
                    outcome = search_event(
                        row["id"],
                        row["arr_instance_id"],
                        job_id=job_id,
                        cancel=signal,
                        adaptive=adaptive,
                        # A per-row language when the caller asked for one, so a
                        # single missing-language badge can be actioned on its own
                        # the way PATCH episodes/subtitles does for an episode.
                        language=row.get("language", language),
                    )
                    outcomes.append(
                        {"event_id": row["id"], "arr_instance_id": row["arr_instance_id"]}
                        | outcome
                    )
                    if outcome.get("cancelled"):
                        break
                except JobCancelled:
                    raise
                except Exception:
                    if signal.is_set():
                        raise JobCancelled("Sportarr owner stopped") from None
                    failures.append(row["id"])
                    logging.exception(
                        "Sports subtitle search failed for event %s, owner %s",
                        row["id"],
                        row["arr_instance_id"],
                    )
                jobs_queue.update_job_progress(job_id, progress_value=position)
    except JobCancelled:
        if not any(item["downloads"] for item in outcomes):
            raise
    published = any(item["downloads"] for item in outcomes)
    message = (
        outcomes[0]["message"]
        if len(outcomes) == 1
        else f"Searched {len(outcomes) + len(failures)} sports files; {sum(item['downloads'] for item in outcomes)} subtitle(s) downloaded"
    )
    warnings = [
        item["message"]
        for item in outcomes
        if item["status"] == "published_with_warnings"
    ]
    if warnings and len(outcomes) != 1:
        message += "; " + " ".join(warnings)
    if failures:
        message = (
            message + f"; {len(failures)} other sports searches failed"
            if published
            else f"{len(failures)} sports searches failed; see the log"
        )
    jobs_queue.update_job_progress(
        job_id,
        progress_value="max",
        progress_message=message,
        allow_cancelled=published,
    )
    if failures and not published:
        raise OSError(message)
    if failures:
        return {"message": message, "data": outcomes, "failed_events": failures}
    return {"message": message, "data": outcomes}


def automatic_search_sports(event_id, arr_instance_id, job_id=None, language=None):
    """Search one event, optionally for a single language.

    Without ``language`` this searches every missing language on the event,
    which is all it could ever do. Episodes and movies have had a
    "download this one language now" path since forever (PATCH
    episodes/subtitles), so the sports Wanted page had to substitute a manual
    search modal for a single missing-language badge.
    """
    if not job_id:
        require_sports_enabled()
        resolve_event_in_session(database, event_id, arr_instance_id)
        return jobs_queue.add_job_from_function(
            "Searching sports subtitles", is_progress=True
        )
    return _run_events(
        [{"id": event_id, "arr_instance_id": arr_instance_id, "language": language}],
        job_id,
    )


def wanted_search_missing_subtitles_sports(
    job_id=None, wait_for_completion=False, arr_instance_id=None
):
    if not job_id:
        require_sports_enabled()
        if not wanted_rows(database, arr_instance_id):
            return False
        return jobs_queue.add_job_from_function(
            "Searching for missing sports subtitles",
            is_progress=True,
            wait_for_completion=wait_for_completion,
        )
    return _run_events(wanted_rows(database, arr_instance_id), job_id, adaptive=True)


def sports_download_subtitles(league_id, arr_instance_id, job_id=None):
    if not job_id:
        require_sports_enabled()
        if get_league(database, league_id, arr_instance_id) is None:
            raise SportsNotFound("Sports league not found for this owner")
        return jobs_queue.add_job_from_function(
            "Downloading missing league subtitles", is_progress=True
        )
    return _run_events(wanted_rows(database, arr_instance_id, league_id), job_id)


def blacklist_sports_subtitle(history_id, arr_instance_id, job_id=None):
    if not job_id:
        require_sports_enabled()
        require_sportarr(database, arr_instance_id)
        return jobs_queue.add_job_from_function(
            "Excluding sports subtitle and finding replacement", is_progress=True
        )
    signal = SportsJobSignal(arr_instance_id, job_id)
    try:
        result = blacklist_history(
            history_id, arr_instance_id, cancel=signal, job_id=job_id
        )
    except ValueError:
        if signal.is_set():
            raise JobCancelled("Sportarr owner stopped") from None
        raise
    jobs_queue.update_job_progress(
        job_id,
        progress_value="max",
        progress_message=result["message"] + ". " + result["replacement"]["message"],
        allow_cancelled=bool(result["replacement"]["downloads"]),
    )
    return result


def _latest_history_rows(session, rows):
    """The newest history row id for each (owner, event, language) in ``rows``.

    Recency is read the way upgrade_rows' ORDER BY does, including the corner
    where a row carries no timestamp: PostgreSQL sorts NULLs first under DESC,
    so such a row would be the one upgrade_rows sees and swallows. Treating it
    as the newest here keeps the flag on the conservative side on both
    backends, rather than promising an upgrade that would never run.
    """
    keys = {(row.arr_instance_id, row.event_id, row.language) for row in rows}
    if not keys:
        return {}
    candidates = session.execute(
        select(TableHistorySports.id, TableHistorySports.arr_instance_id,
               TableHistorySports.event_id, TableHistorySports.language,
               TableHistorySports.timestamp)
        .where(TableHistorySports.arr_instance_id.in_({key[0] for key in keys}),
               TableHistorySports.event_id.in_({key[1] for key in keys}))
    ).all()

    def recency(candidate):
        return (candidate.timestamp is None,
                candidate.timestamp or datetime.datetime.min,
                candidate.id)

    newest = {}
    for candidate in candidates:
        key = (candidate.arr_instance_id, candidate.event_id, candidate.language)
        if key not in keys:
            continue
        current = newest.get(key)
        if current is None or recency(candidate) > recency(current):
            newest[key] = candidate
    return {key: candidate.id for key, candidate in newest.items()}


def upgradable_history_ids(session, history_ids):
    """The subset of ``history_ids`` the upgrade run would consider, cheaply.

    Deliberately NOT upgrade_rows: that walks the whole history and hashes the
    subtitle file on disk for every candidate, which is right for the upgrade
    job and far too expensive for a history page request. This applies the
    criteria that need no IO, which is what the flag is for, and leaves the
    artifact verification where it belongs, in the run itself. The episodes
    endpoint has the same character: it marks a row upgradable and then
    re-checks the paths before acting.
    """
    from subtitles.upgrade import (
        get_queries_condition_parameters,
        _language_still_desired,
        _ai_translated_subtitles_are_upgrade_candidates,
    )
    from sportarr.identity import resolve_event_in_session

    if not settings.general.upgrade_subs or not history_ids:
        return set()
    minimum_timestamp, actions = get_queries_condition_parameters()
    allow_high_score_ai = _ai_translated_subtitles_are_upgrade_candidates()
    rows = session.execute(
        select(TableHistorySports)
        .join(TableArrInstances,
              TableHistorySports.arr_instance_id == TableArrInstances.id)
        .where(TableArrInstances.kind == "sportarr", TableArrInstances.enabled == 1,
               TableHistorySports.id.in_(list(history_ids)))
    ).scalars().all()
    # Only the newest row of each (owner, event, language) can be upgraded:
    # upgrade_rows() walks history newest-first and marks the tuple seen before
    # it applies any eligibility filter, so it never reaches an older row.
    # Flagging one anyway put an upgrade indicator on the Sports History page
    # that the upgrade run would silently skip forever.
    latest = _latest_history_rows(session, rows)
    upgradable = set()
    for row in rows:
        if latest.get((row.arr_instance_id, row.event_id, row.language)) != row.id:
            continue
        if (
            row.action not in actions
            or not row.timestamp
            or row.timestamp <= minimum_timestamp
            or (row.score is None and row.action != 6)
            or (
                (row.score or 0) >= (row.score_out_of or 180) - 3
                and not (allow_high_score_ai and row.ai_translated is True)
            )
            or not row.artifact
        ):
            continue
        try:
            context = resolve_event_in_session(session, row.event_id, row.arr_instance_id)
        except ValueError:
            continue
        if eligibility(session, context) or not _language_still_desired(
            row.language, context.profile_id
        ):
            continue
        upgradable.add(row.id)
    return upgradable


def upgrade_rows(session, arr_instance_id=None, job_id=None, *, event_ids=None, league_ids=None):
    from subtitles.upgrade import (
        get_queries_condition_parameters,
        _language_still_desired,
        _ai_translated_subtitles_are_upgrade_candidates,
    )
    from sportarr.artifacts import capture_artifact
    from sportarr.subtitles import candidate_signature
    from utilities.path_mappings import apply_sports_mapping, read_sports_mappings

    if not settings.general.upgrade_subs:
        return []
    allow_high_score_ai = _ai_translated_subtitles_are_upgrade_candidates()
    if arr_instance_id is not None:
        require_sportarr(session, arr_instance_id)
    minimum_timestamp, actions = get_queries_condition_parameters()
    query = (
        select(TableHistorySports)
        .join(
            TableArrInstances,
            TableHistorySports.arr_instance_id == TableArrInstances.id,
        )
        .where(TableArrInstances.kind == "sportarr", TableArrInstances.enabled == 1)
        .order_by(TableHistorySports.timestamp.desc(), TableHistorySports.id.desc())
    )
    if arr_instance_id is not None:
        query = query.where(TableHistorySports.arr_instance_id == arr_instance_id)
    if event_ids is not None or league_ids is not None:
        selected_events = select(TableSportsEvents.id).where(
            (TableSportsEvents.id.in_(event_ids or [])) |
            (TableSportsEvents.league_id.in_(league_ids or []))
        )
        if arr_instance_id is not None:
            selected_events = selected_events.where(TableSportsEvents.arr_instance_id == arr_instance_id)
        query = query.where(TableHistorySports.event_id.in_(selected_events))
    seen, rows = set(), []
    for row in session.execute(query).scalars():
        check_cancelled(SportsJobSignal(row.arr_instance_id, job_id))
        key = (row.arr_instance_id, row.event_id, row.language)
        if key in seen:
            continue
        seen.add(key)
        if (
            row.action not in actions
            or not row.timestamp
            or row.timestamp <= minimum_timestamp
            or (row.score is None and row.action != 6)
            or (
                (row.score or 0) >= (row.score_out_of or 180) - 3
                and not (allow_high_score_ai and row.ai_translated is True)
            )
            or not row.artifact
        ):
            continue
        context = resolve_event_in_session(session, row.event_id, row.arr_instance_id)
        if eligibility(session, context) or not _language_still_desired(
            row.language, context.profile_id
        ):
            continue
        instance = require_sportarr(session, row.arr_instance_id)
        path = apply_sports_mapping(
            row.subtitles_path, read_sports_mappings(instance.path_mappings)
        )
        try:
            if (
                capture_artifact(
                    context,
                    path,
                    candidate_signature(context),
                    SportsJobSignal(row.arr_instance_id, job_id),
                )
                != row.artifact
            ):
                continue
        except (OSError, ValueError):
            continue
        rows.append(row.to_dict() | {"mapped_subtitles_path": path})
    return rows


def upgrade_sports_subtitles(
    job_id=None, wait_for_completion=False, arr_instance_id=None, event_ids=None, league_ids=None
):
    if not job_id:
        require_sports_enabled()
        if not settings.general.upgrade_subs:
            return False
        if arr_instance_id is not None:
            require_sportarr(database, arr_instance_id)
        return jobs_queue.add_job_from_function(
            "Upgrading sports subtitles",
            is_progress=True,
            wait_for_completion=wait_for_completion,
        )
    rows = upgrade_rows(database, arr_instance_id, job_id, event_ids=event_ids, league_ids=league_ids)
    outcomes = []
    from sportarr.notify import rescan_batch

    jobs_queue.update_job_progress(job_id, progress_max=len(rows) or 1)
    try:
        # One whole-library Sportarr rescan per affected owner per operation,
        # merged the same way the wanted download runs merge theirs.
        with rescan_batch():
            for position, row in enumerate(rows, 1):
                signal = SportsJobSignal(row["arr_instance_id"], job_id)
                try:
                    outcomes.append(
                        search_event(
                            row["event_id"],
                            row["arr_instance_id"],
                            language=row["language"],
                            minimum_score=(row["score"] or 0) + 1,
                            upgraded_from_id=row["upgradedFromId"] or row["id"],
                            job_id=job_id,
                            cancel=signal,
                            previous_artifact=(
                                row["mapped_subtitles_path"],
                                row["artifact"],
                            ),
                        )
                    )
                except ValueError:
                    if signal.is_set():
                        raise JobCancelled("Sportarr owner stopped") from None
                    raise
                if outcomes[-1].get("cancelled"):
                    break
                jobs_queue.update_job_progress(job_id, progress_value=position)
    except JobCancelled:
        if not any(item["downloads"] for item in outcomes):
            raise
    message = f"Checked {len(outcomes)} sports upgrades; {sum(item['downloads'] for item in outcomes)} published"
    warnings = [
        item["message"]
        for item in outcomes
        if item["status"] == "published_with_warnings"
    ]
    if warnings:
        message += "; " + " ".join(warnings)
    jobs_queue.update_job_progress(
        job_id,
        progress_value="max",
        progress_message=message,
        allow_cancelled=any(item["downloads"] for item in outcomes),
    )
    return {"message": message, "data": outcomes}


def search_after_sync(session, event_ids, arr_instance_id, cancel=None):
    from sportarr.settings import get_sports_settings

    check_cancelled(cancel)
    if not get_sports_settings(require_sportarr(session, arr_instance_id))[
        "search_on_sync"
    ]:
        return []
    queued = []
    for event_id in event_ids:
        check_cancelled(cancel)
        context = resolve_event_in_session(session, event_id, arr_instance_id)
        row = session.get(TableSportsEvents, event_id, populate_existing=True)
        if not eligibility(session, context) and ast.literal_eval(
            row.missing_subtitles or "[]"
        ):
            queued.append(
                jobs_queue.feed_jobs_pending_queue(
                    job_name="Searching sports subtitles after sync",
                    module="sportarr.workflows",
                    func="automatic_search_sports",
                    kwargs={"event_id": event_id, "arr_instance_id": arr_instance_id},
                    is_progress=True,
                )
            )
    return queued


SPORTS_JOB_FUNCTIONS = frozenset(
    {
        "automatic_search_sports",
        "sports_download_subtitles",
        "wanted_search_missing_subtitles_sports",
        "upgrade_sports_subtitles",
        "blacklist_sports_subtitle",
    }
)

# The library sync jobs are queued by the sports API from their own modules
# (sportarr.sync.leagues / sportarr.sync.events), not from this one, because
# they are the scheduled sync entry points rather than per-subtitle workflows.
# They still carry a job_id the client polls through the same
# /sports/jobs/<id> endpoint, so a predicate that only knew about this module
# answered 404 for a job that had just been queued, and the client retried it
# every second forever. The function is pinned to its module so a job cannot
# pair a known function name with an unrelated module.
SPORTS_SYNC_JOB_MODULES = {
    "sportarr.sync.leagues": frozenset({"update_sports_for_instance"}),
    "sportarr.sync.events": frozenset({"sync_one_league"}),
    # A manual download publishes like any other sports job, so switching
    # Sportarr off has to remove or stop it too.
    "sportarr.manual_jobs": frozenset({"sports_manually_download_subtitle"}),
}


def is_sports_job(job):
    module, func = job.get("module"), job.get("func")
    if module == "sportarr.workflows":
        return func in SPORTS_JOB_FUNCTIONS
    allowed = SPORTS_SYNC_JOB_MODULES.get(module)
    return allowed is not None and func in allowed


def sports_job_status(session, job_id, arr_instance_id):
    require_sportarr(session, arr_instance_id)
    rows = jobs_queue.list_jobs_from_queue(job_id=job_id)
    if not rows:
        return None
    job = rows[0]
    if (
        not is_sports_job(job)
        or job["kwargs"].get("arr_instance_id") != arr_instance_id
    ):
        return None

    def outcome(value):
        if not isinstance(value, dict):
            return None
        result = {
            key: value[key]
            for key, expected in (
                ("status", str),
                ("message", str),
                ("downloads", int),
                ("event_id", int),
                ("arr_instance_id", int),
                ("cancelled", bool),
            )
            if key in value and type(value[key]) is expected
        }
        publication = value.get("publication")
        if isinstance(publication, dict):
            result["publication"] = {
                key: publication[key]
                for key, expected in (
                    ("published", bool),
                    ("status", str),
                    ("processing", str),
                    ("artifact", str),
                    ("history", str),
                    ("index", str),
                    ("refresh_attempts", int),
                    ("refresh_queued", bool),
                    ("failed_phase", (str, type(None))),
                    ("cancelled", bool),
                    ("message", str),
                )
                if key in publication and isinstance(publication[key], expected)
            }
        return result

    result = job.get("job_returned_value")
    projected = None
    if isinstance(result, dict):
        projected = {}
        if isinstance(result.get("message"), str):
            projected["message"] = result["message"]
        if result.get("file_status") in ("deleted", "absent", "preserved"):
            projected["file_status"] = result["file_status"]
            projected["replacement"] = outcome(result.get("replacement"))
        if isinstance(result.get("data"), list):
            projected["data"] = [
                outcome(item) for item in result["data"] if isinstance(item, dict)
            ]
    return {
        "job_id": job_id,
        "arr_instance_id": arr_instance_id,
        "status": job["status"],
        "cancelled": bool(job.get("cancelled")),
        "message": job["progress_message"],
        "result": projected,
    }


def cancel_disabled_jobs(enabled_owners):
    for job in jobs_queue.list_jobs_from_queue():
        if (
            is_sports_job(job)
            and job["kwargs"].get("arr_instance_id") is not None
            and job["kwargs"]["arr_instance_id"] not in enabled_owners
        ):
            if job["status"] == "pending":
                jobs_queue.remove_job_from_pending_queue(job["job_id"])
            elif job["status"] == "running":
                jobs_queue.cancel_running_job(job["job_id"])
