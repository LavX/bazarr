# coding=utf-8

import logging
from flask_restx import Resource, Namespace, fields

from app.jobs_queue import jobs_queue
from subtitles.mass_operations import mass_batch_operation, VALID_ACTIONS
from ..utils import authenticate

ACTION_LABELS = {
    'sync': 'Syncing Subtitles',
    'translate': 'Translating Subtitles',
    'OCR_fixes': 'Applying OCR Fixes',
    'common': 'Applying Common Fixes',
    'remove_HI': 'Removing Hearing Impaired Tags',
    'remove_tags': 'Removing Style Tags',
    'fix_uppercase': 'Fixing Uppercase',
    'reverse_rtl': 'Reversing RTL',
    'scan-disk': 'Scanning Disk',
    'search-missing': 'Searching Missing Subtitles',
}

logger = logging.getLogger(__name__)

api_ns_batch = Namespace('Batch', description='Unified batch subtitle operations')


@api_ns_batch.route('subtitles/batch')
class BatchOperation(Resource):
    post_item_model = api_ns_batch.model('BatchItem', {
        'type': fields.String(required=True, description='Type: "episode", "movie", or "series"'),
        'sonarrSeriesId': fields.Integer(description='Sonarr Series ID'),
        'sonarrEpisodeId': fields.Integer(description='Sonarr Episode ID'),
        'radarrId': fields.Integer(description='Radarr Movie ID'),
        'sourceLanguage': fields.String(description='Source language (translate only)'),
        'targetLanguage': fields.String(description='Target language (translate only)'),
        'forced': fields.Boolean(description='Forced subtitle flag'),
        'hi': fields.Boolean(description='Hearing impaired flag'),
    })

    post_options_model = api_ns_batch.model('BatchOptions', {
        'max_offset_seconds': fields.Integer(default=60),
        'no_fix_framerate': fields.Boolean(default=True),
        'gss': fields.Boolean(default=True),
        'force_resync': fields.Boolean(default=False),
    })

    post_request_model = api_ns_batch.model('BatchRequest', {
        'items': fields.List(fields.Nested(post_item_model), required=True),
        'action': fields.String(required=True, description=f'Action: one of {", ".join(sorted(VALID_ACTIONS))}'),
        'options': fields.Nested(post_options_model),
    })

    post_response_model = api_ns_batch.model('BatchResponse', {
        'queued': fields.Integer(description='Number of items processed'),
        'skipped': fields.Integer(description='Number of items skipped'),
        'errors': fields.List(fields.String(), description='Error messages'),
    })

    @authenticate
    @api_ns_batch.doc(body=post_request_model)
    @api_ns_batch.response(200, 'Success', post_response_model)
    @api_ns_batch.response(400, 'Bad Request')
    @api_ns_batch.response(401, 'Not Authenticated')
    def post(self):
        """Execute a batch operation on multiple items"""
        from flask import request
        data = request.get_json()

        if not data:
            return {'error': 'No data provided'}, 400

        action = data.get('action')
        if not action or action not in VALID_ACTIONS:
            return {'error': f'Invalid action. Must be one of: {", ".join(sorted(VALID_ACTIONS))}'}, 400

        items = data.get('items')
        if items is None:
            return {'error': 'No items provided'}, 400

        if not items:
            return {'error': 'Empty items list'}, 400

        MAX_BATCH_SIZE = 10000

        if len(items) > MAX_BATCH_SIZE:
            return {'error': f'Batch size exceeds maximum of {MAX_BATCH_SIZE}'}, 400

        options = data.get('options', {})

        label = ACTION_LABELS.get(action, f'Batch {action}')
        job_name = f"{label} ({len(items)} items)"

        job_id = jobs_queue.feed_jobs_pending_queue(
            job_name=job_name,
            module='subtitles.mass_operations',
            func='mass_batch_operation',
            kwargs={
                'items': items,
                'action': action,
                'options': options,
            },
            is_progress=True,
        )

        return {'queued': len(items), 'skipped': 0, 'errors': [], 'job_id': job_id}, 200
