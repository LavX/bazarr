# coding=utf-8
"""Failing a queued job with a reason the user can read.

The jobs queue marks a job failed only when its function raises. What it
raised is logged, but the job itself keeps only its status, so a failure in
the jobs drawer said "failed" and nothing else. Writing the reason into the
job's progress message as well puts the sentence next to the job it belongs
to, and the exception carries the same sentence for the log and for anything
that reads the failure off the exception.
"""

from app.jobs_queue import jobs_queue


class JobFailed(RuntimeError):
    """A job failed for a reason stated in its message."""


def fail_job(job_id, message, cause=None):
    """Record ``message`` on the running job, then raise it.

    Never returns. ``cause`` is chained so the log keeps the original trace.
    """
    if job_id:
        try:
            # allow_cancelled: this reports the outcome of work that already
            # happened, so a cancel request arriving meanwhile must not turn
            # the reason into a JobCancelled.
            jobs_queue.update_job_progress(job_id=job_id, progress_message=message,
                                           allow_cancelled=True)
        except Exception:
            pass
    raise JobFailed(message) from cause


def reason_of(error):
    """The exception's own words, or its type when it has none."""
    return str(error).strip() or error.__class__.__name__
