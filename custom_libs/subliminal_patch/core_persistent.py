# coding=utf-8
from __future__ import absolute_import

from collections import defaultdict
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    Future,
    TimeoutError as FTimeout,
)
import logging
import threading
import time
from typing import Dict, Optional, Tuple

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
    fallback_allowed=False,
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
            fallback_allowed=fallback_allowed,
        )
        logger.info("Downloaded %d subtitle(s)", len(subtitles))
        downloaded_subtitles[video].extend(subtitles)

    return downloaded_subtitles


# ---- Shared bounded executor for compat fanout ----
#
# A single process-wide ThreadPoolExecutor is shared across every
# compat fanout. The previous design created (and abandoned) a fresh
# executor per request, which under repeated timed-out searches would
# accumulate background threads without any global cap. Sharing one
# bounded pool gives us:
#   - hard ceiling on total live provider threads
#   - back-pressure when many fanouts time out at once: new submissions
#     queue inside the pool instead of spawning more OS threads
#   - observable abandoned-future count
# Workers whose providers blocked on network I/O still finish
# out-of-band because Python cannot cancel a blocking requests.get().
# What changed is that they now finish inside this bounded pool.

_DEFAULT_MAX_WORKERS = 32
_DEFAULT_MAX_CONCURRENT_FANOUTS = 4

_pool_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None
_fanout_sem: Optional[threading.Semaphore] = None
_pool_max_workers = _DEFAULT_MAX_WORKERS
_pool_max_fanouts = _DEFAULT_MAX_CONCURRENT_FANOUTS

_abandoned_lock = threading.Lock()
_abandoned: Dict[Future, Tuple[str, float]] = {}
_abandoned_total = 0  # all-time counter, for diagnostics


def _read_settings() -> Tuple[int, int]:
    """Pool-sizing knobs from settings, with defensive fallbacks."""
    try:
        from app.config import settings
        max_workers = max(4, int(getattr(
            settings.compat_endpoint, "fanout_max_workers",
            _DEFAULT_MAX_WORKERS,
        )))
        max_fanouts = max(1, int(getattr(
            settings.compat_endpoint, "max_concurrent_fanouts",
            _DEFAULT_MAX_CONCURRENT_FANOUTS,
        )))
    except Exception:
        return _DEFAULT_MAX_WORKERS, _DEFAULT_MAX_CONCURRENT_FANOUTS
    return max_workers, max_fanouts


def _get_pool() -> Tuple[ThreadPoolExecutor, threading.Semaphore]:
    """Lazy-init the shared executor and concurrency semaphore."""
    global _executor, _fanout_sem, _pool_max_workers, _pool_max_fanouts
    with _pool_lock:
        if _executor is None:
            _pool_max_workers, _pool_max_fanouts = _read_settings()
            _executor = ThreadPoolExecutor(
                max_workers=_pool_max_workers,
                thread_name_prefix="compat-fanout",
            )
            _fanout_sem = threading.Semaphore(_pool_max_fanouts)
            logger.info(
                "compat fanout pool initialised: max_workers=%d "
                "max_concurrent_fanouts=%d",
                _pool_max_workers, _pool_max_fanouts,
            )
        return _executor, _fanout_sem


def _reap_abandoned() -> None:
    """Drop completed abandoned futures from the registry. The pool
    keeps internal refs to running futures; this just prevents the
    diagnostics dict from growing unboundedly."""
    with _abandoned_lock:
        done = [f for f in _abandoned if f.done()]
        for f in done:
            _abandoned.pop(f, None)


def fanout_stats() -> dict:
    """Snapshot for tests and diagnostics."""
    _reap_abandoned()
    with _abandoned_lock:
        pending = len(_abandoned)
    with _pool_lock:
        max_workers = _pool_max_workers
        max_fanouts = _pool_max_fanouts
        live = _executor is not None
    return {
        "live": live,
        "max_workers": max_workers,
        "max_concurrent_fanouts": max_fanouts,
        "abandoned_pending": pending,
        "abandoned_total": _abandoned_total,
    }


def reset_pool() -> None:
    """Tear down the shared executor and clear the abandoned registry.
    Intended for tests and for code paths that change the pool's sizing
    knobs (config save). New fanouts after reset will lazily re-init.

    A concurrent fanout that already captured a reference to the old
    executor before reset can still submit work to it briefly; the
    submission path uses _safe_submit() which retries against a fresh
    pool if it hits the executor-after-shutdown RuntimeError."""
    global _executor, _fanout_sem, _abandoned_total
    with _pool_lock:
        ex = _executor
        _executor = None
        _fanout_sem = None
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)
    with _abandoned_lock:
        _abandoned.clear()
        _abandoned_total = 0


def _safe_submit(executor: ThreadPoolExecutor, fn, *args, **kwargs):
    """Submit fn to the captured executor; if it was shutdown by a
    concurrent reset_pool() (config change), grab a fresh executor and
    retry once. Returning the Future as if nothing happened keeps the
    fanout's as_completed() loop oblivious to the swap; futures from
    two pools mix fine since as_completed accepts an arbitrary set."""
    try:
        return executor.submit(fn, *args, **kwargs)
    except RuntimeError:
        # Most likely "cannot schedule new futures after shutdown".
        # Reload and retry exactly once.
        fresh, _ = _get_pool()
        return fresh.submit(fn, *args, **kwargs)


def list_all_subtitles_parallel(videos, languages, pool_instance,
                                 per_provider_timeout: int = 5,
                                 wall_timeout: int = 8,
                                 exclude_providers=None,
                                 on_result=None):
    """Parallel fanout with a hard wall-clock timeout, sharing one
    bounded executor process-wide.

    Used exclusively by the compat endpoint. DO NOT replace
    list_subtitles_prioritized for existing code paths.

    Contract:
      - Submits every non-excluded provider to the shared compat-fanout
        ThreadPoolExecutor (bounded; see module docstring).
      - Iterates `as_completed(futures, timeout=wall_timeout)` so the
        wall is enforced by cpython itself, not by a per-iteration check.
      - On wall expiry, any future that finished before the wall but was
        not yet yielded is harvested (no results dropped); futures still
        running are reported as "abandoned" via on_result and registered
        in the module-level abandoned set so they remain observable.
      - Concurrency cap: a process-wide semaphore allows at most
        max_concurrent_fanouts simultaneous fanouts. If the cap is hit
        for longer than wall_timeout, the request degrades to "no
        results" rather than spawning more background work.

    Invariant (load-bearing): `out[video].extend(...)` is called ONLY
    from the calling thread, never from a worker thread or done-callback.
    The dogpile.cache.region write path reads `out` after this function
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
    global _abandoned_total
    exclude = set(exclude_providers or ())
    out = defaultdict(list)
    if not videos:
        return out
    providers = [p for p in pool_instance.providers
                 if p not in getattr(pool_instance, "discarded_providers", set())
                 and p not in exclude]
    if not providers:
        return out

    executor, sem = _get_pool()
    _reap_abandoned()

    # The wall_timeout is the contract caller's hard budget for the
    # whole fanout. Track a deadline so the semaphore wait, future
    # submission, and as_completed iteration ALL fit inside it. Without
    # this, a saturated semaphore could double the effective wall.
    deadline = time.monotonic() + float(wall_timeout)

    if not sem.acquire(timeout=float(wall_timeout)):
        logger.warning(
            "compat fanout: dropped (concurrency cap reached, wall=%ds)",
            wall_timeout,
        )
        return out

    try:
        for video in videos:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Sem wait consumed the entire budget. Skip the fanout
                # rather than start work the caller no longer has time
                # to wait for.
                logger.warning(
                    "compat fanout: dropped (no budget left after "
                    "concurrency wait, wall=%ds)",
                    wall_timeout,
                )
                return out

            futures: Dict[Future, str] = {}
            start_times: Dict[Future, float] = {}
            for p in providers:
                fut = _safe_submit(executor,
                                    pool_instance.list_subtitles_provider,
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
                for fut in as_completed(futures, timeout=remaining):
                    processed.add(fut)
                    _ingest(fut, futures[fut],
                             time.monotonic() - start_times[fut])
            except FTimeout:
                pass
            finally:
                # Three groups left:
                #   1. futures that finished but weren't yielded before
                #      the wall - harvest their results.
                #   2. futures still queued (never picked up by a worker
                #      because the shared pool was saturated) - cancel
                #      them so no provider work runs after the caller
                #      has returned. cancel() returns True only when the
                #      future hadn't started yet.
                #   3. futures already running - cannot be cancelled.
                #      Report abandoned and register them so they're
                #      observable. They keep running in the shared pool;
                #      max_workers caps the total background work
                #      regardless of how many fanouts have been
                #      abandoned.
                now = time.monotonic()
                still_running = []
                for fut, name in futures.items():
                    if fut in processed:
                        continue
                    latency_s = now - start_times[fut]
                    if fut.done() and not fut.cancelled():
                        _ingest(fut, name, latency_s)
                    elif fut.cancel():
                        # Was queued, not running. No work happened, no
                        # work will happen. Still report as abandoned
                        # for the health tracker - a queued provider
                        # that never got CPU time is just as useless to
                        # the caller as one that timed out.
                        _emit(name, "abandoned", latency_s)
                    else:
                        _emit(name, "abandoned", latency_s)
                        still_running.append((fut, name))
                if still_running:
                    with _abandoned_lock:
                        for fut, name in still_running:
                            _abandoned[fut] = (name, now)
                        _abandoned_total += len(still_running)
    finally:
        sem.release()
    return out
