# coding=utf-8

import logging
from dataclasses import dataclass

from .composer import compose
from .naming import compose_combined_filename
from .rules import resolve_source_paths


@dataclass(frozen=True)
class CombineResult:
    status: str            # "built" | "skipped" | "failed"
    path: str = ""
    alignment: str = ""    # for "built" only
    reason: str = ""       # for "skipped" only
    error: str = ""        # for "failed" only


def try_combine_for_video(video_path, media_type, sonarr_series_id=None,
                            sonarr_episode_id=None, radarr_id=None,
                            languages=None, format=None):
    """Single entry point: build (or rebuild) a combined subtitle file
    for the given video. Best-effort, never raises.

    languages, format: when both provided, use as an ad-hoc override.
    Otherwise read the profile's combine rule.
    """
    try:
        rule = _resolve_rule(
            media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
            override_languages=languages, override_format=format,
        )
        if rule is None:
            return CombineResult(status="skipped", reason="no rule")

        sources = resolve_source_paths(
            video_path=video_path,
            languages=rule["languages"],
        )
        if sources is None:
            return CombineResult(
                status="skipped",
                reason=f"missing source(s) for {rule['languages']}",
            )

        out_path = compose_combined_filename(
            video_path=video_path,
            primary=rule["languages"][0],
            secondaries=rule["languages"][1:],
            format=rule["format"],
        )

        try:
            content = compose(
                primary_path=sources.primary,
                secondary_paths=sources.secondaries,
                format=rule["format"],
            )
        except Exception as e:
            logging.exception("BAZARR combine compose failed for %s", video_path)
            return CombineResult(status="failed", error=str(e))

        try:
            with open(out_path, "wb") as fh:
                fh.write(content)
        except OSError as e:
            logging.exception("BAZARR combine write failed for %s", out_path)
            return CombineResult(status="failed", error=str(e))

        _post_write(out_path, video_path, media_type,
                     sonarr_episode_id, radarr_id)

        logging.info(
            "BAZARR combine built %s for %s", out_path, video_path,
        )
        return CombineResult(status="built", path=out_path, alignment="ok")
    except Exception as e:
        logging.exception("BAZARR combine top-level failure")
        return CombineResult(status="failed", error=str(e))


def _resolve_rule(media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
                   override_languages, override_format):
    if override_languages and override_format:
        return {"languages": list(override_languages), "format": override_format}
    profile = _profile_for(
        media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
    )
    return get_combine_rule(profile) if profile else None


def _profile_for(media_type, sonarr_series_id, sonarr_episode_id, radarr_id):
    from app.database import get_profile_id, get_profiles_list
    if media_type == "movies":
        profile_id = get_profile_id(movie_id=radarr_id)
    else:
        profile_id = (
            get_profile_id(episode_id=sonarr_episode_id)
            or get_profile_id(series_id=sonarr_series_id)
        )
    if not profile_id:
        return None
    return get_profiles_list(profile_id=profile_id)


def _post_write(out_path, video_path, media_type, sonarr_episode_id, radarr_id):
    """Hook into the same postprocess chain a downloaded subtitle uses.

    postprocess_subtitles uses the 'episode'/'movie' media-type convention and
    its episode branch keys off the value being exactly 'episode'. The combine
    pipeline uses the 'series'/'movies' convention, so map it here and pass the
    matching id (episode id for series, radarr id for movies)."""
    try:
        from api.subtitles.subtitles import postprocess_subtitles
        is_movie = media_type == "movies"
        pp_media_type = "movie" if is_movie else "episode"
        pp_id = radarr_id if is_movie else sonarr_episode_id
        postprocess_subtitles(out_path, video_path, pp_media_type, None, pp_id)
    except Exception:
        logging.exception("BAZARR combine post-write hook failed")


# Late import to avoid circular at module load.
from subtitles.language_profiles import get_combine_rule  # noqa: E402
