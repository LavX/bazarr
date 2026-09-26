# coding=utf-8
"""Bounded, process-local observation of the work this Bazarr host is doing.

Nothing here schedules, cancels, retries or changes a job. It records what an
existing execution path already knows at the moment it knows it, so a read-only
summary can tell running work from queue membership, a waiting host from an
active translation, and a real publication from a generic completed envelope.

Three properties are load bearing.

Identity. Queue job ids restart with the process, so an observation is keyed by
``PROCESS_RUN_ID`` plus the queue job id. A remote service job id is an alias
onto that identity, never an identity of its own: the sidecar payload carries
no instance and, for an episode, a series-level media id, so two concurrent
translations can share it.

Truth about outcomes. ``partial``, ``failed`` and ``cancelled`` dominate. A
producer that later reports success cannot promote a partial result, because
the partial marker is the reliable evidence and the success is the ambiguous
one. Only a positively observed success is retained for arrivals.

Bounds. Active observations and retained publications are capped, and the
remote alias map is capped and expired, so a long-running install cannot grow
this registry without limit.
"""

import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import local, RLock

PROCESS_RUN_ID = uuid.uuid4().hex

MAX_ACTIVE = 256
MAX_RETAINED = 64
MAX_ALIASES = 512
ALIAS_RETENTION_SECONDS = 900

# Severity order. A recorded outcome is only ever replaced by a stricter one.
_OUTCOME_RANK = {"success": 0, "unknown": 1, "partial": 2, "cancelled": 3, "failed": 4}

_SCOPE_FIELDS = (
    "scope_kind", "arr_instance_id", "media_type", "movie_id", "series_id", "episode_id",
    "upstream_movie_id", "upstream_series_id", "upstream_episode_id", "season", "episode",
    "title", "episode_title", "language", "source_language", "forced", "hi", "provider",
)

_lock = RLock()
_active = OrderedDict()
_retained = OrderedDict()
_aliases = OrderedDict()
_local = local()


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def activity_id_for_job(job_id):
    """The stable identity of a queue job's observation in this process."""
    if job_id is None or isinstance(job_id, bool) or not isinstance(job_id, int):
        return None
    return f"{PROCESS_RUN_ID}:job:{job_id}"


def new_activity_id(kind):
    """A fresh identity for work that never enters the jobs queue."""
    return f"{PROCESS_RUN_ID}:{kind}:{uuid.uuid4().hex[:12]}"


def _blank(activity_id, operation, scope_kind):
    moment = _now()
    return {
        "activity_id": activity_id,
        "operation": operation,
        "state": "running",
        "scope_kind": scope_kind,
        "parent_activity_id": None,
        "scheduler_run_id": current_scheduler_run(),
        "progress": None,
        "remote": None,
        "publication_outcome": None,
        "publication_detail": None,
        "published_at": None,
        "created_at": moment,
        "updated_at": moment,
        "finished_at": None,
        "expires_at": None,
        "monotonic": time.monotonic(),
    }


def _trim():
    while len(_active) > MAX_ACTIVE:
        _active.popitem(last=False)
    while len(_retained) > MAX_RETAINED:
        _retained.popitem(last=False)
    cutoff = time.monotonic() - ALIAS_RETENTION_SECONDS
    for key in [key for key, value in _aliases.items() if value[1] < cutoff]:
        del _aliases[key]
    while len(_aliases) > MAX_ALIASES:
        _aliases.popitem(last=False)


def register(activity_id, operation="other", scope_kind="unknown", ttl_seconds=None, **scope):
    """Record that ``activity_id`` is running, or refresh an existing record.

    ``ttl_seconds`` is for work this host submits and then stops watching. Such
    an observation expires instead of claiming to run forever, because a host
    that is no longer following a job does not know that it is still running.
    """
    if not activity_id:
        return None
    with _lock:
        observation = _active.get(activity_id)
        if observation is None:
            observation = _blank(activity_id, operation, scope_kind)
            _active[activity_id] = observation
        else:
            observation["operation"] = operation or observation["operation"]
            observation["scope_kind"] = scope_kind or observation["scope_kind"]
        if ttl_seconds:
            observation["expires_at"] = time.monotonic() + ttl_seconds
        _apply_scope(observation, scope)
        _trim()
    return activity_id


def _apply_scope(observation, scope):
    for key, value in scope.items():
        if key in _SCOPE_FIELDS and value is not None:
            observation[key] = value
        elif key in ("parent_activity_id", "scheduler_run_id") and value is not None:
            observation[key] = value
    observation["updated_at"] = _now()


def note_scope(activity_id, **fields):
    with _lock:
        observation = _active.get(activity_id) or _retained.get(activity_id)
        if observation is not None:
            _apply_scope(observation, fields)


def note_progress(activity_id, unit="item", value=None, total=None):
    """Record measured progress. Indeterminate work keeps no percentage."""
    if unit not in ("item", "percent"):
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0 or value > total:
        return
    with _lock:
        observation = _active.get(activity_id)
        if observation is not None:
            observation["progress"] = {"unit": unit, "value": value, "total": total}
            observation["updated_at"] = _now()


def _measure(value):
    """A finite non-negative count as an int, or None. Services send floats."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return int(value)


def note_remote_submission(activity_id, service_id=None, remote_job_id=None):
    """Bind an opaque remote service job onto this host activity."""
    if not activity_id or not service_id or not remote_job_id:
        return
    with _lock:
        observation = _active.get(activity_id)
        if observation is None:
            return
        # No observed_at yet: submission is a local event, and stamping it with
        # local time would make the service's own first report look late.
        observation["remote"] = {
            "service_id": str(service_id), "job_id": str(remote_job_id),
            "phase": "submitted", "observed_at": None, "progress": None, "total": None,
        }
        observation["updated_at"] = _now()
        _aliases[(str(service_id), str(remote_job_id))] = (activity_id, time.monotonic())
        _trim()


def note_remote_phase(activity_id=None, service_id=None, remote_job_id=None, phase=None,
                      progress=None, total=None, observed_at=None):
    """Update the last observed remote phase. A late report never wins."""
    with _lock:
        if activity_id is None and service_id and remote_job_id:
            alias = _aliases.get((str(service_id), str(remote_job_id)))
            activity_id = alias[0] if alias else None
        observation = _active.get(activity_id)
        if observation is None or not observation.get("remote"):
            return
        remote = observation["remote"]
        moment = observed_at or _now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if remote["observed_at"] is not None and moment < remote["observed_at"]:
            return
        remote["observed_at"] = moment
        if phase:
            remote["phase"] = str(phase)
        remote["progress"] = _measure(progress)
        remote["total"] = _measure(total)
        observation["updated_at"] = _now()
        if remote["phase"] == "processing":
            note_progress(activity_id, unit="percent", value=remote["progress"],
                          total=remote["total"] if remote["total"] else 100)
        else:
            observation["progress"] = None


def note_publication(activity_id, outcome=None, detail=None, **scope):
    """Record a typed outcome at a real publication boundary.

    A stricter outcome is never replaced by a softer one, so a generic completed
    envelope after a partial result cannot claim a success.
    """
    if outcome not in _OUTCOME_RANK:
        return
    with _lock:
        observation = _active.get(activity_id) or _retained.get(activity_id)
        if observation is None:
            return
        current = observation.get("publication_outcome")
        if current is None or _OUTCOME_RANK[outcome] > _OUTCOME_RANK[current]:
            observation["publication_outcome"] = outcome
            observation["publication_detail"] = detail
            observation["published_at"] = _now()
        _apply_scope(observation, scope)


def finish(activity_id, outcome=None, detail=None):
    """Retire an activity, keeping only a positively observed publication."""
    with _lock:
        observation = _active.pop(activity_id, None)
        if observation is None:
            return
        if outcome in _OUTCOME_RANK:
            current = observation.get("publication_outcome")
            if current is None or _OUTCOME_RANK[outcome] > _OUTCOME_RANK[current]:
                observation["publication_outcome"] = outcome
                observation["publication_detail"] = detail
                observation["published_at"] = _now()
        observation["state"] = "finished"
        observation["finished_at"] = _now()
        if observation.get("publication_outcome") is not None:
            _retained[activity_id] = observation
        _trim()


def _detached(observation):
    item = {key: value for key, value in observation.items()
            if key not in ("monotonic", "expires_at")}
    for key in ("created_at", "updated_at", "finished_at", "published_at"):
        item[key] = _iso(item.get(key))
    item["age_seconds"] = max(0.0, round(time.monotonic() - observation["monotonic"], 3))
    progress = observation.get("progress")
    item["progress"] = dict(progress) if progress else None
    remote = observation.get("remote")
    if remote:
        item["remote"] = {**remote, "observed_at": _iso(remote["observed_at"])}
    return item


def snapshot(limit=MAX_ACTIVE):
    """Detached copies of the currently observed work, oldest first."""
    now = time.monotonic()
    with _lock:
        for identity in [identity for identity, observation in _active.items()
                         if observation["expires_at"] and observation["expires_at"] <= now]:
            del _active[identity]
        return [_detached(observation) for observation in list(_active.values())[:limit]]


def observed_publications(limit=MAX_RETAINED):
    """Detached copies of retained publication outcomes, oldest first."""
    with _lock:
        return [_detached(observation) for observation in list(_retained.values())[:limit]]


def current_scheduler_run():
    return getattr(_local, "scheduler_run", None)


@contextmanager
def scheduler_run(task_id, name=None):
    """Mark the calling thread as executing one recurring scheduler task.

    The queue child a scheduled wrapper enqueues inherits this id, which is what
    lets the summary show one search rather than a wrapper and its child.
    """
    previous = getattr(_local, "scheduler_run", None)
    run_id = f"{PROCESS_RUN_ID}:schedule:{task_id}:{uuid.uuid4().hex[:8]}"
    _local.scheduler_run = run_id
    del name
    try:
        yield run_id
    finally:
        _local.scheduler_run = previous


class _TaggedSchedulerPool:
    """Wraps a worker pool so each scheduled execution knows its task id.

    APScheduler submits ``run_job(job, alias, run_times, logger_name)`` to its
    executor pool. Tagging here rather than wrapping the registered callable
    matters: a job's ``func`` has to stay the exact object that was registered,
    and every submission argument, result and exception is forwarded unchanged.
    """

    def __init__(self, pool):
        self._pool = pool

    def submit(self, run_job, job, *rest):
        def observed(job, *arguments):
            with scheduler_run(getattr(job, "id", "task")):
                return run_job(job, *arguments)

        return self._pool.submit(observed, job, *rest)

    def shutdown(self, wait=True):
        self._pool.shutdown(wait)


def tagged_scheduler_pool(pool):
    return _TaggedSchedulerPool(pool)


@contextmanager
def observed_operation(operation, scope_kind="request", activity_id=None, **scope):
    """Observe a synchronous execution path that never enters the jobs queue."""
    identity = activity_id or new_activity_id(operation)
    register(identity, operation=operation, scope_kind=scope_kind, **scope)
    try:
        yield identity
    except BaseException:
        finish(identity)
        raise
    else:
        finish(identity)


def reset():
    """Discard every observation. Test support only."""
    with _lock:
        _active.clear()
        _retained.clear()
        _aliases.clear()
    _local.scheduler_run = None
