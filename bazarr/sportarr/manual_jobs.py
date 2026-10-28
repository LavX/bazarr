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
from threading import Thread

from app import activity
from app.database import TableSportsEvents, database
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue

# Same sentence the route used for a failure that carried no reason.
DOWNLOAD_FALLBACK = "Subtitle was not published. Check the file and provider before trying again."


def queue_sports_search(event_id, language, hi, forced, arr_instance_id):
    """Queue the manual search for a sports event as a job and start it at once.

    The caller has already answered what it can at once: an unknown event or a
    language that is not one. What fails after this is the search itself, and
    the job says why. A person pressing Search is waiting, so the job does not
    queue behind library work, and pressing it again for the same search
    follows the one that is running.

    :return: The job id.
    """
    row = database.get(TableSportsEvents, event_id)
    title = getattr(row, "title", None) or f"sports event {event_id}"
    job_id = jobs_queue.feed_jobs_pending_queue(
        job_name=f"Searching subtitles for {title}", module=__name__, func="run_sports_search_job",
        kwargs={"event_id": event_id, "language": language, "hi": hi, "forced": forced,
                "arr_instance_id": arr_instance_id},
        is_progress=True, progress_max=2, retryable=True, return_existing=True)
    Thread(target=jobs_queue.force_start_pending_job, args=(job_id, True), daemon=True).start()
    return job_id


# Same sentence the route used for a failure that carried no reason.
SEARCH_FALLBACK = "Could not search this sports file. Check its accessibility and providers."


def run_sports_search_job(event_id, language, hi, forced, arr_instance_id=None, job_id=None):
    """The job body: search the providers for one sports file and publish the rows."""
    from app.jobs_queue import JobCancelled, JobFailed
    from sportarr.errors import SportsNotFound
    from sportarr.subtitles import manual_search_sports
    from sportarr.workflows import SportsJobSignal
    from subtitles.search_result import marshal_search_results

    activity.register(activity.activity_id_for_job(job_id), operation='manual_search', scope_kind='media',
                      media_type='sports')
    jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=2,
                                   progress_message="Searching the providers")
    try:
        # The job's own signal, so Stop reaches the provider wait instead of
        # only flagging a job that never looks.
        found = manual_search_sports(event_id, language, hi, forced, arr_instance_id,
                                     cancel=SportsJobSignal(arr_instance_id, job_id), observed=False)
    except JobCancelled:
        raise
    except (SportsNotFound, ValueError) as error:
        # The event or the language went away between the click and the job.
        logging.warning("Sports search failed: reason=invalid_request event=%s: %s", event_id, error)
        raise JobFailed(str(error).strip() or SEARCH_FALLBACK, reason="invalid_request", retryable=False) from error
    except Exception as error:
        # manual_search answers with the failing stage as a sentence and the
        # sports layer re-raises it as the exception's argument, so the
        # exception already says what to do.
        logging.warning("Sports search failed: reason=search_failed event=%s: %s", event_id, error)
        raise JobFailed(str(error).strip() or SEARCH_FALLBACK, reason="search_failed") from error
    jobs_queue.update_job_progress(job_id=job_id, progress_value=1,
                                   progress_message=f"Preparing {len(found)} result(s)")
    jobs_queue.publish_job_result(job_id, marshal_search_results(found))
    jobs_queue.update_job_progress(job_id=job_id, progress_value="max", allow_cancelled=True,
                                   progress_message=f"{len(found)} subtitle(s) found")


def sports_manually_download_subtitle(event_id, candidate, arr_instance_id=None, job_id=None):
    if not job_id:
        return jobs_queue.add_job_from_function("Manually downloading Subtitles", is_progress=False)

    from app.jobs_queue import JobCancelled, JobFailed
    from sportarr import library
    from sportarr.subtitles import manual_download_sports
    from sportarr.workflows import SportsJobSignal

    row = database.get(TableSportsEvents, event_id)
    title = getattr(row, "title", None) or f"sports event {event_id}"
    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Manually downloading Subtitles for {title}")
    try:
        # The job's own signal, so Stop reaches the download and its
        # publication instead of only flagging a job that never looks.
        result = manual_download_sports(event_id, candidate, arr_instance_id,
                                        cancel=SportsJobSignal(arr_instance_id, job_id))
    except JobCancelled:
        raise
    except Exception as error:
        # manual_download_subtitle answers with the failing stage as a
        # sentence and the sports layer re-raises it as the exception's
        # argument, so the exception already says what to do.
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Failed to download Subtitles for {title}")
        raise JobFailed(str(error).strip() or DOWNLOAD_FALLBACK) from error

    publication = result.publication if isinstance(result.publication, dict) else {}
    if publication.get("status") == "published_with_warnings":
        # Published, so not a failed download, but part of the work after the
        # write did not finish. The warning names what, and what to do, so it
        # goes on the job where the drawer shows it.
        jobs_queue.update_job_name(job_id=job_id,
                                   new_job_name=f"Downloaded Subtitles with warnings for {title}")
        if publication.get("message"):
            jobs_queue.update_job_progress(job_id=job_id, progress_message=publication["message"],
                                           allow_cancelled=True)
    else:
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
