# coding=utf-8

import logging
import os
import ast
import re
import subprocess
from app.database import TableEpisodes, TableMovies, TableShows, database, select
from app.jobs_queue import jobs_queue
from app.event_handler import event_stream
from utilities.path_mappings import path_mappings
from utilities.binaries import get_binary
from utilities.video_analyzer import parse_video_metadata, _handle_alpha3
from languages.get_languages import alpha3_from_alpha2
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.tools.translate.main import translate_subtitles_file

logger = logging.getLogger(__name__)


def process_episode_translation(
    item, source_language, target_language, forced, hi, subtitle_path, job_id=None
):
    """Process a single episode for translation in background"""
    sonarr_series_id = item.get("sonarrSeriesId")
    sonarr_episode_id = item.get("sonarrEpisodeId")

    if not sonarr_series_id or not sonarr_episode_id:
        logger.error("Missing sonarrSeriesId or sonarrEpisodeId")
        return False

    # Get episode info from database
    episode = database.execute(
        select(
            TableEpisodes.path,
            TableEpisodes.subtitles,
            TableEpisodes.sonarrSeriesId,
            TableEpisodes.title,
            TableEpisodes.season,
            TableEpisodes.episode,
        ).where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id)
    ).first()

    if not episode:
        logger.error(f"Episode {sonarr_episode_id} not found")  # noqa: G004
        return False

    # Get series title
    show = database.execute(
        select(TableShows.title).where(TableShows.sonarrSeriesId == sonarr_series_id)
    ).first()
    show_title = show.title if show else "Unknown"

    # Update job name with actual episode info
    if job_id:
        ep_label = f"{show_title} S{episode.season:02d}E{episode.episode:02d}"
        jobs_queue.update_job_name(
            job_id=job_id,
            new_job_name=f"Translating {ep_label} ({source_language.upper()} → {target_language.upper()})",
        )

    video_path = path_mappings.path_replace(episode.path)

    # Find source subtitle
    source_subtitle_path = subtitle_path
    detected_source_lang = None
    if not source_subtitle_path:
        source_subtitle_path, detected_source_lang = find_subtitle_by_language(
            episode.subtitles, source_language, video_path, media_type="episode"
        )

    if not source_subtitle_path:
        logger.error(
            "No subtitle found for episode %s (requested source: %s)",
            sonarr_episode_id,
            source_language,
        )
        return False

    # Use detected language if available
    if detected_source_lang:
        source_language = detected_source_lang

    # Queue translation
    try:
        translate_subtitles_file(
            video_path=video_path,
            source_srt_file=source_subtitle_path,
            from_lang=source_language,
            to_lang=target_language,
            forced=forced,
            hi=hi,
            media_type="episode",
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_id=None,
            metadata=episode,
            job_id=job_id,
        )
        # Re-index subtitles so Bazarr's DB knows about the new translated file
        store_subtitles(path_mappings.path_replace_reverse(video_path), video_path)
        # Notify frontend to refresh series and episode views
        event_stream(type="series", payload=sonarr_series_id)
        event_stream(type="episode", payload=sonarr_episode_id)
        return True
    except Exception as e:
        logger.error(f"Translation failed for episode {sonarr_episode_id}: {e}")  # noqa: G004
        return False


def process_movie_translation(
    item, source_language, target_language, forced, hi, subtitle_path, job_id=None
):
    """Process a single movie for translation in background"""
    radarr_id = item.get("radarrId")

    if not radarr_id:
        logger.error("Missing radarrId")
        return False

    # Get movie info from database
    movie = database.execute(
        select(TableMovies.path, TableMovies.subtitles, TableMovies.title).where(
            TableMovies.radarrId == radarr_id
        )
    ).first()

    if not movie:
        logger.error(f"Movie {radarr_id} not found")  # noqa: G004
        return False

    # Update job name with actual movie title
    if job_id:
        jobs_queue.update_job_name(
            job_id=job_id,
            new_job_name=f"Translating {movie.title} ({source_language.upper()} → {target_language.upper()})",
        )

    video_path = path_mappings.path_replace_movie(movie.path)

    # Find source subtitle
    source_subtitle_path = subtitle_path
    detected_source_lang = None
    if not source_subtitle_path:
        source_subtitle_path, detected_source_lang = find_subtitle_by_language(
            movie.subtitles, source_language, video_path, media_type="movie"
        )

    if not source_subtitle_path:
        logger.error(
            "No subtitle found for movie %s (requested source: %s)",
            radarr_id,
            source_language,
        )
        return False

    # Use detected language if available
    if detected_source_lang:
        source_language = detected_source_lang

    # Queue translation
    try:
        translate_subtitles_file(
            video_path=video_path,
            source_srt_file=source_subtitle_path,
            from_lang=source_language,
            to_lang=target_language,
            forced=forced,
            hi=hi,
            media_type="movies",
            sonarr_series_id=None,
            sonarr_episode_id=None,
            radarr_id=radarr_id,
            metadata=movie,
            job_id=job_id,
        )
        # Re-index subtitles so Bazarr's DB knows about the new translated file
        store_subtitles_movie(
            path_mappings.path_replace_reverse_movie(video_path), video_path
        )
        # Notify frontend to refresh movie view
        event_stream(type="movie", payload=radarr_id)
        return True
    except Exception as e:
        logger.error(f"Translation failed for movie {radarr_id}: {e}")  # noqa: G004
        return False


def find_subtitle_by_language(subtitles, language_code, video_path, media_type="movie"):
    """Find a subtitle file by language code from the subtitles list."""
    available_subtitles = []

    if subtitles:
        # Parse subtitles if it's a string (Python literal from DB)
        if isinstance(subtitles, str):
            try:
                subtitles = ast.literal_eval(subtitles)
            except (ValueError, SyntaxError):
                logger.error("Failed to parse subtitles from database")
                subtitles = []

        if isinstance(subtitles, list):
            # Collect available subtitles with their paths for better processing
            for sub in subtitles:
                # DB format is [lang_str, path, size]
                if isinstance(sub, (list, tuple)) and len(sub) >= 2:
                    lang_parts = sub[0].split(":")
                    sub_code = lang_parts[0]
                    sub_path = sub[1]
                    sub_hi = len(lang_parts) > 1 and lang_parts[1].lower() == "hi"
                    sub_forced = (
                        len(lang_parts) > 1 and lang_parts[1].lower() == "forced"
                    )

                    if sub_path:
                        available_subtitles.append(
                            {
                                "code2": sub_code,
                                "path": sub_path,
                                "hi": sub_hi,
                                "forced": sub_forced,
                            }
                        )

    # Helper function to resolve and validate subtitle path
    def resolve_subtitle_path(sub_path):
        # Apply path mapping based on media type
        if media_type == "episode":
            mapped_path = path_mappings.path_replace(sub_path)
        else:
            mapped_path = path_mappings.path_replace_movie(sub_path)

        # Check if file exists at mapped path
        if os.path.exists(mapped_path):
            return mapped_path
        # Fallback to original path
        elif os.path.exists(sub_path):
            return sub_path

        return None

    # First pass: Look for exact language match in DB
    exact_matches = [s for s in available_subtitles if s["code2"] == language_code]

    # Sort matches: prefer non-HI, non-forced first, then HI, then forced
    exact_matches.sort(key=lambda x: (x["forced"], x["hi"]))

    for sub in exact_matches:
        resolved_path = resolve_subtitle_path(sub["path"])
        if resolved_path:
            return resolved_path, sub["code2"]

    # Second pass: If no exact match found in DB, try any available subtitle from DB
    if available_subtitles:
        # Sort all available: prefer non-HI, non-forced, and prioritize common languages
        common_languages = ["en", "eng"]  # English often has good quality subs

        def sort_key(sub):
            is_common = sub["code2"] in common_languages
            return (sub["forced"], sub["hi"], not is_common)

        available_subtitles.sort(key=sort_key)

        for sub in available_subtitles:
            resolved_path = resolve_subtitle_path(sub["path"])
            if resolved_path:
                return resolved_path, sub["code2"]

    # Third pass: Extract embedded subtitles from video container
    if subtitles and isinstance(subtitles, list):
        embedded_subs = []
        for sub in subtitles:
            if isinstance(sub, (list, tuple)) and len(sub) >= 2:
                lang_parts = sub[0].split(":")
                sub_code = lang_parts[0]
                sub_path = sub[1]
                if sub_path is None:  # Embedded subtitle (no file on disk)
                    embedded_subs.append(
                        {
                            "code2": sub_code,
                            "hi": len(lang_parts) > 1 and lang_parts[1].lower() == "hi",
                            "forced": len(lang_parts) > 1
                            and lang_parts[1].lower() == "forced",
                        }
                    )

        if embedded_subs:
            # Prefer exact language match, then fall back to any (preferring English)
            candidates = [s for s in embedded_subs if s["code2"] == language_code]
            if not candidates:
                common_languages = ["en", "eng"]
                candidates = sorted(
                    embedded_subs,
                    key=lambda x: (
                        x["forced"],
                        x["hi"],
                        x["code2"] not in common_languages,
                    ),
                )

            for sub in candidates:
                extracted_path = extract_embedded_subtitle(
                    video_path,
                    sub["code2"],
                    media_type,
                    hi=sub["hi"],
                    forced=sub["forced"],
                )
                if extracted_path:
                    return extracted_path, sub["code2"]

    # Fourth pass: Scan filesystem fallback
    filesystem_subs = scan_filesystem_for_subtitles(video_path)

    if filesystem_subs:
        # Prefer English
        for sub in filesystem_subs:
            if sub["is_english"]:
                return sub["path"], "en"

        # Use first available
        sub = filesystem_subs[0]
        return sub["path"], sub["detected_language"]

    return None, None


def extract_embedded_subtitle(
    video_path, language_code2, media_type, hi=False, forced=False
):
    """Extract an embedded subtitle track from a video file using ffmpeg.

    Why: Isolates embedded-track extraction so callers (API and batch) share one
    implementation with a deterministic cache key that encodes hi/forced flags.
    What: Looks up video metadata, finds the matching subtitle stream, runs ffmpeg
    to produce a .srt file, and caches it keyed by video hash + language + hi + forced.
    Returns the path to the extracted .srt file, or None on failure.
    Test: See tests/bazarr/test_embedded_subtitle_extraction.py — covers text codec,
    bitmap rejection, cache hit, hi/forced key separation.
    """
    if not language_code2:
        logger.warning(
            "extract_embedded_subtitle called with empty language_code2 — skipping"
        )
        return None

    target_alpha3 = alpha3_from_alpha2(language_code2)
    if not target_alpha3:
        logger.error(f"Cannot convert language code {language_code2} to alpha3")  # noqa: G004
        return None

    # Look up file metadata needed by parse_video_metadata
    if media_type == "episode":
        db_path = path_mappings.path_replace_reverse(video_path)
        media = database.execute(
            select(TableEpisodes.episode_file_id, TableEpisodes.file_size).where(
                TableEpisodes.path == db_path
            )
        ).first()
        if not media:
            return None
        data = parse_video_metadata(
            video_path, media.file_size, episode_file_id=media.episode_file_id
        )
    else:
        db_path = path_mappings.path_replace_reverse_movie(video_path)
        media = database.execute(
            select(TableMovies.movie_file_id, TableMovies.file_size).where(
                TableMovies.path == db_path
            )
        ).first()
        if not media:
            return None
        data = parse_video_metadata(
            video_path, media.file_size, movie_file_id=media.movie_file_id
        )

    if not data:
        return None

    # Find the subtitle provider (ffprobe or mediainfo)
    cache_provider = None
    if data.get("ffprobe") and "subtitle" in data["ffprobe"]:
        cache_provider = "ffprobe"
    elif data.get("mediainfo") and "subtitle" in data["mediainfo"]:
        cache_provider = "mediainfo"
    if not cache_provider:
        return None

    # Bitmap-based subtitle codecs that cannot be converted to SRT
    bitmap_codecs = [
        "pgs",
        "vobsub",
        "dvd_subtitle",
        "dvbsub",
        "hdmv_pgs_subtitle",
        "dvd",
    ]

    # Find the matching subtitle stream index using a two-pass approach:
    # Pass 1: exact match on language + hi + forced disposition flags.
    # Pass 2: fall back to first language-only match if no exact match found,
    # so extraction never silently returns the wrong track.
    exact_track = None
    fallback_track = None
    track_id = 0
    for track in data[cache_provider]["subtitle"]:
        codec = (track.get("format") or track.get("name") or "").lower()
        if any(bc in codec for bc in bitmap_codecs):
            track_id += 1
            continue

        if "language" not in track:
            track_id += 1
            continue

        track_alpha3 = _handle_alpha3(track)
        if track_alpha3 == target_alpha3:
            if (
                track.get("hearing_impaired", False) == hi
                and track.get("forced", False) == forced
            ):
                exact_track = track_id
                break
            if fallback_track is None:
                fallback_track = track_id
        track_id += 1

    found_track = exact_track if exact_track is not None else fallback_track

    if found_track is None:
        logger.debug(
            "No extractable embedded subtitle found for language %s in %s",
            language_code2,
            video_path,
        )
        return None

    # Build output path in Bazarr's config dir so Jellyfin won't pick it up
    import hashlib

    from app.get_args import args as bazarr_args

    extract_dir = os.path.join(bazarr_args.config_dir, "extracted_subs")
    os.makedirs(extract_dir, exist_ok=True)
    video_hash = hashlib.md5(video_path.encode()).hexdigest()
    suffix = ""
    if hi:
        suffix += ".hi"
    if forced:
        suffix += ".forced"
    output_path = os.path.join(
        extract_dir, f"{video_hash}.{language_code2}{suffix}.srt"
    )

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        logger.debug("Using cached extracted subtitle: %s", output_path)
        return output_path

    # Extract using ffmpeg
    try:
        ffmpeg_path = get_binary("ffmpeg")
    except Exception:
        logger.error("ffmpeg binary not found, cannot extract embedded subtitles")
        return None

    cmd = [
        ffmpeg_path,
        "-y",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-map",
        f"0:s:{found_track}",
        "-c:s",
        "srt",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(f"ffmpeg extraction failed for {video_path}: {result.stderr}")  # noqa: G004
            if os.path.exists(output_path):
                os.remove(output_path)
            return None
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            # Strip Windows carriage returns (\r) that ffmpeg may produce
            with open(output_path, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
            if "\r" in content:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content.replace("\r", ""))
            logger.info(
                "Extracted embedded %s subtitle to: %s",
                language_code2,
                output_path,
            )
            return output_path
        return None
    except subprocess.TimeoutExpired:
        logger.error(f"ffmpeg extraction timed out for {video_path}")  # noqa: G004
        if os.path.exists(output_path):
            os.remove(output_path)
        return None
    except Exception as e:
        logger.error(f"Failed to extract embedded subtitle: {e}")  # noqa: G004
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


def scan_filesystem_for_subtitles(video_path):
    """Scan filesystem for .srt files next to the video file."""
    ENGLISH_PATTERNS = [
        r"\.en\.srt$",
        r"\.eng\.srt$",
        r"\.english\.srt$",
        r"[._-]en[._-]",
        r"[._-]eng[._-]",
        r"[._-]english[._-]",
    ]

    video_dir = os.path.dirname(video_path)
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    results = []

    # Search directories
    search_dirs = [video_dir]
    for subfolder in ["Subs", "Subtitles", "subs", "subtitles", video_name]:
        subdir = os.path.join(video_dir, subfolder)
        if os.path.isdir(subdir):
            search_dirs.append(subdir)

    for directory in search_dirs:
        try:
            for filename in os.listdir(directory):
                if filename.lower().endswith(".srt"):
                    full_path = os.path.join(directory, filename)

                    # Detect language from filename
                    is_english = any(
                        re.search(p, filename.lower()) for p in ENGLISH_PATTERNS
                    )
                    detected_lang = (
                        "en" if is_english else detect_language_from_content(full_path)
                    )

                    results.append(
                        {
                            "path": full_path,
                            "filename": filename,
                            "is_english": is_english or detected_lang == "en",
                            "detected_language": detected_lang or "und",
                        }
                    )
        except OSError:
            continue

    # Sort: English first
    results.sort(key=lambda x: (not x["is_english"], x["filename"]))
    return results


def detect_language_from_content(srt_path):
    """Detect language by analyzing subtitle content."""
    from guess_language import guess_language
    from charset_normalizer import detect

    try:
        with open(srt_path, "rb") as f:
            raw = f.read(8192)  # Read first 8KB

        encoding = detect(raw)
        if encoding and encoding.get("encoding"):
            text = raw.decode(encoding["encoding"], errors="ignore")
            return guess_language(text)
    except Exception:
        pass
    return None
