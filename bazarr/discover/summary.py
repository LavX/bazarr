"""Read-only bounded aggregate of local Bazarr work, shown beside global discovery.

Nothing in here writes. It never runs an indexer, never recalculates a language
profile, never prunes the throttle table and never triggers the health check,
because each of those mutates state that a status read has no business changing.

Every component answers separately and carries its own availability, so one
failed source leaves the others intact. An unavailable source reports unknown
with null counts. It never reports zero, because zero work and no answer are
different facts and only one of them is reassuring.

Query shape is fixed: one instance identity projection, one grouped requirement
aggregate and one uncomputed count per media kind, one bounded history read per
media kind, and one grouped inaccessible-root-folder read per media kind. The
grouped aggregates parse each distinct stored requirement list once and multiply
by its row count, so no query fans out per media item or per tile.
"""
from __future__ import annotations

import ast
import datetime as dt
import logging
import sys
import time
from threading import Lock

from sqlalchemy import and_, func, or_, select

logger = logging.getLogger(__name__)

# One statement per bounded read, with headroom for the connection preamble.
QUERY_BUDGET = 12

HISTORY_CANDIDATE_LIMIT = 25
ARRIVAL_DISPLAY_LIMIT = 4
ACTIVITY_SAMPLE_LIMIT = 5
ROOTFOLDER_GROUP_LIMIT = 10
WANTED_GROUP_BUDGET = 5000
WANTED_CHUNK = 500
WANTED_CACHE_SECONDS = 10
MAX_REQUIREMENT_BLOB = 4000
MAX_REQUIREMENT_TOKENS = 200

# Recorded acquisition events. 0 is a deletion, 5 a sync and 7 an embedded-state
# observation; 6 conflates a full translation with a partial one and is proven
# only by a live typed publication observation, never by the history row.
ARRIVAL_ACTIONS = (1, 2, 3, 4)

# Classify by the module and function a job actually runs. Matching words in a
# job name counts unrelated work: the translator status endpoint does that today
# and a "Translating" substring is not a contract.
OPERATIONS = {
    ("subtitles.tools.translate.main", "translate_subtitles_file"): "translation",
    ("subtitles.wanted.movies", "wanted_search_missing_subtitles_movies"): "wanted_search",
    ("subtitles.wanted.series", "wanted_search_missing_subtitles_series"): "wanted_search",
    ("subtitles.manual", "episode_manually_download_specific_subtitle"): "manual_download",
    ("subtitles.manual", "movie_manually_download_specific_subtitle"): "manual_download",
    ("subtitles.upgrade", "upgrade_subtitles"): "upgrade",
    ("subtitles.mass_operations", "mass_batch_operation"): "batch_operation",
    ("sonarr.sync.series", "update_series_for_instance"): "library_sync",
    ("sonarr.sync.episodes", "sync_episodes"): "library_sync",
    ("radarr.sync.movies", "update_movies_for_instance"): "library_sync",
    ("subtitles.indexer.series", "series_full_scan_subtitles"): "library_scan",
    ("subtitles.indexer.movies", "movies_full_scan_subtitles"): "library_scan",
}

# A synchronous execution path has no job name of its own, so it needs one that
# says what is happening rather than exposing an internal operation key.
OPERATION_LABELS = {
    "discover_search": "Searching subtitle providers",
    "manual_search": "Searching subtitle providers",
    "translation": "Translating subtitles",
    "manual_download": "Downloading a subtitle",
    "wanted_search": "Searching for missing subtitles",
}

_wanted_lock = Lock()
_wanted_cache = {"value": None, "expires": 0.0}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment):
    """UTC instant, or None. Used for observations this process timed itself."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_local(moment):
    """A stored naive timestamp, kept in the application's local convention.

    History rows are written with ``datetime.now()``, so they are local wall
    clock with no offset. Attaching the local offset preserves the instant the
    user saw without silently reinterpreting it as UTC.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return moment.isoformat()


def _component(availability="available", **fields):
    return {"availability": availability, "observed_at": None, "complete": True, **fields}


def _connection():
    from app.database import engine
    return engine.connect()


# ---------------------------------------------------------------- instances


def _instances():
    """Identity only. The stored status columns have no runtime writer."""
    from app.database import TableArrInstances
    with _connection() as connection:
        rows = connection.execute(select(
            TableArrInstances.id, TableArrInstances.kind, TableArrInstances.name,
            TableArrInstances.enabled, TableArrInstances.is_default,
        )).all()
    return {row.id: {"arr_instance_id": row.id, "kind": row.kind, "instance_name": row.name,
                     "enabled": bool(row.enabled), "is_default": bool(row.is_default)}
            for row in rows}


def _instance_name(instances, instance_id):
    entry = instances.get(instance_id)
    return entry["instance_name"] if entry else None


# ------------------------------------------------------------------ wanted


def _parse_requirements(blob):
    """Distinct requirement tokens in one stored list, or None when unreadable.

    The indexers store a Python list repr such as ``['en', 'hu:forced']``, so a
    modifier is a distinct requirement and a JSON cast would be wrong. A repeated
    identical token is corrupt data rather than extra demand, and says so.
    """
    if not isinstance(blob, str) or len(blob) > MAX_REQUIREMENT_BLOB:
        return None, "malformed_requirements"
    try:
        value = ast.literal_eval(blob)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None, "malformed_requirements"
    if not isinstance(value, list) or len(value) > MAX_REQUIREMENT_TOKENS:
        return None, "malformed_requirements"
    tokens = []
    for token in value:
        if not isinstance(token, str) or not token.strip() or len(token) > 60:
            return None, "malformed_requirements"
        tokens.append(token)
    distinct = set(tokens)
    return len(distinct), ("duplicate_requirement_tokens" if len(distinct) != len(tokens) else None)


def _wanted_predicate(kind):
    """The exact predicate the existing Wanted surface already uses."""
    from app.database import TableEpisodes, TableMovies, get_exclusion_clause
    table = TableEpisodes if kind == "episode" else TableMovies
    conditions = [table.missing_subtitles.is_not(None), table.missing_subtitles != "[]"]
    conditions += get_exclusion_clause("series" if kind == "episode" else "movie")
    return conditions


def _wanted_kind(connection, kind, qualifications):
    from app.database import TableEpisodes, TableMovies, TableShows
    table = TableEpisodes if kind == "episode" else TableMovies
    statement = select(table.arr_instance_id, table.missing_subtitles,
                       func.count().label("media_rows"))
    if kind == "episode":
        statement = statement.select_from(TableEpisodes).join(
            TableShows, TableEpisodes.series_id == TableShows.id)
    else:
        statement = statement.select_from(TableMovies)
    statement = (statement.where(and_(*_wanted_predicate(kind)))
                 .group_by(table.arr_instance_id, table.missing_subtitles))

    requirements = 0
    media = 0
    unknown_media = 0
    by_owner = {}
    groups = 0
    result = connection.execute(statement.execution_options(yield_per=WANTED_CHUNK))
    for chunk in result.partitions(WANTED_CHUNK):
        for owner, blob, rows in chunk:
            groups += 1
            if groups > WANTED_GROUP_BUDGET:
                result.close()
                raise _AggregateBudgetExceeded(kind)
            media += rows
            count, qualification = _parse_requirements(blob)
            if qualification:
                qualifications.add(qualification)
            if count is None:
                unknown_media += rows
                continue
            requirements += count * rows
            by_owner[owner] = by_owner.get(owner, 0) + count * rows

    # Eligible media whose requirement list has never been computed. The manual
    # provider API reindexes that case; a status read must not, so it is
    # reported as unknown rather than folded into the total.
    profile_owner = TableShows.profileId if kind == "episode" else TableMovies.profileId
    uncomputed = select(func.count()).select_from(table)
    if kind == "episode":
        uncomputed = uncomputed.join(TableShows, TableEpisodes.series_id == TableShows.id)
    conditions = [or_(table.missing_subtitles.is_(None), table.missing_subtitles == ""),
                  profile_owner.is_not(None)]
    from app.database import get_exclusion_clause
    conditions += get_exclusion_clause("series" if kind == "episode" else "movie")
    unknown_media += connection.execute(uncomputed.where(and_(*conditions))).scalar() or 0
    return requirements, media, unknown_media, by_owner


class _AggregateBudgetExceeded(RuntimeError):
    """The grouped requirement scan exceeded its fixed group budget."""


def _wanted_component(instances):
    qualifications = set()
    with _connection() as connection:
        episodes, episode_media, episode_unknown, episode_owners = _wanted_kind(
            connection, "episode", qualifications)
        movies, movie_media, movie_unknown, movie_owners = _wanted_kind(
            connection, "movie", qualifications)

    unknown_media = episode_unknown + movie_unknown
    if unknown_media:
        qualifications.add("uncomputed_media")
    owners = {}
    for source in (episode_owners, movie_owners):
        for owner, count in source.items():
            owners[owner] = owners.get(owner, 0) + count

    return {
        "availability": "available",
        "observed_at": _iso(_now()),
        "complete": not unknown_media and "malformed_requirements" not in qualifications,
        "requirements": episodes + movies,
        "episode_requirements": episodes,
        "movie_requirements": movies,
        "media_count": episode_media + movie_media,
        "unknown_media_count": unknown_media,
        "qualifications": sorted(qualifications),
        "by_instance": [{"arr_instance_id": owner,
                         "instance_name": _instance_name(instances, owner),
                         "requirements": count}
                        for owner, count in sorted(owners.items(), key=lambda item: (item[0] is None, item[0]))],
    }


def _unknown_wanted():
    return {
        "availability": "unknown", "observed_at": None, "complete": False,
        "requirements": None, "episode_requirements": None, "movie_requirements": None,
        "media_count": None, "unknown_media_count": None,
        "qualifications": ["source_unavailable"], "by_instance": [],
    }


def _cached_wanted(instances):
    """Server-wide aggregate, cached briefly and refreshed under one lock.

    The database still examines every nonempty requirement row, because no
    maintained requirement count exists. Caching keeps that off the per-request
    and per-browser path; it is not a claim that the scan is constant time.
    """
    now = time.monotonic()
    with _wanted_lock:
        cached = _wanted_cache["value"]
        if cached is not None and _wanted_cache["expires"] > now:
            return dict(cached)
        try:
            component = _wanted_component(instances)
        except _AggregateBudgetExceeded:
            # Same treatment as any other failed read below: a transient breach
            # on a large library must not throw away a good previous reading.
            if cached is not None:
                return dict(cached, availability="stale",
                            qualifications=sorted({*cached["qualifications"],
                                                   "group_budget_exceeded"}))
            component = dict(_unknown_wanted(), qualifications=["group_budget_exceeded"])
        except Exception:
            logger.exception("Discover summary could not read outstanding subtitle requirements")
            if cached is not None:
                return dict(cached, availability="stale")
            component = _unknown_wanted()
        _wanted_cache["value"] = component
        _wanted_cache["expires"] = time.monotonic() + WANTED_CACHE_SECONDS
        return dict(component)


def reset_cache():
    with _wanted_lock:
        _wanted_cache["value"] = None
        _wanted_cache["expires"] = 0.0


# ---------------------------------------------------------------- activity


def _schedules():
    """Recurring schedules, only when this process actually has a scheduler."""
    module = sys.modules.get("app.scheduler")
    scheduler = getattr(module, "scheduler", None) if module else None
    if scheduler is None:
        return None
    return {"tasks": list(scheduler.get_task_list()),
            "running": list(scheduler.get_running_tasks())}


def _scheduler_task(run_id):
    parts = (run_id or "").split(":schedule:")
    return parts[1].rsplit(":", 1)[0] if len(parts) == 2 else None


def _queue_progress(sample):
    if not sample["is_progress"]:
        return None
    value, total = sample["progress_value"], sample["progress_max"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0 or value > total:
        return None
    return {"unit": "item", "value": value, "total": total}


def _scoped_owner(observation):
    """The owning instance, or None for work that spans the whole server.

    A server-wide scan touches every instance in turn, so an owner recorded
    against it would only ever name whichever one it reached last.
    """
    if observation.get("scope_kind") == "server":
        return None
    return observation.get("arr_instance_id")


def _activity_item(sample, observation, state, instances):
    observation = observation or {}
    remote = observation.get("remote")
    waiting = bool(remote) and remote.get("phase") in ("queued", "submitted")
    phase = "queued" if state == "queued" else "waiting_for_service" if waiting else "running"
    progress = None
    if state == "running" and not waiting:
        progress = observation.get("progress") or _queue_progress(sample)
    owner = _scoped_owner(observation)
    return {
        "activity_id": sample["activity_id"],
        "operation": OPERATIONS.get((sample["module"], sample["func"]), "other"),
        "state": state,
        "phase": phase,
        "name": sample["job_name"],
        "scope_kind": observation.get("scope_kind", "unknown"),
        "arr_instance_id": owner,
        "instance_name": _instance_name(instances, owner),
        "media_type": observation.get("media_type"),
        "title": observation.get("title"),
        "season": observation.get("season"),
        "episode": observation.get("episode"),
        "episode_title": observation.get("episode_title"),
        "language": observation.get("language"),
        "progress": progress,
        "remote": remote,
        "parent_activity_id": observation.get("parent_activity_id"),
        "scheduler_run_id": sample["scheduler_run_id"] or observation.get("scheduler_run_id"),
        # Queue observations are recorded as aware UTC by this process.
        "observed_at": _iso(sample["updated_at"] or sample["started_at"] or sample["created_at"]),
    }


def _detached_item(observation, instances):
    """A running execution path that never enters the jobs queue."""
    owner = _scoped_owner(observation)
    remote = observation.get("remote")
    waiting = bool(remote) and remote.get("phase") in ("queued", "submitted")
    operation = observation.get("operation", "other")
    label = OPERATION_LABELS.get(operation, "Local work")
    title = observation.get("title")
    return {
        "activity_id": observation["activity_id"],
        "operation": operation,
        "state": "running",
        "phase": "waiting_for_service" if waiting else "running",
        "name": f"{label}: {title}" if title else label,
        "scope_kind": observation.get("scope_kind", "unknown"),
        "arr_instance_id": owner,
        "instance_name": _instance_name(instances, owner),
        "media_type": observation.get("media_type"),
        "title": observation.get("title"),
        "season": observation.get("season"),
        "episode": observation.get("episode"),
        "episode_title": observation.get("episode_title"),
        "language": observation.get("language"),
        "progress": None if waiting else observation.get("progress"),
        "remote": remote,
        "parent_activity_id": observation.get("parent_activity_id"),
        "scheduler_run_id": observation.get("scheduler_run_id"),
        "observed_at": observation.get("updated_at"),
    }


def _activity_component(instances):
    from app import activity
    from app.jobs_queue import jobs_queue

    queue = jobs_queue.snapshot_activity(limit_per_state=ACTIVITY_SAMPLE_LIMIT)
    observations = {item["activity_id"]: item for item in activity.snapshot()}

    running = [_activity_item(sample, observations.get(sample["activity_id"]), "running", instances)
               for sample in queue["samples"]["running"]]
    # A reserved job sits on the running deque with a pending status, one step
    # before it starts. It is still queued work, not work in progress.
    queued = [_activity_item(sample, observations.get(sample["activity_id"]), "queued", instances)
              for sample in queue["samples"]["pending"] + queue["samples"]["reserved"]]

    # An observation that already has a queue job is the same work seen twice.
    claimed = {item["activity_id"] for item in running + queued}
    running += [_detached_item(observation, instances)
                for identity, observation in observations.items()
                if identity not in claimed and observation.get("state") == "running"
                and not identity.startswith(f"{activity.PROCESS_RUN_ID}:job:")]

    schedules = _schedules()
    scheduled = []
    scheduled_count = None
    if schedules is not None:
        # A scheduled wrapper that enqueued a child and is waiting on it is that
        # child, not a second search. Prefer the child and list only schedules
        # that are not currently executing.
        child_tasks = {_scheduler_task(item["scheduler_run_id"]) for item in running + queued}
        # A truncated sample cannot prove a running task has no child here, so
        # no scheduled task is promoted to running work in that case.
        bounded = not (queue["truncated"]["running"] or queue["truncated"]["pending"]
                       or queue["truncated"]["reserved"])
        for task in schedules["tasks"]:
            if task.get("job_running") or task["job_id"] in schedules["running"]:
                if task["job_id"] in child_tasks or not bounded:
                    continue
                running.append({
                    "activity_id": f"schedule:{task['job_id']}",
                    "operation": "scheduled_task", "state": "running", "phase": "running",
                    "name": task.get("name") or task["job_id"], "scope_kind": "server",
                    "arr_instance_id": None, "instance_name": None, "media_type": None,
                    "title": None, "season": None, "episode": None, "episode_title": None,
                    "language": None, "progress": None, "remote": None,
                    "parent_activity_id": None, "scheduler_run_id": None,
                    "observed_at": _iso(queue["observed_at"]),
                })
                continue
            scheduled.append({"job_id": task["job_id"], "name": task.get("name") or task["job_id"],
                              "interval": task.get("interval"),
                              "next_run_in": task.get("next_run_in")})
        scheduled_count = len(scheduled)

    return {
        "availability": "available",
        "observed_at": _iso(queue["observed_at"]),
        "complete": schedules is not None,
        "truncated": any(queue["truncated"][state] for state in ("pending", "running", "reserved")),
        # The queue count is the whole-queue truth; ``running`` is a bounded
        # sample of it. Reporting the sample length here would have said "5
        # running" beside a truthful queued figure once the configured job
        # concurrency exceeded the sample limit.
        "running_count": queue["counts"]["running"] + len(running) - len(queue["samples"]["running"]),
        "queued_count": queue["counts"]["pending"] + queue["counts"]["reserved"],
        "scheduled_count": scheduled_count,
        "running": running,
        "queued": queued,
        "scheduled": scheduled,
        "unknown_sources": [] if schedules is not None else ["schedules"],
    }


def _unknown_activity():
    return {
        "availability": "unknown", "observed_at": None, "complete": False, "truncated": False,
        "running_count": None, "queued_count": None, "scheduled_count": None,
        "running": [], "queued": [], "scheduled": [], "unknown_sources": ["jobs"],
    }


# ---------------------------------------------------------------- arrivals


def _history_candidates(connection, kind, instances, qualifications):
    from app.database import (TableEpisodes, TableHistory, TableHistoryMovie, TableMovies,
                              TableShows)
    if kind == "episode":
        statement = (select(TableHistory.id, TableHistory.action, TableHistory.language,
                            TableHistory.provider, TableHistory.timestamp,
                            TableHistory.arr_instance_id,
                            TableEpisodes.arr_instance_id.label("media_owner"),
                            TableEpisodes.season, TableEpisodes.episode,
                            TableEpisodes.title.label("episode_title"),
                            TableShows.id.label("library_id"),
                            TableShows.title.label("show_title"),
                            TableShows.poster.label("poster_url"))
                     .select_from(TableHistory)
                     .outerjoin(TableEpisodes, TableHistory.episode_id == TableEpisodes.id)
                     .outerjoin(TableShows, TableHistory.series_id == TableShows.id)
                     .where(and_(TableHistory.action != 7,
                                 TableHistory.action.in_(ARRIVAL_ACTIONS)))
                     .order_by(TableHistory.timestamp.desc(), TableHistory.id.desc())
                     .limit(HISTORY_CANDIDATE_LIMIT))
    else:
        statement = (select(TableHistoryMovie.id, TableHistoryMovie.action,
                            TableHistoryMovie.language, TableHistoryMovie.provider,
                            TableHistoryMovie.timestamp, TableHistoryMovie.arr_instance_id,
                            TableMovies.arr_instance_id.label("media_owner"),
                            TableMovies.id.label("library_id"),
                            TableMovies.title.label("movie_title"),
                            TableMovies.poster.label("poster_url"))
                     .select_from(TableHistoryMovie)
                     .outerjoin(TableMovies, TableHistoryMovie.movie_id == TableMovies.id)
                     .where(and_(TableHistoryMovie.action != 7,
                                 TableHistoryMovie.action.in_(ARRIVAL_ACTIONS)))
                     .order_by(TableHistoryMovie.timestamp.desc(), TableHistoryMovie.id.desc())
                     .limit(HISTORY_CANDIDATE_LIMIT))

    arrivals = []
    for row in connection.execute(statement).all():
        title = row.show_title if kind == "episode" else row.movie_title
        # Both joins are outer, so a surviving show with a deleted episode would
        # otherwise be published as an exact arrival with a null season, episode
        # and episode title. An arrival either carries its exact identity or it
        # is not an arrival.
        incomplete = kind == "episode" and (
            row.season is None or row.episode is None or row.episode_title is None)
        if title is None or incomplete:
            # Deleted or unresolvable media cannot supply an exact title, and
            # nothing may be reconstructed from a filename.
            qualifications.add("unresolvable_media")
            continue
        if row.arr_instance_id is not None and row.media_owner is not None \
                and row.arr_instance_id != row.media_owner:
            # Historic misattribution is not repairable by inference here.
            qualifications.add("owner_mismatch")
            continue
        owner = row.arr_instance_id if row.arr_instance_id is not None else row.media_owner
        arrivals.append({
            "kind": kind, "event_id": f"{kind}:{row.id}", "status": "success",
            "action": row.action, "title": title, "poster_url": row.poster_url,
            "library_id": row.library_id,
            "season": row.season if kind == "episode" else None,
            "episode": row.episode if kind == "episode" else None,
            "episode_title": row.episode_title if kind == "episode" else None,
            "language": row.language, "provider": row.provider,
            "arr_instance_id": owner, "instance_name": _instance_name(instances, owner),
            # Sorted on one convention: history rows are naive local wall
            # clock, so the observed translations below are converted to the
            # same local naive instant before the two lists are merged.
            "timestamp": _iso_local(row.timestamp), "_sort": row.timestamp,
        })
    return arrivals


def _observed_translation_arrivals(instances):
    """Only a positively observed publication. Ambiguity stays out."""
    from app import activity
    arrivals = []
    for observation in activity.observed_publications():
        if observation.get("publication_outcome") != "success":
            continue
        if observation.get("operation") != "translation":
            continue
        owner = observation.get("arr_instance_id")
        published = observation.get("published_at")
        arrivals.append({
            "kind": "translation", "event_id": observation["activity_id"], "status": "success",
            "action": None, "title": observation.get("title"),
            "season": observation.get("season"), "episode": observation.get("episode"),
            "episode_title": observation.get("episode_title"),
            "language": observation.get("language"), "provider": None,
            "arr_instance_id": owner, "instance_name": _instance_name(instances, owner),
            "timestamp": published,
            # published_at is aware UTC. Stripping the offset would rank a
            # just-published translation as if it were UTC-offset hours old, and
            # east of UTC that pushes it out of a four-slot panel entirely.
            # Convert to the same local naive instant the history rows use.
            "_sort": (dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
                      .astimezone().replace(tzinfo=None)) if published else dt.datetime.min,
        })
    return arrivals


def _arrivals(instances):
    qualifications = set()
    with _connection() as connection:
        candidates = _history_candidates(connection, "episode", instances, qualifications)
        candidates += _history_candidates(connection, "movie", instances, qualifications)
    candidates += _observed_translation_arrivals(instances)
    candidates.sort(key=lambda item: item["_sort"], reverse=True)
    selected = candidates[:ARRIVAL_DISPLAY_LIMIT]
    for item in selected:
        del item["_sort"]
    status = {
        "availability": "available", "observed_at": _iso(_now()),
        "complete": not qualifications, "truncated": len(candidates) > len(selected),
        "candidate_limit": HISTORY_CANDIDATE_LIMIT, "display_limit": ARRIVAL_DISPLAY_LIMIT,
        "qualifications": sorted(qualifications),
    }
    return selected, status


def _unknown_arrivals():
    return [], {"availability": "unknown", "observed_at": None, "complete": False,
                "truncated": False, "candidate_limit": HISTORY_CANDIDATE_LIMIT,
                "display_limit": ARRIVAL_DISPLAY_LIMIT,
                "qualifications": ["source_unavailable"]}


# --------------------------------------------------------------- attention


def _live_feed_observations():
    """Per-instance feed state, for the feeds this install actually runs.

    The module instantiates both singletons at import with ``connected`` false,
    and the badges API imports it, so every running Bazarr has them whether or
    not they were ever started. main.py starts a client only when the matching
    ``use_sonarr`` or ``use_radarr`` is set and SignalR is not disabled. A client
    that was never asked to run is therefore not a disconnection; it is an
    absence of any observation, and reporting it as a failure would turn an
    optional unused library into a permanent attention item.
    """
    from app.config import settings
    from app.get_args import args
    module = sys.modules.get("app.signalr_client")
    if module is None:
        return None
    if getattr(args, "no_signalr", False):
        return []
    clients = []
    for kind, singleton, extras in (("sonarr", "sonarr_signalr_client", "_sonarr_signalr_clients"),
                                    ("radarr", "radarr_signalr_client", "_radarr_signalr_clients")):
        if not getattr(settings.general, f"use_{kind}", False):
            continue
        primary = getattr(module, singleton, None)
        candidates = ([primary] if primary is not None else [])
        candidates += list(getattr(module, extras, None) or [])
        clients.extend({"kind": kind,
                        "arr_instance_id": getattr(client, "arr_instance_id", None),
                        "connected": bool(getattr(client, "connected", False))}
                       for client in candidates)
    return clients


def _inaccessible_rootfolders(connection):
    from app.database import TableMoviesRootfolder, TableShowsRootfolder
    groups = []
    for table in (TableShowsRootfolder, TableMoviesRootfolder):
        rows = connection.execute(
            select(table.arr_instance_id, func.count().label("folders"),
                   func.min(table.path).label("example"))
            .where(table.accessible == 0)
            .group_by(table.arr_instance_id)
            # Without an order the engine chooses which owners survive the
            # bound, and SQLite and PostgreSQL need not choose the same ones.
            .order_by(table.arr_instance_id)
            .limit(ROOTFOLDER_GROUP_LIMIT)).all()
        groups += [{"arr_instance_id": row.arr_instance_id, "folders": row.folders,
                    "example": row.example} for row in rows]
    return groups


# The reasons a provider can be cooling down, in the reader's language.
#
# The stored reason is the class name of whatever exception threw
# (app/get_providers.py stores cls.__name__), so it is unbounded: no map can
# ever cover it, and the incumbent throttle map alone routinely produces
# APIThrottled, ParseResponseError, ReadTimeout, PermissionError, ProxyError,
# SearchLimitReached and ForbiddenError. A code identifier names neither the
# problem nor the recovery, which is what an error owes the person reading it,
# so an unrecognised reason gets an honest default rather than being printed
# raw or relabelled as a cause we do recognise. The exact reason is not lost:
# it stays in the item's scope for diagnosis, and System, Provider Status shows
# it verbatim.
_UNRECOGNISED_REASON = "the provider reported a problem"
_PROVIDER_REASONS = {
    "AuthenticationError": "sign-in was rejected",
    "ConfigurationError": "its settings are incomplete",
    "TooManyRequests": "the provider asked us to slow down",
    "APIThrottled": "the provider asked us to slow down",
    "SearchLimitReached": "its search limit is used up",
    "DownloadLimitExceeded": "the daily download limit is used up",
    "ServiceUnavailable": "the provider is unavailable",
    "ProviderError": "the provider reported an error",
    "ParseResponseError": "its response could not be read",
    "IPAddressBlocked": "this address is blocked by the provider",
    "ForbiddenError": "the provider refused the request",
    "PermissionError": "the provider refused the request",
    "CaptchaRequired": "the provider is asking for a captcha",
    "ConnectionError": "the provider could not be reached",
    "ProxyError": "the proxy could not be reached",
    "Timeout": "the provider did not answer in time",
    "ConnectTimeout": "the provider did not answer in time",
    "ReadTimeout": "the provider did not answer in time",
}
_PROVIDER_RECOVERY = {
    "AuthenticationError": "Fix provider sign-in",
    "ConfigurationError": "Finish provider setup",
}


def _provider_reason(reason):
    """A phrase a reader can act on. Never a class name, whatever threw."""
    return _PROVIDER_REASONS.get((reason or "").strip(), _UNRECOGNISED_REASON)


def _provider_recovery(providers):
    """Name the recovery only when every cooling provider shares one.

    An unmapped cause counts as its own answer rather than being ignored, so a
    mixed set falls back to the general label instead of promising a fix that
    only addresses some of them.
    """
    general = "Review provider status"
    labels = {_PROVIDER_RECOVERY.get((item.get("reason") or "").strip(), general)
              for item in providers}
    return labels.pop() if len(labels) == 1 else general


def _attention(instances):
    from app.get_providers import snapshot_throttled_providers
    items = []
    unknown_sources = []

    feeds = _live_feed_observations()
    if feeds is None:
        unknown_sources.append("live_feed")
    else:
        for client in feeds:
            if client["connected"]:
                continue
            owner = client["arr_instance_id"]
            name = _instance_name(instances, owner) or f"the default {client['kind'].title()}"
            items.append({
                "id": f"library_sync:{owner if owner is not None else client['kind']}",
                "capability": "library_sync", "severity": "warning",
                "scope": {"arr_instance_id": owner, "instance_name": _instance_name(instances, owner),
                          "kind": client["kind"]},
                "summary": f"Live sync for {name} is disconnected.",
                "detail": "Other instances and subtitle downloads are unaffected.",
                "freshness": "last_recorded_observation",
                "recovery": {"label": f"Check {name}", "target": "/settings/connections"},
            })

    with _connection() as connection:
        for group in _inaccessible_rootfolders(connection):
            owner = group["arr_instance_id"]
            name = _instance_name(instances, owner) or "an unnamed instance"
            folders = group["folders"]
            items.append({
                "id": f"library_paths:{owner}",
                "capability": "library_paths", "severity": "warning",
                "scope": {"arr_instance_id": owner, "instance_name": _instance_name(instances, owner),
                          "folders": folders, "example": group["example"]},
                "summary": (f"{folders} root folder is not reachable for {name}." if folders == 1
                            else f"{folders} root folders are not reachable for {name}."),
                "detail": "This is the last recorded check. Its age is unknown.",
                "freshness": "last_recorded_observation",
                "recovery": {"label": "Review path mapping", "target": "/settings/connections"},
            })

    throttled = snapshot_throttled_providers()
    if throttled["providers"]:
        names = [item["provider"] for item in throttled["providers"]]
        total = throttled["enabled_count"]
        remaining = total - len(names)
        others = ("Searches still use the other one." if remaining == 1 else
                  f"Searches still use the other {remaining}." if remaining else
                  "No other provider is enabled.")
        items.append({
            "id": "subtitle_providers", "capability": "subtitle_providers", "severity": "warning",
            # The exact reasons stay in the machine-readable scope so nothing is
            # lost, while the reader-facing detail stays free of identifiers.
            "scope": {"providers": names, "alternatives": remaining > 0,
                      "enabled_count": total,
                      "reasons": {item["provider"]: (item["reason"] or "").strip()
                                  for item in throttled["providers"]}},
            "summary": (f"{len(names)} of {total} provider{'' if total == 1 else 's'} "
                        f"{'is' if len(names) == 1 else 'are'} cooling down. " + others),
            "detail": "; ".join(f"{item['provider']}: {_provider_reason(item['reason'])}"
                                for item in throttled["providers"]),
            "freshness": "live",
            # Naming the actual problem, rather than repeating the destination
            # the rail already offers under Local pages.
            "recovery": {"label": _provider_recovery(throttled["providers"]),
                         "target": "/system/providers"},
        })

    return {"availability": "available", "observed_at": _iso(_now()),
            "complete": not unknown_sources, "unknown_sources": unknown_sources,
            "items": items}


def _unknown_attention():
    return {"availability": "unknown", "observed_at": None, "complete": False,
            "unknown_sources": ["attention"], "items": []}


# -------------------------------------------------------------- onboarding


def _first_run(instances):
    """Whether the instance has never been configured at all.

    The same rule the wizard itself uses: setup is not marked complete, no arr
    instance exists, neither legacy flag is set, and no provider is enabled. Any
    single sign of prior configuration means this is not a first run.
    """
    from app.config import settings
    general = settings.general
    return not (bool(general.setup_complete) or instances
                or bool(general.use_sonarr) or bool(general.use_radarr)
                or list(general.enabled_providers or []))


def _onboarding(instances):
    from app.config import settings
    items = []
    if _first_run(instances):
        # The first-run guide is reachable from nowhere else once Discover is
        # the default entry: the redirect that used to carry a fresh install to
        # it is gone, and /setup is a hidden route. Offering it here keeps it
        # optional and keeps Discover the entry, which is what the spec asks
        # for, without stranding the wizard behind a typed URL.
        items.append({"id": "first_run",
                      "summary": "The first-run setup guide has not been used. "
                                 "It is optional, and Settings covers the same ground.",
                      "target": "/setup"})
    if not [entry for entry in instances.values() if entry["enabled"]]:
        items.append({"id": "library",
                      "summary": "No Sonarr or Radarr instance is connected yet. "
                                 "Discover works without one.",
                      "target": "/settings/connections"})
    if not (settings.translator.openrouter_url or "").strip():
        items.append({"id": "translator",
                      "summary": "The optional AI translator is not configured.",
                      "target": "/settings/translator"})
    return {"availability": "available", "observed_at": _iso(_now()), "complete": True,
            "items": items}


# ------------------------------------------------------------------ result


def _state(activity_component, wanted, arrivals, attention, onboarding, instances):
    components = (activity_component, wanted, attention, onboarding)
    if any(item["availability"] == "unknown" for item in components):
        return "unknown"
    idle = not activity_component["running_count"] and not activity_component["queued_count"]
    if (not instances and idle and not arrivals
            and not wanted.get("requirements") and not wanted.get("unknown_media_count")):
        return "new_installation"
    if attention["items"]:
        return "degraded"
    return "quiet" if idle else "busy"


def get_summary():
    """One bounded read-only aggregate of this Bazarr's own work."""
    try:
        instances = _instances()
    except Exception:
        logger.exception("Discover summary could not read library instance identity")
        instances = {}

    try:
        activity_component = _activity_component(instances)
    except Exception:
        logger.exception("Discover summary could not read the jobs queue")
        activity_component = _unknown_activity()

    wanted = _cached_wanted(instances)

    try:
        arrivals, arrivals_status = _arrivals(instances)
    except Exception:
        logger.exception("Discover summary could not read subtitle history")
        arrivals, arrivals_status = _unknown_arrivals()

    try:
        attention = _attention(instances)
    except Exception:
        logger.exception("Discover summary could not read scoped attention")
        attention = _unknown_attention()

    try:
        onboarding = _onboarding(instances)
    except Exception:
        logger.exception("Discover summary could not read optional setup state")
        onboarding = {"availability": "unknown", "observed_at": None, "complete": False,
                      "items": []}

    return {
        "generated_at": _iso(_now()),
        "state": _state(activity_component, wanted, arrivals, attention, onboarding, instances),
        "query_budget": QUERY_BUDGET,
        "activity": activity_component,
        "wanted": wanted,
        "arrivals": arrivals,
        "arrivals_status": arrivals_status,
        "attention": attention,
        "onboarding": onboarding,
    }
