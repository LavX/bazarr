# coding=utf-8

import logging
import os
import ast
import re
import subprocess
import uuid
from app.database import (TableEpisodes, TableMovies, TableShows, TableSportsEvents,
                          database, select)
from app.config import settings
from app.jobs_queue import jobs_queue
from app.event_handler import event_stream
from arr_instances.resolution import scoped
from utilities.path_mappings import path_mappings
from utilities.binaries import get_binary
from utilities.video_analyzer import parse_video_metadata
from languages.get_languages import alpha3_from_alpha2
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.tools.translate.main import translate_subtitles_file
from subtitles.indexer.entries import normalize_codec
from subtitles.tools.embedded_streams import (BITMAP_CONTAINERS, bitmap_family, is_bitmap_codec,
                                              select_subtitle_stream, track_codec)

logger = logging.getLogger(__name__)


def extracted_subtitles_dir():
    """Where server-side embedded-track extractions are cached.

    Inside Bazarr's config dir rather than next to the video, so a media server
    scanning the library does not pick the extraction up as a real subtitle.
    """
    from app.get_args import args as bazarr_args

    return os.path.join(bazarr_args.config_dir, "extracted_subs")


def extracted_subtitle_key(video_path):
    """The cache key an extraction of ``video_path`` is named after.

    Keyed on the video's identity (path + size + mtime), not just the path: a
    replaced or upgraded file at the same path would otherwise reuse the
    previous video's extracted subtitle (stale text), the exact upgrade case
    embedded translation targets.

    Shared with the sports profile guard, which recomputes this key to tell one
    of these artifacts from an arbitrary file handed to it. The two have to
    agree on the name, so they read it from here.
    """
    import hashlib

    try:
        stat_result = os.stat(video_path)
        identity = f"{video_path}:{stat_result.st_size}:{int(stat_result.st_mtime)}"
    except OSError:
        identity = video_path
    return hashlib.md5(identity.encode()).hexdigest()


def process_episode_translation(
    item, source_language, target_language, forced, hi, subtitle_path, job_id=None
):
    """Process a single episode for translation in background"""
    sonarr_series_id = item.get("sonarrSeriesId")
    sonarr_episode_id = item.get("sonarrEpisodeId")
    arr_instance_id = item.get("arr_instance_id")

    if not sonarr_series_id or not sonarr_episode_id:
        logger.error("Missing sonarrSeriesId or sonarrEpisodeId")
        return False

    # Get episode info from database, scoped to the owning instance (#156) so a
    # sonarrEpisodeId shared across instances reads the right row and we can emit
    # its LOCAL id to the frontend.
    episode = database.execute(
        scoped(
            select(
                TableEpisodes.id,
                TableEpisodes.path,
                TableEpisodes.subtitles,
                TableEpisodes.sonarrSeriesId,
                TableEpisodes.title,
                TableEpisodes.season,
                TableEpisodes.episode,
            ).where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
            TableEpisodes.arr_instance_id, arr_instance_id)
    ).first()

    if not episode:
        logger.error(f"Episode {sonarr_episode_id} not found")  # noqa: G004
        return False

    # Get series title (scoped to the owning instance, #156)
    show = database.execute(
        scoped(select(TableShows.title).where(TableShows.sonarrSeriesId == sonarr_series_id),
               TableShows.arr_instance_id, arr_instance_id)
    ).first()
    show_title = show.title if show else "Unknown"

    # Update job name with actual episode info
    if job_id:
        ep_label = f"{show_title} S{episode.season:02d}E{episode.episode:02d}"
        jobs_queue.update_job_name(
            job_id=job_id,
            new_job_name=f"Translating {ep_label} ({source_language.upper()} → {target_language.upper()})",
        )

    video_path = path_mappings.path_replace_instance(episode.path, arr_instance_id, 'episode')

    # Find source subtitle
    source_subtitle_path = subtitle_path
    detected_source_lang = None
    if not source_subtitle_path:
        source_subtitle_path, detected_source_lang = find_subtitle_by_language(
            episode.subtitles, source_language, video_path, media_type="episode",
            arr_instance_id=arr_instance_id
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
            arr_instance_id=arr_instance_id,
        )
        # Re-index subtitles so Bazarr's DB knows about the new translated file
        store_subtitles(
            path_mappings.path_replace_reverse_instance(video_path, arr_instance_id, 'episode'),
            video_path, arr_instance_id=arr_instance_id)
        # Notify frontend to refresh series and episode views. Emit the LOCAL
        # episode id (#156): the frontend caches episode detail by local id and
        # the upstream sonarrEpisodeId is not unique across instances.
        event_stream(type="series", payload=sonarr_series_id)
        event_stream(type="episode", payload=episode.id)
        return True
    except Exception as e:
        logger.error(f"Translation failed for episode {sonarr_episode_id}: {e}")  # noqa: G004
        return False


def process_movie_translation(
    item, source_language, target_language, forced, hi, subtitle_path, job_id=None
):
    """Process a single movie for translation in background"""
    radarr_id = item.get("radarrId")
    arr_instance_id = item.get("arr_instance_id")

    if not radarr_id:
        logger.error("Missing radarrId")
        return False

    # Get movie info from database, scoped to the owning instance (#156):
    # radarrId is not unique across instances, so an unscoped lookup can return
    # a sibling instance's movie and translate the wrong file.
    movie = database.execute(
        scoped(
            select(TableMovies.path, TableMovies.subtitles, TableMovies.title).where(
                TableMovies.radarrId == radarr_id
            ),
            TableMovies.arr_instance_id, arr_instance_id)
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

    video_path = path_mappings.path_replace_instance(movie.path, arr_instance_id, 'movie')

    # Find source subtitle
    source_subtitle_path = subtitle_path
    detected_source_lang = None
    if not source_subtitle_path:
        source_subtitle_path, detected_source_lang = find_subtitle_by_language(
            movie.subtitles, source_language, video_path, media_type="movie",
            arr_instance_id=arr_instance_id
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
            arr_instance_id=arr_instance_id,
        )
        # Re-index subtitles so Bazarr's DB knows about the new translated file
        store_subtitles_movie(
            path_mappings.path_replace_reverse_instance(video_path, arr_instance_id, 'movie'),
            video_path, arr_instance_id=arr_instance_id
        )
        # Notify frontend to refresh movie view
        event_stream(type="movie", payload=radarr_id)
        return True
    except Exception as e:
        logger.error(f"Translation failed for movie {radarr_id}: {e}")  # noqa: G004
        return False


def find_subtitle_by_language(subtitles, language_code, video_path, media_type="movie",
                             arr_instance_id=None):
    """Find a subtitle file by language code from the subtitles list.

    ``arr_instance_id`` is the instance that owns the media (#156). Extraction
    reverses the video path with that instance's mapping and scopes its row
    lookup by it, so a caller that resolved an owner has to pass it on or the
    lookup runs unscoped and can select another instance's subtitle stream.
    """
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
        # The owning instance's mapping, the same one the caller used for the
        # video. Mixing them pairs a secondary instance's video with a default
        # instance's subtitle when both paths happen to exist, or reports no
        # subtitle at all when they do not.
        kind = "episode" if media_type == "episode" else "movie"
        mapped_path = path_mappings.path_replace_instance(sub_path, arr_instance_id, kind)

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
                    # Inspect every modifier, not just the first: a track can be
                    # both hi and forced (e.g. "en:hi:forced"), so checking only
                    # lang_parts[1] would drop the second flag and mis-select the
                    # stream when several same-language tracks exist.
                    modifiers = {part.lower() for part in lang_parts[1:]}
                    embedded_subs.append(
                        {
                            "code2": sub_code,
                            "hi": "hi" in modifiers,
                            "forced": "forced" in modifiers,
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
                    arr_instance_id=arr_instance_id,
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


def embedded_subtitle_tracks(video_path, media_type, arr_instance_id=None):
    """The probed subtitle tracks of the media row behind ``video_path``.

    In stream order, so a position in the list is what ffmpeg's ``0:s:N``
    names. None when the row, its probe or its subtitle list is missing. The
    row is found by reversing ``video_path`` with the owning instance's
    mapping, the one that mapped it forward (#156).
    """
    if media_type == "sports":
        db_path = path_mappings.path_replace_reverse_instance(
            video_path, arr_instance_id, "sports")
        # Sports rows are keyed by the local event id and the probe cache hangs
        # off the same row, so the row is resolved by mapped path and owner and
        # its id drives the sports branch of parse_video_metadata. Without this
        # arm a sports extraction ran the movie lookup, found no movie row and
        # returned None, so a collected event could never be extracted.
        media = database.execute(
            scoped(
                select(TableSportsEvents.id, TableSportsEvents.file_size).where(
                    TableSportsEvents.path == db_path
                ),
                TableSportsEvents.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not media:
            return None
        data = parse_video_metadata(
            video_path, media.file_size, arr_instance_id=arr_instance_id,
            sports_event_id=media.id
        )
    elif media_type == "episode":
        db_path = path_mappings.path_replace_reverse_instance(
            video_path, arr_instance_id, "series")
        # Scoped to the owning instance (#156): two instances can index the
        # same path, and an unscoped first() would take an arbitrary row and
        # feed the wrong file_size and episode_file_id to the metadata cache.
        media = database.execute(
            scoped(
                select(TableEpisodes.episode_file_id, TableEpisodes.file_size).where(
                    TableEpisodes.path == db_path
                ),
                TableEpisodes.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not media:
            return None
        data = parse_video_metadata(
            video_path, media.file_size, episode_file_id=media.episode_file_id,
            arr_instance_id=arr_instance_id
        )
    else:
        db_path = path_mappings.path_replace_reverse_instance(
            video_path, arr_instance_id, "movie")
        media = database.execute(
            scoped(
                select(TableMovies.movie_file_id, TableMovies.file_size).where(
                    TableMovies.path == db_path
                ),
                TableMovies.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not media:
            return None
        data = parse_video_metadata(
            video_path, media.file_size, movie_file_id=media.movie_file_id,
            arr_instance_id=arr_instance_id
        )

    if not data:
        return None

    # ffprobe or mediainfo, whichever parsed the file
    for provider in ("ffprobe", "mediainfo"):
        if data.get(provider) and "subtitle" in data[provider]:
            return data[provider]["subtitle"]
    return None


def extract_embedded_subtitle(
    video_path, language_code2, media_type, hi=False, forced=False, arr_instance_id=None,
    exact=False, codec=None,
):
    """Extract an embedded subtitle track from a video file using ffmpeg.

    Why: Isolates embedded-track extraction so callers (API and batch) share one
    implementation with a deterministic cache key that encodes hi/forced flags.
    What: Looks up video metadata, finds the matching subtitle stream, runs ffmpeg
    to produce a .srt file, and caches it keyed by video hash + language + hi + forced.
    Returns the path to the extracted .srt file, or None on failure.
    Test: See tests/bazarr/test_embedded_subtitle_extraction.py, covers text codec,
    bitmap rejection, cache hit, hi/forced key separation, and
    tests/bazarr/test_embedded_extraction_instance_scope.py for the mapping.

    ``arr_instance_id`` is the instance that owns the media (#156). The caller
    maps the path forward with that instance's mapping, so the reverse has to
    use the same one or the row lookup misses entirely; path_replace_reverse_instance
    falls back to the global mapping when the instance has none, which is what
    a None here relies on.

    ``exact`` turns the language-only fallback off. Translate keeps it, so a
    source whose flags drifted still translates; Extract to file and the tools
    write the track a row names into a file named after that row, and another
    track's text under that name would be wrong, so they pass True and get
    None.

    ``codec`` is the text codec the row showed. Two text tracks with one
    language key and different codecs (an SRT and an ASS English) are two
    rows, so a track of that codec is taken first, and cached under its own
    name; without one the first matching text track is taken, as before. Two
    tracks with one key and one codec cannot be told apart (the listing
    stores no stream id) and resolve to the first.
    """
    if not language_code2:
        logger.warning(
            "extract_embedded_subtitle called with empty language_code2, skipping"
        )
        return None

    target_alpha3 = alpha3_from_alpha2(language_code2)
    if not target_alpha3:
        logger.error(f"Cannot convert language code {language_code2} to alpha3")  # noqa: G004
        return None

    tracks = embedded_subtitle_tracks(video_path, media_type, arr_instance_id)
    if not tracks:
        return None

    # The track the row stands for: exact language, hi and forced first, then,
    # unless ``exact``, the first text track in that language. Bitmap tracks
    # are passed over here; extract_embedded_bitmap copies those out as they are.
    und_setting = settings.general.default_und_embedded_subtitles_lang
    und_default_language = alpha3_from_alpha2(und_setting) if und_setting else None
    text_codec = normalize_codec(codec)
    if is_bitmap_codec(text_codec):
        text_codec = None
    found_track = None
    if text_codec:
        found_track = select_subtitle_stream(
            tracks, target_alpha3, hi=hi, forced=forced, exact=exact,
            und_default_language=und_default_language,
            accept=lambda found: found == text_codec)
    if found_track is None:
        text_codec = None
        found_track = select_subtitle_stream(
            tracks, target_alpha3, hi=hi, forced=forced, exact=exact,
            und_default_language=und_default_language)

    if found_track is None:
        logger.debug(
            "No extractable embedded subtitle found for language %s in %s",
            language_code2,
            video_path,
        )
        return None

    # Build output path in Bazarr's config dir so Jellyfin won't pick it up
    extract_dir = extracted_subtitles_dir()
    os.makedirs(extract_dir, exist_ok=True)
    video_hash = extracted_subtitle_key(video_path)
    suffix = ""
    if hi:
        suffix += ".hi"
    if forced:
        suffix += ".forced"
    if text_codec:
        # Another text track of the same key may be cached under the plain name.
        suffix += "." + re.sub(r"[^a-z0-9]+", "-", text_codec).strip("-")
    output_path = os.path.join(
        extract_dir, f"{video_hash}.{language_code2}{suffix}.srt"
    )

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        logger.debug("Using cached extracted subtitle: %s", output_path)
        return output_path

    # ffmpeg writes to a temporary name and the finished file is moved into
    # place, so output_path either does not exist or is complete. Writing
    # straight to it meant a second caller arriving mid-write saw a non-empty
    # file, took it as a cache hit, and translated a truncated subtitle. The
    # The temporary name is unique per call, not per process: jobs run as
    # threads inside one process, so a pid would be the same for both and the
    # first os.replace would pull the file out from under the second. It keeps
    # the .srt extension because ffmpeg picks its muxer from it.
    temp_path = f"{output_path}.{uuid.uuid4().hex}.part.srt"

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
        # Name the container format rather than leaving ffmpeg to infer it from
        # the extension: the output goes to a temporary name, and an
        # unrecognised one makes it fail with "Unable to choose an output
        # format" instead of writing anything.
        "-f",
        "srt",
        temp_path,
    ]

    def _discard_temp():
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                logger.debug("Could not remove the temporary file %s", temp_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error(f"ffmpeg extraction failed for {video_path}: {result.stderr}")  # noqa: G004
            _discard_temp()
            return None
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            # Strip Windows carriage returns (\r) that ffmpeg may produce
            with open(temp_path, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
            if "\r" in content:
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(content.replace("\r", ""))
            # Atomic on the same filesystem: a reader sees the old file or the
            # new one, never a partial write.
            os.replace(temp_path, output_path)
            logger.info(
                "Extracted embedded %s subtitle to: %s",
                language_code2,
                output_path,
            )
            return output_path
        _discard_temp()
        return None
    except subprocess.TimeoutExpired:
        logger.error(f"ffmpeg extraction timed out for {video_path}")  # noqa: G004
        _discard_temp()
        return None
    except Exception as e:
        logger.error(f"Failed to extract embedded subtitle: {e}")  # noqa: G004
        _discard_temp()
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


def extract_embedded_bitmap(video_path, language_code2, media_type, work_dir, hi=False, forced=False,
                            arr_instance_id=None, codec=None, before_copy=None):
    """Copy an image-based embedded track out of the video as it is, into ``work_dir``.

    PGS lands as a .sup, VobSub as an .idx and .sub pair and DVB as a Matroska
    .mks: none of them can become text without OCR. ``codec`` is the codec the
    row showed; when it names a bitmap family only a track of that family is
    taken, so an SRT and a PGS track in the same language stay apart. Only the
    track with exactly the requested HI and forced flags is taken, never
    another variant of the language: the copy becomes that row's file.
    ``before_copy`` is called with the chosen track's family before ffmpeg
    reads the container, and may raise to stop the copy. Returns the written
    paths (the .idx first for VobSub), or None when the language has no such
    image-based track or ffmpeg fails.
    """
    target_alpha3 = alpha3_from_alpha2(language_code2) if language_code2 else None
    if not target_alpha3:
        return None
    tracks = embedded_subtitle_tracks(video_path, media_type, arr_instance_id)
    if not tracks:
        return None
    wanted = bitmap_family(codec)
    und_setting = settings.general.default_und_embedded_subtitles_lang
    index = select_subtitle_stream(
        tracks, target_alpha3, hi=hi, forced=forced, exact=True,
        und_default_language=alpha3_from_alpha2(und_setting) if und_setting else None,
        accept=(lambda found: bitmap_family(found) == wanted) if wanted else is_bitmap_codec)
    if index is None:
        return None
    family = bitmap_family(track_codec(tracks[index]))
    if before_copy is not None:
        before_copy(family)
    try:
        ffmpeg = get_binary("ffmpeg")
    except Exception:
        logger.error("ffmpeg binary not found, cannot extract embedded subtitles")
        return None
    extension, muxer = BITMAP_CONTAINERS[family]
    output = os.path.join(work_dir, f"track{extension}")
    command = [ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", video_path,
               "-map", f"0:s:{index}", "-c:s", "copy", "-f", muxer, output]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out copying subtitle stream %s of %s", index, video_path)
        return None
    if result.returncode != 0 or not os.path.isfile(output) or not os.path.getsize(output):
        logger.error("ffmpeg could not copy subtitle stream %s of %s: %s", index, video_path,
                     result.stderr.strip())
        return None
    if family != "vobsub":
        return [output]
    from subtitles.tools.vobsub import write_vobsub_index
    index_path = os.path.join(work_dir, "track.idx")
    write_vobsub_index(video_path, index, output, index_path, language_code2)
    return [index_path, output]
