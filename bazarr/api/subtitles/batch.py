# coding=utf-8

import logging
from flask_restx import Resource, Namespace, fields
from sqlalchemy import and_, or_, func

from app.config import settings
from app.database import TableHistory, TableHistoryMovie, TableEpisodes, TableMovies, database, select
from app.jobs_queue import jobs_queue
from subtitles.mass_operations import mass_batch_operation, VALID_ACTIONS  # noqa: F401
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
    'emoji': 'Removing Emoji',
    'scan-disk': 'Scanning Disk',
    'search-missing': 'Searching Missing Subtitles',
    'upgrade': 'Upgrading Subtitles',
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
        'arr_instance_id': fields.Integer(description='Owning Sonarr/Radarr instance id (#156)'),
    })

    post_options_model = api_ns_batch.model('BatchOptions', {
        'max_offset_seconds': fields.Integer(default=60),
        'no_fix_framerate': fields.Boolean(default=True),
        'gss': fields.Boolean(default=True),
        'force_resync': fields.Boolean(default=False),
        'output_mode': fields.String(description='Sync output mode: overwrite or keep_all'),
        'from_lang': fields.String(description='Source language code for translate action'),
        'to_lang': fields.String(description='Target language code for translate action'),
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

        if not isinstance(items, list):
            return {'error': 'items must be a list'}, 400

        if not items:
            return {'error': 'Empty items list'}, 400

        VALID_ITEM_KEYS = {'type', 'sonarrSeriesId', 'sonarrEpisodeId', 'radarrId', 'arr_instance_id'}
        VALID_TYPES = {'episode', 'movie', 'series'}

        sanitized_items = []
        for item in items:
            if not isinstance(item, dict) or item.get('type') not in VALID_TYPES:
                continue
            sanitized_items.append({k: v for k, v in item.items() if k in VALID_ITEM_KEYS})
        items = sanitized_items

        if not items:
            return {'error': 'No valid items after sanitization'}, 400

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


def get_upgradable_media_ids():
    """Return sets of radarrIds and sonarrSeriesIds that have upgradable subtitles.

    Uses the same latest-row-per-video/language logic as upgrade.py to avoid
    false positives from old history entries that have already been superseded.
    """
    if not settings.general.upgrade_subs:
        return {'movies': [], 'series': []}

    from subtitles.upgrade import get_queries_condition_parameters
    minimum_timestamp, query_actions = get_queries_condition_parameters()

    # Movies: only consider the latest eligible history row per (video_path, language)
    max_movie_ts = select(
        TableHistoryMovie.video_path,
        TableHistoryMovie.language,
        func.max(TableHistoryMovie.timestamp).label('timestamp')
    ).where(
        TableHistoryMovie.action.in_(query_actions)
    ).group_by(
        TableHistoryMovie.video_path, TableHistoryMovie.language
    ).distinct().subquery()

    movie_results = database.execute(
        select(TableHistoryMovie.radarrId)
        .distinct()
        .join(TableMovies, TableHistoryMovie.radarrId == TableMovies.radarrId)
        .join(max_movie_ts, onclause=and_(
            TableHistoryMovie.video_path == max_movie_ts.c.video_path,
            TableHistoryMovie.language == max_movie_ts.c.language,
            TableHistoryMovie.timestamp == max_movie_ts.c.timestamp,
        ))
        .where(and_(
            TableHistoryMovie.action.in_(query_actions),
            TableHistoryMovie.timestamp > minimum_timestamp,
            or_(
                and_(TableHistoryMovie.score.is_(None), TableHistoryMovie.action == 6),
                TableHistoryMovie.score < TableHistoryMovie.score_out_of - 3
            )
        ))
    ).all()
    movie_ids = [r.radarrId for r in movie_results]

    # Series: only consider the latest eligible history row per (video_path, language)
    max_episode_ts = select(
        TableHistory.video_path,
        TableHistory.language,
        func.max(TableHistory.timestamp).label('timestamp')
    ).where(
        TableHistory.action.in_(query_actions)
    ).group_by(
        TableHistory.video_path, TableHistory.language
    ).distinct().subquery()

    series_results = database.execute(
        select(TableHistory.sonarrSeriesId)
        .distinct()
        .join(TableEpisodes, TableHistory.sonarrEpisodeId == TableEpisodes.sonarrEpisodeId)
        .join(max_episode_ts, onclause=and_(
            TableHistory.video_path == max_episode_ts.c.video_path,
            TableHistory.language == max_episode_ts.c.language,
            TableHistory.timestamp == max_episode_ts.c.timestamp,
        ))
        .where(and_(
            TableHistory.action.in_(query_actions),
            TableHistory.timestamp > minimum_timestamp,
            or_(
                and_(TableHistory.score.is_(None), TableHistory.action == 6),
                TableHistory.score < TableHistory.score_out_of - 3
            )
        ))
    ).all()
    series_ids = [r.sonarrSeriesId for r in series_results]

    return {'movies': movie_ids, 'series': series_ids}


@api_ns_batch.route('subtitles/upgradable')
class UpgradableMedia(Resource):
    @authenticate
    def get(self):
        """Return movie and series IDs that have upgradable subtitles"""
        return get_upgradable_media_ids(), 200
