# coding=utf-8
"""Wording for jobs that fail with ``app.jobs_queue.JobFailed``."""


def reason_of(error):
    """The exception's own words, or its type when it has none."""
    return str(error).strip() or error.__class__.__name__
