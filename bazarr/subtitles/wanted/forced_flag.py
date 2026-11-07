# coding=utf-8
# fmt: off

"""Run the forced duplicate search for a flagged episode and drop the request once it is answered.

The runs that consume the request (the scheduled wanted search, "Search All" and the series "Search")
hand a flagged episode to :func:`run_flagged_search` instead of searching it the normal way. The
request goes away only when the search reports every language as answered. A failure, a provider that
could not answer, or a stopped job leaves it in place for the next run.
"""

import logging

from arr_instances.resolution import scoped
from app.database import TableEpisodes, database, update
from app.event_handler import event_stream
from app.jobs_queue import JobCancelled

from ..manual import _JobStop
from .forced import run_forced_search


def clear_forced_search_request(episode):
    """Drop the request this run consumed.

    The write only matches the request the run started from. If the user withdrew it and asked again
    while the search ran, the newer request is a different one and stays.
    """
    requested_at = getattr(episode, 'forced_search_requested_at', None)
    if requested_at is None:
        return
    arr_instance_id = getattr(episode, 'arr_instance_id', None)
    database.execute(
        scoped(update(TableEpisodes)
               .values(forced_search_requested_at=None)
               .where(TableEpisodes.sonarrEpisodeId == episode.sonarrEpisodeId,
                      TableEpisodes.forced_search_requested_at == requested_at),
               TableEpisodes.arr_instance_id, arr_instance_id))
    event_stream(type='series', action='update', payload=episode.sonarrSeriesId)


def run_flagged_search(episode, job_id=None):
    """Run the forced search for a flagged episode and clear the request when it is complete.

    Returns the :class:`~subtitles.wanted.forced.ForcedSearchResult`, or ``None`` when the search raised.
    A stop (``JobCancelled``) propagates with the request still set. Any other error is logged and also
    leaves the request set, so one broken episode does not end the run for the rest.
    """
    try:
        result = run_forced_search(episode, job_id=job_id, cancel=_JobStop(job_id) if job_id else None)
    except JobCancelled:
        raise
    except Exception:
        logging.exception('BAZARR forced search failed for %s; the request stays set',
                          getattr(episode, 'path', episode))
        return None
    if result.complete:
        clear_forced_search_request(episode)
    else:
        logging.info('BAZARR forced search for %s is not complete; the request stays set',
                     getattr(episode, 'path', episode))
    return result
