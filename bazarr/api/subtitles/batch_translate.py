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
        detected_source_lang = None
        if not source_subtitle_path:
            source_subtitle_path, detected_source_lang = self._find_subtitle_by_language(
                episode.subtitles, source_language, video_path, media_type='series'
            )

        if not source_subtitle_path:
            return {
                'queued': False,
                'error': f'No subtitle found for episode {sonarr_episode_id} (requested source: {source_language})'
            }

        # Use detected language if available
        if detected_source_lang:
            source_language = detected_source_lang

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
        detected_source_lang = None
        if not source_subtitle_path:
            source_subtitle_path, detected_source_lang = self._find_subtitle_by_language(
                movie.subtitles, source_language, video_path, media_type='movie'
            )

        if not source_subtitle_path:
            return {
                'queued': False,
                'error': f'No subtitle found for movie {radarr_id} (requested source: {source_language})'
            }

        # Use detected language if available
        if detected_source_lang:
            source_language = detected_source_lang

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

    def _find_subtitle_by_language(self, subtitles, language_code, video_path, media_type='movie'):
        """Find a subtitle file by language code from the subtitles list.
        
        If no exact language match is found, falls back to using any available subtitle.
        This is useful when the source language detection might differ or when users
        want to translate from whatever subtitle is available.
        
        Args:
            subtitles: List of subtitle dictionaries or JSON string
            language_code: Preferred source language code (e.g., "en")
            video_path: Path to the video file
            media_type: Either 'movie' or 'series' for correct path mapping
            
        Returns:
            Tuple of (Path to the subtitle file, detected language code), or (None, None) if no subtitles available
        """
        import ast
        import os

        logger.debug(f'Looking for "{language_code}" subtitle. Subtitles data type: {type(subtitles)}')

        available_subtitles = []

        if subtitles:
            # Parse subtitles if it's a string (Python literal from DB)
            if isinstance(subtitles, str):
                try:
                    subtitles = ast.literal_eval(subtitles)
                except (ValueError, SyntaxError):
                    logger.error('Failed to parse subtitles from database')
                    subtitles = []

            if isinstance(subtitles, list):
                logger.debug(f'Found {len(subtitles)} subtitle(s) in database')

                # Collect available subtitles with their paths for better processing
                for sub in subtitles:
                    # DB format is [lang_str, path, size]
                    if isinstance(sub, (list, tuple)) and len(sub) >= 2:
                        lang_parts = sub[0].split(':')
                        sub_code = lang_parts[0]
                        sub_path = sub[1]
                        sub_hi = len(lang_parts) > 1 and lang_parts[1].lower() == 'hi'
                        sub_forced = len(lang_parts) > 1 and lang_parts[1].lower() == 'forced'
                        
                        if sub_path:
                            available_subtitles.append({
                                'code2': sub_code,
                                'path': sub_path,
                                'hi': sub_hi,
                                'forced': sub_forced
                            })

        # Helper function to resolve and validate subtitle path
        def resolve_subtitle_path(sub_path):
            # Apply path mapping based on media type
            if media_type == 'series':
                mapped_path = path_mappings.path_replace(sub_path)
            else:
                mapped_path = path_mappings.path_replace_movie(sub_path)
            
            logger.debug(f'Checking path: {mapped_path} (original: {sub_path})')
            
            # Check if file exists at mapped path
            if os.path.exists(mapped_path):
                return mapped_path
            # Fallback to original path
            elif os.path.exists(sub_path):
                return sub_path
            
            return None

        # First pass: Look for exact language match in DB
        exact_matches = [s for s in available_subtitles if s['code2'] == language_code]
        
        # Sort matches: prefer non-HI, non-forced first, then HI, then forced
        exact_matches.sort(key=lambda x: (x['forced'], x['hi']))
        
        for sub in exact_matches:
            resolved_path = resolve_subtitle_path(sub['path'])
            if resolved_path:
                logger.info(f'Found exact language match "{language_code}" at {resolved_path} '
                           f'(hi={sub["hi"]}, forced={sub["forced"]})')
                return resolved_path, sub['code2']

        # Second pass: If no exact match found in DB, try any available subtitle from DB
        if available_subtitles:
            logger.info(f'No exact match for "{language_code}" found in DB. '
                       f'Falling back to any available subtitle from DB.')
            
            # Sort all available: prefer non-HI, non-forced, and prioritize common languages
            common_languages = ['en', 'eng']  # English often has good quality subs
            
            def sort_key(sub):
                is_common = sub['code2'] in common_languages
                return (sub['forced'], sub['hi'], not is_common)
            
            available_subtitles.sort(key=sort_key)
            
            for sub in available_subtitles:
                resolved_path = resolve_subtitle_path(sub['path'])
                if resolved_path:
                    logger.warning(f'Using fallback subtitle with language "{sub["code2"]}" at {resolved_path} '
                                  f'(hi={sub["hi"]}, forced={sub["forced"]}). '
                                  f'Requested language was "{language_code}".')
                    return resolved_path, sub['code2']

        # Third pass: Scan filesystem fallback
        logger.info(f'No usable subtitle found in DB. Scanning filesystem near {video_path}')
        filesystem_subs = self._scan_filesystem_for_subtitles(video_path)
        
        if filesystem_subs:
            # Prefer English
            for sub in filesystem_subs:
                if sub['is_english']:
                    logger.info(f'Found English subtitle on filesystem: {sub["path"]}')
                    return sub['path'], 'en'
            
            # Use first available
            sub = filesystem_subs[0]
            logger.info(f'Using non-English subtitle from filesystem: {sub["path"]} (detected: {sub["detected_language"]})')
            return sub['path'], sub['detected_language']
        
        logger.warning(f'No usable subtitle files found in DB or on filesystem.')
        return None, None

    def _scan_filesystem_for_subtitles(self, video_path):
        """Scan filesystem for .srt files next to the video file."""
        import os
        import re

        ENGLISH_PATTERNS = [
            r'\.en\.srt$', r'\.eng\.srt$', r'\.english\.srt$',
            r'[._-]en[._-]', r'[._-]eng[._-]', r'[._-]english[._-]',
        ]
        
        video_dir = os.path.dirname(video_path)
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        results = []
        
        # Search directories
        search_dirs = [video_dir]
        for subfolder in ['Subs', 'Subtitles', 'subs', 'subtitles', video_name]:
            subdir = os.path.join(video_dir, subfolder)
            if os.path.isdir(subdir):
                search_dirs.append(subdir)
        
        for directory in search_dirs:
            try:
                for filename in os.listdir(directory):
                    if filename.lower().endswith('.srt'):
                        full_path = os.path.join(directory, filename)
                        
                        # Detect language from filename
                        is_english = any(re.search(p, filename.lower()) for p in ENGLISH_PATTERNS)
                        detected_lang = 'en' if is_english else self._detect_language_from_content(full_path)
                        
                        results.append({
                            'path': full_path,
                            'filename': filename,
                            'is_english': is_english or detected_lang == 'en',
                            'detected_language': detected_lang or 'und'
                        })
            except OSError:
                continue
        
        # Sort: English first
        results.sort(key=lambda x: (not x['is_english'], x['filename']))
        return results

    def _detect_language_from_content(self, srt_path):
        """Detect language by analyzing subtitle content."""
        from guess_language import guess_language
        from charset_normalizer import detect
        try:
            with open(srt_path, 'rb') as f:
                raw = f.read(8192)  # Read first 8KB
            
            encoding = detect(raw)
            if encoding and encoding.get('encoding'):
                text = raw.decode(encoding['encoding'], errors='ignore')
                return guess_language(text)
        except Exception:
            pass
        return None
