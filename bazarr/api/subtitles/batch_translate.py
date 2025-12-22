# coding=utf-8

import logging
from flask_restx import Resource, Namespace, reqparse, fields

from app.jobs_queue import jobs_queue
from ..utils import authenticate

logger = logging.getLogger(__name__)

api_ns_batch_translate = Namespace('BatchTranslate', description='Batch translate subtitles')


@api_ns_batch_translate.route('subtitles/translate/batch')
class BatchTranslate(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('items', type=list, location='json', required=True,
                                     help='List of items to translate')

    post_item_model = api_ns_batch_translate.model('BatchTranslateItem', {
        'type': fields.String(required=True, description='Type: "episode" or "movie"'),
        'sonarrSeriesId': fields.Integer(description='Sonarr Series ID (for episodes)'),
        'sonarrEpisodeId': fields.Integer(description='Sonarr Episode ID (for episodes)'),
        'radarrId': fields.Integer(description='Radarr Movie ID (for movies)'),
        'sourceLanguage': fields.String(required=True, description='Source language code (e.g., "en")'),
        'targetLanguage': fields.String(required=True, description='Target language code (e.g., "hu")'),
        'subtitlePath': fields.String(description='Optional specific subtitle path to translate'),
        'forced': fields.Boolean(default=False, description='Forced subtitle flag'),
        'hi': fields.Boolean(default=False, description='Hearing impaired flag'),
    })

    post_request_model = api_ns_batch_translate.model('BatchTranslateRequest', {
        'items': fields.List(fields.Nested(post_item_model), required=True),
    })

    post_response_model = api_ns_batch_translate.model('BatchTranslateResponse', {
        'queued': fields.Integer(description='Number of items queued'),
        'skipped': fields.Integer(description='Number of items skipped'),
        'errors': fields.List(fields.String(), description='Error messages'),
    })

    @authenticate
    @api_ns_batch_translate.doc(body=post_request_model)
    @api_ns_batch_translate.response(200, 'Success', post_response_model)
    @api_ns_batch_translate.response(400, 'Bad Request')
    @api_ns_batch_translate.response(401, 'Not Authenticated')
    def post(self):
        """Queue batch translation jobs for multiple items"""
        from flask import request
        data = request.get_json()
        
        if not data or 'items' not in data:
            return {'error': 'No items provided'}, 400

        items = data.get('items', [])
        if not items:
            return {'error': 'Empty items list'}, 400

        queued = 0
        skipped = 0
        errors = []

        for item in items:
            try:
                item_type = item.get('type')
                source_language = item.get('sourceLanguage')
                target_language = item.get('targetLanguage')
                forced = item.get('forced', False)
                hi = item.get('hi', False)
                subtitle_path = item.get('subtitlePath')

                if not item_type or not source_language or not target_language:
                    errors.append(f'Missing required fields in item: {item}')
                    skipped += 1
                    continue

                if item_type == 'episode':
                    job_name = f"Translate Episode {item.get('sonarrEpisodeId')}"
                    func = 'process_episode_translation'
                elif item_type == 'movie':
                    job_name = f"Translate Movie {item.get('radarrId')}"
                    func = 'process_movie_translation'
                else:
                    errors.append(f'Invalid type "{item_type}" in item')
                    skipped += 1
                    continue

                # Queue the job individually
                jobs_queue.feed_jobs_pending_queue(
                    job_name=job_name,
                    module='subtitles.tools.translate.batch',
                    func=func,
                    kwargs={
                        'item': item,
                        'source_language': source_language,
                        'target_language': target_language,
                        'forced': forced,
                        'hi': hi,
                        'subtitle_path': subtitle_path
                    }
                )
                queued += 1

            except Exception as e:
                logger.error(f'Error queuing batch translate item: {e}', exc_info=True)
                errors.append(str(e))
                skipped += 1

        return {
            'queued': queued,
            'skipped': skipped,
            'errors': errors
        }, 200
