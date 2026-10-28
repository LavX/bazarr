# coding=utf-8
import logging

from app.job_errors import reason_of
from app.jobs_queue import JobFailed, jobs_queue

from .jobs import failed_catalog_sources
from .service import check_updates, refresh_catalog


def provider_hub_check_updates(job_id=None, wait_for_completion=False):
    """Refresh the catalog and look for provider updates, as a standard job.

    The scheduler's thread and the Tasks page's Run now both call this without
    a job: it queues itself and the queue runs it again with one, so the check
    shows in the Jobs list and a failure reaches the drawer with its reason.
    """
    if not job_id:
        return jobs_queue.add_job_from_function("Checking Provider Hub updates", is_progress=False,
                                                wait_for_completion=wait_for_completion)

    refresh_failure = None
    try:
        refresh_catalog()
        # A source that cannot be fetched is recorded on it and the refresh
        # carries on, so the check still runs against what the others offered.
        failed = failed_catalog_sources()
        if failed:
            refresh_failure = f"Could not refresh the provider catalog from {', '.join(failed)}"
    except Exception as error:
        logging.exception("Provider Hub catalog refresh failed")
        refresh_failure = f"Could not refresh the provider catalog: {reason_of(error)}"
    try:
        outcome = check_updates()
    except Exception as error:
        logging.exception("Provider Hub update check failed")
        raise JobFailed(f"Could not check provider updates: {reason_of(error)}", reason="update_check_failed") from error
    if refresh_failure:
        logging.warning("Provider Hub update check failed: reason=catalog_refresh_failed: %s", refresh_failure)
        raise JobFailed(refresh_failure, reason="catalog_refresh_failed")
    message = (outcome or {}).get("message")
    if message:
        jobs_queue.update_job_progress(job_id=job_id, progress_message=message, allow_cancelled=True)
    return outcome
