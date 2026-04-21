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
                                 per_provider_timeout: int = 12,
                                 wall_timeout: int = 20):
    """Parallel fanout with per-provider + wall timeouts.

    Unlike list_subtitles_prioritized, this does NOT early-stop. Every enabled
    provider is queried; slow providers are cancelled at per_provider_timeout
    without holding back the wall_timeout.

    Used exclusively by the compat endpoint. DO NOT replace list_subtitles_prioritized
    for existing code paths.
    """
    out = defaultdict(list)
    if not videos:
        return out
    providers = [p for p in pool_instance.providers
                 if p not in getattr(pool_instance, "discarded_providers", set())]
    for video in videos:
        with ThreadPoolExecutor(max_workers=max(1, len(providers))) as ex:
            futures = {ex.submit(pool_instance.list_subtitles_provider,
                                 p, video, languages): p for p in providers}
            deadline = time.monotonic() + wall_timeout
            try:
                for fut in as_completed(futures):
                    try:
                        remaining = max(0.1, deadline - time.monotonic())
                        if remaining <= 0:
                            break
                        subs = fut.result(timeout=min(per_provider_timeout, remaining))
                        if subs:
                            out[video].extend(subs)
                    except FTimeout:
                        continue
                    except Exception:
                        continue
            except FTimeout:
                pass
    return out
