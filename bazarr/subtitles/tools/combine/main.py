# coding=utf-8

import logging
import os
import re
from dataclasses import dataclass

from .composer import compose
from .naming import compose_combined_filename
from .rules import resolve_source_paths

_TWO_LETTER_RE = re.compile(r"^[a-z]{2}$")


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

        # Validate the resolved languages for both the profile and the ad-hoc
        # override paths. The profile path is already validated on save, but
        # ad-hoc requests reach here unchecked: reject duplicates (which would
        # otherwise resolve the same file as primary and secondary and emit a
        # nonsense Movie.en.combined-en.srt), out-of-range counts, and codes that
        # are not plain 2-letter lowercase (defends compose_combined_filename
        # against a crafted code reaching the on-disk filename).
        langs = rule["languages"]
        if not (2 <= len(langs) <= 3) or len(set(langs)) != len(langs):
            return CombineResult(
                status="failed",
                error=f"combine requires 2 to 3 distinct languages, got {langs}",
            )
        if not all(isinstance(c, str) and _TWO_LETTER_RE.match(c) for c in langs):
            return CombineResult(
                status="failed",
                error=f"combine languages must be 2-letter lowercase codes, got {langs}",
            )
        if rule["format"] not in ("srt", "ass"):
            return CombineResult(
                status="failed",
                error=f"invalid combine format: {rule['format']!r}",
            )

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
            # Create the destination directory (mirrors how Bazarr's
            # get_target_folder makedirs the configured subtitle subfolder) so a
            # first-time combine into a not-yet-created absolute/relative folder
            # succeeds instead of failing on open().
            out_dir = os.path.dirname(out_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
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
    # Accept both media-type conventions: the REST endpoints pass 'movies'
    # (plural), but the auto-combine path forwards process_subtitle's 'movie'
    # (singular). Treating only the plural as a movie silently skipped every
    # automatic movie combine.
    if media_type in ("movies", "movie"):
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
    pipeline uses the 'series'/'movies' convention, so map it here. It also
    dereferences the metadata row (sonarrSeriesId/imdbId/...) to emit events and
    refresh the media-server library, so build the same metadata the editor
    endpoint does instead of passing None."""
    try:
        from api.subtitles.subtitles import postprocess_subtitles
        from app.database import (TableEpisodes, TableShows, TableMovies,
                                  database, select)
        is_movie = media_type in ("movies", "movie")
        if is_movie:
            metadata = database.execute(
                select(TableMovies.path, TableMovies.subtitles,
                       TableMovies.imdbId, TableMovies.tmdbId)
                .where(TableMovies.radarrId == radarr_id)
            ).first()
            postprocess_subtitles(out_path, video_path, "movie", metadata, radarr_id)
        else:
            metadata = database.execute(
                select(TableEpisodes.path, TableEpisodes.sonarrSeriesId,
                       TableEpisodes.subtitles, TableEpisodes.season,
                       TableEpisodes.episode, TableShows.imdbId, TableShows.tvdbId)
                .join(TableShows)
                .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id)
            ).first()
            postprocess_subtitles(out_path, video_path, "episode", metadata,
                                  sonarr_episode_id)
    except Exception:
        logging.exception("BAZARR combine post-write hook failed")


# Late import to avoid circular at module load.
from subtitles.language_profiles import get_combine_rule  # noqa: E402
