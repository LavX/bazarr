# coding=utf-8

import logging
from flask_restx import Resource, Namespace, reqparse, fields

from app.database import TableEpisodes, TableMovies, database, select
from utilities.path_mappings import path_mappings
from subtitles.tools.translate.main import translate_subtitles_file
from app.config import settings

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
                    result = self._process_episode(item, source_language, target_language, 
                                                   forced, hi, subtitle_path)
                elif item_type == 'movie':
                    result = self._process_movie(item, source_language, target_language,
                                                 forced, hi, subtitle_path)
                else:
                    errors.append(f'Invalid type "{item_type}" in item')
                    skipped += 1
                    continue

                if result.get('queued'):
                    queued += 1
                else:
                    skipped += 1
                    if result.get('error'):
                        errors.append(result['error'])

            except Exception as e:
                logger.error(f'Error processing batch translate item: {e}', exc_info=True)
                errors.append(str(e))
                skipped += 1

        return {
            'queued': queued,
            'skipped': skipped,
            'errors': errors
        }, 200

    def _process_episode(self, item, source_language, target_language, forced, hi, subtitle_path):
        """Process a single episode for translation"""
        sonarr_series_id = item.get('sonarrSeriesId')
        sonarr_episode_id = item.get('sonarrEpisodeId')

        if not sonarr_series_id or not sonarr_episode_id:
            return {'queued': False, 'error': 'Missing sonarrSeriesId or sonarrEpisodeId'}

        # Get episode info from database
        episode = database.execute(
            select(TableEpisodes.path, TableEpisodes.subtitles, TableEpisodes.sonarrSeriesId)
            .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id)
        ).first()

        if not episode:
            return {'queued': False, 'error': f'Episode {sonarr_episode_id} not found'}

        video_path = path_mappings.path_replace(episode.path)

        # Find source subtitle
        source_subtitle_path = subtitle_path
        if not source_subtitle_path:
            source_subtitle_path = self._find_subtitle_by_language(
                episode.subtitles, source_language, video_path
            )

        if not source_subtitle_path:
            return {
                'queued': False, 
                'error': f'No {source_language} subtitle found for episode {sonarr_episode_id}'
            }

        # Queue translation
        try:
            result = translate_subtitles_file(
                video_path=video_path,
                source_srt_file=source_subtitle_path,
                from_lang=source_language,
                to_lang=target_language,
                forced=forced,
                hi=hi,
                media_type="series",
                sonarr_series_id=sonarr_series_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_id=None
            )
            return {'queued': result is not False}
        except Exception as e:
            logger.error(f'Translation failed for episode {sonarr_episode_id}: {e}')
            return {'queued': False, 'error': str(e)}

    def _process_movie(self, item, source_language, target_language, forced, hi, subtitle_path):
        """Process a single movie for translation"""
        radarr_id = item.get('radarrId')

        if not radarr_id:
            return {'queued': False, 'error': 'Missing radarrId'}

        # Get movie info from database
        movie = database.execute(
            select(TableMovies.path, TableMovies.subtitles)
            .where(TableMovies.radarrId == radarr_id)
        ).first()

        if not movie:
            return {'queued': False, 'error': f'Movie {radarr_id} not found'}

        video_path = path_mappings.path_replace_movie(movie.path)

        # Find source subtitle
        source_subtitle_path = subtitle_path
        if not source_subtitle_path:
            source_subtitle_path = self._find_subtitle_by_language(
                movie.subtitles, source_language, video_path
            )

        if not source_subtitle_path:
            return {
                'queued': False,
                'error': f'No {source_language} subtitle found for movie {radarr_id}'
            }

        # Queue translation  
        try:
            result = translate_subtitles_file(
                video_path=video_path,
                source_srt_file=source_subtitle_path,
                from_lang=source_language,
                to_lang=target_language,
                forced=forced,
                hi=hi,
                media_type="movies",
                sonarr_series_id=None,
                sonarr_episode_id=None,
                radarr_id=radarr_id
            )
            return {'queued': result is not False}
        except Exception as e:
            logger.error(f'Translation failed for movie {radarr_id}: {e}')
            return {'queued': False, 'error': str(e)}

    def _find_subtitle_by_language(self, subtitles, language_code, video_path):
        """Find a subtitle file by language code from the subtitles list"""
        import json
        import os

        logger.debug(f'Looking for "{language_code}" subtitle. Subtitles data type: {type(subtitles)}')

        if not subtitles:
            logger.debug('No subtitles data found in database for this media')
            return None

        # Parse subtitles if it's a string (JSON)
        if isinstance(subtitles, str):
            try:
                subtitles = json.loads(subtitles)
            except json.JSONDecodeError:
                logger.error('Failed to parse subtitles JSON from database')
                return None

        if not isinstance(subtitles, list):
            logger.debug(f'Subtitles is not a list: {type(subtitles)}')
            return None

        logger.debug(f'Found {len(subtitles)} subtitle(s) in database')

        # Collect available language codes for better error reporting
        available_codes = []
        for sub in subtitles:
            if isinstance(sub, dict):
                code = sub.get('code2', '')
                if code:
                    available_codes.append(code)

        if available_codes:
            logger.info(f'Available subtitle language codes: {available_codes}')
        else:
            logger.warning('No language codes found in subtitle data')

        # Check if requested language exists
        if language_code not in available_codes:
            logger.warning(f'Requested language code "{language_code}" not in available codes: {available_codes}. '
                          f'Make sure you\'re using the correct 2-letter language code (e.g., "en" not "English").')

        # Look for matching subtitle
        for sub in subtitles:
            sub_code = sub.get('code2', '')
            sub_path = sub.get('path', '')
            
            logger.debug(f'Checking subtitle: code2="{sub_code}", path="{sub_path}"')
            
            if sub_code == language_code and sub_path:
                # Apply path mapping if needed
                mapped_path = path_mappings.path_replace_movie(sub_path)
                logger.debug(f'Mapped path: {mapped_path}')
                
                # Check if file exists
                if os.path.exists(mapped_path):
                    logger.debug(f'Found matching subtitle at {mapped_path}')
                    return mapped_path
                elif os.path.exists(sub_path):
                    logger.debug(f'Found matching subtitle at original path {sub_path}')
                    return sub_path
                else:
                    logger.warning(f'Subtitle path does not exist: {mapped_path} (original: {sub_path})')
                    
        logger.warning(f'No "{language_code}" subtitle found. Available languages: {available_codes}')
        return None