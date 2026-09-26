# coding=utf-8

"""Enqueue a job, or hand back the one already doing the same work.

The jobs queue refuses a job whose module, function and arguments match one that
is pending or running, but it answers only False, so a caller that has to tell the
user which job to follow would lose the id. This finds it.
"""

from app.jobs_queue import jobs_queue


def _without_job_id(kwargs):
    cleaned = dict(kwargs or {})
    cleaned.pop('job_id', None)
    return cleaned


def active_job_id(module, func, kwargs):
    """The id of a pending or running job for exactly this call, else None."""
    wanted = _without_job_id(kwargs)
    for status in ('running', 'pending'):
        for job in jobs_queue.list_jobs_from_queue(status=status):
            if (job.get('module') == module and job.get('func') == func
                    and _without_job_id(job.get('kwargs')) == wanted):
                return job.get('job_id')
    return None


def enqueue_or_existing(job_name, module, func, kwargs, **options):
    """Queue ``module.func(**kwargs)`` and return its job id.

    When the same call is already pending or running, return that job's id instead
    of starting a second one. None means the duplicate finished between the two
    looks, so the caller should read whatever that job left behind.
    """
    job_id = jobs_queue.feed_jobs_pending_queue(job_name=job_name, module=module, func=func,
                                                kwargs=kwargs, **options)
    if job_id:
        return job_id
    return active_job_id(module, func, kwargs)
