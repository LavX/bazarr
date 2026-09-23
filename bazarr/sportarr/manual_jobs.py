# coding=utf-8
"""A sports manual download as a queued job.

The library's manual download has run on the jobs queue for a long time
(subtitles/manual.py). The sports one ran inside its request instead, holding
it for the whole provider download and publication. This mirrors the library
pattern: the request validates what it can answer at once and queues the rest,
and the job raises with the reason when the download or its publication
fails, which is what marks it failed.
"""

import logging

from app.database import TableSportsEvents, database
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue

# Same sentence the route used for a failure that carried no reason.
DOWNLOAD_FALLBACK = "Subtitle was not published. Check the file and provider before trying again."


def sports_manually_download_subtitle(event_id, candidate, arr_instance_id=None, job_id=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Manually downloading Subtitles", is_progress=False)

    from app.job_errors import fail_job
    from sportarr import library
    from sportarr.subtitles import manual_download_sports

    row = database.get(TableSportsEvents, event_id)
    title = getattr(row, "title", None) or f"sports event {event_id}"
    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloading Subtitles for {title}")
    try:
        result = manual_download_sports(event_id, candidate, arr_instance_id)
    except Exception as error:
        # manual_download_subtitle answers with the failing stage as a
        # sentence and the sports layer re-raises it as the exception's
        # argument, so the exception already says what to do.
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Failed to download Subtitles for {title}")
        fail_job(job_id, str(error).strip() or DOWNLOAD_FALLBACK, error)

    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloaded Subtitles for {title}")
    try:
        event = library.get_event(database, event_id, arr_instance_id)
    except Exception:
        # Publication already happened. A later owner or read failure must not
        # turn the job into an ordinary failed download.
        logging.debug("BAZARR could not read sports event %s back after a manual download", event_id,
                      exc_info=True)
        event = None
    event_stream(type='sports', action='update', payload=event_id)
    event_stream(type='badges')
    return {"event": event, "publication": result.publication}
