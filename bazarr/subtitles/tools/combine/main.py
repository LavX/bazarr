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
            _write_history_row(
                video_path=video_path, media_type=media_type,
                sources=sources, format=rule["format"], output=out_path,
                alignment="error", success=False, error=str(e),
                sonarr_series_id=sonarr_series_id,
                sonarr_episode_id=sonarr_episode_id, radarr_id=radarr_id,
            )
            return CombineResult(status="failed", error=str(e))

        try:
            with open(out_path, "wb") as fh:
                fh.write(content)
        except OSError as e:
            logging.exception("BAZARR combine write failed for %s", out_path)
            _write_history_row(
                video_path=video_path, media_type=media_type,
                sources=sources, format=rule["format"], output=out_path,
                alignment="error", success=False, error=str(e),
                sonarr_series_id=sonarr_series_id,
                sonarr_episode_id=sonarr_episode_id, radarr_id=radarr_id,
            )
            return CombineResult(status="failed", error=str(e))

        _post_write(out_path, video_path, media_type,
                     sonarr_episode_id, radarr_id)

        _write_history_row(
            video_path=video_path, media_type=media_type,
            sources=sources, format=rule["format"], output=out_path,
            alignment="ok", success=True, error="",
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id, radarr_id=radarr_id,
        )
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
    """Hook into the same postprocess chain a downloaded subtitle uses."""
    try:
        from api.subtitles.subtitles import postprocess_subtitles
        postprocess_subtitles(
            out_path, video_path, media_type, None,
            sonarr_episode_id if media_type != "movies" else radarr_id,
        )
    except Exception:
        logging.exception("BAZARR combine post-write hook failed")


def _write_history_row(video_path, media_type, sources, format, output,
                         alignment, success, error, sonarr_series_id,
                         sonarr_episode_id, radarr_id):
    """Insert a row into the appropriate history table with action='combined'."""
    import json
    payload = {
        "sources": [sources.primary] + list(sources.secondaries),
        "format": format,
        "alignment": alignment,
        "output": output,
    }
    if not success:
        payload["error"] = error
    description = json.dumps(payload)

    try:
        if media_type == "movies":
            _insert_movie_history(radarr_id, description)
        else:
            _insert_episode_history(sonarr_series_id, sonarr_episode_id, description)
    except Exception:
        logging.exception("BAZARR combine history write failed")


def _insert_movie_history(radarr_id, description):
    from app.database import TableHistoryMovie, database
    import time
    database.execute(
        TableHistoryMovie.__table__.insert().values(
            action="combined",
            radarrId=radarr_id,
            timestamp=int(time.time()),
            description=description,
        )
    )


def _insert_episode_history(series_id, episode_id, description):
    from app.database import TableHistory, database
    import time
    database.execute(
        TableHistory.__table__.insert().values(
            action="combined",
            sonarrSeriesId=series_id,
            sonarrEpisodeId=episode_id,
            timestamp=int(time.time()),
            description=description,
        )
    )


# Late import to avoid circular at module load.
from subtitles.language_profiles import get_combine_rule  # noqa: E402
