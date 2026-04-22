# coding=utf-8
from __future__ import absolute_import

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FTimeout
import logging
import time

from subliminal.core import check_video

logger = logging.getLogger(__name__)

# list_all_subtitles, list_supported_languages, list_supported_video_types, download_subtitles, download_best_subtitles
def list_all_subtitles(videos, languages, pool_instance, min_score=0):
    listed_subtitles = defaultdict(list)

    # return immediatly if no video passed the checks
    if not videos:
        return listed_subtitles

    for video in videos:
        logger.info("Listing subtitles for %r", video)
        subtitles = pool_instance.list_subtitles_prioritized(
            video, languages - video.subtitle_languages, min_score=min_score
        )
        listed_subtitles[video].extend(subtitles)
        logger.info("Found %d subtitle(s)", len(subtitles))

    return listed_subtitles


def list_supported_languages(pool_instance):
    return pool_instance.list_supported_languages()


def list_supported_video_types(pool_instance):
    return pool_instance.list_supported_video_types()


def download_subtitles(subtitles, pool_instance):
    for subtitle in subtitles:
        logger.info("Downloading subtitle %r with score %s", subtitle, subtitle.score)
        pool_instance.download_subtitle(subtitle)


def download_best_subtitles(
    videos,
    languages,
    pool_instance,
    min_score=0,
    hearing_impaired=False,
    only_one=False,
    use_original_format=False,
    use_provider_priority=True,
    **kwargs
):
    downloaded_subtitles = defaultdict(list)

    # check videos
    checked_videos = []
    for video in videos:
        if not check_video(video, languages=languages, undefined=only_one):
            logger.info("Skipping video %r", video)
            continue
        checked_videos.append(video)

    # return immediately if no video passed the checks
    if not checked_videos:
        return downloaded_subtitles

    # download best subtitles
    for video in checked_videos:
        logger.info("Downloading best subtitles for %r", video)
        if use_provider_priority:
            listed = pool_instance.list_subtitles_prioritized(video, languages - video.subtitle_languages, min_score=min_score)
        else:
            listed = pool_instance.list_subtitles(video, languages - video.subtitle_languages)
        subtitles = pool_instance.download_best_subtitles(
            listed,
            video,
            languages,
            min_score=min_score,
            hearing_impaired=hearing_impaired,
            only_one=only_one,
            use_original_format=use_original_format,
        )
        logger.info("Downloaded %d subtitle(s)", len(subtitles))
        downloaded_subtitles[video].extend(subtitles)

    return downloaded_subtitles


def list_all_subtitles_parallel(videos, languages, pool_instance,
                                 per_provider_timeout: int = 5,
                                 wall_timeout: int = 8,
                                 exclude_providers=None,
                                 on_result=None):
    """Parallel fanout with a hard wall-clock timeout.

    Used exclusively by the compat endpoint. DO NOT replace
    list_subtitles_prioritized for existing code paths.

    Contract:
      - Submits every non-excluded provider to a ThreadPoolExecutor.
      - Iterates `as_completed(futures, timeout=wall_timeout)` so the wall
        is enforced by cpython itself, not by a per-iteration check.
      - On wall expiry, any future that finished before the wall but was
        not yet yielded is harvested (no results dropped); futures still
        running are reported as "abandoned" - the caller decides what
        that means (see provider_health).
      - `ex.shutdown(wait=False, cancel_futures=True)` lets this function
        return on time even when some provider threads are still blocked
        on network I/O. Those threads finish out-of-band because Python
        cannot cancel a blocking `requests.get()` call.

    Invariant (load-bearing): `out[video].extend(...)` is called ONLY from
    the calling thread, never from a worker thread or done-callback. The
    dogpile.cache.region write path reads `out` after this function
    returns; if a worker mutated it concurrently, the cached dict would
    grow after the cache entry was frozen. Do not refactor to
    add_done_callback(...) writing to `out`.

    Args:
      per_provider_timeout: latency threshold above which a successful
        provider is labeled "slow" via on_result. Advisory only - does
        not cut providers off (the wall does that).
      wall_timeout: hard budget for the whole fanout, in seconds.
      exclude_providers: iterable of provider names to skip entirely.
      on_result: optional callable ``(name, outcome, latency_ms) -> None``
        invoked exactly once per non-excluded provider. Outcomes:
        ``"ok"`` (returned within per_provider_timeout),
        ``"slow"`` (returned, but over threshold),
        ``"exception"`` (raised),
        ``"abandoned"`` (wall fired before completion).
    """
    exclude = set(exclude_providers or ())
    out = defaultdict(list)
    if not videos:
        return out
    providers = [p for p in pool_instance.providers
                 if p not in getattr(pool_instance, "discarded_providers", set())
                 and p not in exclude]
    for video in videos:
        ex = ThreadPoolExecutor(max_workers=max(1, len(providers)))
        futures = {}
        start_times = {}
        for p in providers:
            fut = ex.submit(pool_instance.list_subtitles_provider,
                            p, video, languages)
            futures[fut] = p
            start_times[fut] = time.monotonic()

        slow_threshold = float(per_provider_timeout)
        processed = set()

        def _emit(name, outcome, latency_s):
            if on_result is not None:
                try:
                    on_result(name, outcome, int(latency_s * 1000))
                except Exception:
                    logger.debug("on_result callback raised", exc_info=True)

        def _ingest(fut, name, latency_s):
            # Process a done future: record outcome, extend `out` on success.
            try:
                result = fut.result(timeout=0)
            except Exception:
                _emit(name, "exception", latency_s)
                return
            if isinstance(result, tuple) and len(result) == 2:
                _, subs = result
            else:
                subs = result
            _emit(name, "slow" if latency_s > slow_threshold else "ok", latency_s)
            if subs:
                out[video].extend(subs)

        try:
            try:
                for fut in as_completed(futures, timeout=wall_timeout):
                    processed.add(fut)
                    _ingest(fut, futures[fut],
                             time.monotonic() - start_times[fut])
            except FTimeout:
                pass
        finally:
            # Two groups left:
            #   1. futures that finished but weren't yielded before the
            #      wall - harvest their results.
            #   2. futures still running - report abandoned.
            now = time.monotonic()
            for fut, name in futures.items():
                if fut in processed:
                    continue
                latency_s = now - start_times[fut]
                if fut.done() and not fut.cancelled():
                    _ingest(fut, name, latency_s)
                else:
                    _emit(name, "abandoned", latency_s)
            # cancel_futures=True drains queued tasks that never started.
            # wait=False returns immediately; running workers finish in
            # the background (see module docstring for why this is safe).
            ex.shutdown(wait=False, cancel_futures=True)
    return out
