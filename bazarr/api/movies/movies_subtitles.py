# coding=utf-8

import os

from io import BytesIO
from flask import request
from flask_restx import Resource, Namespace, reqparse
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from werkzeug.datastructures import FileStorage

from app.database import TableMovies, get_profile_id, database, select  # noqa: F401
from arr_instances.resolution import scoped
from utilities.path_mappings import path_mappings
from subtitles.upload import manual_upload_subtitle
from subtitles.mass_download.movies import movie_download_specific_subtitles
from subtitles.download import generate_subtitles  # noqa: F401
from subtitles.tools.delete import delete_subtitles
from subtitles.tools.delete_ownership import SubtitleDeletionError, resolve_subtitle_for_deletion
from subtitles.tools.combine.main import try_combine_for_video
from app.event_handler import event_stream  # noqa: F401
from app.config import settings  # noqa: F401
from app.jobs_queue import jobs_queue  # noqa: F401

from ..utils import (MAX_SUBTITLE_UPLOAD_SIZE, UploadTooLarge, authenticate, read_bounded_upload,
                     upload_declared_too_large, upload_too_large_message)

api_ns_movies_subtitles = Namespace('Movies Subtitles', description='Download, upload or delete movies subtitles')


@api_ns_movies_subtitles.route('movies/subtitles')
class MoviesSubtitles(Resource):
    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument('radarrid', type=int, required=True, help='Movie ID')
    patch_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    patch_request_parser.add_argument('forced', type=str, required=True, help='Forced true/false as string')
    patch_request_parser.add_argument('hi', type=str, required=True, help='HI true/false as string')
    patch_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                      help='Owning Sonarr/Radarr instance id (#156)')

    @authenticate
    @api_ns_movies_subtitles.doc(parser=patch_request_parser)
    @api_ns_movies_subtitles.response(204, 'Success')
    @api_ns_movies_subtitles.response(401, 'Not Authenticated')
    @api_ns_movies_subtitles.response(404, 'Movie not found')
    @api_ns_movies_subtitles.response(409, 'Unable to save subtitles file. Permission or path mapping issue?')
    @api_ns_movies_subtitles.response(500, 'Custom error messages')
    def patch(self):
        """Download a movie subtitles"""
        args = self.patch_request_parser.parse_args()

        movie_download_specific_subtitles(radarr_id=args.get('radarrid'), language=args.get('language'),
                                          hi=args.get('hi').capitalize(),
                                          forced=args.get('forced').capitalize(), job_id=None,
                                          arr_instance_id=args.get('arr_instance_id'))

        return '', 204

    # POST: Upload Subtitles
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('radarrid', type=int, required=True, help='Movie ID')
    post_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    post_request_parser.add_argument('forced', type=str, required=True, help='Forced true/false as string')
    post_request_parser.add_argument('hi', type=str, required=True, help='HI true/false as string')
    post_request_parser.add_argument('file', type=FileStorage, location='files', required=True,
                                     help='Subtitles file as file upload object')
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Sonarr/Radarr instance id (#156)')

    @authenticate
    @api_ns_movies_subtitles.doc(parser=post_request_parser)
    @api_ns_movies_subtitles.response(204, 'Success')
    @api_ns_movies_subtitles.response(400, 'A subtitle of an invalid format was uploaded')
    @api_ns_movies_subtitles.response(401, 'Not Authenticated')
    @api_ns_movies_subtitles.response(404, 'Movie not found')
    @api_ns_movies_subtitles.response(409, 'Unable to save subtitles file. Permission or path mapping issue?')
    @api_ns_movies_subtitles.response(413, 'Subtitle file is too large')
    @api_ns_movies_subtitles.response(500, 'Movie file not found. Path mapping issue?')
    def post(self):
        """Upload a movie subtitles"""
        # Refused from the declared length before the form is parsed.
        if upload_declared_too_large(MAX_SUBTITLE_UPLOAD_SIZE):
            return upload_too_large_message('Subtitle file', MAX_SUBTITLE_UPLOAD_SIZE), 413
        # TODO: Support Multiply Upload
        args = self.post_request_parser.parse_args()

        uploaded_file = args.get('file')
        _, ext = os.path.splitext(uploaded_file.filename)

        if not isinstance(ext, str) or ext.lower() not in SUBTITLE_EXTENSIONS:
            return 'A subtitle of an invalid format was uploaded.', 400

        radarrId = args.get('radarrid')
        arr_instance_id = args.get('arr_instance_id')
        movieInfo = database.execute(scoped(
            select(TableMovies.path, TableMovies.audio_language)
            .where(TableMovies.radarrId == radarrId),
            TableMovies.arr_instance_id, arr_instance_id)) \
            .first()

        if not movieInfo:
            return 'Movie not found', 404

        moviePath = path_mappings.path_replace_movie(movieInfo.path)

        if not os.path.exists(moviePath):
            return 'Movie file not found. Path mapping issue?', 500

        try:
            subtitle_content = BytesIO(read_bounded_upload(uploaded_file, MAX_SUBTITLE_UPLOAD_SIZE))
        except UploadTooLarge:
            return upload_too_large_message('Subtitle file', MAX_SUBTITLE_UPLOAD_SIZE), 413

        manual_upload_subtitle(path=moviePath,
                               language=args.get('language'),
                               forced=True if args.get('forced') == 'true' else False,
                               hi=True if args.get('hi') == 'true' else False,
                               media_type='movie',
                               subtitle=subtitle_content,
                               filename=uploaded_file.filename,
                               audio_language=movieInfo.audio_language,
                               radarrId=radarrId,
                               arr_instance_id=arr_instance_id)

        return '', 204

    # DELETE: Delete Subtitles
    delete_request_parser = reqparse.RequestParser()
    delete_request_parser.add_argument('radarrid', type=int, required=True, help='Movie ID')
    delete_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    delete_request_parser.add_argument('forced', type=str, required=True, help='Forced true/false as string')
    delete_request_parser.add_argument('hi', type=str, required=True, help='HI true/false as string')
    delete_request_parser.add_argument('path', type=str, required=True, help='Path of the subtitles file')
    delete_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                       help='Owning Sonarr/Radarr instance id (#156)')

    @authenticate
    @api_ns_movies_subtitles.doc(parser=delete_request_parser)
    @api_ns_movies_subtitles.response(204, 'Success')
    @api_ns_movies_subtitles.response(401, 'Not Authenticated')
    @api_ns_movies_subtitles.response(403, "Subtitle is not one of this movie's current subtitles")
    @api_ns_movies_subtitles.response(404, 'Movie not found')
    @api_ns_movies_subtitles.response(409, 'Owning instance is ambiguous, or ownership changed before deletion')
    @api_ns_movies_subtitles.response(500, 'Subtitles file not found or permission issue.')
    def delete(self):
        """Delete a movie subtitles"""
        args = self.delete_request_parser.parse_args()
        radarrId = args.get('radarrid')

        try:
            # The path only selects one of this movie's indexed subtitles; the
            # file removed is that entry, mapped through the owning instance.
            target = resolve_subtitle_for_deletion('movie', radarrId, args.get('path'),
                                                   args.get('arr_instance_id'), session=database)
            removed = delete_subtitles(media_type='movie',
                                       language=args.get('language'),
                                       forced=args.get('forced'),
                                       hi=args.get('hi'),
                                       media_path=target.media_path,
                                       subtitles_path=target.stored_path,
                                       radarr_id=radarrId,
                                       arr_instance_id=target.row.arr_instance_id,
                                       revalidate=target.revalidate)
        except SubtitleDeletionError as exc:
            return str(exc), exc.status

        if removed:
            return '', 204
        else:
            return 'Subtitles file not found or permission issue.', 500


@api_ns_movies_subtitles.route('movies/<int:radarr_id>/subtitles/combine')
class MoviesSubtitlesCombine(Resource):
    @authenticate
    @api_ns_movies_subtitles.response(200, 'Result of combine attempt')
    @api_ns_movies_subtitles.response(401, 'Not Authenticated')
    @api_ns_movies_subtitles.response(404, 'Movie not found')
    @api_ns_movies_subtitles.response(500, 'Combine failed')
    def post(self, radarr_id):
        """Build (or rebuild) the combined subtitle file for this movie."""
        payload = request.get_json(silent=True) or {}
        languages = payload.get('languages')
        format_ = payload.get('format')

        arr_instance_id = request.args.get('arr_instance_id', type=int)
        row = database.execute(
            scoped(
                select(TableMovies.path, TableMovies.arr_instance_id).where(TableMovies.radarrId == radarr_id),
                TableMovies.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not row:
            return {'status': 'not_found'}, 404
        arr_instance_id = row.arr_instance_id
        video_path = path_mappings.path_replace_instance(row.path, arr_instance_id, 'movie')

        result = try_combine_for_video(
            video_path=video_path,
            media_type='movies',
            radarr_id=radarr_id,
            sonarr_series_id=None,
            sonarr_episode_id=None,
            languages=languages,
            format=format_,
            arr_instance_id=arr_instance_id,
        )
        body = {
            'status': result.status,
            'path': result.path,
            'alignment': result.alignment,
            'reason': result.reason,
            'error': result.error,
        }
        http_status = 500 if result.status == 'failed' else 200
        return body, http_status
