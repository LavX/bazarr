# coding=utf-8

import logging
import importlib
import inspect
import os
import time

from time import sleep
from datetime import datetime, timezone
from collections import deque
from typing import Union
from threading import Thread, Lock, RLock

from app import activity
from app.event_handler import event_stream
from app.process_session import PROCESS_SESSION
from app.config import settings
from secret_store.scrub import scrub_text, scrub_value

# The package directory, i.e. the one holding subtitles/, sonarr/, app/ and the
# rest. add_job_from_function derives a job's module from its filename relative
# to this, and _run_job resolves that string with importlib, so it has to be the
# name the module already has in this process.
#
# It used to be one dirname() higher, naming the repository root, so every job
# was queued as "bazarr.<x>" while the running process had imported "<x>".
# app/libs.py puts the parent on sys.path as well, so both names resolve and
# Python keeps two module objects for the same file, each with its own
# module-level state. subtitles/cache.py documents that split and works around
# it, because the UUID a manual search stored in one copy was looked for in the
# other. subtitles/pool.py holds _pools the same way, and Provider Hub spawns a
# worker subprocess per pool and provider, so the split also bought a second
# worker fleet on any warm install.
bazarr_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# How many failed and how many completed jobs the queue remembers, each. Ten
# was enough for a drawer nobody scrolled; with more actions on the standard
# path it dropped a job before its owner had read how it ended. The client
# keeps its own cache at least this deep for both histories plus whatever is
# still active, so a trimmed cache never drops an active job first.
JOB_HISTORY_DEPTH = 50

# How many of the most recent ended jobs may still hold a published result. A
# result can be the rows of a whole search, so the history keeps them for the
# searches a person is likely to come back to and drops them from older jobs.
JOB_RESULTS_KEPT = 10

# Jobs a person starts by pressing a button are forced to run at once, past the
# general limit, because they are waiting on them. Each one is a thread, so a
# lane caps how many such jobs run together. A job over its lane's cap is not
# refused: it stays on the pending queue and runs in its turn.
USER_SEARCH_LANE = "user_search"
USER_ACTION_LANE = "user_action"
LANE_LIMITS = {USER_SEARCH_LANE: 4, USER_ACTION_LANE: 4}
LANE_FULL_MESSAGE = "Waiting for your other requests to finish"
# How often the consumer loop logs an error it survived, so a fault that repeats every pass is not a flood.
CONSUMER_ERROR_LOG_SECONDS = 60


def _job_event(action: str, payload: dict):
    """Send one jobs event, stamped with the backend session it belongs to.

    Job ids restart with the backend, so the stamp is what lets a client tell
    an event about one of its own jobs from one about a new job that reuses the
    id after a restart.
    """
    event_stream(type='jobs', action=action, payload={**payload, "session": PROCESS_SESSION})


class JobCancelled(Exception):
    """Raised when a running job is cancelled by the user.

    ``job_id`` names the stopped job. A stop can surface inside another job's
    work, because the provider pool a search reports its progress through is
    shared between the jobs of a profile, so work being interrupted checks
    whose stop arrived before it ends its own job. The message is optional: a
    caller that only signals the stop raises the class bare.
    """

    def __init__(self, message="", job_id=None):
        super().__init__(message)
        self.job_id = job_id


class JobFailed(Exception):
    """Raised by a job that failed for a reason it can name.

    The job ends up in the failed queue like any other failure. Its ``error``
    carries ``reason`` and the one-line ``message``, which the user sees in the
    failure notification and in the Jobs drawer. The queue logs it at DEBUG
    only: the job is expected to have logged the cause itself, once.
    ``retryable`` overrides the job's own flag for this failure, for example a
    reason that no retry can fix.
    """

    def __init__(self, message: str, reason: str = "failed", returned_value=None, retryable: bool = None):
        super().__init__(message)
        self.reason = reason
        self.returned_value = returned_value
        self.retryable = retryable


# What a user sees for a failure the job did not name. The exception text can
# carry paths or provider payloads, so it stays in the log.
UNEXPECTED_JOB_ERROR = {"reason": "unexpected_error",
                        "message": "The job failed unexpectedly. Check the logs for details."}

# What a user sees for a job whose thread the operating system would not start.
COULD_NOT_START_JOB_ERROR = {"reason": "could_not_start",
                             "message": "The job could not be started. Check the logs for details."}

# The jobs that call the AI Subtitle Translator, by the module and function
# they run: a library translation and an editor translation. They share the
# translation lane. A job's name cannot decide that: it carries titles and
# filenames, so a download for "Lost in Translation" read as a translation,
# and it changes while the job runs.
TRANSLATION_JOBS = frozenset({
    ('subtitles.tools.translate.main', 'translate_subtitles_file'),
    ('subtitles.tools.translate.editor', 'translate_editor_lines'),
})


def is_translation_job(job) -> bool:
    """Whether ``job`` runs in the translation lane."""
    return (job.module, job.func) in TRANSLATION_JOBS


class Job:
    """
    Represents a job with details necessary for its identification and execution.

    This class encapsulates information about a job, including its unique identifier,
    name, and the module or function it executes. It can also include optional
    arguments and keyword arguments for job execution. The status of the job is also
    tracked.

    :ivar job_id: Unique identifier of the job.
    :type job_id: int
    :ivar session: The backend run that created the job. Ids restart with the backend, so a job is only
        named by its id together with this.
    :type session: str
    :ivar job_name: Descriptive name of the job.
    :type job_name: str
    :ivar module: Name of the module where the job function resides.
    :type module: str
    :ivar func: The name of the function to execute the job.
    :type func: str
    :ivar args: Positional arguments for the function, it defaults to None.
    :type args: list, optional
    :ivar kwargs: Keyword arguments for the function, it defaults to None.
    :type kwargs: dict, optional
    :ivar status: Current status of the job, initialized to 'pending'.
    :type status: str
    :ivar last_run_time: Last time the job was run, initialized to None.
    :type last_run_time: datetime
    :ivar is_progress: Indicates whether the job is a progress job, defaults to False.
    :type is_progress: bool
    :ivar is_signalr: Indicates whether the job as been initiated by a SignalR event, defaults to False.
    :type is_signalr: bool
    :ivar progress_value: Actual value of the job's progress, initialized to 0.
    :type progress_value: int
    :ivar progress_max: Maximum value of the job's progress, initialized to 0.
    :type progress_max: int
    :ivar progress_message: Message shown for this job's progress, initialized to an empty string.
    :type progress_message: str
    :ivar job_returned_value: Value returned by the job function, initialized to None.
    :type job_returned_value: Any
    :ivar error: Why the job failed, ``{"reason", "message"}``, or None. Shown to the user.
    :type error: dict, optional
    :ivar action: What the user can do with the finished job, set by the job itself:
        ``{"kind", "label", ...}``. The frontend knows how to run each kind.
    :type action: dict, optional
    :ivar result: What the job offers its caller beyond ``job_returned_value``: a JSON-safe value the job
        published on purpose, such as the rows of a search. It is never part of a job's listing or of its events,
        and is read through ``get_job_result`` only. The job may publish it again while it runs, so a caller can
        follow a partial result. None until the job publishes one.
    :type result: Any
    :ivar retryable: Whether a failed job may be queued again with the same arguments.
    :type retryable: bool
    :ivar retry_of: The id of the failed job this one retries, or None.
    :type retry_of: int, optional
    :ivar stopped: Whether the job ended at Stop instead of finishing, which its ``cancelled`` status
        says as well. The flag is kept for consumers that keyed on it while a stopped job still read as
        completed. ``cancelled`` alone only records that Stop was pressed, and a job past its last
        checkpoint finishes anyway, with status ``completed``.
    :type stopped: bool
    :ivar origin: Who started the job: ``"user"`` for one an authenticated request enqueued, ``"scheduled"``
        for one the scheduler's task pool enqueued, None when nothing knows. Sent with the job, so the
        client can announce the user's own work and leave routine work quiet.
    :type origin: str, optional
    :ivar lane: The lane a job a person starts is counted in, one of ``LANE_LIMITS``, or None. Forcing a
        job to start at once is held to its lane's cap.
    :type lane: str, optional
    """
    def __init__(self, job_id: int, job_name: str, module: str, func: str, args: list = None, kwargs: dict = None,
                 is_progress: bool = False, is_signalr: bool = False, progress_max: int = 0, job_returned_value=None,
                 retryable: bool = False, retry_of: int = None, lane: str = None):
        self.job_id = job_id
        self.session = PROCESS_SESSION
        # A name is built from a media title or an uploaded filename and the jobs API returns it
        # as is, so a credential-shaped title is masked where the name is set.
        # Trade-off: the name is scrubbed against every registered value of
        # six or more characters, so a username that is also a word in a title
        # or a path (a Postgres user "bazarr") is masked there too. A leak in
        # a name reaches the API and the logs; a masked title only costs
        # readability.
        self.job_name = scrub_text(job_name)
        self.module = module
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.status = 'pending'
        self.last_run_time = datetime.now(timezone.utc)
        self.is_progress = is_progress
        self.is_signalr = is_signalr
        self.progress_value = 0
        self.progress_max = progress_max
        self.progress_message = ""
        self.job_returned_value = job_returned_value
        self.error = None
        self.action = None
        self.result = None
        self.retryable = retryable
        self.retry_of = retry_of
        self.lane = lane
        # Set when a person started the job and its lane was full, so it sits on pending
        # waiting for a lane slot. The queue starts such a job as soon as the lane has room.
        self.lane_waiting = False
        self.cancelled = False
        self.stopped = False
        # Who started the job, read from the enqueuing thread: an
        # authenticated request says the user, and the scheduler's task pool
        # says routine work. Unlike the observations below it is sent with
        # the job.
        self.origin = activity.current_job_origin()
        # Observation only. ``last_run_time`` is overwritten at creation, start
        # and terminal state, so it cannot tell those three apart; these can.
        # They never take part in equality, scheduling or execution.
        self.activity_id = activity.activity_id_for_job(job_id)
        self.scheduler_run_id = activity.current_scheduler_run()
        self.observed_created_at = datetime.now(timezone.utc)
        self.observed_started_at = None
        self.observed_updated_at = None
        self.observed_finished_at = None
        self.observed_monotonic = time.monotonic()

    def __eq__(self, other):
        """
        Custom equality check for job objects to compare only based on job_id when trying to remove existing jobs from
        queues.
        """
        return self.job_id == other.job_id


class JobsQueue:
    """
    Manages a queue of jobs, tracks their states, and processes them.

    This class is designed to handle a queue of jobs, enabling submission, tracking,
    and execution of tasks. Jobs are categorized into different queues (`pending`,
    `running`, `failed`, and `completed`) based on their current status. It provides
    methods to add, list, remove, and consume jobs in a controlled manner.

    :ivar jobs_pending_queue: Queue containing jobs that are pending execution.
    :type jobs_pending_queue: deque
    :ivar jobs_running_queue: Queue containing jobs that are currently being executed.
    :type jobs_running_queue: deque
    :ivar jobs_failed_queue: Queue containing jobs that failed during execution. It maintains a
        maximum size of ``JOB_HISTORY_DEPTH`` entries.
    :type jobs_failed_queue: deque
    :ivar jobs_completed_queue: Queue containing jobs that were executed successfully. It maintains
        a maximum size of ``JOB_HISTORY_DEPTH`` entries.
    :type jobs_completed_queue: deque
    :ivar current_job_id: Identifier of the latest job, incremented with each new job added to the queue.
    :type current_job_id: int
    """
    def __init__(self):
        self.jobs_pending_queue = deque()
        self.jobs_running_queue = deque()
        self.jobs_failed_queue = deque(maxlen=JOB_HISTORY_DEPTH)
        self.jobs_completed_queue = deque(maxlen=JOB_HISTORY_DEPTH)
        self.current_job_id = 0
        
        # Add locks for thread safety
        self._queue_lock = RLock()  # Reentrant lock for nested operations
        self._job_id_lock = Lock()  # Separate lock for ID generation
        self._import_lock = Lock()  # Lock for module imports

        # Inline runs a batch job claimed in place of queueing one, each keyed
        # like the job the queue would refuse to duplicate and owned by the
        # claiming job's id. They are read in the same look, under this same
        # lock, as the pending and running queues.
        self._inline_claims = []

        # Throttle progress events: buffer latest payload per job, flush every 250 ms
        self._progress_buffer = {}
        self._progress_buffer_lock = Lock()
        flush_thread = Thread(target=self._flush_progress_loop, daemon=True)
        flush_thread.start()

    def feed_jobs_pending_queue(self, job_name, module, func, args: list = None, kwargs: dict = None,
                                is_progress=False, is_signalr=False, progress_max: int = 0,
                                retryable: bool = False, retry_of: int = None, return_existing: bool = False,
                                lane: str = None):
        """
        Adds a new job to the pending jobs queue with specified details and triggers an event
        to notify about the queue update. Each job is uniquely identified by a job ID,
        which is automatically incremented for each new job. Logging is performed to
        record the job addition.

        :param job_name: Name of the job to be added to the queue.
        :type job_name: str
        :param module: Module under which the job's function resides (ex: sonarr.sync.series).
        :type module: str
        :param func: Function name that represents the job (ex: update_series).
        :type func: str
        :param args: List of positional arguments to be passed to the function.
        :type args: list
        :param kwargs: Dictionary of keyword arguments to be passed to the function.
        :type kwargs: dict
        :param is_progress: Indicates whether the job is a progress job, defaults to False.
        :type is_progress: bool
        :param is_signalr: Indicates whether the job as been initiated by a SignalR event, defaults to False.
        :type is_signalr: bool
        :param progress_max: Maximum value of the job's progress, initialized to 0.
        :type progress_max: int
        :param retryable: Whether the user may retry the job with the same arguments if it fails.
        :type retryable: bool
        :param retry_of: The id of the failed job this one retries.
        :type retry_of: int
        :param return_existing: When an identical job is already pending or running, or runs inline in a
            claimed batch job, return that job's ID instead of False. It is read in the same look as the
            match, under the queue lock, so the caller gets a job to follow even when that job finishes a
            moment later.
        :type return_existing: bool
        :param lane: The lane a job a person starts is counted in, one of ``LANE_LIMITS``. Forcing the job to
            start at once is held to that lane's cap.
        :type lane: str
        :return: The unique job ID assigned to the newly queued job, or False when an identical job is already
            pending or running.
        :rtype: int | bool
        """
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}

        with self._queue_lock:
            existing = self._find_existing_job(module, func, args, kwargs)
            if existing is not None and existing.cancelled and return_existing:
                # A caller that wants to follow the running job has nothing to gain from one that was told
                # to stop and only ends at its next check: it would read as stopped. It gets a job of its own.
                existing = None
            if existing is not None:
                logging.debug(f"Task {job_name} already exists in pending and running queue")  # noqa: G004
                return existing.job_id if return_existing else False

            claim = self._find_inline_claim(module, func, args, kwargs)
            if claim is not None:
                # The work runs inline in the batch job that claimed it, so a
                # request that would queue the same work follows that job
                # instead of starting a second one.
                logging.debug(f"Task {job_name} runs inline in job {claim['job_id']}")  # noqa: G004
                return claim['job_id'] if return_existing else False

            with self._job_id_lock:
                new_job_id = self.current_job_id = self.current_job_id + 1

            self.jobs_pending_queue.append(
                Job(job_id=new_job_id,
                    job_name=job_name,
                    module=module,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    is_progress=is_progress,
                    is_signalr=is_signalr,
                    progress_max=progress_max,
                    retryable=retryable,
                    retry_of=retry_of,
                    lane=lane,)
            )

        logging.debug(f"Task {job_name} ({new_job_id}) added to queue")  # noqa: G004
        _job_event('update', {"job_id": new_job_id, "progress_value": None, "status": "pending"})
        return new_job_id

    def list_jobs_from_queue(self, job_id: int = None, status: str = None):
        """
        List jobs from a specific queue or all queues based on filters.

        This method retrieves job details from various job queues based on provided
        criteria. It can filter jobs by their `job_id` and/or their `status`. If no
        `job_id` or `status` is provided, it returns details of all jobs across
        all queues.

        :param job_id: Optional; The unique ID of the job to filter the results.
        :type job_id: int
        :param status: Optional; The status of jobs to filter the results. Expected
            values are 'pending', 'running', 'failed', 'completed', or 'cancelled'.
        :type status: str
        :return: A list of dictionaries with job details that match the given filters.
            If no matches are found, an empty list is returned.
        :rtype: list[dict]
        """
        queues = self.jobs_pending_queue + self.jobs_running_queue + self.jobs_failed_queue + self.jobs_completed_queue
        if status:
            terminal = status in ('completed', 'cancelled')
            name = f'jobs_{status}_queue'
            if status == 'cancelled':
                # A stopped job is recorded as cancelled and kept on the
                # completed deque: one terminal history, one cap, and the one
                # queue the drawer's Stopped group clears.
                name = 'jobs_completed_queue'
            try:
                queues = self.__dict__[name]
            except KeyError:
                return []
            if terminal:
                # Two statuses share the one deque, so the row's own status
                # tells them apart: 'completed' does not answer a stopped job.
                queues = [job for job in queues if job.status == status]

        if job_id:
            return [vars(job) for job in queues if job.job_id == job_id]
        else:
            return [vars(job) for job in queues]

    SNAPSHOT_FIELDS = (
        'job_id', 'job_name', 'module', 'func', 'status', 'is_progress', 'is_signalr',
        'progress_value', 'progress_max', 'progress_message', 'cancelled',
        'activity_id', 'scheduler_run_id',
        'created_at', 'started_at', 'updated_at', 'finished_at', 'age_seconds',
    )

    def snapshot_activity(self, limit_per_state: int = 5):
        """A detached, whitelisted view of the queues for read-only reporting.

        ``list_jobs_from_queue`` concatenates the deques without the lock and
        hands back ``vars(job)``, which aliases the live job and exposes its
        args, kwargs and returned value. Those carry subtitle objects, absolute
        paths, provider clients and API keys, so this copies a fixed field list
        under the lock instead.

        Counts are reported for the whole queue and samples are bounded, so a
        truncated sample can never be mistaken for the total. ``running`` counts
        jobs whose status has actually flipped: reservation puts a job on the
        running deque one step before it starts, and that gap is reported
        separately as ``reserved`` rather than as work in progress.
        """
        limit = max(0, int(limit_per_state))
        now = time.monotonic()

        def project(job):
            return {
                'job_id': job.job_id,
                'job_name': job.job_name,
                'module': job.module,
                'func': job.func,
                'status': job.status,
                'is_progress': bool(job.is_progress),
                'is_signalr': bool(job.is_signalr),
                'progress_value': job.progress_value,
                'progress_max': job.progress_max,
                'progress_message': job.progress_message,
                'cancelled': bool(job.cancelled),
                'activity_id': getattr(job, 'activity_id', None),
                'scheduler_run_id': getattr(job, 'scheduler_run_id', None),
                'created_at': getattr(job, 'observed_created_at', None),
                'started_at': getattr(job, 'observed_started_at', None),
                'updated_at': getattr(job, 'observed_updated_at', None),
                'finished_at': getattr(job, 'observed_finished_at', None),
                'age_seconds': max(0.0, round(now - getattr(job, 'observed_monotonic', now), 3)),
            }

        with self._queue_lock:
            pending = list(self.jobs_pending_queue)
            running_members = list(self.jobs_running_queue)
            failed = list(self.jobs_failed_queue)
            completed = list(self.jobs_completed_queue)
            running = [job for job in running_members if job.status == 'running']
            reserved = [job for job in running_members if job.status != 'running']
            samples = {
                'pending': [project(job) for job in pending[:limit]],
                'running': [project(job) for job in running[:limit]],
                'reserved': [project(job) for job in reserved[:limit]],
                'failed': [project(job) for job in failed[:limit]],
                'completed': [project(job) for job in completed[:limit]],
            }
            counts = {'pending': len(pending), 'running': len(running), 'reserved': len(reserved),
                      'failed': len(failed), 'completed': len(completed)}

        return {
            'observed_at': datetime.now(timezone.utc),
            'counts': counts,
            'samples': samples,
            'truncated': {state: counts[state] > len(samples[state]) for state in counts},
        }

    def get_job_status(self, job_id: int):
        """
        Retrieves the status of a job by its ID from a queue. If the job exists and has a
        status field, it returns its value. Otherwise, it returns "Unknown job".

        :param job_id: ID of the job to retrieve status for
        :type job_id: int
        :return: The status of the job if available, otherwise "Unknown job"
        :rtype: str
        """
        job = self.list_jobs_from_queue(job_id=job_id)
        if job and 'status' in job[0]:
            return job[0]['status']
        else:
            return "Unknown job"

    def update_job_name(self, job_id: int, new_job_name: str) -> bool:
        """
        Updates the name of a job present in one of the job queues. The job is searched by its unique
        identifier (`job_id`) in all available queues, and if found, the job's name is updated to
        `new_job_name`. After updating, it triggers an event to notify the frontend about the job update.

        :param job_id: The unique identifier of the job to be updated.
        :param new_job_name: The new name to assign to the job.
        :return: A boolean indicating whether the job name was successfully updated (True) or the job
                 was not found in any of the queues (False).
        """
        queues = self.jobs_pending_queue + self.jobs_running_queue + self.jobs_failed_queue + self.jobs_completed_queue
        
        for job in queues:
            if job.job_id == job_id:
                # Same trade-off as in Job.__init__: a registered value inside
                # the name is masked, even when it is also an ordinary word.
                job.job_name = scrub_text(new_job_name)
                _job_event('update', {"job_id": job.job_id})
                return True
        return False

    def get_job_name(self, job_id: int) -> str:
        """Get the current name of a job by its ID."""
        queues = self.jobs_pending_queue + self.jobs_running_queue + self.jobs_failed_queue + self.jobs_completed_queue
        for job in queues:
            if job.job_id == job_id:
                return job.job_name
        return ""

    def set_job_action(self, job_id: int, action: dict) -> bool:
        """Offer the user something to do with this job once it has finished.

        ``action`` is a flat, JSON-safe dict with a ``kind`` the frontend knows
        how to run and a ``label`` for its button, plus whatever that kind
        needs, for example ``{"kind": "discover.save", "label": "Save",
        "ticket": 12}``. The completion notification and the Jobs drawer show
        it; nothing else about the job is exposed. It is scrubbed like every
        other job text: a filename a provider chose can carry a credential.
        ``kind`` is dispatch, not text, so it is kept as given: a credential
        that happens to equal a handler name must not stop the frontend
        finding that handler.
        """
        action = dict(action)
        kind = action.get('kind')
        scrubbed = scrub_value(action)
        if kind is not None:
            scrubbed['kind'] = kind
        with self._queue_lock:
            for job in self.jobs_running_queue:
                if job.job_id == job_id:
                    job.action = scrubbed
                    return True
        return False

    def publish_job_result(self, job_id: int, result) -> bool:
        """Offer ``result`` to whoever follows this running job.

        The value must be JSON-safe and fit to be shown to the user: it is
        served as is by ``get_job_result``. It replaces any earlier one, so a
        job may publish a partial result and finish with the whole one. A
        value that carries anything that must not leave the process belongs in
        the job's own store instead, as the Discover download keeps its file.
        Publishing never stops a job: a cancelled job still answers True, and
        the next progress update is where it learns of the stop.

        :return: False when no running job has this id.
        """
        with self._queue_lock:
            for job in self.jobs_running_queue:
                if job.job_id == job_id:
                    job.result = result
                    return True
        return False

    def get_job_result(self, job_id: int):
        """The job's outcome for a caller that wants what it produced, or None for an unknown id.

        ``{"job_id", "session", "status", "error", "result"}``. ``result`` is
        whatever the job published, which is the partial one while it is still
        running and the last one once it has ended, or None.
        """
        with self._queue_lock:
            queues = (list(self.jobs_pending_queue) + list(self.jobs_running_queue)
                      + list(self.jobs_failed_queue) + list(self.jobs_completed_queue))
            job = next((item for item in queues if item.job_id == job_id), None)
            if job is None:
                return None
            return {"job_id": job.job_id, "session": job.session, "status": job.status,
                    "error": job.error, "result": job.result}

    def retry_job(self, job_id: int) -> Union[int, bool]:
        """Queue a failed, retryable job again with its original arguments.

        Returns the new job's id, which carries ``retry_of`` so whoever was
        following the failed job can follow its retry, or False.
        """
        with self._queue_lock:
            job = next((item for item in self.jobs_failed_queue if item.job_id == job_id), None)
        if job is None or not job.retryable:
            return False
        kwargs = {key: value for key, value in (job.kwargs or {}).items() if key != 'job_id'}
        return self.feed_jobs_pending_queue(job_name=job.job_name, module=job.module, func=job.func,
                                            args=list(job.args or []), kwargs=kwargs,
                                            is_progress=job.is_progress, progress_max=job.progress_max,
                                            retryable=True, retry_of=job.job_id)

    def get_job_returned_value(self, job_id: int):
        """
        Fetches the returned value of a job from the queue provided its unique identifier.

        This function retrieves the job details from the queue using the provided job
        identifier. If the job exists and contains a 'job_returned_value' key, the
        function returns the corresponding value. Otherwise, it defaults to returning
        None.

        :param job_id: The unique identifier of the job to fetch the returned value for.
        :type job_id: int
        :return: The returned value of the job if it exists, otherwise None.
        :rtype: Any
        """
        job = self.list_jobs_from_queue(job_id=job_id)
        if job and 'job_returned_value' in job[0]:
            return job[0]['job_returned_value']
        else:
            return None

    def _flush_progress_loop(self):
        while True:
            sleep(0.25)
            with self._progress_buffer_lock:
                payloads = list(self._progress_buffer.values())
                self._progress_buffer.clear()
            for payload in payloads:
                _job_event('update', payload)

    def update_job_progress(self, job_id: int, progress_value: Union[int, str, None] = None,
                            progress_max: Union[int, None] = None, progress_message: str = "", *,
                            allow_cancelled: bool = False):
        """
        Updates the progress value and message for a specific job within the running jobs queue. The function
        iterates through a queue of running jobs, identifies the matching job by its ID, and updates its progress
        value and message. Afterward, triggers an event stream for the updated job.

        :param job_id: The unique identifier of the job to be updated.
        :type job_id: int
        :param progress_value: The new progress value to be set for the job. If 'max' is provided, progress_value will
        equal progress_max.
        :type progress_value: int or str or None
        :param progress_max: Maximum value of the job's progress.
        :type progress_max: int or None
        :param progress_message: An optional message providing additional details about the current progress.
        :type progress_message: str
        :return: Returns True if the job's progress was successfully updated, otherwise False.
        :rtype: bool
        """
        for job in self.jobs_running_queue:
            if job.job_id == job_id:
                # Only final accounting of an already committed operation may
                # report after cancellation. Work checks retain the default.
                if job.cancelled and not allow_cancelled:
                    raise JobCancelled(f"Job {job.job_name} ({job.job_id}) was cancelled", job_id=job_id)
                payload = self._build_progress_payload(job, progress_value, progress_max, progress_message)
                with self._progress_buffer_lock:
                    self._progress_buffer[job_id] = payload
                return True
        return False

    @staticmethod
    def _build_progress_payload(job, progress_value: Union[int, str, None],
                                progress_max: Union[int, None], progress_message: str):
        """
        Builds the payload dictionary for job progress updates and updates job attributes.

        :param job: The job instance to update.
        :param progress_value: The new progress value to be set for the job.
        :type progress_value: int or str or None
        :param progress_max: Maximum value of the job's progress.
        :type progress_max: int or None
        :param progress_message: An optional message providing additional details about the current progress.
        :type progress_message: str
        :return: Dictionary containing the payload for the event stream.
        :rtype: dict
        """
        # A progress message is built by app code out of a provider's or a
        # plugin's own words, and a credential can be inside one: it is
        # stored on the job, sent over the socket and read back by the API,
        # so it is scrubbed once, here, before any of them.
        progress_message = scrub_text(progress_message)
        payload = {"job_id": job.job_id, "status": job.status}
        progress_max_updated = False

        # ``progress_value`` may legitimately be 0 (a reset at the start of a new
        # phase). A truthiness check here silently dropped those resets, leaving a
        # stale large value behind a freshly-set small max -> >100% progress rings.
        if progress_value is not None:
            if progress_value == 'max':
                progress_value = job.progress_max or 1
                job.progress_value = job.progress_max = progress_value
                progress_max_updated = True
            else:
                job.progress_value = progress_value
        payload["progress_value"] = job.progress_value

        if progress_max and not progress_max_updated:
            job.progress_max = progress_max
        payload["progress_max"] = job.progress_max

        if progress_message:
            job.progress_message = progress_message
        payload["progress_message"] = job.progress_message

        job.observed_updated_at = datetime.now(timezone.utc)
        return payload

    def update_job_progress_status(self, job_id: int, is_progress: bool = False) -> bool:
        """
        Updates the is_progress attribute for a specific job.

        :param job_id: The unique identifier of the job to be updated.
        :type job_id: int
        :param is_progress: The new value for is_progress attribute.
        :type is_progress: bool
        :return: Returns True if the job's progress status was successfully updated, otherwise False.
        :rtype: bool
        """
        for job in self.jobs_running_queue:
            if job.job_id == job_id:
                job.is_progress = is_progress
                _job_event('update', {"job_id": job.job_id})
                return True
        return False

    def add_job_from_function(self, job_name: str, is_progress: bool, progress_max: int = 0,
                              wait_for_completion: bool = False, return_existing: bool = False) -> int | bool:
        """
        Adds a job to the pending queue using the details of the calling function. The job is then executed.

        :param job_name: Name of the job to be added.
        :type job_name: str
        :param is_progress: Flag indicating whether the progress of the job should be tracked.
        :type is_progress: bool
        :param progress_max: Maximum progress value for the job, default is 0.
        :type progress_max: int
        :param wait_for_completion: Flag indicating whether to wait for the job to complete before returning.
        :type wait_for_completion: bool
        :param return_existing: Return the matching active job's id when this call is already queued.
        :type return_existing: bool
        :return: ID of the added job.
        :rtype: int | bool
        """
        # Get the current frame
        current_frame = inspect.currentframe()

        # Get the frame of the caller (parent function)
        # The caller's frame is at index 1 in the stack
        caller_frame = current_frame.f_back

        # Get the code object of the caller
        caller_code = caller_frame.f_code

        # Get the name of the parent function
        parent_function_name = caller_code.co_name

        # Get the file path of the parent function
        relative_parent_function_path = os.path.relpath(caller_code.co_filename, start=bazarr_dir)
        parent_function_path = os.path.splitext(relative_parent_function_path)[0].replace(os.sep, '.')

        # Get the function signature of the caller
        caller_signature = inspect.signature(inspect.getmodule(caller_code).__dict__[caller_code.co_name])
        # Get the local variables within the caller's frame
        caller_locals = caller_frame.f_locals

        bound_arguments = caller_signature.bind(**caller_locals)
        arguments = bound_arguments.arguments

        # Clean up the frame objects to prevent reference cycles
        del current_frame, caller_frame, caller_code, caller_signature, caller_locals, bound_arguments

        # Feed the job to the pending queue
        job_id = self.feed_jobs_pending_queue(job_name=job_name, module=parent_function_path, func=parent_function_name,
                                              kwargs=arguments, is_progress=is_progress, progress_max=progress_max,
                                              return_existing=return_existing)

        if not job_id:
            return False

        if wait_for_completion:
            time.sleep(1)
            while jobs_queue.get_job_status(job_id) in ['pending', 'running']:
                time.sleep(1)

        return job_id

    def remove_job_from_pending_queue(self, job_id: int):
        """
        Removes a job from the pending queue based on the provided job ID.

        This method iterates over the jobs in the pending queue and identifies the
        job that matches the given job ID. If the job exists in the queue, it is
        removed, and a debug message is logged. Additionally, an event is streamed
        to indicate the deletion action. If the job is not found, the method returns
        False.

        :param job_id: The ID of the job to be removed.
        :type job_id: int
        :return: A boolean indicating whether the removal was successful. Returns
                 True if the job was removed, otherwise False.
        :rtype: bool
        """
        # The consumer scans pending under this lock, so an edit made without it can end that scan with
        # "deque mutated during iteration". The event goes out after the lock is let go.
        with self._queue_lock:
            job = next((item for item in list(self.jobs_pending_queue)
                        if item.job_id == job_id and item.status == 'pending'), None)
            if job is None:
                return False
            try:
                self.jobs_pending_queue.remove(job)
            except ValueError:
                return False
        logging.debug(f"Task {job.job_name} ({job.job_id}) removed from queue")  # noqa: G004
        _job_event('delete', {"job_id": job.job_id})
        return True

    def move_job_in_pending_queue(self, job_id: int, move_destination: str) -> bool:
        """
        Moves a job within the pending queue to a specified location.

        This method attempts to move a job in the pending queue to either the
        top or bottom of the queue. It identifies the job by its ID and ensures
        that its status is 'pending' before performing the operation.

        :param job_id: The unique identifier of the job to move.
        :type job_id: int
        :param move_destination: Specifies where to move the job in the pending
            queue. Accepted values are 'top' and 'bottom'.
        :type move_destination: str
        :return: A boolean indicating whether the operation was successful.
        :rtype: bool
        """
        with self._queue_lock:
            job = next((item for item in list(self.jobs_pending_queue)
                        if item.job_id == job_id and item.status == 'pending'), None)
            if job is None:
                return False
            if move_destination not in ('top', 'bottom'):
                logging.error(f"Invalid move destination: {move_destination}. Accepted values are 'top' and "  # noqa: G004
                              f"'bottom'")
                return False
            try:
                self.jobs_pending_queue.remove(job)
            except ValueError:
                return False
            except Exception as e:
                logging.exception(f"Unhandled exception while trying to move job {job.job_name} ({job.job_id}) in "  # noqa: G004
                                  f"pending queue: {e}")
                return False
            if move_destination == 'top':
                self.jobs_pending_queue.appendleft(job)
            else:
                self.jobs_pending_queue.append(job)
        logging.debug(f"Task {job.job_name} ({job.job_id}) moved to {move_destination} of the pending "  # noqa: G004
                      f"queue")
        _job_event('update', {"job_id": job.job_id})
        return True

    def cancel_running_job(self, job_id: int) -> bool:
        """
        Requests cancellation of a running job. The job will be aborted on its next
        progress update call.

        :param job_id: The unique identifier of the job to cancel.
        :type job_id: int
        :return: True if the job was found and marked for cancellation, False otherwise.
        :rtype: bool
        """
        for job in self.jobs_running_queue:
            if job.job_id == job_id:
                job.cancelled = True
                logging.info(f"Job {job.job_name} ({job.job_id}) marked for cancellation")  # noqa: G004
                return True
        return False

    def raise_if_cancelled(self, job_id: int) -> None:
        """
        Raises ``JobCancelled`` when the running job was told to stop. It reads the flag and nothing
        else: unlike ``update_job_progress`` it publishes no progress, so work that checks often may
        call it as often as it likes.

        :param job_id: The unique identifier of the running job.
        :type job_id: int
        :raises JobCancelled: If Stop was pressed on the job.
        """
        with self._queue_lock:
            job = next((item for item in self.jobs_running_queue if item.job_id == job_id), None)
            if job is not None and job.cancelled:
                raise JobCancelled(f"Job {job.job_name} ({job.job_id}) was cancelled", job_id=job_id)

    def force_start_pending_job(self, job_id: int, user_start: bool = False) -> bool:
        """
        Forces the execution of a job currently in the pending queue. Only jobs with
        a status of 'pending' will be processed. If a matching job is found and
        successfully initiated, the function returns True. Otherwise, it returns False.

        :param job_id: Identifier of the job to be forcefully started.
        :type job_id: int
        :param user_start: Whether the user chose this start, which makes the job's
            outcome theirs to hear. The job was already queued by whoever first
            scheduled it, so its origin is written here and not at creation. Internal
            callers leave it off and the origin stays as it was.
        :type user_start: bool
        :return: A boolean value indicating whether the job was successfully initiated. False as well when
            the job's lane is full: it stays pending, says so in its progress message, and runs in its turn.
        :rtype: bool
        """
        with self._queue_lock:
            job = next((item for item in self.jobs_pending_queue
                        if item.job_id == job_id and item.status == 'pending'), None)
            if job is None:
                return False

            # Deliberately past the general limit: forcing a job is an explicit
            # user action. Its lane still caps how many run together, and a
            # job over the cap stays pending and runs in its turn. It reserves
            # the same way otherwise, so the job is counted exactly once and
            # never sits in neither queue.
            #
            # The lane's free slots go to the searches that have waited longest first. A press that
            # finds a slot free does not take it from an older search waiting for it: that one starts,
            # and the press joins the line unless the lane has room for it as well.
            if user_start:
                job.origin = 'user'
            starting = self._starting_with(job)
            for item in starting:
                self.jobs_pending_queue.remove(item)
                self.jobs_running_queue.append(item)
                self._clear_lane_wait(item)
            started = job in starting
            waits_for_lane = not started
            if waits_for_lane:
                job.progress_message = LANE_FULL_MESSAGE
                job.lane_waiting = True

        # A press whose own job could not get a thread has failed, which its own event announces.
        for item in starting:
            if not self._start_job_thread(item) and item is job:
                started = False
        if waits_for_lane:
            _job_event('update', {"job_id": job.job_id})
        return started

    def _start_job_thread(self, job) -> bool:
        """Start the thread that runs ``job``, which the caller already moved from pending to running.

        The operating system can refuse a thread. The job then has nothing behind it, and it would sit on
        running holding its general and lane slots for good, so repeated refusals would fill all the
        capacity. It fails instead, with a reason the user can read, and its slots are free again.
        """
        try:
            job_thread = Thread(target=self._run_job, args=(job,))
            job_thread.daemon = True
            job_thread.start()
            return True
        except Exception:
            logging.exception("BAZARR could not start a thread for job %s (id %s)", job.job_name, job.job_id)
            job.error = dict(COULD_NOT_START_JOB_ERROR)
            self._mark_failed(job)
            try:
                _job_event('update', {"job_id": job.job_id, "status": job.status, "progress_value": None,
                                      "error": job.error, "action": job.action})
            except Exception:
                logging.exception("BAZARR could not announce that job %s failed to start", job.job_id)
            return False

    def _starting_with(self, job) -> list:
        """The jobs a press on ``job`` starts now, in pending order, ``job`` among them when it is its turn.

        An unlaned job, or one in a lane with room for it and every search already waiting there, starts
        at once. A lane that cannot take them all starts the waiters in the order the consumer promotes
        them, which is the order of the pending queue: the longest waiter first until someone moves a
        search to the top or the bottom. Call with ``_queue_lock`` held.
        """
        limit = LANE_LIMITS.get(job.lane)
        if limit is None:
            return [job]
        waiting = [item for item in self.jobs_pending_queue
                   if item.lane == job.lane and item.status == 'pending' and (item.lane_waiting or item is job)]
        free = limit - sum(1 for running in self.jobs_running_queue if running.lane == job.lane)
        return waiting[:max(free, 0)]

    def empty_jobs_queue(self, queue_name: str):
        """
        Empties the jobs queue for a specified queue name if it exists among the predefined
        queue categories ('pending', 'failed', 'completed'). Clears all elements within the
        specified queue and indicates success or failure for the operation.

        :param queue_name: The name of the queue to be emptied ('pending', 'failed', or 'completed').
        :type queue_name: str

        :return: A boolean value indicating whether the specified queue was successfully emptied.
        :rtype: bool
        """
        if queue_name in ['pending', 'failed', 'completed']:
            logging.debug(f"Emptying jobs queue for {queue_name} jobs")  # noqa: G004
            with self._queue_lock:
                getattr(self, f'jobs_{queue_name}_queue').clear()
            return True
        return False

    def consume_jobs_pending_queue(self):
        """
        Continuously consumes jobs from the pending jobs queue and processes them by starting a new thread
        for each job, subject to the limit of concurrent jobs allowed in the running queue.

        The function will terminate in response to a KeyboardInterrupt or SystemExit exception. Any other
        error ends only the pass it happened in: this thread is the only one that starts queued jobs, so
        it is logged (with its traceback, at most once a minute) and the loop goes on.

        :raises KeyboardInterrupt: If the execution is interrupted manually.
        :raises SystemExit: If the execution is interrupted by a system exit event.
        """
        last_logged = None
        while True:
            try:
                job = self._reserve_next_job()
                if job is None:
                    sleep(0.5)
                    continue

                self._start_job_thread(job)
            except (KeyboardInterrupt, SystemExit):
                break
            except Exception:
                now = time.monotonic()
                if last_logged is None or now - last_logged >= CONSUMER_ERROR_LOG_SECONDS:
                    last_logged = now
                    logging.exception("BAZARR the jobs queue consumer hit an error and goes on")
                sleep(0.5)

    def _has_capacity_for(self, job) -> bool:
        """Whether ``job`` may start now. Call with ``_queue_lock`` held.

        The general limit applies to every job. The translation lane is a
        sub-limit inside it, not a parallel gate: the settings field says
        "Number of concurrent jobs allowed in the jobs manager", and a
        translation admitted purely against its own lane meant a translation
        plus a general job ran under a configured limit of 1. Which jobs are
        translations is decided by ``is_translation_job``.
        """
        if len(self.jobs_running_queue) >= settings.general.concurrent_jobs:
            return False

        if not is_translation_job(job):
            return True

        running_translations = sum(1 for running in self.jobs_running_queue if is_translation_job(running))
        return running_translations < settings.translator.openrouter_max_concurrent

    def translation_lane_counts(self) -> dict:
        """The translations waiting and the ones holding a lane slot, counted the way the lane counts them."""
        with self._queue_lock:
            return {
                'pending': sum(1 for job in self.jobs_pending_queue if is_translation_job(job)),
                'running': sum(1 for job in self.jobs_running_queue if is_translation_job(job)),
            }

    def _reserve_next_job(self):
        """Take the next runnable job off pending and put it on running, or None.

        One critical section for the whole decision. The check and the act used
        to be separate: capacity was compared under the lock, the lock was
        released, a worker was spawned, and only that worker appended the job to
        the running queue. In between, the job was in neither queue, so the
        consumer's next iteration read a running count that was one too low and
        started another worker. With a limit of 1 and a backlog, the loop has no
        sleep on that path and won the race essentially every time, which is why
        two jobs ran and stayed at two.
        """
        with self._queue_lock:
            if not self.jobs_pending_queue:
                return None

            # A job a person started that waited for its lane goes first once the lane has room.
            # Like any started job it is past the general limit, and the lane cap is its only gate.
            # Scans walk a copy: nothing may end one by changing the queue underneath it.
            pending = list(self.jobs_pending_queue)
            waiting = next((item for item in pending
                            if item.lane_waiting and item.status == 'pending' and self._lane_has_room(item)), None)
            if waiting is not None:
                self.jobs_pending_queue.remove(waiting)
                self.jobs_running_queue.append(waiting)
                self._clear_lane_wait(waiting)
                return waiting

            # The oldest job that may start. A job whose lane is full is passed over, not waited
            # for: the lane caps its own jobs, and library work queued behind it runs meanwhile.
            # Any other job that cannot start holds the line, as it always did.
            for job in pending:
                if not self._lane_has_room(job):
                    continue
                if not self._has_capacity_for(job):
                    return None

                self.jobs_pending_queue.remove(job)
                self.jobs_running_queue.append(job)
                self._clear_lane_wait(job)
                return job
            return None

    def _lane_has_room(self, job) -> bool:
        """Whether the lane ``job`` is counted in can take one more running job. Call with ``_queue_lock`` held."""
        limit = LANE_LIMITS.get(job.lane)
        return limit is None or sum(1 for running in self.jobs_running_queue if running.lane == job.lane) < limit

    @staticmethod
    def _clear_lane_wait(job):
        """Drop the waiting-for-the-lane note once the job starts, so it never reads as still waiting."""
        if job.lane_waiting:
            job.lane_waiting = False
            if job.progress_message == LANE_FULL_MESSAGE:
                job.progress_message = ""

    def _run_job(self, job_instance=None) -> bool:
        """
        Handles the execution of a job from the pending jobs queue or an explicitly provided
        job instance. Manages job state transitions including updating job status, generating
        event streams for job status updates, and handling job results or exceptions.

        :param job_instance: Optional; Specific job instance to execute. If not provided,
            a job will be dequeued from the pending jobs queue.
        :type job_instance: Optional[Job]
        :return: A boolean indicating the success or failure of the job execution. Returns
            True if the job was successfully completed, otherwise False.
        :rtype: bool
        """
        # The caller reserved this job: it is already off pending and on
        # running, which is what keeps the capacity check honest. This method
        # only runs it.
        job = job_instance
        if not job:
            sleep(0.1)
            return False
        try:
            job.status = 'running'
            job.last_run_time = datetime.now(timezone.utc)
            job.observed_started_at = datetime.now(timezone.utc)
            if 'job_id' not in job.kwargs or not job.kwargs['job_id']:
                job.kwargs['job_id'] = job.job_id

            # sending event to update the status of progress jobs
            payload = {"job_id": job.job_id, "status": job.status}
            if job.is_progress:
                payload["progress_value"] = None
                payload["progress_max"] = job.progress_max
                payload["progress_message"] = job.progress_message
            _job_event('update', payload)

            # Never the arguments: they can hold API keys or a whole uploaded
            # package, and an f-string formatted them even with debug off.
            logging.debug("Running job %s (id %s): %s.%s", job.job_name, job.job_id, job.module, job.func)
            
            # Use import lock to prevent deadlocks
            with self._import_lock:
                module = importlib.import_module(job.module)
            
            job.job_returned_value = getattr(module, job.func)(*job.args, **job.kwargs)
        except JobCancelled:
            logging.info(f"Job {job.job_name} ({job.job_id}) was cancelled by user")  # noqa: G004
            # A terminal status of its own: anything reading the API directly
            # used to see a stopped job as completed, and a waiter keyed on
            # completed could count a stop as a finish.
            job.status = 'cancelled'
            job.stopped = True
            job.progress_message = "Cancelled by user"
            job.last_run_time = datetime.now(timezone.utc)
            job.observed_finished_at = datetime.now(timezone.utc)
            activity.finish(job.activity_id, outcome='cancelled')
            with self._queue_lock:
                self.jobs_running_queue.remove(job)
            self.jobs_completed_queue.append(job)
            self._trim_results()
            return False
        except JobFailed as e:
            logging.debug(f"Job {job.job_name} ({job.job_id}) failed: {e}")  # noqa: G004
            job.job_returned_value = e.returned_value
            # The message is whatever the raising code built, often from a
            # provider's own error body, so a credential can be inside it: it
            # is stored on the job, sent over the socket and read by the API,
            # so it is scrubbed once, here, before any of them.
            job.error = {"reason": e.reason, "message": scrub_text(str(e))}
            if e.retryable is not None:
                job.retryable = e.retryable
            self._mark_failed(job)
            return False
        except Exception as e:
            logging.exception(f"Exception raised while running function: {e}")  # noqa: G004
            job.error = dict(UNEXPECTED_JOB_ERROR)
            self._mark_failed(job)
            return False
        else:
            job.status = 'completed'
            job.last_run_time = datetime.now(timezone.utc)
            job.observed_finished_at = datetime.now(timezone.utc)
            # A generic completed envelope is not a publication. Only a typed
            # outcome recorded at a real publication boundary can claim one.
            activity.finish(job.activity_id)
            with self._queue_lock:
                self.jobs_running_queue.remove(job)
            self.jobs_completed_queue.append(job)
            self._trim_results()
            return True
        finally:
            try:
                # Discard any buffered progress for this job so the completion
                # event is always the last thing the client sees for it.
                with self._progress_buffer_lock:
                    self._progress_buffer.pop(job.job_id, None)
                # Send a complete event payload with status and progress_value
                # progress_value being None forces frontend to fetch a full job payload
                payload = {
                    "job_id": job.job_id,
                    "status": job.status,  # 'completed', 'cancelled' or 'failed'
                    "progress_value": None,  # Trigger frontend API call to update the whole job payload
                    "error": job.error,
                    "action": job.action,
                }
                _job_event('update', payload)
            except Exception as e:
                logging.exception(f"Exception raised while sending event: {e}")  # noqa: G004

    def _trim_results(self):
        """Keep a published result on only the most recent ended jobs.

        The history is ``JOB_HISTORY_DEPTH`` deep for each outcome, but a
        result can be the rows of a whole search. Older jobs keep their row in
        the list and lose the rows they once offered.
        """
        with self._queue_lock:
            holders = [job for job in (*self.jobs_failed_queue, *self.jobs_completed_queue)
                       if job.result is not None]
            if len(holders) <= JOB_RESULTS_KEPT:
                return
            holders.sort(key=lambda job: job.observed_finished_at or datetime.min.replace(tzinfo=timezone.utc))
            for job in holders[:-JOB_RESULTS_KEPT]:
                job.result = None

    def _mark_failed(self, job):
        job.status = 'failed'
        job.last_run_time = datetime.now(timezone.utc)
        job.observed_finished_at = datetime.now(timezone.utc)
        activity.finish(job.activity_id, outcome='failed')
        with self._queue_lock:
            self.jobs_running_queue.remove(job)
        self.jobs_failed_queue.append(job)
        self._trim_results()

    def _is_an_existing_job(self, module, func, args, kwargs):
        """
        Checks if a job with matching attributes already exists in pending or running queues.

        :param module: Module name of the job to check.
        :type module: str
        :param func: Function name of the job to check.
        :type func: str
        :param args: Positional arguments of the job to check.
        :type args: list
        :param kwargs: Keyword arguments of the job to check.
        :type kwargs: dict
        :return: True if a matching job exists in pending or running queues, False otherwise.
        :rtype: bool
        """
        return self._find_existing_job(module, func, args, kwargs) is not None

    def _find_existing_job(self, module, func, args, kwargs):
        """The pending or running job with the same module, function and arguments, or None.

        A ``job_id`` in either set of keyword arguments is not part of the comparison.
        """
        cleaned_kwargs = kwargs.copy()
        cleaned_kwargs.pop('job_id', None)
        with (self._queue_lock):
            queues_to_check = list(self.jobs_pending_queue) + list(self.jobs_running_queue)

            for job in queues_to_check:
                cleaned_job_kwargs = job.kwargs.copy()
                cleaned_job_kwargs.pop('job_id', None)
                if (job.module == module and
                        job.func == func and
                        job.args == args and
                        cleaned_job_kwargs == cleaned_kwargs):
                    return job
            return None

    def claim_inline_job(self, module, func, args=None, kwargs=None, job_id=None):
        """Claim an inline run of work the queue would refuse to duplicate.

        A batch job that runs the work itself, inline, publishes the run here
        before it starts. The claim is read in the same look, under the same
        lock, that ``feed_jobs_pending_queue`` makes before queueing, so both
        directions of the race are covered: this batch leaves alone work a job
        another request already queued or claimed, and a request that arrives
        while the batch runs the work follows this job instead of queueing a
        second one.

        :param module: Module the inline work runs from.
        :type module: str
        :param func: Function the inline work runs.
        :type func: str
        :param args: Positional arguments of the work, as the queue would bind them.
        :type args: list
        :param kwargs: Keyword arguments of the work, as the queue would bind them.
        :type kwargs: dict
        :param job_id: The claiming batch job's id.
        :type job_id: int
        :return: The id of the job that owns the work: ``job_id`` when this
            caller claimed it, or the id of the job that already does.
        :rtype: int
        """
        args = list(args or [])
        cleaned_kwargs = dict(kwargs or {})
        cleaned_kwargs.pop('job_id', None)
        with self._queue_lock:
            existing = self._find_existing_job(module, func, args, cleaned_kwargs)
            if existing is not None:
                return existing.job_id
            claim = self._find_inline_claim(module, func, args, cleaned_kwargs)
            if claim is not None:
                return claim['job_id']
            self._inline_claims.append({'module': module, 'func': func, 'args': args,
                                        'kwargs': cleaned_kwargs, 'job_id': job_id})
            return job_id

    def release_inline_job(self, module, func, args=None, kwargs=None):
        """Drop the claim ``claim_inline_job`` recorded for this inline run.

        The batch that claimed the run calls this when the run ends, whether it
        completed, failed or was stopped, so a later request may start the work
        again.

        :param module: Module the inline work runs from.
        :type module: str
        :param func: Function the inline work runs.
        :type func: str
        :param args: Positional arguments of the work, as the queue would bind them.
        :type args: list
        :param kwargs: Keyword arguments of the work, as the queue would bind them.
        :type kwargs: dict
        """
        args = list(args or [])
        cleaned_kwargs = dict(kwargs or {})
        cleaned_kwargs.pop('job_id', None)
        with self._queue_lock:
            for claim in self._inline_claims:
                if (claim['module'] == module and claim['func'] == func and
                        claim['args'] == args and claim['kwargs'] == cleaned_kwargs):
                    self._inline_claims.remove(claim)
                    return

    def _find_inline_claim(self, module, func, args, kwargs):
        """The inline claim for the same work, or None.

        A ``job_id`` in either set of keyword arguments is not part of the
        comparison, exactly as in ``_find_existing_job``.
        """
        cleaned_kwargs = dict(kwargs or {})
        cleaned_kwargs.pop('job_id', None)
        for claim in self._inline_claims:
            if (claim['module'] == module and claim['func'] == func and
                    claim['args'] == list(args or []) and claim['kwargs'] == cleaned_kwargs):
                return claim
        return None


jobs_queue = JobsQueue()

