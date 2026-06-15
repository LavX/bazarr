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
from subtitles.tools.combine.main import try_combine_for_video
from app.event_handler import event_stream  # noqa: F401
from app.config import settings  # noqa: F401
from app.jobs_queue import jobs_queue  # noqa: F401

from ..utils import authenticate

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
    @api_ns_movies_subtitles.response(401, 'Not Authenticated')
    @api_ns_movies_subtitles.response(404, 'Movie not found')
    @api_ns_movies_subtitles.response(409, 'Unable to save subtitles file. Permission or path mapping issue?')
    @api_ns_movies_subtitles.response(500, 'Movie file not found. Path mapping issue?')
    def post(self):
        """Upload a movie subtitles"""
        # TODO: Support Multiply Upload
        args = self.post_request_parser.parse_args()

        uploaded_file = args.get('file')
        _, ext = os.path.splitext(uploaded_file.filename)

        if not isinstance(ext, str) or ext.lower() not in SUBTITLE_EXTENSIONS:
            raise ValueError('A subtitle of an invalid format was uploaded.')

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

        subtitle_content = BytesIO(uploaded_file.read())

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
    @api_ns_movies_subtitles.response(404, 'Movie not found')
    @api_ns_movies_subtitles.response(500, 'Subtitles file not found or permission issue.')
    def delete(self):
        """Delete a movie subtitles"""
        args = self.delete_request_parser.parse_args()
        radarrId = args.get('radarrid')
        arr_instance_id = args.get('arr_instance_id')
        movieInfo = database.execute(scoped(
            select(TableMovies.path)
            .where(TableMovies.radarrId == radarrId),
            TableMovies.arr_instance_id, arr_instance_id)) \
            .first()

        if not movieInfo:
            return 'Movie not found', 404

        moviePath = path_mappings.path_replace_movie(movieInfo.path)

        language = args.get('language')
        forced = args.get('forced')
        hi = args.get('hi')
        subtitlesPath = args.get('path')

        subtitlesPath = path_mappings.path_replace_reverse_movie(subtitlesPath)

        if delete_subtitles(media_type='movie',
                            language=language,
                            forced=forced,
                            hi=hi,
                            media_path=moviePath,
                            subtitles_path=subtitlesPath,
                            radarr_id=radarrId,
                            arr_instance_id=arr_instance_id):
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
                select(TableMovies.path).where(TableMovies.radarrId == radarr_id),
                TableMovies.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not row:
            return {'status': 'not_found'}, 404
        video_path = path_mappings.path_replace_movie(row.path)

        result = try_combine_for_video(
            video_path=video_path,
            media_type='movies',
            radarr_id=radarr_id,
            sonarr_series_id=None,
            sonarr_episode_id=None,
            languages=languages,
            format=format_,
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
