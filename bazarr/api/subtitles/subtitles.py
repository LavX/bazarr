# coding=utf-8

import logging
import os
import sys

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.database import TableShows, TableEpisodes, TableMovies, database, select
from arr_instances.resolution import scoped
from languages.get_languages import alpha3_from_alpha2
from utilities.path_mappings import path_mappings
from utilities.video_analyzer import subtitles_sync_references
from subtitles.tools.subsyncer import SubSyncer  # noqa: F401
from subtitles.tools.subsync_engines import is_sync_engine_output
from subtitles.tools.translate.main import translate_subtitles_file
from subtitles.tools.translate.batch import extract_embedded_subtitle
from subtitles.tools.mods import subtitles_apply_mods
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.sync import sync_subtitles
from app.config import settings, empty_values, get_array_from
from app.event_handler import event_stream
from plex.operations import plex_refresh_item
from jellyfin.operations import jellyfin_refresh_item


from ..utils import authenticate

api_ns_subtitles = Namespace(
    "Subtitles", description="Apply mods/tools on external subtitles"
)


@api_ns_subtitles.route("subtitles")
class Subtitles(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument(
        "subtitlesPath", type=str, required=True, help="External subtitles file path"
    )
    get_request_parser.add_argument(
        "sonarrEpisodeId", type=int, required=False, help="Sonarr Episode ID"
    )
    get_request_parser.add_argument(
        "radarrMovieId", type=int, required=False, help="Radarr Movie ID"
    )
    get_request_parser.add_argument(
        "arr_instance_id",
        type=int,
        required=False,
        help="Owning Sonarr/Radarr instance id (#156)",
    )

    audio_tracks_data_model = api_ns_subtitles.model(
        "audio_tracks_data_model",
        {
            "stream": fields.String(),
            "name": fields.String(),
            "language": fields.String(),
        },
    )

    embedded_subtitles_data_model = api_ns_subtitles.model(
        "embedded_subtitles_data_model",
        {
            "stream": fields.String(),
            "name": fields.String(),
            "language": fields.String(),
            "forced": fields.Boolean(),
            "hearing_impaired": fields.Boolean(),
        },
    )

    external_subtitles_data_model = api_ns_subtitles.model(
        "external_subtitles_data_model",
        {
            "name": fields.String(),
            "path": fields.String(),
            "language": fields.String(),
            "forced": fields.Boolean(),
            "hearing_impaired": fields.Boolean(),
        },
    )

    get_response_model = api_ns_subtitles.model(
        "SubtitlesGetResponse",
        {
            "audio_tracks": fields.Nested(audio_tracks_data_model),
            "embedded_subtitles_tracks": fields.Nested(embedded_subtitles_data_model),
            "external_subtitles_tracks": fields.Nested(external_subtitles_data_model),
        },
    )

    @authenticate
    @api_ns_subtitles.response(200, "Success")
    @api_ns_subtitles.response(401, "Not Authenticated")
    @api_ns_subtitles.doc(parser=get_request_parser)
    def get(self):
        """Return available audio and embedded subtitles tracks with external subtitles. Used for manual subsync
        modal"""
        args = self.get_request_parser.parse_args()
        subtitlesPath = args.get("subtitlesPath")
        episodeId = args.get("sonarrEpisodeId", None)
        movieId = args.get("radarrMovieId", None)
        arr_instance_id = args.get("arr_instance_id")

        result = subtitles_sync_references(
            subtitles_path=subtitlesPath,
            sonarr_episode_id=episodeId,
            radarr_movie_id=movieId,
            arr_instance_id=arr_instance_id,
        )

        return marshal(result, self.get_response_model, envelope="data")

    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument(
        "action",
        type=str,
        required=True,
        help='Action from ["sync", "translate" or mods name]',
    )
    patch_request_parser.add_argument(
        "language", type=str, required=True, help="Language code2"
    )
    patch_request_parser.add_argument(
        "path",
        type=str,
        required=False,
        help="Subtitles file path (empty for embedded tracks)",
    )
    patch_request_parser.add_argument(
        "from_language",
        type=str,
        required=False,
        help="Source language code2 (required when path is empty, i.e. embedded track)",
    )
    patch_request_parser.add_argument(
        "from_hi",
        type=str,
        required=False,
        help='HI flag of the embedded source track from ["True", "False"]',
    )
    patch_request_parser.add_argument(
        "from_forced",
        type=str,
        required=False,
        help='Forced flag of the embedded source track from ["True", "False"]',
    )
    patch_request_parser.add_argument(
        "type", type=str, required=True, help='Media type from ["episode", "movie"]'
    )
    patch_request_parser.add_argument(
        "id", type=int, required=True, help="Media ID (episodeId, radarrId)"
    )
    patch_request_parser.add_argument(
        "arr_instance_id",
        type=int,
        required=False,
        help="Owning Sonarr/Radarr instance id (#156)",
    )
    patch_request_parser.add_argument(
        "forced",
        type=str,
        required=False,
        help='Forced subtitles from ["True", "False"]',
    )
    patch_request_parser.add_argument(
        "hi", type=str, required=False, help='HI subtitles from ["True", "False"]'
    )
    patch_request_parser.add_argument(
        "original_format",
        type=str,
        required=False,
        help='Use original subtitles format from ["True", "False"]',
    )
    patch_request_parser.add_argument(
        "reference",
        type=str,
        required=False,
        help="Reference to use for sync from video file track number (a:0) or some "
        "subtitles file path",
    )
    patch_request_parser.add_argument(
        "max_offset_seconds",
        type=str,
        required=False,
        help="Maximum offset seconds to allow",
    )
    patch_request_parser.add_argument(
        "no_fix_framerate",
        type=str,
        required=False,
        help='Don\'t try to fix framerate from ["True", "False"]',
    )
    patch_request_parser.add_argument(
        "gss",
        type=str,
        required=False,
        help='Use Golden-Section Search from ["True", "False"]',
    )
    patch_request_parser.add_argument(
        "output_mode",
        type=str,
        required=False,
        help='Output mode from ["overwrite", "keep_all"]',
    )
    patch_request_parser.add_argument(
        "enabled_engines",
        type=str,
        required=False,
        help="Comma-separated sync engines to use",
    )

    @authenticate
    @api_ns_subtitles.doc(parser=patch_request_parser)
    @api_ns_subtitles.response(204, "Success")
    @api_ns_subtitles.response(401, "Not Authenticated")
    @api_ns_subtitles.response(400, "Generated sync output files cannot be synchronized again")
    @api_ns_subtitles.response(404, "Episode/movie not found")
    @api_ns_subtitles.response(409, "Unable to edit subtitles file. Check logs.")
    @api_ns_subtitles.response(500, "Subtitles file not found. Path mapping issue?")
    def patch(self):
        """Apply mods/tools on external subtitles"""
        args = self.patch_request_parser.parse_args()
        action = args.get("action")

        language = args.get("language")
        subtitles_path = args.get("path") or ""
        media_type = args.get("type")
        id = args.get("id")
        arr_instance_id = args.get("arr_instance_id")
        forced = True if args.get("forced") == "True" else False
        hi = True if args.get("hi") == "True" else False

        # Embedded track: path is absent/empty, extract the subtitle from the
        # video container into {config_dir}/extracted_subs/ first.
        # Only translate is supported for embedded tracks (no file to sync/mod).
        # NOTE: do NOT delete the extracted file here; translate_subtitles_file()
        # dispatches an async background job that reads the file after this request
        # returns. The extracted_subs/ directory is intentionally persistent.
        if not subtitles_path and action == "translate":
            from_language_arg = args.get("from_language")
            if not from_language_arg:
                return (
                    "from_language is required when path is empty (embedded track)",
                    400,
                )
            if len(from_language_arg) != 2 or not alpha3_from_alpha2(from_language_arg):
                return "from_language must be a valid alpha2 language code", 400

            # The hi/forced args describe the OUTPUT subtitle. The source embedded
            # track to extract can be a different variant (e.g. translating a normal
            # subtitle from the HI English track), so honour from_hi/from_forced when
            # the caller sends them and fall back to the output flags otherwise (the
            # direct-embedded-translate path where source == output).
            from_hi_arg = args.get("from_hi")
            from_forced_arg = args.get("from_forced")
            source_hi = (from_hi_arg == "True") if from_hi_arg is not None else hi
            source_forced = (
                (from_forced_arg == "True") if from_forced_arg is not None else forced
            )

            # Resolve the video path from the DB using the media ID
            if media_type == "episode":
                ep_stmt = scoped(
                    select(TableEpisodes.path).where(
                        TableEpisodes.sonarrEpisodeId == id
                    ),
                    TableEpisodes.arr_instance_id,
                    arr_instance_id,
                )
                ep_meta = database.execute(ep_stmt).first()
                if not ep_meta:
                    return "Episode not found", 404
                embedded_video_path = path_mappings.path_replace(ep_meta.path)
            else:
                mv_stmt = scoped(
                    select(TableMovies.path).where(TableMovies.radarrId == id),
                    TableMovies.arr_instance_id,
                    arr_instance_id,
                )
                mv_meta = database.execute(mv_stmt).first()
                if not mv_meta:
                    return "Movie not found", 404
                embedded_video_path = path_mappings.path_replace_movie(mv_meta.path)

            extracted = extract_embedded_subtitle(
                embedded_video_path,
                from_language_arg,
                media_type,
                hi=source_hi,
                forced=source_forced,
            )
            if not extracted:
                return (
                    "Could not extract embedded subtitle: codec may be bitmap (PGS/VobSub) "
                    "or the language track was not found",
                    400,
                )
            subtitles_path = extracted

        if not subtitles_path or not os.path.exists(subtitles_path):
            return "Subtitles file not found. Path mapping issue?", 500

        if action == "sync" and is_sync_engine_output(subtitles_path):
            return "Generated sync output files cannot be synchronized again.", 400

        if media_type == "episode":
            metadata_stmt = scoped(
                select(
                    TableEpisodes.path,
                    TableEpisodes.sonarrSeriesId,
                    TableEpisodes.subtitles,
                    TableEpisodes.season,
                    TableEpisodes.episode,
                    TableShows.imdbId,
                    TableShows.tvdbId,
                )
                .join(TableShows)
                .where(TableEpisodes.sonarrEpisodeId == id),
                TableEpisodes.arr_instance_id,
                arr_instance_id,
            )
            metadata = database.execute(metadata_stmt).first()

            if not metadata:
                return "Episode not found", 404

            video_path = path_mappings.path_replace(metadata.path)
        else:
            metadata_stmt = scoped(
                select(
                    TableMovies.path,
                    TableMovies.subtitles,
                    TableMovies.imdbId,
                    TableMovies.tmdbId,
                ).where(TableMovies.radarrId == id),
                TableMovies.arr_instance_id,
                arr_instance_id,
            )
            metadata = database.execute(metadata_stmt).first()

            if not metadata:
                return "Movie not found", 404

            video_path = path_mappings.path_replace_movie(metadata.path)

        if action == "sync":
            try:

                def postprocess_callback():
                    return postprocess_subtitles(
                        subtitles_path, video_path, media_type, metadata, id,
                        arr_instance_id=arr_instance_id
                    )

                sync_subtitles(
                    video_path=video_path,
                    srt_path=subtitles_path,
                    srt_lang=language,
                    hi=hi,
                    forced=forced,
                    percent_score=0,  # make sure to always sync when requested manually
                    reference=args.get("reference")
                    if args.get("reference") not in empty_values
                    else video_path,
                    # Fall back to None (not the global value) so an unset Max
                    # Offset resolves the owning instance's per-instance override
                    # inside sync_subtitles (#227), mirroring enabled_engines below.
                    max_offset_seconds=args.get("max_offset_seconds")
                    if args.get("max_offset_seconds") not in empty_values
                    else None,
                    no_fix_framerate=args.get("no_fix_framerate") == "True",
                    gss=args.get("gss") == "True",
                    output_mode=args.get("output_mode")
                    if args.get("output_mode") not in empty_values
                    else None,
                    enabled_engines=args.get("enabled_engines")
                    if args.get("enabled_engines") not in empty_values
                    else None,
                    sonarr_series_id=metadata.sonarrSeriesId
                    if media_type == "episode"
                    else None,
                    sonarr_episode_id=id if media_type == "episode" else None,
                    radarr_id=id if media_type == "movie" else None,
                    force_sync=True,
                    callback=postprocess_callback,
                    # Thread the owning instance (#156) so the subsync
                    # original-language lookup reads the exact owner, not the
                    # default-preferred instance on an upstream-id collision.
                    arr_instance_id=arr_instance_id,
                )
            except OSError:
                return "Unable to edit subtitles file. Check logs.", 409
        elif action == "translate":
            dest_language = language
            # from_language may be pre-set by the embedded track extraction branch above
            from_language = args.get("from_language") or None

            if from_language and (
                len(from_language) != 2 or not alpha3_from_alpha2(from_language)
            ):
                return "from_language must be a valid alpha2 language code", 400

            if not from_language and metadata.subtitles:
                subtitles_list = get_array_from(metadata.subtitles)
                subtitles_filename = os.path.basename(subtitles_path)

                for subtitle_entry in subtitles_list:
                    if len(subtitle_entry) >= 2 and subtitle_entry[1] is not None:
                        db_subtitle_filename = os.path.basename(subtitle_entry[1])
                        if db_subtitle_filename == subtitles_filename:
                            # Remove any suffix (e.g., :hi, :forced) from language code
                            from_language = subtitle_entry[0].split(":")[0]
                            break

            if not from_language or not alpha3_from_alpha2(from_language):
                from_language = subtitles_lang_from_filename(subtitles_path)
            if not from_language or not alpha3_from_alpha2(from_language):
                return "Invalid source language code", 400

            try:
                translate_subtitles_file(
                    video_path=video_path,
                    source_srt_file=subtitles_path,
                    from_lang=from_language,
                    to_lang=dest_language,
                    forced=forced,
                    hi=hi,
                    media_type=media_type,
                    sonarr_series_id=metadata.sonarrSeriesId
                    if media_type == "episode"
                    else None,
                    sonarr_episode_id=id if media_type == "episode" else None,
                    radarr_id=id if media_type == "movie" else None,
                    metadata=metadata,
                )
            except OSError:
                return "Unable to edit subtitles file. Check logs.", 409
        else:
            try:
                subtitles_apply_mods(
                    language=language,
                    subtitle_path=subtitles_path,
                    mods=[action],
                    video_path=video_path,
                )
                postprocess_subtitles(
                    subtitles_path, video_path, media_type, metadata, id,
                    arr_instance_id=arr_instance_id
                )
            except OSError:
                return "Unable to edit subtitles file. Check logs.", 409

        return "", 204


def postprocess_subtitles(subtitles_path, video_path, media_type, metadata, id, arr_instance_id=None):
    # apply chmod if required
    chmod = (
        int(settings.general.chmod, 8)
        if not sys.platform.startswith("win") and settings.general.chmod_enabled
        else None
    )
    if chmod:
        # Path-injection containment for the chmod sink only: a crafted `path` must
        # not steer chmod at an arbitrary file. Only chmod a subtitle that lives
        # where Bazarr stores subtitles for this video (alongside it / a relative
        # subfolder under it, or the configured absolute custom subfolder). This
        # gates ONLY the chmod; the store/refresh below uses video_path and runs
        # regardless. commonpath raises across different Windows drives, so guard it.
        real_sub = os.path.realpath(subtitles_path)
        contained = False
        try:
            video_dir = os.path.realpath(os.path.dirname(video_path))
            contained = os.path.commonpath([video_dir, real_sub]) == video_dir
            if not contained and settings.general.subfolder == "absolute":
                custom = str(settings.general.subfolder_custom).strip()
                if custom:
                    custom_dir = os.path.realpath(custom)
                    contained = os.path.commonpath([custom_dir, real_sub]) == custom_dir
        except ValueError:
            contained = False  # paths on different Windows drives: not contained
        if contained:
            os.chmod(real_sub, chmod)
        else:
            logging.warning(
                "BAZARR refusing to chmod a subtitle outside the video's subtitle folder: %s",
                subtitles_path)

    if media_type == "episode":
        store_subtitles(path_mappings.path_replace_reverse(video_path), video_path)
        event_stream(type="series", payload=metadata.sonarrSeriesId)
        # Emit the LOCAL episode id (#156): `id` is the upstream sonarrEpisodeId
        # (not unique across instances), but the frontend caches episode detail
        # by local id. Resolve it scoped to the owning instance.
        from utilities.media_ids import local_episode_id
        event_stream(type="episode", payload=local_episode_id(id, arr_instance_id))

        if settings.general.use_plex and settings.plex.update_series_library:
            plex_refresh_item(
                metadata.imdbId,
                is_movie=False,
                season=metadata.season,
                episode=metadata.episode,
            )
        if settings.general.use_jellyfin and settings.jellyfin.update_series_library:
            jellyfin_refresh_item(
                metadata.imdbId,
                is_movie=False,
                season=metadata.season,
                episode=metadata.episode,
                tvdb_id=metadata.tvdbId,
            )
    else:
        store_subtitles_movie(
            path_mappings.path_replace_reverse_movie(video_path), video_path
        )
        event_stream(type="movie", payload=id)

        if settings.general.use_plex and settings.plex.update_movie_library:
            plex_refresh_item(metadata.imdbId, is_movie=True)
        if settings.general.use_jellyfin and settings.jellyfin.update_movie_library:
            jellyfin_refresh_item(
                metadata.imdbId, is_movie=True, tmdb_id=metadata.tmdbId
            )


def subtitles_lang_from_filename(path):
    split_extensionless_path = os.path.splitext(path.lower())[0].rsplit(".", 2)

    if len(split_extensionless_path) < 2:
        return None
    elif len(split_extensionless_path) == 2:
        return_lang = split_extensionless_path[-1]
    else:
        first_ext = split_extensionless_path[-1]
        second_ext = split_extensionless_path[-2]

        if first_ext in ["hi", "sdh", "cc"]:
            if alpha3_from_alpha2(second_ext):
                return_lang = second_ext
            else:
                return first_ext
        else:
            return_lang = first_ext

    return return_lang.replace("_", "-")
