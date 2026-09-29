# coding=utf-8

"""Enqueue a job, or hand back the one already doing the same work.

The jobs queue refuses a job whose module, function and arguments match one that
is pending or running. A caller that has to tell the user which job to follow
needs that job's id, which the queue returns when asked.
"""

from app.jobs_queue import jobs_queue


def enqueue_or_existing(job_name, module, func, kwargs, **options):
    """Queue ``module.func(**kwargs)`` and return its job id.

    When the same call is already pending or running, return that job's id instead
    of starting a second one. The queue reads it in the same step that matches it,
    so the id is there even when that job finishes a moment later, and the caller
    finds its outcome on it.
    """
    return jobs_queue.feed_jobs_pending_queue(job_name=job_name, module=module, func=func,
                                              kwargs=kwargs, return_existing=True, **options)
