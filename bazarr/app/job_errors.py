# coding=utf-8
"""Wording for jobs that fail with ``app.jobs_queue.JobFailed``."""


def reason_of(error):
    """The exception's own words, or its type when it has none."""
    return str(error).strip() or error.__class__.__name__


class ArrSyncFailed(Exception):
    """A one-item sync with Sonarr or Radarr that got nothing to sync.

    The webhook and live-feed callers log the cause and carry on. The detail
    page's job asks for this instead, so the drawer shows why nothing changed.
    ``reason`` is the code the failed job carries.
    """

    def __init__(self, message, reason="failed"):
        super().__init__(message)
        self.reason = reason


class ArrSyncSkipped(Exception):
    """A one-item sync with Sonarr or Radarr that was left alone on purpose.

    Raised only for a caller that asked to hear about it (``raise_on_failure``): the detail page's job
    ends as a skip, named for it, instead of reading as a sync that ran. Everyone else gets the quiet
    return they always did.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
