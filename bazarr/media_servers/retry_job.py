# coding=utf-8
"""A manual "Retry pending" as a standard job.

The dispatcher keeps the work that is waiting for a destination and drains it
on its own worker. Automatic refreshes stay there. A person pressing Retry
pending is waiting for an answer, though, and the answer used to be a status
card that was read every ten seconds. This queues the press as a job on the
application's queue: it hands the pending refreshes back to the dispatcher,
reports how many it has drained as it goes, and fails with the reason the
destination gave when some of them could not be confirmed.
"""

import logging
from threading import Thread

from app.jobs_queue import JobFailed, jobs_queue

from .dispatcher import _get_dispatcher, get_refresh_status

_GENERIC = "Check the saved connection, server access and path mappings or selected libraries."
_REASONS = {
    "queue_overflow": "The refresh queue overflowed. The refreshes it kept were retried, but the ones it dropped "
                      "need a new refresh.",
    "sidecar_unsupported": "Silo only refreshes subtitles stored beside the video file. Move the subtitle there, "
                           "then retry.",
    "library_missing": "This destination has no library selected for that kind of media, so there is nothing to "
                       "scan. Choose one and retry.",
    "configuration_changed": "The destination's settings changed while it was refreshing. Save them, then retry.",
    "connection_disabled": "This destination is turned off. Turn it on and save, then retry.",
}


def queue_retry_pending(instance_id, name=None):
    """Queue the retry of one destination's pending refreshes and start it at once.

    Pressing it again while the retry runs follows that job.

    :return: The job id.
    """
    job_id = jobs_queue.feed_jobs_pending_queue(
        job_name=f"Retrying pending refreshes for {name or 'a media server'}", module=__name__,
        func="retry_pending_job", kwargs={"instance_id": instance_id, "name": name}, is_progress=True,
        progress_max=1, retryable=True, return_existing=True)
    Thread(target=jobs_queue.force_start_pending_job, args=(job_id, True), daemon=True).start()
    return job_id


def _refreshed(total, status):
    return max(0, min(total, total - status["pending"]))


def retry_pending_job(instance_id, name=None, job_id=None):
    """The job body: retry, then follow the dispatcher's worker until it has drained."""
    label = name or "this destination"
    dispatcher = _get_dispatcher()
    total = dispatcher.retry(instance_id)
    if total == 0:
        status = get_refresh_status(instance_id)
        if status["pending"]:
            # Something is waiting, but the destination cannot be asked now.
            _fail(label, status["error_code"] or "connection_disabled")
        jobs_queue.update_job_progress(job_id=job_id, progress_value="max", allow_cancelled=True,
                                       progress_message=f"Nothing was waiting for {label}")
        return {"retried": 0}
    jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=total,
                                   progress_message=f"{label}: 0 of {total} refreshed")
    # Stop ends the wait, not the dispatcher's worker: a refresh already sent
    # to the server cannot be called back.
    while not dispatcher.wait_idle(1.0, server=instance_id):
        status = get_refresh_status(instance_id)
        refreshed = _refreshed(total, status)
        jobs_queue.update_job_progress(job_id=job_id, progress_value=refreshed, progress_max=total,
                                       progress_message=f"{label}: {refreshed} of {total} refreshed")
    status = get_refresh_status(instance_id)
    if status["pending"] or status["error_code"] or status["state"] == "unconfirmed":
        _fail(label, status["error_code"] or "internal_error", refreshed=_refreshed(total, status), total=total)
    jobs_queue.update_job_progress(job_id=job_id, progress_value="max", allow_cancelled=True,
                                   progress_message=f"{label}: {total} of {total} refreshed")
    return {"retried": total}


def _fail(label, code, refreshed=0, total=0):
    logging.warning("Retry pending refreshes failed: reason=%s refreshed=%s of %s", code, refreshed, total)
    progress = f" {refreshed} of {total} were refreshed." if total else ""
    raise JobFailed(f"{label} could not confirm every refresh.{progress} {_REASONS.get(code, _GENERIC)}",
                    reason=code)
