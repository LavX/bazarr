# coding=utf-8
"""Wording shared by subtitle jobs that fail with ``app.jobs_queue.JobFailed``."""

# How many failed item names a batch failure spells out before "and N more".
FAILURE_NAMES_SHOWN = 3


def describe_failures(failures, shown=FAILURE_NAMES_SHOWN):
    """The first few failed items of a batch, for the job's failure reason."""
    text = '; '.join(failures[:shown])
    more = len(failures) - shown
    if more > 0:
        text += f'; and {more} more'
    return text
