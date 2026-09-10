"""Wanted, league and upgrade jobs with local event IDs and exact owners."""

import ast
import logging
import time

from sqlalchemy import select

from app.config import settings
from app.database import (
    database,
    TableArrInstances,
    TableHistorySports,
    TableSportsEvents,
)
from app.jobs_queue import jobs_queue, JobCancelled
from sportarr.automatic import eligibility, search_event
from sportarr.connection import check_cancelled
from sportarr.history import blacklist_history
from sportarr.identity import resolve_event_in_session
from sportarr.library import _event_query, _serialize_event, get_league
from sportarr.pagination import validate_page
from sportarr.sync.leagues import require_sportarr


class SportsJobSignal:
    def __init__(self, owner, job_id=None, parent=None):
        self.owner, self.job_id, self.parent = owner, job_id, parent

    def is_set(self):
        if self.job_id:
            jobs_queue.update_job_progress(self.job_id)
        if self.parent is not None and self.parent.is_set():
            return True
        owner = database.execute(
            select(TableArrInstances.enabled).where(
                TableArrInstances.id == self.owner, TableArrInstances.kind == "sportarr"
            )
        ).scalar_one_or_none()
        return owner != 1

    def wait(self, timeout):
        if self.parent is not None:
            self.parent.wait(timeout)
        else:
            time.sleep(timeout)
        return self.is_set()


def wanted_rows(session, arr_instance_id=None, league_id=None):
    if arr_instance_id is not None:
        require_sportarr(session, arr_instance_id)
    if (
        league_id is not None
        and get_league(session, league_id, arr_instance_id) is None
    ):
        raise ValueError("Sports league not found for this owner")
    query = _event_query(arr_instance_id).where(
        TableSportsEvents.missing_subtitles.is_not(None),
        TableSportsEvents.missing_subtitles != "[]",
    )
    if league_id is not None:
        query = query.where(TableSportsEvents.league_id == league_id)
    result = []
    for row in session.execute(query.order_by(TableSportsEvents.id)):
        context = resolve_event_in_session(session, row[0].id, row[0].arr_instance_id)
        if not eligibility(session, context):
            result.append(_serialize_event(row))
    return result


def list_wanted(session, arr_instance_id=None, start=0, length=100):
    limit = validate_page(start, length)
    rows = wanted_rows(session, arr_instance_id)
    page = rows[start:] if limit is None else rows[start : start + limit]
    # The same flag the episodes and movies wanted endpoints carry, so a
    # "subtitles exist but only for another release" diagnosis reaches the
    # sports page instead of being recorded and never shown.
    from subtitles.mismatch import flagged_media_ids

    mismatched = flagged_media_ids(session, "sports", [row["id"] for row in page])
    for row in page:
        row["release_mismatch"] = row["id"] in mismatched
    return {"data": page, "total": len(rows)}


def wanted_badge(session):
    return sum(len(row["missing_subtitles"]) for row in wanted_rows(session))


def _run_events(rows, job_id, adaptive=False):
    outcomes = []
    failures = []
    jobs_queue.update_job_progress(job_id, progress_max=len(rows) or 1)
    try:
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


def automatic_search_sports(event_id, arr_instance_id, job_id=None):
    if not job_id:
        resolve_event_in_session(database, event_id, arr_instance_id)
        return jobs_queue.add_job_from_function(
            "Searching sports subtitles", is_progress=True
        )
    return _run_events([{"id": event_id, "arr_instance_id": arr_instance_id}], job_id)


def wanted_search_missing_subtitles_sports(
    job_id=None, wait_for_completion=False, arr_instance_id=None
):
    if not job_id:
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
        if get_league(database, league_id, arr_instance_id) is None:
            raise ValueError("Sports league not found for this owner")
        return jobs_queue.add_job_from_function(
            "Downloading missing league subtitles", is_progress=True
        )
    return _run_events(wanted_rows(database, arr_instance_id, league_id), job_id)


def blacklist_sports_subtitle(history_id, arr_instance_id, job_id=None):
    if not job_id:
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


def upgrade_rows(session, arr_instance_id=None, job_id=None):
    from subtitles.upgrade import (
        get_queries_condition_parameters,
        _language_still_desired,
    )
    from sportarr.artifacts import capture_artifact
    from sportarr.subtitles import candidate_signature
    from utilities.path_mappings import apply_sports_mapping, read_sports_mappings

    if not settings.general.upgrade_subs:
        return []
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
            or row.score is None
            or row.score >= (row.score_out_of or 180) - 3
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
    job_id=None, wait_for_completion=False, arr_instance_id=None
):
    if not job_id:
        if not settings.general.upgrade_subs:
            return False
        if arr_instance_id is not None:
            require_sportarr(database, arr_instance_id)
        return jobs_queue.add_job_from_function(
            "Upgrading sports subtitles",
            is_progress=True,
            wait_for_completion=wait_for_completion,
        )
    rows = upgrade_rows(database, arr_instance_id, job_id)
    outcomes = []
    jobs_queue.update_job_progress(job_id, progress_max=len(rows) or 1)
    try:
        for position, row in enumerate(rows, 1):
            signal = SportsJobSignal(row["arr_instance_id"], job_id)
            try:
                outcomes.append(
                    search_event(
                        row["event_id"],
                        row["arr_instance_id"],
                        language=row["language"],
                        minimum_score=row["score"] + 1,
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


def sports_job_status(session, job_id, arr_instance_id):
    require_sportarr(session, arr_instance_id)
    rows = jobs_queue.list_jobs_from_queue(job_id=job_id)
    if not rows:
        return None
    job = rows[0]
    if (
        job["module"] != "sportarr.workflows"
        or job["func"] not in SPORTS_JOB_FUNCTIONS
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
            job["module"] == "sportarr.workflows"
            and job["func"] in SPORTS_JOB_FUNCTIONS
            and job["kwargs"].get("arr_instance_id") is not None
            and job["kwargs"]["arr_instance_id"] not in enabled_owners
        ):
            if job["status"] == "pending":
                jobs_queue.remove_job_from_pending_queue(job["job_id"])
            elif job["status"] == "running":
                jobs_queue.cancel_running_job(job["job_id"])
