# coding=utf-8

import os

from io import BytesIO
from flask import request
from flask_restx import Resource, Namespace, reqparse
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from werkzeug.datastructures import FileStorage

from app.database import TableShows, TableEpisodes, get_profile_id, database, select  # noqa: F401
from arr_instances.resolution import scoped
from utilities.path_mappings import path_mappings
from subtitles.upload import manual_upload_subtitle
from subtitles.mass_download.series import episode_download_specific_subtitles
from subtitles.download import generate_subtitles  # noqa: F401
from subtitles.tools.delete import delete_subtitles
from subtitles.tools.combine.main import try_combine_for_video
from app.jobs_queue import jobs_queue  # noqa: F401
from app.event_handler import event_stream  # noqa: F401
from app.config import settings  # noqa: F401

from ..utils import authenticate

api_ns_episodes_subtitles = Namespace('Episodes Subtitles', description='Download, upload or delete episodes subtitles')


@api_ns_episodes_subtitles.route('episodes/subtitles')
class EpisodesSubtitles(Resource):
    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument('seriesid', type=int, required=True, help='Series ID')
    patch_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    patch_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    patch_request_parser.add_argument('forced', type=str, required=True, help='Forced true/false as string')
    patch_request_parser.add_argument('hi', type=str, required=True, help='HI true/false as string')

    @authenticate
    @api_ns_episodes_subtitles.doc(parser=patch_request_parser)
    @api_ns_episodes_subtitles.response(204, 'Success')
    @api_ns_episodes_subtitles.response(401, 'Not Authenticated')
    @api_ns_episodes_subtitles.response(404, 'Episode not found')
    @api_ns_episodes_subtitles.response(409, 'Unable to save subtitles file. Permission or path mapping issue?')
    @api_ns_episodes_subtitles.response(500, 'Custom error messages')
    def patch(self):
        """Download an episode subtitles"""
        args = self.patch_request_parser.parse_args()

        episode_download_specific_subtitles(sonarr_series_id=args.get('seriesid'),
                                            sonarr_episode_id=args.get('episodeid'),
                                            language=args.get('language'), hi=args.get('hi').capitalize(),
                                            forced=args.get('forced').capitalize(), job_id=None)

        return '', 204

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('seriesid', type=int, required=True, help='Series ID')
    post_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    post_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    post_request_parser.add_argument('forced', type=str, required=True, help='Forced true/false as string')
    post_request_parser.add_argument('hi', type=str, required=True, help='HI true/false as string')
    post_request_parser.add_argument('file', type=FileStorage, location='files', required=True,
                                     help='Subtitles file as file upload object')
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Sonarr/Radarr instance id (#156)')

    @authenticate
    @api_ns_episodes_subtitles.doc(parser=post_request_parser)
    @api_ns_episodes_subtitles.response(204, 'Success')
    @api_ns_episodes_subtitles.response(401, 'Not Authenticated')
    @api_ns_episodes_subtitles.response(404, 'Episode not found')
    @api_ns_episodes_subtitles.response(409, 'Unable to save subtitles file. Permission or path mapping issue?')
    @api_ns_episodes_subtitles.response(500, 'Episode file not found. Path mapping issue?')
    def post(self):
        """Upload an episode subtitles"""
        args = self.post_request_parser.parse_args()

        uploaded_file = args.get('file')
        _, ext = os.path.splitext(uploaded_file.filename)

        if not isinstance(ext, str) or ext.lower() not in SUBTITLE_EXTENSIONS:
            raise ValueError('A subtitle of an invalid format was uploaded.')

        sonarrSeriesId = args.get('seriesid')
        sonarrEpisodeId = args.get('episodeid')
        arr_instance_id = args.get('arr_instance_id')
        episodeInfo = database.execute(scoped(
            select(TableEpisodes.path,
                   TableEpisodes.audio_language)
            .where(TableEpisodes.sonarrEpisodeId == sonarrEpisodeId),
            TableEpisodes.arr_instance_id, arr_instance_id)) \
            .first()

        if not episodeInfo:
            return 'Episode not found', 404

        episodePath = path_mappings.path_replace(episodeInfo.path)

        if not os.path.exists(episodePath):
            return 'Episode file not found. Path mapping issue?', 500

        subtitle_content = BytesIO(uploaded_file.read())

        manual_upload_subtitle(path=episodePath,
                               language=args.get('language'),
                               forced=True if args.get('forced') == 'true' else False,
                               hi=True if args.get('hi') == 'true' else False,
                               media_type='series',
                               subtitle=subtitle_content,
                               filename=uploaded_file.filename,
                               audio_language=episodeInfo.audio_language,
                               sonarrSeriesId=sonarrSeriesId,
                               sonarrEpisodeId=sonarrEpisodeId,
                               arr_instance_id=arr_instance_id)

        return '', 204

    delete_request_parser = reqparse.RequestParser()
    delete_request_parser.add_argument('seriesid', type=int, required=True, help='Series ID')
    delete_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    delete_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    delete_request_parser.add_argument('forced', type=str, required=True, help='Forced true/false as string')
    delete_request_parser.add_argument('hi', type=str, required=True, help='HI true/false as string')
    delete_request_parser.add_argument('path', type=str, required=True, help='Path of the subtitles file')
    delete_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                       help='Owning Sonarr/Radarr instance id (#156)')

    @authenticate
    @api_ns_episodes_subtitles.doc(parser=delete_request_parser)
    @api_ns_episodes_subtitles.response(204, 'Success')
    @api_ns_episodes_subtitles.response(401, 'Not Authenticated')
    @api_ns_episodes_subtitles.response(404, 'Episode not found')
    @api_ns_episodes_subtitles.response(500, 'Subtitles file not found or permission issue.')
    def delete(self):
        """Delete an episode subtitles"""
        args = self.delete_request_parser.parse_args()
        sonarrSeriesId = args.get('seriesid')
        sonarrEpisodeId = args.get('episodeid')
        arr_instance_id = args.get('arr_instance_id')
        episodeInfo = database.execute(scoped(
            select(TableEpisodes.path)
            .where(TableEpisodes.sonarrEpisodeId == sonarrEpisodeId),
            TableEpisodes.arr_instance_id, arr_instance_id)) \
            .first()

        if not episodeInfo:
            return 'Episode not found', 404

        episodePath = path_mappings.path_replace(episodeInfo.path)

        language = args.get('language')
        forced = args.get('forced')
        hi = args.get('hi')
        subtitlesPath = args.get('path')

        subtitlesPath = path_mappings.path_replace_reverse(subtitlesPath)

        if delete_subtitles(media_type='series',
                            language=language,
                            forced=forced,
                            hi=hi,
                            media_path=episodePath,
                            subtitles_path=subtitlesPath,
                            sonarr_series_id=sonarrSeriesId,
                            sonarr_episode_id=sonarrEpisodeId,
                            arr_instance_id=arr_instance_id):
            return '', 204
        else:
            return 'Subtitles file not found or permission issue.', 500


@api_ns_episodes_subtitles.route('episodes/<int:episode_id>/subtitles/combine')
class EpisodesSubtitlesCombine(Resource):
    @authenticate
    @api_ns_episodes_subtitles.response(200, 'Result of combine attempt')
    @api_ns_episodes_subtitles.response(401, 'Not Authenticated')
    @api_ns_episodes_subtitles.response(404, 'Episode not found')
    @api_ns_episodes_subtitles.response(500, 'Combine failed')
    def post(self, episode_id):
        """Build (or rebuild) the combined subtitle file for this episode."""
        payload = request.get_json(silent=True) or {}
        languages = payload.get('languages')
        format_ = payload.get('format')

        row = database.execute(
            select(TableEpisodes.path, TableEpisodes.sonarrSeriesId)
            .where(TableEpisodes.sonarrEpisodeId == episode_id)
        ).first()
        if not row:
            return {'status': 'not_found'}, 404
        video_path = path_mappings.path_replace(row.path)

        result = try_combine_for_video(
            video_path=video_path,
            media_type='series',
            radarr_id=None,
            sonarr_series_id=row.sonarrSeriesId,
            sonarr_episode_id=episode_id,
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
